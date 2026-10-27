"""Shared filesystem locations for generated run artifacts."""

import os

from rosc_acdc import config


def output_path(filename: str) -> str:
    """Return a path under the configured output folder, creating the directory if needed."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    return os.path.join(config.OUTPUT_DIR, filename)
