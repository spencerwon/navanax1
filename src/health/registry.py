"""The module registry: config/health/modules.yaml, read without a dependency.

HREQ-X-01 / docs/health/04 §6.5: every module has a flag, and `health.cli status` prints
every module with its state so a disabled module is visible rather than merely absent.
A flag nothing reads is a CFG defect; this module is the minimum consumer.

The registry is YAML, and the stdlib has no YAML parser. PyYAML is used when it is
importable (the installed environment, CI after `pip install`); before that -- the
stdlib-only floor, docs/03 §4.6 -- a deliberately narrow scanner reads the handful of
scalar fields `status` needs (`id`, `enabled`, `owner`, `phase`, `adr`, `depends_on`).
tests/health_kb_selftest.py holds the two readers to the same answer on the shipped file,
so the scanner cannot drift from the real parse unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "config" / "health" / "modules.yaml"

_ITEM = re.compile(r"^  - id:\s*(\S+)\s*$")
_FIELD = re.compile(r"^    (\w+):\s*(.*?)\s*$")
_LIST_ITEM = re.compile(r"^      - (\S+)\s*$")


def _scalar(text: str) -> Any:
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text == "[]":
        return []
    if text.startswith("[") and text.endswith("]"):
        return [t.strip() for t in text[1:-1].split(",") if t.strip()]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text.strip("\"'")


def _scan(text: str) -> list[dict[str, Any]]:
    """Read the registry's `- id:` blocks and their simple scalar / inline-list fields.

    Block scalars (`>`) and nested lists under a key are read as far as `status` needs:
    a `depends_on:` or `paths:` list written one item per line is collected; a `>` block
    is kept as its joined text.
    """
    modules: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    block_key: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ITEM.match(line)
        if m:
            cur = {"id": m.group(1)}
            modules.append(cur)
            block_key = None
            continue
        if cur is None:
            continue
        m = _FIELD.match(line)
        if m:
            key, val = m.group(1), m.group(2)
            if val == ">" or val == "|":
                cur[key] = ""
                block_key = key
            elif val == "":
                cur[key] = []
                block_key = key
            else:
                cur[key] = _scalar(val)
                block_key = None
            continue
        m = _LIST_ITEM.match(line)
        if m and block_key is not None and isinstance(cur.get(block_key), list):
            cur[block_key].append(m.group(1))
            continue
        if block_key is not None and isinstance(cur.get(block_key), str):
            cur[block_key] = (cur[block_key] + " " + line.strip()).strip()
    return modules


def load_modules(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Every module in the registry, as dicts with at least `id` and `enabled`.

    Uses PyYAML when importable, else the narrow scanner above. Raises FileNotFoundError
    when the registry is absent -- a missing registry is a CFG defect, never an empty
    list that reads as "no modules".
    """
    p = Path(path) if path is not None else DEFAULT_PATH
    text = p.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        return _scan(text)
    data = yaml.safe_load(text) or {}
    mods = data.get("modules") or []
    return [dict(m) for m in mods]


def format_modules(modules: list[dict[str, Any]]) -> str:
    """One line per state: `modules (n): id on · id on` and, separately, any disabled."""
    on = [m["id"] for m in modules if m.get("enabled") is True]
    off = [m["id"] for m in modules if m.get("enabled") is not True]
    lines = [f"modules ({len(modules)}): " + (" · ".join(f"{i} on" for i in on) or "(none enabled)")]
    if off:
        lines.append("modules DISABLED: " + " · ".join(off))
    return "\n".join(lines)
