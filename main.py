"""Multi-model (AC vs DC and variants) load flow / contingency-analysis comparison study - entry point.
"""

import logging

import json
import matplotlib
import numpy as np
import openpyxl
import pandas as pd
import pypowsybl as pp
import seaborn as sns

from rosc_acdc import (
    config,
    contingencies,
    kpis,
    loadflow,
    logging_setup,
    models,
    network_io,
    plotting,
    security_analysis,
    sensitivity,
)

logger = logging.getLogger(__name__)


def _model_label(spec):
    """"DC" for a model named after its mode, "DC_fast (DC)" for one that is not."""
    mode = "DC" if spec.dc else "AC"
    return spec.name if spec.name == mode else f"{spec.name} ({mode})"


def main():
    log_path = logging_setup.configure_logging()
    logger.info("Logging to %s", log_path)

    # Validated before the network is loaded: a bad model spec should not cost a load.
    model_specs = config.MODELS
    reference = config.REFERENCE_MODEL
    models.validate_models(model_specs, reference)
    compared_models = [spec.name for spec in model_specs if spec.name != reference]
    logger.info(
        "Models: %s | reference: %s",
        ", ".join(_model_label(spec) for spec in model_specs), reference,
    )

    network = network_io.load_network()

    hv_lines, rcc_lines, hv_transformers, rcc_transformers, hv_transformers3, rcc_transformers3 = (
        network_io.get_network_items(network)
    )
    items = {
        "lines": hv_lines,
        "transformers": hv_transformers,
        "transformers3": hv_transformers3,
    }

    # --- Load flow per model ---
    # Each model runs in its own variant, so the element frames fetched below are unaffected by
    # the runs and only have to be fetched once.
    runs = loadflow.run_all_models(network, model_specs, items)

    lines = network.get_lines().fillna(0)
    transformers = network.get_2_windings_transformers().fillna(0)
    generators = network.get_generators().fillna(0)
    ptcs = network.get_phase_tap_changers().fillna(0)
    limits = network.get_loading_limits().reset_index()

    # --- Base case (N-0) comparison ---
    base_case_by_kind, kept_ids = loadflow.build_base_case_comparison(limits, items, runs, reference)
    base_case_df = pd.concat(base_case_by_kind.values())

    base_case_comparisons = {
        name: kpis.prepare_comparison(
            base_case_df,
            reference_value_col=f"{reference} LF", model_value_col=f"{name} LF", limit_col="PATL",
            reference_loading_col=f"{reference} LF %", model_loading_col=f"{name} LF %",
        )
        for name in compared_models
    }
    kpis.log_all_kpis_for_models(
        "Base Case (N-0)", base_case_comparisons, reference,
        id_col=pd.Series(base_case_df.index, index=base_case_df.index),
    )

    # --- Sensitivity analysis (optional) ---
    if config.Sens:
        sensitivity.run_sensitivity_analyses(network, kept_ids["lines"], generators, ptcs)

    # --- Contingency scenarios ---
    data = contingencies.load_contingency_data()
    valid_ids = contingencies.build_valid_ids(network)

    sa = pp.security.create_analysis()
    branch_ids = (
        hv_lines.index.tolist() + hv_transformers.index.tolist() +
        rcc_lines.index.tolist() + rcc_transformers.index.tolist()
    )
    tr3_ids = hv_transformers3.index.tolist() + rcc_transformers3.index.tolist()

    element_info = pd.concat([
        hv_lines, hv_transformers, hv_transformers3, rcc_lines, rcc_transformers, rcc_transformers3,
    ])
    element_info.index.name = "subject_id"

    sa.add_monitored_elements(branch_ids=branch_ids, three_windings_transformer_ids=tr3_ids)
    contingencies.add_contingencies_and_actions(sa, data, valid_ids)

    shortlist_con_mge = None
    sides_all = None
    patl_all = None
    reference_con_analysis = None

    timings = {f"{spec.name} loadflow (s)": runs[spec.name].lf_time for spec in model_specs}

    if config.SA:
        sa_results = security_analysis.run_security_analysis(sa, network, model_specs, reference)
        sides = security_analysis.build_side_comparison(sa_results, lines, transformers, reference)
        shortlist_con_mge, sides_all, patl_all = security_analysis.build_shortlist_and_full_comparison(
            limits, sides, network, element_info, compared_models,
        )

        sa_comparisons = {
            name: kpis.prepare_comparison(
                sides_all,
                reference_value_col="i", model_value_col=f"i_{name}", limit_col="patl",
                reference_loading_col="loading_pct",
            )
            for name in compared_models
        }
        kpis.log_all_kpis_for_models(
            "Contingency (SA)", sa_comparisons, reference,
            id_col=sides_all["subject_id"], group_col=sides_all["contingency_id"],
        )

        timings.update({
            f"{name} contingency analysis (s)": elapsed
            for name, (_, elapsed) in sa_results.items()
        })
        reference_con_analysis = sa_results[reference][1]

        kpis.log_kpi_table("Performance", kpis.performance_kpis(timings))
        if compared_models:  # nothing to plot when the reference is the only model
            plotting.plot_sa_comparison(sides_all, reference, compared_models)
    else:
        kpis.log_kpi_table("Performance", kpis.performance_kpis(timings))

    if not config.RAO_RUN:
        return

    # --- RAO ---
    from rosc_acdc.rao import crac_builder, runner

    topo_actions = crac_builder.load_topo_actions(network)
    redispatch_actions = crac_builder.load_redispatch_actions(network)
    pst_actions = crac_builder.load_pst_actions(network)

    if config.SA:
        rcc_pool = pd.concat([rcc_lines, rcc_transformers], axis=0)
        ncc_pool = pd.concat([hv_lines, hv_transformers], axis=0)
        rcc_line_ids = rcc_pool.loc[rcc_pool.index.isin(shortlist_con_mge["subject_id"])]
        ncc_line_ids = ncc_pool.loc[ncc_pool.index.isin(shortlist_con_mge["subject_id"])]
    else:
        rcc_line_ids = rcc_lines
        ncc_line_ids = hv_lines

    if config.RANDOM_SEL:
        if config.rand_seed:
            rcc_line_ids = rcc_line_ids.sample(n=config.rNoRCC, random_state=config.rand_seed)
            ncc_line_ids = ncc_line_ids.sample(n=config.rNoNCC, random_state=config.rand_seed)
        else:
            rcc_line_ids = rcc_line_ids.sample(n=config.rNoRCC)
            ncc_line_ids = ncc_line_ids.sample(n=config.rNoNCC)

    if config.SA:
        shortlist_con_mge = shortlist_con_mge.copy()
        shortlist_con_mge["CC"] = pd.NA
        shortlist_con_mge.loc[shortlist_con_mge["subject_id"].isin(ncc_line_ids.index), "CC"] = "NCC"
        shortlist_con_mge.loc[shortlist_con_mge["subject_id"].isin(rcc_line_ids.index), "CC"] = "RCC"

    crac = crac_builder.build_crac(
        network, data, valid_ids, shortlist_con_mge, rcc_line_ids, ncc_line_ids,
        patl_all, topo_actions, redispatch_actions, pst_actions,
    )

    rao_crac = runner.load_crac(network, crac)
    result, rao_time = runner.run_rao(
        network, rao_crac, len(crac["flowCnecs"]), reference_con_analysis,
    )

    if config.save_result:
        runner.save_and_process_result(result)


if __name__ == "__main__":
    main()
