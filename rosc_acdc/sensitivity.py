"""DC/AC redispatch and PST sensitivity analyses (only run when config.Sens is set)."""

import logging
import time

import pypowsybl as pp

logger = logging.getLogger(__name__)


def run_sensitivity_analyses(network, final_lines_id, generators, ptcs):
    results = {}

    t0 = time.perf_counter()
    analysis = pp.sensitivity.create_dc_analysis()
    analysis.add_branch_flow_factor_matrix(
        branches_ids=final_lines_id,
        variables_ids=generators.index,
        matrix_id="dc_rd",
    )
    result = analysis.run(network)
    results["dc_rd"] = result.get_sensitivity_matrix("dc_rd")
    dc_rd_sens_time = time.perf_counter() - t0
    logger.info("DC RD sensitivity time: %.3f s", dc_rd_sens_time)

    t0 = time.perf_counter()
    analysis = pp.sensitivity.create_dc_analysis()
    analysis.add_branch_flow_factor_matrix(
        branches_ids=final_lines_id,
        variables_ids=ptcs.index,
        matrix_id="dc_pst",
    )
    result = analysis.run(network)
    dc_pst_sens_time = time.perf_counter() - t0
    logger.info("DC PST sensitivity time: %.3f s", dc_pst_sens_time)
    results["dc_pst"] = result.get_sensitivity_matrix("dc_pst")

    t0 = time.perf_counter()
    analysis = pp.sensitivity.create_ac_analysis()
    analysis.add_branch_flow_factor_matrix(
        branches_ids=final_lines_id,
        variables_ids=generators.index,
        matrix_id="ac_rd",
    )
    result = analysis.run(network)
    results["ac_rd"] = result.get_sensitivity_matrix("ac_rd")
    ac_rd_sens_time = time.perf_counter() - t0
    logger.info("AC RD sensitivity time: %.3f s", ac_rd_sens_time)

    t0 = time.perf_counter()
    analysis = pp.sensitivity.create_ac_analysis()
    analysis.add_branch_flow_factor_matrix(
        branches_ids=final_lines_id,
        variables_ids=ptcs.index,
        matrix_id="ac_pst",
    )
    result = analysis.run(network)
    results["ac_pst"] = result.get_sensitivity_matrix("ac_pst")
    ac_pst_sens_time = time.perf_counter() - t0
    logger.info("AC PST sensitivity time: %.3f s", ac_pst_sens_time)

    return results
