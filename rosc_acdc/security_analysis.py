"""AC/DC security (contingency) analysis and the resulting AC vs DC comparison dataset."""

import logging
import time

import numpy as np
import pandas as pd
import pypowsybl as pp

from rosc_acdc import config
from rosc_acdc.paths import output_path

logger = logging.getLogger(__name__)


def run_security_analysis(security_analysis, network):
    """Run the DC then AC security analyses, logging their timings."""
    report_dc = pp.report.ReportNode()
    report_ac = pp.report.ReportNode()

    t0 = time.perf_counter()
    result_dc = security_analysis.run_dc(network, report_node=report_dc)
    con_analysis = time.perf_counter() - t0
    logger.info("DC contingency analysis time: %.3f s", con_analysis)

    t0 = time.perf_counter()
    result_ac = security_analysis.run_ac(network, report_node=report_ac)
    ac_con_analysis = time.perf_counter() - t0
    logger.info("AC contingency analysis time: %.3f s", ac_con_analysis)

    logger.info(
        "DC CURRENT violations %d",
        len(result_dc.limit_violations[result_dc.limit_violations["limit_type"] == "CURRENT"]),
    )
    logger.info(
        "AC CURRENT violations %d",
        len(result_ac.limit_violations[result_ac.limit_violations["limit_type"] == "CURRENT"]),
    )
    logger.info("Post-contingency DC cases: %d", len(result_dc.post_contingency_results))
    logger.info("Post-contingency AC cases: %d", len(result_ac.post_contingency_results))

    return result_dc, result_ac, con_analysis, ac_con_analysis


def branch_results(result, lines, transformers):
    branches = result.branch_results[["i1", "i2"]].rename(columns={"i1": "ONE", "i2": "TWO"}).stack().rename(
        "i").reset_index().rename(columns={"branch_id": "subject_id", "level_2": "side"})
    branches["Elm_Type"] = np.select(
        [branches["subject_id"].isin(lines.index), branches["subject_id"].isin(transformers.index)],
        ["Line", "2-Winding Transformer"], default="Unknown Branch")

    tr3 = result.three_windings_transformer_results[["i1", "i2", "i3"]].rename(
        columns={"i1": "ONE", "i2": "TWO", "i3": "THREE"}).stack().rename("i").reset_index().rename(
        columns={"transformer_id": "subject_id", "level_2": "side"})
    tr3["Elm_Type"] = "3-Winding Transformer"

    results = pd.concat([branches, tr3], ignore_index=True)
    keys = ["subject_id", "contingency_id", "side"]
    return results.loc[results.groupby(keys, dropna=False)["i"].idxmin()].reset_index(drop=True)


def build_side_comparison(result_dc, result_ac, lines, transformers):
    sides_dc = branch_results(result_dc, lines, transformers)
    sides_dc.index = sides_dc[["subject_id", "side", "contingency_id"]].fillna("").astype(str).agg("_".join, axis=1)

    sides_ac = branch_results(result_ac, lines, transformers)
    sides_ac.index = sides_ac[["subject_id", "side", "contingency_id"]].fillna("").astype(str).agg("_".join, axis=1)

    sides_dc.rename(columns={"i": "i_DC"}, inplace=True)
    sides_ac = pd.concat([sides_ac, sides_dc[["i_DC"]]], axis=1)
    return sides_ac


def build_shortlist_and_full_comparison(limits, sides_ac, network, element_info):
    """Build the SA threshold shortlist (for RAO CNEC selection) and the full AC/DC dataset.
    """
    patl_all = limits[limits["acceptable_duration"] == -1]
    patl_all = patl_all.set_index(["element_id", "side"])["value"]
    patl_all = patl_all[patl_all > 1]

    sides_ac = sides_ac.copy()
    sides_ac["patl"] = sides_ac.set_index(["subject_id", "side"]).index.map(patl_all)
    sides_ac["loading_pct"] = sides_ac["i"] / sides_ac["patl"] * 100

    base_i = pd.concat([
        network.get_lines()[["i1", "i2"]].rename(columns={"i1": "ONE", "i2": "TWO"}),
        network.get_2_windings_transformers()[["i1", "i2"]].rename(columns={"i1": "ONE", "i2": "TWO"}),
    ]).stack().rename("i").reset_index().rename(columns={"level_0": "subject_id", "level_1": "side"})
    base_i["contingency_id"] = None  # N-state

    sides_ac_all = pd.concat([base_i, sides_ac], ignore_index=True)
    sides_ac_all["side"] = sides_ac_all["side"].fillna(sides_ac_all["level_3"])
    sides_ac_all["patl"] = sides_ac_all.set_index(["subject_id", "side"]).index.map(patl_all)
    sides_ac_all["loading_pct"] = sides_ac_all["i"] / sides_ac_all["patl"] * 100
    sides_ac_all["subject_name"] = sides_ac_all["subject_id"].map(element_info["name"].drop_duplicates())

    shortlist_con_mge = (
        sides_ac_all.dropna(subset=["patl"])
        .loc[sides_ac_all["loading_pct"] > config.SA_Thres,
             ["contingency_id", "subject_id", "subject_name", "side", "i", "patl", "loading_pct"]]
        .sort_values("loading_pct", ascending=False)
    )[["contingency_id", "subject_id", "subject_name"]]

    subject_id_exclude = (
        sides_ac_all.groupby("subject_id")["loading_pct"]
        .agg(["min", "max", "mean"])
        .query("max >= 95 and (max - min)/mean <= 0.01")
        .index
    ).drop_duplicates().values
    subject_id_exclude = subject_id_exclude.tolist() + config.ignore_mge_list

    shortlist_con_mge = shortlist_con_mge.drop_duplicates()
    shortlist_con_mge = shortlist_con_mge[~shortlist_con_mge["subject_id"].isin(subject_id_exclude)]
    logger.info(
        "Considering SA Threshold for MGE-CON Shortlist: %s | excluded items: %d | shortlist size: %d",
        config.SA_Thres, len(subject_id_exclude), len(shortlist_con_mge),
    )

    sides_ac_all = sides_ac_all[sides_ac_all["loading_pct"] > config.SA_ACTIVE_THRESHOLD_PCT]
    sides_ac_all["i - i_DC"] = sides_ac_all["i"].sub(sides_ac_all["i_DC"])
    sides_ac_all["(i - i_DC)/patl %"] = sides_ac_all["i - i_DC"].div(sides_ac_all["patl"]).mul(100)

    sides_ac_all["Loading_Bin"] = pd.cut(
        sides_ac_all["loading_pct"],
        bins=[50, 60, 70, 80, 90, 100, np.inf],
        labels=["50-60%", "60-70%", "70-80%", "80-90%", "90-100%", "100%+"],
    )

    sides_ac_all[sides_ac_all["loading_pct"] > 80].to_excel(output_path("AC_DC_Contingency_Comparison.xlsx"))

    return shortlist_con_mge, sides_ac_all, patl_all
