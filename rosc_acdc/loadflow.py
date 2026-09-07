"""AC/DC load flow execution and base-case (N-0) AC vs DC comparison dataframes."""

import logging
import time

import numpy as np
import pandas as pd
import pypowsybl as pp

from rosc_acdc import config

logger = logging.getLogger(__name__)


def run_ac(network):
    parameters = pp.loadflow.Parameters()
    parameters.dc = False
    t0 = time.perf_counter()
    ac_result = pp.loadflow.run_ac(network, parameters)
    ac_lf_time = time.perf_counter() - t0
    logger.info("AC loadflow: %.3f s", ac_lf_time)
    return ac_result, ac_lf_time


def run_dc(network):
    parameters = pp.loadflow.Parameters()
    parameters.dc = True
    t0 = time.perf_counter()
    dc_result = pp.loadflow.run_dc(network, parameters)
    dc_lf_time = time.perf_counter() - t0
    logger.info("DC loadflow: %.3f s", dc_lf_time)
    return dc_result, dc_lf_time


def branch_apparent_power(elements_df):
    """MVA magnitude from p1/q1, as the original script computed it."""
    return np.hypot(elements_df["p1"], elements_df["q1"])


def _compare_ac_dc(ac_series, dc_series, patl_one, name_lookup, active_threshold_pct, kind_label):
    """Build the AC/DC comparison dataframe for one element type (lines, 2W-TR, 3W-TR).
    """
    ac_series = ac_series.rename("AC LF")
    dc_series = dc_series.rename("DC LF")
    comparison = pd.concat([ac_series, dc_series], axis=1)

    missing_ids = [element_id for element_id in comparison.index if element_id not in patl_one.index]
    if missing_ids:
        for element_id in missing_ids:
            name = name_lookup.loc[element_id]["name"] if element_id in name_lookup.index else ""
            logger.warning("No PATL found for %s: %s (%s)", kind_label, element_id, name)

    kept_ids = [element_id for element_id in comparison.index if element_id in patl_one.index]
    comparison = comparison.loc[kept_ids]

    comparison["PATL"] = patl_one.loc[kept_ids]
    comparison["AC LF %"] = comparison["AC LF"].div(comparison["PATL"]).mul(100)
    comparison["DC LF %"] = comparison["DC LF"].div(comparison["PATL"]).mul(100)
    comparison["(DC-AC)/AC %"] = (
        (comparison["DC LF"].sub(comparison["AC LF"])).div(comparison["AC LF"])
    ).mul(100).fillna(0)

    active_comparison = comparison[comparison["AC LF %"] > active_threshold_pct]
    return active_comparison, kept_ids


def build_base_case_comparison(limits, hv_lines, hv_transformers, hv_transformers3,
                                ac_ln_mva, ac_tr_mva, ac_tr3_mva,
                                dc_ln_mva, dc_tr_mva, dc_tr3_mva):
    """Build the three base-case (N-0) AC vs DC comparison dataframes (lines, 2W-TR, 3W-TR).
    """
    patl = limits[(limits["acceptable_duration"] == -1) & (limits["side"] == "ONE")]
    patl = patl.set_index("element_id")["value"]

    lines_cmp, final_lines_id = _compare_ac_dc(
        ac_ln_mva, dc_ln_mva, patl, hv_lines, config.BASE_CASE_ACTIVE_THRESHOLD_PCT, "line",
    )
    transformers_cmp, final_tr_id = _compare_ac_dc(
        ac_tr_mva, dc_tr_mva, patl, hv_transformers, config.BASE_CASE_ACTIVE_THRESHOLD_PCT, "2-winding transformer",
    )
    transformers3_cmp, final_tr3_id = _compare_ac_dc(
        ac_tr3_mva, dc_tr3_mva, patl, hv_transformers3, config.BASE_CASE_ACTIVE_THRESHOLD_PCT, "3-winding transformer",
    )

    return (
        lines_cmp, transformers_cmp, transformers3_cmp,
        final_lines_id, final_tr_id, final_tr3_id,
    )
