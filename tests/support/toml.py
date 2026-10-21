"""Small TOML compatibility adapter for the Python 3.10 test floor."""

import importlib
from pathlib import Path

try:
    load = importlib.import_module("tomllib").load
except ModuleNotFoundError:
    load = importlib.import_module("tomli").load

REPO = Path(__file__).resolve().parents[2]


def load_project() -> dict:
    """Load the repository ``pyproject.toml`` as a plain mapping."""

    with (REPO / "pyproject.toml").open("rb") as file:
        return load(file)
