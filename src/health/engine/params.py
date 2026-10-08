"""Parameter table loader (port of `defaultParams` / `paramTable` in engine/model.js).

`params.json` beside this file is a byte-identical copy of
reference/metabolic-map-v1/engine/params.json, the canonical V1 table: every parameter
with its value, unit, range, evidence and grade. The self-test checks the two files are
identical, so the JavaScript reference and this port can never read different numbers.

Values are returned exactly as JSON parses them (an integer stays an `int`). That is
numerically identical to the JavaScript doubles: every V1 value is a small integer or a
decimal literal, and Python's int/float arithmetic on them rounds exactly as IEEE-754
double arithmetic does.
"""

from __future__ import annotations

import copy
import json
from importlib import resources
from pathlib import Path
from typing import Any

PARAMS_FILE = "params.json"


def _read_table_text() -> str:
    """The table as text: package data when installed, the file beside us otherwise."""
    try:
        return (resources.files(__package__ or "health.engine")
                .joinpath(PARAMS_FILE).read_text(encoding="utf-8"))
    except (ModuleNotFoundError, FileNotFoundError, NotADirectoryError, TypeError):
        return (Path(__file__).parent / PARAMS_FILE).read_text(encoding="utf-8")


# Loaded once; never handed out (callers get deep copies), so it cannot be mutated.
_PARAM_TABLE: dict[str, dict[str, Any]] = json.loads(_read_table_text())


def default_params() -> dict[str, Any]:
    """Deep copy of the parameter VALUES from params.json, in table order (spec §3 defaultParams)."""
    return {k: copy.deepcopy(v["value"]) for k, v in _PARAM_TABLE.items()}


def param_table() -> dict[str, dict[str, Any]]:
    """Full parameter table (values + units + ranges + evidence), deep-copied."""
    return copy.deepcopy(_PARAM_TABLE)
