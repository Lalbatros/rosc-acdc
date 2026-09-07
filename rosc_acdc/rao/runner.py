"""Load a CRAC into pypowsybl, run OpenRAO, and process the results."""

import json
import logging
import time

import pandas as pd

from rosc_acdc import config
from rosc_acdc.paths import output_path

logger = logging.getLogger(__name__)


def _apply_mod_crac(crac_path=None):
    """Optional debug CRAC patch, only applied when config.mod_crac is truthy (default: off)."""
    with open(config.input_crac, "r") as file_temp:
        data_mod = json.load(file_temp)

    data_mod["injectionRangeActions"][0].pop("onInstantUsageRules", None)
    data_mod["injectionRangeActions"][0].pop("onContingencyStateUsageRules", None)
    data_mod["injectionRangeActions"][0].setdefault("onConstraintUsageRules", [])
    data_mod["injectionRangeActions"][0]["onConstraintUsageRules"] = [
        {
            "instant": "preventive",
            "cnecId": config.mod_crac_cnec_id,
        }
    ]
    mod_crac_path = output_path("rao_crac_mod.json")
    with open(mod_crac_path, "w") as f:
        json.dump(data_mod, f, indent=2)
    return mod_crac_path


def load_crac(network, crac: dict):
    """Turn the in-memory CRAC dict into a pypowsybl Crac, per config.CRAC_BUFF."""
    from pypowsybl.rao import Crac

    if config.mod_crac:
        mod_crac_path = _apply_mod_crac()

    if config.CRAC_BUFF == 1:
        import io
        crac_buffer = io.BytesIO(
            json.dumps(crac, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        return Crac.from_buffer_source(network, crac_buffer)

    if config.CRAC_BUFF == 2:
        if config.mod_crac:
            return Crac.from_file_source(network, mod_crac_path)
        return Crac.from_file_source(network, config.input_crac)

    crac_path = output_path("rao_crac.json")
    with open(crac_path, "w") as f:
        json.dump(crac, f, indent=2)
    return Crac.from_file_source(network, crac_path)


def run_rao(network, rao_crac, flow_cnec_count, ac_con_analysis=None):
    """Run OpenRAO against the given CRAC and log the outcome."""
    from pypowsybl.rao import create_rao
    import pypowsybl as pp

    if config.logging_PSB:
        import logging as _logging
        _logging.basicConfig()
        _logging.getLogger("powsybl").setLevel(_logging.DEBUG)

    rao_parameters = pp.rao.Parameters()
    rao_parameters = rao_parameters.from_file_source("rao_parameters.json")

    rao = create_rao()

    t0 = time.perf_counter()
    result = rao.run(crac=rao_crac, network=network, parameters=rao_parameters, rao_provider="FastRao")
    rao_time = time.perf_counter() - t0

    logger.info("There are %d flowCNECs", flow_cnec_count)
    if ac_con_analysis is not None:
        logger.info("AC contingency analysis time: %.3f s", ac_con_analysis)
    logger.info("RAO completed in: %.3f s", rao_time)

    return result, rao_time


def save_and_process_result(result):
    """Serialize the RAO result to json and flatten range/network action results to RA_result.csv."""
    result_path = output_path("rao_result.json")
    result.serialize(result_path)

    with open(result_path, "r") as file_temp:
        data = json.load(file_temp)

    range_actions = pd.DataFrame([])
    for temp in data["rangeActionResults"]:
        range_actions = pd.concat([range_actions, pd.DataFrame.from_dict(temp, orient="index")], axis=1)
    for temp in data["networkActionResults"]:
        range_actions = pd.concat([range_actions, pd.DataFrame.from_dict(temp, orient="index")], axis=1)
    for instant in ["initial", "preventive", "outage", "curative"]:
        temp_cost = data["costResults"][instant]
        range_actions = pd.concat(
            [range_actions, pd.DataFrame.from_dict(temp_cost, orient="index", columns=[instant])], axis=1,
        )

    range_actions.to_csv(output_path("RA_result.csv"))
    logger.info("RAO results written to %s and %s", result_path, output_path("RA_result.csv"))
