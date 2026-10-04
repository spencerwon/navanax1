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
   Two more documents are validated when the schema declares their definitions (the M1
   extensions): params.json against #/definitions/EngineParams (after the cross-file
   parameter rules, which keep the fields they read: PARAM_RULE_FIELDS) and
   VERIFICATION_LOG.json's curationRecords against #/definitions/CurationRecord.

2. CROSS-FILE. What JSON Schema cannot say (the schema's own description; docs/health/03
   §6; docs/health/01 HREQ-E-01..E-07, E-10, U-01): references resolve, parent scale <=
   child scale, engine mirrors, params.data.js == params.json, verified external
   identifiers (the LATEST log record for an entity/db/id governs: the log is
   append-only), word limits, unsourced conflicts say so, the grade ceiling, ranges hold
   their values, every params.json row Monte Carlo holds fixed records why (fixed-reason),
   calibratedAgainst agrees with the expectations' calibrates (calibration-link), and an
   evidence record marked engineOnly is cited by exactly the expectations its citedBy
   lists and by no knowledge-base record (engine-only-evidence; the marker is what exempts
   it from unused-evidence).

   calibration-link and engine-only-evidence read the expectation registry from the
   reference engine's scenarios.js AS TEXT (`expectation_records`): the knowledge base
   depends on the reference, never on the Python engine, so it does not import
   health.engine. The text comes from `kb["scenarios_js"]` when the caller supplies it
   (tests plant it there), else from scenarios.js beside the params.json in use, else from
   the reference checkout. A registry it cannot find, read or find an expectation id in is
   an error (expectations-unavailable), never a quiet pass.

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
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, NamedTuple
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
    "date-pattern": ("error", "accessed / checkedOn (or any definitions/isoDate) is not "
                              "YYYY-MM-DD"),
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
    "fixed-reason": ("error", "a params.json row Monte Carlo holds fixed lacks mc: false with a "
                              "fixedReason of scenario-condition, classification-threshold or "
                              "index-constant (HREQ-U-01): mc: false with no or another reason; "
                              "a single-point range without mc: false; a sampled row carrying "
                              "a reason; an mc that is not a boolean"),
    "calibration-link": ("error", "params.json calibratedAgainst and the scenarios.js "
                                  "expectations' calibrates disagree, an entry is not an "
                                  "expectation id <scenario_id>/<NN> or names no registered "
                                  "expectation, a row whose notes say its value was "
                                  "calibrated, co-developed, tuned or chosen to hit a target "
                                  "carries no calibratedAgainst, or scenarios.js registers an "
                                  "expectation id twice (HREQ-E-10)"),
    "expectations-unavailable": ("error", "scenarios.js was not found, cannot be read, or "
                                          "registers no expectation id, so calibratedAgainst "
                                          "and engineOnly.citedBy cannot be resolved and "
                                          "calibration-link checks only the params.json side"),
    "engine-only-evidence": ("error", "an evidence record marked engineOnly is cited by a "
                                      "knowledge-base record or a params.json row, or its "
                                      "citedBy disagrees with the scenarios.js expectations "
                                      "that cite it (an id no expectation registers, an "
                                      "expectation that does not cite it, one that does and "
                                      "is not listed)"),
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
                                "engine param cites, and that is not marked engineOnly"),
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
    "fixed-reason": ("fixed-reason",),
    "calibration-link": ("calibration-link", "expectations-unavailable"),
    "append-only": ("entity-removed",),          # partial; the rest is DEFERRED below
}
"""docs/health/03 §6 rule name -> the RULES codes that implement it (all `error`)."""

DEFERRED_RULES: dict[str, str] = {
    "range-kind": "needs a structured range-kind field on every sampled parameter (HREQ-E-08; "
                  "M1, tracker W-18); today the kind is prose in `notes`",
    "dispersion": "needs structured dispersion type and n fields (HREQ-E-09; M1, tracker W-18)",
    "append-only": "needs the last released snapshot of the KB and evidence ledger to compare "
                   "against, and a `supersedes` field the schema does not have (HREQ-E-06, "
                   "HREQ-V-18); until then only entity-removed runs",
}
"""docs/health/03 §6 rules not (fully) enforced yet, with what each is waiting for."""

GRADES = ("A-meta", "A-primary", "B-textbook", "C-model", "D-animal", "E-assumption")
"""Best first. The ranking grade-ceiling and `report`'s 'graded >= B' use."""

FIXED_REASONS = ("scenario-condition", "classification-threshold", "index-constant")
"""docs/health/01 §4.4, HREQ-U-01: the only reasons a params.json row may be held fixed in
Monte Carlo, as its `fixedReason` field spells them (rule fixed-reason)."""

EXPECTATION_ID_PATTERN = r"^[A-Za-z0-9_]+/[0-9]{2}$"
"""`<scenario_id>/<NN>` (docs/health/01 §6.3, HREQ-E-13): what a calibratedAgainst entry
names and what scenarios.js gives each expectation record as its `id`."""

PARAM_RULE_FIELDS = ("value", "unit", "range", "evidence", "grade", "mc", "fixedReason",
                     "calibratedAgainst")
"""The params.json row fields the cross-file rules read (param-field, range-contains-value,
ref-resolves, grade-ceiling, fixed-reason, calibration-link). When schema.json declares
#/definitions/EngineParams the table is validated against it too, and a schema violation
in one of these fields is left to the rule that already reported the field (one finding per
malformed value)."""

SCENARIOS_JS_FILE = "scenarios.js"
_REFERENCE_SCENARIOS_JS = (Path(__file__).resolve().parents[3] / "reference" / "metabolic-map-v1"
                           / "engine" / SCENARIOS_JS_FILE)

_CALIBRATION_NOTE = re.compile(
    r"\b(?:calibrat\w*|co-?developed|tuned|chosen\s+(?:so|to|for|with))\b", re.I)
"""Wording in a params.json row's notes that says its value was set to make an output hit a
target. Such a row must carry calibratedAgainst (HREQ-E-10). Deliberately broad: a note
that only mentions calibration must be reworded or linked, never silently passed."""

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

_OPTIONAL_RULE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("/definitions/Param/properties", PARAM_RULE_FIELDS),
    ("/definitions/Evidence/properties/engineOnly/properties", ("citedBy",)),
)
"""The same, for nodes a schema may declare (the M1 extensions): checked when the node that
holds the properties (Param, engineOnly) is declared at all."""

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
    ("#/definitions/isoDate/", "date-pattern"),
)
"""Where a failing `pattern` sits in the schema -> the finding's code (naming only)."""

_REF_CODES: dict[str, tuple[str, str]] = {
    "#/definitions/range": ("range-shape", "expected [low, high] (two numbers)"),
}
"""A $ref target whose violations are reported as ONE finding at the referring value."""

_CONDITIONAL_REQUIRED = {"doi": "doi-required", "pmid": "pmid-required"}

_PARAM_WHERE = re.compile(r"^params\.json\[([^\]]*)\](?:\.([A-Za-z_$][\w$]*))?")
"""A finding's `where` on a params.json row: the row name and the field, if any."""

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
# scenarios.js, read as text (calibration-link)
# ---------------------------------------------------------------------------
class ScriptError(ValueError):
    """scenarios.js text the expectation scanner cannot read: an unterminated string,
    template literal or comment, or unbalanced brackets."""


_JS_PUNCT = "{}[](),:;"
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_JS_KEY = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_EXPR = object()
"""A value the scanner does not evaluate (an identifier, a call, a template literal with a
substitution, an array holding anything but string literals)."""


def _js_line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _js_string(text: str, i: int) -> tuple[str, int]:
    """The string literal opening at text[i] (' or "): its value and the index after it."""
    quote, j, n, buf = text[i], i + 1, len(text), []
    while j < n:
        ch = text[j]
        if ch == quote:
            return "".join(buf), j + 1
        if ch == "\n":
            break
        if ch == "\\" and j + 1 < n:
            nxt = text[j + 1]
            j += 2
            if nxt in "ux":
                width = 4 if nxt == "u" else 2
                digits = text[j:j + width]
                if nxt == "u" and text.startswith("{", j):
                    end = text.find("}", j)
                    digits, width = (text[j + 1:end], end - j + 1) if end > 0 else ("", 0)
                try:
                    buf.append(chr(int(digits, 16)))
                except ValueError as exc:
                    raise ScriptError(f"bad \\{nxt} escape at line {_js_line(text, j)}") from exc
                j += width
            elif nxt == "\r" and text.startswith("\n", j):
                j += 1                                   # line continuation (CRLF)
            elif nxt != "\n":                            # "\<newline>" continues the line
                buf.append(_JS_ESCAPES.get(nxt, nxt))
            continue
        buf.append(ch)
        j += 1
    raise ScriptError(f"unterminated string literal at line {_js_line(text, i)}")


def _js_skip_code(text: str, i: int) -> int:
    """Skip a template substitution's code from text[i] to its closing }; the index after."""
    depth, n = 1, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            i = _js_string(text, i)[1]
        elif ch == "`":
            i = _js_template(text, i)[1]
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                break
            i = end + 2
        else:
            depth += {"{": 1, "}": -1}.get(ch, 0)
            i += 1
            if depth == 0:
                return i
    raise ScriptError(f"unterminated template substitution at line {_js_line(text, i)}")


def _js_template(text: str, i: int) -> tuple[str | None, int]:
    """The template literal opening at text[i]: its text (None when it has a ${...}
    substitution, whose value the scanner does not compute) and the index after it."""
    j, n, buf, substituted = i + 1, len(text), [], False
    while j < n:
        ch = text[j]
        if ch == "`":
            return (None if substituted else "".join(buf)), j + 1
        if ch == "\\" and j + 1 < n:
            buf.append(_JS_ESCAPES.get(text[j + 1], text[j + 1]))
            j += 2
        elif text.startswith("${", j):
            substituted = True
            j = _js_skip_code(text, j + 2)
        else:
            buf.append(ch)
            j += 1
    raise ScriptError(f"unterminated template literal at line {_js_line(text, i)}")


def js_tokens(text: str) -> list[tuple[str, Any]]:
    """The tokens of JavaScript source that object literals are made of: ("str", value) for a
    string or substitution-free template literal, ("expr", None) for a template with a
    substitution, ("p", c) for each of {}[](),:; and ("w", run) for any other run of
    characters. Comments and whitespace are dropped. A `/` is read as division: regular
    expression literals are not recognised (scenarios.js has none)."""
    out: list[tuple[str, Any]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ScriptError(f"unterminated comment at line {_js_line(text, i)}")
            i = end + 2
        elif ch in "'\"":
            value, i = _js_string(text, i)
            out.append(("str", value))
        elif ch == "`":
            tvalue, i = _js_template(text, i)
            out.append(("str", tvalue) if tvalue is not None else ("expr", None))
        elif ch in _JS_PUNCT:
            out.append(("p", ch))
            i += 1
        else:
            j = i + 1
            while j < n and not (text[j].isspace() or text[j] in _JS_PUNCT or text[j] in "'\"`"
                                 or text.startswith("//", j) or text.startswith("/*", j)):
                j += 1
            out.append(("w", text[i:j]))
            i = j
    return out


def _js_key(toks: list[tuple[str, Any]], k: int) -> str | None:
    """The property name at toks[k] when toks[k] is one (a name or string literal right after
    `{` or `,`, followed by `:`), else None."""
    if k < 1 or toks[k - 1] not in (("p", "{"), ("p", ",")):
        return None
    kind, value = toks[k]
    if kind == "str" or (kind == "w" and _JS_KEY.match(value)):
        return value
    return None


class Expectation(NamedTuple):
    """One expectation record of scenarios.js as the scanner reads it. `calibrates` and
    `evidence` are tuples of strings for a literal array of string literals, None when the
    record has no such key, and _EXPR for any other value; `role` is the string, None or
    _EXPR."""
    id: str
    calibrates: Any
    role: Any
    evidence: Any


@lru_cache(maxsize=8)
def expectation_records(text: str) -> tuple[Expectation, ...]:
    """Every object literal in scenarios.js whose `id` is a string matching
    EXPECTATION_ID_PATTERN, in source order, as an Expectation (id, calibrates, role,
    evidence).

    The scan is structural, not a JavaScript evaluator: it tracks {}, [] and () and reads
    `name: 'string'` and `name: ['a', 'b']` inside object literals, which is how the
    reference writes its expectation records. Raises ScriptError on text it cannot
    tokenise or whose brackets do not balance."""
    toks = js_tokens(text)
    closer = {"}": "{", "]": "[", ")": "("}
    stack: list[dict[str, Any]] = []
    found: list[Expectation] = []
    for k, (kind, value) in enumerate(toks):
        top = stack[-1] if stack else None
        if kind == "p" and value in "{[(":
            if top is not None and top["kind"] == "[":
                top["literal"] = False
            frame: dict[str, Any] = {"kind": value, "keys": {}, "items": [], "literal": True,
                                     "owner": None}
            if value == "[" and top is not None and top["kind"] == "{" and k >= 2 \
                    and toks[k - 1] == ("p", ":") and _js_key(toks, k - 2) is not None:
                frame["owner"] = (top, _js_key(toks, k - 2))
            stack.append(frame)
            continue
        if kind == "p" and value in closer:
            if top is None or top["kind"] != closer[value]:
                raise ScriptError(f"unbalanced {value!r} (token {k})")
            stack.pop()
            if value == "]" and top["owner"] is not None:
                owner, key = top["owner"]
                owner["keys"][key] = tuple(top["items"]) if top["literal"] else _EXPR
            elif value == "}":
                eid = top["keys"].get("id")
                if isinstance(eid, str) and matches(EXPECTATION_ID_PATTERN, eid):
                    keys = top["keys"]
                    found.append(Expectation(eid, keys.get("calibrates"), keys.get("role"),
                                             keys.get("evidence")))
            continue
        if top is not None and top["kind"] == "[":
            if kind == "str":
                top["items"].append(value)
            elif (kind, value) != ("p", ","):
                top["literal"] = False
        if (kind, value) == ("p", ":") and top is not None and top["kind"] == "{":
            key = _js_key(toks, k - 1)
            if key is None:
                continue
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            after = toks[k + 2] if k + 2 < len(toks) else None
            if nxt is not None and nxt[0] == "str" and after in (("p", ","), ("p", "}")):
                top["keys"][key] = nxt[1]
            elif nxt != ("p", "["):
                top["keys"][key] = _EXPR             # an array is recorded when it closes
    if stack:
        raise ScriptError(f"{len(stack)} unclosed bracket(s) at the end of the text")
    return tuple(found)


def scenarios_js_path(params_path: Any = None) -> Path | None:
    """Where calibration-link reads the expectation registry from when the caller supplies
    no text: scenarios.js beside the params.json in use, else the reference checkout."""
    candidates = []
    if isinstance(params_path, str) and params_path:
        candidates.append(Path(params_path).with_name(SCENARIOS_JS_FILE))
    candidates.append(_REFERENCE_SCENARIOS_JS)
    for path in candidates:
        try:
            if path.is_file():
                return path
        except (OSError, ValueError):
            continue
    return None


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
        self.schema: _Schema | None = None           # the linted schema, once schema_layer ran
        self._exps: Any = _MISSING                   # expectations(): computed once per run

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
            schema = self.schema = _Schema(schema_doc)
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
        # Optional nodes: a schema that declares the params.json fields must agree with the
        # rules that enforce them (fixed-reason, calibration-link).
        reasons = _pointer(schema, "/definitions/fixedReason/enum")
        if reasons is not _MISSING and (not isinstance(reasons, list)
                                        or set(map(str, reasons)) != set(FIXED_REASONS)):
            self.add("schema-drift", f"{fname}#/definitions/fixedReason/enum",
                     f"fixed-reason permits {list(FIXED_REASONS)} (HREQ-U-01) but the schema's "
                     f"fixedReason enum is {_show(reasons)}; change HREQ-U-01 and "
                     "health/kb/check.py FIXED_REASONS with it")
        pattern = _pointer(schema, "/definitions/expectationId/pattern")
        if pattern is not _MISSING and pattern != EXPECTATION_ID_PATTERN:
            self.add("schema-drift", f"{fname}#/definitions/expectationId/pattern",
                     f"calibration-link reads expectation ids as {EXPECTATION_ID_PATTERN} but "
                     f"the schema's expectationId pattern is {_show(pattern)}; change "
                     "health/kb/check.py EXPECTATION_ID_PATTERN with it")
        optional = [(ptr, fields) for ptr, fields in _OPTIONAL_RULE_FIELDS
                    if _pointer(schema, ptr.rsplit("/", 1)[0]) is not _MISSING]
        for ptr, fields in (*_RULE_FIELDS, *optional):
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
        self.curation_records(doc)

    def curation_records(self, doc: dict[str, Any]) -> None:
        """VERIFICATION_LOG.json `curationRecords` (checks, fetch attempts and changes that
        are not external identifiers), validated against #/definitions/CurationRecord when
        the schema declares it. The external-identifier `records` keep their own rules."""
        items = doc.get("curationRecords", _MISSING)
        if items is _MISSING:
            return
        fname = DATA_FILES["verification"]
        if not isinstance(items, list):
            self.add("file-shape", f"{fname}.curationRecords",
                     f"expected an array, got {_jtype(items)}")
            return
        ref = "#/definitions/CurationRecord"
        if self.schema is None or self.schema.ref(ref)[0] is _MISSING:
            return
        for i, r in enumerate(items):
            out: list = []
            self.schema.validate(r, {"$ref": ref}, "#", ("verification", "curationRecords", i),
                                 out)
            for path, _ptr, _kw, code, message, _extra in out:
                self.bad.add(path)
                self.add(code, self.where(path), message)

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
            self.engine_only(evs, params_cited)
            if params_ok and ents is not None and rels is not None:
                for i, v in enumerate(evs):
                    if not isinstance(v, dict) or "engineOnly" in v:
                        continue                   # engine-only-evidence holds the claim to account
                    vid = self.string(v, "id", ("evidence", "evidence", i))
                    if vid is not None and vid not in cited:
                        self.add("unused-evidence", self.where(("evidence", "evidence", i)),
                                 "cited by no entity, relation, quantity, conflict or params.json "
                                 "row, and not marked engineOnly (a source only the engine's "
                                 "scenario expectations cite says so in engineOnly.citedBy)")
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

    def engine_only(self, evs: list[Any], params_cited: set[str]) -> None:
        """Evidence.engineOnly (tracker W-6): a source cited by the engine's scenario
        expectations (or only its bibliography) and by no knowledge-base record. The marker
        exempts the record from unused-evidence, so the claim is checked: no knowledge-base
        record or params.json row cites it, and citedBy is exactly the set of expectations
        scenarios.js registers that cite it (an empty list: none does). A malformed marker is
        the schema layer's to report, once."""
        kb_cites: dict[str, list[str]] = {}
        for ref, where in self.ev_refs:
            kb_cites.setdefault(ref, []).append(where)
        for i, v in enumerate(evs):
            if not isinstance(v, dict) or not isinstance(v.get("engineOnly"), dict):
                continue
            path = ("evidence", "evidence", i)
            vid = self.string(v, "id", path)
            marker = v["engineOnly"]
            if vid is None or not self.clean(path + ("engineOnly",)):
                continue
            where = f"{DATA_FILES['evidence']}[{vid}].engineOnly"
            citing = kb_cites.get(vid, []) + (["params.json"] if vid in params_cited else [])
            if citing:
                shown = ", ".join(citing[:3]) + (" ..." if len(citing) > 3 else "")
                self.add("engine-only-evidence", where,
                         f"marked engine-only, but cited by {shown}: a source a knowledge-base "
                         "record or a parameter cites is not engine-only; drop the marker")
            listed = marker.get("citedBy")
            if not (isinstance(listed, list) and all(isinstance(x, str) for x in listed)
                    and self.clean(path + ("engineOnly", "citedBy"))
                    and all(self.clean(path + ("engineOnly", "citedBy", j))
                            for j in range(len(listed)))):
                continue
            exps = self.expectations()
            if exps is None:
                continue                                   # expectations-unavailable reports it
            unknown = [x for x in listed if x not in exps]
            unread = [x for x in listed if x in exps and exps[x].evidence is _EXPR]
            silent = [x for x in listed if x in exps and x not in unread
                      and not (isinstance(exps[x].evidence, tuple) and vid in exps[x].evidence)]
            missed = [eid for eid, rec in exps.items()
                      if isinstance(rec.evidence, tuple) and vid in rec.evidence
                      and eid not in listed]
            problems = []
            if unknown:
                problems.append(f"{unknown} name(s) no expectation {SCENARIOS_JS_FILE} registers")
            if unread:
                problems.append(f"{unread} carry an evidence value that is not a literal array "
                                "of string literals, which the checker reads as text and cannot "
                                "resolve")
            if silent:
                problems.append(f"{silent} do(es) not cite {vid}")
            if missed:
                problems.append(f"{missed} cite(s) {vid} but "
                                f"{'is' if len(missed) == 1 else 'are'} not listed")
            if problems:
                self.add("engine-only-evidence", f"{where}.citedBy",
                         "; ".join(problems) + ": citedBy lists exactly the expectations that "
                         "cite the record (the claim that exempts it from unused-evidence)")

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
        self.fixed_reasons(params)
        self.calibration_links(params)
        self.params_mirror(params, errors, paths)
        self.params_schema(params)
        return True, cited

    def params_schema(self, params: dict[str, Any]) -> None:
        """params.json validated against #/definitions/EngineParams when schema.json declares
        it (the M1 extension): what the hand-written rules above do not read is enforced from
        the live schema -- an undeclared or misspelt field, a missing description or notes, a
        malformed row name. A violation in a field one of those rules already reported for
        that row (PARAM_RULE_FIELDS; fixed-reason answers for mc and fixedReason together) is
        dropped, so a malformed value is still reported once, by the rule it breaks."""
        ref = "#/definitions/EngineParams"
        if self.schema is None or self.schema.ref(ref)[0] is _MISSING:
            return
        reported: set[tuple[str, str]] = set()
        for f in self.findings:
            m = _PARAM_WHERE.match(f.where)
            if m is None:
                continue
            name, field = m.group(1), m.group(2) or "*"
            reported.update((name, x) for x in (("mc", "fixedReason") if f.code == "fixed-reason"
                                                 and field in ("mc", "fixedReason") else (field,)))
        out: list = []
        self.schema.validate(params, {"$ref": ref}, "#", ("params",), out)
        for path, _ptr, kw, code, message, extra in out:
            name = str(path[1]) if len(path) > 1 else None
            field = (str(path[2]) if len(path) > 2
                     else str(extra) if kw == "required" and extra is not None else None)
            if name is not None and ((name, "*") in reported
                                     or (field is not None and (name, field) in reported)):
                continue
            where = "params.json" + "".join(f"[{part}]" if i == 0 else
                                            (f"[{part}]" if isinstance(part, int) else f".{part}")
                                            for i, part in enumerate(path[1:]))
            self.add(code, where, message)

    def fixed_reasons(self, params: dict[str, Any]) -> None:
        """HREQ-U-01 (rule fixed-reason): a row is held fixed in Monte Carlo only as a scenario
        condition, a classification threshold or an index-definition constant, recorded as
        `fixedReason`. Both engines hold a row fixed when `mc` is exactly false OR its range
        is a single point (samplingMode), so both ways in are checked."""
        permitted = ", ".join(FIXED_REASONS)
        for name, row in params.items():
            if not isinstance(row, dict):
                continue                                   # param-field reports it
            where = f"params.json[{name}]"
            mc, reason = row.get("mc", _MISSING), row.get("fixedReason", _MISSING)
            if mc is not _MISSING and not isinstance(mc, bool):
                self.add("fixed-reason", f"{where}.mc",
                         f"expected true or false, got {_show(mc)}: both engines hold a row "
                         "fixed only when mc is exactly false, so this row is sampled whatever "
                         "it was meant to say")
            elif mc is False and reason is _MISSING:
                self.add("fixed-reason", f"{where}.fixedReason",
                         f"mc: false but no fixedReason: a row is held fixed in Monte Carlo only "
                         f"as one of {permitted} (HREQ-U-01), recorded as a field, not as prose "
                         "in notes")
            elif mc is False and reason not in FIXED_REASONS:
                self.add("fixed-reason", f"{where}.fixedReason",
                         f"{_show(reason)} is not a permitted reason; HREQ-U-01 allows only "
                         f"{permitted}")
            elif mc is not False and reason is not _MISSING:
                self.add("fixed-reason", f"{where}.fixedReason",
                         f"carries fixedReason {_show(reason)} but is sampled "
                         f"({'mc: true' if mc is True else 'no mc: false'}): a reason without a "
                         "fixed row is a contradiction; set mc: false or drop the field")
            elif mc is not False:
                rng = row.get("range")
                if (isinstance(rng, list) and len(rng) == 2 and all(_is_number(x) for x in rng)
                        and rng[0] == rng[1]):
                    self.add("fixed-reason", f"{where}.range",
                             f"the range [{rng[0]}, {rng[1]}] is a single point, so Monte Carlo "
                             "holds the row fixed (samplingMode), but it is not mc: false and "
                             f"records no fixedReason ({permitted}; HREQ-U-01)")

    def scenarios_text(self) -> tuple[str | None, str, str | None]:
        """(the scenarios.js text, where it came from, why it is unusable or None)."""
        paths = self.kb.get("paths") if isinstance(self.kb.get("paths"), dict) else {}
        errors = self.kb.get("load_errors") if isinstance(self.kb.get("load_errors"), dict) else {}
        label = str(paths.get("scenarios_js") or SCENARIOS_JS_FILE)
        if errors.get("scenarios_js"):
            return None, label, str(errors["scenarios_js"])
        if "scenarios_js" in self.kb:
            text = self.kb["scenarios_js"]
            if not isinstance(text, str):
                return None, label, ("no scenarios.js found" if text is None
                                     else f"expected the file's text, got {_jtype(text)}")
            return text, label, None
        src = scenarios_js_path(paths.get("params"))
        if src is None:
            return None, SCENARIOS_JS_FILE, ("not found beside params.json or in the reference "
                                             "checkout (reference/metabolic-map-v1/engine)")
        try:
            return src.read_text(encoding="utf-8"), str(src), None
        except (OSError, UnicodeDecodeError) as exc:
            return None, str(src), f"cannot be read ({exc})"

    def expectations(self) -> dict[str, Expectation] | None:
        """Expectation id -> its record in scenarios.js; None (and one
        expectations-unavailable error) when the registry cannot be had. Read once per run:
        calibration-link and engine-only-evidence share it. An id registered twice is a
        calibration-link error (which record governs is undefined); the first is used."""
        if self._exps is _MISSING:
            self._exps = self._read_expectations()
        return self._exps

    def _read_expectations(self) -> dict[str, Expectation] | None:
        params = self.kb.get("params") if isinstance(self.kb.get("params"), dict) else {}
        evs = self.lists.get("evidence") or []
        text, label, why = self.scenarios_text()
        records: tuple[Expectation, ...] = ()
        if text is not None:
            try:
                records = expectation_records(text)
            except ScriptError as exc:
                why = f"cannot be read as JavaScript ({exc})"
            else:
                if not records:
                    why = ("registers no expectation id (each record in a scenario's expects "
                           "carries id: '<scenario_id>/<NN>')")
        if why is not None:
            rows = [n for n, r in params.items()
                    if isinstance(r, dict) and "calibratedAgainst" in r]
            marked = [v["id"] for v in evs if isinstance(v, dict) and "engineOnly" in v
                      and isinstance(v.get("id"), str)]
            self.add("expectations-unavailable", SCENARIOS_JS_FILE,
                     f"{label}: {why}. Not checked: whether the calibratedAgainst of "
                     f"{len(rows)} params.json row(s) ({', '.join(map(str, rows)) or 'none'}) "
                     "name registered expectations, and whether every expectation's "
                     "calibrates agrees with them (calibration-link, HREQ-E-10); nor the "
                     f"engineOnly.citedBy of {len(marked)} evidence record(s) "
                     f"({', '.join(marked) or 'none'}; engine-only-evidence)")
            return None
        out: dict[str, Expectation] = {}
        for rec in records:
            out.setdefault(rec.id, rec)
        for eid, n in Counter(rec.id for rec in records).items():
            if n > 1:
                self.add("calibration-link", f"{SCENARIOS_JS_FILE}[{eid}]",
                         f"registered {n} times: an expectation id names one record (HREQ-E-13), "
                         "so calibratedAgainst and engineOnly.citedBy cannot tell which of them "
                         "they name")
        return out

    def calibration_links(self, params: dict[str, Any]) -> None:
        """HREQ-E-10 (rule calibration-link): a parameter whose value was chosen to make an
        output match a target lists that expectation in calibratedAgainst; the expectation
        lists the parameter in calibrates; the two lists agree."""
        exps = self.expectations()
        listed: dict[str, list[str]] = {}          # row name -> expectations whose calibrates name it
        for eid, rec in (exps or {}).items():
            cal = rec.calibrates
            if cal is None:
                continue
            where = f"{SCENARIOS_JS_FILE}[{eid}].calibrates"
            if not isinstance(cal, tuple):
                self.add("calibration-link", where,
                         "expected a literal array of params.json row names ('name', ...); the "
                         "checker reads scenarios.js as text and cannot resolve anything else")
                continue
            for pname in cal:
                if isinstance(params.get(pname), dict):
                    listed.setdefault(pname, []).append(eid)
                else:
                    self.add("calibration-link", where,
                             f"names {pname!r}, which is not a params.json row")
        for name, row in params.items():
            if not isinstance(row, dict):
                continue
            where = f"params.json[{name}].calibratedAgainst"
            against = row.get("calibratedAgainst", _MISSING)
            notes = row.get("notes")
            said = _CALIBRATION_NOTE.search(notes) if isinstance(notes, str) else None
            if against is _MISSING:
                why = []
                if said is not None:
                    why.append(f"its notes say {notes[said.start():said.end() + 50]!r}...")
                if name in listed:
                    why.append(f"{SCENARIOS_JS_FILE} {', '.join(listed[name])} list(s) it in "
                               "calibrates")
                if why:
                    self.add("calibration-link", where,
                             "absent, but " + " and ".join(why) + ": a parameter tuned to a "
                             "target names that expectation in calibratedAgainst (HREQ-E-10)")
                continue
            if not (isinstance(against, list) and against
                    and all(isinstance(x, str) for x in against)):
                self.add("calibration-link", where,
                         "expected a non-empty array of expectation ids (<scenario_id>/<NN>), "
                         f"got {_show(against)}")
                continue
            bad = [x for x in against if not matches(EXPECTATION_ID_PATTERN, x)]
            twice = sorted({x for x in against if against.count(x) > 1})
            if bad or twice:
                self.add("calibration-link", where,
                         (f"{bad} {'is' if len(bad) == 1 else 'are'} not an expectation id "
                          f"({EXPECTATION_ID_PATTERN}, the scenario id and the two-digit "
                          "position)" if bad else f"names {twice} more than once"))
                continue
            if exps is None:
                continue                                   # expectations-unavailable reports it
            for eid in against:
                if eid not in exps:
                    self.add("calibration-link", where,
                             f"{eid!r} names no expectation {SCENARIOS_JS_FILE} registers")
                    continue
                cal = exps[eid].calibrates
                if cal is _EXPR or (isinstance(cal, tuple) and name in cal):
                    continue                               # malformed calibrates: reported once
                self.add("calibration-link", where,
                         f"names {eid!r}, but that expectation's calibrates "
                         + (f"lists {list(cal)}" if isinstance(cal, tuple) else "is absent")
                         + f", not {name!r}: the two lists must agree (HREQ-E-10)")
            for eid in listed.get(name, []):
                if eid not in against:
                    self.add("calibration-link", where,
                             f"{SCENARIOS_JS_FILE} {eid} lists this row in calibrates, but "
                             f"calibratedAgainst {against} does not name it: the two lists "
                             "must agree (HREQ-E-10)")

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


__all__ = ["DEFAULT_GRADE_BY_SOURCE", "DEFERRED_RULES", "DOCUMENTED_RULES",
           "EXPECTATION_ID_PATTERN", "FIXED_REASONS", "GRADES", "PARAM_RULE_FIELDS", "RULES",
           "SCENARIOS_JS_FILE", "SEVERITIES", "Expectation", "Finding", "PatternError",
           "ScriptError", "check_kb", "counts_by_severity", "ecma_to_python",
           "expectation_records", "js_tokens", "matches", "scenarios_js_path", "word_count"]
