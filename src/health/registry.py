"""The module registry: config/health/modules.yaml, read without a dependency.

HREQ-X-01 / docs/health/04 §6.5: every module has a flag, and `health.cli status` prints
every module with its state so a disabled module is visible rather than merely absent.
A flag nothing reads is a CFG defect; `is_enabled` / `module_state` are what a surface
asks before it shows a module's output (docs/health/07, "Flags and what reads them").

    python -m health.registry --check      # every rule below; exit 1 on a problem
    python -m health.registry --write-07   # regenerate the table and recipes of 07

`--check` (a gate in tools/gates.py and CI) holds the registry to what is on disk: every
entry has the HREQ-X-01 fields; ids are unique; every `depends_on` names a registered
module and the edges have no cycle; every `paths` and `tests` entry exists (a
`file::function` test names a function that is defined); every file under src/health/ and
every health test, fixture and tool belongs to exactly one module; every `health.*`
import in src/ follows a `depends_on` edge; every recipe sets `enabled: false`, removes
its entry and runs tools/gates.py; every recipe names each live file that still names the
module's paths after its dependants are gone (`LIVE_SCOPE`); and the generated block of
docs/health/07_MODULE_REGISTRY.md is the rendering of this file.

`HEALTH_MODULES_PATH`, when set, replaces config/health/modules.yaml as the registry that
is read. It exists for tests only (a test shows a surface with a module switched off
without editing the real file); nothing in the product sets it.

The registry is YAML, and the stdlib has no YAML parser. PyYAML is used when it is
importable (the installed environment, CI after `pip install`); before that -- the
stdlib-only floor, docs/03 §4.6 -- `parse_yaml` below reads the file. It is a small
parser for a SUBSET of YAML, written to give exactly PyYAML's `safe_load` answer on
everything inside the subset and to refuse (YamlSubsetError) everything outside it:

    inside   block mappings and block sequences at any consistent indentation
             (including a sequence written at its parent key's indentation, and a
             mapping that starts on a `- ` line); flow sequences and flow mappings
             `[a, "b, c"]`, `{id: a, enabled: false}`; plain, single- and double-quoted
             scalars, multi-line ones folded as YAML folds them; `|` and `>` block scalars
             with chomping (`-`, `+`) and indentation indicators; `#` comments outside
             quotes; YAML 1.1 resolution exactly as PyYAML does it (true/false/yes/no/
             on/off in lower, Capitalised or UPPER case; null and `~`; decimal, octal
             `0..`, hex, binary and base-60 ints; floats incl. .inf/.nan; dates);
             a `---` start marker and `...` end marker around ONE document.
    refused  anchors, aliases, tags, `?` complex keys, merge keys `<<`, directives,
             several documents -- and a TAB anywhere PyYAML refuses one (indentation,
             after `:` or `-`, around a plain scalar), so a file that PyYAML rejects is
             rejected here too rather than read differently.

tests/health_kb_selftest.py compares the two readers on the shipped registry, on
yaml.safe_dump's re-serialisation of it, on a copy with a comment after every flag, on a
4-space-indented copy, on a copy with `id` as the second key, and on a corpus of edge
cases, so the floor cannot drift from the real parse unnoticed.
"""

from __future__ import annotations

import ast
import datetime
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "config" / "health" / "modules.yaml"
ENV_PATH = "HEALTH_MODULES_PATH"


class YamlSubsetError(ValueError):
    """Not YAML, or YAML outside the subset `parse_yaml` reads (see the module docstring)."""


# ---------------------------------------------------------------------------
# Scalar resolution: PyYAML's YAML 1.1 implicit resolvers, in PyYAML's order.
# ---------------------------------------------------------------------------
_BOOL_RE = re.compile(r"""^(?:yes|Yes|YES|no|No|NO
                        |true|True|TRUE|false|False|FALSE
                        |on|On|ON|off|Off|OFF)$""", re.X)
_TRUE = frozenset({"yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"})
_FLOAT_RE = re.compile(r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
                        |\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
                        |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
                        |[-+]?\.(?:inf|Inf|INF)
                        |\.(?:nan|NaN|NAN))$""", re.X)
_INT_RE = re.compile(r"""^(?:[-+]?0b[0-1_]+
                        |[-+]?0[0-7_]+
                        |[-+]?(?:0|[1-9][0-9_]*)
                        |[-+]?0x[0-9a-fA-F_]+
                        |[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$""", re.X)
_NULLS = frozenset({"", "~", "null", "Null", "NULL"})
_TIMESTAMP_RE = re.compile(r"""^(?P<year>[0-9][0-9][0-9][0-9])
                -(?P<month>[0-9][0-9]?)
                -(?P<day>[0-9][0-9]?)
                (?:(?:[Tt]|[ \t]+)
                (?P<hour>[0-9][0-9]?)
                :(?P<minute>[0-9][0-9])
                :(?P<second>[0-9][0-9])
                (?:\.(?P<fraction>[0-9]*))?
                (?:[ \t]*(?P<tz>Z|(?P<tz_sign>[-+])(?P<tz_hour>[0-9][0-9]?)
                (?::(?P<tz_minute>[0-9][0-9]))?))?)?$""", re.X)
_TIMESTAMP_IMPLICIT = re.compile(r"""^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]
                |[0-9][0-9][0-9][0-9] -[0-9][0-9]? -[0-9][0-9]?
                 (?:[Tt]|[ \t]+)[0-9][0-9]?
                 :[0-9][0-9] :[0-9][0-9] (?:\.[0-9]*)?
                 (?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$""", re.X)


def _base60(parts: list[str], conv: Any) -> Any:
    value, base = conv(0), 1
    for digit in reversed([conv(p) for p in parts]):
        value += digit * base
        base *= 60
    return value


def _construct_int(text: str) -> int:
    v = text.replace("_", "")
    sign = -1 if v[0] == "-" else 1
    if v[0] in "+-":
        v = v[1:]
    if v == "0":
        return 0
    if v.startswith("0b"):
        return sign * int(v[2:], 2)
    if v.startswith("0x"):
        return sign * int(v[2:], 16)
    if v[0] == "0":
        return sign * int(v, 8)
    if ":" in v:
        return sign * _base60(v.split(":"), int)
    return sign * int(v)


def _construct_float(text: str) -> float:
    v = text.replace("_", "").lower()
    sign = -1 if v[0] == "-" else 1
    if v[0] in "+-":
        v = v[1:]
    if v == ".inf":
        return sign * math.inf
    if v == ".nan":
        return math.nan
    if ":" in v:
        return sign * _base60(v.split(":"), float)
    return sign * float(v)


def _construct_timestamp(text: str) -> datetime.date:
    m = _TIMESTAMP_RE.match(text)
    if m is None:                                   # pragma: no cover - implicit regex matched
        raise YamlSubsetError(f"unreadable timestamp {text!r}")
    g = m.groupdict()
    year, month, day = int(g["year"]), int(g["month"]), int(g["day"])
    if not g["hour"]:
        return datetime.date(year, month, day)
    fraction = 0
    if g["fraction"]:
        fraction = int(g["fraction"][:6].ljust(6, "0"))
    tzinfo: datetime.tzinfo | None = None
    if g["tz_sign"]:
        delta = datetime.timedelta(hours=int(g["tz_hour"]), minutes=int(g["tz_minute"] or 0))
        tzinfo = datetime.timezone(-delta if g["tz_sign"] == "-" else delta)
    elif g["tz"]:
        tzinfo = datetime.timezone.utc
    return datetime.datetime(year, month, day, int(g["hour"]), int(g["minute"]),
                             int(g["second"]), fraction, tzinfo=tzinfo)


def resolve_plain(text: str) -> Any:
    """A plain (unquoted) scalar as PyYAML's safe_load constructs it."""
    if text in _NULLS:
        return None
    first = text[0]
    if first in "yYnNtTfFoO" and _BOOL_RE.match(text):
        return text in _TRUE
    if first in "-+0123456789." and _FLOAT_RE.match(text):
        return _construct_float(text)
    if first in "-+0123456789" and _INT_RE.match(text):
        return _construct_int(text)
    if text == "<<":
        raise YamlSubsetError("merge keys (<<) are outside the subset")
    if first in "0123456789" and _TIMESTAMP_IMPLICIT.match(text):
        return _construct_timestamp(text)
    if text == "=":
        raise YamlSubsetError("'=' resolves to the YAML value tag, which safe_load refuses")
    return text


# ---------------------------------------------------------------------------
# Quoted scalars (PyYAML's scan_flow_scalar, on a character buffer)
# ---------------------------------------------------------------------------
_ESCAPES = {"0": "\0", "a": "\x07", "b": "\x08", "t": "\t", "\t": "\t", "n": "\n",
            "v": "\x0b", "f": "\x0c", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "\\": "\\",
            "/": "/", "N": "\x85", "_": "\xa0", "L": " ", "P": " "}
_ESCAPE_CODES = {"x": 2, "u": 4, "U": 8}
_HEX = frozenset("0123456789abcdefABCDEF")


def _flow_breaks(s: str, p: int) -> tuple[list[str], int]:
    breaks: list[str] = []
    while True:
        if s[p:p + 3] in ("---", "...") and (p + 3 >= len(s) or s[p + 3] in " \t\n"):
            raise YamlSubsetError("document separator inside a quoted scalar")
        while p < len(s) and s[p] in " \t":
            p += 1
        if p < len(s) and s[p] == "\n":
            breaks.append("\n")
            p += 1
        else:
            return breaks, p


def scan_quoted(s: str, p: int) -> tuple[str, int]:
    """The quoted scalar starting at s[p] (a quote); returns (value, index after it)."""
    quote = s[p]
    double = quote == '"'
    p += 1
    chunks: list[str] = []
    while True:
        while True:                                  # non-space run
            start = p
            while p < len(s) and s[p] not in "'\"\\ \t\n":
                p += 1
            chunks.append(s[start:p])
            if p >= len(s):
                raise YamlSubsetError("unterminated quoted scalar")
            ch = s[p]
            if not double and ch == "'" and s[p + 1:p + 2] == "'":
                chunks.append("'")
                p += 2
            elif (double and ch == "'") or (not double and ch in '"\\'):
                chunks.append(ch)
                p += 1
            elif double and ch == "\\":
                p += 1
                ch = s[p] if p < len(s) else ""
                if ch in _ESCAPES and ch:
                    chunks.append(_ESCAPES[ch])
                    p += 1
                elif ch in _ESCAPE_CODES:
                    n = _ESCAPE_CODES[ch]
                    digits = s[p + 1:p + 1 + n]
                    if len(digits) != n or not set(digits) <= _HEX:
                        raise YamlSubsetError(f"bad escape \\{ch}{digits}")
                    chunks.append(chr(int(digits, 16)))
                    p += 1 + n
                elif ch == "\n":
                    breaks, p = _flow_breaks(s, p + 1)
                    chunks.extend(breaks)
                else:
                    raise YamlSubsetError(f"unknown escape \\{ch}")
            else:
                break
        if s[p] == quote:
            return "".join(chunks), p + 1
        start = p                                    # whitespace run
        while p < len(s) and s[p] in " \t":
            p += 1
        if p >= len(s):
            raise YamlSubsetError("unterminated quoted scalar")
        if s[p] == "\n":
            breaks, p = _flow_breaks(s, p + 1)
            if not breaks:
                chunks.append(" ")
            chunks.extend(breaks)
        else:
            chunks.append(s[start:p])


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------
_LINES = re.compile(r"\r\n|\r|\n")
_FLOW_STOP = frozenset(",?[]{}")


def _comment_at(seg: str) -> int:
    """Index of a `#` that starts a comment in a quote-free segment, or -1."""
    for k, ch in enumerate(seg):
        if ch == "#" and (k == 0 or seg[k - 1] in " \t"):
            return k
    return -1


def _is_entry(body: str) -> bool:
    return body == "-" or body.startswith("- ") or body.startswith("-\t")


def _is_marker(line: str, marker: str) -> bool:
    return line == marker or line.startswith(marker + " ") or line.startswith(marker + "\t")


def _has_value_indicator(seg: str) -> bool:
    return ": " in seg or ":\t" in seg or seg.endswith(":")


class _Parser:
    def __init__(self, text: str) -> None:
        if text.startswith("﻿"):
            text = text[1:]
        lines = _LINES.split(text)
        self.final_newline = lines[-1] == ""
        if self.final_newline:
            lines.pop()
        self.lines = lines
        self.n = len(lines)
        self.i = 0

    def error(self, msg: str, line: int | None = None) -> YamlSubsetError:
        return YamlSubsetError(f"line {(self.i if line is None else line) + 1}: {msg}")

    @staticmethod
    def _tab(where: str) -> str:
        return f"tab character {where} (PyYAML refuses it; YAML indents with spaces)"

    # -- lines -----------------------------------------------------------------
    def indent(self, k: int) -> int:
        line = self.lines[k]
        return len(line) - len(line.lstrip(" "))

    def blank(self, k: int) -> bool:
        body = self.lines[k].lstrip(" ")
        if body.startswith("\t"):
            raise self.error(self._tab("in indentation or before a token"), k)
        return body == "" or body.startswith("#")

    def skip_blank(self) -> None:
        while self.i < self.n and self.blank(self.i):
            self.i += 1

    def at_marker(self) -> bool:
        line = self.lines[self.i]
        return _is_marker(line, "---") or _is_marker(line, "...")

    def buffer(self, k: int, col: int) -> str:
        return "\n".join([self.lines[k][col:], *self.lines[k + 1:]]) + "\n"

    def position(self, k: int, col: int, buf: str, p: int) -> tuple[int, int]:
        nl = buf.count("\n", 0, p)
        if nl == 0:
            return k, col + p
        return k + nl, p - (buf.rfind("\n", 0, p) + 1)

    def finish_line(self, k: int, c: int, what: str) -> None:
        """After a quoted or flow node ending at (k, c): only a comment may follow."""
        rest = self.lines[k][c:] if k < self.n else ""
        stripped = rest.lstrip(" ")
        if stripped.startswith("\t"):
            raise self.error(self._tab(f"after a {what}"), k)
        if stripped and not stripped.startswith("#"):
            if stripped.startswith(":"):
                raise self.error("mapping values are not allowed here", k)
            raise self.error(f"unexpected text after a {what}: {stripped[:30]!r}", k)
        self.i = k + 1

    # -- document ------------------------------------------------------------------
    def document(self) -> Any:
        self.skip_blank()
        if self.i < self.n and self.lines[self.i].startswith("%"):
            raise self.error("directives are outside the subset")
        if self.i < self.n and _is_marker(self.lines[self.i], "---"):
            rest = self.lines[self.i][3:].strip(" ")
            if rest and not rest.startswith("#"):
                raise self.error("content on the '---' line is outside the subset")
            self.i += 1
            self.skip_blank()
        value = None
        if self.i < self.n and not self.at_marker():
            value = self.block(self.indent(self.i), -1)
            self.skip_blank()
        if self.i < self.n and _is_marker(self.lines[self.i], "..."):
            self.i += 1
            self.skip_blank()
        if self.i < self.n:
            if _is_marker(self.lines[self.i], "---"):
                raise self.error("more than one document (safe_load reads exactly one)")
            raise self.error("unexpected content (bad indentation?)")
        return value

    def block(self, ind: int, parent: int) -> Any:
        body = self.lines[self.i][ind:]
        if _is_entry(body):
            return self.sequence(ind)
        if self.key_of(body) is not None:
            return self.mapping(ind)
        return self.inline(self.i, ind, parent)

    # -- keys ------------------------------------------------------------------------
    def key_of(self, body: str) -> tuple[Any, int] | None:
        """(key, offset just past its ':') when `body` starts a `key: value` entry."""
        if not body:
            return None
        first = body[0]
        if first in "\"'":
            try:
                key, end = scan_quoted(body + "\n", 0)
            except YamlSubsetError:
                return None
            if end > len(body):                      # the quoted scalar spans lines
                return None
            j = end
            while j < len(body) and body[j] == " ":
                j += 1
            if j < len(body) and body[j] == ":" and (j + 1 == len(body) or body[j + 1] in " \t"):
                return key, j + 1
            return None
        if first in "[{" or _is_entry(body):
            return None
        if first == "?" and (len(body) == 1 or body[1] in " \t"):
            raise self.error("complex keys ('? ') are outside the subset")
        j = 0
        while True:
            k = body.find(":", j)
            if k < 0:
                return None
            if _comment_at(body[:k]) >= 0:
                return None
            if k + 1 == len(body) or body[k + 1] in " \t":
                text = body[:k]
                if "\t" in text:
                    raise self.error(self._tab("in a mapping key"))
                text = text.rstrip(" ")
                if not text:
                    raise self.error("a mapping entry without a key")
                if text[0] in "&*!":
                    raise self.error("anchors, aliases and tags are outside the subset")
                if text[0] in "%@`|>,]}#":
                    raise self.error(f"a plain key cannot start with {text[0]!r}")
                return resolve_plain(text), k + 1
            j = k + 1

    # -- collections -----------------------------------------------------------------
    def mapping(self, ind: int) -> dict[Any, Any]:
        out: dict[Any, Any] = {}
        while True:
            self.skip_blank()
            if self.i >= self.n or self.at_marker():
                return out
            lind = self.indent(self.i)
            if lind < ind:
                return out
            if lind > ind:
                raise self.error("bad indentation of a mapping entry")
            body = self.lines[self.i][ind:]
            if _is_entry(body):
                raise self.error("a sequence entry where a mapping key was expected")
            kv = self.key_of(body)
            if kv is None:
                raise self.error("expected 'key: value'")
            key, off = kv
            try:
                hash(key)
            except TypeError as exc:
                raise self.error("unhashable mapping key") from exc
            out[key] = self.value_after(ind + off, ind, mapping_value=True)

    def sequence(self, ind: int) -> list[Any]:
        out: list[Any] = []
        while True:
            self.skip_blank()
            if self.i >= self.n or self.at_marker():
                return out
            lind = self.indent(self.i)
            if lind < ind:
                return out
            if lind > ind:
                raise self.error("bad indentation of a sequence entry")
            if not _is_entry(self.lines[self.i][ind:]):
                return out
            out.append(self.value_after(ind + 1, ind, mapping_value=False))

    def value_after(self, col: int, ind: int, *, mapping_value: bool) -> Any:
        """The value that follows `key:` or `-` ending at column `col` of the current line."""
        line = self.lines[self.i]
        rest = line[col:]
        if rest.startswith("\t"):
            raise self.error(self._tab("after ':' or '-'"))
        text = rest.lstrip(" ")
        if text.startswith("\t"):
            raise self.error(self._tab("before a value"))
        if text == "" or text.startswith("#"):
            self.i += 1
            self.skip_blank()
            if self.i >= self.n or self.at_marker():
                return None
            nind = self.indent(self.i)
            if nind > ind:
                return self.block(nind, ind)
            if mapping_value and nind == ind and _is_entry(self.lines[self.i][ind:]):
                return self.sequence(ind)             # a sequence at its key's indentation
            return None
        vcol = len(line) - len(text)
        if not mapping_value and (_is_entry(text) or self.key_of(text) is not None):
            self.lines[self.i] = " " * vcol + text    # `- key: v` / `- - x`: a nested block
            return self.block(vcol, ind)
        return self.inline(self.i, vcol, ind)

    # -- scalars and flow nodes ----------------------------------------------------------
    def inline(self, k: int, col: int, parent: int) -> Any:
        text = self.lines[k][col:]
        first = text[0]
        if first in "&*!":
            raise self.error("anchors, aliases and tags are outside the subset", k)
        if first in "|>":
            return self.block_scalar(text, parent)
        if first in "\"'":
            buf = self.buffer(k, col)
            value, p = scan_quoted(buf, 0)
            self.finish_line(*self.position(k, col, buf, p), "quoted scalar")
            return value
        if first in "[{":
            buf = self.buffer(k, col)
            value, p = _Flow(buf).node(0)
            self.finish_line(*self.position(k, col, buf, p), "flow collection")
            return value
        if first in "%@`,]}":
            raise self.error(f"a plain scalar cannot start with {first!r}", k)
        if first in "-?:" and (len(text) == 1 or text[1] in " \t"):
            raise self.error(f"'{first} ' is not allowed here", k)
        return self.plain(k, col, parent)

    def plain(self, k: int, col: int, parent: int) -> Any:
        seg = self.lines[k][col:]
        cut = _comment_at(seg)
        if cut >= 0:
            seg = seg[:cut]
        if "\t" in seg:
            raise self.error(self._tab("in or after a plain scalar"), k)
        if _has_value_indicator(seg.rstrip(" ")):
            raise self.error("mapping values are not allowed here", k)
        parts = [seg.rstrip(" ")]
        self.i = k + 1
        if cut >= 0:
            return resolve_plain(parts[0])
        pending = 0
        while self.i < self.n:
            line = self.lines[self.i]
            body = line.lstrip(" ")
            if body == "":
                pending += 1
                self.i += 1
                continue
            if body.startswith("\t"):
                raise self.error(self._tab("in indentation"))
            if len(line) - len(body) <= parent or body.startswith("#") or self.at_marker():
                break
            cut = _comment_at(body)
            seg = body[:cut] if cut >= 0 else body
            if "\t" in seg:
                raise self.error(self._tab("in or after a plain scalar"))
            seg = seg.rstrip(" ")
            if _has_value_indicator(seg):
                raise self.error("mapping values are not allowed here")
            parts.append("\n" * pending if pending else " ")
            parts.append(seg)
            pending = 0
            self.i += 1
            if cut >= 0:
                break
        return resolve_plain("".join(parts))

    def block_scalar(self, header: str, parent: int) -> str:
        style, j, chomp, inc = header[0], 1, None, None
        for _ in range(2):
            if j < len(header) and header[j] in "+-" and chomp is None:
                chomp = header[j] == "+"
                j += 1
            elif j < len(header) and header[j] in "123456789" and inc is None:
                inc = int(header[j])
                j += 1
        rest = header[j:]
        stripped = rest.lstrip(" ")
        if (rest and rest[0] != " ") or (stripped and not stripped.startswith("#")):
            raise self.error("expected a comment or a line break after a block scalar header")
        min_indent = max(parent + 1, 1)
        k = self.i + 1
        lines = self.lines
        if inc is None:
            max_ind, scan = 0, k
            while scan < self.n:
                ln = lines[scan]
                sp = len(ln) - len(ln.lstrip(" "))
                max_ind = max(max_ind, sp)
                if ln.strip(" ") != "":
                    break
                scan += 1
            ind = max(min_indent, max_ind)
        else:
            ind = min_indent + inc - 1

        def empty(q: int) -> bool:
            ln = lines[q]
            return len(ln) <= ind and ln.strip(" ") == ""

        def content(q: int) -> bool:
            ln = lines[q]
            return len(ln) > ind and ln[:ind].strip(" ") == "" and len(ln) - len(ln.lstrip(" ")) >= ind

        def line_break(q: int) -> str:
            return "\n" if q < self.n - 1 or self.final_newline else ""

        chunks: list[str] = []
        breaks: list[str] = []
        while k < self.n and empty(k):
            breaks.append(line_break(k))
            k += 1
        brk = ""
        while k < self.n and content(k):
            chunks.extend(breaks)
            text = lines[k][ind:]
            leading_non_space = text[:1] not in (" ", "\t")
            chunks.append(text)
            brk = line_break(k)
            k += 1
            breaks = []
            while k < self.n and empty(k):
                breaks.append(line_break(k))
                k += 1
            if k < self.n and content(k):
                nxt = lines[k][ind:]
                if style == ">" and brk == "\n" and leading_non_space and nxt[:1] not in (" ", "\t"):
                    if not breaks:
                        chunks.append(" ")
                else:
                    chunks.append(brk)
            else:
                break
        if chomp is not False:
            chunks.append(brk)
        if chomp is True:
            chunks.extend(breaks)
        self.i = k
        return "".join(chunks)


class _Flow:
    """Flow collections `[...]` / `{...}` (possibly spanning lines) on a character buffer."""

    def __init__(self, s: str) -> None:
        self.s = s

    def skip(self, p: int) -> int:
        s = self.s
        while p < len(s):
            ch = s[p]
            if ch in " \n":
                p += 1
            elif ch == "\t":
                raise YamlSubsetError("tab character inside a flow collection (PyYAML refuses it)")
            elif ch == "#" and (p == 0 or s[p - 1] in " \n"):
                while p < len(s) and s[p] != "\n":
                    p += 1
            else:
                return p
        raise YamlSubsetError("unterminated flow collection")

    def node(self, p: int) -> tuple[Any, int]:
        p = self.skip(p)
        ch = self.s[p]
        if ch == "[":
            return self.sequence(p + 1)
        if ch == "{":
            return self.mapping(p + 1)
        if ch in "\"'":
            return scan_quoted(self.s, p)
        if ch in "&*!":
            raise YamlSubsetError("anchors, aliases and tags are outside the subset")
        return self.plain(p)

    def plain(self, p: int) -> tuple[Any, int]:
        s = self.s
        if s[p] in ",?:[]{}#%@`|>" or (s[p] == "-" and s[p + 1:p + 2] in (" ", "\n", "")):
            raise YamlSubsetError(f"a flow plain scalar cannot start with {s[p]!r}")
        chunks: list[str] = []
        while True:
            start = p
            while p < len(s):
                ch = s[p]
                if ch in " \t\n" or ch in _FLOW_STOP:
                    break
                if ch == ":" and (p + 1 >= len(s) or s[p + 1] in " \t\n,[]{}"):
                    break
                p += 1
            chunks.append(s[start:p])
            q = p
            while q < len(s) and s[q] == " ":
                q += 1
            if q < len(s) and s[q] == "\t":
                raise YamlSubsetError("tab character inside a flow collection (PyYAML refuses it)")
            joiner = s[p:q]
            if q < len(s) and s[q] == "\n":           # a line break folds, as in block context
                breaks = 0
                while q < len(s) and s[q] in " \n":
                    if s[q] == "\n":
                        breaks += 1
                    q += 1
                if q < len(s) and s[q] == "\t":
                    raise YamlSubsetError("tab character inside a flow collection (PyYAML "
                                          "refuses it)")
                joiner = " " if breaks == 1 else "\n" * (breaks - 1)
            if (q > p and q < len(s) and s[q] not in _FLOW_STOP and s[q] != "#"
                    and not (s[q] == ":" and (q + 1 >= len(s) or s[q + 1] in " \t\n,[]{}"))):
                chunks.append(joiner)
                p = q
                continue
            return resolve_plain("".join(chunks)), p

    def sequence(self, p: int) -> tuple[list[Any], int]:
        out: list[Any] = []
        while True:
            p = self.skip(p)
            if self.s[p] == "]":
                return out, p + 1
            value, p = self.node(p)
            p = self.skip(p)
            if self.s[p] == ":":
                raise YamlSubsetError("a single-pair mapping inside a flow sequence is outside "
                                      "the subset")
            out.append(value)
            if self.s[p] == ",":
                p += 1
            elif self.s[p] != "]":
                raise YamlSubsetError(f"expected ',' or ']' in a flow sequence, got {self.s[p]!r}")

    def mapping(self, p: int) -> tuple[dict[Any, Any], int]:
        out: dict[Any, Any] = {}
        while True:
            p = self.skip(p)
            if self.s[p] == "}":
                return out, p + 1
            if self.s[p] in "[{":
                raise YamlSubsetError("a flow collection as a mapping key is outside the subset")
            start = p
            key, p = self.node(p)
            if "\n" in self.s[start:p]:
                raise YamlSubsetError("a flow mapping key must be on one line")
            q = p
            while q < len(self.s) and self.s[q] == " ":
                q += 1
            p = self.skip(p)
            if self.s[p] == ":" and p != q:
                raise YamlSubsetError("a flow mapping key must end on the line of its ':'")
            value = None
            if self.s[p] == ":":
                p = self.skip(p + 1)
                if self.s[p] not in ",}":
                    value, p = self.node(p)
                    p = self.skip(p)
            out[key] = value
            if self.s[p] == ",":
                p += 1
            elif self.s[p] != "}":
                raise YamlSubsetError(f"expected ',' or '}}' in a flow mapping, got {self.s[p]!r}")


def parse_yaml(text: str) -> Any:
    """One YAML document, read as `yaml.safe_load` reads it -- or YamlSubsetError when the
    text leaves the subset this parser handles (see the module docstring)."""
    try:
        return _Parser(text).document()
    except YamlSubsetError:
        raise
    except IndexError as exc:                       # a scanner ran off the end of the text
        raise YamlSubsetError(f"unexpected end of input ({exc})") from exc
    except ValueError as exc:                       # e.g. a timestamp with month 13
        raise YamlSubsetError(str(exc)) from exc


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
#: HREQ-X-01: the fields every entry carries (plus `description`, which is prose).
REQUIRED_FIELDS = ("id", "enabled", "owner", "phase", "paths", "depends_on", "tests", "adr",
                   "removal")
#: Words every removal recipe contains (HREQ-X-02: flag off, entry removed, gates run).
RECIPE_STEPS = ("enabled: false", "this entry", "tools/gates.py")
#: Where a module's paths could still be named by something that runs or indexes the system
#: after the module is gone: code, tests, tools, configuration, CI, packaging, the READMEs,
#: the commands table of 04 and this registry's page. Design documents, ADRs and logs are
#: history and are not scanned. config/health/modules.yaml itself is skipped (the entry is
#: the module's own).
LIVE_SCOPE = ("src", "tests", "tools", "config", ".github", "pyproject.toml", "README.md",
              "docs/README.md", "docs/health/README.md",
              "docs/health/04_ENVIRONMENTS_AND_UNDO.md", "docs/health/07_MODULE_REGISTRY.md")
#: Files that must belong to exactly one module (a file no module owns cannot be removed by
#: any recipe; a file two modules own is deleted by whichever goes first).
OWNED_SCOPE = (("src/health", "*"), ("tests", "health_*"), ("tests/fixtures/health", "*"),
               ("tools", "health_*"))
PAGE = ROOT / "docs" / "health" / "07_MODULE_REGISTRY.md"
PAGE_BEGIN = ("<!-- BEGIN generated by `PYTHONPATH=src python3 -m health.registry --write-07` "
              "from config/health/modules.yaml; edit the registry, not this block -->")
PAGE_END = "<!-- END generated -->"
_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SKIP_DIRS = frozenset({"__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", ".git",
                        "node_modules", ".venv"})


def _modules_from(data: Any) -> list[dict[str, Any]]:
    """The `modules` list of a parsed registry, the same way for both readers."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"registry: the top level is a {type(data).__name__}, not a mapping")
    mods = data.get("modules") or []
    if not isinstance(mods, list):
        raise ValueError(f"registry: 'modules' is a {type(mods).__name__}, not a list")
    out = []
    for m in mods:
        if not isinstance(m, dict):
            raise ValueError(f"registry: module entry {m!r} is not a mapping")
        out.append(dict(m))
    return out


def _scan(text: str) -> list[dict[str, Any]]:
    """The registry's modules read with the stdlib parser (the pre-install floor)."""
    return _modules_from(parse_yaml(text))


def registry_path() -> Path:
    """config/health/modules.yaml -- or the file HEALTH_MODULES_PATH names (tests only)."""
    override = os.environ.get(ENV_PATH)
    return Path(override) if override else DEFAULT_PATH


def display_path(path: Path | str) -> str:
    """`path` relative to the repository when it is inside it, else as given."""
    p = Path(path)
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def load_modules(path: Path | str | None = None, *, reader: str = "auto") -> list[dict[str, Any]]:
    """Every module in the registry, as dicts with at least `id` and `enabled`.

    `path` defaults to `registry_path()`. `reader` is "auto" (PyYAML when importable, else
    `parse_yaml`), "pyyaml" or "stdlib". Raises FileNotFoundError when the registry is
    absent -- a missing registry is a CFG defect, never an empty list that reads as "no
    modules".
    """
    p = Path(path) if path is not None else registry_path()
    text = p.read_text(encoding="utf-8")
    if reader not in ("auto", "pyyaml", "stdlib"):
        raise ValueError(f"reader must be auto, pyyaml or stdlib, not {reader!r}")
    if reader == "stdlib":
        return _scan(text)
    try:
        import yaml
    except ImportError:
        if reader == "pyyaml":
            raise
        return _scan(text)
    return _modules_from(yaml.safe_load(text))


def module_state(module_id: str, modules: list[dict[str, Any]] | None = None) -> str:
    """"enabled", "disabled" or "unregistered" for `module_id` (the registry is read when
    `modules` is None; FileNotFoundError / ValueError when it cannot be). Two entries under
    one id (a `--check` error) read as disabled unless both are on: the flag fails closed."""
    mods = load_modules() if modules is None else modules
    flags = [m.get("enabled") is True for m in mods if m.get("id") == module_id]
    if not flags:
        return "unregistered"
    return "enabled" if all(flags) else "disabled"


def is_enabled(module_id: str, modules: list[dict[str, Any]] | None = None) -> bool:
    """True only when `module_id` is registered with `enabled: true`.

    HREQ-X-01: the flag governs every surface -- `health.cli status` today, the app at M1.
    A surface asks this before it shows a module's output, and shows "unavailable" (with
    the reason) when the answer is no. The tests and the package do not ask: a disabled
    module is still imported by its tests and verified by the gates. The merge is the code
    switch; the flag is the surface switch (docs/health/07)."""
    return module_state(module_id, modules) == "enabled"


def format_modules(modules: list[dict[str, Any]]) -> str:
    """One line per state: `modules (n): id on · id on` and, separately, any disabled."""
    on = [str(m.get("id")) for m in modules if m.get("enabled") is True]
    off = [str(m.get("id")) for m in modules if m.get("enabled") is not True]
    lines = [f"modules ({len(modules)}): " + (" · ".join(f"{i} on" for i in on) or "(none enabled)")]
    if off:
        lines.append("modules DISABLED: " + " · ".join(off))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --check: the registry against the repository
# ---------------------------------------------------------------------------
def _strs(m: dict[str, Any], key: str) -> list[str]:
    v = m.get(key)
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _norm(path: str) -> str:
    return path.strip().rstrip("/")


def _under(rel: str, paths: list[str]) -> bool:
    return any(rel == _norm(p) or rel.startswith(_norm(p) + "/") for p in paths if _norm(p))


def _field_problems(modules: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    ids = [m.get("id") for m in modules]
    for i, m in enumerate(modules):
        mid = m.get("id")
        where = f"module {mid!r}" if isinstance(mid, str) else f"module entry {i + 1}"
        missing = [f for f in REQUIRED_FIELDS if f not in m]
        if missing:
            problems.append(f"{where}: missing {', '.join(missing)} (HREQ-X-01: every entry "
                            f"carries {', '.join(REQUIRED_FIELDS)})")
        if "id" in m and not (isinstance(mid, str) and _ID_RE.match(mid)):
            problems.append(f"{where}: id must be lower-case letters, digits and '-'")
        if "enabled" in m and not isinstance(m["enabled"], bool):
            problems.append(f"{where}: enabled is {m['enabled']!r}; it must be true or false")
        if "phase" in m and (isinstance(m["phase"], bool) or not isinstance(m["phase"], int)):
            problems.append(f"{where}: phase is {m['phase']!r}; it must be an integer")
        for key in ("owner", "adr", "removal"):
            if key in m and not (isinstance(m[key], str) and m[key].strip()):
                problems.append(f"{where}: {key} must be non-empty text")
        for key in ("paths", "depends_on", "tests"):
            v = m.get(key)
            if key in m and not (isinstance(v, list)
                                 and all(isinstance(x, str) and x.strip() for x in v)):
                problems.append(f"{where}: {key} must be a list of non-empty strings, not {v!r}")
        if isinstance(m.get("paths"), list) and not m["paths"]:
            problems.append(f"{where}: paths is empty -- a module with no files cannot be removed")
        removal = m.get("removal")
        if isinstance(removal, str):
            for phrase in RECIPE_STEPS:
                if phrase not in removal:
                    problems.append(f"{where}: the removal recipe never says {phrase!r} "
                                    "(HREQ-X-02: flag off, entry removed, tools/gates.py run)")
    for dupe in sorted({i for i in ids if isinstance(i, str) and ids.count(i) > 1}):
        problems.append(f"module {dupe!r}: registered {ids.count(dupe)} times -- ids are unique")
    return problems


def _edge_problems(modules: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    ids = {m.get("id") for m in modules}
    for m in modules:
        for dep in _strs(m, "depends_on"):
            if dep == m.get("id"):
                problems.append(f"module {dep!r}: depends on itself")
            elif dep not in ids:
                problems.append(f"module {m.get('id')!r}: depends on {dep!r}, which is not "
                                f"registered -- {m.get('id')!r} must be removed first (or "
                                "the edge was wrong)")
    graph = {m.get("id"): [d for d in _strs(m, "depends_on") if d in ids] for m in modules}
    state: dict[Any, int] = {}

    def visit(node: Any, trail: list[Any]) -> None:
        state[node] = 1
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                cycle = trail[trail.index(nxt):] + [nxt] if nxt in trail else [node, nxt]
                problems.append("depends_on cycle: " + " -> ".join(map(str, cycle))
                                + " (no removal order exists)")
            elif state.get(nxt) is None:
                visit(nxt, trail + [nxt])
        state[node] = 2

    for node in graph:
        if state.get(node) is None:
            visit(node, [node])
    return problems


def _existence_problems(modules: list[dict[str, Any]], root: Path) -> list[str]:
    problems: list[str] = []
    for m in modules:
        mid = m.get("id")
        for p in _strs(m, "paths"):
            if not (root / _norm(p)).exists():
                problems.append(f"module {mid!r}: path {p} does not exist")
        for t in _strs(m, "tests"):
            file, _, fn = t.partition("::")
            f = root / file
            if not f.is_file():
                problems.append(f"module {mid!r}: test {t} -- {file} does not exist (a module "
                                "removed with it? its recipe edits this entry)")
            elif fn and f"def {fn}(" not in f.read_text(encoding="utf-8", errors="replace"):
                problems.append(f"module {mid!r}: test {t} -- `def {fn}(` is not in {file}")
    return problems


def _owned_files(root: Path) -> list[str]:
    out: list[str] = []
    for base, pattern in OWNED_SCOPE:
        d = root / base
        if not d.is_dir():
            continue
        found = d.rglob(pattern) if pattern == "*" else d.glob(pattern)
        for f in found:
            rel = f.relative_to(root)
            if f.is_file() and not _SKIP_DIRS.intersection(rel.parts):
                out.append(rel.as_posix())
    return sorted(set(out))


def _module_of(name: str, root: Path) -> str | None:
    """The file a dotted `health.*` name is (src/<a/b>.py or src/<a/b>/__init__.py)."""
    base = root / "src" / Path(*name.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand.relative_to(root).as_posix()
    return None


def _health_imports(path: Path, rel: str) -> set[str]:
    """Every `health.*` module a source file imports, anywhere in it, with its parent
    packages (importing health.kb.check imports health.kb and health too)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()
    dotted = rel[len("src/"):-len(".py")].split("/")
    package = dotted[:-1]                                   # __init__.py: its own package
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            names.add(mod)
            names.update(f"{mod}.{a.name}" for a in node.names)
    out: set[str] = set()
    for n in names:
        parts = n.split(".")
        if parts[0] != "health":
            continue
        out.update(".".join(parts[:k]) for k in range(1, len(parts) + 1))
    return out


def _ownership_problems(modules: list[dict[str, Any]], root: Path) -> list[str]:
    problems: list[str] = []
    owners: dict[str, list[str]] = {}
    for rel in _owned_files(root):
        owners[rel] = [str(m.get("id")) for m in modules if _under(rel, _strs(m, "paths"))]
        if not owners[rel]:
            problems.append(f"{rel}: in no module's paths -- no recipe can remove it")
        elif len(owners[rel]) > 1:
            problems.append(f"{rel}: in the paths of {', '.join(owners[rel])} -- a file "
                            "belongs to one module")
    by_id = {m.get("id"): m for m in modules}
    for rel, own in sorted(owners.items()):
        if len(own) != 1 or not rel.startswith("src/") or not rel.endswith(".py"):
            continue
        me = by_id[own[0]]
        deps = set(_strs(me, "depends_on"))
        for name in sorted(_health_imports(root / rel, rel)):
            target = _module_of(name, root)
            them = owners.get(target or "", [])
            if len(them) == 1 and them[0] != own[0] and them[0] not in deps:
                problems.append(f"module {own[0]!r}: {rel} imports {name} ({target}, module "
                                f"{them[0]!r}) but depends_on does not list {them[0]!r}")
    return problems


def _tracked_paths(root: Path) -> list[Path] | None:
    """Git-tracked files under `root` (`git ls-files -z`), or None when git is unavailable
    or `root` is not a checkout -- the caller then walks the tree, so a copy with no .git
    is still checked. Ignored and untracked paths (a build's *.egg-info, a builder's
    worktree under .claude/worktrees/, a scratch file) are not the repository and never
    count as live files (BUG-20261003-166 taught the ledger gate the same rule)."""
    try:
        p = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                           capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return [root / n for n in p.stdout.decode("utf-8", "surrogateescape").split("\0") if n]


def _live_files(root: Path) -> list[tuple[str, str]]:
    tracked = _tracked_paths(root)
    out: dict[str, str] = {}
    for entry in LIVE_SCOPE:
        base = root / entry
        if tracked is not None:
            files = [f for f in tracked if f == base or base in f.parents]
        else:
            files = [base] if base.is_file() else sorted(base.rglob("*")) if base.is_dir() else []
        for f in files:
            rel = f.relative_to(root)
            if not f.is_file() or _SKIP_DIRS.intersection(rel.parts):
                continue
            try:
                out[rel.as_posix()] = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
    return sorted(out.items())


def _names_file(text: str, rel: str) -> bool:
    """`text` names the file `rel` as a whole path (README.md is not named by
    docs/health/README.md)."""
    return re.search(rf"(?<![\w./-]){re.escape(rel)}(?![\w/-])", text) is not None


def dependants(modules: list[dict[str, Any]], module_id: str) -> set[str]:
    """Every module that depends on `module_id`, directly or through another module (each
    of them is removed before it)."""
    out: set[str] = set()
    frontier = {module_id}
    while frontier:
        nxt = {str(m.get("id")) for m in modules
               if set(_strs(m, "depends_on")) & frontier} - out - {module_id}
        out |= nxt
        frontier = nxt
    return out


def recipe_gaps(modules: list[dict[str, Any]], root: Path = ROOT) -> list[str]:
    """For every module: each live file (LIVE_SCOPE) that names one of its paths and will
    still exist when it is removed -- not its own, not a dependant's -- must be named in its
    removal recipe. A recipe that forgets a file leaves a stale reference or a red gate."""
    problems: list[str] = []
    registry = DEFAULT_PATH.relative_to(ROOT).as_posix()
    live = _live_files(root)
    by_id = {m.get("id"): m for m in modules}
    for m in modules:
        mid = str(m.get("id"))
        own = _strs(m, "paths")
        gone = own + [p for d in dependants(modules, mid) for p in _strs(by_id[d], "paths")]
        needles = [_norm(p) for p in own if _norm(p)]
        removal = m.get("removal") if isinstance(m.get("removal"), str) else ""
        for rel, text in live:
            if rel == registry or _under(rel, gone) or _names_file(removal, rel):
                continue
            hit = next((n for n in needles if n in text), None)
            if hit:
                problems.append(f"module {mid!r}: {rel} names {hit}, and the removal recipe "
                                f"never mentions {rel} -- add the step that edits it (or "
                                "says why it stays)")
    return problems


def render_page_block(modules: list[dict[str, Any]]) -> str:
    """The generated block of docs/health/07: the table and every recipe, verbatim."""
    def cell(items: list[str]) -> str:
        return "<br>".join(f"`{x}`" for x in items) or "—"

    lines = [PAGE_BEGIN, "",
             "| id | enabled | phase | owner | depends on | paths | tests | ADR |",
             "|---|---|---|---|---|---|---|---|"]
    for m in modules:
        enabled = str(m.get("enabled")).lower() if isinstance(m.get("enabled"), bool) \
            else repr(m.get("enabled"))
        lines.append(f"| `{m.get('id')}` | {enabled} | {m.get('phase')} | {m.get('owner')} | "
                     f"{cell(_strs(m, 'depends_on'))} | {cell(_strs(m, 'paths'))} | "
                     f"{cell(_strs(m, 'tests'))} | {m.get('adr')} |")
    lines += ["", "### Removal recipes", ""]
    for m in modules:
        lines += [f"#### `{m.get('id')}`", "", str(m.get("removal") or "").rstrip(), ""]
    lines.append(PAGE_END)
    return "\n".join(lines)


def page_problems(modules: list[dict[str, Any]], page: Path = PAGE) -> list[str]:
    """docs/health/07's generated block must be `render_page_block(modules)`, byte for byte
    (HREQ-X-01: the page says the same thing as the file)."""
    if not page.is_file():
        return [f"{display_path(page)} does not exist -- the registry has no readable page"]
    text = page.read_text(encoding="utf-8")
    if text.count(PAGE_BEGIN) != 1 or text.count(PAGE_END) != 1:
        return [f"{display_path(page)}: the generated block's markers are missing or repeated"]
    start = text.index(PAGE_BEGIN)
    got = text[start:text.index(PAGE_END, start) + len(PAGE_END)]
    if got != render_page_block(modules):
        return [f"{display_path(page)}: the generated block is not the registry's rendering "
                "-- run `PYTHONPATH=src python3 -m health.registry --write-07`"]
    return []


def write_page(modules: list[dict[str, Any]], page: Path = PAGE) -> bool:
    """Replace 07's generated block with the registry's rendering; True when it changed."""
    text = page.read_text(encoding="utf-8")
    start = text.index(PAGE_BEGIN)
    end = text.index(PAGE_END, start) + len(PAGE_END)
    new = text[:start] + render_page_block(modules) + text[end:]
    if new != text:
        page.write_text(new, encoding="utf-8")
    return new != text


def validate_modules(modules: list[dict[str, Any]], root: Path = ROOT, *,
                     page: Path | None = PAGE) -> list[str]:
    """Every `--check` rule (see the module docstring); [] when the registry is sound."""
    problems = (_field_problems(modules) + _edge_problems(modules)
                + _existence_problems(modules, root) + _ownership_problems(modules, root)
                + recipe_gaps(modules, root))
    if page is not None:
        problems += page_problems(modules, page)
    return problems


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m health.registry",
                                 description="Check config/health/modules.yaml against the "
                                             "repository, or regenerate docs/health/07.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="every rule; exit 1 on a problem")
    group.add_argument("--write-07", action="store_true",
                       help="regenerate the generated block of docs/health/07_MODULE_REGISTRY.md")
    a = ap.parse_args(argv)
    path = registry_path()
    try:
        modules = load_modules(path)
    except (OSError, ValueError) as exc:
        print(f"module registry: cannot read {display_path(path)}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    if a.write_07:
        changed = write_page(modules)
        print(f"{display_path(PAGE)}: {'regenerated' if changed else 'already current'} "
              f"({len(modules)} modules)")
        return 0
    problems = validate_modules(modules)
    for p in problems:
        print(f"  {p}")
    print(f"module registry: {len(modules)} modules, {len(problems)} problem(s) -- "
          f"{'FAIL' if problems else 'PASS'}")
    return 1 if problems else 0


__all__ = ["DEFAULT_PATH", "ENV_PATH", "LIVE_SCOPE", "REQUIRED_FIELDS", "YamlSubsetError",
           "dependants", "display_path", "format_modules", "is_enabled", "load_modules",
           "module_state", "page_problems", "parse_yaml", "recipe_gaps", "registry_path",
           "render_page_block", "resolve_plain", "scan_quoted", "validate_modules",
           "write_page"]


if __name__ == "__main__":
    raise SystemExit(main())
