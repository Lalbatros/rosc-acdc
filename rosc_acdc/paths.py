"""Shared filesystem locations for generated run artifacts."""

import os

OUTPUT_DIR = "output"


def output_path(filename: str) -> str:
    """Return a path under OUTPUT_DIR for a generated artifact, creating the directory if needed."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.join(OUTPUT_DIR, filename)
