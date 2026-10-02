"""Per-model load flow execution and the base-case (N-0) comparison dataframes."""

import logging
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pypowsybl as pp

from rosc_acdc import config, models

logger = logging.getLogger(__name__)

# Element kinds compared in the base case, and the label used in log messages.
KIND_LABELS = {
    "lines": "line",
    "transformers": "2-winding transformer",
    "transformers3": "3-winding transformer",
}

# IIDM side label -> the column suffix pypowsybl uses for that side (i1/p1, i2/p2, i3/p3).
SIDE_COLUMN_SUFFIX = {"ONE": 1, "TWO": 2, "THREE": 3}


@dataclass
class ModelRun:
    """What one model's load flow produced."""

    spec: models.ModelSpec
    result: list
    lf_time: float
    flows: dict = field(default_factory=dict)  # element kind -> current (A) per (element, side)


def _by_side(current_per_side):
    """Turn {side: current per element} into one row per (element_id, side)."""
    return (
        pd.DataFrame(current_per_side)
        .rename_axis(index="element_id", columns="side")
        .stack()
    )


def branch_current(elements_df):
    """AC current (A) per side, as left on the network by an AC load flow.

    One row per (element, side) - the shape security_analysis.branch_results() uses - so
    each side can be compared against its own thermal limit.
    """
    return _by_side({
        side: elements_df[f"i{suffix}"].abs()
        for side, suffix in SIDE_COLUMN_SUFFIX.items() if f"i{suffix}" in elements_df
    })


def branch_current_dc(elements_df, voltage_levels):
    """DC current (A) per side, rebuilt from the DC active flow: |P| / (sqrt(3) * Vnom).

    A DC load flow leaves i1/i2/i3 (and q1) unset on the network, so the current is derived
    from the nominal voltage of each side - the same convention pypowsybl uses for DC
    security-analysis branch results, which keeps the base case comparable with the SA
    dataset. Each side is taken separately because each carries its own voltage level.
    """
    currents = {}
    for side, suffix in SIDE_COLUMN_SUFFIX.items():
        if f"p{suffix}" not in elements_df:
            continue
        nominal_v = elements_df[f"voltage_level{suffix}_id"].map(voltage_levels["nominal_v"])
        currents[side] = elements_df[f"p{suffix}"].abs().mul(1000).div(np.sqrt(3) * nominal_v)
    return _by_side(currents)


def permanent_current_limits(limits):
    """One permanent CURRENT rating per (element_id, side); the most restrictive wins.

    CIM current limits are terminal-specific, so a branch can be rated differently on each
    side. Only CURRENT-type permanent limits are selected: comparing a current against an
    MVA or MW rating would corrupt every loading, margin and violation flag derived from it.
    """
    patl = limits[(limits["acceptable_duration"] == -1) & (limits["type"] == "CURRENT")]
    return patl.groupby(["element_id", "side"])["value"].min()


def binding_sides(currents, patl_per_side):
    """element_id -> the side whose loading against its own PATL is highest.

    The same rule `_compare_models()` applies below, extracted so it can be asked of any
    element rather than only the ones that reach the base-case comparison. The KPI workbook
    needs it for the SA population too: an SA row exists per (element, side), but the element
    has to be attributed to a single country and voltage level for grouping, and the binding
    side is the choice the base case already makes. Feed it the reference model's currents,
    for the same reason the base case ranks on the reference.

    Computed before any loading filter, so lightly loaded elements still resolve. An element
    with no rated side at all is absent from the result; the caller decides what to do with it.
    """
    loading = currents.div(patl_per_side.reindex(currents.index)).dropna()
    if loading.empty:
        return pd.Series(dtype=object)
    return loading.groupby(level="element_id").idxmax().map(lambda key: key[1])


def _flow_columns(elements_df, spec):
    """The columns branch_current / branch_current_dc read, for the sides this kind has."""
    if spec.dc:
        return [
            column
            for suffix in SIDE_COLUMN_SUFFIX.values()
            for column in (f"p{suffix}", f"voltage_level{suffix}_id")
            if column in elements_df
        ]
    return [f"i{suffix}" for suffix in SIDE_COLUMN_SUFFIX.values() if f"i{suffix}" in elements_df]


def _snapshot_flows(network, spec, voltage_levels):
    """Re-read the monitored elements' currents (A) per side from the current variant.

    Element classification is state-independent and done once by network_io.get_network_items;
    only the flows have to be read again per model, and they are read for every element of
    each kind - the base case slices out the HV subset itself, and the KPI workbook needs the
    RCC elements' reference currents too. The fillna(0) reproduces what get_network_items
    applied to the whole frame: it keeps a disconnected or unsolved element at 0 A instead of
    dropping it out of the comparison as if it were unrated.
    """
    getters = {
        "lines": network.get_lines,
        "transformers": network.get_2_windings_transformers,
        "transformers3": network.get_3_windings_transformers,
    }
    flows = {}
    for kind, getter in getters.items():
        elements = getter()
        elements = elements[_flow_columns(elements, spec)].fillna(0)
        flows[kind] = (
            branch_current_dc(elements, voltage_levels) if spec.dc else branch_current(elements)
        )
    return flows


def _log_status(spec, result):
    """Log the main component's convergence, and warn when a model did not converge."""
    if not result:
        logger.warning("%s loadflow returned no component result", spec.name)
        return
    main_component = result[0]
    status = main_component.status.name
    logger.info("%s loadflow status: %s (%s)", spec.name, status, main_component.status_text)
    if status != "CONVERGED":
        logger.warning(
            "%s loadflow did not converge (%s): its flows are not usable, and the missing "
            "values become zeros downstream",
            spec.name, status,
        )


def run_model(network, spec, voltage_levels):
    """Run one model's load flow on the current variant and snapshot its flows."""
    parameters = models.build_lf_parameters(spec)
    runner = pp.loadflow.run_dc if spec.dc else pp.loadflow.run_ac

    t0 = time.perf_counter()
    result = runner(network, parameters)
    lf_time = time.perf_counter() - t0
    logger.info("%s loadflow: %.3f s", spec.name, lf_time)
    _log_status(spec, result)

    return ModelRun(
        spec=spec, result=result, lf_time=lf_time,
        flows=_snapshot_flows(network, spec, voltage_levels),
    )


def run_all_models(network, model_specs):
    """Run every model on its own copy of the current variant, and return {name: ModelRun}.

    A load flow writes its results onto the network, and with the default read_slack_bus /
    write_slack_bus it also leaves a slack terminal extension behind that the next run reuses.
    Cloning the initial variant per model keeps model N+1 from starting where model N stopped,
    and leaves the network as it was found once every model has run.
    """
    # Nominal voltages do not depend on the variant, so they are read once, outside the runs.
    voltage_levels = network.get_voltage_levels()

    initial_variant = network.get_working_variant_id()
    runs = {}
    for spec in model_specs:
        variant_id = f"model_{spec.name}"
        network.clone_variant(initial_variant, variant_id)
        network.set_working_variant(variant_id)
        try:
            runs[spec.name] = run_model(network, spec, voltage_levels)
        finally:
            network.set_working_variant(initial_variant)
            network.remove_variant(variant_id)
    return runs


def _select_elements(currents, element_ids):
    """The (element, side) rows of `currents` that belong to `element_ids`."""
    return currents[currents.index.get_level_values("element_id").isin(element_ids)]


def _compare_models(flows_by_model, patl_per_side, name_lookup, active_threshold_pct, kind_label,
                    reference):
    """Build the base-case comparison dataframe for one element type (lines, 2W-TR, 3W-TR).

    Each element is reported on its binding side: the side whose *reference* loading against
    that side's own PATL is highest. The side is chosen on the reference model because it is
    what every other model is measured against, and the other models' currents are then read
    from that same side, so each reported deviation stays a model-approximation error rather
    than a difference between two terminals. An element rated on one side only has that side
    as its binding side. The "<name> LF" and "PATL" columns are all currents in A; the "%"
    columns are loadings in %.
    """
    per_side = pd.concat(
        [flows.rename(f"{name} LF") for name, flows in flows_by_model.items()], axis=1,
    )
    per_side["PATL"] = patl_per_side.reindex(per_side.index)
    per_side["loading"] = per_side[f"{reference} LF"].div(per_side["PATL"])

    # A side with no CURRENT PATL has no loading to rank it by.
    ranked = per_side.dropna(subset=["loading"])
    binding_side = ranked.groupby(level="element_id")["loading"].idxmax()

    elements = per_side.index.get_level_values("element_id").unique()
    comparison = ranked.loc[list(binding_side)].reset_index("side").rename(
        columns={"side": "Binding Side"},
    )
    comparison = comparison.reindex(elements[elements.isin(comparison.index)])
    comparison = comparison[[f"{name} LF" for name in flows_by_model] + ["PATL", "Binding Side"]]

    for name in flows_by_model:
        comparison[f"{name} LF %"] = comparison[f"{name} LF"].div(comparison["PATL"]).mul(100)

    reference_flow = comparison[f"{reference} LF"]
    for name in flows_by_model:
        if name == reference:
            continue
        comparison[f"({name}-{reference})/{reference} %"] = (
            comparison[f"{name} LF"].sub(reference_flow).div(reference_flow).mul(100).fillna(0)
        )

    for element_id in elements.difference(comparison.index):
        name = name_lookup.loc[element_id]["name"] if element_id in name_lookup.index else ""
        logger.warning("No PATL found for %s: %s (%s)", kind_label, element_id, name)

    if not comparison.empty:
        logger.info("Binding side for %s: %s", kind_label,
                    comparison["Binding Side"].value_counts().to_dict())

    # The activity filter is on the reference model's loading, so every model is compared on
    # the same set of elements.
    active_comparison = comparison[comparison[f"{reference} LF %"] > active_threshold_pct]
    return active_comparison, comparison.index.tolist()


def build_base_case_comparison(limits, items, runs, reference):
    """Build the base-case (N-0) comparison per element kind.

    Every element is compared on its binding side, against that side's own CURRENT PATL. CIM
    current limits are terminal-specific, so a branch can be rated differently on each side -
    a transformer most of all, where the two ratings express the same MVA at different
    voltages - and side ONE is not reliably the side that binds.

    Returns ({kind: comparison dataframe}, {kind: ids that had a PATL}, [single-sided ids]).
    """
    # One PATL per (element, side); the most restrictive rating wins if a side carries several.
    patl_per_side = permanent_current_limits(limits)

    comparisons, kept_ids = {}, {}
    for kind, kind_label in KIND_LABELS.items():
        comparisons[kind], kept_ids[kind] = _compare_models(
            {
                name: _select_elements(run.flows[kind], items[kind].index)
                for name, run in runs.items()
            },
            patl_per_side, items[kind], config.BASE_CASE_ACTIVE_THRESHOLD_PCT, kind_label,
            reference,
        )

    assessed = [element_id for ids in kept_ids.values() for element_id in ids]
    return comparisons, kept_ids, _single_sided_elements(patl_per_side, assessed)


def _single_sided_elements(patl_per_side, assessed):
    """The assessed elements carrying a CURRENT PATL on one side only.

    A data-quality note about the ratings in the input: such an element has no second side to
    compare, so its binding side is decided by the data rather than by the load flow. Counted
    over the elements the base case assessed, before the loading threshold is applied.
    """
    rated_sides = patl_per_side.groupby(level="element_id").size().reindex(assessed)
    return rated_sides[rated_sides == 1].index.tolist()
