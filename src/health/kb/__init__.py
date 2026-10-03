"""Metabolic-map knowledge base (KB): the packaged data and its loader.

The KB is five JSON files shipped byte-identical from the curated reference
(reference/metabolic-map-v1/kb): `entities.json`, `relations.json`,
`evidence.json`, `VERIFICATION_LOG.json` and the contract `schema.json`.
`load_kb()` reads them into one dict that `health.kb.check.check_kb` and
`health.kb.report.summary` take:

    {
      "entities":     <entities.json document>,
      "relations":    <relations.json document>,
      "evidence":     <evidence.json document>,
      "verification": <VERIFICATION_LOG.json document>,
      "schema":       <schema.json document>,
      "params":       <engine params.json document, or None if none was found>,
      "paths":        {key: where each document was read from},
    }

Documents are kept whole (not just their record arrays) because the contract
covers the top-level keys too (`additionalProperties: false`).

engine/params.json belongs to the engine, not the KB, but the KB contract reaches
into it: a `quantity.engineParam` must mirror a params.json row exactly. It is
looked up in this order: an explicit `params_path`; `params.json` inside `root`
(so a self-contained KB copy can carry its own); the packaged
`health/engine/params.json`; and, in a source checkout only, the curated
reference copy. `paths["params"]` records which one was used.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

DATA_FILES: dict[str, str] = {
    "entities": "entities.json",
    "relations": "relations.json",
    "evidence": "evidence.json",
    "verification": "VERIFICATION_LOG.json",
    "schema": "schema.json",
}
"""KB key -> file name, for every file `load_kb` requires."""

RECORD_KEYS: dict[str, str] = {
    "entities": "entities",
    "relations": "relations",
    "evidence": "evidence",
    "verification": "records",
}
"""KB key -> the array of records inside that document."""

PARAMS_FILE = "params.json"

_PACKAGE_DIR = Path(__file__).resolve().parent
_REFERENCE_PARAMS = (_PACKAGE_DIR.parents[2] / "reference" / "metabolic-map-v1"
                     / "engine" / "params.json")


class KBLoadError(ValueError):
    """A KB file is missing or is not valid JSON. Nothing can be checked."""


def data_dir() -> Any:
    """The packaged data directory (a Traversable; a Path for normal installs)."""
    try:
        return resources.files(__name__).joinpath("data")
    except (ModuleNotFoundError, TypeError, AttributeError):
        return _PACKAGE_DIR / "data"


def _read_json(source: Any, label: str) -> Any:
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KBLoadError(f"{label}: file not found") from exc
    except OSError as exc:
        raise KBLoadError(f"{label}: cannot read ({exc})") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise KBLoadError(f"{label}: invalid JSON ({exc})") from exc


def _packaged_params() -> Any | None:
    try:
        candidate = resources.files("health").joinpath("engine").joinpath(PARAMS_FILE)
    except (ModuleNotFoundError, TypeError, AttributeError):
        candidate = _PACKAGE_DIR.parent / "engine" / PARAMS_FILE
    return candidate if candidate.is_file() else None


def _params_source(root: Path | None, params_path: str | Path | None) -> Any | None:
    if params_path is not None:
        return Path(params_path)
    if root is not None and (root / PARAMS_FILE).is_file():
        return root / PARAMS_FILE
    packaged = _packaged_params()
    if packaged is not None:
        return packaged
    if _REFERENCE_PARAMS.is_file():
        return _REFERENCE_PARAMS
    return None


def load_kb(root: str | Path | None = None, *,
            params_path: str | Path | None = None) -> dict[str, Any]:
    """Load the KB documents (and the engine params they mirror) into one dict.

    `root` is a directory holding the five KB files; None means the packaged
    data. Raises KBLoadError when a required file is missing or not JSON. A
    missing params.json is NOT an error here -- `params` is None and the checker
    reports it if any quantity mirrors an engine parameter.
    """
    base: Any = data_dir() if root is None else Path(root)
    root_path = None if root is None else Path(root)
    kb: dict[str, Any] = {"paths": {}}
    for key, name in DATA_FILES.items():
        source = base.joinpath(name)
        kb[key] = _read_json(source, name)
        kb["paths"][key] = str(source)
    params_src = _params_source(root_path, params_path)
    if params_src is None:
        kb["params"] = None
        kb["paths"]["params"] = None
    else:
        kb["params"] = _read_json(params_src, str(params_src))
        kb["paths"]["params"] = str(params_src)
    return kb


def records(kb: dict[str, Any], key: str) -> list[Any]:
    """The record array of one KB document, or [] if the document is malformed.

    Never raises: the checker reports malformed documents; everything else
    (summaries, status) should degrade to "nothing there" rather than crash.
    """
    doc = kb.get(key)
    if not isinstance(doc, dict):
        return []
    items = doc.get(RECORD_KEYS[key])
    return items if isinstance(items, list) else []


__all__ = ["DATA_FILES", "KBLoadError", "RECORD_KEYS", "data_dir", "load_kb", "records"]
