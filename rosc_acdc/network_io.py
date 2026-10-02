"""Network loading and HV/RCC element classification.
"""

import logging

import pandas as pd
import pypowsybl as pp

from rosc_acdc import config

logger = logging.getLogger(__name__)

# IIDM side label -> the suffix the voltage-level column carries for that side.
SIDE_VOLTAGE_LEVEL_COLUMN = {
    "ONE": "voltage_level1_id",
    "TWO": "voltage_level2_id",
    "THREE": "voltage_level3_id",
}


def load_network():
    """Load the network.

    xiidm_file == 1 -> load the pre-converted .xiidm file (read-only).
    xiidm_file == 0 -> import the CGMES zip and save a .xiidm copy next to it.
    """
    if config.xiidm_file == 1:
        network = pp.network.load(config.cgmes_zip.replace(".zip", ".xiidm"))
    else:
        network = pp.network.load(config.cgmes_zip, config.cgmes_import_parameters)
        network.save(config.cgmes_zip.replace(".zip", ".xiidm"))
    logger.info("Network loaded successfully!")
    return network


def get_network_items(network):
    """Classify lines / 2-winding / 3-winding transformers into HV and RCC subsets."""
    voltage_levels = network.get_voltage_levels()
    hv_voltage_levels = voltage_levels[
        voltage_levels["nominal_v"].isin(config.HV_VOLTAGE_LEVELS_KV)
    ].index
    hv_trafo_voltage_levels = voltage_levels[
        voltage_levels["nominal_v"].isin(config.HV_TRAFO_VOLTAGE_LEVELS_KV)
    ].index
    rcc_voltage_levels = voltage_levels[
        voltage_levels["nominal_v"].isin(config.RCC_VOLTAGE_LEVELS_KV)
    ].index

    lines = network.get_lines().fillna(0)
    hv_lines = lines[
        lines["voltage_level1_id"].isin(hv_voltage_levels) |
        lines["voltage_level2_id"].isin(hv_voltage_levels)
    ]
    rcc_lines = lines[
        (lines["voltage_level1_id"].isin(rcc_voltage_levels) &
         lines["voltage_level2_id"].isin(rcc_voltage_levels)) & ~lines.index.isin(hv_lines.index)
    ]

    transformers = network.get_2_windings_transformers().fillna(0)
    hv_transformers = transformers[
        transformers["voltage_level1_id"].isin(hv_trafo_voltage_levels) &
        transformers["voltage_level2_id"].isin(hv_trafo_voltage_levels)
    ]
    rcc_transformers = transformers[
        (transformers["voltage_level1_id"].isin(rcc_voltage_levels) &
         transformers["voltage_level2_id"].isin(rcc_voltage_levels))
        & ~transformers.index.isin(hv_transformers.index)
    ]

    transformers3 = network.get_3_windings_transformers().fillna(0)
    hv_transformers3 = transformers3[
        transformers3["voltage_level1_id"].isin(hv_voltage_levels) |
        transformers3["voltage_level2_id"].isin(hv_voltage_levels) |
        transformers3["voltage_level3_id"].isin(hv_voltage_levels)
    ]
    rcc_transformers3 = transformers3[
        (transformers3["voltage_level1_id"].isin(rcc_voltage_levels) &
         transformers3["voltage_level2_id"].isin(rcc_voltage_levels) &
         transformers3["voltage_level3_id"].isin(rcc_voltage_levels))
        & ~transformers3.index.isin(hv_transformers3.index)
    ]

    return hv_lines, rcc_lines, hv_transformers, rcc_transformers, hv_transformers3, rcc_transformers3


def element_locations(network, element_info, binding_side=None):
    """One (Country, VoltageLevel_kV) per element, for KPI_Data's grouping keys.

    Both are read from the element's **binding side** - the side the base case already
    reports it on - rather than from side 1 or from whichever side a given SA row happens to
    represent. A branch spans two voltage levels and, on a tie line, two countries, so the
    pair only describes something real if both come from the same terminal. Elements with no
    binding side (nothing rated) fall back to side ONE.

    Country comes from the substation behind the voltage level, the join pypowsybl uses
    itself: voltage_levels[substation_id] -> substations[country]. CGMES derives that
    country from the GeographicalRegion, which lives in the boundary file, so a model
    imported without boundaries resolves every country to null; those elements take
    config.KPI_FALLBACK_COUNTRY rather than dropping out of the workbook.
    """
    voltage_levels = network.get_voltage_levels()
    substations = network.get_substations()

    country_of_voltage_level = voltage_levels["substation_id"].map(substations["country"])
    nominal_v_of_voltage_level = voltage_levels["nominal_v"]

    elements = element_info[~element_info.index.duplicated()]
    if binding_side is None:
        binding_side = pd.Series(dtype=object)
    sides = binding_side.reindex(elements.index).fillna("ONE")

    voltage_level_id = pd.Series(index=elements.index, dtype=object)
    for side, column in SIDE_VOLTAGE_LEVEL_COLUMN.items():
        if column not in elements:
            continue
        on_this_side = sides == side
        voltage_level_id[on_this_side] = elements.loc[on_this_side, column]

    country = voltage_level_id.map(country_of_voltage_level)
    # An empty string is what pypowsybl returns for a substation with no country, so it is
    # as unresolved as a missing value and has to be treated the same way.
    country = country.replace("", None)
    unresolved = int(country.isna().sum())
    if unresolved:
        logger.warning(
            "No country on %d of %d elements; reporting them as %s",
            unresolved, len(country), config.KPI_FALLBACK_COUNTRY,
        )

    return pd.DataFrame({
        "Country": country.fillna(config.KPI_FALLBACK_COUNTRY),
        "VoltageLevel_kV": voltage_level_id.map(nominal_v_of_voltage_level),
    })
