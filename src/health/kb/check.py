"""KB integrity checker: kb/schema.json, enforced generically, plus the cross-file contract.

`check_kb(kb)` takes the dict `health.kb.load_kb()` returns and lists Findings. It never
raises on malformed data and never stops at the first problem: a broken record is
reported and the rest of the KB is still checked.

Two layers.

1. SCHEMA. The entities, relations and evidence documents are validated against the
   schema.json shipped beside them, read at check time: a JSON Schema draft-07 validator
   (stdlib only; the `jsonschema` package is not a dependency) evaluates every keyword
   the document uses -- type, enum, const, pattern, min/maxLength, minimum/maximum and
   the exclusive forms, multipleOf, min/maxItems, uniqueItems, items, additionalItems,
   contains, required, properties, patternProperties, additionalProperties,
   min/maxProperties, propertyNames, dependencies, allOf, anyOf, oneOf, not,
   if/then/else and local $ref. Nothing is mirrored by hand, so an edit to schema.json
   is ENFORCED on the data, not merely detected. Patterns are ECMA-262, as the schema
   language specifies: they are translated so that `\\d` is [0-9], `\\w` is
   [A-Za-z0-9_], `\\s` is JavaScript's Unicode whitespace (NBSP, U+FEFF, U+3000 ...),
   `.` excludes line terminators and `$` is end of input.
   A violation's CODE is chosen by keyword and by where in the schema it sits
   (`_PATTERN_CODES`, `_REF_CODES`): naming only, never the constraint itself.
   `schema-drift` (error) is what remains: a keyword or construct this validator cannot
   evaluate (it fails closed rather than skipping it), an unresolvable $ref, a pattern
   that is not translatable ECMA-262, and the few schema nodes the cross-file rules
   depend on for meaning -- the grade enum (its ranking drives grade-ceiling and the
   >= B share), the sourceType enum (the §3.2 default-grade mapping) and the fields the
   cross-file rules read.

2. CROSS-FILE. What JSON Schema cannot say (the schema's own description; docs/health/03
   §6; docs/health/01 HREQ-E-01..E-07): references resolve, parent scale <= child scale,
   engine mirrors, params.data.js == params.json, verified external identifiers (the
   LATEST log record for an entity/db/id governs: the log is append-only), word limits,
   unsourced conflicts say so, the grade ceiling, ranges hold their values.

Each rule has its own code in RULES; tests/health_kb_selftest.py plants a violation of
every one and checks it fires alone. DOCUMENTED_RULES maps every rule of the docs/03 §6
table to the codes implementing it, DEFERRED_RULES names the ones whose data fields do
not exist yet, and the self-test parses the §6 table to hold the two to the document.

Severities: `error` -- the contract is broken (kb-check exits 1); `warn` -- legal but
suspect; `info` -- reported metrics.

Policy on cascades: a malformed value is reported once, by the rule it breaks. Every
cross-file rule reads only values that passed the schema layer (and skips what it cannot
compute: no usable params.json -> no unused-evidence; no usable log -> no verification
rules), so a plant of one rule fires that rule alone.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import cache
from typing import Any
from urllib.parse import urlsplit

from . import DATA_FILES, RECORD_KEYS

# ---------------------------------------------------------------------------
# Findings and the rule registry
# ---------------------------------------------------------------------------
SEVERITIES = ("error", "warn", "info")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity:<5}  {self.code:<26}  {self.where}  {self.message}"


RULES: dict[str, tuple[str, str]] = {
    # -- schema layer (generic, from the live schema.json) -----------------------
    "file-shape": ("error", "a KB file is not a JSON object, or its record array is missing "
                            "or not an array"),
    "unknown-key": ("error", "a key the schema does not declare (additionalProperties: false, "
                             "propertyNames)"),
    "missing-field": ("error", "a field the schema requires is absent"),
    "wrong-type": ("error", "a value has the wrong JSON type (NaN and Infinity are not JSON "
                            "numbers), or matches none of an anyOf/oneOf's forms"),
    "empty": ("error", "an empty string, array or object where the schema requires content "
                       "(minLength / minItems / minProperties)"),
    "not-unique": ("error", "an array that must hold unique items repeats one (uniqueItems)"),
    "bad-enum": ("error", "a value outside its enum, or not its const"),
    "out-of-bounds": ("error", "a number, length or size outside its schema bounds (minimum, "
                               "maximum, exclusive*, multipleOf, maxLength, maxItems, ...)"),
    "range-shape": ("error", "a range is not an array of exactly two numbers "
                             "(definitions/range)"),
    "id-pattern": ("error", "an entity id (id, parent, relation from/to) does not match "
                            "definitions/id"),
    "evidence-id-pattern": ("error", "an evidence id (record id or reference) does not match "
                                     "definitions/evidenceId"),
    "relation-id-pattern": ("error", "a relation id does not match its schema pattern"),
    "external-id-pattern": ("error", "an externalIds value does not match its registry's pattern"),
    "doi-pattern": ("error", "evidence doi does not match its schema pattern (ECMA-262 \\S: "
                             "no Unicode space of any kind)"),
    "pmid-pattern": ("error", "evidence pmid does not match its schema pattern"),
    "url-https": ("error", "evidence url does not match its schema pattern (https://)"),
    "date-pattern": ("error", "accessed / checkedOn is not YYYY-MM-DD"),
    "pattern-mismatch": ("error", "a string fails a schema pattern that has no more specific "
                                  "code"),
    "doi-required": ("error", "evidence with kind 'doi' has no doi (schema if/then)"),
    "pmid-required": ("error", "evidence with kind 'pmid' has no pmid (schema if/then)"),
    "schema-violation": ("error", "a value fails a schema combinator with no more specific "
                                  "code (not, oneOf matched twice, contains, a false schema)"),
    "schema-drift": ("error", "schema.json uses a keyword or construct the checker cannot "
                              "evaluate, or changed a node a cross-file rule depends on (grade "
                              "ranking, sourceType mapping, a field the rules read)"),
    # -- cross-file contract ----------------------------------------------------------
    "duplicate-id": ("error", "the same id appears more than once in one file"),
    "dangling-evidence": ("error", "an evidence reference names no evidence record"),
    "dangling-endpoint": ("error", "a relation from/to names no entity"),
    "dangling-parent": ("error", "an entity parent names no entity"),
    "self-relation": ("error", "a relation whose from and to are the same entity"),
    "parent-scale": ("error", "an entity's parent has a larger scale than the entity"),
    "engine-params-unavailable": ("error", "no usable params.json was loaded, so the engine "
                                           "mirrors and the parameter rules cannot be checked"),
    "engine-param-unknown": ("error", "quantity.engineParam names no params.json row"),
    "engine-param-mismatch": ("error", "a mirrored quantity's value, range, unit or grade differs "
                                       "from its params.json row"),
    "params-mirror": ("error", "params.data.js (the engine's JavaScript copy) differs from "
                               "params.json, or cannot be read"),
    "param-field": ("error", "a params.json row lacks a number value, a non-empty unit, a "
                             "[low, high] range of numbers, a non-empty evidence list or a "
                             "grade from the enum (HREQ-E-01)"),
    "param-evidence-unresolved": ("error", "a params.json row cites an evidence id not in "
                                           "evidence.json"),
    "external-id-unverified": ("error", "an externalIds value whose LATEST VERIFICATION_LOG "
                                        "record for that entity/db/id is not resolved: true "
                                        "(or has none)"),
    "entity-removed": ("error", "the append-only VERIFICATION_LOG names an entity entities.json "
                                "no longer holds: a KB record was deleted (HREQ-E-06, partial "
                                "append-only check)"),
    "summary-too-long": ("error", "entity summary exceeds 60 words"),
    "quote-too-long": ("error", "evidence quote exceeds 25 words"),
    "conflict-unsourced-note": ("error", "a conflict with empty evidence whose note does not say "
                                         "so: one of the note's sentences or clauses (split at . "
                                         "; : and line breaks) must BEGIN with 'Unsourced' or "
                                         "'No verified source'"),
    "range-order": ("error", "a range whose low end exceeds its high end"),
    "value-outside-range": ("error", "a value outside its own range (quantity or params.json "
                                     "row)"),
    "grade-ceiling": ("error", "a grade better than the best defaultGrade of the evidence it "
                               "cites (HREQ-E-03)"),
    "evidence-grading-missing": ("error", "an evidence record without sourceType or defaultGrade "
                                          "(HREQ-E-02)"),
    "default-grade-mapping": ("error", "an evidence record's defaultGrade is not the one "
                                       "docs/health/01 §3.2 maps its sourceType to (HREQ-E-02)"),
    "evidence-unverified": ("error", "a cited evidence record has no verification record "
                                     "(HREQ-D-02)"),
    "evidence-retracted": ("error", "a cited evidence record whose verification.retraction is "
                                    "not 'none ...' / 'not applicable ...' (HREQ-D-02)"),
    "evidence-title-mismatch": ("error", "a cited evidence record whose verification says "
                                         "titleMatch: false (HREQ-D-02)"),
    # -- warnings: legal but suspect --------------------------------------------------------
    "unused-evidence": ("warn", "an evidence record no entity, relation, quantity, conflict or "
                                "engine param cites"),
    "orphan-verification": ("warn", "a resolved (latest) VERIFICATION_LOG record for an id its "
                                    "entity no longer carries"),
    "parent-cycle": ("warn", "following parent links returns to the start entity"),
    "meta-mismatch": ("warn", "entities/relations/evidence disagree on kbVersion, generated or "
                              "curator"),
    "doi-url": ("warn", "an evidence record of kind 'doi' whose url is not on doi.org"),
    "params-mirror-unchecked": ("warn", "no params.data.js was found beside the params.json in "
                                        "use, so params-mirror could not be checked"),
    # -- info ---------------------------------------------------------------------------------
    "e-assumption-share": ("info", "share of quantities (and engine params) graded E-assumption"),
}

DOCUMENTED_RULES: dict[str, tuple[str, ...]] = {
    "schema": ("file-shape", "unknown-key", "missing-field", "wrong-type", "empty",
               "not-unique", "bad-enum", "out-of-bounds", "range-shape", "id-pattern",
               "evidence-id-pattern", "relation-id-pattern", "external-id-pattern",
               "doi-pattern", "pmid-pattern", "url-https", "date-pattern", "pattern-mismatch",
               "doi-required", "pmid-required", "schema-violation", "schema-drift"),
    "ref-resolves": ("dangling-evidence", "dangling-endpoint", "dangling-parent",
                     "param-evidence-unresolved"),
    "parent-scale": ("parent-scale",),
    "engine-mirror": ("engine-params-unavailable", "engine-param-unknown",
                      "engine-param-mismatch"),
    "params-mirror": ("params-mirror",),
    "xid-verified": ("external-id-unverified",),
    "word-limits": ("summary-too-long", "quote-too-long"),
    "conflict-unsourced": ("conflict-unsourced-note",),
    "grade-ceiling": ("grade-ceiling",),
    "range-contains-value": ("value-outside-range", "range-order"),
    "unique-ids": ("duplicate-id",),
    "append-only": ("entity-removed",),          # partial; the rest is DEFERRED below
}
"""docs/health/03 §6 rule name -> the RULES codes that implement it (all `error`)."""

DEFERRED_RULES: dict[str, str] = {
    "range-kind": "needs a structured range-kind field on every sampled parameter (HREQ-E-08; "
                  "M1, tracker W-18); today the kind is prose in `notes`",
    "dispersion": "needs structured dispersion type and n fields (HREQ-E-09; M1, tracker W-18)",
    "calibration-link": "needs `calibratedAgainst` on params.json rows; `calibrates` lives only "
                        "on scenario expectations today (HREQ-E-10; M1, tracker W-18)",
    "fixed-reason": "needs a structured reason field on every `mc: false` row (HREQ-U-01; M1, "
                    "tracker W-18)",
    "append-only": "needs the last released snapshot of the KB and evidence ledger to compare "
                   "against, and a `supersedes` field the schema does not have (HREQ-E-06, "
                   "HREQ-V-18); until then only entity-removed runs",
}
"""docs/health/03 §6 rules not (fully) enforced yet, with what each is waiting for."""

GRADES = ("A-meta", "A-primary", "B-textbook", "C-model", "D-animal", "E-assumption")
"""Best first. The ranking grade-ceiling and `report`'s 'graded >= B' use."""

DEFAULT_GRADE_BY_SOURCE: dict[str, str] = {
    "meta-analysis": "A-meta", "systematic-review": "A-meta",
    "primary-human": "A-primary", "rct": "A-primary",
    "review": "B-textbook", "textbook": "B-textbook", "guideline": "B-textbook",
    "model": "C-model",
    "primary-animal": "D-animal", "primary-in-vitro": "D-animal",
}
"""docs/health/01 §3.2: sourceType -> defaultGrade (HREQ-E-02)."""

SUMMARY_MAX_WORDS = 60
QUOTE_MAX_WORDS = 25
META_FIELDS = ("kbVersion", "generated", "curator")
LOG_REQUIRED = ("entity", "db", "id", "resolved")
"""VERIFICATION_LOG.json has no schema; these are the fields the verification rules read."""
LOG_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

DRAFT_07 = "http://json-schema.org/draft-07/schema"

_RULE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("/properties", ("kbVersion", "generated", "curator", "entities", "relations", "evidence")),
    ("/definitions/Entity/properties", ("id", "parent", "scale", "externalIds", "summary",
                                        "evidence")),
    ("/definitions/Relation/properties", ("id", "from", "to", "quantity", "evidence",
                                          "conflicts")),
    ("/definitions/quantity/properties", ("value", "unit", "range", "evidence", "grade",
                                          "engineParam", "conflicts")),
    ("/definitions/conflict/properties", ("value", "range", "evidence", "note")),
    ("/definitions/Evidence/properties", ("id", "kind", "url", "quote", "sourceType",
                                          "defaultGrade", "verification")),
    ("/definitions/Evidence/properties/verification/properties", ("retraction", "titleMatch")),
)
"""Schema fields the cross-file rules read: renaming one away is schema-drift."""

_PATTERN_CODES: tuple[tuple[str, str], ...] = (
    ("#/definitions/id/", "id-pattern"),
    ("#/definitions/evidenceId/", "evidence-id-pattern"),
    ("#/definitions/Relation/properties/id/", "relation-id-pattern"),
    ("#/definitions/externalIds/", "external-id-pattern"),
    ("#/definitions/Evidence/properties/doi/", "doi-pattern"),
    ("#/definitions/Evidence/properties/pmid/", "pmid-pattern"),
    ("#/definitions/Evidence/properties/url/", "url-https"),
    ("#/definitions/Evidence/properties/accessed/", "date-pattern"),
    ("#/definitions/Evidence/properties/verification/properties/checkedOn/", "date-pattern"),
)
"""Where a failing `pattern` sits in the schema -> the finding's code (naming only)."""

_REF_CODES: dict[str, tuple[str, str]] = {
    "#/definitions/range": ("range-shape", "expected [low, high] (two numbers)"),
}
"""A $ref target whose violations are reported as ONE finding at the referring value."""

_CONDITIONAL_REQUIRED = {"doi": "doi-required", "pmid": "pmid-required"}

_UNSOURCED_CLAUSE = re.compile(r"(?:unsourced|no verified sources?)\b", re.I)
_CLAUSE_SPLIT = re.compile(r"[.;:\n]+")
_RETRACTION_CLEAR = re.compile(r"\s*(?:none|not applicable)\b", re.I)

_MISSING = object()

# ---------------------------------------------------------------------------
# ECMA-262 patterns
# ---------------------------------------------------------------------------
_DIGIT = ((0x30, 0x39),)
_WORD = ((0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A))
_SPACE = ((0x09, 0x0D), (0x20, 0x20), (0xA0, 0xA0), (0x1680, 0x1680), (0x2000, 0x200A),
          (0x2028, 0x2029), (0x202F, 0x202F), (0x205F, 0x205F), (0x3000, 0x3000),
          (0xFEFF, 0xFEFF))
"""JavaScript's \\s: WhiteSpace (TAB VT FF SP NBSP ZWNBSP and Unicode Zs) + LineTerminator."""
_CLASSES = {"d": _DIGIT, "w": _WORD, "s": _SPACE}


def _render(ranges: tuple[tuple[int, int], ...]) -> str:
    return "".join(f"\\U{lo:08x}" if lo == hi else f"\\U{lo:08x}-\\U{hi:08x}"
                   for lo, hi in ranges)


def _complement(ranges: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
    out, nxt = [], 0
    for lo, hi in ranges:
        if lo > nxt:
            out.append((nxt, lo - 1))
        nxt = hi + 1
    if nxt <= 0x10FFFF:
        out.append((nxt, 0x10FFFF))
    return tuple(out)


_W = _render(_WORD)
_WORD_BOUNDARY = f"(?:(?<=[{_W}])(?![{_W}])|(?<![{_W}])(?=[{_W}]))"
_NOT_WORD_BOUNDARY = f"(?:(?<=[{_W}])(?=[{_W}])|(?<![{_W}])(?![{_W}]))"
_DOT = "[^\\n\\r\\u2028\\u2029]"
_WS_RUN = re.compile(f"[{_render(_SPACE)}]+")


class PatternError(ValueError):
    """A schema pattern that is not ECMA-262 this translator can reproduce exactly."""


def ecma_to_python(pattern: str) -> str:
    """Translate an ECMA-262 (JSON Schema) pattern into a Python one with the same meaning.

    Unsupported constructs raise PatternError rather than being read with Python's
    meaning: backreferences, inline flags, `\\p{..}`, `\\c`, identity escapes of letters,
    `[]` / `[^]`.
    """
    out: list[str] = []
    i, n, in_class = 0, len(pattern), False
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                raise PatternError("pattern ends with a backslash")
            d = pattern[i + 1]
            i += 2
            if d.lower() in _CLASSES:
                ranges = _CLASSES[d.lower()]
                if in_class:
                    out.append(_render(_complement(ranges) if d.isupper() else ranges))
                else:
                    out.append(("[^" if d.isupper() else "[") + _render(ranges) + "]")
            elif d in "bB":
                out.append("\\x08" if in_class else
                           (_WORD_BOUNDARY if d == "b" else _NOT_WORD_BOUNDARY))
            elif d in "tnrvf":
                out.append("\\" + d)
            elif d == "x":
                hexs = pattern[i:i + 2]
                if len(hexs) != 2 or not all(h in "0123456789abcdefABCDEF" for h in hexs):
                    raise PatternError("\\x needs two hex digits")
                out.append("\\x" + hexs)
                i += 2
            elif d == "u":
                hexs = pattern[i:i + 4]
                if len(hexs) != 4 or not all(h in "0123456789abcdefABCDEF" for h in hexs):
                    raise PatternError("\\u needs four hex digits (\\u{...} is not supported)")
                out.append("\\u" + hexs)
                i += 4
            elif d == "0" and not pattern[i:i + 1].isdigit():
                out.append("\\x00")
            elif d.isalnum() or d == "_":
                raise PatternError(f"escape \\{d} is not supported (backreference, \\p, \\c, "
                                   "\\k or an identity escape of a letter)")
            else:
                out.append("\\" + d)
            continue
        if in_class:
            if c == "]":
                in_class = False
                out.append(c)
            elif c in "[&~|":
                out.append("\\" + c)
            else:
                out.append(c)
            i += 1
            continue
        if c == "[":
            j = i + 1
            if pattern[j:j + 1] == "^":
                j += 1
            if pattern[j:j + 1] == "]":
                raise PatternError("empty character classes [] / [^] are not supported")
            out.append(pattern[i:j])
            in_class = True
            i = j
            continue
        if c == "(" and pattern.startswith("(?", i):
            rest = pattern[i + 2:i + 4]
            if rest[:1] in (":", "=", "!") or rest in ("<=", "<!"):
                out.append("(?")
                i += 2
                continue
            if rest[:1] == "<":
                out.append("(?P<")
                i += 3
                continue
            raise PatternError(f"group syntax {pattern[i:i + 4]!r} is not ECMA-262")
        if c == ".":
            out.append(_DOT)
        elif c == "$":
            out.append("\\Z")
        else:
            out.append(c)
        i += 1
    if in_class:
        raise PatternError("unterminated character class")
    return "".join(out)


@cache
def _regex(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(ecma_to_python(pattern))
    except re.error as exc:
        raise PatternError(f"does not compile: {exc}") from exc


def matches(pattern: str, value: str) -> bool:
    """ECMA-262 `RegExp(pattern).test(value)` (JSON Schema `pattern` semantics)."""
    return _regex(pattern).search(value) is not None


def word_count(text: str) -> int:
    """Words as JavaScript's `text.trim().split(/\\s+/)` counts them (ECMA whitespace)."""
    return sum(1 for w in _WS_RUN.split(text) if w)


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
_JSON_TYPES = ("null", "boolean", "object", "array", "number", "string", "integer")


def _jtype(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number" if math.isfinite(v) else f"non-finite number ({v})"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _is_number(v: Any) -> bool:
    return (isinstance(v, int) and not isinstance(v, bool)) or (
        isinstance(v, float) and math.isfinite(v))


def _is_integer(v: Any) -> bool:
    return (isinstance(v, int) and not isinstance(v, bool)) or (
        isinstance(v, float) and math.isfinite(v) and v.is_integer())


def _type_ok(v: Any, t: Any) -> bool:
    if isinstance(t, list):
        return any(_type_ok(v, x) for x in t)
    if t == "number":
        return _is_number(v)
    if t == "integer":
        return _is_integer(v)
    if t == "string":
        return isinstance(v, str)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "null":
        return v is None
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    return False


def _jkey(v: Any) -> Any:
    """A hashable key with JSON equality (1 == 1.0, true != 1, objects unordered)."""
    if v is None:
        return ("z",)
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float)):
        return ("n", v)
    if isinstance(v, str):
        return ("s", v)
    if isinstance(v, list):
        return ("a", tuple(_jkey(x) for x in v))
    if isinstance(v, dict):
        return ("o", tuple(sorted((str(k), _jkey(x)) for k, x in v.items())))
    return ("?", repr(v))


def _canonical(v: Any) -> str:
    try:
        return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(v)


def _pointer(doc: Any, pointer: str) -> Any:
    node = doc
    for raw in pointer.strip("/").split("/") if pointer.strip("/") else []:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _esc(token: str) -> str:
    token = str(token)
    if "~" in token or "/" in token:
        return token.replace("~", "~0").replace("/", "~1")
    return token


def _label(fname: str, rec: Any, index: int) -> str:
    if isinstance(rec, dict) and isinstance(rec.get("id"), str) and rec["id"]:
        return f"{fname}[{rec['id']}]"
    return f"{fname}[#{index}]"


def _says_unsourced(note: str) -> bool:
    for clause in _CLAUSE_SPLIT.split(note):
        if _UNSOURCED_CLAUSE.match(clause.strip().lstrip("([{\"'*_-–— ")):
            return True
    return False


def _show(v: Any) -> str:
    return "absent" if v is _MISSING else _canonical(v)


def _same(a: Any, b: Any) -> bool:
    if a is _MISSING or b is _MISSING:
        return False
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, (bool, list, dict)) or isinstance(b, (bool, list, dict)):
        return _canonical(a) == _canonical(b)
    return type(a) is type(b) and a == b


# ---------------------------------------------------------------------------
# The generic draft-07 validator
# ---------------------------------------------------------------------------
_KEYWORDS = frozenset({
    "$ref", "type", "enum", "const", "pattern", "minLength", "maxLength", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "items", "additionalItems",
    "minItems", "maxItems", "uniqueItems", "contains", "required", "properties",
    "patternProperties", "additionalProperties", "minProperties", "maxProperties",
    "propertyNames", "dependencies", "allOf", "anyOf", "oneOf", "not", "if", "then", "else"})
_ANNOTATIONS = frozenset({"$schema", "$id", "$comment", "title", "description", "default",
                          "examples", "readOnly", "writeOnly", "definitions",
                          "contentMediaType", "contentEncoding"})
_SCHEMA_VALUED = ("additionalProperties", "additionalItems", "contains", "propertyNames",
                  "not", "if", "then", "else")
_SCHEMA_LISTS = ("allOf", "anyOf", "oneOf")
_COUNTS = ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties")
_BOUNDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")

(_K_ENUM, _K_CONST, _K_PATTERN, _K_COUNT, _K_BOUND, _K_ARRAY, _K_OBJECT, _K_COMBINATOR,
 _K_NOT, _K_IF) = range(10)
_KIND: dict[str, int] = {
    "enum": _K_ENUM, "const": _K_CONST, "pattern": _K_PATTERN,
    **dict.fromkeys(_COUNTS, _K_COUNT), **dict.fromkeys(_BOUNDS, _K_BOUND),
    **dict.fromkeys(("items", "additionalItems", "uniqueItems", "contains"), _K_ARRAY),
    **dict.fromkeys(("required", "properties", "patternProperties", "additionalProperties",
                     "propertyNames", "dependencies"), _K_OBJECT),
    **dict.fromkeys(_SCHEMA_LISTS, _K_COMBINATOR), "not": _K_NOT, "if": _K_IF,
}
"""Asserting keywords by kind; `type` and `$ref` are handled first, `then`/`else` by `if`.
A violation is a tuple (instance path, schema pointer, keyword, code, message, extra)."""


class _Schema:
    """One schema document: linting (schema-drift) and validation of instances."""

    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root
        self.drift: list[tuple[str, str]] = []           # (pointer, message)
        self._enums: dict[int, tuple[Any, frozenset]] = {}
        self._refs: dict[str, tuple[Any, str]] = {}
        self._plans: dict[int, tuple[dict[str, Any], tuple[tuple[str, Any, int], ...]]] = {}

    def enum_keys(self, values: list[Any]) -> frozenset:
        hit = self._enums.get(id(values))
        if hit is None or hit[0] is not values:
            hit = (values, frozenset(_jkey(e) for e in values))
            self._enums[id(values)] = hit
        return hit[1]

    # -- refs and patterns ---------------------------------------------------------
    def ref(self, ref: Any) -> tuple[Any, str]:
        hit = self._refs.get(ref) if isinstance(ref, str) else None
        if hit is None:
            if not isinstance(ref, str) or not (ref == "#" or ref.startswith("#/")):
                return _MISSING, ""
            hit = self._refs[ref] = (_pointer(self.root, ref[1:]), ref)
        return hit

    def pattern_code(self, ptr: str) -> str:
        for prefix, code in _PATTERN_CODES:
            if ptr.startswith(prefix):
                return code
        return "pattern-mismatch"

    # -- lint -----------------------------------------------------------------------------
    def lint(self) -> None:
        declared = self.root.get("$schema", DRAFT_07)
        if not isinstance(declared, str) or declared.rstrip("#") != DRAFT_07:
            self.drift.append(("#/$schema", f"declares {declared!r}; the checker evaluates "
                                            "JSON Schema draft-07 only"))
        self._lint(self.root, "#")

    def _bad(self, ptr: str, msg: str) -> None:
        self.drift.append((ptr, msg))

    def _lint(self, node: Any, ptr: str) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            self._bad(ptr, f"a schema must be an object or a boolean, not {_jtype(node)}")
            return
        if "$ref" in node:
            target, _ = self.ref(node["$ref"])
            if target is _MISSING:
                self._bad(f"{ptr}/$ref", f"$ref {node['$ref']!r} does not resolve to a node "
                                         "of this schema (only local refs are evaluated)")
            extra = sorted(set(node) - {"$ref"} - _ANNOTATIONS)
            if extra:
                self._bad(ptr, f"keywords {extra} beside $ref are ignored by draft-07; the "
                               "checker cannot tell which meaning was intended")
        for kw, val in node.items():
            p = f"{ptr}/{_esc(kw)}"
            if kw == "definitions":
                if not isinstance(val, dict):
                    self._bad(p, "definitions must be an object")
                    continue
                for name, sub in val.items():
                    self._lint(sub, f"{p}/{_esc(name)}")
                continue
            if kw in _ANNOTATIONS or kw == "$ref":
                continue
            if kw not in _KEYWORDS:
                self._bad(p, f"keyword {kw!r} is not evaluated by the checker (it fails closed "
                             "instead of skipping it)")
                continue
            self._lint_value(kw, val, p)

    def _lint_value(self, kw: str, val: Any, p: str) -> None:
        if kw == "type":
            types = val if isinstance(val, list) else [val]
            if not types or any(t not in _JSON_TYPES for t in types):
                self._bad(p, f"type {_canonical(val)} is not a JSON Schema type")
        elif kw == "enum":
            if not isinstance(val, list) or not val:
                self._bad(p, "enum must be a non-empty array")
        elif kw == "pattern":
            self._lint_pattern(val, p)
        elif kw in _COUNTS:
            if not _is_integer(val) or val < 0:
                self._bad(p, f"{kw} must be a non-negative integer")
        elif kw in _BOUNDS:
            if not _is_number(val) or (kw == "multipleOf" and val <= 0):
                self._bad(p, f"{kw} must be a number" + (" > 0" if kw == "multipleOf" else ""))
        elif kw == "uniqueItems":
            if not isinstance(val, bool):
                self._bad(p, "uniqueItems must be a boolean")
        elif kw == "required":
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                self._bad(p, "required must be an array of strings")
        elif kw in ("properties", "patternProperties"):
            if not isinstance(val, dict):
                self._bad(p, f"{kw} must be an object")
                return
            for name, sub in val.items():
                if kw == "patternProperties":
                    self._lint_pattern(name, f"{p}/{_esc(name)}")
                self._lint(sub, f"{p}/{_esc(name)}")
        elif kw == "items":
            if isinstance(val, list):
                for i, sub in enumerate(val):
                    self._lint(sub, f"{p}/{i}")
            else:
                self._lint(val, p)
        elif kw in _SCHEMA_LISTS:
            if not isinstance(val, list) or not val:
                self._bad(p, f"{kw} must be a non-empty array of schemas")
                return
            for i, sub in enumerate(val):
                self._lint(sub, f"{p}/{i}")
        elif kw == "dependencies":
            if not isinstance(val, dict):
                self._bad(p, "dependencies must be an object")
                return
            for name, dep in val.items():
                if isinstance(dep, list):
                    if not all(isinstance(x, str) for x in dep):
                        self._bad(f"{p}/{_esc(name)}", "a property dependency must list strings")
                else:
                    self._lint(dep, f"{p}/{_esc(name)}")
        elif kw in _SCHEMA_VALUED:
            self._lint(val, p)

    def _lint_pattern(self, val: Any, p: str) -> None:
        if not isinstance(val, str):
            self._bad(p, "a pattern must be a string")
            return
        try:
            _regex(val)
        except PatternError as exc:
            self._bad(p, f"pattern {val!r} cannot be evaluated as ECMA-262: {exc}")

    # -- validation ------------------------------------------------------------------------
    def admits(self, sch: Any, inst: Any) -> bool:
        """Whether `sch`'s own `type` (following $ref) admits `inst`."""
        for _ in range(32):
            if not isinstance(sch, dict):
                return True
            if "$ref" in sch:
                sch, _ = self.ref(sch["$ref"])
                continue
            return "type" not in sch or _type_ok(inst, sch["type"])
        return True

    def validate(self, inst: Any, sch: Any, ptr: str, path: tuple, out: list) -> None:
        if sch is True:
            return
        if sch is False:
            out.append((path, ptr, "false", "schema-violation", "no value is allowed here "
                        "(the schema is false)", None))
            return
        if not isinstance(sch, dict):
            return
        if "$ref" in sch:
            target, tptr = self.ref(sch["$ref"])
            if target is _MISSING:
                return                                    # reported as schema-drift
            collapse = _REF_CODES.get(tptr)
            if collapse is None:
                self.validate(inst, target, tptr, path, out)
                return
            sub: list = []
            self.validate(inst, target, tptr, path, sub)
            if sub:
                code, want = collapse
                out.append((path, tptr, "$ref", code, f"{want}, got {_canonical(inst)}", None))
            return
        t = sch.get("type", _MISSING)
        if t is not _MISSING and isinstance(t, (str, list)) and not _type_ok(inst, t):
            want = " or ".join(map(str, t)) if isinstance(t, list) else t
            out.append((path, f"{ptr}/type", "type", "wrong-type",
                        f"expected {want}, got {_jtype(inst)}", None))
            return                                        # a mistyped value is reported once
        for kw, val, kind in self.plan(sch):
            p = ptr + "/" + kw                            # keywords need no ~ escaping
            if kind == _K_ENUM:
                if isinstance(val, list) and _jkey(inst) not in self.enum_keys(val):
                    allowed = ", ".join(str(e) for e in val)
                    out.append((path, p, kw, "bad-enum", f"{_canonical(inst)} is not one of "
                                f"{allowed}", None))
            elif kind == _K_CONST:
                if _jkey(inst) != _jkey(val):
                    out.append((path, p, kw, "bad-enum",
                                f"{_canonical(inst)} is not {_canonical(val)}", None))
            elif kind == _K_PATTERN:
                if isinstance(inst, str) and isinstance(val, str):
                    try:
                        ok = matches(val, inst)
                    except PatternError:
                        continue                          # reported as schema-drift
                    if not ok:
                        out.append((path, p, kw, self.pattern_code(p),
                                    f"{inst!r} does not match {val}", None))
            elif kind == _K_COUNT:
                self._count(kw, val, inst, path, p, out)
            elif kind == _K_BOUND:
                self._bound(kw, val, inst, path, p, out)
            elif kind == _K_ARRAY:
                if isinstance(inst, list):
                    self._array(kw, val, sch, inst, path, p, out)
            elif kind == _K_OBJECT:
                if isinstance(inst, dict):
                    self._object(kw, val, sch, ptr, inst, path, p, out)
            elif kind == _K_COMBINATOR:
                if isinstance(val, list):
                    self._combinator(kw, val, inst, path, p, out)
            elif kind == _K_NOT:
                sub: list = []
                self.validate(inst, val, p, path, sub)
                if not sub:
                    out.append((path, p, kw, "schema-violation",
                                "matches a schema it must not match (not)", None))
            elif kind == _K_IF:
                self._conditional(val, sch, ptr, inst, path, out)

    def plan(self, sch: dict[str, Any]) -> tuple[tuple[str, Any, int], ...]:
        """The keywords of one schema node that assert something, classified once per check."""
        hit = self._plans.get(id(sch))
        if hit is None or hit[0] is not sch:
            steps = tuple((kw, val, _KIND[kw]) for kw, val in sch.items() if kw in _KIND)
            hit = self._plans[id(sch)] = (sch, steps)
        return hit[1]

    def _count(self, kw: str, limit: Any, inst: Any, path: tuple, p: str, out: list) -> None:
        if not _is_integer(limit):
            return
        if kw.endswith("Length"):
            if not isinstance(inst, str):
                return
            size, what = len(inst), "length"
        elif kw.endswith("Items"):
            if not isinstance(inst, list):
                return
            size, what = len(inst), "item count"
        else:
            if not isinstance(inst, dict):
                return
            size, what = len(inst), "property count"
        if kw.startswith("min") and size < limit:
            if size == 0:
                out.append((path, p, kw, "empty", f"must not be empty ({kw} {int(limit)})", None))
            else:
                out.append((path, p, kw, "out-of-bounds",
                            f"{what} {size} is below {kw} {int(limit)}", None))
        elif kw.startswith("max") and size > limit:
            out.append((path, p, kw, "out-of-bounds", f"{what} {size} exceeds {kw} {int(limit)}",
                        None))

    def _bound(self, kw: str, limit: Any, inst: Any, path: tuple, p: str, out: list) -> None:
        if not _is_number(inst) or not _is_number(limit):
            return
        bad = {"minimum": inst < limit, "maximum": inst > limit,
               "exclusiveMinimum": inst <= limit, "exclusiveMaximum": inst >= limit}.get(kw)
        if kw == "multipleOf":
            q = inst / limit
            bad = not (math.isfinite(q) and abs(q - round(q)) < 1e-9)
        if bad:
            out.append((path, p, kw, "out-of-bounds", f"{inst} violates {kw} {limit}", None))

    def _array(self, kw: str, val: Any, sch: dict, inst: list, path: tuple, p: str,
               out: list) -> None:
        if kw == "items":
            if isinstance(val, list):
                for i, sub in enumerate(val[:len(inst)]):
                    self.validate(inst[i], sub, f"{p}/{i}", path + (i,), out)
            else:
                for i, item in enumerate(inst):
                    self.validate(item, val, p, path + (i,), out)
        elif kw == "additionalItems":
            items = sch.get("items")
            if isinstance(items, list) and len(inst) > len(items):
                if val is False:
                    out.append((path, p, kw, "out-of-bounds", f"{len(inst)} items; the schema "
                                f"allows {len(items)}", None))
                else:
                    for i in range(len(items), len(inst)):
                        self.validate(inst[i], val, p, path + (i,), out)
        elif kw == "uniqueItems":
            if val is True:
                counts = Counter(_jkey(x) for x in inst)
                seen: set = set()
                for x in inst:
                    k = _jkey(x)
                    if counts[k] > 1 and k not in seen:
                        seen.add(k)
                        out.append((path, p, kw, "not-unique",
                                    f"{_canonical(x)} appears {counts[k]} times (uniqueItems)",
                                    None))
        elif kw == "contains":
            for item in inst:
                sub: list = []
                self.validate(item, val, p, path, sub)
                if not sub:
                    return
            out.append((path, p, kw, "schema-violation", "no item matches `contains`", None))

    def _object(self, kw: str, val: Any, sch: dict, ptr: str, inst: dict, path: tuple, p: str,
                out: list) -> None:
        if kw == "required":
            for key in val if isinstance(val, list) else []:
                if key not in inst:
                    out.append((path, p, kw, "missing-field",
                                f"required field {key!r} is missing", key))
        elif kw == "properties":
            if isinstance(val, dict):
                for key, sub in val.items():
                    if key in inst:
                        self.validate(inst[key], sub, f"{p}/{_esc(key)}", path + (key,), out)
        elif kw == "patternProperties":
            if isinstance(val, dict):
                for pat, sub in val.items():
                    try:
                        rx = _regex(pat)
                    except PatternError:
                        continue
                    for key in inst:
                        if rx.search(key):
                            self.validate(inst[key], sub, f"{p}/{_esc(pat)}", path + (key,), out)
        elif kw == "additionalProperties":
            declared = sch.get("properties") if isinstance(sch.get("properties"), dict) else {}
            pats = []
            for pat in sch.get("patternProperties") or {}:
                try:
                    pats.append(_regex(pat))
                except PatternError:
                    continue
            for key in inst:
                if key in declared or any(rx.search(key) for rx in pats):
                    continue
                if val is False:
                    names = ", ".join(declared) or "none"
                    out.append((path + (key,), p, kw, "unknown-key",
                                f"key {key!r} is not declared by the schema (additionalProperties"
                                f": false; declared: {names})", None))
                else:
                    self.validate(inst[key], val, p, path + (key,), out)
        elif kw == "propertyNames":
            for key in inst:
                sub: list = []
                self.validate(key, val, p, path + (key,), sub)
                if sub:
                    out.append((path + (key,), p, kw, "unknown-key",
                                f"key {key!r} fails propertyNames ({sub[0][4]})", None))
        elif kw == "dependencies":
            if isinstance(val, dict):
                for key, dep in val.items():
                    if key not in inst:
                        continue
                    if isinstance(dep, list):
                        for other in dep:
                            if other not in inst:
                                out.append((path, p, kw, "missing-field", f"field {other!r} is "
                                            f"required when {key!r} is present", other))
                    else:
                        self.validate(inst, dep, f"{p}/{_esc(key)}", path, out)

    def _combinator(self, kw: str, branches: list, inst: Any, path: tuple, p: str,
                    out: list) -> None:
        if kw == "allOf":
            for i, sub in enumerate(branches):
                self.validate(inst, sub, f"{p}/{i}", path, out)
            return
        results: list[tuple[Any, list]] = []
        for i, sub in enumerate(branches):
            errs: list = []
            self.validate(inst, sub, f"{p}/{i}", path, errs)
            if kw == "anyOf" and not errs:
                return                                    # one passing branch is enough
            results.append((sub, errs))
        passing = sum(1 for _, errs in results if not errs)
        if (kw == "anyOf" and passing) or (kw == "oneOf" and passing == 1):
            return
        if kw == "oneOf" and passing > 1:
            out.append((path, p, kw, "schema-violation",
                        f"matches {passing} of the oneOf forms; exactly one is allowed", None))
            return
        admitted = [errs for sub, errs in results if self.admits(sub, inst)]
        if len(admitted) == 1:
            out.extend(admitted[0])
        elif admitted:
            out.extend(min(admitted, key=len))
        else:
            out.append((path, p, kw, "wrong-type",
                        f"{_jtype(inst)} matches none of the allowed forms", None))

    def _conditional(self, cond: Any, sch: dict, ptr: str, inst: Any, path: tuple,
                     out: list) -> None:
        sub: list = []
        self.validate(inst, cond, f"{ptr}/if", path, sub)
        branch = "then" if not sub else "else"
        if branch not in sch:
            return
        res: list = []
        self.validate(inst, sch[branch], f"{ptr}/{branch}", path, res)
        when = _describe_condition(cond) if branch == "then" else "the if-condition fails"
        for v in res:
            if v[2] == "required" and v[0] == path:
                code = _CONDITIONAL_REQUIRED.get(v[5], "missing-field")
                out.append((v[0], v[1], v[2], code, f"{when} but the record has no {v[5]}", v[5]))
            else:
                out.append(v)


def _describe_condition(cond: Any) -> str:
    props = cond.get("properties") if isinstance(cond, dict) else None
    if isinstance(props, dict) and len(props) == 1:
        (key, sub), = props.items()
        if isinstance(sub, dict) and "const" in sub:
            return f"{key} is {_canonical(sub['const'])}"
    return f"the record matches {_canonical(cond)}"


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------
class _Checker:
    def __init__(self, kb: dict[str, Any]) -> None:
        self.kb = kb
        self.findings: list[Finding] = []
        self.bad: set[tuple] = set()          # instance paths that broke a schema/log rule
        self.lists: dict[str, list[Any] | None] = {}
        self.ev_refs: list[tuple[str, str]] = []     # (evidence id, where)

    # -- emit ---------------------------------------------------------------------------
    def add(self, code: str, where: str, message: str) -> None:
        self.findings.append(Finding(code, RULES[code][0], where, message))

    def where(self, path: tuple) -> str:
        key = path[0]
        fname = DATA_FILES[key]
        rest = list(path[1:])
        out = fname
        if len(rest) >= 2 and isinstance(rest[1], int):
            arr = rest[0]
            doc = self.kb.get(key)
            items = doc.get(arr) if isinstance(doc, dict) else None
            rec = items[rest[1]] if isinstance(items, list) and rest[1] < len(items) else None
            label = (_label("", rec, rest[1]) if key != "verification" else f"[#{rest[1]}]")
            out = (fname if arr == RECORD_KEYS[key] else f"{fname}.{arr}") + label
            rest = rest[2:]
        for part in rest:
            out += f"[{part}]" if isinstance(part, int) else f".{part}"
        return out

    def clean(self, path: tuple) -> bool:
        return path not in self.bad

    # -- schema layer ---------------------------------------------------------------------
    def schema_layer(self) -> None:
        schema_doc = self.kb.get("schema")
        fname = DATA_FILES["schema"]
        schema: _Schema | None = None
        if not isinstance(schema_doc, dict):
            self.add("schema-drift", fname, f"expected the schema object, got "
                                            f"{_jtype(schema_doc)}; no schema rule can be "
                                            "evaluated")
        else:
            schema = _Schema(schema_doc)
            schema.lint()
            for ptr, msg in schema.drift:
                self.add("schema-drift", f"{fname}{ptr}", msg)
            self.dependencies(schema_doc)
        for key in ("entities", "relations", "evidence"):
            doc = self.document(key)
            if doc is None or schema is None:
                continue
            out: list = []
            schema.validate(doc, schema_doc, "#", (key,), out)
            for path, _ptr, _kw, code, message, _extra in out:
                self.bad.add(path)
                self.add(code, self.where(path), message)
        self.verification_log()

    def dependencies(self, schema: dict[str, Any]) -> None:
        fname = DATA_FILES["schema"]
        grades = _pointer(schema, "/definitions/grade/enum")
        if not isinstance(grades, list) or set(map(str, grades)) != set(GRADES):
            self.add("schema-drift", f"{fname}#/definitions/grade/enum",
                     f"the checker ranks {list(GRADES)} (grade-ceiling, the >= B share) but the "
                     f"schema's grade enum is {_show(grades)}; give health/kb/check.py GRADES "
                     "the new ranking")
        sources = _pointer(schema, "/definitions/Evidence/properties/sourceType/enum")
        if not isinstance(sources, list) or set(map(str, sources)) != set(DEFAULT_GRADE_BY_SOURCE):
            self.add("schema-drift", f"{fname}#/definitions/Evidence/properties/sourceType/enum",
                     f"the §3.2 default-grade mapping covers {sorted(DEFAULT_GRADE_BY_SOURCE)} but "
                     f"the schema's sourceType enum is {_show(sources)}; extend "
                     "DEFAULT_GRADE_BY_SOURCE (docs/health/01 §3.2)")
        for ptr, fields in _RULE_FIELDS:
            node = _pointer(schema, ptr)
            missing = [f for f in fields if not (isinstance(node, dict) and f in node)]
            if missing:
                self.add("schema-drift", f"{fname}#{ptr}",
                         f"cross-file rules read {missing}, which the schema no longer declares "
                         "there; update health/kb/check.py with the schema")

    def document(self, key: str) -> dict[str, Any] | None:
        """The document to validate (its record array type-checked here), or None."""
        fname = DATA_FILES[key]
        doc = self.kb.get(key)
        self.lists[key] = None
        if not isinstance(doc, dict):
            self.add("file-shape", fname, f"expected a JSON object, got {_jtype(doc)}")
            return None
        list_key = RECORD_KEYS[key]
        items = doc.get(list_key, _MISSING)
        if items is _MISSING:
            self.add("file-shape", fname, f"has no {list_key!r} array")
            return doc
        if not isinstance(items, list):
            self.add("file-shape", f"{fname}.{list_key}", f"expected an array, got {_jtype(items)}")
            return {k: v for k, v in doc.items() if k != list_key}
        self.lists[key] = items
        return doc

    def verification_log(self) -> None:
        fname = DATA_FILES["verification"]
        doc = self.kb.get("verification")
        self.lists["verification"] = None
        if not isinstance(doc, dict):
            self.add("file-shape", fname, f"expected a JSON object, got {_jtype(doc)}")
            return
        items = doc.get("records", _MISSING)
        if items is _MISSING:
            self.add("file-shape", fname, "has no 'records' array")
            return
        if not isinstance(items, list):
            self.add("file-shape", f"{fname}.records", f"expected an array, got {_jtype(items)}")
            return
        self.lists["verification"] = items
        for i, r in enumerate(items):
            path = ("verification", "records", i)
            w = f"{fname}[#{i}]"
            if not isinstance(r, dict):
                self.bad.add(path)
                self.add("wrong-type", w, f"expected an object, got {_jtype(r)}")
                continue
            for key in LOG_REQUIRED:
                if key not in r:
                    self.bad.add(path)
                    self.add("missing-field", w, f"required field {key!r} is missing")
            for key in ("entity", "db", "id"):
                if key in r and not isinstance(r[key], str):
                    self.bad.add(path)
                    self.add("wrong-type", f"{w}.{key}", f"expected string, got {_jtype(r[key])}")
            if "resolved" in r and not isinstance(r["resolved"], bool):
                self.bad.add(path)
                self.add("wrong-type", f"{w}.resolved",
                         f"expected boolean, got {_jtype(r['resolved'])}")
            if "checkedOn" in r:
                v = r["checkedOn"]
                if not isinstance(v, str):
                    self.add("wrong-type", f"{w}.checkedOn", f"expected string, got {_jtype(v)}")
                elif not matches(LOG_DATE_PATTERN, v):
                    self.add("date-pattern", f"{w}.checkedOn",
                             f"{v!r} does not match {LOG_DATE_PATTERN}")

    # -- cross-file -------------------------------------------------------------------------
    def string(self, obj: dict[str, Any], key: str, path: tuple) -> str | None:
        v = obj.get(key)
        return v if isinstance(v, str) and self.clean(path + (key,)) else None

    def refs(self, obj: dict[str, Any], key: str, path: tuple, where: str) -> list[str] | None:
        """The well-formed evidence ids in obj[key] (recorded as citations), or None."""
        v = obj.get(key)
        if not isinstance(v, list):
            return None
        out = [x for i, x in enumerate(v) if isinstance(x, str) and self.clean(path + (key, i))]
        self.ev_refs.extend((x, where) for x in out)
        return out

    def range_pair(self, obj: dict[str, Any], path: tuple, where: str) -> tuple[Any, Any] | None:
        rng = obj.get("range")
        if not (isinstance(rng, list) and len(rng) == 2 and all(_is_number(x) for x in rng)
                and self.clean(path + ("range",))):
            return None
        lo, hi = rng
        if lo > hi:
            self.add("range-order", f"{where}.range", f"low end {lo} exceeds high end {hi}")
            return None
        return lo, hi

    def cross_file(self) -> None:
        ents, rels, evs = (self.lists.get(k) for k in ("entities", "relations", "evidence"))
        entity_ids: dict[str, dict[str, Any]] = {}
        all_entity_ids: set[str] = set()
        parents: list[tuple[str, str, str]] = []
        ext_ids: list[tuple[str, str, str, str]] = []
        carried_any: dict[str, set[tuple[str, str]]] = {}
        for i, e in enumerate(ents or []):
            if not isinstance(e, dict):
                continue
            path, where = ("entities", "entities", i), self.where(("entities", "entities", i))
            if isinstance(e.get("id"), str):
                all_entity_ids.add(e["id"])
            eid = self.string(e, "id", path)
            if eid is not None:
                entity_ids.setdefault(eid, e)
            parent = self.string(e, "parent", path)
            if parent is not None and eid is not None:
                parents.append((eid, parent, f"{where}.parent"))
            summary = self.string(e, "summary", path)
            if summary is not None and word_count(summary) > SUMMARY_MAX_WORDS:
                self.add("summary-too-long", f"{where}.summary",
                         f"{word_count(summary)} words (limit {SUMMARY_MAX_WORDS})")
            self.refs(e, "evidence", path, f"{where}.evidence")
            xids = e.get("externalIds")
            if isinstance(xids, dict) and isinstance(e.get("id"), str):
                for db, val in xids.items():
                    vals = val if isinstance(val, list) else [val]
                    for j, item in enumerate(vals):
                        if not isinstance(item, str):
                            continue
                        carried_any.setdefault(e["id"], set()).add((db, item))
                        ipath = path + ("externalIds", db) + ((j,) if isinstance(val, list) else ())
                        if (eid is not None and self.clean(ipath)
                                and self.clean(path + ("externalIds", db))):
                            ext_ids.append((eid, db, item, f"{where}.externalIds.{db}"))
        quantities: list[tuple[dict[str, Any], tuple, str]] = []
        endpoints: list[tuple[str, str]] = []
        for i, r in enumerate(rels or []):
            if not isinstance(r, dict):
                continue
            path, where = ("relations", "relations", i), self.where(("relations", "relations", i))
            ends = {}
            for end in ("from", "to"):
                v = self.string(r, end, path)
                if v is not None:
                    ends[end] = v
                    endpoints.append((v, f"{where}.{end}"))
            if len(ends) == 2 and ends["from"] == ends["to"]:
                self.add("self-relation", f"{where}.to",
                         f"relation goes from {ends['from']!r} to itself")
            self.refs(r, "evidence", path, f"{where}.evidence")
            self.conflicts(r, path, where)
            q = r.get("quantity")
            if isinstance(q, dict):
                qpath, qwhere = path + ("quantity",), f"{where}.quantity"
                quantities.append((q, qpath, qwhere))
                self.refs(q, "evidence", qpath, f"{qwhere}.evidence")
                self.conflicts(q, qpath, qwhere)
                rng = self.range_pair(q, qpath, qwhere)
                value = q.get("value")
                if (rng is not None and _is_number(value) and self.clean(qpath + ("value",))
                        and not rng[0] <= value <= rng[1]):
                    self.add("value-outside-range", f"{qwhere}.value",
                             f"value {value} lies outside its range [{rng[0]}, {rng[1]}]")
        evidence: dict[str, dict[str, Any]] = {}
        all_evidence_ids: set[str] = set()
        for i, v in enumerate(evs or []):
            if not isinstance(v, dict):
                continue
            path, where = ("evidence", "evidence", i), self.where(("evidence", "evidence", i))
            if isinstance(v.get("id"), str):
                all_evidence_ids.add(v["id"])
            vid = self.string(v, "id", path)
            if vid is not None:
                evidence.setdefault(vid, v)
            quote = self.string(v, "quote", path)
            if quote is not None and word_count(quote) > QUOTE_MAX_WORDS:
                self.add("quote-too-long", f"{where}.quote",
                         f"{word_count(quote)} words (limit {QUOTE_MAX_WORDS})")
            self.evidence_grading(v, path, where)
            kind, url = self.string(v, "kind", path), self.string(v, "url", path)
            if kind == "doi" and url is not None:
                host = (urlsplit(url).hostname or "").lower()
                if host != "doi.org" and not host.endswith(".doi.org"):
                    self.add("doi-url", f"{where}.url",
                             f"kind is 'doi' but the url {url!r} is not on doi.org")

        for fname, _key, items in ((DATA_FILES["entities"], "entities", ents),
                                  (DATA_FILES["relations"], "relations", rels),
                                  (DATA_FILES["evidence"], "evidence", evs)):
            ids = Counter(r["id"] for r in items or []
                          if isinstance(r, dict) and isinstance(r.get("id"), str))
            for rid, n in ids.items():
                if n > 1:
                    self.add("duplicate-id", f"{fname}[{rid}]", f"id appears {n} times in {fname}")
        if ents is not None:
            for ref, where in endpoints:
                if ref not in all_entity_ids:
                    self.add("dangling-endpoint", where,
                             f"{ref!r} is not an entities.json record")
            self.hierarchy(entity_ids, all_entity_ids, parents)

        params_ok, params_cited = self.params(evidence, quantities)
        if evs is not None:
            for ref, where in self.ev_refs:
                if ref not in all_evidence_ids:
                    self.add("dangling-evidence", where, f"{ref!r} is not an evidence.json record")
            cited = {ref for ref, _ in self.ev_refs} | params_cited
            self.cited_evidence(evidence, cited)
            if params_ok and ents is not None and rels is not None:
                for i, v in enumerate(evs):
                    if not isinstance(v, dict):
                        continue
                    vid = self.string(v, "id", ("evidence", "evidence", i))
                    if vid is not None and vid not in cited:
                        self.add("unused-evidence", self.where(("evidence", "evidence", i)),
                                 "cited by no entity, relation, quantity, conflict or params.json "
                                 "row (engine validation scenarios are not scanned)")
            for q, qpath, qwhere in quantities:
                grade = self.string(q, "grade", qpath)
                refs = q.get("evidence") if self.clean(qpath + ("evidence",)) else None
                self.ceiling(grade, refs, evidence, qwhere)
        if ents is not None:
            self.verified_ids(entity_ids, all_entity_ids, ext_ids, carried_any)
        self.meta_consistency()
        self.assumption_share(quantities)

    def conflicts(self, obj: dict[str, Any], path: tuple, where: str) -> None:
        cs = obj.get("conflicts")
        if not isinstance(cs, list):
            return
        for i, c in enumerate(cs):
            if not isinstance(c, dict):
                continue
            cpath, cwhere = path + ("conflicts", i), f"{where}.conflicts[{i}]"
            cited = self.refs(c, "evidence", cpath, f"{cwhere}.evidence")
            self.range_pair(c, cpath, cwhere)
            note = self.string(c, "note", cpath)
            if (cited is not None and not c["evidence"] and note is not None
                    and not _says_unsourced(note)):
                self.add("conflict-unsourced-note", f"{cwhere}.note",
                         "conflict cites no evidence, so a sentence or clause of its note must "
                         "begin with 'Unsourced' or 'No verified source'; note reads "
                         f"{note[:80]!r}")

    def evidence_grading(self, v: dict[str, Any], path: tuple, where: str) -> None:
        missing = [k for k in ("sourceType", "defaultGrade") if k not in v]
        if missing:
            self.add("evidence-grading-missing", where,
                     f"no {' or '.join(missing)}: every evidence record carries both (HREQ-E-02)")
            return
        source, grade = self.string(v, "sourceType", path), self.string(v, "defaultGrade", path)
        if source in DEFAULT_GRADE_BY_SOURCE and grade is not None:
            want = DEFAULT_GRADE_BY_SOURCE[source]
            if grade != want:
                self.add("default-grade-mapping", f"{where}.defaultGrade",
                         f"sourceType {source!r} maps to defaultGrade {want!r} (docs/health/01 "
                         f"§3.2), not {grade!r}")

    def cited_evidence(self, evidence: dict[str, dict[str, Any]], cited: set[str]) -> None:
        for vid in sorted(cited & set(evidence)):
            v = evidence[vid]
            where = f"{DATA_FILES['evidence']}[{vid}]"
            ver = v.get("verification", _MISSING)
            if ver is _MISSING:
                self.add("evidence-unverified", where, "cited, but carries no verification "
                         "record (method, checkedOn, retraction; HREQ-D-02)")
                continue
            if not isinstance(ver, dict):
                continue
            retraction = ver.get("retraction")
            if isinstance(retraction, str) and not _RETRACTION_CLEAR.match(retraction):
                self.add("evidence-retracted", f"{where}.verification.retraction",
                         f"cited, but its retraction check reads {retraction[:80]!r} (expected "
                         "'none ...' or 'not applicable ...'; a retracted source is an S0b)")
            if ver.get("titleMatch") is False:
                self.add("evidence-title-mismatch", f"{where}.verification.titleMatch",
                         "cited, but its verification found the title does not match the "
                         "registry's (titleMatch: false)")

    def ceiling(self, grade: str | None, refs: Any, evidence: dict[str, dict[str, Any]],
                where: str) -> None:
        if grade not in GRADES or not isinstance(refs, list) or not refs:
            return
        best: int | None = None
        for ref in refs:
            v = evidence.get(ref) if isinstance(ref, str) else None
            dg = v.get("defaultGrade") if v is not None else None
            if dg not in GRADES:
                return                       # unresolved or ungraded source: other rules report it
            best = GRADES.index(dg) if best is None else min(best, GRADES.index(dg))
        if best is not None and GRADES.index(grade) < best:
            self.add("grade-ceiling", f"{where}.grade",
                     f"graded {grade}, above the best defaultGrade of its evidence "
                     f"({GRADES[best]}; {', '.join(map(str, refs))}): a number is never graded "
                     "above its source (HREQ-E-03)")

    def hierarchy(self, entities: dict[str, dict[str, Any]], all_ids: set[str],
                  parents: list[tuple[str, str, str]]) -> None:
        links: dict[str, str] = {}
        for child, parent, where in parents:
            if parent not in all_ids:
                self.add("dangling-parent", where, f"{parent!r} is not an entities.json record")
                continue
            if parent not in entities:
                continue
            links.setdefault(child, parent)
            cs, ps = entities[child].get("scale"), entities[parent].get("scale")
            if _is_integer(cs) and _is_integer(ps) and ps > cs:
                self.add("parent-scale", where,
                         f"parent {parent!r} has scale {ps} > this entity's scale {cs}")
        state: dict[str, int] = {}                 # 1 = on the current walk, 2 = finished
        for start in links:
            if start in state:
                continue
            walk: list[str] = []
            node: str | None = start
            while node is not None and node not in state:
                state[node] = 1
                walk.append(node)
                node = links.get(node)
            if node is not None and state[node] == 1:
                cycle = walk[walk.index(node):]
                k = cycle.index(min(cycle))
                cycle = cycle[k:] + cycle[:k]
                self.add("parent-cycle", f"{DATA_FILES['entities']}[{cycle[0]}].parent",
                         "parent chain loops: " + " -> ".join(cycle + [cycle[0]]))
            for n in walk:
                state[n] = 2

    def params(self, evidence: dict[str, dict[str, Any]],
               quantities: list[tuple[dict[str, Any], tuple, str]]) -> tuple[bool, set[str]]:
        """Engine parameters: mirrors, HREQ-E-01 rows, params-mirror. Returns (usable, cited)."""
        params = self.kb.get("params")
        errors = self.kb.get("load_errors") if isinstance(self.kb.get("load_errors"), dict) else {}
        paths = self.kb.get("paths") if isinstance(self.kb.get("paths"), dict) else {}
        mirrors = [(q["engineParam"], q, qwhere) for q, qpath, qwhere in quantities
                   if self.string(q, "engineParam", qpath) is not None]
        cited: set[str] = set()
        if not isinstance(params, dict):
            if errors.get("params"):
                why = str(errors["params"])
            elif params is None and not paths.get("params"):
                why = "no params.json found"
            else:
                why = f"params.json is {_jtype(params)}, not an object of rows"
            names = ", ".join(sorted({p for p, _, _ in mirrors})) or "none"
            self.add("engine-params-unavailable", "params.json",
                     f"{why}. Not checked: {len(mirrors)} engine mirrors ({names}), the "
                     "params.json rows (HREQ-E-01, grade ceiling), params-mirror and "
                     "unused-evidence")
            return False, cited
        evidence_ids = set(evidence)
        for name, row in params.items():
            where = f"params.json[{name}]"
            if not isinstance(row, dict):
                self.add("param-field", where, f"expected an object, got {_jtype(row)}")
                continue
            value = row.get("value", _MISSING)
            if not _is_number(value):
                self.add("param-field", f"{where}.value", f"expected a number, got {_show(value)}")
            unit = row.get("unit", _MISSING)
            if not (isinstance(unit, str) and unit.strip()):
                self.add("param-field", f"{where}.unit",
                         f"expected a non-empty string, got {_show(unit)}")
            rng = row.get("range", _MISSING)
            if not (isinstance(rng, list) and len(rng) == 2 and all(_is_number(x) for x in rng)):
                self.add("param-field", f"{where}.range",
                         f"expected [low, high] (two numbers), got {_show(rng)}")
            elif rng[0] > rng[1]:
                self.add("range-order", f"{where}.range", f"low end {rng[0]} exceeds high end "
                                                          f"{rng[1]}")
            elif _is_number(value) and not rng[0] <= value <= rng[1]:
                self.add("value-outside-range", f"{where}.value",
                         f"value {value} lies outside its range [{rng[0]}, {rng[1]}]")
            refs = row.get("evidence", _MISSING)
            if not (isinstance(refs, list) and refs and all(isinstance(x, str) for x in refs)):
                self.add("param-field", f"{where}.evidence",
                         f"expected a non-empty array of evidence ids, got {_show(refs)}")
                refs = None
            else:
                cited.update(refs)
                for ref in refs:
                    if ref not in evidence_ids:
                        self.add("param-evidence-unresolved", f"{where}.evidence",
                                 f"{ref!r} is not an evidence.json record")
            grade = row.get("grade", _MISSING)
            if grade not in GRADES:
                self.add("param-field", f"{where}.grade",
                         f"expected one of {', '.join(GRADES)}, got {_show(grade)}")
            elif refs is not None:
                self.ceiling(grade, refs, evidence, where)
        for name, q, qwhere in mirrors:
            row = params.get(name)
            if not isinstance(row, dict):
                self.add("engine-param-unknown", f"{qwhere}.engineParam",
                         f"{name!r} is not a params.json row")
                continue
            diffs = []
            for field in ("value", "range", "unit", "grade"):
                qv, pv = q.get(field, _MISSING), row.get(field, _MISSING)
                if not _same(qv, pv):
                    diffs.append(f"{field} {_show(qv)} != params {_show(pv)}")
            if diffs:
                self.add("engine-param-mismatch", f"{qwhere}.engineParam",
                         f"mirrors params.json[{name!r}] but " + "; ".join(diffs))
        self.params_mirror(params, errors, paths)
        return True, cited

    def params_mirror(self, params: dict[str, Any], errors: dict[str, Any],
                      paths: dict[str, Any]) -> None:
        if errors.get("params_js"):
            self.add("params-mirror", "params.data.js", str(errors["params_js"]))
            return
        js = self.kb.get("params_js")
        if js is None:
            self.add("params-mirror-unchecked", "params.data.js",
                     f"none found beside {paths.get('params') or 'params.json'} (or in the "
                     "reference checkout); params-mirror not checked")
            return
        if not isinstance(js, dict):
            self.add("params-mirror", "params.data.js",
                     f"exports a {_jtype(js)}, not the params.json object")
            return
        for name in sorted(set(params) | set(js), key=str):
            where = f"params.data.js[{name}]"
            if name not in js:
                self.add("params-mirror", where, "row is in params.json but not in "
                         "params.data.js; regenerate the mirror")
            elif name not in params:
                self.add("params-mirror", where, "row is in params.data.js but not in "
                         "params.json")
            elif _jkey(js[name]) != _jkey(params[name]):
                fields = sorted({k for k in set(js[name]) | set(params[name])
                                 if _jkey(js[name].get(k, _MISSING)) !=
                                 _jkey(params[name].get(k, _MISSING))}
                                if isinstance(js[name], dict) and isinstance(params[name], dict)
                                else ["(row)"], key=str)
                self.add("params-mirror", where, f"differs from params.json in {', '.join(fields)}"
                                                 "; regenerate the mirror")

    def verified_ids(self, entities: dict[str, dict[str, Any]], all_ids: set[str],
                     ext_ids: list[tuple[str, str, str, str]],
                     carried: dict[str, set[tuple[str, str]]]) -> None:
        log = self.lists.get("verification")
        if log is None:
            return
        latest: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
        earlier_true: set[tuple[str, str, str]] = set()
        removed: dict[str, list[int]] = {}
        for i, r in enumerate(log):
            if not self.clean(("verification", "records", i)) or not isinstance(r, dict):
                continue
            key = (r["entity"], r["db"], r["id"])
            prev = latest.get(key)
            if prev is not None and prev[1]["resolved"] is True:
                earlier_true.add(key)
            latest[key] = (i, r)
            if r["entity"] not in all_ids:
                removed.setdefault(r["entity"], []).append(i)
        log_name = DATA_FILES["verification"]
        for eid, db, item, where in ext_ids:
            got = latest.get((eid, db, item))
            if got is None:
                self.add("external-id-unverified", where,
                         f"{db} {item!r} for {eid} has no VERIFICATION_LOG.json record")
            elif got[1]["resolved"] is not True:
                i, r = got
                was = ("; an earlier record said resolved: true, and the log is append-only, "
                       "so the latest record governs" if (eid, db, item) in earlier_true else "")
                self.add("external-id-unverified", where,
                         f"{db} {item!r} for {eid}: the latest VERIFICATION_LOG.json record "
                         f"(#{i}, checkedOn {r.get('checkedOn', '?')}) says resolved: false{was}")
        for eid, idx in removed.items():
            self.add("entity-removed", f"{log_name}[#{idx[0]}]",
                     f"the append-only log holds {len(idx)} record(s) for {eid!r}, which "
                     "entities.json no longer holds: a KB record was deleted (HREQ-E-06)")
        for (eid, db, item), (i, r) in latest.items():
            if r["resolved"] is True and eid in all_ids and (db, item) not in carried.get(eid, ()):
                self.add("orphan-verification", f"{log_name}[#{i}]",
                         f"resolved record for {eid} {db} {item!r}, which that entity no "
                         "longer carries")

    def meta_consistency(self) -> None:
        for field in META_FIELDS:
            seen: dict[str, str] = {}
            for key in ("entities", "relations", "evidence"):
                doc = self.kb.get(key)
                if isinstance(doc, dict) and isinstance(doc.get(field), str):
                    seen[DATA_FILES[key]] = doc[field]
            if len(set(seen.values())) > 1:
                self.add("meta-mismatch", f"{'/'.join(seen)}:{field}",
                         "files disagree: " + ", ".join(f"{f}={v!r}" for f, v in seen.items()))

    def assumption_share(self, quantities: list[tuple[dict[str, Any], tuple, str]]) -> None:
        total = len(quantities)
        if total:
            n = sum(1 for q, _, _ in quantities if q.get("grade") == "E-assumption")
            self.add("e-assumption-share", f"{DATA_FILES['relations']} quantities",
                     f"{n} of {total} quantities ({100.0 * n / total:.1f}%) are graded "
                     "E-assumption")
        params = self.kb.get("params")
        if isinstance(params, dict) and params:
            rows = [r for r in params.values() if isinstance(r, dict)]
            n = sum(1 for r in rows if r.get("grade") == "E-assumption")
            if rows:
                self.add("e-assumption-share", "params.json",
                         f"{n} of {len(rows)} engine params ({100.0 * n / len(rows):.1f}%) are "
                         "graded E-assumption")

    # -- driver ------------------------------------------------------------------------------
    def run(self) -> list[Finding]:
        self.schema_layer()
        self.cross_file()
        return self.findings


def check_kb(kb: dict[str, Any]) -> list[Finding]:
    """Every contract rule over a loaded KB. Never raises on malformed data."""
    return _Checker(kb).run()


def counts_by_severity(findings: list[Finding]) -> dict[str, int]:
    c = Counter(f.severity for f in findings)
    return {s: c.get(s, 0) for s in SEVERITIES}


__all__ = ["DEFAULT_GRADE_BY_SOURCE", "DEFERRED_RULES", "DOCUMENTED_RULES", "GRADES",
           "PatternError", "RULES", "SEVERITIES", "Finding", "check_kb", "counts_by_severity",
           "ecma_to_python", "matches", "word_count"]
