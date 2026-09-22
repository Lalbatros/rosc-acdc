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


@dataclass
class ModelRun:
    """What one model's load flow produced."""

    spec: models.ModelSpec
    result: list
    lf_time: float
    flows: dict = field(default_factory=dict)  # element kind -> MVA magnitude per element


def branch_apparent_power(elements_df):
    """MVA magnitude from p1/q1, as the original script computed it."""
    return np.hypot(elements_df["p1"], elements_df["q1"])


def _snapshot_flows(network, items):
    """Re-read p1/q1 of the monitored elements from the current variant, as MVA magnitudes.

    Element classification is state-independent and done once by network_io.get_network_items;
    only the flows have to be read again per model. The fillna(0) reproduces what
    get_network_items applied to the whole frame - it is what keeps DC flows finite, since a
    DC load flow leaves q1 (and i1/i2) unset.
    """
    getters = {
        "lines": network.get_lines,
        "transformers": network.get_2_windings_transformers,
        "transformers3": network.get_3_windings_transformers,
    }
    return {
        kind: branch_apparent_power(getter()[["p1", "q1"]].fillna(0).loc[items[kind].index])
        for kind, getter in getters.items()
    }


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


def run_model(network, spec, items):
    """Run one model's load flow on the current variant and snapshot its flows."""
    parameters = models.build_lf_parameters(spec)
    runner = pp.loadflow.run_dc if spec.dc else pp.loadflow.run_ac

    t0 = time.perf_counter()
    result = runner(network, parameters)
    lf_time = time.perf_counter() - t0
    logger.info("%s loadflow: %.3f s", spec.name, lf_time)
    _log_status(spec, result)

    return ModelRun(spec=spec, result=result, lf_time=lf_time, flows=_snapshot_flows(network, items))


def run_all_models(network, model_specs, items):
    """Run every model on its own copy of the current variant, and return {name: ModelRun}.

    A load flow writes its results onto the network, and with the default read_slack_bus /
    write_slack_bus it also leaves a slack terminal extension behind that the next run reuses.
    Cloning the initial variant per model keeps model N+1 from starting where model N stopped,
    and leaves the network as it was found once every model has run.
    """
    initial_variant = network.get_working_variant_id()
    runs = {}
    for spec in model_specs:
        variant_id = f"model_{spec.name}"
        network.clone_variant(initial_variant, variant_id)
        network.set_working_variant(variant_id)
        try:
            runs[spec.name] = run_model(network, spec, items)
        finally:
            network.set_working_variant(initial_variant)
            network.remove_variant(variant_id)
    return runs


def _compare_models(flows_by_model, patl_one, name_lookup, active_threshold_pct, kind_label,
                    reference):
    """Build the base-case comparison dataframe for one element type (lines, 2W-TR, 3W-TR)."""
    comparison = pd.concat(
        [flows.rename(f"{name} LF") for name, flows in flows_by_model.items()], axis=1,
    )

    missing_ids = [element_id for element_id in comparison.index if element_id not in patl_one.index]
    for element_id in missing_ids:
        name = name_lookup.loc[element_id]["name"] if element_id in name_lookup.index else ""
        logger.warning("No PATL found for %s: %s (%s)", kind_label, element_id, name)

    kept_ids = [element_id for element_id in comparison.index if element_id in patl_one.index]
    comparison = comparison.loc[kept_ids]
    comparison["PATL"] = patl_one.loc[kept_ids]

    for name in flows_by_model:
        comparison[f"{name} LF %"] = comparison[f"{name} LF"].div(comparison["PATL"]).mul(100)

    reference_flow = comparison[f"{reference} LF"]
    for name in flows_by_model:
        if name == reference:
            continue
        comparison[f"({name}-{reference})/{reference} %"] = (
            comparison[f"{name} LF"].sub(reference_flow).div(reference_flow).mul(100).fillna(0)
        )

    # The activity filter is on the reference model's loading, so every model is compared on
    # the same set of elements.
    active_comparison = comparison[comparison[f"{reference} LF %"] > active_threshold_pct]
    return active_comparison, kept_ids


def build_base_case_comparison(limits, items, runs, reference):
    """Build the base-case (N-0) comparison per element kind.

    Returns ({kind: comparison dataframe}, {kind: ids that had a PATL}).
    """
    patl = limits[(limits["acceptable_duration"] == -1) & (limits["side"] == "ONE")]
    patl = patl.set_index("element_id")["value"]

    comparisons, kept_ids = {}, {}
    for kind, kind_label in KIND_LABELS.items():
        comparisons[kind], kept_ids[kind] = _compare_models(
            {name: run.flows[kind] for name, run in runs.items()},
            patl, items[kind], config.BASE_CASE_ACTIVE_THRESHOLD_PCT, kind_label, reference,
        )
    return comparisons, kept_ids
