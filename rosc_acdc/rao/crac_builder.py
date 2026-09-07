"""Build the OpenRAO CRAC (contingencies, CNECs, remedial actions) from the network and inputs-RA.xlsx.

inputs-RA.xlsx is only ever read, never written.
"""

import logging
import random

import pandas as pd

from rosc_acdc import config
from rosc_acdc.contingencies import normalize

logger = logging.getLogger(__name__)


def close_gen_switch(network, gen_id):
    """Best-effort topological search to close whatever switch is isolating a generator."""
    import networkx as nx

    generators = network.get_generators()
    gen = generators.loc[gen_id]
    if gen["connected"]:
        return

    logger.info("Trying to identify open switches for generator %s", gen_id)
    try:
        vl_id = gen["voltage_level_id"]
        logger.info("Voltage level = %s", vl_id)
        topology = network.get_node_breaker_topology(vl_id)
        nodes = topology.nodes
        switches = topology.switches
        internal_connections = topology.internal_connections
        gen_nodes = nodes[nodes["connectable_id"] == gen_id]

        if gen_nodes.empty:
            logger.warning("Could not find node connected to generator %s", gen_id)
            return

        gen_node = gen_nodes.index[0]
        logger.info("Generator node = %s", gen_node)
        graph = nx.Graph()
        for node_id in nodes.index:
            graph.add_node(node_id)
        for _, row in internal_connections.iterrows():
            graph.add_edge(row["node1"], row["node2"], type="internal_connection")
        for switch_id, row in switches.iterrows():
            if not row["open"]:
                graph.add_edge(row["node1"], row["node2"], type="switch", switch_id=switch_id)

        component = nx.node_connected_component(graph, gen_node)
        logger.info("Generator connected component: %s", component)

        candidate_open_switches = []
        for switch_id, row in switches.iterrows():
            if row["open"]:
                node1, node2 = row["node1"], row["node2"]
                if (node1 in component and node2 not in component) or (
                    node2 in component and node1 not in component
                ):
                    candidate_open_switches.append(switch_id)

        if candidate_open_switches:
            logger.info("Open switch(es) found upstream of generator:")
            for switch_id in candidate_open_switches:
                sw = switches.loc[switch_id]
                logger.info(
                    "  %s | %s | node1=%s | node2=%s",
                    switch_id, sw["kind"], sw["node1"], sw["node2"],
                )
                network.update_switches(id=switch_id, open=False)
                network.update_generators(id=gen_id, connected=True, p=0, q=0)
        else:
            logger.info("No open switch directly separating the generator from its connected topology.")
    except Exception as e:
        logger.warning("Switch investigation failed: %s", e)


def load_topo_actions(network, inputs_ra_path=None):
    topo_pd = pd.read_excel(inputs_ra_path or config.inputs_RA, sheet_name="Topo")
    topo_actions = []
    for k in range(len(topo_pd)):
        switches = network.get_switches()
        topo_sw = topo_pd.iloc[k, 1]
        switch_id = switches[switches["name"] == topo_sw].index[0]
        old_status = switches.loc[switch_id, "open"]
        topo_actions.append({"id": topo_sw, "switch_id": switch_id, "open": not old_status, "cost": 0.3})
    return topo_actions


def load_redispatch_actions(network, inputs_ra_path=None):
    """Read the RD sheet and ensure each referenced generator is connected, closing switches if needed."""
    gen_pd = pd.read_excel(inputs_ra_path or config.inputs_RA, sheet_name="RD")
    redispatch_actions = []
    for k in range(len(gen_pd)):
        gen_name = gen_pd.iloc[k, 1]
        generators = network.get_generators()
        gen_id = generators[generators["name"] == gen_name].index[0]
        pmin = generators.loc[gen_id]["min_p"]
        pmax = generators.loc[gen_id]["max_p"]
        redispatch_actions.append({
            "id": gen_name, "generator_id": gen_id, "min_mw": pmin, "max_mw": pmax,
            "activation_cost": 20, "cost_up": 100, "cost_down": 90,
        })

        gen = generators[generators["name"] == gen_name]
        flag_gen_close = "Normal Close"
        if not gen["connected"].iloc[0]:
            network.update_generators(id=gen_id, connected=True, p=0, q=0)
            generators = network.get_generators()
            gen = generators[generators["name"] == gen_name]
            if not gen["connected"].iloc[0]:
                network.connect(gen_id, operate_disconnectors=True, operate_fictitious=True)
                generators = network.get_generators()
                gen = generators[generators["name"] == gen_name]
                flag_gen_close = "PowSyBl Network Connect Close"
                if not gen["connected"].iloc[0]:
                    logger.info("Using tree search algorithm for generator: %s", gen_name)
                    close_gen_switch(network, gen_id)
                    flag_gen_close = "Network Search Algorithm Close"
            logger.info("Unit closed %s through: %s", gen_name, flag_gen_close)

    return redispatch_actions


def load_pst_actions(network, inputs_ra_path=None):
    pst_pd = pd.read_excel(inputs_ra_path or config.inputs_RA, sheet_name="PST")
    transformers2 = network.get_2_windings_transformers()
    pst_actions = []
    for k in range(len(pst_pd)):
        pst_sens = pst_pd.iloc[k, 0]
        pst_id = transformers2[transformers2["name"] == pst_sens].index[0]
        pst_actions.append({"id": pst_sens, "pst_id": pst_id, "activation_cost": 0})
    return pst_actions


def _add_flow_cnecs_full(crac, cnec_lookup, line_ids, patl_all, contingency_ids, optimized, monitored, kind_label):
    for line_id in line_ids:
        try:
            patl_temp1 = patl_all.loc[line_id, "ONE"]
            patl_temp2 = patl_all.loc[line_id, "TWO"]
        except Exception:
            logger.warning("CANNOT FIND PATL FOR %s: %s", kind_label, line_id)
            continue
        crac["flowCnecs"].append({
            "id": f"{line_id}_preventive",
            "networkElementId": line_id,
            "instant": "preventive",
            "optimized": optimized,
            "monitored": monitored,
            "thresholds": [
                {"unit": "ampere", "side": 1, "min": -patl_temp1, "max": patl_temp1},
                {"unit": "ampere", "side": 2, "min": -patl_temp2, "max": patl_temp2},
            ],
        })
        cnec_lookup[(line_id, "preventive", None)] = f"{line_id}_preventive"
        if config.CON_FCNEC and not config.SA:
            for instant_psb in ["outage", "auto", "curative"]:
                for co_id in contingency_ids:
                    crac["flowCnecs"].append({
                        "id": f"{line_id}_{co_id}_{instant_psb}",
                        "networkElementId": line_id,
                        "instant": instant_psb,
                        "contingencyId": co_id,
                        "optimized": optimized,
                        "monitored": monitored,
                        "thresholds": [
                            {"unit": "ampere", "side": 1, "min": -patl_temp1, "max": patl_temp1},
                            {"unit": "ampere", "side": 2, "min": -patl_temp2, "max": patl_temp2},
                        ],
                    })
                    cnec_lookup[(line_id, instant_psb, co_id)] = f"{line_id}_{co_id}_{instant_psb}"


def _add_flow_cnecs_from_shortlist(crac, cnec_lookup, shortlist_con_mge, cc_label, patl_all, optimized, monitored):
    shortlist = shortlist_con_mge[shortlist_con_mge["CC"] == cc_label]
    mon_list = shortlist[["subject_id"]].drop_duplicates()
    con_list = shortlist[["contingency_id"]].drop_duplicates()
    logger.info(
        "No. of %s CON %d and No. of %s MGE %d; %s MON %s %s OPT %s",
        cc_label, len(con_list), cc_label, len(mon_list), cc_label, monitored, cc_label, optimized,
    )
    for instant_psb in ["outage", "auto", "curative"]:
        for i in range(len(mon_list)):
            for j in range(len(con_list)):
                line_id = mon_list.iloc[i]["subject_id"]
                co_id = con_list.iloc[j]["contingency_id"]
                if len(str(co_id)) < 1:
                    continue
                try:
                    patl_temp1 = patl_all.loc[line_id, "ONE"]
                    patl_temp2 = patl_all.loc[line_id, "TWO"]
                except Exception:
                    continue
                crac["flowCnecs"].append({
                    "id": f"{line_id}_{co_id}_{instant_psb}",
                    "networkElementId": line_id,
                    "instant": instant_psb,
                    "contingencyId": co_id,
                    "optimized": optimized,
                    "monitored": monitored,
                    "thresholds": [
                        {"unit": "ampere", "side": 1, "min": -patl_temp1, "max": patl_temp1},
                        {"unit": "ampere", "side": 2, "min": -patl_temp2, "max": patl_temp2},
                    ],
                })
                cnec_lookup[(line_id, instant_psb, co_id)] = f"{line_id}_{co_id}_{instant_psb}"


def build_crac(network, data, valid_ids, shortlist_con_mge, rcc_line_ids, ncc_line_ids,
                patl_all, topo_actions, redispatch_actions, pst_actions):
    """Build the CRAC dict, mirroring the original script's contingency/CNEC/RA construction."""
    contingency_ids = list(data.keys())

    crac = {
        "type": "CRAC",
        "version": "2.11",
        "id": "acdc-rao-crac",
        "name": "AC DC study RAO CRAC",
        "instants": [
            {"id": "preventive", "kind": "PREVENTIVE"},
            {"id": "outage", "kind": "OUTAGE"},
            {"id": "auto", "kind": "AUTO"},
            {"id": "curative", "kind": "CURATIVE"},
        ],
        "contingencies": [],
        "flowCnecs": [],
        "networkActions": [],
        "pstRangeActions": [],
        "injectionRangeActions": [],
    }

    if config.RANDOM_SEL:
        rand_con_list = list(data.items())
        if config.rand_seed:
            random.seed(config.rand_seed)
        rand_con_list = random.sample(rand_con_list, k=config.rNoCON)
    else:
        rand_con_list = None

    for scenario_name, case in data.items():
        if config.SA and scenario_name not in shortlist_con_mge["contingency_id"].values:
            continue
        if rand_con_list is not None and [scenario_name, case] not in rand_con_list:
            continue

        elements_ids = []
        for comp in case.get("InterruptedComponents", []) + case.get("OpenedSwitches", []):
            rdf_id = normalize(comp["RdfId"])
            if rdf_id in valid_ids:
                elements_ids.append(rdf_id)
        if not elements_ids:
            continue

        crac["contingencies"].append({"id": scenario_name, "networkElementsIds": elements_ids})

        closed_switch_ids = [
            normalize(sw["RdfId"]) for sw in case.get("ClosedSwitches", [])
            if normalize(sw["RdfId"]) in valid_ids
        ]
        if closed_switch_ids:
            crac["networkActions"].append({
                "id": f"{scenario_name}_auto_close",
                "switchActions": [
                    {"networkElementId": sw_id, "actionType": "close"} for sw_id in closed_switch_ids
                ],
                "onContingencyStateUsageRules": [{"instant": "auto", "contingencyId": scenario_name}],
            })

    built_contingency_ids = {c["id"] for c in crac["contingencies"]}
    contingency_ids = [c for c in contingency_ids if c in built_contingency_ids]

    cnec_lookup = {}

    _add_flow_cnecs_full(
        crac, cnec_lookup, ncc_line_ids.index, patl_all, contingency_ids,
        config.NCC_OPT, config.NCC_MON, "NCC",
    )
    if config.CON_FCNEC and config.SA:
        _add_flow_cnecs_from_shortlist(crac, cnec_lookup, shortlist_con_mge, "NCC", patl_all, config.NCC_OPT, config.NCC_MON)

    _add_flow_cnecs_full(
        crac, cnec_lookup, rcc_line_ids.index, patl_all, contingency_ids,
        config.RCC_OPT, config.RCC_MON, "RCC",
    )
    if config.CON_FCNEC and config.SA:
        _add_flow_cnecs_from_shortlist(crac, cnec_lookup, shortlist_con_mge, "RCC", patl_all, config.RCC_OPT, config.RCC_MON)

    logger.info("There are %d flowCNECs", len(crac["flowCnecs"]))

    for ta in topo_actions:
        crac["networkActions"].append({
            "id": ta["id"],
            "activationCost": ta["cost"],
            "switchActions": [{
                "networkElementId": ta["switch_id"],
                "actionType": "open" if ta["open"] else "close",
            }],
            "onInstantUsageRules": [{"instant": "preventive"}],
        })

    steps_df = network.get_phase_tap_changer_steps()
    for pa in pst_actions:
        pst_id = pa["pst_id"]
        steps = steps_df.loc[pst_id]
        tap_positions = [int(pos) for pos in steps.index]
        min_tap, max_tap = min(tap_positions), max(tap_positions)
        crac["pstRangeActions"].append({
            "id": pa["id"],
            "activationCost": pa["activation_cost"],
            "variationCosts": {"up": 0.1, "down": 0.1},
            "networkElementId": pst_id,
            "ranges": [{"rangeType": "absolute", "min": min_tap, "max": max_tap}],
            "onInstantUsageRules": [{"instant": "preventive"}],
        })

    for ra in redispatch_actions:
        crac["injectionRangeActions"].append({
            "id": ra["id"],
            "activationCost": ra["activation_cost"],
            "variationCosts": {"up": ra["cost_up"], "down": ra["cost_down"]},
            "networkElementIdsAndKeys": {ra["generator_id"]: 1},
            "ranges": [{"rangeType": "absolute", "min": ra["min_mw"], "max": ra["max_mw"]}],
            "onInstantUsageRules": [{"instant": "preventive"}],
        })

    return crac
