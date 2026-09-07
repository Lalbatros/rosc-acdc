"""AC vs DC load flow / contingency-analysis comparison study - entry point.
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
    network_io,
    plotting,
    security_analysis,
    sensitivity,
)

logger = logging.getLogger(__name__)


def main():
    log_path = logging_setup.configure_logging()
    logger.info("Logging to %s", log_path)
    
    network = network_io.load_network()

    # --- AC load flow ---
    ac_result, ac_lf_time = loadflow.run_ac(network)

    buses = network.get_buses().fillna(0)
    lines = network.get_lines().fillna(0)
    transformers = network.get_2_windings_transformers().fillna(0)
    transformers3 = network.get_3_windings_transformers().fillna(0)

    hv_lines, rcc_lines, hv_transformers, rcc_transformers, hv_transformers3, rcc_transformers3 = (
        network_io.get_network_items(network)
    )

    ac_ln_mva = loadflow.branch_apparent_power(hv_lines)
    ac_tr_mva = loadflow.branch_apparent_power(hv_transformers)
    ac_tr3_mva = loadflow.branch_apparent_power(hv_transformers3)

    # --- DC load flow ---
    dc_result, dc_lf_time = loadflow.run_dc(network)

    buses = network.get_buses().fillna(0)
    hv_lines, rcc_lines, hv_transformers, rcc_transformers, hv_transformers3, rcc_transformers3 = (
        network_io.get_network_items(network)
    )
    generators = network.get_generators().fillna(0)
    ptcs = network.get_phase_tap_changers().fillna(0)

    dc_ln_mva = loadflow.branch_apparent_power(hv_lines)
    dc_tr_mva = loadflow.branch_apparent_power(hv_transformers)
    dc_tr3_mva = loadflow.branch_apparent_power(hv_transformers3)

    limits = network.get_loading_limits().reset_index()

    (
        lines_cmp, transformers_cmp, transformers3_cmp,
        final_lines_id, final_tr_id, final_tr3_id,
    ) = loadflow.build_base_case_comparison(
        limits, hv_lines, hv_transformers, hv_transformers3,
        ac_ln_mva, ac_tr_mva, ac_tr3_mva,
        dc_ln_mva, dc_tr_mva, dc_tr3_mva,
    )

    base_case_df = pd.concat([lines_cmp, transformers_cmp, transformers3_cmp])
    base_case_comparison = kpis.prepare_comparison(
        base_case_df, ac_value_col="AC LF", dc_value_col="DC LF", limit_col="PATL",
        ac_loading_col="AC LF %", dc_loading_col="DC LF %",
    )
    kpis.log_all_priority1_kpis(
        "Base Case (N-0)", base_case_comparison, id_col=pd.Series(base_case_df.index, index=base_case_df.index),
    )

    # --- Sensitivity analysis (optional) ---
    if config.Sens:
        sensitivity.run_sensitivity_analyses(network, final_lines_id, generators, ptcs)

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
    sides_ac_all = None
    patl_all = None
    con_analysis = ac_con_analysis = None

    if config.SA:
        result_dc, result_ac, con_analysis, ac_con_analysis = security_analysis.run_security_analysis(sa, network)
        sides_ac = security_analysis.build_side_comparison(result_dc, result_ac, lines, transformers)
        shortlist_con_mge, sides_ac_all, patl_all = security_analysis.build_shortlist_and_full_comparison(
            limits, sides_ac, network, element_info,
        )

        sa_comparison = kpis.prepare_comparison(
            sides_ac_all, ac_value_col="i", dc_value_col="i_DC", limit_col="patl",
            ac_loading_col="loading_pct",
        )
        kpis.log_all_priority1_kpis(
            "Contingency (SA)", sa_comparison,
            id_col=sides_ac_all["subject_id"], group_col=sides_ac_all["contingency_id"],
        )

        kpis.log_kpi_table("Performance", kpis.performance_kpis({
            "AC loadflow (s)": ac_lf_time,
            "DC loadflow (s)": dc_lf_time,
            "DC contingency analysis (s)": con_analysis,
            "AC contingency analysis (s)": ac_con_analysis,
        }))

        plotting.plot_sa_comparison(sides_ac_all)
    else:
        kpis.log_kpi_table("Performance", kpis.performance_kpis({
            "AC loadflow (s)": ac_lf_time,
            "DC loadflow (s)": dc_lf_time,
        }))

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
    result, rao_time = runner.run_rao(network, rao_crac, len(crac["flowCnecs"]), ac_con_analysis)

    if config.save_result:
        runner.save_and_process_result(result)


if __name__ == "__main__":
    main()
