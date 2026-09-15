"""Loading contingency scenarios (NCC/RCC json files) and wiring them into a security analysis.
"""

import logging

from rosc_acdc import config

logger = logging.getLogger(__name__)


def normalize(rdf_id: str) -> str:
    return rdf_id.lstrip("_")


def load_contingency_data():
    """Load and, depending on config.CON_OPT, merge the NCC/RCC contingency scenarios."""
    import json

    if config.CON_OPT == 0:  # Use only NCC contingencies
        with open(config.json_file, "r") as file_temp:
            data = json.load(file_temp)
        return data

    if config.CON_OPT == 1:  # Merge NCC contingencies with RCC
        with open(config.json_file, "r") as file_temp:
            data = json.load(file_temp)
        with open(config.json_file_rcc, "r") as file_temp:
            data_rcc = json.load(file_temp)
        con_ids_ncc = list(data.keys())
        con_ids_rcc = list(data_rcc.keys())
        con_ids_all = con_ids_ncc + [
            contingency_id
            for contingency_id in con_ids_rcc
            if contingency_id not in data
        ]
        data_all = data.copy()
        data_all.update({
            contingency_id: contingency_data
            for contingency_id, contingency_data in data_rcc.items()
            if contingency_id not in data_all
        })
        logger.info("NCC contingencies: %d", len(con_ids_ncc))
        logger.info("RCC contingencies: %d", len(con_ids_rcc))
        logger.info("Common contingencies: %d", len(set(con_ids_ncc) & set(con_ids_rcc)))
        logger.info("RCC-only contingencies: %d", len(set(con_ids_rcc) - set(con_ids_ncc)))
        logger.info("Combined contingencies: %d", len(con_ids_all))
        return data_all
    elif config.CON_OPT==3:

        import pandas as pd
        import xml.etree.ElementTree as ET

        def parse_co(path):
            ns = {'cim':'http://iec.ch/TC57/CIM100#','nc':'http://entsoe.eu/ns/nc#'}
            RES = '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource'
            root = ET.parse(path).getroot()

            eq_rows = []
            for el in root.findall('cim:ContingencyEquipment', ns):
                eq_rows.append({
                    'mRID': el.findtext('cim:IdentifiedObject.mRID', namespaces=ns),
                    'name': el.findtext('cim:IdentifiedObject.name', namespaces=ns),
                    'Contingency': el.find('cim:ContingencyElement.Contingency', ns).get(RES).lstrip('#').lstrip('_'),
                    'Equipment': el.find('cim:ContingencyEquipment.Equipment', ns).get(RES).lstrip('#').lstrip('_'),
                })

            co_rows = []
            for el in root.findall('nc:OrdinaryContingency', ns):
                co_rows.append({
                    'mRID': el.findtext('cim:IdentifiedObject.mRID', namespaces=ns),
                    'name': el.findtext('cim:IdentifiedObject.name', namespaces=ns),
                })

            return pd.DataFrame(eq_rows), pd.DataFrame(co_rows)

        df_equipment, df_contingency = parse_co(config.CO_XML_loc)
        return [df_equipment, df_contingency ]

    # CON_OPT == 2: use only RCC contingencies
    with open(config.json_file_rcc, "r") as file_temp:
        data = json.load(file_temp)
    return data


def build_valid_ids(network):
    """Set of every network element id contingencies/actions may reference."""
    valid_ids = set()
    valid_ids.update(network.get_lines().index)
    valid_ids.update(network.get_2_windings_transformers().index)
    valid_ids.update(network.get_3_windings_transformers().index)
    valid_ids.update(network.get_switches().index)
    valid_ids.update(network.get_generators().index)
    valid_ids.update(network.get_loads().index)
    valid_ids.update(network.get_voltage_levels().index)
    valid_ids.update(network.get_busbar_sections().index)
    valid_ids.update(network.get_substations().index)
    valid_ids.update(network.get_shunt_compensators().index)
    valid_ids.update(network.get_static_var_compensators().index)
    valid_ids.update(network.get_dangling_lines().index)
    valid_ids.update(network.get_buses().index)
    valid_ids.update(network.get_hvdc_lines().index)
    valid_ids.update(network.get_vsc_converter_stations().index)
    valid_ids.update(network.get_lcc_converter_stations().index)
    valid_ids.update(network.get_batteries().index)
    valid_ids.update(network.get_ratio_tap_changers().index)
    valid_ids.update(network.get_phase_tap_changers().index)
    return valid_ids


def add_contingencies_and_actions(security_analysis, data, valid_ids):
    """Add multi-element contingencies, close-switch actions and operator strategies.

    Returns (missing, contingency_count) where `missing` is a list of
    (scenario_name, element_name, rdf_id) tuples for elements referenced by a
    scenario but absent from the network.
    """
    import pypowsybl as pp

    missing = []
    k_con = 0
    if config.CON_OPT!=3:
        for scenario_name, case in data.items():
            elements_ids = []
            for comp in case.get("InterruptedComponents", []) + case.get("OpenedSwitches", []):
                rdf_id = normalize(comp["RdfId"])
                if rdf_id in valid_ids:
                    elements_ids.append(rdf_id)
                elif rdf_id:
                    missing.append((scenario_name, comp["Name"], rdf_id))

            if elements_ids:
                k_con += 1
                security_analysis.add_multiple_elements_contingency(
                    elements_ids,
                    contingency_id=scenario_name,
                )

            actions = []
            for sw in case.get("ClosedSwitches", []):
                sw_rdfid = normalize(sw["RdfId"])
                if sw_rdfid in valid_ids:
                    action_id = f"{scenario_name}_close_{sw_rdfid}"
                    security_analysis.add_switch_action(action_id, switch_id=sw_rdfid, open=False)
                    actions.append(action_id)
                else:
                    missing.append((scenario_name, sw["Name"], sw_rdfid))

            if elements_ids and actions:
                security_analysis.add_operator_strategy(
                    f"{scenario_name}_strategy",
                    contingency_id=scenario_name,
                    action_ids=actions,
                    condition_type=pp.security.ConditionType.TRUE_CONDITION,
                )
    else: #USING CO LIST FROM XML
        [df_equipment, df_contingency ] = data
        df_contingency=df_contingency.set_index("mRID")
        list_con=df_equipment["Contingency"].drop_duplicates()
        for case_con in list_con:
            try:
                scenario_name=df_contingency.loc[case_con].values[0]
            except:
                scenario_name=case_con
                print("CANNOT FIND NAME for",case_con)

            list_elements=df_equipment[df_equipment["Contingency"]==case_con]
            elements_ids = []
            for temploc in range(len(list_elements)):
                if list_elements["mRID"].iloc[temploc] in valid_ids:
                    elements_ids.append(list_elements["mRID"].iloc[temploc])
                else:
                    missing.append(list_elements["mRID"].iloc[temploc])

            if elements_ids:
                k_con+=1
                security_analysis.add_multiple_elements_contingency(
                    elements_ids,
                    contingency_id=scenario_name
                )
    if missing:
        logger.warning("Skipped %d missing elements in contingencies", len(missing))

    return missing, k_con
