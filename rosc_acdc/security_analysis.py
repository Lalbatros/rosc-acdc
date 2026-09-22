"""Per-model security (contingency) analysis and the resulting comparison dataset."""

import logging
import time

import numpy as np
import pandas as pd
import pypowsybl as pp

from rosc_acdc import config, models
from rosc_acdc.paths import output_path

logger = logging.getLogger(__name__)


def run_security_analysis(security_analysis, network, model_specs, reference):
    """Run one security analysis per model and return {name: (result, elapsed seconds)}.

    The reference model runs last: the first run of the batch pays the JVM warm-up, and the
    Performance table is easier to read across runs when that cost always lands on the same
    model. Results themselves do not depend on the order (the analyses do not write back to
    the network), only the timings do.
    """
    ordered = ([spec for spec in model_specs if spec.name != reference]
               + [spec for spec in model_specs if spec.name == reference])

    results = {}
    for spec in ordered:
        report = pp.report.ReportNode()
        runner = security_analysis.run_dc if spec.dc else security_analysis.run_ac
        t0 = time.perf_counter()
        result = runner(network, parameters=models.build_sa_parameters(spec), report_node=report)
        elapsed = time.perf_counter() - t0
        logger.info("%s contingency analysis time: %.3f s", spec.name, elapsed)
        results[spec.name] = (result, elapsed)

    for name, (result, _) in results.items():
        violations = result.limit_violations
        logger.info("%s CURRENT violations %d",
                    name, len(violations[violations["limit_type"] == "CURRENT"]))
    for name, (result, _) in results.items():
        logger.info("Post-contingency %s cases: %d", name, len(result.post_contingency_results))

    return results


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

    # Note: branch_results has one index level more than the stacked side, so for branches the
    # side lands in "level_3" and the rename above is a no-op - they keep side=NaN until
    # build_shortlist_and_full_comparison fills it from level_3, while 3W transformers get
    # "side" here. That also makes the join labels differ between the two; existing behaviour.
    results = pd.concat([branches, tr3], ignore_index=True)
    keys = ["subject_id", "contingency_id", "side"]
    return results.loc[results.groupby(keys, dropna=False)["i"].idxmin()].reset_index(drop=True)


def _side_index(frame):
    return frame[["subject_id", "side", "contingency_id"]].fillna("").astype(str).agg("_".join, axis=1)


def build_side_comparison(sa_results, lines, transformers, reference):
    """Join every other model's post-contingency current onto the reference model's rows.

    The reference current stays in "i"; each other model contributes an "i_<name>" column.
    """
    sides = {}
    for name, (result, _) in sa_results.items():
        frame = branch_results(result, lines, transformers)
        frame.index = _side_index(frame)
        sides[name] = frame

    comparison = sides[reference]
    for name, frame in sides.items():
        if name == reference:
            continue
        column = f"i_{name}"
        comparison = pd.concat([comparison, frame[["i"]].rename(columns={"i": column})], axis=1)
        missing = int(comparison[column].isna().sum())
        if missing:
            logger.warning(
                "%s has no value on %d of %d rows: the %s and %s results did not align",
                column, missing, len(comparison), reference, name,
            )
    return comparison


def build_shortlist_and_full_comparison(limits, sides, network, element_info, model_names):
    """Build the SA threshold shortlist (for RAO CNEC selection) and the full dataset.

    The shortlist and "loading_pct" are the reference model's; each other model in
    `model_names` adds its own deviation columns.
    """
    patl_all = limits[limits["acceptable_duration"] == -1]
    patl_all = patl_all.set_index(["element_id", "side"])["value"]
    patl_all = patl_all[patl_all > 1]

    sides = sides.copy()
    sides["patl"] = sides.set_index(["subject_id", "side"]).index.map(patl_all)
    sides["loading_pct"] = sides["i"] / sides["patl"] * 100

    # N-state rows. These used to read i1/i2 back from the network after the DC load flow, where
    # they are unset, so the rows never survive the loading filter below. Models now run in their
    # own variants and leave the initial state untouched, so that read would no longer return
    # missing values: the currents are blanked here to keep the rows as inert as they were.
    # Giving them real base-case currents is a separate, deliberate change - it feeds RAO input.
    base_i = (
        pd.concat([
            network.get_lines()[["i1", "i2"]],
            network.get_2_windings_transformers()[["i1", "i2"]],
        ])
        .rename(columns={"i1": "ONE", "i2": "TWO"})
        .assign(ONE=np.nan, TWO=np.nan)
        .stack().rename("i").reset_index()
        .rename(columns={"level_0": "subject_id", "level_1": "side"})
    )
    base_i["contingency_id"] = None  # N-state

    sides_all = pd.concat([base_i, sides], ignore_index=True)
    sides_all["side"] = sides_all["side"].fillna(sides_all["level_3"])
    sides_all["patl"] = sides_all.set_index(["subject_id", "side"]).index.map(patl_all)
    sides_all["loading_pct"] = sides_all["i"] / sides_all["patl"] * 100
    sides_all["subject_name"] = sides_all["subject_id"].map(element_info["name"].drop_duplicates())

    shortlist_con_mge = (
        sides_all.dropna(subset=["patl"])
        .loc[sides_all["loading_pct"] > config.SA_Thres,
             ["contingency_id", "subject_id", "subject_name", "side", "i", "patl", "loading_pct"]]
        .sort_values("loading_pct", ascending=False)
    )[["contingency_id", "subject_id", "subject_name"]]

    subject_id_exclude = (
        sides_all.groupby("subject_id")["loading_pct"]
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

    sides_all = sides_all[sides_all["loading_pct"] > config.SA_ACTIVE_THRESHOLD_PCT]
    for name in model_names:
        sides_all[f"i - i_{name}"] = sides_all["i"].sub(sides_all[f"i_{name}"])
        sides_all[f"(i - i_{name})/patl %"] = (
            sides_all[f"i - i_{name}"].div(sides_all["patl"]).mul(100)
        )

    sides_all["Loading_Bin"] = pd.cut(
        sides_all["loading_pct"],
        bins=[50, 60, 70, 80, 90, 100, np.inf],
        labels=["50-60%", "60-70%", "70-80%", "80-90%", "90-100%", "100%+"],
    )

    sides_all[sides_all["loading_pct"] > 80].to_excel(output_path("AC_DC_Contingency_Comparison.xlsx"))

    return shortlist_con_mge, sides_all, patl_all
