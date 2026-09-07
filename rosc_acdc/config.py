"""Run configuration (constants and paths) for the AC/DC load flow comparison study.

This file is a template: every value below is a generic default of the right type.
Real values for your environment go in input/config_local.py (git-ignored, overrides
the defaults below via the import at the bottom of this file). See
input/config_local.py.example for the list of keys and how to fill them in.
"""

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
# Local overrides (git-ignored, real values for this environment)
# ---------------------------------------------------------------------------
try:
    from input.config_local import *  # noqa: F401,F403
except ImportError:
    pass
