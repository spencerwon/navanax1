"""Minimal .env loader.

Exists because BUG-20260909-001: every entry point read `os.environ` only, while
every error message and every doc told the operator to put the key in `.env`.
The instruction and the implementation disagreed, and the error message actively
sent the user back to the thing that was already correct.

No third-party dependency: `python-dotenv` would do this, but this is 40 lines
and one fewer install for a first-run experience that has to work.

Handles the failure modes that actually bite on macOS:
  * TextEdit saving Rich Text (RTF) instead of plain text — the single most
    common way `open -e .env` goes wrong, and it is invisible in the editor
  * UTF-8 BOM, CRLF line endings
  * quoted values, `export ` prefixes, inline comments, stray whitespace
"""

from __future__ import annotations

import os
from pathlib import Path


class DotenvError(RuntimeError):
    """The .env file exists but cannot be read as key=value pairs."""


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # Strip matched surrounding quotes, then any trailing comment on an
        # unquoted value. Quoted values keep their '#' — it may be in the key.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        if key:
            out[key] = val
    return out


def load(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    """Read `path` into os.environ. Returns what was loaded.

    A real environment variable wins over the file unless `override=True`, so a
    one-off `OPENSEA_API_KEY=... python ...` still works for testing.
    """
    p = Path(path)
    if not p.exists():
        return {}

    raw = p.read_bytes()
    if raw[:5] == b"{\\rtf":
        raise DotenvError(
            f"{p} was saved as Rich Text (RTF), not plain text.\n"
            f"  TextEdit does this silently when it is in rich-text mode, and the\n"
            f"  file looks correct on screen.\n"
            f"  Fix: open {p} in TextEdit, then Format -> Make Plain Text, and save.\n"
            f"  Or from Terminal:  nano {p}"
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DotenvError(f"{p} is not valid UTF-8 text: {exc}") from exc

    values = parse(text)
    for k, v in values.items():
        if override or k not in os.environ:
            os.environ[k] = v
    return values


def require(name: str, *, path: str | Path = ".env") -> str:
    """Get a required setting, loading .env first. Raises with real guidance."""
    load(path)
    val = os.environ.get(name, "").strip()
    if not val:
        p = Path(path)
        if not p.exists():
            hint = (f"No {p} file found.\n"
                    f"  Create one:  cp .env.example .env\n"
                    f"  Then open it and put your key after {name}=")
        else:
            hint = (f"{p} exists but has no value for {name}.\n"
                    f"  Open it and check the line reads:  {name}=your_key_here\n"
                    f"  (no spaces around the =, no quotes needed)")
        raise DotenvError(f"{name} is not set.\n  {hint}")
    return val
