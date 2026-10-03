"""The module registry: config/health/modules.yaml, read without a dependency.

HREQ-X-01 / docs/health/04 §6.5: every module has a flag, and `health.cli status` prints
every module with its state so a disabled module is visible rather than merely absent.
A flag nothing reads is a CFG defect; this module is the minimum consumer.

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

import datetime
import math
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "config" / "health" / "modules.yaml"


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


def load_modules(path: Path | str | None = None, *, reader: str = "auto") -> list[dict[str, Any]]:
    """Every module in the registry, as dicts with at least `id` and `enabled`.

    `reader` is "auto" (PyYAML when importable, else `parse_yaml`), "pyyaml" or "stdlib".
    Raises FileNotFoundError when the registry is absent -- a missing registry is a CFG
    defect, never an empty list that reads as "no modules".
    """
    p = Path(path) if path is not None else DEFAULT_PATH
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


def format_modules(modules: list[dict[str, Any]]) -> str:
    """One line per state: `modules (n): id on · id on` and, separately, any disabled."""
    on = [str(m.get("id")) for m in modules if m.get("enabled") is True]
    off = [str(m.get("id")) for m in modules if m.get("enabled") is not True]
    lines = [f"modules ({len(modules)}): " + (" · ".join(f"{i} on" for i in on) or "(none enabled)")]
    if off:
        lines.append("modules DISABLED: " + " · ".join(off))
    return "\n".join(lines)


__all__ = ["DEFAULT_PATH", "YamlSubsetError", "format_modules", "load_modules", "parse_yaml",
           "resolve_plain", "scan_quoted"]
