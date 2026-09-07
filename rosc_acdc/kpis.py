"""Implements: Flow deviation, Loading & limit-based, Violation detection,
Severity-based, Ranking & critical element, and Performance KPIs.
"""

import logging

import numpy as np
import pandas as pd
from prettytable import PrettyTable

logger = logging.getLogger(__name__)

VIOLATION_THRESHOLD_PCT = 100
NEAR_LIMIT_THRESHOLDS_PCT = (80, 90, 95)
TOP_N_DEFAULT = 10


def prepare_comparison(df, ac_value_col, dc_value_col, limit_col,
                        ac_loading_col=None, dc_loading_col=None):
    """Normalize an AC/DC comparison dataframe to the common KPI column names."""
    out = pd.DataFrame(index=df.index)
    out["ac_value"] = df[ac_value_col]
    out["dc_value"] = df[dc_value_col]
    out["limit"] = df[limit_col]
    out["ac_loading_pct"] = df[ac_loading_col] if ac_loading_col else out["ac_value"] / out["limit"] * 100
    out["dc_loading_pct"] = df[dc_loading_col] if dc_loading_col else out["dc_value"] / out["limit"] * 100
    out["abs_error"] = (out["dc_value"] - out["ac_value"]).abs()
    out["signed_loading_deviation"] = out["dc_loading_pct"] - out["ac_loading_pct"]
    out["margin_ac"] = out["limit"] - out["ac_value"].abs()
    out["margin_dc"] = out["limit"] - out["dc_value"].abs()
    out["signed_margin_error"] = out["margin_dc"] - out["margin_ac"]
    out["ac_violation"] = out["ac_loading_pct"] > VIOLATION_THRESHOLD_PCT
    out["dc_violation"] = out["dc_loading_pct"] > VIOLATION_THRESHOLD_PCT
    return out


def flow_deviation_kpis(comparison: pd.DataFrame) -> dict:
    """MAE, RMSE, P95 and max absolute error between AC and DC flows."""
    errors = comparison["abs_error"].dropna()
    if errors.empty:
        return {"MAE": np.nan, "RMSE": np.nan, "P95 Error": np.nan, "Max Error": np.nan}
    return {
        "MAE": errors.mean(),
        "RMSE": np.sqrt((errors ** 2).mean()),
        "P95 Error": errors.quantile(0.95),
        "Max Error": errors.max(),
    }


def loading_limit_kpis(comparison: pd.DataFrame) -> dict:
    """Signed loading-% deviation and margin error, overall and near the thermal limit."""
    kpis = {
        "Mean Loading % Deviation (DC-AC)": comparison["signed_loading_deviation"].mean(),
        "Mean Margin Error (DC-AC)": comparison["signed_margin_error"].mean(),
    }
    for threshold in NEAR_LIMIT_THRESHOLDS_PCT:
        subset = comparison[comparison["ac_loading_pct"] >= threshold]
        kpis[f"Near-Limit ({threshold}%+) Mean Loading % Deviation"] = (
            subset["signed_loading_deviation"].mean() if not subset.empty else np.nan
        )
    return kpis


def violation_kpis(comparison: pd.DataFrame) -> dict:
    """False negatives/positives and their rates, using loading% > 100 as the violation flag."""
    ac_violation = comparison["ac_violation"]
    dc_violation = comparison["dc_violation"]

    false_negatives = ac_violation & ~dc_violation
    false_positives = ~ac_violation & dc_violation

    ac_violation_count = int(ac_violation.sum())
    ac_secure_count = int((~ac_violation).sum())

    return {
        "False Negatives": int(false_negatives.sum()),
        "False Positives": int(false_positives.sum()),
        "Missed Overload Rate": (
            false_negatives.sum() / ac_violation_count if ac_violation_count else np.nan
        ),
        "False Alarm Rate": (
            false_positives.sum() / ac_secure_count if ac_secure_count else np.nan
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
        "Missed Overload Volume": (missed["ac_value"].abs() - missed["limit"]).clip(lower=0).sum(),
        "False Overload Volume": (false["dc_value"].abs() - false["limit"]).clip(lower=0).sum(),
        "False Negatives Avg Loading %": missed["ac_loading_pct"].mean() if not missed.empty else np.nan,
        "False Positives Avg Loading %": false["dc_loading_pct"].mean() if not false.empty else np.nan,
    }


def ranking_kpis(comparison: pd.DataFrame, id_col: pd.Series, group_col: pd.Series = None,
                  top_n: int = TOP_N_DEFAULT) -> dict:
    """Top-N critical element overlap and worst-case match rate, between AC and DC rankings.

    When `group_col` is given (e.g. contingency_id), ranking is done per group
    and the reported figures are averaged across groups; otherwise ranking is
    global (base-case usage).
    """
    df = comparison.copy()
    df["_id"] = id_col.reindex(df.index)

    def _group_metrics(group_df):
        if len(group_df) < 2:
            return None
        ac_top = set(group_df.nlargest(min(top_n, len(group_df)), "ac_loading_pct")["_id"])
        dc_top = set(group_df.nlargest(min(top_n, len(group_df)), "dc_loading_pct")["_id"])
        overlap = len(ac_top & dc_top) / len(ac_top) if ac_top else np.nan
        ac_worst = group_df.loc[group_df["ac_loading_pct"].idxmax(), "_id"]
        dc_worst = group_df.loc[group_df["dc_loading_pct"].idxmax(), "_id"]
        return overlap, ac_worst == dc_worst

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


def log_kpi_table(title: str, kpis: dict) -> None:
    """Render a KPI dict as a PrettyTable and log it."""
    table = PrettyTable()
    table.field_names = ["KPI", "Value"]
    table.align["KPI"] = "l"
    table.align["Value"] = "r"
    for name, value in kpis.items():
        if name.startswith("_"):
            continue
        if isinstance(value, float):
            table.add_row([name, f"{value:.3f}"])
        else:
            table.add_row([name, value])
    logger.info("%s\n%s", title, table)


def log_all_priority1_kpis(label: str, comparison: pd.DataFrame, id_col: pd.Series = None,
                            group_col: pd.Series = None) -> None:
    """Compute and log every Priority 1 KPI group for one comparison dataset."""
    log_kpi_table(f"{label} - Flow Deviation KPIs", flow_deviation_kpis(comparison))
    log_kpi_table(f"{label} - Loading & Limit-Based KPIs", loading_limit_kpis(comparison))

    violation = violation_kpis(comparison)
    log_kpi_table(f"{label} - Violation Detection KPIs", violation)
    log_kpi_table(f"{label} - Severity-Based KPIs", severity_kpis(comparison, violation))

    if id_col is not None:
        log_kpi_table(
            f"{label} - Ranking & Critical Element KPIs",
            ranking_kpis(comparison, id_col, group_col),
        )
