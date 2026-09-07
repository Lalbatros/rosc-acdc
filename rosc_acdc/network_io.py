"""Network loading and HV/RCC element classification.
"""

import logging

import pypowsybl as pp

from rosc_acdc import config

logger = logging.getLogger(__name__)


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
