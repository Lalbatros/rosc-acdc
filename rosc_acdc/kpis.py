"""Implements: Flow deviation, Loading & limit-based, Violation detection,
Severity-based, Ranking & critical element, and Performance KPIs.

Every KPI compares one model against the reference model, so a comparison frame carries
``ref_*`` and ``model_*`` columns rather than AC/DC ones.
"""

import logging

import numpy as np
import pandas as pd
from prettytable import PrettyTable

logger = logging.getLogger(__name__)

VIOLATION_THRESHOLD_PCT = 100
NEAR_LIMIT_THRESHOLDS_PCT = (80, 90, 95)
TOP_N_DEFAULT = 10


def prepare_comparison(df, reference_value_col, model_value_col, limit_col,
                        reference_loading_col=None, model_loading_col=None):
    """Normalize one model-vs-reference comparison dataframe to the common KPI column names."""
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
    out["abs_error"] = (out["model_value"] - out["ref_value"]).abs()
    out["signed_loading_deviation"] = out["model_loading_pct"] - out["ref_loading_pct"]
    out["margin_ref"] = out["limit"] - out["ref_value"].abs()
    out["margin_model"] = out["limit"] - out["model_value"].abs()
    out["signed_margin_error"] = out["margin_model"] - out["margin_ref"]
    out["ref_violation"] = out["ref_loading_pct"] > VIOLATION_THRESHOLD_PCT
    out["model_violation"] = out["model_loading_pct"] > VIOLATION_THRESHOLD_PCT
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
        "Missed Overload Volume": (missed["ref_value"].abs() - missed["limit"]).clip(lower=0).sum(),
        "False Overload Volume": (false["model_value"].abs() - false["limit"]).clip(lower=0).sum(),
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
