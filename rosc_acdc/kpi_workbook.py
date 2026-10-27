"""Builds the KPI workbook: the KPI fact table, its dimensions and its parameters.

One workbook per reference/model pair, named by config.KPI_WORKBOOK_FILENAME_TEMPLATE, so
the AC/DC pair renders the agreed ``Core_AC_DC_KPI.xlsx`` and every further model in
config.MODELS gets its own file beside it. The schema is intrinsically two-model
(``N_AC_Viol``/``N_DC_Viol``, ``Current_AC_A``/``Current_DC_A``, Dim_KPI's AC-vs-DC prose);
one file per pair is what keeps those names meaningful without changing the agreed format.
Each file names its own pair in the ``Parameters`` sheet.

KPI_Data reports one row per BusinessDay x Timestamp x Country x Case x VoltageLevel. The
tool runs a single snapshot, so BusinessDay and Timestamp are one fixed pair per run and the
grain that actually varies is Country x Case x VoltageLevel.
"""

import datetime
import logging

import numpy as np
import pandas as pd

from rosc_acdc import config, kpis, loadflow, network_io
from rosc_acdc.paths import output_path

logger = logging.getLogger(__name__)

CASE_BASE = "BaseCase"
CASE_CONTINGENCY = "Contingency"
CASE_ALL = "All"

README_SHEET = "README"
KPI_DATA_SHEET = "KPI_Data"
PERF_SHEET = "Perf_Computation"
PARAMETERS_SHEET = "Parameters"
RAW_SAMPLE_SHEET = "Raw_Sample"
DIM_KPI_SHEET = "Dim_KPI"

SHEET_ORDER = [
    README_SHEET, KPI_DATA_SHEET, PERF_SHEET, RAW_SAMPLE_SHEET, "Dim_Date", "Dim_Time",
    "Dim_Country", "Dim_Case", "Dim_VoltageLevel", DIM_KPI_SHEET, PARAMETERS_SHEET,
]

KEY_COLUMNS = [
    "BusinessDay", "Timestamp", "TimestampID", "Country", "Case", "VoltageLevel_kV",
]
FLOW_COLUMNS = ["MAE_A", "RMSE_A", "P95_Err_A", "Max_Err_A"]
LOADING_COLUMNS = [
    "Mean_LoadingDev_pp", "Mean_Signed_Margin_Err_A", "Sum_NearLimit_LoadingDev_pp",
    "N_NearLimit_Obs", "NearLimit_Mean_LoadingDev_pp", "N_NearLimit_Underest",
]
VIOLATION_COLUMNS = ["N_FN", "N_FP", "Missed_Overload_Rate", "False_Alarm_Rate"]
SEVERITY_COLUMNS = [
    "Missed_Overload_Volume_A", "False_Overload_Volume_A", "Avg_FN_Loading_AC_pct",
    "Avg_FP_Loading_DC_pct",
]
RANKING_COLUMNS = ["TopN_Overlap_Count", "TopN_N", "TopN_Overlap_Rate", "WorstCase_Match_Rate"]
PRIORITY2_COLUMNS = [
    "NearLimit_Underest_Rate", "Missed_Critical_CO_Rate", "False_Critical_CO_Rate",
    "Mean_WorstCO_SevDev_A",
]
PRIMITIVE_COLUMNS = [
    "N_Obs", "N_CO", "N_Elements", "Sum_AbsErr_A", "Sum_SqErr_A", "Sum_SignedErr_A",
    "Max_AbsErr_A", "Sum_LoadingDev_pp", "Sum_MarginDev_A", "N_AC_Viol", "N_DC_Viol",
    "N_TP", "N_TN", "Sum_MissedOverload_A", "Sum_FalseOverload_A", "Sum_FN_Loading_AC_pct",
    "Sum_FP_Loading_DC_pct",
]

KPI_DATA_COLUMNS = (
    KEY_COLUMNS + FLOW_COLUMNS + LOADING_COLUMNS + VIOLATION_COLUMNS + SEVERITY_COLUMNS
    + RANKING_COLUMNS + PRIORITY2_COLUMNS + PRIMITIVE_COLUMNS
)

PERF_COLUMNS = [
    "BusinessDay", "Timestamp", "TimestampID", "Process", "Process_Type", "Model",
    "Computation_Time_s", "N_Elements_Evaluated", "N_Contingencies",
]

# ---------------------------------------------------------------------------
# Dim_KPI worksheet catalog and formatting
# ---------------------------------------------------------------------------

DIM_KPI_COLUMNS = [
    "KPI_ID", "KPI_Name", "Section", "Priority", "Unit", "Direction",
    "Definition", "Measure_Logic_DAX", "Target", "Number_Format", "Source_Table",
]

_FLOW = "Flow deviation"
_LOADING = "Loading & limits"
_VIOLATION = "Violation detection"
_SEVERITY = "Severity"
_RANKING = "Ranking & critical elements"
_PERF = "Performance"
_BASE = "Base case"
_CO = "Contingency analysis"
_BIAS = "Bias"

_LOWER = "Lower is better"
_HIGHER = "Higher is better"
_CLOSER = "Closer to 0 is better"

_KPI_DATA = "KPI_Data"
_PERF_COMPUTATION = "Perf_Computation"

# The catalog keeps its agreed AC-vs-DC wording. In a workbook for another pair, "AC" reads
# as the reference model and "DC" as the compared one; the Parameters sheet names both.
# `Direction` and `Target` are documentation for the reader: no cell is conditionally
# formatted against them, by design.
_DIM_KPI_CATALOG = [
    ("K01", "Mean Absolute Error (MAE)", _FLOW, 1, "A", _LOWER,
     "Typical absolute Amp deviation between DC and AC flow.",
     "SUM(Sum_AbsErr_A) / SUM(N_Obs)", 60, "0.0", _KPI_DATA),
    ("K02", "Root Mean Square Error (RMSE)", _FLOW, 1, "A", _LOWER,
     "Overall deviation, penalising large mismatches more heavily.",
     "SQRT( SUM(Sum_SqErr_A) / SUM(N_Obs) )", 90, "0.0", _KPI_DATA),
    ("K03", "95th Percentile Error (P95)", _FLOW, 1, "A", _LOWER,
     "Error level reached in the worst 5% of observations.",
     "Weighted average of P95_AbsErr_A (exact P95 needs the element-level table)",
     200, "0.0", _KPI_DATA),
    ("K04", "Maximum Error", _FLOW, 1, "A", _LOWER,
     "Single largest observed DC vs AC mismatch.",
     "MAX(Max_AbsErr_A)", 400, "0.0", _KPI_DATA),
    ("K05", "Loading Percentage Deviation (signed)", _LOADING, 1, "pp", _CLOSER,
     "Signed difference in element loading, DC minus AC.",
     "SUM(Sum_LoadingDev_pp) / SUM(N_Obs)", 0, "0.00", _KPI_DATA),
    ("K06", "Limit Margin Error (signed)", _LOADING, 1, "A", _CLOSER,
     "Signed difference in remaining margin to the thermal limit, DC minus AC.",
     "SUM(Sum_MarginDev_A) / SUM(N_Obs)", 0, "0.0", _KPI_DATA),
    ("K07", "Near-Limit Error", _LOADING, 1, "pp", _CLOSER,
     "Loading deviation restricted to elements with AC loading >= 90%.",
     "SUM(Sum_NearLimit_LoadingDev_pp) / SUM(N_NearLimit_Obs)", 0, "0.00", _KPI_DATA),
    ("K08", "False Negatives (count)", _VIOLATION, 1, "count", _LOWER,
     "AC shows an overload, DC does not.",
     "SUM(N_FN)", 0, "#,##0", _KPI_DATA),
    ("K09", "False Positives (count)", _VIOLATION, 1, "count", _LOWER,
     "DC shows an overload, AC does not.",
     "SUM(N_FP)", 0, "#,##0", _KPI_DATA),
    ("K10", "Missed Overload Rate", _VIOLATION, 1, "%", _LOWER,
     "Share of AC violations that DC fails to detect.",
     "DIVIDE( SUM(N_FN), SUM(N_AC_Viol) )", 0.1, "0.0%", _KPI_DATA),
    ("K11", "False Alarm Rate", _VIOLATION, 1, "%", _LOWER,
     "Share of AC-secure observations DC flags as violations.",
     "DIVIDE( SUM(N_FP), SUM(N_Obs) - SUM(N_AC_Viol) )", 0.05, "0.0%", _KPI_DATA),
    ("K12", "Missed Overload Volume", _SEVERITY, 1, "A", _LOWER,
     "Total Amp severity of AC overloads DC fails to detect.",
     "SUM(Sum_MissedOverload_A)", 0, "#,##0", _KPI_DATA),
    ("K13", "False Overload Volume", _SEVERITY, 1, "A", _LOWER,
     "Total Amp severity of DC overloads that do not exist in AC.",
     "SUM(Sum_FalseOverload_A)", 0, "#,##0", _KPI_DATA),
    ("K14", "False Negatives avg load", _SEVERITY, 1, "%", _LOWER,
     "Average AC loading of the false-negative observations.",
     "DIVIDE( SUM(Sum_FN_Loading_AC_pct), SUM(N_FN) )", 0, "0.0", _KPI_DATA),
    ("K15", "False Positives avg load", _SEVERITY, 1, "%", _LOWER,
     "Average DC loading of the false-positive observations.",
     "DIVIDE( SUM(Sum_FP_Loading_DC_pct), SUM(N_FP) )", 0, "0.0", _KPI_DATA),
    ("K16", "Top-N Critical Element Overlap", _RANKING, 1, "%", _HIGHER,
     "Overlap between the AC and DC top-N most loaded elements (N = 5).",
     "DIVIDE( SUM(TopN_Overlap_Count), SUM(TopN_N) )", 0.9, "0.0%", _KPI_DATA),
    ("K17", "Worst-Case Match Rate", _RANKING, 1, "%", _HIGHER,
     "How often AC and DC identify the same single worst element.",
     "DIVIDE( SUM(WorstCase_Match_Flag), SUM(WorstCase_Total) )", 0.85, "0.0%", _KPI_DATA),
    ("K18", "Computation time", _PERF, 1, "s", _LOWER,
     "Wall-clock time per process and timestamp.",
     "SUM(Perf_Computation[Computation_Time_s])", 0, "#,##0.0", _PERF_COMPUTATION),
    ("K19", "N-0 Flow MAE / RMSE", _BASE, 2, "A", _LOWER,
     "MAE and RMSE on the base-case subset only.",
     'Same measures as K01/K02, filtered to Case = "BaseCase"', 40, "0.0", _KPI_DATA),
    ("K20", "N-0 Missed Overload Volume", _BASE, 2, "A", _LOWER,
     "Base-case AC overload severity missed by DC.",
     'SUM(Sum_MissedOverload_A) filtered to Case = "BaseCase"', 0, "#,##0", _KPI_DATA),
    ("K21", "Missed Critical CO Rate", _CO, 2, "%", _LOWER,
     "Contingencies critical in AC but not flagged in DC.",
     "DIVIDE( SUM(N_CO_MissedCritical), SUM(N_CO_AC_Viol) )", 0.1, "0.0%", _KPI_DATA),
    ("K22", "False Critical CO Rate", _CO, 2, "%", _LOWER,
     "Contingencies critical in DC but not in AC.",
     "DIVIDE( SUM(N_CO_FalseCritical), SUM(N_CO) - SUM(N_CO_AC_Viol) )",
     0.05, "0.0%", _KPI_DATA),
    ("K23", "Worst CO Severity Deviation", _CO, 2, "A", _CLOSER,
     "Difference in maximum overload severity per contingency, DC minus AC.",
     "DIVIDE( SUM(Sum_WorstCO_SevDev_A), SUM(N_CO_SevDev_Obs) )", 0, "0.0", _KPI_DATA),
    ("K24", "Mean Signed (Margin) Error", _BIAS, 2, "A", _CLOSER,
     "Positive = DC systematically overstates available margin (optimistic).",
     "SUM(Sum_MarginDev_A) / SUM(N_Obs)", 0, "0.0", _KPI_DATA),
    ("K25", "Near-Limit Underestimation Rate", _BIAS, 2, "%", _LOWER,
     "How often DC underestimates loading among near-limit elements.",
     "DIVIDE( SUM(N_NearLimit_Underest), SUM(N_NearLimit_Obs) )", 0.5, "0.0%", _KPI_DATA),
]

COLUMN_BY_KPI_ID = {
    "K01": "MAE_A",
    "K02": "RMSE_A",
    "K03": "P95_Err_A",
    "K04": "Max_Err_A",
    "K05": "Mean_LoadingDev_pp",
    "K06": "Mean_Signed_Margin_Err_A",
    "K07": "NearLimit_Mean_LoadingDev_pp",
    "K08": "N_FN",
    "K09": "N_FP",
    "K10": "Missed_Overload_Rate",
    "K11": "False_Alarm_Rate",
    "K12": "Missed_Overload_Volume_A",
    "K13": "False_Overload_Volume_A",
    "K14": "Avg_FN_Loading_AC_pct",
    "K15": "Avg_FP_Loading_DC_pct",
    "K16": "TopN_Overlap_Rate",
    "K17": "WorstCase_Match_Rate",
    "K18": "Computation_Time_s",
    "K21": "Missed_Critical_CO_Rate",
    "K22": "False_Critical_CO_Rate",
    "K23": "Mean_WorstCO_SevDev_A",
    "K24": "Mean_Signed_Margin_Err_A",
    "K25": "NearLimit_Underest_Rate",
}

PRIMITIVE_COUNT_FORMAT = "#,##0"
PRIMITIVE_SUM_FORMAT = "#,##0.0"


def embedded_catalog() -> pd.DataFrame:
    """The Dim_KPI catalog as a worksheet-ready dataframe."""
    return pd.DataFrame(_DIM_KPI_CATALOG, columns=DIM_KPI_COLUMNS)


def column_formats(catalog: pd.DataFrame, columns) -> dict:
    """column -> Excel number format, for the columns Dim_KPI governs.

    Only the format is read from the catalog. `Direction` and `Target` are documentation,
    not thresholds applied to cells, so nothing else is threaded through from here.
    """
    formats = {}
    known = set(columns)

    for row in catalog.itertuples():
        column = COLUMN_BY_KPI_ID.get(str(getattr(row, "KPI_ID", "") or ""))
        if column is None or column not in known:
            continue

        number_format = getattr(row, "Number_Format", None)
        if number_format is None or (
            isinstance(number_format, float) and np.isnan(number_format)
        ):
            continue
        number_format = str(number_format)

        existing = formats.get(column)
        if existing is not None and existing != number_format:
            logger.warning(
                "Dim_KPI: %s is governed by two rows with different Number_Format "
                "(%s vs %s); keeping the first", column, existing, number_format,
            )
            continue
        formats[column] = number_format
    return formats


def base_case_readings(kpi_data: pd.DataFrame, base_case_label: str = CASE_BASE) -> dict:
    """K19 and K20: K01/K02 and K12 re-derived over the BaseCase rows only.

    They are not KPI_Data columns - the schema is fixed and has no place for them - so they
    are read back off the fact table and logged.
    """
    empty = {"K19_MAE_A": np.nan, "K19_RMSE_A": np.nan, "K20_Missed_Overload_Volume_A": np.nan}
    if kpi_data.empty or "Case" not in kpi_data.columns:
        return empty

    base = kpi_data[kpi_data["Case"] == base_case_label]
    if base.empty:
        logger.warning(
            "Dim_KPI: no rows with Case == %r, so K19/K20 have nothing to read",
            base_case_label,
        )
        return empty

    observations = base["N_Obs"].sum()
    if not observations:
        return empty

    return {
        "K19_MAE_A": base["Sum_AbsErr_A"].sum() / observations,
        "K19_RMSE_A": (base["Sum_SqErr_A"].sum() / observations) ** 0.5,
        "K20_Missed_Overload_Volume_A": base["Sum_MissedOverload_A"].sum(),
    }


# ---------------------------------------------------------------------------
# Dimension sheets
# ---------------------------------------------------------------------------

DIM_COUNTRY_COLUMNS = [
    "Country", "Country_Name", "TSO", "Latitude", "Longitude", "CCR", "Bidding_Zone",
    "N_Voltage_Levels",
]

# Core CCR member countries: public reference data, keyed by the ISO code pypowsybl reads
# off the substation. Only the countries the loaded model actually contains reach the sheet,
# so this map describes the capacity-calculation region rather than any one study's inputs.
_CORE_COUNTRIES = {
    "AT": ("Austria", "APG", "AT"),
    "BE": ("Belgium", "Elia", "BE"),
    "CZ": ("Czechia", "CEPS", "CZ"),
    "DE": ("Germany", "50Hertz / Amprion / TenneT DE / TransnetBW", "DE-LU"),
    "FR": ("France", "RTE", "FR"),
    "HR": ("Croatia", "HOPS", "HR"),
    "HU": ("Hungary", "MAVIR", "HU"),
    "LU": ("Luxembourg", "Creos", "DE-LU"),
    "NL": ("Netherlands", "TenneT NL", "NL"),
    "PL": ("Poland", "PSE", "PL"),
    "RO": ("Romania", "Transelectrica", "RO"),
    "SI": ("Slovenia", "ELES", "SI"),
    "SK": ("Slovakia", "SEPS", "SK"),
}


def build_dim_country(locations: pd.DataFrame) -> pd.DataFrame:
    """Dim_Country for the countries this model actually contains.

    Derived from the elements' own locations rather than hardcoded, so the sheet describes
    the loaded network. A country outside the static Core map above still gets a row, with
    its code standing in for the name and no TSO - better an honest gap than a missing
    country. `Latitude`/`Longitude` are part of the agreed schema but the network carries no
    geodata, so they stay empty rather than being invented.

    `N_Voltage_Levels` counts the distinct nominal voltages the elements of that country are
    reported at, which is the grain KPI_Data groups by.
    """
    if locations is None or locations.empty:
        return pd.DataFrame(columns=DIM_COUNTRY_COLUMNS)

    voltage_levels_per_country = (
        locations.groupby("Country")["VoltageLevel_kV"].nunique()
    )

    rows = []
    for country, n_voltage_levels in voltage_levels_per_country.items():
        name, tso, bidding_zone = _CORE_COUNTRIES.get(country, (country, None, None))
        rows.append({
            "Country": country,
            "Country_Name": name,
            "TSO": tso,
            "Latitude": None,
            "Longitude": None,
            "CCR": "Core" if country in _CORE_COUNTRIES else None,
            "Bidding_Zone": bidding_zone,
            "N_Voltage_Levels": int(n_voltage_levels),
        })
    return pd.DataFrame(rows, columns=DIM_COUNTRY_COLUMNS)


def build_dim_date(business_day) -> pd.DataFrame:
    """Dim_Date for the single business day this snapshot represents."""
    day = pd.Timestamp(business_day)
    return pd.DataFrame([{
        "BusinessDay": business_day,
        "Year": int(day.year),
        "Quarter": int(day.quarter),
        "Month": int(day.month),
        "Month_Name": day.strftime("%B"),
        "Day": int(day.day),
        "Day_Of_Week": day.strftime("%A"),
        "Is_Weekend": bool(day.dayofweek >= 5),
        "ISO_Week": int(day.isocalendar().week),
    }])


def build_dim_time(timestamp, timestamp_id) -> pd.DataFrame:
    """Dim_Time for the single timestamp this snapshot represents (naive UTC)."""
    moment = pd.Timestamp(timestamp)
    return pd.DataFrame([{
        "TimestampID": timestamp_id,
        "Timestamp": timestamp,
        "Hour_UTC": int(moment.hour),
        "Minute": int(moment.minute),
        "Time_Label": moment.strftime("%H:%M"),
        "Day_Period": (
            "Night" if moment.hour < 6 else
            "Morning" if moment.hour < 12 else
            "Afternoon" if moment.hour < 18 else "Evening"
        ),
    }])


def build_dim_case() -> pd.DataFrame:
    """Dim_Case: what each Case value in KPI_Data selects."""
    return pd.DataFrame([
        {"Case": CASE_BASE, "Case_Name": "Intact network (N-0)",
         "Description": "Base-case observations only, one row per element on its binding side."},
        {"Case": CASE_CONTINGENCY, "Case_Name": "Contingency (N-1)",
         "Description": "Security-analysis observations only, one row per element, side and "
                        "contingency. Intact-network rows are excluded here; they are "
                        "already counted under BaseCase."},
        {"Case": CASE_ALL, "Case_Name": "Pooled",
         "Description": "The union of the two rows above, recomputed over the pooled "
                        "observations - never an arithmetic combination of them, which "
                        "would get P95 and every ratio wrong. N_Obs is therefore the sum of "
                        "the other two."},
    ])


def build_dim_voltage_level(locations: pd.DataFrame) -> pd.DataFrame:
    """Dim_VoltageLevel for the nominal voltages present, with a reporting band."""
    if locations is None or locations.empty:
        return pd.DataFrame(columns=["VoltageLevel_kV", "Voltage_Label", "Band", "N_Elements"])

    counts = locations.groupby("VoltageLevel_kV").size()
    rows = []
    for voltage, n_elements in counts.items():
        if pd.isna(voltage):
            continue
        rows.append({
            "VoltageLevel_kV": voltage,
            "Voltage_Label": f"{voltage:g} kV",
            "Band": (
                "EHV (>= 300 kV)" if voltage >= 300 else
                "HV (200-299 kV)" if voltage >= 200 else
                "HV (100-199 kV)" if voltage >= 100 else "MV (< 100 kV)"
            ),
            "N_Elements": int(n_elements),
        })
    return pd.DataFrame(rows, columns=["VoltageLevel_kV", "Voltage_Label", "Band", "N_Elements"])


def build_readme(reference: str, model: str) -> pd.DataFrame:
    """The README sheet: what this workbook is and how to read it."""
    rows = [
        ("Workbook", f"AC-vs-DC KPI comparison: {model} measured against {reference}."),
        ("Pair", f"Reference model = {reference}; compared model = {model}. One workbook is "
                 f"written per compared model, so columns naming AC and DC mean the "
                 f"reference and the compared model of this file. Parameters names both."),
        ("Grain", "KPI_Data carries one row per BusinessDay x Timestamp x Country x Case x "
                  "VoltageLevel. The study runs a single snapshot, so the grain that varies "
                  "is Country x Case x VoltageLevel."),
        ("Case", 'BaseCase = N-0 only, Contingency = N-1 only, All = the two pooled and '
                 'recomputed. See Dim_Case. All.N_Obs equals BaseCase.N_Obs + '
                 'Contingency.N_Obs for the same Country and VoltageLevel.'),
        ("Units", "Every flow, limit, margin and overload figure is a current in Amperes. "
                  "Loadings and their deviations are percentages; a deviation of loadings is "
                  "in percentage points (pp)."),
        ("Roll-ups", "Each KPI_Data row carries the raw counts and sums behind its ratios "
                     "(N_Obs, Sum_AbsErr_A, ...), so a pivot re-derives any KPI at any "
                     "roll-up using Dim_KPI's Measure_Logic_DAX. Averaging the ratio "
                     "columns instead would be wrong."),
        ("Exceptions", "Missed_Critical_CO_Rate, False_Critical_CO_Rate and "
                       "Mean_WorstCO_SevDev_A (K21-K23) count contingencies, not "
                       "observations, and their primitives are not in the schema. They are "
                       "valid at the row grain only and must not be rolled up."),
        ("Dim_KPI", "Direction and Target document what good looks like. No cell in this "
                    "workbook is conditionally formatted or flagged against them."),
        ("Raw_Sample", f"A review extract, capped at "
                       f"{config.KPI_MAX_RECORDS_PER_SCENARIO} rows per scenario and sorted "
                       f"worst-first. Every KPI above is computed on the full population, "
                       f"not on this sample."),
        ("Scope", f"Elements below {config.KPI_MIN_VOLTAGE_LEVEL_KV:g} kV are excluded, and "
                  f"the two source datasets are already filtered by loading "
                  f"(base case > {config.BASE_CASE_ACTIVE_THRESHOLD_PCT}%, SA > "
                  f"{config.SA_ACTIVE_THRESHOLD_PCT}%). The population is therefore the "
                  f"monitored elements above those loadings."),
        ("Country", "Read per element from the substation behind its binding side. "
                    f"{config.KPI_FALLBACK_COUNTRY} means the substation carries no country, "
                    "which is every element in a model imported without its boundary file."),
    ]
    return pd.DataFrame(rows, columns=["Topic", "Notes"])


# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------


def run_timestamp(network) -> tuple:
    """(BusinessDay, Timestamp, TimestampID) for this run, from the network's own case date.

    `case_date` is the scenario time the CGMES import read out of the model's FullModel
    header, which is the time the network actually represents. The CGM filename encodes a
    time too and is not used: the two can disagree by a day or more on a given sample, and
    the .xiidm import path never sees a CGMES filename at all.

    BusinessDay is the UTC date, with no timezone conversion. TimestampID is the UTC hour,
    so it names the hour-slot this snapshot represents rather than counting runs.

    Excel holds no timezone, so the returned Timestamp is naive UTC.
    """
    case_date = network.case_date
    if case_date.tzinfo is not None:
        case_date = case_date.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return case_date.date(), case_date, int(case_date.hour)


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


def _observations(comparison, element_ids, contingency_ids, case, locations, element_types):
    """One comparison dataset as observation rows carrying their grouping keys."""
    observations = comparison.copy()
    observations["Element_Id"] = element_ids.reindex(comparison.index)
    observations["Contingency_Id"] = (
        contingency_ids.reindex(comparison.index) if contingency_ids is not None
        else pd.Series(np.nan, index=comparison.index)
    )
    observations["Case"] = case

    located = locations.reindex(observations["Element_Id"])
    observations["Country"] = located["Country"].to_numpy()
    observations["VoltageLevel_kV"] = located["VoltageLevel_kV"].to_numpy()
    observations["Element_Type"] = (
        element_types.reindex(observations["Element_Id"]).to_numpy()
        if element_types is not None else None
    )
    return observations.reset_index(drop=True)


def filter_voltage_levels(observations: pd.DataFrame, min_voltage_kv: float = None) -> pd.DataFrame:
    """Exclude network elements below the minimum voltage threshold before KPI calculation."""
    if observations is None or observations.empty:
        return observations
    if min_voltage_kv is None:
        min_voltage_kv = config.KPI_MIN_VOLTAGE_LEVEL_KV

    filtered = observations[observations["VoltageLevel_kV"] >= min_voltage_kv].copy()
    dropped = len(observations) - len(filtered)
    if dropped:
        logger.info(
            "KPI_Data: excluded %d observation(s) below %.0f kV minimum voltage threshold",
            dropped, min_voltage_kv,
        )
    return filtered.reset_index(drop=True)


def build_observations(locations, base_case_comparison=None, base_case_ids=None,
                        sa_comparison=None, sa_element_ids=None, sa_contingency_ids=None,
                        element_types=None):
    """Pool the base-case and contingency observations into one table with grouping keys.

    The Case populations are then slices of this table, which is what makes `All` the union
    of the other two rather than a third calculation: the same KPI functions run over the
    same observations, just a wider selection of them. It is also the only way to get a
    correct pooled P95, which cannot be rebuilt from summed primitives.
    """
    frames = []

    if base_case_comparison is not None and not base_case_comparison.empty:
        frames.append(_observations(
            base_case_comparison, base_case_ids, None, CASE_BASE, locations, element_types,
        ))

    if sa_comparison is not None and not sa_comparison.empty:
        # The SA dataset carries the intact network as rows with no contingency id
        # (security_analysis.build_shortlist_and_full_comparison concatenates them in). They
        # are the base case a second time, so counting them here would double-count every
        # N-0 observation in the All rows. They are dropped explicitly rather than left to
        # prepare_comparison's unpaired-value filter: that filter happens to remove them
        # today only because the N-state currents are deliberately blanked upstream, and
        # giving those rows real currents must not silently reintroduce the double count.
        contingency_only = sa_contingency_ids.reindex(sa_comparison.index).notna()
        dropped = int((~contingency_only).sum())
        if dropped:
            logger.info(
                "KPI_Data: excluded %d intact-network row(s) from the Contingency case; "
                "the base case is already counted under Case=BaseCase", dropped,
            )
        frames.append(_observations(
            sa_comparison[contingency_only], sa_element_ids, sa_contingency_ids,
            CASE_CONTINGENCY, locations, element_types,
        ))

    if not frames:
        return pd.DataFrame()
    return filter_voltage_levels(pd.concat(frames, ignore_index=True))


# ---------------------------------------------------------------------------
# One KPI_Data row
# ---------------------------------------------------------------------------


def _kpi_row(observations: pd.DataFrame, pair_label: str) -> dict:
    """Every KPI and primitive for one (Country, Case, VoltageLevel) slice."""
    element_ids = observations["Element_Id"]
    contingency_ids = observations["Contingency_Id"]

    flow = kpis.flow_deviation_kpis(observations)
    loading = kpis.loading_limit_kpis(observations, pair_label)
    near_limit = kpis.near_limit_kpis(observations)
    violation = kpis.violation_kpis(observations)
    severity = kpis.severity_kpis(observations, violation)
    ranking = kpis.element_ranking_kpis(observations, element_ids)
    co_level = kpis.co_level_kpis(observations, contingency_ids)
    primitives = kpis.raw_primitives(observations, violation, element_ids, contingency_ids)

    row = {
        "MAE_A": flow["MAE"],
        "RMSE_A": flow["RMSE"],
        "P95_Err_A": flow["P95 Error"],
        "Max_Err_A": flow["Max Error"],

        # Built with the same f-string loading_limit_kpis uses, never a hardcoded label: the
        # pair label is a config product, so a literal "(DC-AC)" here would be a KeyError on
        # any other pair.
        "Mean_LoadingDev_pp": loading[f"Mean Loading % Deviation ({pair_label})"],
        "Mean_Signed_Margin_Err_A": loading[f"Mean Margin Error ({pair_label})"],
        "Sum_NearLimit_LoadingDev_pp": near_limit["Sum_NearLimit_LoadingDev_pp"],
        "N_NearLimit_Obs": near_limit["N_NearLimit_Obs"],
        # Taken from near_limit_kpis, which keys it by name, rather than from
        # loading_limit_kpis, whose near-limit keys embed the threshold value.
        "NearLimit_Mean_LoadingDev_pp": near_limit["NearLimit_Mean_LoadingDev_pp"],
        "N_NearLimit_Underest": near_limit["N_NearLimit_Underest"],

        "N_FN": violation["False Negatives"],
        "N_FP": violation["False Positives"],
        "Missed_Overload_Rate": violation["Missed Overload Rate"],
        "False_Alarm_Rate": violation["False Alarm Rate"],

        "Missed_Overload_Volume_A": severity["Missed Overload Volume"],
        "False_Overload_Volume_A": severity["False Overload Volume"],
        "Avg_FN_Loading_AC_pct": severity["False Negatives Avg Loading %"],
        "Avg_FP_Loading_DC_pct": severity["False Positives Avg Loading %"],

        "TopN_Overlap_Count": ranking["TopN_Overlap_Count"],
        "TopN_N": ranking["TopN_N"],
        "TopN_Overlap_Rate": ranking["TopN_Overlap_Rate"],
        "WorstCase_Match_Rate": ranking["WorstCase_Match_Rate"],

        "NearLimit_Underest_Rate": near_limit["NearLimit_Underest_Rate"],
        "Missed_Critical_CO_Rate": co_level["Missed_Critical_CO_Rate"],
        "False_Critical_CO_Rate": co_level["False_Critical_CO_Rate"],
        "Mean_WorstCO_SevDev_A": co_level["Mean_WorstCO_SevDev_A"],
    }
    row.update(primitives)
    return row


def build_kpi_data(observations: pd.DataFrame, business_day, timestamp, timestamp_id,
                    pair_label: str):
    """The KPI_Data fact table: one row per Country x Case x VoltageLevel for this snapshot.

    `All` pools the BaseCase and Contingency observations and runs the same KPI functions
    over the union. It is never an arithmetic combination of the other two rows - that would
    get P95 wrong, and would re-derive every ratio from ratios.

    Country is whatever each element's own location resolved to. It is never overwritten
    wholesale from the input filename: on a merged or CGM model that would collapse every
    row onto one country and defeat the per-country grain.
    """
    if observations.empty:
        logger.warning("KPI_Data: no observations, writing an empty sheet")
        return pd.DataFrame(columns=KPI_DATA_COLUMNS)

    rows = []
    group_keys = ["Country", "VoltageLevel_kV"]
    for (country, voltage_level), located in observations.groupby(group_keys, dropna=False):
        populations = [
            (CASE_BASE, located[located["Case"] == CASE_BASE]),
            (CASE_CONTINGENCY, located[located["Case"] == CASE_CONTINGENCY]),
            (CASE_ALL, located),
        ]
        for case, population in populations:
            if population.empty:
                continue
            row = {
                "BusinessDay": business_day,
                "Timestamp": timestamp,
                "TimestampID": timestamp_id,
                "Country": country,
                "Case": case,
                "VoltageLevel_kV": voltage_level,
            }
            row.update(_kpi_row(population, pair_label))
            rows.append(row)

    kpi_data = pd.DataFrame(rows, columns=KPI_DATA_COLUMNS)
    logger.info(
        "KPI_Data: %d rows over %d country/voltage-level combination(s)",
        len(kpi_data),
        kpi_data.groupby(group_keys, dropna=False).ngroups if len(kpi_data) else 0,
    )
    return kpi_data


# ---------------------------------------------------------------------------
# Perf_Computation
# ---------------------------------------------------------------------------


def build_perf_computation(business_day, timestamp, timestamp_id, stage_times,
                            n_elements_evaluated, n_contingencies):
    """One row per computation stage: the 9 confirmed columns, and nothing else.

    No totals and no speed-up ratio: those are not among Perf_Computation's columns, and
    this workbook writes only what the schema carries - a pivot sums the rows instead.

    `stage_times` is an ordered (Process, Process_Type, Model, seconds) sequence. A stage
    that did not run carries None and is left out rather than written as a zero, which would
    read as "ran instantly".
    """
    rows = [
        {
            "BusinessDay": business_day,
            "Timestamp": timestamp,
            "TimestampID": timestamp_id,
            "Process": process,
            "Process_Type": process_type,
            "Model": model,
            "Computation_Time_s": seconds,
            "N_Elements_Evaluated": n_elements_evaluated,
            "N_Contingencies": n_contingencies,
        }
        for process, process_type, model, seconds in stage_times
        if seconds is not None
    ]
    return pd.DataFrame(rows, columns=PERF_COLUMNS)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def build_parameters(reference: str, model: str, single_sided_elements=None) -> pd.DataFrame:
    """The config this run actually used, reported after the fact.

    Nobody edits this sheet to change a run; the input is rosc_acdc/config.py plus the
    git-ignored input/config_local.py override.
    """
    rows = [
        ("Reference model", reference,
         "The model every KPI measures against. Columns naming AC carry this model."),
        ("Compared model", model,
         "The model being assessed. Columns naming DC carry this model. One workbook is "
         "written per compared model in config.MODELS."),
        ("Violation threshold (% loading)", config.KPI_VIOLATION_THRESHOLD_PCT,
         "An element is in violation above this loading (>). Drives N_AC_Viol, N_DC_Viol, "
         "N_FN, N_FP, N_TP, N_TN."),
        ("Near-limit threshold (% loading, reference)", config.KPI_NEAR_LIMIT_THRESHOLD_PCT,
         "Near-limit KPIs look only at elements whose reference loading reaches this (>=). "
         "Distinct from the violation threshold above."),
        ("Top-N for critical element overlap", config.KPI_TOP_N,
         "N for TopN_Overlap_Count and the TopN_N column."),
        ("Minimum voltage level (kV)", config.KPI_MIN_VOLTAGE_LEVEL_KV,
         "Elements whose binding side is below this nominal voltage are excluded before any "
         "KPI aggregation."),
        ("Max Raw_Sample records per scenario", config.KPI_MAX_RECORDS_PER_SCENARIO,
         "Caps the Raw_Sample review sheet only. Every KPI is computed on the full "
         "population."),
        ("Business days simulated", config.KPI_BUSINESS_DAYS_SIMULATED,
         "Report-only: this build runs a single snapshot."),
        ("Timestamps per business day", config.KPI_TIMESTAMPS_PER_BUSINESS_DAY,
         "Report-only: this build runs a single snapshot."),
        ("Contingencies per country / voltage level",
         config.KPI_CONTINGENCIES_PER_COUNTRY_VOLTAGE_LEVEL,
         "Report-only. Perf_Computation's N_Contingencies reports the contingencies the "
         "security analysis actually ran, not this value."),
        ("Flow unit", config.KPI_FLOW_UNIT, "Display and labelling only."),
        ("Default country (unresolved)", config.KPI_FALLBACK_COUNTRY,
         "Reported for elements whose substation carries no country, which is every element "
         "in a model imported without its boundary files."),
    ]
    if single_sided_elements is not None:
        rows.append((
            "Elements rated on one side only", len(single_sided_elements),
            "A note on the input ratings, not a KPI: such an element has no second side to "
            "compare, so its binding side is decided by the data rather than by the load "
            "flow.",
        ))
    return pd.DataFrame(rows, columns=["Parameter", "Value", "Comment"])


# ---------------------------------------------------------------------------
# Raw_Sample
# ---------------------------------------------------------------------------

RAW_SAMPLE_COLUMNS = [
    "BusinessDay", "Timestamp", "TimestampID", "Country", "Case", "VoltageLevel_kV",
    "Element_ID", "Contingency_Id", "Element_Type", "Current_AC_A", "Current_DC_A",
    "PATL_A", "Loading_AC_pct", "Loading_DC_pct", "Viol_AC", "Viol_DC", "NearLimit",
    "TN", "TP", "FP", "FN",
]

# Worst-first within each scenario, all descending: anything either model flags, then
# anything the two disagree about, then near-limit, then by loading and by error size.
_RAW_SAMPLE_SORT_COLUMNS = [
    "_violation_flag", "_class_flag", "NearLimit", "Loading_AC_pct", "Loading_DC_pct",
    "_signed_loading_deviation", "_abs_error",
]

# Appended as final, ascending tiebreakers so the sheet's row order is reproducible. Roughly
# half the rows tie exactly on the keys above - the same element under many contingencies
# often gives identical readings - and the security analysis currents only reproduce to about
# 1e-10 between runs. Without a tiebreaker that wobble silently permutes the sheet, which
# makes diffing two workbooks useless. It does not make the file bit-identical across runs:
# rows whose keys genuinely differ by less than the noise can still swap.
_RAW_SAMPLE_TIEBREAK_COLUMNS = ["Element_ID", "Contingency_Id"]


def build_raw_sample(observations: pd.DataFrame, business_day, timestamp, timestamp_id,
                      element_names: pd.Series = None,
                      max_records_per_scenario: int = None) -> pd.DataFrame:
    """Build the Raw_Sample worksheet from the actual KPI observation rows.

    A review extract, capped per scenario and sorted worst-first, while every KPI above
    keeps running on the full population. Built with frame operations rather than row by
    row: the observation set is the whole monitored population times every contingency.

    Reads the comparison columns by direct indexing, not `.get()`: a future rename of the
    comparison schema has to fail here rather than quietly fill the sheet with blanks.
    """
    if observations is None or observations.empty:
        return pd.DataFrame(columns=RAW_SAMPLE_COLUMNS)

    if max_records_per_scenario is None:
        max_records_per_scenario = config.KPI_MAX_RECORDS_PER_SCENARIO

    filtered = filter_voltage_levels(observations)
    if filtered.empty:
        return pd.DataFrame(columns=RAW_SAMPLE_COLUMNS)

    reference_violation = filtered["ref_violation"].fillna(False).astype(bool)
    model_violation = filtered["model_violation"].fillna(False).astype(bool)
    near_limit = filtered["ref_loading_pct"] >= config.KPI_NEAR_LIMIT_THRESHOLD_PCT

    element_ids = filtered["Element_Id"]
    labels = element_ids.astype(object)
    if element_names is not None:
        resolved = element_names.reindex(element_ids).to_numpy()
        resolved = pd.Series(resolved, index=filtered.index).astype(object)
        resolved = resolved.where(
            resolved.notna() & (resolved.astype(str).str.strip() != ""), labels,
        )
        labels = resolved

    sample = pd.DataFrame({
        "BusinessDay": business_day,
        "Timestamp": timestamp,
        "TimestampID": timestamp_id,
        "Country": filtered["Country"],
        "Case": filtered["Case"],
        "VoltageLevel_kV": filtered["VoltageLevel_kV"],
        "Element_ID": labels,
        "Contingency_Id": filtered["Contingency_Id"],
        "Element_Type": filtered["Element_Type"],
        "Current_AC_A": filtered["ref_value"],
        "Current_DC_A": filtered["model_value"],
        "PATL_A": filtered["limit"],
        "Loading_AC_pct": filtered["ref_loading_pct"],
        "Loading_DC_pct": filtered["model_loading_pct"],
        "Viol_AC": reference_violation.astype(int),
        "Viol_DC": model_violation.astype(int),
        "NearLimit": near_limit.astype(int),
        "TN": (~reference_violation & ~model_violation).astype(int),
        "TP": (reference_violation & model_violation).astype(int),
        "FP": (~reference_violation & model_violation).astype(int),
        "FN": (reference_violation & ~model_violation).astype(int),
        "_violation_flag": (reference_violation | model_violation).astype(int),
        "_class_flag": (reference_violation != model_violation).astype(int),
        "_signed_loading_deviation": filtered["signed_loading_deviation"],
        "_abs_error": filtered["abs_error"],
    })

    # One scenario is one contingency, or the Case itself for the rows that carry none.
    scenario = filtered["Contingency_Id"].astype(object)
    blank = scenario.isna() | (scenario.astype(str).str.strip() == "")
    scenario = scenario.where(~blank, filtered["Case"])

    order = sample.sort_values(
        by=_RAW_SAMPLE_SORT_COLUMNS + _RAW_SAMPLE_TIEBREAK_COLUMNS,
        ascending=(
            [False] * len(_RAW_SAMPLE_SORT_COLUMNS)
            + [True] * len(_RAW_SAMPLE_TIEBREAK_COLUMNS)
        ),
        kind="mergesort", na_position="last",
    ).index
    capped = (
        sample.loc[order]
        .groupby(scenario.loc[order], sort=False, dropna=False)
        .head(max_records_per_scenario)
    )
    return capped.reset_index(drop=True)[RAW_SAMPLE_COLUMNS]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _default_format(column: str):
    """The format for a column Dim_KPI does not govern - a primitive or a key column.

    Assigned by name so adding a primitive column cannot silently leave it unformatted.
    """
    if column == "BusinessDay":
        return "yyyy-mm-dd"
    if column == "Timestamp":
        return "yyyy-mm-dd hh:mm"
    if column.startswith(("Sum_", "Max_")):
        return PRIMITIVE_SUM_FORMAT
    if column.startswith(("N_", "TopN_")) or column in ("TimestampID", "VoltageLevel_kV"):
        return PRIMITIVE_COUNT_FORMAT
    return None


def _number_formats(catalog: pd.DataFrame, columns) -> dict:
    """column -> number format: Dim_KPI for the KPI columns, name-based defaults elsewhere."""
    formats = {column: _default_format(column) for column in columns}
    formats.update(column_formats(catalog, columns))
    return formats


def _apply_formats(worksheet, columns, formats) -> None:
    """Apply number formatting only.

    Cells keep the standard Excel font and no fill: the workbook is deliberately neutral,
    and Dim_KPI's Direction/Target are documentation rather than conditional formatting.
    """
    for index, column in enumerate(columns, start=1):
        number_format = formats.get(column)
        if not number_format:
            continue
        for row in range(2, worksheet.max_row + 1):
            worksheet.cell(row=row, column=index).number_format = number_format


def write_workbook(sheets: dict, catalog: pd.DataFrame, filename: str) -> str:
    """Write every tab in SHEET_ORDER and return the path.

    The workbook is overwritten on every run - no versioning, no timestamp in the name.
    """
    path = output_path(filename)
    kpi_data = sheets[KPI_DATA_SHEET]
    perf_computation = sheets[PERF_SHEET]
    formats = _number_formats(
        catalog, list(kpi_data.columns) + list(perf_computation.columns),
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name in SHEET_ORDER:
            sheets[sheet_name].to_excel(writer, sheet_name=sheet_name, index=False)

        _apply_formats(writer.sheets[KPI_DATA_SHEET], list(kpi_data.columns), formats)
        _apply_formats(writer.sheets[PERF_SHEET], list(perf_computation.columns), formats)

        # Written in order above, so this only guards against openpyxl having placed a sheet
        # elsewhere; move_sheet is used rather than assigning to the private book._sheets.
        book = writer.book
        for position, sheet_name in enumerate(SHEET_ORDER):
            current = book.sheetnames.index(sheet_name)
            if current != position:
                book.move_sheet(sheet_name, offset=position - current)

    logger.info(
        "Wrote %s: %d KPI_Data row(s), %d Perf_Computation row(s), %d Raw_Sample row(s), "
        "%d Dim_KPI row(s)",
        path, len(kpi_data), len(perf_computation), len(sheets[RAW_SAMPLE_SHEET]),
        len(catalog),
    )
    return path


def build_and_write(network, locations, stage_times, n_elements_evaluated, n_contingencies,
                     reference: str, model: str, base_case_comparison=None,
                     base_case_ids=None, sa_comparison=None, sa_element_ids=None,
                     sa_contingency_ids=None, element_names: pd.Series = None,
                     element_types: pd.Series = None, single_sided_elements=None) -> str:
    """Build every tab for one reference/model pair and write that pair's workbook."""
    business_day, timestamp, timestamp_id = run_timestamp(network)
    pair_label = f"{model}-{reference}"
    logger.info(
        "KPI workbook [%s vs %s]: BusinessDay=%s Timestamp=%s TimestampID=%s",
        model, reference, business_day, timestamp, timestamp_id,
    )

    observations = build_observations(
        locations, base_case_comparison, base_case_ids,
        sa_comparison, sa_element_ids, sa_contingency_ids, element_types,
    )
    kpi_data = build_kpi_data(observations, business_day, timestamp, timestamp_id, pair_label)
    raw_sample = build_raw_sample(
        observations, business_day, timestamp, timestamp_id, element_names=element_names,
    )

    # K19/K20 are not columns: they are K01/K02 and K12 read on the BaseCase rows. Logged
    # rather than written, since KPI_Data's schema is fixed and has no place for them.
    readings = base_case_readings(kpi_data, CASE_BASE)
    logger.info(
        "[%s vs %s] K19 (N-0 MAE/RMSE) = %.3f / %.3f A; "
        "K20 (N-0 Missed Overload Volume) = %.3f A",
        model, reference, readings["K19_MAE_A"], readings["K19_RMSE_A"],
        readings["K20_Missed_Overload_Volume_A"],
    )

    catalog = embedded_catalog()
    # One row per *element* that reached the workbook, not per observation: Dim_Country and
    # Dim_VoltageLevel count elements and voltage levels, and an element appears under every
    # contingency. Restricting to the observed ids also applies the voltage floor.
    located = (
        locations.loc[locations.index.isin(set(observations["Element_Id"]))]
        if not observations.empty else None
    )
    sheets = {
        README_SHEET: build_readme(reference, model),
        KPI_DATA_SHEET: kpi_data,
        PERF_SHEET: build_perf_computation(
            business_day, timestamp, timestamp_id, stage_times, n_elements_evaluated,
            n_contingencies,
        ),
        RAW_SAMPLE_SHEET: raw_sample,
        "Dim_Date": build_dim_date(business_day),
        "Dim_Time": build_dim_time(timestamp, timestamp_id),
        "Dim_Country": build_dim_country(located),
        "Dim_Case": build_dim_case(),
        "Dim_VoltageLevel": build_dim_voltage_level(located),
        DIM_KPI_SHEET: catalog,
        PARAMETERS_SHEET: build_parameters(reference, model, single_sided_elements),
    }
    filename = config.KPI_WORKBOOK_FILENAME_TEMPLATE.format(reference=reference, model=model)
    return write_workbook(sheets, catalog, filename)


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def _element_types(elements_by_kind) -> pd.Series:
    """element_id -> the display label Raw_Sample's Element_Type carries.

    Taken from which frame the element came in, since the element frames themselves carry no
    kind, and deduplicated by id: an element listed in both the HV and the RCC set of its kind
    is one element of one kind.
    """
    types = pd.concat([
        pd.Series(loadflow.KIND_LABELS[kind].capitalize(), index=frame.index)
        for kind, frame in elements_by_kind.items()
    ])
    return types[~types.index.duplicated()]


def _stage_times(reference: str, model: str, runs, sa_times) -> list:
    """Perf_Computation's rows for one pair: both models' load flow and security analysis.

    A stage that did not run contributes None, which build_perf_computation drops rather than
    writing as a zero - with SA off there are simply no Security Analysis rows.
    """
    return [
        ("Load Flow", "Load Flow", reference, runs[reference].lf_time),
        ("Load Flow", "Load Flow", model, runs[model].lf_time),
        ("Security Analysis", "Security Analysis", reference, sa_times.get(reference)),
        ("Security Analysis", "Security Analysis", model, sa_times.get(model)),
    ]


def write_workbooks(network, runs, reference: str, compared_models, limits, elements_by_kind,
                     base_case_comparisons, base_case_index, n_elements_evaluated,
                     n_contingencies, sa_comparisons=None, sa_sides=None, sa_times=None,
                     single_sided_elements=None) -> list:
    """Write one workbook per compared model and return the paths, in `compared_models` order.

    The whole stage behind `config.KPI_WORKBOOK`: everything the workbooks need is derived
    here, so the caller hands over the run's results and nothing else. `elements_by_kind` maps
    each `loadflow.KIND_LABELS` kind to that kind's elements, HV and RCC together, which is
    also how `runs[*].flows` is keyed.

    `sa_comparisons`, `sa_sides` and `sa_times` are absent when the security analysis did not
    run, and each workbook then reports the base case alone.
    """
    sa_comparisons = sa_comparisons or {}
    sa_times = sa_times or {}

    elements = pd.concat(elements_by_kind.values())

    # Every element is attributed to a single side for the Country and VoltageLevel grouping
    # keys - the side the base case already reports it on. The SA dataset keeps both sides as
    # separate observations, which is correct and unchanged; this only decides which country
    # and voltage level those observations are filed under, so a tie line does not land in two
    # countries depending on the row. The reference model's flows cover every element of each
    # kind, RCC included, so lightly loaded elements still resolve a side.
    binding_side = loadflow.binding_sides(
        pd.concat([runs[reference].flows[kind] for kind in elements_by_kind]),
        loadflow.permanent_current_limits(limits),
    )
    locations = network_io.element_locations(network, elements, binding_side)

    base_case_ids = pd.Series(base_case_index, index=base_case_index)
    element_names = elements["name"].loc[~elements.index.duplicated()]
    element_types = _element_types(elements_by_kind)

    paths = []
    for model in compared_models:
        paths.append(build_and_write(
            network,
            locations,
            _stage_times(reference, model, runs, sa_times),
            n_elements_evaluated=n_elements_evaluated,
            n_contingencies=n_contingencies,
            reference=reference,
            model=model,
            base_case_comparison=base_case_comparisons[model],
            base_case_ids=base_case_ids,
            sa_comparison=sa_comparisons.get(model),
            sa_element_ids=None if sa_sides is None else sa_sides["subject_id"],
            sa_contingency_ids=None if sa_sides is None else sa_sides["contingency_id"],
            element_names=element_names,
            element_types=element_types,
            single_sided_elements=single_sided_elements,
        ))
    return paths
