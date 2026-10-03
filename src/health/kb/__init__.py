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
      "params":       <engine params.json document, or None if none was usable>,
      "params_js":    <params.data.js parsed to a document, or None>,
      "paths":        {key: where each document was read from},
      "load_errors":  {"params" | "params_js": why that optional document is unusable},
    }

Documents are kept whole (not just their record arrays) because the contract
covers the top-level keys too (`additionalProperties: false`).

engine/params.json belongs to the engine, not the KB, but the KB contract reaches
into it: a `quantity.engineParam` must mirror a params.json row exactly. It is
looked up in this order: an explicit `params_path`; `params.json` inside `root`
(so a self-contained KB copy can carry its own); the packaged
`health/engine/params.json`; and, in a source checkout only, the curated
reference copy. `paths["params"]` records which one was used. A params.json that is
missing or not JSON does not stop the load: `params` is None, `load_errors["params"]`
says why, and the checker reports it (engine-params-unavailable) -- the KB itself can
still be checked and summarised.

params.data.js is the engine's JavaScript mirror of params.json (rule `params-mirror`,
docs/health/03 §6). It is read from beside the params.json in use, or, when the
packaged params.json is in use, from the curated reference checkout.

JSON here means RFC 8259 JSON, as JavaScript's JSON.parse reads it: the NaN, Infinity
and -Infinity tokens Python's json module accepts are refused (KBLoadError).
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
PARAMS_JS_FILE = "params.data.js"

_PACKAGE_DIR = Path(__file__).resolve().parent
_REFERENCE_PARAMS = (_PACKAGE_DIR.parents[2] / "reference" / "metabolic-map-v1"
                     / "engine" / "params.json")
_REFERENCE_PARAMS_JS = _REFERENCE_PARAMS.with_name(PARAMS_JS_FILE)


class KBLoadError(ValueError):
    """A KB file is missing or is not valid JSON. Nothing can be checked."""


def data_dir() -> Any:
    """The packaged data directory (a Traversable; a Path for normal installs)."""
    try:
        return resources.files(__name__).joinpath("data")
    except (ModuleNotFoundError, TypeError, AttributeError):
        return _PACKAGE_DIR / "data"


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-standard JSON token {token!r}: JSON has no NaN or Infinity "
                     "(JavaScript's JSON.parse refuses it)")


def parse_json(text: str, label: str) -> Any:
    """`text` as strict JSON; KBLoadError (naming `label`) when it is not."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:                    # JSONDecodeError is a ValueError too
        raise KBLoadError(f"{label}: invalid JSON ({exc})") from exc


def _read_text(source: Any, label: str) -> str:
    try:
        return source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KBLoadError(f"{label}: file not found") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise KBLoadError(f"{label}: cannot read ({exc})") from exc


def _read_json(source: Any, label: str) -> Any:
    return parse_json(_read_text(source, label), label)


def parse_params_js(text: str, label: str = PARAMS_JS_FILE) -> Any:
    """params.data.js -> the document it exports: header comments, `export default` and
    the trailing `;` stripped, the rest read as strict JSON. KBLoadError otherwise."""
    head, sep, body = text.partition("export default")
    if not sep:
        raise KBLoadError(f"{label}: no 'export default' found")
    for line in head.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("//"):
            raise KBLoadError(f"{label}: unexpected code before 'export default': "
                              f"{stripped[:60]!r}")
    body = body.strip()
    if body.endswith(";"):
        body = body[:-1]
    return parse_json(body, label)


def _packaged_params() -> Any | None:
    try:
        candidate = resources.files("health").joinpath("engine").joinpath(PARAMS_FILE)
    except (ModuleNotFoundError, TypeError, AttributeError):
        candidate = _PACKAGE_DIR.parent / "engine" / PARAMS_FILE
    return candidate if candidate.is_file() else None


def _params_source(root: Path | None, params_path: str | Path | None) -> tuple[Any | None, bool]:
    """(where params.json comes from, whether that is the packaged/reference copy)."""
    if params_path is not None:
        return Path(params_path), False
    if root is not None and (root / PARAMS_FILE).is_file():
        return root / PARAMS_FILE, False
    packaged = _packaged_params()
    if packaged is not None:
        return packaged, True
    if _REFERENCE_PARAMS.is_file():
        return _REFERENCE_PARAMS, True
    return None, False


def _params_js_source(params_src: Any | None, shipped: bool) -> Any | None:
    if params_src is None:
        return None
    beside = params_src.parent / PARAMS_JS_FILE if isinstance(params_src, Path) else None
    if beside is not None and beside.is_file():
        return beside
    if shipped and _REFERENCE_PARAMS_JS.is_file():
        return _REFERENCE_PARAMS_JS
    return None


def load_kb(root: str | Path | None = None, *,
            params_path: str | Path | None = None) -> dict[str, Any]:
    """Load the KB documents (and the engine params they mirror) into one dict.

    `root` is a directory holding the five KB files; None means the packaged
    data. Raises KBLoadError when one of those five is missing or not strict JSON.
    A missing or unparsable params.json (or params.data.js) is NOT an error here --
    the document is None, `load_errors` says why, and the checker reports it.
    """
    base: Any = data_dir() if root is None else Path(root)
    root_path = None if root is None else Path(root)
    kb: dict[str, Any] = {"paths": {}, "load_errors": {}}
    for key, name in DATA_FILES.items():
        source = base.joinpath(name)
        kb[key] = _read_json(source, name)
        kb["paths"][key] = str(source)
    params_src, shipped = _params_source(root_path, params_path)
    kb["params"] = kb["params_js"] = None
    kb["paths"]["params"] = None if params_src is None else str(params_src)
    kb["paths"]["params_js"] = None
    if params_src is None:
        kb["load_errors"]["params"] = "no params.json found"
        return kb
    try:
        kb["params"] = _read_json(params_src, str(params_src))
    except KBLoadError as exc:
        kb["load_errors"]["params"] = str(exc)
    js_src = _params_js_source(params_src, shipped)
    if js_src is not None:
        kb["paths"]["params_js"] = str(js_src)
        try:
            kb["params_js"] = parse_params_js(_read_text(js_src, str(js_src)), str(js_src))
        except KBLoadError as exc:
            kb["load_errors"]["params_js"] = str(exc)
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


__all__ = ["DATA_FILES", "KBLoadError", "PARAMS_JS_FILE", "RECORD_KEYS", "data_dir", "load_kb",
           "parse_json", "parse_params_js", "records"]
