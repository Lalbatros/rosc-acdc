"""Implements: Flow deviation, Loading & limit-based, Violation detection,
Severity-based, Ranking & critical element, and Performance KPIs.

Every KPI compares one model against the reference model, so a comparison frame carries
``ref_*`` and ``model_*`` columns rather than AC/DC ones.

The functions below the "KPI workbook primitives" banner return **machine keys** rather
than the display labels the log tables use. Core_<ref>_<model>_KPI.xlsx reads them by key,
and a display label that embeds a config value (a threshold, a pair label) would turn a
config edit into a KeyError in the workbook.
"""

import logging

import numpy as np
import pandas as pd
from prettytable import PrettyTable

from rosc_acdc import config

logger = logging.getLogger(__name__)

# An element is in violation above this loading. Kept as a module constant so the log
# tables and the workbook share one threshold, and defaulted from config so the workbook's
# Parameters sheet reports the value that was actually applied.
VIOLATION_THRESHOLD_PCT = config.KPI_VIOLATION_THRESHOLD_PCT
NEAR_LIMIT_THRESHOLDS_PCT = (80, 90, 95)
TOP_N_DEFAULT = 10


def prepare_comparison(df, reference_value_col, model_value_col, limit_col,
                        reference_loading_col=None, model_loading_col=None):
    """Normalize one model-vs-reference comparison dataframe to the common KPI column names.

    Observations without both a reference and a model value (and a limit) are dropped here so
    every KPI below runs on the same population: a missing model flow is missing data, not
    "the model sees no violation" - counting it as one inflates the false-negative KPIs.
    """
    out = pd.DataFrame(index=df.index)
    out["ref_value"] = df[reference_value_col]
    out["model_value"] = df[model_value_col]
    out["limit"] = df[limit_col]
    out["ref_loading_pct"] = (
        df[reference_loading_col] if reference_loading_col else out["ref_value"] / out["limit"] * 100
    )
    out["model_loading_pct"] = (
        df[model_loading_col] if model_loading_col else out["model_value"] / out["limit"] * 100
    )
    paired = out[
        ["ref_value", "model_value", "limit", "ref_loading_pct", "model_loading_pct"]
    ].notna().all(axis=1)
    if not paired.all():
        logger.warning(
            "Excluding %d of %d observations with no paired reference/model value or no limit",
            int((~paired).sum()), len(out),
        )
        out = out[paired]

    out["signed_error"] = out["model_value"] - out["ref_value"]
    out["abs_error"] = out["signed_error"].abs()
    out["signed_loading_deviation"] = out["model_loading_pct"] - out["ref_loading_pct"]
    out["margin_ref"] = out["limit"] - out["ref_value"].abs()
    out["margin_model"] = out["limit"] - out["model_value"].abs()
    out["signed_margin_error"] = out["margin_model"] - out["margin_ref"]
    out["ref_violation"] = out["ref_loading_pct"] > VIOLATION_THRESHOLD_PCT
    out["model_violation"] = out["model_loading_pct"] > VIOLATION_THRESHOLD_PCT
    # Overload magnitude above the limit, per side of the comparison. Computed once here so
    # the severity KPIs, the CO-level KPIs and their primitives read the same numbers.
    out["ref_overload"] = (out["ref_value"].abs() - out["limit"]).clip(lower=0)
    out["model_overload"] = (out["model_value"].abs() - out["limit"]).clip(lower=0)
    return out


def flow_deviation_kpis(comparison: pd.DataFrame) -> dict:
    """MAE, RMSE, P95 and max absolute error between the model's and the reference's flows."""
    errors = comparison["abs_error"].dropna()
    if errors.empty:
        return {"MAE": np.nan, "RMSE": np.nan, "P95 Error": np.nan, "Max Error": np.nan}
    return {
        "MAE": errors.mean(),
        "RMSE": np.sqrt((errors ** 2).mean()),
        "P95 Error": errors.quantile(0.95),
        "Max Error": errors.max(),
    }


def loading_limit_kpis(comparison: pd.DataFrame, pair_label: str) -> dict:
    """Signed loading-% deviation and margin error, overall and near the thermal limit.

    `pair_label` names the signed difference's direction, e.g. "DC-AC".
    """
    kpis = {
        f"Mean Loading % Deviation ({pair_label})": comparison["signed_loading_deviation"].mean(),
        f"Mean Margin Error ({pair_label})": comparison["signed_margin_error"].mean(),
    }
    for threshold in NEAR_LIMIT_THRESHOLDS_PCT:
        subset = comparison[comparison["ref_loading_pct"] >= threshold]
        kpis[f"Near-Limit ({threshold}%+) Mean Loading % Deviation"] = (
            subset["signed_loading_deviation"].mean() if not subset.empty else np.nan
        )
    return kpis


def violation_kpis(comparison: pd.DataFrame) -> dict:
    """False negatives/positives and their rates, using loading% > 100 as the violation flag."""
    ref_violation = comparison["ref_violation"]
    model_violation = comparison["model_violation"]

    false_negatives = ref_violation & ~model_violation
    false_positives = ~ref_violation & model_violation

    ref_violation_count = int(ref_violation.sum())
    ref_secure_count = int((~ref_violation).sum())

    return {
        "False Negatives": int(false_negatives.sum()),
        "False Positives": int(false_positives.sum()),
        "Missed Overload Rate": (
            false_negatives.sum() / ref_violation_count if ref_violation_count else np.nan
        ),
        "False Alarm Rate": (
            false_positives.sum() / ref_secure_count if ref_secure_count else np.nan
        ),
        "_false_negatives_mask": false_negatives,
        "_false_positives_mask": false_positives,
    }


def severity_kpis(comparison: pd.DataFrame, violation: dict) -> dict:
    """Volume (sum) and average magnitude of missed/false overloads."""
    false_negatives = violation["_false_negatives_mask"]
    false_positives = violation["_false_positives_mask"]

    missed = comparison.loc[false_negatives]
    false = comparison.loc[false_positives]

    return {
        "Missed Overload Volume": missed["ref_overload"].sum(),
        "False Overload Volume": false["model_overload"].sum(),
        "False Negatives Avg Loading %": missed["ref_loading_pct"].mean() if not missed.empty else np.nan,
        "False Positives Avg Loading %": false["model_loading_pct"].mean() if not false.empty else np.nan,
    }


def ranking_kpis(comparison: pd.DataFrame, id_col: pd.Series, group_col: pd.Series = None,
                  top_n: int = TOP_N_DEFAULT) -> dict:
    """Top-N critical element overlap and worst-case match rate, between the two rankings.

    When `group_col` is given (e.g. contingency_id), ranking is done per group
    and the reported figures are averaged across groups; otherwise ranking is
    global (base-case usage).
    """
    df = comparison.copy()
    df["_id"] = id_col.reindex(df.index)

    def _group_metrics(group_df):
        if len(group_df) < 2:
            return None
        ref_top = set(group_df.nlargest(min(top_n, len(group_df)), "ref_loading_pct")["_id"])
        model_top = set(group_df.nlargest(min(top_n, len(group_df)), "model_loading_pct")["_id"])
        overlap = len(ref_top & model_top) / len(ref_top) if ref_top else np.nan
        ref_worst = group_df.loc[group_df["ref_loading_pct"].idxmax(), "_id"]
        model_worst = group_df.loc[group_df["model_loading_pct"].idxmax(), "_id"]
        return overlap, ref_worst == model_worst

    if group_col is not None:
        df["_group"] = group_col.reindex(df.index)
        overlaps, matches = [], []
        for _, group_df in df.groupby("_group"):
            metrics = _group_metrics(group_df)
            if metrics is not None:
                overlaps.append(metrics[0])
                matches.append(metrics[1])
        return {
            "Top-N Critical Element Overlap": np.nanmean(overlaps) if overlaps else np.nan,
            "Worst-Case Match Rate": np.mean(matches) if matches else np.nan,
        }

    metrics = _group_metrics(df)
    if metrics is None:
        return {"Top-N Critical Element Overlap": np.nan, "Worst-Case Match Rate": np.nan}
    overlap, match = metrics
    return {"Top-N Critical Element Overlap": overlap, "Worst-Case Match Rate": float(match)}


def performance_kpis(timings: dict) -> dict:
    """Pass-through formatting for the study's stage timings, in seconds."""
    return {name: value for name, value in timings.items() if value is not None}


def _format(value):
    return f"{value:.3f}" if isinstance(value, float) else value


def log_kpi_table(title: str, kpis: dict) -> None:
    """Render a KPI dict as a PrettyTable and log it."""
    table = PrettyTable()
    table.field_names = ["KPI", "Value"]
    table.align["KPI"] = "l"
    table.align["Value"] = "r"
    for name, value in kpis.items():
        if name.startswith("_"):
            continue
        table.add_row([name, _format(value)])
    logger.info("%s\n%s", title, table)


def log_cross_model_kpi_table(title: str, kpis_by_model: dict) -> None:
    """Render KPI rows against one column per model, for comparing models at a glance."""
    table = PrettyTable()
    model_names = list(kpis_by_model)
    table.field_names = ["KPI"] + model_names
    table.align["KPI"] = "l"
    for name in model_names:
        table.align[name] = "r"

    kpi_names = []
    for kpis in kpis_by_model.values():
        for kpi_name in kpis:
            if not kpi_name.startswith("_") and kpi_name not in kpi_names:
                kpi_names.append(kpi_name)

    for kpi_name in kpi_names:
        table.add_row([kpi_name] + [
            _format(kpis_by_model[name].get(kpi_name, "")) for name in model_names
        ])
    logger.info("%s\n%s", title, table)


def log_all_kpis_for_models(label: str, comparisons: dict, reference: str,
                            id_col: pd.Series = None, group_col: pd.Series = None) -> None:
    """Log every KPI group for each model, plus a cross-model summary when there are several.

    `comparisons` maps a model name to what prepare_comparison returned for that model against
    `reference`. With a single model the block titles stay unqualified and the summary table is
    skipped, since it would only repeat the blocks above it.
    """
    several = len(comparisons) > 1
    summary = {}

    for name, comparison in comparisons.items():
        pair_label = f"{name}-{reference}"
        block = f"{label} [{name} vs {reference}]" if several else label

        flow_deviation = flow_deviation_kpis(comparison)
        loading_limit = loading_limit_kpis(comparison, pair_label)
        violation = violation_kpis(comparison)
        severity = severity_kpis(comparison, violation)

        log_kpi_table(f"{block} - Flow Deviation KPIs", flow_deviation)
        log_kpi_table(f"{block} - Loading & Limit-Based KPIs", loading_limit)
        log_kpi_table(f"{block} - Violation Detection KPIs", violation)
        log_kpi_table(f"{block} - Severity-Based KPIs", severity)

        ranking = {}
        if id_col is not None:
            ranking = ranking_kpis(comparison, id_col, group_col)
            log_kpi_table(f"{block} - Ranking & Critical Element KPIs", ranking)

        if several:
            # The pair label differs per model, so it is replaced by a label the shared rows of
            # the cross-model table can carry (the model is already the column).
            summary[name] = {
                key.replace(f"({pair_label})", f"(vs {reference})"): value
                for group in (flow_deviation, loading_limit, violation, severity, ranking)
                for key, value in group.items()
            }

    if several:
        log_cross_model_kpi_table(f"{label} - KPIs by model (vs {reference})", summary)


# ---------------------------------------------------------------------------
# KPI workbook primitives
#
# Everything below returns machine keys, not display labels: Core_<ref>_<model>_KPI.xlsx
# reads these by key, so a key must not embed a config value. They are the counts and sums
# the KPI functions above compute and then divide away, plus the two KPI families that only
# the workbook reports (near-limit at the configured threshold, and contingency-level).
# ---------------------------------------------------------------------------


def near_limit_subset(comparison: pd.DataFrame, threshold_pct: float = None) -> pd.DataFrame:
    """The observations whose *reference* loading reaches the near-limit threshold.

    One place decides this population, so the near-limit KPI and its primitives
    (N_NearLimit_Obs, Sum_NearLimit_LoadingDev_pp, N_NearLimit_Underest) cannot drift
    apart. ">=" is deliberate: the threshold selects a population here, unlike the
    violation test above, which decides pass/fail and so stays strict.
    """
    if threshold_pct is None:
        threshold_pct = config.KPI_NEAR_LIMIT_THRESHOLD_PCT
    return comparison[comparison["ref_loading_pct"] >= threshold_pct]


def near_limit_kpis(comparison: pd.DataFrame, threshold_pct: float = None) -> dict:
    """The near-limit KPI at one threshold, with the primitives that let it be re-derived.

    An "underestimation" is an observation where the model reports a lower loading than the
    reference: the error that matters near the limit, because it is the one that hides an
    overload.
    """
    subset = near_limit_subset(comparison, threshold_pct)
    deviation = subset["signed_loading_deviation"]
    return {
        "Sum_NearLimit_LoadingDev_pp": deviation.sum(),
        "N_NearLimit_Obs": int(len(subset)),
        "NearLimit_Mean_LoadingDev_pp": deviation.mean() if len(subset) else np.nan,
        "N_NearLimit_Underest": int((deviation < 0).sum()),
        "NearLimit_Underest_Rate": (
            float((deviation < 0).sum()) / len(subset) if len(subset) else np.nan
        ),
    }


def element_ranking_kpis(comparison: pd.DataFrame, id_col: pd.Series,
                          top_n: int = None) -> dict:
    """Top-N overlap and worst-case match for one KPI_Data row, ranked over its elements.

    One ranking per row, not one per contingency (which is what ranking_kpis above does for
    the log): TopN_N is the configured N on every row regardless of Case, so the numerator
    has to be a single group's overlap count for SUM(TopN_Overlap_Count) / SUM(TopN_N) to
    stay in range when rows are rolled up in a pivot.

    Elements are ranked on their worst loading within the row's population. An element
    appearing under several contingencies would otherwise occupy several of the N slots and
    collapse the comparison to a handful of distinct elements.

    WorstCase_Match_Rate is therefore 0 or 1 here - whether the reference and the model
    agree on this row's single most loaded element.
    """
    if top_n is None:
        top_n = config.KPI_TOP_N

    unrankable = {
        "TopN_Overlap_Count": np.nan, "TopN_N": top_n,
        "TopN_Overlap_Rate": np.nan, "WorstCase_Match_Rate": np.nan,
    }
    if comparison.empty:
        return unrankable

    elements = pd.DataFrame({
        "ref_loading_pct": comparison["ref_loading_pct"],
        "model_loading_pct": comparison["model_loading_pct"],
        "_id": id_col.reindex(comparison.index),
    }).groupby("_id")[["ref_loading_pct", "model_loading_pct"]].max()

    if len(elements) < 2:
        return unrankable

    ref_top = set(elements.nlargest(min(top_n, len(elements)), "ref_loading_pct").index)
    model_top = set(elements.nlargest(min(top_n, len(elements)), "model_loading_pct").index)
    overlap_count = len(ref_top & model_top)

    return {
        "TopN_Overlap_Count": overlap_count,
        "TopN_N": top_n,
        # Divided by N, as Dim_KPI's K16 formula defines it - not by the number of elements
        # ranked, so a population smaller than N cannot reach 1.0.
        "TopN_Overlap_Rate": overlap_count / top_n,
        "WorstCase_Match_Rate": float(
            elements["ref_loading_pct"].idxmax() == elements["model_loading_pct"].idxmax()
        ),
    }


def contingency_count(group_col: pd.Series) -> int:
    """Distinct contingencies behind a population; the intact state is not one of them."""
    if group_col is None:
        return 0
    return int(group_col.dropna().nunique())


def raw_primitives(comparison: pd.DataFrame, violation: dict, id_col: pd.Series = None,
                    group_col: pd.Series = None) -> dict:
    """The counts and sums KPI_Data carries alongside its computed KPIs.

    These let a pivot re-derive every ratio KPI at any roll-up of the fact table, which an
    average of per-row ratios cannot do.

    The N_AC_Viol / N_DC_Viol / Sum_FN_Loading_AC_pct / Sum_FP_Loading_DC_pct keys keep the
    AC/DC wording the agreed workbook schema fixes. They carry the reference and the
    compared model of this workbook, which the Parameters sheet names.
    """
    false_negatives = violation["_false_negatives_mask"]
    false_positives = violation["_false_positives_mask"]
    ref_violation = comparison["ref_violation"]
    model_violation = comparison["model_violation"]

    return {
        "N_Obs": int(len(comparison)),
        "N_CO": contingency_count(group_col),
        "N_Elements": int(id_col.reindex(comparison.index).nunique()) if id_col is not None else 0,
        "Sum_AbsErr_A": comparison["abs_error"].sum(),
        "Sum_SqErr_A": (comparison["abs_error"] ** 2).sum(),
        "Sum_SignedErr_A": comparison["signed_error"].sum(),
        "Max_AbsErr_A": comparison["abs_error"].max() if len(comparison) else np.nan,
        "Sum_LoadingDev_pp": comparison["signed_loading_deviation"].sum(),
        "Sum_MarginDev_A": comparison["signed_margin_error"].sum(),
        "N_AC_Viol": int(ref_violation.sum()),
        "N_DC_Viol": int(model_violation.sum()),
        # N_TN is not violation_kpis' ref_secure_count: that one is TN + FP.
        "N_TP": int((ref_violation & model_violation).sum()),
        "N_TN": int((~ref_violation & ~model_violation).sum()),
        "Sum_MissedOverload_A": comparison.loc[false_negatives, "ref_overload"].sum(),
        "Sum_FalseOverload_A": comparison.loc[false_positives, "model_overload"].sum(),
        "Sum_FN_Loading_AC_pct": comparison.loc[false_negatives, "ref_loading_pct"].sum(),
        "Sum_FP_Loading_DC_pct": comparison.loc[false_positives, "model_loading_pct"].sum(),
    }


def co_level_kpis(comparison: pd.DataFrame, group_col: pd.Series = None) -> dict:
    """The contingency-level (K21-K23) KPIs: criticality agreement and worst-CO severity.

    A contingency is "critical" when at least one of its observations is in violation. The
    rates below therefore count contingencies, not elements, which is what separates these
    from the element-level Missed Overload / False Alarm rates.

    Only real contingencies take part: intact-state observations carry no contingency id and
    are dropped here, so the All case's base-case half cannot be counted as a CO.

    Formulas follow Dim_KPI's K21, K22 and K23 exactly:

        K21 = N_CO_MissedCritical / N_CO_AC_Viol
        K22 = N_CO_FalseCritical / (N_CO - N_CO_AC_Viol)
        K23 = Sum_WorstCO_SevDev_A / N_CO_SevDev_Obs

    The CO-level primitives those formulas name are returned under private keys. They are
    deliberately **not** written to KPI_Data, whose column set is fixed and has no place for
    them, so these three KPIs ship at their native grain only: a pivot must not try to roll
    them up.
    """
    empty = {
        "Missed_Critical_CO_Rate": np.nan,
        "False_Critical_CO_Rate": np.nan,
        "Mean_WorstCO_SevDev_A": np.nan,
        "_n_co": 0, "_n_co_ref_viol": 0, "_n_co_missed_critical": 0,
        "_n_co_false_critical": 0, "_sum_worstco_sevdev_a": np.nan, "_n_co_sevdev_obs": 0,
    }
    if group_col is None:
        return empty

    groups = group_col.reindex(comparison.index)
    per_co = comparison[groups.notna()]
    if per_co.empty:
        return empty
    labels = groups[groups.notna()]

    ref_critical = per_co["ref_violation"].groupby(labels).any()
    model_critical = per_co["model_violation"].groupby(labels).any()

    n_co = int(len(ref_critical))
    n_co_ref_viol = int(ref_critical.sum())
    n_co_ref_secure = n_co - n_co_ref_viol
    n_missed = int((ref_critical & ~model_critical).sum())
    n_false = int((~ref_critical & model_critical).sum())

    # Worst overload severity in a contingency, reference vs model. "Overload severity" is
    # the magnitude above the limit and never below zero - the same ref_overload /
    # model_overload the volume KPIs are built from - so a contingency neither side
    # overloads contributes a deviation of zero rather than a margin.
    worst_ref = per_co["ref_overload"].groupby(labels).max()
    worst_model = per_co["model_overload"].groupby(labels).max()
    severity_deviation = worst_model - worst_ref
    sum_severity_deviation = severity_deviation.sum()
    n_severity_observations = int(len(severity_deviation))

    return {
        "Missed_Critical_CO_Rate": n_missed / n_co_ref_viol if n_co_ref_viol else np.nan,
        # Denominator is the reference-secure contingencies, mirroring K11's false alarm rate.
        "False_Critical_CO_Rate": n_false / n_co_ref_secure if n_co_ref_secure else np.nan,
        "Mean_WorstCO_SevDev_A": (
            sum_severity_deviation / n_severity_observations if n_severity_observations
            else np.nan
        ),
        "_n_co": n_co,
        "_n_co_ref_viol": n_co_ref_viol,
        "_n_co_missed_critical": n_missed,
        "_n_co_false_critical": n_false,
        "_sum_worstco_sevdev_a": sum_severity_deviation,
        "_n_co_sevdev_obs": n_severity_observations,
    }
