"""KB integrity checker: every rule of kb/schema.json plus the cross-file contract.

`check_kb(kb)` takes the dict `health.kb.load_kb()` returns and lists Findings.
It never raises on malformed data and never stops at the first problem: a
broken record is reported and the rest of the KB is still checked.

Why not the `jsonschema` library: the package is stdlib-only, and half the
contract is not expressible in JSON Schema anyway (references resolve, parent
scale <= child scale, word limits, engine mirrors, verified identifiers -- see
the schema's own top-level description). Each rule here therefore has its own
code, listed in RULES, and the self-test plants a violation of every one.

The constants below MIRROR schema.json. Because they are a copy, the checker
also checks the copy (rule `schema-drift`): every mirrored node is compared with
the schema shipped beside the data (error on mismatch), and the whole schema's
canonical sha256 is compared with the one this module was written against (warn
on mismatch: the schema moved somewhere this module does not mirror). A schema
edit therefore cannot silently leave the checker enforcing a stale contract.

Severities: `error` -- the contract is broken; `warn` -- legal but suspect
(beyond what the schema states, or heuristic); `info` -- reported metrics.

Policy on cascades: a malformed id is reported once, by its pattern rule. It is
not ALSO reported as an unresolved reference -- a reference that cannot be
well-formed cannot resolve, and repeating that adds noise, not information.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from functools import cache
from typing import Any

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
    # -- document / record structure (schema keywords) ------------------------
    "file-shape": ("error", "a KB file is not a JSON object, or its record array is missing "
                            "or not an array"),
    "unknown-key": ("error", "a key the contract does not declare (additionalProperties: false; "
                             "for externalIds: an unknown registry)"),
    "missing-field": ("error", "a required field is absent"),
    "wrong-type": ("error", "a value has the wrong JSON type (string, number, integer, boolean, "
                            "array, object, null)"),
    "empty": ("error", "an empty string, array or object where the contract requires content "
                       "(minLength / minItems / minProperties)"),
    "not-unique": ("error", "an array that must hold unique items repeats one (uniqueItems)"),
    "bad-enum": ("error", "a value outside its enum (entity type, relation type, grade, kind, "
                          "sourceType, defaultGrade)"),
    "out-of-bounds": ("error", "an integer outside its bounds (scale 0..7, year 1800..2100)"),
    "range-shape": ("error", "a range is not an array of exactly two numbers"),
    # -- patterns ---------------------------------------------------------------
    "id-pattern": ("error", "an entity id (id, parent, relation from/to) does not match "
                            "definitions/id"),
    "evidence-id-pattern": ("error", "an evidence id (record id or reference) does not match "
                                     "definitions/evidenceId"),
    "relation-id-pattern": ("error", "a relation id does not match ^rel:[a-z0-9_:.-]+$"),
    "external-id-pattern": ("error", "an externalIds value does not match its registry's pattern"),
    "doi-pattern": ("error", "evidence doi does not match ^10\\.\\d{4,9}/\\S+$"),
    "pmid-pattern": ("error", "evidence pmid is not all digits"),
    "url-https": ("error", "evidence url does not start with https://"),
    "date-pattern": ("error", "accessed / checkedOn is not YYYY-MM-DD"),
    "doi-required": ("error", "evidence with kind 'doi' has no doi"),
    "pmid-required": ("error", "evidence with kind 'pmid' has no pmid"),
    # -- cross-file contract (schema descriptions) -----------------------------
    "duplicate-id": ("error", "the same id appears more than once in one file"),
    "dangling-evidence": ("error", "an evidence reference names no evidence record"),
    "dangling-endpoint": ("error", "a relation from/to names no entity"),
    "dangling-parent": ("error", "an entity parent names no entity"),
    "parent-scale": ("error", "an entity's parent has a larger scale than the entity"),
    "engine-params-unavailable": ("error", "quantities mirror engine params but no params.json "
                                           "was loaded"),
    "engine-param-unknown": ("error", "quantity.engineParam names no params.json row"),
    "engine-param-mismatch": ("error", "a mirrored quantity's value, range, unit or grade differs "
                                       "from its params.json row"),
    "external-id-unverified": ("error", "an externalIds value has no VERIFICATION_LOG record with "
                                        "resolved: true for that entity/db/id"),
    "summary-too-long": ("error", "entity summary exceeds 60 words"),
    "quote-too-long": ("error", "evidence quote exceeds 25 words"),
    "schema-drift": ("error", "schema.json no longer matches the contract this checker mirrors "
                              "(error: a mirrored node differs; warn: an unmirrored part changed)"),
    # -- warnings: legal but suspect --------------------------------------------
    "unused-evidence": ("warn", "an evidence record no entity, relation, quantity, conflict or "
                                "engine param cites"),
    "conflict-unsourced-note": ("warn", "a conflict with empty evidence whose note does not say "
                                        "the claim is unsourced (schema: 'note must say so')"),
    "orphan-verification": ("warn", "a resolved VERIFICATION_LOG record for an entity/id the KB "
                                    "no longer carries"),
    "parent-cycle": ("warn", "following parent links returns to the start entity"),
    "range-order": ("warn", "a range whose low end exceeds its high end"),
    "value-outside-range": ("warn", "a quantity value outside its own range"),
    "meta-mismatch": ("warn", "entities/relations/evidence disagree on kbVersion, generated or "
                              "curator"),
    "param-evidence-unresolved": ("warn", "a params.json row cites an evidence id not in "
                                          "evidence.json"),
    # -- info ---------------------------------------------------------------------
    "e-assumption-share": ("info", "share of quantities (and engine params) graded E-assumption"),
}

# ---------------------------------------------------------------------------
# Contract constants -- a MIRROR of kb/schema.json (verified by `schema-drift`)
# ---------------------------------------------------------------------------
ID_PATTERN = r"^[a-z_]+:[a-z0-9][a-z0-9_.:-]*$"
EVIDENCE_ID_PATTERN = r"^ev:[a-z0-9][a-z0-9-]*$"
RELATION_ID_PATTERN = r"^rel:[a-z0-9_:.-]+$"
DOI_PATTERN = r"^10\.\d{4,9}/\S+$"
PMID_PATTERN = r"^\d+$"
URL_PATTERN = r"^https://"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
UNIPROT_PATTERN = r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$"
HGNC_PATTERN = r"^HGNC:\d+$"
EXTERNAL_ID_PATTERNS: dict[str, str] = {
    "UBERON": r"^UBERON:\d{7}$",
    "CL": r"^CL:\d{7}$",
    "GO": r"^GO:\d{7}$",
    "KEGG": r"^(hsa\d{5}|C\d{5}|K\d{5})$",
    "Reactome": r"^R-HSA-\d+$",
    "UniProt": UNIPROT_PATTERN,
    "ChEBI": r"^CHEBI:\d+$",
    "HGNC": HGNC_PATTERN,
}
EXTERNAL_ID_ARRAYS = frozenset({"UniProt", "HGNC"})   # string OR non-empty unique array

GRADES = ("A-meta", "A-primary", "B-textbook", "C-model", "D-animal", "E-assumption")
"""Best first. The order is the ranking `report` uses for 'graded >= B'."""
ENTITY_TYPES = ("organism", "organ_system", "organ", "tissue", "cell", "organelle", "pathway",
                "reaction", "process", "molecule", "protein", "enzyme", "gene", "hormone")
RELATION_TYPES = ("part_of", "located_in", "produces", "consumes", "catalyzes", "regulates_up",
                  "regulates_down", "transports", "expressed_in")
EVIDENCE_KINDS = ("doi", "pmid", "book", "database")
SOURCE_TYPES = ("meta-analysis", "systematic-review", "rct", "primary-human", "primary-animal",
                "primary-in-vitro", "review", "textbook", "guideline", "model")
SCALE_MIN, SCALE_MAX = 0, 7
YEAR_MIN, YEAR_MAX = 1800, 2100
SUMMARY_MAX_WORDS = 60
QUOTE_MAX_WORDS = 25

TOP_KEYS = frozenset({"kbVersion", "generated", "curator", "schema",
                      "entities", "relations", "evidence"})
META_KEYS = ("kbVersion", "generated", "curator", "schema")
ENTITY_REQUIRED = ("id", "type", "name", "synonyms", "scale", "summary", "evidence")
ENTITY_KEYS = frozenset(ENTITY_REQUIRED + ("parent", "externalIds", "system", "quantities"))
RELATION_REQUIRED = ("id", "from", "to", "type", "evidence")
RELATION_KEYS = frozenset(RELATION_REQUIRED + ("quantity", "notes", "conflicts"))
QUANTITY_REQUIRED = ("value", "unit", "range", "evidence", "grade")
QUANTITY_KEYS = frozenset(QUANTITY_REQUIRED + ("measure", "engineParam", "notes", "conflicts"))
CONFLICT_REQUIRED = ("claim", "evidence", "note")
CONFLICT_KEYS = frozenset(CONFLICT_REQUIRED + ("value", "range", "unit"))
EVIDENCE_REQUIRED = ("id", "kind", "title", "authors", "year", "url", "accessed", "notes")
EVIDENCE_KEYS = frozenset(EVIDENCE_REQUIRED + ("doi", "pmid", "journal", "quote", "sourceType",
                                               "defaultGrade", "pubTypes", "verification"))
VERIFICATION_REQUIRED = ("method", "checkedOn", "retraction")
VERIFICATION_KEYS = frozenset(VERIFICATION_REQUIRED + ("by", "titleMatch", "crossrefTitle",
                                                       "errata", "quoteSource"))
# VERIFICATION_LOG.json has no schema; these are the fields the externalIds rule reads.
LOG_REQUIRED = ("entity", "db", "id", "resolved")

_KIND_REQUIRES = [
    {"if": {"properties": {"kind": {"const": "doi"}}}, "then": {"required": ["doi"]}},
    {"if": {"properties": {"kind": {"const": "pmid"}}}, "then": {"required": ["pmid"]}},
]

SCHEMA_MIRROR: tuple[tuple[str, Any], ...] = (
    ("/properties", TOP_KEYS),
    ("/additionalProperties", False),
    ("/definitions/id/pattern", ID_PATTERN),
    ("/definitions/evidenceId/pattern", EVIDENCE_ID_PATTERN),
    ("/definitions/evidenceList/uniqueItems", True),
    ("/definitions/grade/enum", list(GRADES)),
    ("/definitions/range/minItems", 2),
    ("/definitions/range/maxItems", 2),
    ("/definitions/externalIds/properties", frozenset(EXTERNAL_ID_PATTERNS)),
    *((f"/definitions/externalIds/properties/{db}/pattern", p)
      for db, p in EXTERNAL_ID_PATTERNS.items() if db not in EXTERNAL_ID_ARRAYS),
    ("/definitions/externalIds/properties/UniProt/anyOf/0/pattern", UNIPROT_PATTERN),
    ("/definitions/externalIds/properties/UniProt/anyOf/1/items/pattern", UNIPROT_PATTERN),
    ("/definitions/externalIds/properties/UniProt/anyOf/1/minItems", 1),
    ("/definitions/externalIds/properties/UniProt/anyOf/1/uniqueItems", True),
    ("/definitions/externalIds/properties/HGNC/anyOf/0/pattern", HGNC_PATTERN),
    ("/definitions/externalIds/properties/HGNC/anyOf/1/items/pattern", HGNC_PATTERN),
    ("/definitions/externalIds/properties/HGNC/anyOf/1/minItems", 1),
    ("/definitions/externalIds/properties/HGNC/anyOf/1/uniqueItems", True),
    ("/definitions/externalIds/additionalProperties", False),
    ("/definitions/externalIds/minProperties", 1),
    ("/definitions/conflict/required", list(CONFLICT_REQUIRED)),
    ("/definitions/conflict/properties", CONFLICT_KEYS),
    ("/definitions/conflict/additionalProperties", False),
    ("/definitions/quantity/required", list(QUANTITY_REQUIRED)),
    ("/definitions/quantity/properties", QUANTITY_KEYS),
    ("/definitions/quantity/properties/evidence/allOf/1/minItems", 1),
    ("/definitions/quantity/properties/unit/minLength", 1),
    ("/definitions/quantity/additionalProperties", False),
    ("/definitions/Entity/required", list(ENTITY_REQUIRED)),
    ("/definitions/Entity/properties", ENTITY_KEYS),
    ("/definitions/Entity/properties/type/enum", list(ENTITY_TYPES)),
    ("/definitions/Entity/properties/scale/type", "integer"),
    ("/definitions/Entity/properties/scale/minimum", SCALE_MIN),
    ("/definitions/Entity/properties/scale/maximum", SCALE_MAX),
    ("/definitions/Entity/properties/synonyms/uniqueItems", True),
    ("/definitions/Entity/properties/evidence/allOf/1/minItems", 1),
    ("/definitions/Entity/additionalProperties", False),
    ("/definitions/Relation/required", list(RELATION_REQUIRED)),
    ("/definitions/Relation/properties", RELATION_KEYS),
    ("/definitions/Relation/properties/id/pattern", RELATION_ID_PATTERN),
    ("/definitions/Relation/properties/type/enum", list(RELATION_TYPES)),
    ("/definitions/Relation/properties/evidence/allOf/1/minItems", 1),
    ("/definitions/Relation/additionalProperties", False),
    ("/definitions/Evidence/required", list(EVIDENCE_REQUIRED)),
    ("/definitions/Evidence/properties", EVIDENCE_KEYS),
    ("/definitions/Evidence/properties/kind/enum", list(EVIDENCE_KINDS)),
    ("/definitions/Evidence/properties/doi/pattern", DOI_PATTERN),
    ("/definitions/Evidence/properties/pmid/pattern", PMID_PATTERN),
    ("/definitions/Evidence/properties/year/minimum", YEAR_MIN),
    ("/definitions/Evidence/properties/year/maximum", YEAR_MAX),
    ("/definitions/Evidence/properties/url/pattern", URL_PATTERN),
    ("/definitions/Evidence/properties/accessed/pattern", DATE_PATTERN),
    ("/definitions/Evidence/properties/sourceType/enum", list(SOURCE_TYPES)),
    ("/definitions/Evidence/properties/verification/required", list(VERIFICATION_REQUIRED)),
    ("/definitions/Evidence/properties/verification/properties", VERIFICATION_KEYS),
    ("/definitions/Evidence/properties/verification/properties/checkedOn/pattern", DATE_PATTERN),
    ("/definitions/Evidence/properties/verification/additionalProperties", False),
    ("/definitions/Evidence/allOf", _KIND_REQUIRES),
    ("/definitions/Evidence/additionalProperties", False),
)
SCHEMA_SHA256 = "aaf849f5721cc95b0cde54e23c7fc5ef302a6584527715f0e1f16ab38889ffcb"
"""Canonical (sorted-key, compact) sha256 of the schema.json this module mirrors."""

_UNSOURCED_MARKERS = ("unsourced", "no source", "no verified source", "not sourced",
                      "without a source", "no citation", "uncited")

_MISSING = object()


@cache
def _regex(pattern: str) -> re.Pattern[str]:
    # JSON Schema patterns are ECMA-262: `\d` is ASCII-only and `$` is end of input.
    # Python's `$` also matches before a trailing newline, so pin it with `\Z`.
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern, re.ASCII)


def matches(pattern: str, value: str) -> bool:
    return _regex(pattern).search(value) is not None


def word_count(text: str) -> int:
    return len(text.split())


def _jtype(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_integer(v: Any) -> bool:
    return (isinstance(v, int) and not isinstance(v, bool)) or (
        isinstance(v, float) and v.is_integer())


def _canonical(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def schema_sha256(schema: Any) -> str:
    return hashlib.sha256(_canonical(schema).encode("utf-8")).hexdigest()


def _pointer(doc: Any, pointer: str) -> Any:
    node = doc
    for part in pointer.strip("/").split("/"):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _label(fname: str, rec: Any, index: int) -> str:
    if isinstance(rec, dict) and isinstance(rec.get("id"), str) and rec["id"]:
        return f"{fname}[{rec['id']}]"
    return f"{fname}[#{index}]"


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------
class _Checker:
    def __init__(self, kb: dict[str, Any]) -> None:
        self.kb = kb
        self.findings: list[Finding] = []
        self.ev_refs: list[tuple[str, str]] = []               # (evidence id, where)
        self.endpoints: list[tuple[str, str]] = []             # (entity id, where)
        self.parents: list[tuple[str, str, str]] = []          # (child id, parent id, where)
        self.ext_ids: list[tuple[str | None, str, str, str]] = []  # (entity, db, id, where)
        self.mirrors: list[tuple[str, dict[str, Any], str]] = []   # (param, quantity, where)
        self.quantity_grades: list[str | None] = []

    # -- emit ---------------------------------------------------------------
    def add(self, code: str, where: str, message: str, severity: str | None = None) -> None:
        sev = severity or RULES[code][0]
        self.findings.append(Finding(code, sev, where, message))

    # -- generic field helpers ----------------------------------------------------
    def record(self, obj: Any, where: str, required: tuple[str, ...],
               allowed: frozenset[str] | None) -> bool:
        if not isinstance(obj, dict):
            self.add("wrong-type", where, f"expected an object, got {_jtype(obj)}")
            return False
        if allowed is not None:
            for key in obj:
                if key not in allowed:
                    self.add("unknown-key", f"{where}.{key}",
                             f"key {key!r} is not declared by the contract "
                             "(additionalProperties: false)")
        for key in required:
            if key not in obj:
                self.add("missing-field", where, f"required field {key!r} is missing")
        return True

    def string(self, obj: dict[str, Any], key: str, where: str, *, nonempty: bool = False,
               pattern: str | None = None, code: str = "", nullable: bool = False) -> str | None:
        if key not in obj:
            return None
        v, w = obj[key], f"{where}.{key}"
        if v is None and nullable:
            return None
        if not isinstance(v, str):
            want = "string or null" if nullable else "string"
            self.add("wrong-type", w, f"expected {want}, got {_jtype(v)}")
            return None
        if nonempty and not v:
            self.add("empty", w, "must be a non-empty string")
            return None
        if pattern is not None and not matches(pattern, v):
            self.add(code, w, f"{v!r} does not match {pattern}")
            return None
        return v

    def enum(self, obj: dict[str, Any], key: str, where: str,
             allowed: tuple[str, ...]) -> str | None:
        if key not in obj:
            return None
        v, w = obj[key], f"{where}.{key}"
        if not isinstance(v, str):
            self.add("wrong-type", w, f"expected string, got {_jtype(v)}")
            return None
        if v not in allowed:
            self.add("bad-enum", w, f"{v!r} is not one of {', '.join(allowed)}")
            return None
        return v

    def number(self, obj: dict[str, Any], key: str, where: str) -> float | None:
        if key not in obj:
            return None
        v = obj[key]
        if not _is_number(v):
            self.add("wrong-type", f"{where}.{key}", f"expected number, got {_jtype(v)}")
            return None
        return v

    def integer(self, obj: dict[str, Any], key: str, where: str, lo: int, hi: int) -> int | None:
        if key not in obj:
            return None
        v, w = obj[key], f"{where}.{key}"
        if not _is_integer(v):
            self.add("wrong-type", w, f"expected integer, got {_jtype(v)}")
            return None
        if not lo <= v <= hi:
            self.add("out-of-bounds", w, f"{v} is outside {lo}..{hi}")
            return None
        return int(v)

    def boolean(self, obj: dict[str, Any], key: str, where: str) -> bool | None:
        if key not in obj:
            return None
        v = obj[key]
        if not isinstance(v, bool):
            self.add("wrong-type", f"{where}.{key}", f"expected boolean, got {_jtype(v)}")
            return None
        return v

    def string_list(self, obj: dict[str, Any], key: str, where: str, *,
                    nonempty_items: bool = False, unique: bool = False) -> list[str] | None:
        if key not in obj:
            return None
        v, w = obj[key], f"{where}.{key}"
        if not isinstance(v, list):
            self.add("wrong-type", w, f"expected array, got {_jtype(v)}")
            return None
        out: list[str] = []
        for i, item in enumerate(v):
            if not isinstance(item, str):
                self.add("wrong-type", f"{w}[{i}]", f"expected string, got {_jtype(item)}")
            elif nonempty_items and not item:
                self.add("empty", f"{w}[{i}]", "must be a non-empty string")
            else:
                out.append(item)
        if unique:
            self._unique(v, w)
        return out

    def _unique(self, items: list[Any], where: str) -> None:
        counts = Counter(_canonical(x) for x in items)
        for item, n in counts.items():
            if n > 1:
                self.add("not-unique", where, f"{item} appears {n} times (uniqueItems)")

    def evidence_list(self, obj: dict[str, Any], key: str, where: str,
                      min_items: int) -> list[str] | None:
        if key not in obj:
            return None
        v, w = obj[key], f"{where}.{key}"
        if not isinstance(v, list):
            self.add("wrong-type", w, f"expected array of evidence ids, got {_jtype(v)}")
            return None
        if len(v) < min_items:
            self.add("empty", w, f"must cite at least {min_items} evidence record")
        out: list[str] = []
        for i, item in enumerate(v):
            if not isinstance(item, str):
                self.add("wrong-type", f"{w}[{i}]", f"expected string, got {_jtype(item)}")
            elif not matches(EVIDENCE_ID_PATTERN, item):
                self.add("evidence-id-pattern", f"{w}[{i}]",
                         f"{item!r} does not match {EVIDENCE_ID_PATTERN}")
            else:
                out.append(item)
                self.ev_refs.append((item, w))
        self._unique(v, w)
        return out

    def range_(self, obj: dict[str, Any], key: str, where: str) -> tuple[float, float] | None:
        if key not in obj:
            return None
        v, w = obj[key], f"{where}.{key}"
        if not (isinstance(v, list) and len(v) == 2 and all(_is_number(x) for x in v)):
            self.add("range-shape", w, f"expected [low, high] (two numbers), got {_canonical(v)}")
            return None
        lo, hi = v
        if lo > hi:
            self.add("range-order", w, f"low end {lo} exceeds high end {hi}")
            return None
        return lo, hi

    # -- documents ------------------------------------------------------------------
    def document(self, key: str) -> list[Any]:
        fname = DATA_FILES[key]
        doc = self.kb.get(key)
        if not isinstance(doc, dict):
            self.add("file-shape", fname, f"expected a JSON object, got {_jtype(doc)}")
            return []
        if key != "verification":           # the log has no schema; only KB files are closed
            for k in doc:
                if k not in TOP_KEYS:
                    self.add("unknown-key", f"{fname}.{k}",
                             f"top-level key {k!r} is not declared by the contract")
            for k in META_KEYS:
                if k in doc and not isinstance(doc[k], str):
                    self.add("wrong-type", f"{fname}.{k}", f"expected string, got {_jtype(doc[k])}")
        list_key = RECORD_KEYS[key]
        items = doc.get(list_key, _MISSING)
        if items is _MISSING:
            self.add("file-shape", fname, f"has no {list_key!r} array")
            return []
        if not isinstance(items, list):
            self.add("file-shape", f"{fname}.{list_key}", f"expected an array, got {_jtype(items)}")
            return []
        if key != "verification":           # other arrays a KB file may carry, per schema
            for other in ("entities", "relations", "evidence"):
                if other != list_key and other in doc and not isinstance(doc[other], list):
                    self.add("wrong-type", f"{fname}.{other}",
                             f"expected an array, got {_jtype(doc[other])}")
        return items

    # -- records ----------------------------------------------------------------------
    def entity(self, e: Any, where: str) -> None:
        if not self.record(e, where, ENTITY_REQUIRED, ENTITY_KEYS):
            return
        eid = self.string(e, "id", where, pattern=ID_PATTERN, code="id-pattern")
        self.enum(e, "type", where, ENTITY_TYPES)
        self.string(e, "name", where, nonempty=True)
        self.string_list(e, "synonyms", where, nonempty_items=True, unique=True)
        self.integer(e, "scale", where, SCALE_MIN, SCALE_MAX)
        parent = self.string(e, "parent", where, pattern=ID_PATTERN, code="id-pattern",
                             nullable=True)
        if parent is not None and eid is not None:
            self.parents.append((eid, parent, f"{where}.parent"))
        self.external_ids(e, where, eid)
        summary = self.string(e, "summary", where, nonempty=True)
        if summary is not None and word_count(summary) > SUMMARY_MAX_WORDS:
            self.add("summary-too-long", f"{where}.summary",
                     f"{word_count(summary)} words (limit {SUMMARY_MAX_WORDS})")
        self.evidence_list(e, "evidence", where, 1)
        self.string(e, "system", where)
        self.string_list(e, "quantities", where)

    def external_ids(self, e: dict[str, Any], where: str, eid: str | None) -> None:
        if "externalIds" not in e:
            return
        x, w = e["externalIds"], f"{where}.externalIds"
        if not isinstance(x, dict):
            self.add("wrong-type", w, f"expected object, got {_jtype(x)}")
            return
        if not x:
            self.add("empty", w, "must name at least one registry (minProperties: 1)")
        for db, val in x.items():
            wd = f"{w}.{db}"
            if db not in EXTERNAL_ID_PATTERNS:
                self.add("unknown-key", wd, f"registry {db!r} is not declared by the contract "
                                            f"(allowed: {', '.join(EXTERNAL_ID_PATTERNS)})")
                continue
            if isinstance(val, list) and db in EXTERNAL_ID_ARRAYS:
                if not val:
                    self.add("empty", wd, "an identifier array must hold at least one id")
                self._unique(val, wd)
                items = val
            elif isinstance(val, str):
                items = [val]
            else:
                want = "string or array of strings" if db in EXTERNAL_ID_ARRAYS else "string"
                self.add("wrong-type", wd, f"expected {want}, got {_jtype(val)}")
                continue
            for item in items:
                if not isinstance(item, str):
                    self.add("wrong-type", wd, f"expected string, got {_jtype(item)}")
                elif not matches(EXTERNAL_ID_PATTERNS[db], item):
                    self.add("external-id-pattern", wd,
                             f"{item!r} does not match {EXTERNAL_ID_PATTERNS[db]}")
                else:
                    self.ext_ids.append((eid, db, item, wd))

    def relation(self, r: Any, where: str) -> None:
        if not self.record(r, where, RELATION_REQUIRED, RELATION_KEYS):
            return
        self.string(r, "id", where, pattern=RELATION_ID_PATTERN, code="relation-id-pattern")
        for end in ("from", "to"):
            v = self.string(r, end, where, pattern=ID_PATTERN, code="id-pattern")
            if v is not None:
                self.endpoints.append((v, f"{where}.{end}"))
        self.enum(r, "type", where, RELATION_TYPES)
        if "quantity" in r:
            self.quantity(r["quantity"], f"{where}.quantity")
        self.evidence_list(r, "evidence", where, 1)
        self.string(r, "notes", where)
        self.conflicts(r, where)

    def quantity(self, q: Any, where: str) -> None:
        if not self.record(q, where, QUANTITY_REQUIRED, QUANTITY_KEYS):
            return
        value = self.number(q, "value", where)
        self.string(q, "unit", where, nonempty=True)
        rng = self.range_(q, "range", where)
        self.evidence_list(q, "evidence", where, 1)
        self.quantity_grades.append(self.enum(q, "grade", where, GRADES))
        self.string(q, "measure", where)
        self.string(q, "notes", where)
        param = self.string(q, "engineParam", where)
        if param is not None:
            self.mirrors.append((param, q, where))
        self.conflicts(q, where)
        if rng is not None and value is not None and not rng[0] <= value <= rng[1]:
            self.add("value-outside-range", f"{where}.value",
                     f"value {value} lies outside its range [{rng[0]}, {rng[1]}]")

    def conflicts(self, obj: dict[str, Any], where: str) -> None:
        if "conflicts" not in obj:
            return
        cs, w = obj["conflicts"], f"{where}.conflicts"
        if not isinstance(cs, list):
            self.add("wrong-type", w, f"expected array, got {_jtype(cs)}")
            return
        for i, c in enumerate(cs):
            wc = f"{w}[{i}]"
            if not self.record(c, wc, CONFLICT_REQUIRED, CONFLICT_KEYS):
                continue
            self.string(c, "claim", wc, nonempty=True)
            self.number(c, "value", wc)
            self.range_(c, "range", wc)
            self.string(c, "unit", wc)
            cited = self.evidence_list(c, "evidence", wc, 0)
            note = self.string(c, "note", wc)
            if (cited is not None and not c["evidence"] and note is not None
                    and not any(m in note.lower() for m in _UNSOURCED_MARKERS)):
                self.add("conflict-unsourced-note", f"{wc}.note",
                         "conflict cites no evidence, so its note must say the claim is "
                         f"unsourced; note reads {note[:80]!r}")

    def evidence(self, v: Any, where: str) -> None:
        if not self.record(v, where, EVIDENCE_REQUIRED, EVIDENCE_KEYS):
            return
        self.string(v, "id", where, pattern=EVIDENCE_ID_PATTERN, code="evidence-id-pattern")
        kind = self.enum(v, "kind", where, EVIDENCE_KINDS)
        self.string(v, "doi", where, pattern=DOI_PATTERN, code="doi-pattern")
        self.string(v, "pmid", where, pattern=PMID_PATTERN, code="pmid-pattern")
        if kind == "doi" and "doi" not in v:
            self.add("doi-required", where, "kind is 'doi' but the record has no doi")
        if kind == "pmid" and "pmid" not in v:
            self.add("pmid-required", where, "kind is 'pmid' but the record has no pmid")
        self.string(v, "title", where, nonempty=True)
        self.string(v, "authors", where, nonempty=True)
        self.integer(v, "year", where, YEAR_MIN, YEAR_MAX)
        self.string(v, "journal", where)
        self.string(v, "url", where, pattern=URL_PATTERN, code="url-https")
        self.string(v, "accessed", where, pattern=DATE_PATTERN, code="date-pattern")
        quote = self.string(v, "quote", where, nonempty=True)
        if quote is not None and word_count(quote) > QUOTE_MAX_WORDS:
            self.add("quote-too-long", f"{where}.quote",
                     f"{word_count(quote)} words (limit {QUOTE_MAX_WORDS})")
        self.string(v, "notes", where)
        self.enum(v, "sourceType", where, SOURCE_TYPES)
        self.enum(v, "defaultGrade", where, GRADES)
        self.string_list(v, "pubTypes", where)
        if "verification" in v:
            self.verification(v["verification"], f"{where}.verification")

    def verification(self, x: Any, where: str) -> None:
        if not self.record(x, where, VERIFICATION_REQUIRED, VERIFICATION_KEYS):
            return
        self.string(x, "method", where)
        self.string(x, "checkedOn", where, pattern=DATE_PATTERN, code="date-pattern")
        self.string(x, "by", where)
        self.boolean(x, "titleMatch", where)
        self.string(x, "crossrefTitle", where, nullable=True)
        self.string(x, "retraction", where)
        self.string_list(x, "errata", where)
        self.string(x, "quoteSource", where, nullable=True)

    def log_record(self, r: Any, where: str) -> None:
        if not self.record(r, where, LOG_REQUIRED, None):
            return
        for key in ("entity", "db", "id"):
            self.string(r, key, where)
        self.boolean(r, "resolved", where)
        self.string(r, "checkedOn", where, pattern=DATE_PATTERN, code="date-pattern")

    # -- cross-file ------------------------------------------------------------------
    def duplicates(self, fname: str, items: list[Any]) -> None:
        ids = Counter(r["id"] for r in items
                      if isinstance(r, dict) and isinstance(r.get("id"), str))
        for rid, n in ids.items():
            if n > 1:
                self.add("duplicate-id", f"{fname}[{rid}]", f"id appears {n} times in {fname}")

    def cross_file(self, ents: list[Any], rels: list[Any], evs: list[Any],
                   log: list[Any]) -> None:
        for fname, items in ((DATA_FILES["entities"], ents), (DATA_FILES["relations"], rels),
                             (DATA_FILES["evidence"], evs)):
            self.duplicates(fname, items)

        entities: dict[str, dict[str, Any]] = {}
        for e in ents:
            if isinstance(e, dict) and isinstance(e.get("id"), str):
                entities.setdefault(e["id"], e)
        evidence_ids = {v["id"] for v in evs
                        if isinstance(v, dict) and isinstance(v.get("id"), str)}

        for ref, where in self.ev_refs:
            if ref not in evidence_ids:
                self.add("dangling-evidence", where, f"{ref!r} is not an evidence.json record")
        for ref, where in self.endpoints:
            if ref not in entities:
                self.add("dangling-endpoint", where, f"{ref!r} is not an entities.json record")
        self.hierarchy(entities)
        cited = {ref for ref, _ in self.ev_refs} | self.engine_params(evidence_ids)
        for v in evs:
            vid = v.get("id") if isinstance(v, dict) else None
            if (isinstance(vid, str) and matches(EVIDENCE_ID_PATTERN, vid)
                    and vid not in cited):
                self.add("unused-evidence", _label(DATA_FILES["evidence"], v, 0),
                         "cited by no entity, relation, quantity, conflict or params.json row "
                         "(engine validation scenarios are not scanned)")
        self.verified_ids(entities, log)
        self.meta_consistency()
        self.assumption_share()

    def hierarchy(self, entities: dict[str, dict[str, Any]]) -> None:
        links: dict[str, str] = {}
        for child, parent, where in self.parents:
            if parent not in entities:
                self.add("dangling-parent", where, f"{parent!r} is not an entities.json record")
                continue
            links.setdefault(child, parent)
            cs, ps = entities[child].get("scale"), entities[parent].get("scale")
            if _is_integer(cs) and _is_integer(ps) and ps > cs:
                self.add("parent-scale", where,
                         f"parent {parent!r} has scale {ps} > this entity's scale {cs}")
        reported: set[frozenset[str]] = set()
        for start in links:
            seen: list[str] = [start]
            node = links.get(start)
            while node is not None and node not in seen:
                seen.append(node)
                node = links.get(node)
            if node == start:
                cycle = frozenset(seen)
                if cycle not in reported:
                    reported.add(cycle)
                    self.add("parent-cycle", f"{DATA_FILES['entities']}[{min(cycle)}].parent",
                             "parent chain loops: " + " -> ".join(seen + [start]))

    def engine_params(self, evidence_ids: set[str]) -> set[str]:
        """Check the mirrors; return the evidence ids params.json cites."""
        params = self.kb.get("params")
        cited: set[str] = set()
        if not isinstance(params, dict):
            if self.mirrors:
                names = ", ".join(sorted({p for p, _, _ in self.mirrors}))
                self.add("engine-params-unavailable", "params.json",
                         f"{len(self.mirrors)} quantities mirror engine params ({names}) but "
                         "no params.json was loaded, so none of them could be verified")
            return cited
        for name, row in params.items():
            refs = row.get("evidence") if isinstance(row, dict) else None
            for ref in refs if isinstance(refs, list) else []:
                if isinstance(ref, str):
                    cited.add(ref)
                    if ref not in evidence_ids:
                        self.add("param-evidence-unresolved", f"params.json[{name}].evidence",
                                 f"{ref!r} is not an evidence.json record")
        for name, q, where in self.mirrors:
            row = params.get(name)
            if not isinstance(row, dict):
                self.add("engine-param-unknown", f"{where}.engineParam",
                         f"{name!r} is not a params.json row")
                continue
            diffs = []
            for field in ("value", "range", "unit", "grade"):
                qv, pv = q.get(field, _MISSING), row.get(field, _MISSING)
                if not _same(qv, pv):
                    diffs.append(f"{field} {_show(qv)} != params {_show(pv)}")
            if diffs:
                self.add("engine-param-mismatch", f"{where}.engineParam",
                         f"mirrors params.json[{name!r}] but " + "; ".join(diffs))
        return cited

    def verified_ids(self, entities: dict[str, dict[str, Any]], log: list[Any]) -> None:
        verified: set[tuple[str, str, str]] = set()
        logged: set[tuple[str, str, str]] = set()
        resolved_records: list[tuple[int, tuple[str, str, str]]] = []
        for i, r in enumerate(log):
            if not isinstance(r, dict):
                continue
            key = (r.get("entity"), r.get("db"), r.get("id"))
            if not all(isinstance(k, str) for k in key):
                continue
            logged.add(key)
            if r.get("resolved") is True:
                verified.add(key)
                resolved_records.append((i, key))
        carried: set[tuple[str, str, str]] = set()
        for eid, db, item, where in self.ext_ids:
            if eid is None:
                continue
            carried.add((eid, db, item))
            if (eid, db, item) not in verified:
                why = ("is logged but never with resolved: true" if (eid, db, item) in logged
                       else "has no VERIFICATION_LOG.json record")
                self.add("external-id-unverified", where, f"{db} {item!r} for {eid} {why}")
        log_name = DATA_FILES["verification"]
        for i, (eid, db, item) in resolved_records:
            if eid not in entities:
                self.add("orphan-verification", f"{log_name}[#{i}]",
                         f"resolved record for {eid!r}, which is not an entities.json record")
            elif (eid, db, item) not in carried:
                self.add("orphan-verification", f"{log_name}[#{i}]",
                         f"resolved record for {eid} {db} {item!r}, which that entity no "
                         "longer carries")

    def meta_consistency(self) -> None:
        for field in ("kbVersion", "generated", "curator"):
            seen: dict[str, str] = {}
            for key in ("entities", "relations", "evidence"):
                doc = self.kb.get(key)
                if isinstance(doc, dict) and isinstance(doc.get(field), str):
                    seen[DATA_FILES[key]] = doc[field]
            if len(set(seen.values())) > 1:
                self.add("meta-mismatch", f"{'/'.join(seen)}:{field}",
                         "files disagree: " + ", ".join(f"{f}={v!r}" for f, v in seen.items()))

    def assumption_share(self) -> None:
        total = len(self.quantity_grades)
        if total:
            n = sum(1 for g in self.quantity_grades if g == "E-assumption")
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

    def schema_drift(self) -> None:
        schema = self.kb.get("schema")
        fname = DATA_FILES["schema"]
        if not isinstance(schema, dict):
            self.add("schema-drift", fname, f"expected the schema object, got {_jtype(schema)}")
            return
        drifted = False
        for pointer, expected in SCHEMA_MIRROR:
            actual = _pointer(schema, pointer)
            if isinstance(expected, frozenset):
                ok = isinstance(actual, dict) and set(actual) == set(expected)
                shown = sorted(actual) if isinstance(actual, dict) else actual
            else:
                ok = actual is not _MISSING and _canonical(actual) == _canonical(expected)
                shown = actual
            if not ok:
                drifted = True
                got = "absent" if shown is _MISSING else _canonical(shown)
                want = _canonical(sorted(expected) if isinstance(expected, frozenset) else expected)
                self.add("schema-drift", f"{fname}#{pointer}",
                         f"checker mirrors {want} but the schema has {got}; update "
                         "health/kb/check.py to the new contract")
        digest = schema_sha256(schema)
        if not drifted and digest != SCHEMA_SHA256:
            self.add("schema-drift", fname,
                     f"schema sha256 {digest[:12]} differs from the {SCHEMA_SHA256[:12]} this "
                     "checker was written against, in a part it does not mirror; review the "
                     "change and re-pin SCHEMA_SHA256", severity="warn")

    # -- driver --------------------------------------------------------------------------
    def run(self) -> list[Finding]:
        self.schema_drift()
        lists: dict[str, list[Any]] = {}
        per_record = {"entities": self.entity, "relations": self.relation,
                      "evidence": self.evidence, "verification": self.log_record}
        for key, fn in per_record.items():
            items = self.document(key)
            lists[key] = items
            for i, rec in enumerate(items):
                where = (f"{DATA_FILES[key]}[#{i}]" if key == "verification"
                         else _label(DATA_FILES[key], rec, i))
                fn(rec, where)
        self.cross_file(lists["entities"], lists["relations"], lists["evidence"],
                        lists["verification"])
        return self.findings


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


def _show(v: Any) -> str:
    return "absent" if v is _MISSING else _canonical(v)


def check_kb(kb: dict[str, Any]) -> list[Finding]:
    """Every contract rule over a loaded KB. Never raises on malformed data."""
    return _Checker(kb).run()


def counts_by_severity(findings: list[Finding]) -> dict[str, int]:
    c = Counter(f.severity for f in findings)
    return {s: c.get(s, 0) for s in SEVERITIES}


__all__ = ["GRADES", "RULES", "SEVERITIES", "Finding", "check_kb", "counts_by_severity",
           "matches", "word_count"]
