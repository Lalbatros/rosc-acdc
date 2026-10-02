"""Run configuration (constants and paths) for the AC/DC load flow comparison study.

This file is a template: every value below is a generic default of the right type.
Real values for your environment go in input/config_local.py (git-ignored, overrides
the defaults below via the import at the bottom of this file). See
input/config_local.py.example for the list of keys and how to fill them in.
"""

from rosc_acdc.models import ModelSpec

# ---------------------------------------------------------------------------
# General run flags
# ---------------------------------------------------------------------------
Sens = 0                # Perform Sensitivity Analysis
SA = 0                   # Perform Security Analysis
cgmes_zip = ""
xiidm_file = 0            # 0 - loads the cgmes_zip file, saves xiidm_file with same name and location, 1-loads xiidm file with same name and location
CON_OPT = 0               # 0-Use only NCC con from json_file, 1- Use both NCC and RCC con, 2- Use only RCC con from json_file_rcc

# CONTINGENCY FILE (To Be Modified to CO XML)
json_file = ""
json_file_rcc = ""

# ---------------------------------
# --------RAO PART-----------------
RAO_RUN = 0    # Perform OpenRAO
CRAC_BUFF = 0  # 1-CRAC from buffer, dont write it as json (so no read/write time)
CON_FCNEC = 1  # Consider contingencies in flowCNEC definition, 0 means only base case flows are considered

# Randomly select MGE and CON to test NCC, RCC, NCC+RCC performance
RANDOM_SEL = 0   # SELECT RANDOMLY - 1
rNoCON = 800     # NO of random con
rNoRCC = 1500    # NO of random mon for RCC
rNoNCC = 1500    # NO of random opt for NCC
rand_seed = 0    # 0 for no seed
NCC_MON = False  # Should NCC Circuits be monitored?
NCC_OPT = True   # Should NCC Circuits be optimized?
RCC_MON = True   # Should RCC Circuits be monitored?
RCC_OPT = False  # Should RCC Circuits be optimized?
mod_crac = 0     # modified crac to change CBCO applied only to 0 injectionRA item, test only, keep this 0
mod_crac_cnec_id = ""  # cnecId used by the mod_crac debug patch, only used if mod_crac is truthy
save_result = 1  # save rao result as json file
SA_Thres = 80    # % threshold for using CBCO from AC - SA
input_crac = ""
# input_crac is only used if CRAC_BUFF=2
inputs_RA = ""  # This will have all the Remedial Action information, only used in CRAC_BUFF!=2
logging_PSB = 0  # Enable debug logging infor for pypowsybl

ignore_mge_list = []

# CGMES import parameters (used only when xiidm_file == 0)
cgmes_import_parameters = {
    "iidm.import.cgmes.create-busbar-section-for-every-connectivity-node": "true",
    "iidm.import.cgmes.cgm-with-subnetworks": "false",
    "iidm.import.cgmes.source-for-iidm-id": "mRID",
}

# HV / RCC voltage-level classification thresholds (kV), used by network_io.get_network_items
HV_VOLTAGE_LEVELS_KV = [220.0, 380.0, 150.0]
HV_TRAFO_VOLTAGE_LEVELS_KV = [220.0, 380.0, 150.0, 110.0]
RCC_VOLTAGE_LEVELS_KV = [150.0, 110.0, 70.0]

# Loading threshold below which a branch is excluded from the base-case comparison
BASE_CASE_ACTIVE_THRESHOLD_PCT = 50

# Loading threshold below which a branch is excluded from the SA comparison
SA_ACTIVE_THRESHOLD_PCT = 50

# ---------------------------------------------------------------------------
# KPI workbook
#
# One workbook per reference/model pair, written to OUTPUT_DIR by rosc_acdc.kpi_workbook.
# The parameters below are read at run time and written back out to the workbook's own
# `Parameters` tab after the run: that tab is a report of what the run used, never an input.
#
# Naming note: three unrelated loading thresholds already live in this file -
# BASE_CASE_ACTIVE_THRESHOLD_PCT and SA_ACTIVE_THRESHOLD_PCT (population filters, 50) and
# SA_Thres (the RAO CNEC shortlist cutoff, 80). None of them is the near-limit KPI
# threshold. The KPI_ prefix keeps the two families apart.
# ---------------------------------------------------------------------------
KPI_WORKBOOK = 0  # Write the KPI workbook(s)

# An element is in violation above this loading. Shared with the KPI log tables
# (kpis.VIOLATION_THRESHOLD_PCT defaults from it), so the workbook and the logs agree.
KPI_VIOLATION_THRESHOLD_PCT = 100

# The near-limit KPIs look only at elements whose *reference* loading reaches this value
# (>=). Distinct from the violation threshold above: this one selects a population, that
# one decides pass/fail. Not to be confused with SA_Thres.
KPI_NEAR_LIMIT_THRESHOLD_PCT = 90

# N for the Top-N critical element overlap KPI (K16) and the TopN_N column.
KPI_TOP_N = 5

# Only elements whose binding side has at least this nominal voltage reach the workbook.
KPI_MIN_VOLTAGE_LEVEL_KV = 100.0

# Raw_Sample is a review sheet, not a full KPI table: it is capped per scenario to keep the
# workbook a manageable size. Every KPI is still computed on the complete population.
KPI_MAX_RECORDS_PER_SCENARIO = 100

# Display and labelling only; every flow figure in the workbook is a current in A.
KPI_FLOW_UNIT = "A"

# Country code reported for an element whose substation carries no country. CGMES derives
# the country from the GeographicalRegion, which lives in the boundary (EQBD) file, so a
# model imported without one resolves every country to null.
KPI_FALLBACK_COUNTRY = "UNKNOWN"

# Reported in `Parameters` only. This build runs a single snapshot: one business day, one
# timestamp. They would size the BusinessDay / Timestamp dimensions of a multi-run build.
KPI_BUSINESS_DAYS_SIMULATED = 1
KPI_TIMESTAMPS_PER_BUSINESS_DAY = 1

# Reported in `Parameters` only. Perf_Computation's N_Contingencies reports the number of
# contingencies the security analysis actually ran, not this value.
KPI_CONTINGENCIES_PER_COUNTRY_VOLTAGE_LEVEL = 1

# One workbook per pair, overwritten on every run: no versioning, no timestamp in the name.
# {reference} and {model} are the MODELS names, so the AC/DC pair renders the agreed
# Core_AC_DC_KPI.xlsx and any further model gets its own file beside it.
KPI_WORKBOOK_FILENAME_TEMPLATE = "Core_{reference}_{model}_KPI.xlsx"

# Output folder for every generated artifact (plots, SA export, RAO CRACs, KPI workbooks),
# relative to where the tool runs from.
OUTPUT_DIR = "output"

# ---------------------------------------------------------------------------
# Models being compared
# ---------------------------------------------------------------------------
# Each ModelSpec is one way of solving the network. The load flow, KPI, security analysis
# and plotting stages all iterate over MODELS, so comparing a further variant (say DC with
# another provider option) is a change here rather than a change in code.
#
#   name                    unique label, used as-is in column names and plot legends
#   dc                      True -> DC load flow and DC security analysis, False -> AC
#   parameters              pp.loadflow.Parameters kwargs, e.g. {"distributed_slack": False};
#                           "dc" is not accepted here, it comes from the field above
#   provider_parameters     OpenLoadFlow *load flow* options (str -> str), merged onto the
#                           provider defaults, e.g. {"maxNewtonRaphsonIterations": "30"}
#   sa_provider_parameters  OpenLoadFlow *security analysis* options (str -> str), e.g.
#                           {"threadCount": "1"} - a separate namespace from the load flow
#                           options above
#
# pypowsybl ignores unknown provider parameter keys silently, so models.validate_models
# rejects them (and a key put in the wrong bucket) at start-up, before the network loads.
#
# Note: {"dcFastMode": "true"} is the best-known option of that last namespace, but it cannot
# be used here - it fails with "MatrixException: Row index out of bound" whenever the security
# analysis carries the operator strategies contingencies.py registers (it is fine on plain
# contingencies). See input/config_local.py.example.
MODELS = [
    ModelSpec("AC", dc=False),
    ModelSpec("DC", dc=True),
]

# The model every other model is compared against; must be one of the MODELS names.
# All KPIs are asymmetric (false negatives, missed overload volume, margin error), so the
# comparison is reference-vs-others rather than all pairs.
REFERENCE_MODEL = "AC"

# ---------------------------------------------------------------------------
# Local overrides (git-ignored, real values for this environment)
# ---------------------------------------------------------------------------
try:
    from input.config_local import *
except ImportError:
    pass
