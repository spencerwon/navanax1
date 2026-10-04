"""Standard-library-only self-test for the health knowledge base (KB) tooling.

Runnable with a bare `python3 tests/health_kb_selftest.py` -- no pytest, no
jsonschema. It covers health.kb (loader), health.kb.check (the contract checker),
health.kb.report (the summary), health.registry (the module registry and its stdlib
YAML reader) and health.cli (kb-check, kb-summary, status), with the same conventions
as tests/selftest.py: module-level `test_*` functions are discovered in definition
order, `check()` records each assertion, `@needs(...)` declares third-party modules
(PyYAML, for the reader-vs-PyYAML comparison only), and `--no-skips` refuses to exit 0
if anything was skipped.

The central claim is "a rule with no planted-violation test is not a rule" (HREQ-V-17):
test_every_contract_rule_fires_on_a_planted_violation mutates a copy of the shipped KB
to break exactly one rule at a time, asserts the checker reports that rule's code and
NO OTHER, and finally asserts that the plants covered every code in RULES.
test_every_documented_rule_is_implemented_or_deferred holds RULES to the docs/health/03
§6 table, parsed at run time (HREQ-V-16).

This file also carries the independent rigor review of 2026-10-03 (formerly
tests/health_kb_rigor_probe.py): its pinning checks (each kills a mutant the earlier
suite let survive) and its defect regressions.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from health.cli import DISCLAIMER, VALIDATION_STATUS  # noqa: E402
from health.kb import KBLoadError, load_kb, parse_json, parse_params_js, records  # noqa: E402
from health.kb.check import (  # noqa: E402
    DEFERRED_RULES,
    DOCUMENTED_RULES,
    EXPECTATION_ID_PATTERN,
    FIXED_REASONS,
    GRADES,
    RULES,
    Finding,
    PatternError,
    ScriptError,
    check_kb,
    ecma_to_python,
    expectation_records,
    js_tokens,
    matches,
    word_count,
)
from health.kb.report import GRADE_B_OR_BETTER, summary  # noqa: E402

DATA_DIR = ROOT / "src" / "health" / "kb" / "data"
DOC_03 = ROOT / "docs" / "health" / "03_VALIDATION_AND_TESTING.md"
PARAMS_DATA_JS = ROOT / "reference" / "metabolic-map-v1" / "engine" / "params.data.js"
SCENARIOS_JS = ROOT / "reference" / "metabolic-map-v1" / "engine" / "scenarios.js"
BASE_YAML = ROOT / "config" / "health" / "base.yaml"

PASS: list[str] = []
FAIL: list[str] = []
SKIPPED: list[tuple[str, tuple[str, ...]]] = []   # (test name, the modules that were missing)


def needs(*modules: str):
    """Declare the third-party modules a test cannot run without (see tests/selftest.py)."""
    if not modules:
        raise ValueError("needs() requires at least one module name")

    def deco(fn):
        prior = getattr(fn, "needs_modules", ())
        fn.needs_modules = tuple(dict.fromkeys(prior + tuple(modules)))
        return fn

    return deco


_IMPORTABLE: dict[str, bool] = {}


def module_available(name: str) -> bool:
    if name not in _IMPORTABLE:
        try:
            importlib.import_module(name)
            _IMPORTABLE[name] = True
        except ImportError:
            _IMPORTABLE[name] = False
    return _IMPORTABLE[name]


def missing_modules(fn) -> tuple[str, ...]:
    return tuple(m for m in getattr(fn, "needs_modules", ()) if not module_available(m))


def check(name: str, cond: bool, detail: str = "") -> None:
    suffix = f" -- {detail}" if detail and not cond else ""
    PASS.append(name) if cond else FAIL.append(f"{name}{suffix}")
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"\n        {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------------------
# Fixtures: the shipped KB, loaded once; every test mutates a deep copy.
# ---------------------------------------------------------------------------
_BASE: dict[str, Any] = {}
_BASELINE: set[Finding] = set()


def base_kb() -> dict[str, Any]:
    if not _BASE:
        _BASE.update(load_kb())
        _BASELINE.update(check_kb(_BASE))
    return copy.deepcopy(_BASE)


def baseline() -> set[Finding]:
    base_kb()
    return set(_BASELINE)


def new_findings(mutate: Callable[[dict[str, Any]], Any]) -> list[Finding]:
    kb = base_kb()
    mutate(kb)
    return [f for f in check_kb(kb) if f not in _BASELINE]


def new_codes(mutate: Callable[[dict[str, Any]], Any]) -> set[str]:
    return {f.code for f in new_findings(mutate)}


def ent(kb: dict[str, Any], eid: str) -> dict[str, Any]:
    return next(e for e in records(kb, "entities") if e["id"] == eid)


def rel(kb: dict[str, Any], rid: str) -> dict[str, Any]:
    return next(r for r in records(kb, "relations") if r["id"] == rid)


def ev(kb: dict[str, Any], vid: str) -> dict[str, Any]:
    return next(v for v in records(kb, "evidence") if v["id"] == vid)


def log_record(kb: dict[str, Any], eid: str) -> dict[str, Any]:
    return next(r for r in records(kb, "verification") if r["entity"] == eid)


NEPHRON = "rel:part_of:tissue:nephron"          # a quantity that mirrors NO engine param
ADH_SLOPE = "rel:osm:osmoreception-avp"         # quantity mirrors params.json adh_slope
UNSOURCED = "rel:avp:body-clears-avp"           # quantity conflict with empty evidence
GFR = "rel:filt:glomerulus-filters-water"       # quantity conflicts, one with a range
LEAF = "molecule:water"                         # scale 7, nobody's parent
BODY = "organism:body"                          # scale 0, only UBERON in externalIds
HEART = "organ:heart"                           # scale 2, parent scale 1
TEXTBOOK = "ev:guyton-hall-2021"                # B-textbook; cited by entities and params
PRIMARY = "ev:robertson-athar-1976"             # kind doi, cited, verified, titleMatch true
FREE_PARAM = "V_ecf_0"                          # a params.json row no quantity mirrors
ENGINE_ONLY = "ev:crowe-1987"                   # cited only by drink_water_1L expectations
V1_UNUSED = {"ev:shafiee-2005", "ev:crowe-1987", "ev:uttamsingh-1985", "ev:heer-2000",
             "ev:rakova-2017", "ev:suckling-2012"}
"""The six evidence records V1 shipped uncited by any knowledge-base record or params.json
row (tracker W-6). From the curator's M1 records on, each is cited (Suckling 2012) or marked
engineOnly; no other record may become unused without this set changing."""


def nephron_q(kb: dict[str, Any]) -> dict[str, Any]:
    return rel(kb, NEPHRON)["quantity"]


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def _new_evidence(kb: dict[str, Any], vid: str) -> dict[str, Any]:
    v = copy.deepcopy(records(kb, "evidence")[1])
    v["id"] = vid
    records(kb, "evidence").append(v)
    return v


def _new_entity(kb: dict[str, Any], eid: str, **changes: Any) -> dict[str, Any]:
    """A copy of LEAF under a new id, without externalIds (it has no log records)."""
    e = copy.deepcopy(ent(kb, LEAF))
    e.pop("externalIds", None)
    e.update(id=eid, synonyms=[], **changes)
    records(kb, "entities").append(e)
    return e


def _params(kb: dict[str, Any], fn: Callable[[dict[str, Any]], Any]) -> None:
    """Edit params.json AND its params.data.js mirror the same way (as a regeneration
    would), so a parameter plant does not also trip params-mirror."""
    fn(kb["params"])
    fn(kb["params_js"])


def _two_cycle(kb: dict[str, Any]) -> None:
    """A -> B -> A between two same-scale entities, so no scale rule fires too."""
    by_id = {e["id"]: e for e in records(kb, "entities")}
    child = next(e for e in records(kb, "entities")
                 if e.get("parent") and by_id[e["parent"]]["scale"] == e["scale"])
    by_id[child["parent"]]["parent"] = child["id"]


def _schema_def(kb: dict[str, Any], *path: str) -> Any:
    node = kb["schema"]["definitions"]
    for part in path:
        node = node[part]
    return node


def _unsourced(note: str) -> Callable[[dict[str, Any]], None]:
    def mutate(kb: dict[str, Any]) -> None:
        next(c for c in rel(kb, UNSOURCED)["quantity"]["conflicts"]
             if not c["evidence"]).update(note=note)
    return mutate


CALIBRATED = {"map_vol_exp": "chronic_high_salt_30d/01", "pn_gain": "chronic_high_salt_30d/01",
              "aldo_vol_exp": "chronic_high_salt_30d/01",
              "map_auto_tau_h": "chronic_high_salt_30d/02"}
"""The four calibrated parameters and the expectation each was tuned to (docs/health/01
§6.3: the expectation records' `calibrates`)."""


def _fixed_reason(name: str) -> str | None:
    """The reason docs/health/01 §4.4 gives a shipped `mc: false` row."""
    if name.endswith(("_base_Ld", "_base_mmold")):
        return "scenario-condition"
    if name.startswith("na_normal_"):
        return "classification-threshold"
    if name.startswith("strain_"):
        return "index-constant"
    return None


_M1_DEFINITIONS: dict[str, Any] = {
    "isoDate": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    "expectationId": {"type": "string", "pattern": EXPECTATION_ID_PATTERN},
    "fixedReason": {"type": "string", "enum": list(FIXED_REASONS)},
    "Param": {
        "type": "object",
        "required": ["value", "unit", "range", "description", "evidence", "grade", "notes"],
        "properties": {
            "value": {"type": "number"}, "unit": {"type": "string", "minLength": 1},
            "range": {"$ref": "#/definitions/range"}, "mc": {"type": "boolean"},
            "fixedReason": {"$ref": "#/definitions/fixedReason"},
            "description": {"type": "string", "minLength": 1},
            "evidence": {"allOf": [{"$ref": "#/definitions/evidenceList"}, {"minItems": 1}]},
            "grade": {"$ref": "#/definitions/grade"},
            "calibratedAgainst": {"type": "array", "items": {"$ref": "#/definitions/expectationId"},
                                  "minItems": 1, "uniqueItems": True},
            "notes": {"type": "string"}},
        "allOf": [
            {"if": {"properties": {"mc": {"const": False}}, "required": ["mc"]},
             "then": {"required": ["fixedReason"]}},
            {"if": {"required": ["fixedReason"]},
             "then": {"properties": {"mc": {"const": False}}, "required": ["mc"]}}],
        "additionalProperties": False},
    "EngineParams": {"type": "object", "propertyNames": {"pattern": "^[A-Za-z][A-Za-z0-9_]*$"},
                     "additionalProperties": {"$ref": "#/definitions/Param"}, "minProperties": 1},
    "CurationRecord": {
        "type": "object",
        "required": ["seq", "checkedOn", "by", "subject", "action", "method", "result"],
        "properties": {
            "seq": {"type": "integer", "minimum": 1},
            "checkedOn": {"$ref": "#/definitions/isoDate"},
            "by": {"type": "string", "minLength": 1}, "subject": {"type": "string", "minLength": 1},
            "action": {"type": "string", "enum": [
                "schema-change", "field-added", "grade-superseded", "citation-added",
                "notes-appended", "engine-only-marked", "fetch-attempt", "finding"]},
            "method": {"type": "string", "minLength": 1},
            "result": {"type": "string", "minLength": 1},
            "previous": {"type": "string"}, "reason": {"type": "string"},
            "refs": {"type": "array", "items": {"type": "string", "minLength": 1},
                     "uniqueItems": True},
            "supersedes": {"type": "integer", "minimum": 1}},
        "additionalProperties": False},
}
_M1_ENGINE_ONLY: dict[str, Any] = {
    "type": "object", "required": ["reason", "citedBy", "recordedOn", "by"],
    "properties": {"reason": {"type": "string", "minLength": 1},
                   "citedBy": {"type": "array", "items": {"$ref": "#/definitions/expectationId"},
                               "uniqueItems": True},
                   "recordedOn": {"$ref": "#/definitions/isoDate"},
                   "by": {"type": "string", "minLength": 1}},
    "additionalProperties": False}
"""The schema extensions the curator's M1 records add (schema.json definitions Param,
EngineParams, CurationRecord, fixedReason, expectationId, isoDate and Evidence.engineOnly),
constraints only. `_linked` inserts each one ONLY where the shipped schema lacks it."""

VALID_CURATION = {"seq": 1, "checkedOn": "2026-10-03", "by": "planted",
                  "subject": "evidence:ev:crowe-1987", "action": "finding",
                  "method": "planted record", "result": "planted record"}


def _kb_citations(kb: dict[str, Any]) -> set[str]:
    """Every evidence id an entity, relation, quantity, conflict or params.json row cites,
    collected by walking the documents (independently of the checker's own walk)."""
    out: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "evidence" and isinstance(val, list):
                    out.update(x for x in val if isinstance(x, str))
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(records(kb, "entities"))
    walk(records(kb, "relations"))
    for row in (kb["params"] or {}).values():
        out.update(x for x in row.get("evidence", []) if isinstance(x, str))
    return out


def _citing(text: str) -> dict[str, list[str]]:
    """Evidence id -> the expectation ids in scenarios.js `text` whose evidence lists it."""
    out: dict[str, list[str]] = {}
    for rec in expectation_records(text):
        for vid in rec.evidence if isinstance(rec.evidence, tuple) else ():
            out.setdefault(vid, []).append(rec.id)
    return out


def _expected_unused(kb: dict[str, Any]) -> set[str]:
    """The unused-evidence warnings the data calls for, counted here, not by the checker."""
    return {v["id"] for v in records(kb, "evidence")
            if v["id"] not in _kb_citations(kb) and "engineOnly" not in v}


@cache
def _shipped_scenarios_text() -> str:
    return SCENARIOS_JS.read_text(encoding="utf-8")


@cache
def _linked_scenarios_text() -> str:
    """scenarios.js with every expectation record carrying its `id`, and the calibration
    records their `role` and `calibrates`, as the reference carries them from model 1.1.0
    (tracker W-13). The shipped text itself when it already does."""
    text = _shipped_scenarios_text()
    if expectation_records(text):
        return text
    extra: dict[str, list[str]] = {}
    for param, eid in CALIBRATED.items():
        extra.setdefault(eid, []).append(param)
    out, sid, n = [], None, 0
    for line in text.splitlines(keepends=True):
        m = re.match(r"\s*id: '([A-Za-z0-9_]+)', title:", line)
        sid = m.group(1) if m else sid
        n = 0 if re.match(r"\s*expects: \[", line) else n
        if sid and re.match(r"\s*\{ metric: ", line):
            n += 1
            eid = f"{sid}/{n:02d}"
            line = line.replace("{ metric: ", f"{{ id: '{eid}', metric: ", 1)
            if eid in extra:
                head, _, tail = line.rpartition(" },")
                names = ", ".join(f"'{p}'" for p in extra[eid])
                line = f"{head}, role: 'calibration', calibrates: [{names}] }},{tail}"
        out.append(line)
    return "".join(out)


def _linked(kb: dict[str, Any]) -> dict[str, Any]:
    """The M1 data, filled in ONLY where the shipped data lacks it: the schema extensions
    (`_M1_DEFINITIONS`, Evidence.engineOnly), `fixedReason` on every `mc: false` row
    (docs/health/01 §4.4), `calibratedAgainst` on the four calibrated rows (§6.3),
    expectation ids with `role`/`calibrates` in the scenarios.js text, and an engineOnly
    marker, with citedBy read from that text, on each evidence record nothing in the
    knowledge base cites. Where the curator's records and the reference's ids have landed
    this changes nothing; before they land it gives every M1 plant a clean start. The
    shipped data itself is held to every rule by test_shipped_kb_passes_every_contract_rule."""
    defs = kb["schema"].get("definitions") if isinstance(kb.get("schema"), dict) else None
    if isinstance(defs, dict):
        for name, node in _M1_DEFINITIONS.items():
            defs.setdefault(name, copy.deepcopy(node))
        ev_props = (defs.get("Evidence") or {}).get("properties")
        if isinstance(ev_props, dict):
            ev_props.setdefault("engineOnly", copy.deepcopy(_M1_ENGINE_ONLY))

    def fill(p: dict[str, Any]) -> None:
        for name, row in p.items():
            if row.get("mc") is False and "fixedReason" not in row and _fixed_reason(name):
                row["fixedReason"] = _fixed_reason(name)
            if name in CALIBRATED and "calibratedAgainst" not in row:
                row["calibratedAgainst"] = [CALIBRATED[name]]
    _params(kb, fill)
    if _linked_scenarios_text() != _shipped_scenarios_text():
        kb["scenarios_js"] = _linked_scenarios_text()
    citing = _citing(kb.get("scenarios_js", _shipped_scenarios_text()))
    for vid in _expected_unused(kb):
        ev(kb, vid)["engineOnly"] = {"reason": "test fill-in: no knowledge-base record cites it",
                                     "citedBy": citing.get(vid, []), "recordedOn": "2026-10-03",
                                     "by": "tests/health_kb_selftest.py"}
    return kb


def _linked_params(fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], None]:
    """A plant: the M1 fields filled in (`_linked`), then `fn` applied to params.json and its
    params.data.js mirror alike."""
    def mutate(kb: dict[str, Any]) -> None:
        _params(_linked(kb), fn)
    return mutate


def _linked_scenarios(old: str, new: str) -> Callable[[dict[str, Any]], None]:
    """A plant: the M1 fields filled in, then one edit to the scenarios.js text."""
    def mutate(kb: dict[str, Any]) -> None:
        _linked(kb)
        text = kb.get("scenarios_js", _shipped_scenarios_text())
        assert old in text, f"plant anchor {old!r} is not in scenarios.js"
        kb["scenarios_js"] = text.replace(old, new, 1)
    return mutate


def _scenarios_text_is(text: Any) -> Callable[[dict[str, Any]], None]:
    """A plant: the M1 fields filled in, then the scenarios.js text replaced by `text`."""
    def mutate(kb: dict[str, Any]) -> None:
        _linked(kb)["scenarios_js"] = text
    return mutate


def _linked_kb(fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], None]:
    """A plant: the M1 fields filled in, then `fn` applied to the whole KB."""
    def mutate(kb: dict[str, Any]) -> None:
        fn(_linked(kb))
    return mutate


def _engine_only(fn: Callable[[dict[str, Any], dict[str, Any]], Any]
                 ) -> Callable[[dict[str, Any]], None]:
    """A plant: the M1 fields filled in, ENGINE_ONLY marked engine-only with the citedBy the
    registry gives it (the curator's record, where it has landed), then `fn(kb, marker)`."""
    def mutate(kb: dict[str, Any]) -> None:
        _linked(kb)
        citing = _citing(kb.get("scenarios_js", _shipped_scenarios_text()))
        marker = ev(kb, ENGINE_ONLY).setdefault("engineOnly", {
            "reason": "planted", "citedBy": [], "recordedOn": "2026-10-03", "by": "planted"})
        marker["citedBy"] = list(citing.get(ENGINE_ONLY, []))
        fn(kb, marker)
    return mutate


def _curation(fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], None]:
    """A plant: the M1 fields filled in, a valid curation record appended to
    VERIFICATION_LOG.json curationRecords, then `fn` applied to that record."""
    def mutate(kb: dict[str, Any]) -> None:
        recs = _linked(kb)["verification"].setdefault("curationRecords", [])
        rec = dict(VALID_CURATION, seq=len(recs) + 1)
        recs.append(rec)
        fn(rec)
    return mutate


# (expected code, what was planted, mutation). Every plant must fire its code and NO
# other (HREQ-V-17): a cascade is a failure of the plant or of the checker.
Plant = tuple[str, str, Callable[[dict[str, Any]], Any]]

PLANTS: list[Plant] = [
    # -- schema layer: document / record structure -------------------------------------
    ("file-shape", "VERIFICATION_LOG records is an object, not an array",
     lambda kb: kb["verification"].update(records={})),
    ("file-shape", "VERIFICATION_LOG document is an array, not an object",
     lambda kb: kb.update(verification=[])),
    ("file-shape", "entities.json entities is a string, not an array",
     lambda kb: kb["entities"].update(entities="none")),
    ("file-shape", "relations.json has no relations array",
     lambda kb: kb["relations"].pop("relations")),
    ("unknown-key", "entity carries an undeclared key",
     lambda kb: ent(kb, BODY).update(colour="red")),
    ("unknown-key", "quantity carries an undeclared key",
     lambda kb: nephron_q(kb).update(confidence=0.9)),
    ("unknown-key", "conflict carries an undeclared key",
     lambda kb: rel(kb, GFR)["quantity"]["conflicts"][0].update(weight=1)),
    ("unknown-key", "evidence.verification carries an undeclared key",
     lambda kb: ev(kb, TEXTBOOK)["verification"].update(reviewer="x")),
    ("unknown-key", "externalIds names an undeclared registry",
     lambda kb: ent(kb, BODY)["externalIds"].update(MESH="D000001")),
    ("unknown-key", "relations.json carries an undeclared top-level key",
     lambda kb: kb["relations"].update(comment="x")),
    ("unknown-key", "schema.json drops a property the data uses (Entity.system): the edit is "
                    "enforced on every entity carrying it",
     lambda kb: _schema_def(kb, "Entity", "properties").pop("system")),
    ("missing-field", "relation without type",
     lambda kb: rel(kb, NEPHRON).pop("type")),
    ("missing-field", "evidence without notes",
     lambda kb: ev(kb, TEXTBOOK).pop("notes")),
    ("missing-field", "quantity without unit",
     lambda kb: nephron_q(kb).pop("unit")),
    ("missing-field", "evidence.verification without retraction",
     lambda kb: ev(kb, TEXTBOOK)["verification"].pop("retraction")),
    ("missing-field", "an appended VERIFICATION_LOG record without resolved",
     lambda kb: records(kb, "verification").append(
         {k: v for k, v in log_record(kb, BODY).items() if k != "resolved"})),
    ("missing-field", "schema.json makes a field required that one entity lacks",
     lambda kb: (_schema_def(kb, "Entity")["required"].append("system"),
                 [e.update(system="x") for e in records(kb, "entities")
                  if e["id"] != BODY and "system" not in e],
                 ent(kb, BODY).pop("system", None))),
    ("wrong-type", "entity name is a number",
     lambda kb: ent(kb, BODY).update(name=5)),
    ("wrong-type", "entity type is an array (crashed report.summary before)",
     lambda kb: ent(kb, BODY).update(type=["organism"])),
    ("wrong-type", "a relations.json record is null",
     lambda kb: records(kb, "relations").append(None)),
    ("wrong-type", "relation conflicts is a string",
     lambda kb: rel(kb, "rel:expr:cyp11b2-zg").update(conflicts="x")),
    ("wrong-type", "evidence verification.titleMatch is a string",
     lambda kb: ev(kb, PRIMARY)["verification"].update(titleMatch="yes")),
    ("wrong-type", "entities.json kbVersion is a number",
     lambda kb: kb["entities"].update(kbVersion=1)),
    ("wrong-type", "entity synonyms is a string",
     lambda kb: ent(kb, BODY).update(synonyms="organism")),
    ("wrong-type", "entity scale is a string",
     lambda kb: ent(kb, LEAF).update(scale="7")),
    ("wrong-type", "entity parent is a number (matches neither anyOf form)",
     lambda kb: ent(kb, HEART).update(parent=3)),
    ("wrong-type", "quantity value is a string",
     lambda kb: nephron_q(kb).update(value="950000")),
    ("wrong-type", "quantity value is NaN (not a JSON number)",
     lambda kb: nephron_q(kb).update(value=float("nan"))),
    ("wrong-type", "quantity value is Infinity (not a JSON number)",
     lambda kb: nephron_q(kb).update(value=float("inf"))),
    ("wrong-type", "a VERIFICATION_LOG record's entity is a number",
     lambda kb: records(kb, "verification").append(dict(log_record(kb, BODY), entity=5))),
    ("wrong-type", "an extra 'evidence' array in entities.json is validated too (year is a "
                   "string)",
     lambda kb: kb["entities"].update(evidence=[dict(copy.deepcopy(ev(kb, PRIMARY)),
                                                     year="1976")])),
    ("empty", "entity name is empty",
     lambda kb: ent(kb, BODY).update(name="")),
    ("empty", "entity cites no evidence",
     lambda kb: ent(kb, BODY).update(evidence=[])),
    ("empty", "quantity unit is empty",
     lambda kb: nephron_q(kb).update(unit="")),
    ("empty", "UniProt array is empty",
     lambda kb: ent(kb, BODY)["externalIds"].update(UniProt=[])),
    ("empty", "a new entity's externalIds object is empty",
     lambda kb: _new_entity(kb, "molecule:planted-empty-xids", externalIds={})),
    ("not-unique", "entity synonym repeated",
     lambda kb: ent(kb, BODY)["synonyms"].append(ent(kb, BODY)["synonyms"][0])),
    ("not-unique", "entity evidence id repeated",
     lambda kb: ent(kb, BODY)["evidence"].append(ent(kb, BODY)["evidence"][0])),
    ("bad-enum", "entity type outside the enum",
     lambda kb: ent(kb, BODY).update(type="widget")),
    ("bad-enum", "relation type outside the enum",
     lambda kb: rel(kb, NEPHRON).update(type="inhibits")),
    ("bad-enum", "evidence kind outside the enum",
     lambda kb: ev(kb, TEXTBOOK).update(kind="website")),
    ("bad-enum", "evidence sourceType outside the enum",
     lambda kb: ev(kb, TEXTBOOK).update(sourceType="blog")),
    ("bad-enum", "evidence defaultGrade outside the enum",
     lambda kb: ev(kb, TEXTBOOK).update(defaultGrade="A+")),
    ("bad-enum", "quantity grade outside the enum",
     lambda kb: nephron_q(kb).update(grade="F")),
    ("out-of-bounds", "entity scale 8",
     lambda kb: ent(kb, LEAF).update(scale=8)),
    ("out-of-bounds", "evidence year 1700",
     lambda kb: ev(kb, TEXTBOOK).update(year=1700)),
    ("out-of-bounds", "schema.json tightens Entity.name to maxLength 5 (an unmirrored node "
                      "before: only a warn)",
     lambda kb: _schema_def(kb, "Entity", "properties", "name").update(maxLength=5)),
    ("range-shape", "quantity range has one number",
     lambda kb: nephron_q(kb).update(range=[1])),
    ("range-shape", "conflict range has three numbers",
     lambda kb: next(c for c in rel(kb, GFR)["quantity"]["conflicts"] if "range" in c)
     .update(range=[1, 2, 3])),
    ("range-shape", "quantity range is a string",
     lambda kb: nephron_q(kb).update(range="wide")),
    # -- schema layer: patterns ----------------------------------------------------------
    ("id-pattern", "relation from is not a well-formed id",
     lambda kb: rel(kb, NEPHRON).update({"from": "Tissue:Nephron"})),
    ("id-pattern", "entity parent is not a well-formed id",
     lambda kb: ent(kb, HEART).update(parent="Cardio System")),
    ("evidence-id-pattern", "entity cites a malformed evidence id",
     lambda kb: ent(kb, BODY)["evidence"].append("ev:Bad_Id")),
    ("evidence-id-pattern", "an evidence record id is malformed",
     lambda kb: _new_evidence(kb, "EV:planted")),
    ("relation-id-pattern", "relation id has a space and capitals",
     lambda kb: rel(kb, NEPHRON).update(id="rel:Bad Id")),
    ("external-id-pattern", "CL id with too few digits",
     lambda kb: ent(kb, BODY)["externalIds"].update(CL="CL:12")),
    ("external-id-pattern", "UniProt array item malformed",
     lambda kb: ent(kb, BODY)["externalIds"].update(UniProt=["bad1"])),
    ("external-id-pattern", "CL id in fullwidth digits (ECMA \\d is ASCII)",
     lambda kb: ent(kb, BODY)["externalIds"].update(CL="CL:０００００１２")),
    ("external-id-pattern", "ChEBI id in Arabic-Indic digits",
     lambda kb: ent(kb, BODY)["externalIds"].update(ChEBI="CHEBI:١٥٣٧٧")),
    ("doi-pattern", "doi with a 2-digit registrant",
     lambda kb: ev(kb, PRIMARY).update(doi="10.12/x")),
    ("doi-pattern", "doi containing U+00A0 (ECMA \\S excludes it; re.ASCII \\S did not)",
     lambda kb: ev(kb, PRIMARY).update(doi="10.1016/s0889 8529")),
    ("doi-pattern", "doi containing U+3000",
     lambda kb: ev(kb, PRIMARY).update(doi="10.1016/s0889　8529")),
    ("doi-pattern", "doi containing U+FEFF",
     lambda kb: ev(kb, PRIMARY).update(doi="10.1016/﻿s0889")),
    ("pmid-pattern", "pmid with a prefix",
     lambda kb: ev(kb, PRIMARY).update(pmid="PMID:1262438")),
    ("url-https", "plain http url",
     lambda kb: ev(kb, PRIMARY).update(url="http://doi.org/x")),
    ("date-pattern", "accessed with slashes",
     lambda kb: ev(kb, PRIMARY).update(accessed="2026/10/01")),
    ("date-pattern", "verification.checkedOn as words",
     lambda kb: ev(kb, PRIMARY)["verification"].update(checkedOn="Oct 2026")),
    ("date-pattern", "accessed with a trailing newline (Python `$` would accept it)",
     lambda kb: ev(kb, PRIMARY).update(accessed="2026-10-01\n")),
    ("date-pattern", "VERIFICATION_LOG checkedOn as a word",
     lambda kb: log_record(kb, BODY).update(checkedOn="yesterday")),
    ("pattern-mismatch", "schema.json adds a pattern to Entity.name that one name breaks",
     lambda kb: (_schema_def(kb, "Entity", "properties", "name").update(pattern="^[^<>]*$"),
                 ent(kb, BODY).update(name="<b>Whole body</b>"))),
    ("doi-required", "kind doi without a doi",
     lambda kb: ev(kb, PRIMARY).pop("doi")),
    ("pmid-required", "kind pmid without a pmid",
     lambda kb: (ev(kb, PRIMARY).update(kind="pmid"), ev(kb, PRIMARY).pop("pmid"))),
    ("schema-violation", "schema.json adds a `not` to Entity that one entity matches",
     lambda kb: _schema_def(kb, "Entity").update(
         {"not": {"properties": {"name": {"const": "Whole body"}}, "required": ["name"]}})),
    ("schema-drift", "schema grade enum gains a value (the checker's ranking is stale)",
     lambda kb: _schema_def(kb, "grade", "enum").append("F-anecdote")),
    ("schema-drift", "schema sourceType enum gains a value the §3.2 mapping does not cover",
     lambda kb: _schema_def(kb, "Evidence", "properties", "sourceType", "enum").append("blog")),
    ("schema-drift", "schema uses a keyword the checker does not evaluate (format)",
     lambda kb: _schema_def(kb, "Evidence", "properties", "accessed").update(format="date")),
    ("schema-drift", "schema $ref names no definition",
     lambda kb: _schema_def(kb, "Entity", "properties").update(system={"$ref": "#/nope"})),
    ("schema-drift", "schema pattern is not translatable ECMA-262 (\\p{L} needs the u flag)",
     lambda kb: _schema_def(kb, "Relation", "properties", "notes").update(pattern="\\p{L}")),
    ("schema-drift", "schema declares JSON Schema 2020-12",
     lambda kb: kb["schema"].update({"$schema": "https://json-schema.org/draft/2020-12/schema"})),
    ("schema-drift", "schema's fixedReason enum admits a fourth reason HREQ-U-01 does not",
     lambda kb: _schema_def(kb).update(
         fixedReason={"type": "string", "enum": [*FIXED_REASONS, "convenience"]})),
    ("schema-drift", "schema's expectationId pattern admits ids calibration-link refuses "
                     "(three-digit positions; every shipped id still matches it)",
     lambda kb: _schema_def(kb).update(
         expectationId={"type": "string", "pattern": "^[A-Za-z0-9_]+/[0-9]{2,3}$"})),
    ("schema-drift", "schema's Param stops declaring mc, a field fixed-reason reads (its "
                     "additionalProperties relaxed, so no row breaks the edit)",
     _linked_kb(lambda kb: (_schema_def(kb, "Param", "properties").pop("mc"),
                            _schema_def(kb, "Param").update(additionalProperties=True)))),
    ("schema-drift", "schema's Evidence.engineOnly stops declaring citedBy, the field "
                     "engine-only-evidence reads",
     _linked_kb(lambda kb: (_schema_def(kb, "Evidence", "properties", "engineOnly",
                                        "properties").pop("citedBy"),
                            _schema_def(kb, "Evidence", "properties", "engineOnly").update(
                                additionalProperties=True)))),
    # -- cross-file ------------------------------------------------------------------------
    ("duplicate-id", "an evidence record appears twice",
     lambda kb: records(kb, "evidence").append(copy.deepcopy(ev(kb, "ev:thompson-1986")))),
    ("duplicate-id", "an entity appears twice",
     lambda kb: records(kb, "entities").append(copy.deepcopy(ent(kb, LEAF)))),
    ("duplicate-id", "a relation (one without a quantity) appears twice",
     lambda kb: records(kb, "relations").append(copy.deepcopy(
         next(r for r in records(kb, "relations") if "quantity" not in r)))),
    ("dangling-evidence", "entity cites an evidence id with no record",
     lambda kb: ent(kb, BODY)["evidence"].append("ev:does-not-exist")),
    ("dangling-evidence", "quantity cites an evidence id with no record",
     lambda kb: nephron_q(kb)["evidence"].append("ev:does-not-exist")),
    ("dangling-evidence", "conflict cites an evidence id with no record",
     lambda kb: rel(kb, GFR)["quantity"]["conflicts"][0]["evidence"].append("ev:does-not-exist")),
    ("dangling-endpoint", "relation to names no entity",
     lambda kb: rel(kb, NEPHRON).update(to="organ:nonexistent")),
    ("dangling-parent", "entity parent names no entity",
     lambda kb: ent(kb, HEART).update(parent="system:nonexistent")),
    ("self-relation", "relation from and to are the same entity",
     lambda kb: rel(kb, NEPHRON).update(to=rel(kb, NEPHRON)["from"])),
    ("parent-scale", "entity placed above its parent's scale",
     lambda kb: ent(kb, HEART).update(scale=0)),
    ("parent-cycle", "entity is its own parent",
     lambda kb: ent(kb, LEAF).update(parent=LEAF)),
    ("parent-cycle", "two same-scale entities are each other's parent", _two_cycle),
    ("engine-params-unavailable", "params.json is null",
     lambda kb: kb.update(params=None)),
    ("engine-params-unavailable", "params.json could not be parsed",
     lambda kb: (kb.update(params=None),
                 kb["load_errors"].update(params="params.json: invalid JSON (planted)"))),
    ("engine-param-unknown", "engineParam names no params.json row",
     lambda kb: rel(kb, ADH_SLOPE)["quantity"].update(engineParam="no_such_param")),
    ("engine-param-mismatch", "mirrored value differs from params.json",
     lambda kb: rel(kb, ADH_SLOPE)["quantity"].update(value=0.36)),
    ("params-mirror", "params.data.js differs from params.json in one row",
     lambda kb: kb["params_js"]["adh_slope"].update(value=0.36)),
    ("params-mirror", "params.data.js lacks a row params.json has",
     lambda kb: kb["params_js"].pop(FREE_PARAM)),
    ("params-mirror", "params.data.js could not be parsed",
     lambda kb: kb["load_errors"].update(params_js="params.data.js: invalid JSON (planted)")),
    ("params-mirror-unchecked", "no params.data.js found to compare",
     lambda kb: (kb.update(params_js=None), kb["paths"].update(params_js=None))),
    ("param-field", "params.json row grade outside the enum",
     lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(grade="F"))),
    ("param-field", "params.json row without a unit",
     lambda kb: _params(kb, lambda p: p[FREE_PARAM].pop("unit"))),
    ("param-field", "params.json row with a one-number range",
     lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(range=[12]))),
    ("param-field", "params.json row with no evidence",
     lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(evidence=[]))),
    ("param-field", "params.json row value is a string",
     lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(value="14"))),
    ("param-evidence-unresolved", "params.json row cites an unknown evidence id",
     lambda kb: _params(kb, lambda p: p[FREE_PARAM]["evidence"].append("ev:nope"))),
    # fixed-reason (HREQ-U-01): every way a row comes to be held fixed, and the converse.
    ("fixed-reason", "a sampled row turned mc: false with no fixedReason",
     _linked_params(lambda p: p[FREE_PARAM].update(mc=False))),
    ("fixed-reason", "an mc: false row whose fixedReason is not one of the three",
     _linked_params(lambda p: p["na_normal_low"].update(fixedReason="convenience"))),
    ("fixed-reason", "a sampled row carrying a fixedReason",
     _linked_params(lambda p: p[FREE_PARAM].update(fixedReason="scenario-condition"))),
    ("fixed-reason", "a single-point range held fixed by samplingMode without mc: false",
     _linked_params(lambda p: p[FREE_PARAM].update(range=[14.0, 14.0]))),
    ("fixed-reason", "mc is the string 'false' (both engines sample the row)",
     _linked_params(lambda p: p["na_normal_low"].update(mc="false"))),
    # calibration-link (HREQ-E-10): both sides of the link, and the notes that betray one.
    ("calibration-link", "a row whose notes say it was calibrated carries no calibratedAgainst",
     _linked_params(lambda p: p[FREE_PARAM].update(
         notes=p[FREE_PARAM]["notes"] + " Calibrated against the chronic salt band."))),
    ("calibration-link", "a calibrated row lost its calibratedAgainst",
     _linked_params(lambda p: p["map_auto_tau_h"].pop("calibratedAgainst"))),
    ("calibration-link", "a calibratedAgainst entry that is not <scenario_id>/<NN>",
     _linked_params(lambda p: p["pn_gain"].update(calibratedAgainst=["chronic_high_salt_30d:0"]))),
    ("calibration-link", "a calibratedAgainst entry naming no registered expectation",
     _linked_params(lambda p: p["map_auto_tau_h"]["calibratedAgainst"].append(
         "chronic_high_salt_30d/09"))),
    ("calibration-link", "calibratedAgainst names an expectation whose calibrates omits the row",
     _linked_params(lambda p: p[FREE_PARAM].update(calibratedAgainst=["drink_water_1L/01"]))),
    ("calibration-link", "an expectation's calibrates names a row whose calibratedAgainst "
                         "omits it",
     _linked_scenarios("calibrates: ['map_auto_tau_h']",
                       f"calibrates: ['map_auto_tau_h', '{FREE_PARAM}']")),
    ("calibration-link", "an expectation's calibrates names a row whose calibratedAgainst names "
                         "only other expectations",
     _linked_scenarios("calibrates: ['map_vol_exp', 'pn_gain', 'aldo_vol_exp']",
                       "calibrates: ['map_vol_exp', 'pn_gain', 'aldo_vol_exp', 'map_auto_tau_h']")),
    ("calibration-link", "an expectation's calibrates names no params.json row",
     _linked_scenarios("calibrates: ['map_auto_tau_h']",
                       "calibrates: ['map_auto_tau_h', 'no_such_param']")),
    ("calibration-link", "an expectation's calibrates is not a literal array",
     _linked_scenarios("calibrates: ['map_auto_tau_h']", "calibrates: TUNED")),
    ("expectations-unavailable", "scenarios.js registers no expectation id",
     _scenarios_text_is("export const SCENARIOS = Object.freeze({});\n")),
    ("expectations-unavailable", "scenarios.js cannot be tokenised (an unterminated string)",
     _scenarios_text_is("const x = { id: 'baseline/01 };\n")),
    ("expectations-unavailable", "no scenarios.js text at all",
     _scenarios_text_is(None)),
    ("calibration-link", "scenarios.js registers an expectation id twice",
     _linked_scenarios("{ id: 'no_water_24h/04', ", "{ id: 'no_water_24h/03', ")),
    # engine-only-evidence (W-6): the marker that exempts a record from unused-evidence is a
    # claim about the registry and the knowledge base, held to both.
    ("engine-only-evidence", "an engine-only record a relation cites",
     _engine_only(lambda kb, m: rel(kb, NEPHRON)["evidence"].append(ENGINE_ONLY))),
    ("engine-only-evidence", "an engine-only record a params.json row cites",
     _engine_only(lambda kb, m: _params(kb, lambda p: p[FREE_PARAM]["evidence"].append(
         ENGINE_ONLY)))),
    ("engine-only-evidence", "engineOnly.citedBy names an expectation scenarios.js does not "
                             "register",
     _engine_only(lambda kb, m: m["citedBy"].append("drink_water_1L/09"))),
    ("engine-only-evidence", "engineOnly.citedBy names an expectation that does not cite the "
                             "record",
     _engine_only(lambda kb, m: m["citedBy"].append("drink_water_1L/05"))),
    ("engine-only-evidence", "engineOnly.citedBy omits an expectation that cites the record",
     _engine_only(lambda kb, m: m["citedBy"].pop(0))),
    ("pattern-mismatch", "an engineOnly.citedBy entry that is not <scenario_id>/<NN> (the "
                         "schema's to report; the registry comparison is skipped, not repeated)",
     _engine_only(lambda kb, m: m["citedBy"].append("drink_water_1L-02"))),
    # params.json against the schema's EngineParams, and the curation log against
    # CurationRecord, where the schema declares them (the curator's M1 extensions).
    ("unknown-key", "a params.json row carries a misspelt field (calibratedAgaints)",
     _linked_params(lambda p: p[FREE_PARAM].update(calibratedAgaints=["drink_water_1L/01"]))),
    ("unknown-key", "a params.json row name that is not an identifier (EngineParams "
                    "propertyNames)",
     _linked_params(lambda p: p.update({"V ecf 0": p.pop(FREE_PARAM)}))),
    ("missing-field", "a params.json row without its description (EngineParams)",
     _linked_params(lambda p: p[FREE_PARAM].pop("description"))),
    ("wrong-type", "a params.json row's notes is a number (EngineParams)",
     _linked_params(lambda p: p[FREE_PARAM].update(notes=3))),
    ("not-unique", "a params.json row cites the same evidence twice (EngineParams; no "
                   "hand-written rule reads it)",
     _linked_params(lambda p: p[FREE_PARAM]["evidence"].append(p[FREE_PARAM]["evidence"][0]))),
    ("bad-enum", "a curation record's action outside the enum (CurationRecord)",
     _curation(lambda r: r.update(action="edited"))),
    ("missing-field", "a curation record without seq (CurationRecord)",
     _curation(lambda r: r.pop("seq"))),
    ("date-pattern", "a curation record's checkedOn is not YYYY-MM-DD (definitions/isoDate)",
     _curation(lambda r: r.update(checkedOn="3 Oct 2026"))),
    ("file-shape", "VERIFICATION_LOG curationRecords is an object, not an array",
     _linked_kb(lambda kb: kb["verification"].update(curationRecords={}))),
    ("external-id-unverified", "log record says resolved: false",
     lambda kb: log_record(kb, BODY).update(resolved=False)),
    ("external-id-unverified", "externalIds value has no log record",
     lambda kb: ent(kb, BODY)["externalIds"].update(CL="CL:0000000")),
    ("external-id-unverified", "a LATER appended log record says resolved: false",
     lambda kb: records(kb, "verification").append(
         dict(log_record(kb, BODY), resolved=False, checkedOn="2026-10-02"))),
    ("entity-removed", "log records name an entity entities.json does not hold",
     lambda kb: records(kb, "verification").append(
         dict(log_record(kb, BODY), entity="organ:nonexistent"))),
    ("orphan-verification", "log record for an id the entity no longer carries",
     lambda kb: records(kb, "verification").append(
         dict(log_record(kb, BODY), id="UBERON:9999999"))),
    ("summary-too-long", "61-word summary",
     lambda kb: ent(kb, BODY).update(summary=_words(61))),
    ("quote-too-long", "26-word quote",
     lambda kb: ev(kb, PRIMARY).update(quote=_words(26))),
    ("conflict-unsourced-note", "empty-evidence conflict whose note does not say unsourced",
     _unsourced("Different cohort.")),
    ("conflict-unsourced-note", "the marker negated: 'Not unsourced: Smith 2010 table 2.'",
     _unsourced("Not unsourced: Smith 2010 table 2.")),
    ("conflict-unsourced-note", "a lookalike: 'No citation issues; from Smith 2010.'",
     _unsourced("No citation issues; from Smith 2010.")),
    ("conflict-unsourced-note", "an empty note",
     _unsourced("")),
    ("range-order", "quantity range reversed",
     lambda kb: nephron_q(kb)["range"].reverse()),
    ("range-order", "conflict range reversed",
     lambda kb: next(c for c in rel(kb, GFR)["quantity"]["conflicts"] if "range" in c)
     ["range"].reverse()),
    ("range-order", "params.json row range reversed",
     lambda kb: _params(kb, lambda p: p[FREE_PARAM]["range"].reverse())),
    ("value-outside-range", "quantity value above its range",
     lambda kb: nephron_q(kb).update(value=1e9)),
    ("value-outside-range", "params.json row value below its range",
     lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(value=1))),
    ("grade-ceiling", "quantity graded A-meta citing only B-textbook evidence",
     lambda kb: nephron_q(kb).update(grade="A-meta")),
    ("grade-ceiling", "params.json row graded A-primary citing only a textbook",
     lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(grade="A-primary"))),
    ("evidence-grading-missing", "evidence record without sourceType",
     lambda kb: ev(kb, TEXTBOOK).pop("sourceType")),
    ("evidence-grading-missing", "evidence record without defaultGrade",
     lambda kb: ev(kb, TEXTBOOK).pop("defaultGrade")),
    ("default-grade-mapping", "a textbook's defaultGrade raised to A-meta",
     lambda kb: ev(kb, TEXTBOOK).update(defaultGrade="A-meta")),
    ("evidence-unverified", "a cited evidence record without verification",
     lambda kb: ev(kb, PRIMARY).pop("verification")),
    ("evidence-retracted", "a cited evidence record reported retracted",
     lambda kb: ev(kb, PRIMARY)["verification"].update(retraction="RETRACTED 2020 (notice)")),
    ("evidence-title-mismatch", "a cited evidence record with titleMatch: false",
     lambda kb: ev(kb, PRIMARY)["verification"].update(titleMatch=False)),
    ("doi-url", "kind doi, url on example.com",
     lambda kb: ev(kb, PRIMARY).update(url="https://example.com/paper")),
    # -- warnings and info -------------------------------------------------------------------
    ("unused-evidence", "an evidence record nothing cites",
     lambda kb: _new_evidence(kb, "ev:planted-unused")),
    ("meta-mismatch", "relations.json kbVersion differs",
     lambda kb: kb["relations"].update(kbVersion="1.0.1")),
    ("meta-mismatch", "evidence.json curator differs (curator alone)",
     lambda kb: kb["evidence"].update(curator="planted")),
    ("meta-mismatch", "relations.json generated differs (generated alone)",
     lambda kb: kb["relations"].update(generated="planted")),
    ("e-assumption-share", "one more quantity graded E-assumption changes the reported share",
     lambda kb: nephron_q(kb).update(grade="E-assumption")),
]


def _plant_kb(code: str) -> dict[str, Any]:
    """A fresh copy of the shipped KB with the first plant for `code` applied."""
    kb = base_kb()
    next(p for p in PLANTS if p[0] == code)[2](kb)
    return kb


def _section6_rules() -> list[str]:
    text = DOC_03.read_text(encoding="utf-8")
    body = text.split("## 6. Knowledge-Base Gates", 1)[1].split("\n## 7.", 1)[0]
    names: list[str] = []
    for line in body.splitlines():
        if line.startswith("| `"):
            names += re.findall(r"`([a-z-]+)`", line.split("|")[1])
    return names


def _run_cli(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "health.cli", *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=300,
                          check=False)


def _kb_copy(tmp: Path, name: str) -> Path:
    dest = tmp / name
    shutil.copytree(DATA_DIR, dest)
    return dest


def _edit(path: Path, fn: Callable[[Any], Any]) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    fn(doc)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests: the shipped KB and the rule registry
# ---------------------------------------------------------------------------
def test_shipped_kb_passes_every_contract_rule() -> None:
    kb = base_kb()
    findings = check_kb(kb)
    by_sev = Counter(f.severity for f in findings)
    errors = [f for f in findings if f.severity == "error"]
    print(f"      shipped KB: {by_sev.get('error', 0)} error, {by_sev.get('warn', 0)} warn, "
          f"{by_sev.get('info', 0)} info  (params from {kb['paths']['params']}; params.data.js "
          f"from {kb['paths']['params_js']})")
    for f in findings:
        if f.severity != "error":
            print(f"      {f}")
    check("shipped KB: zero error findings", not errors, "\n        ".join(map(str, errors)))
    check("shipped KB: params.json and params.data.js were found, so the engine mirrors and "
          "params-mirror were really compared",
          isinstance(kb["params"], dict) and isinstance(kb["params_js"], dict)
          and not {"engine-params-unavailable", "params-mirror-unchecked"}
          & {f.code for f in findings})
    warn = sorted((f.code, f.where) for f in findings if f.severity == "warn")
    unused = _expected_unused(kb)
    expected = sorted(("unused-evidence", f"evidence.json[{v}]") for v in unused)
    check(f"shipped KB: the warning set is pinned -- exactly the {len(unused)} unused-evidence "
          "record(s) an independent walk of the documents finds (neither cited by a knowledge-"
          "base record or params.json row nor marked engineOnly), no other code (a checker that "
          "stopped counting params.json citations gave 12)", warn == expected, f"got {warn}")
    check(f"shipped KB: every unused record is one of the six V1 shipped uncited (W-6); none "
          f"is new ({len(unused)} of 6 left: 6 before the curator's M1 records, 0 after)",
          unused <= V1_UNUSED, str(sorted(unused - V1_UNUSED)))
    raw = json.loads(Path(kb["paths"]["params"]).read_text(encoding="utf-8"))
    n_e = sum(1 for row in raw.values() if row["grade"] == "E-assumption")
    want = f"{n_e} of {len(raw)} engine params ({100.0 * n_e / len(raw):.1f}%)"
    info = sorted(f.message for f in findings if f.severity == "info")
    check(f"shipped KB: info is exactly the two E-assumption shares (5 of 20 quantities; {want}, "
          "counted here from params.json)",
          len(info) == 2 and any("5 of 20 quantities (25.0%)" in m for m in info)
          and any(want in m for m in info), str(info))
    check("shipped KB: every finding's code and severity are registered",
          all(f.code in RULES and RULES[f.code][0] == f.severity for f in findings))
    shipped = {name: (DATA_DIR / name).stat().st_size for name in
               ("entities.json", "relations.json", "evidence.json", "VERIFICATION_LOG.json",
                "schema.json")}
    check("shipped KB: all five data files are packaged", all(shipped.values()), str(shipped))
    s = summary(kb)
    check("shipped KB: record counts are the curated 107 / 203 / 57 / 154",
          (s["counts"]["entities"], s["counts"]["relations"], s["counts"]["evidence"],
           s["counts"]["verificationRecords"]) == (107, 203, 57, 154), str(s["counts"]))


def test_every_contract_rule_fires_on_a_planted_violation() -> None:
    base = baseline()
    covered: set[str] = set()
    for code, what, mutate in PLANTS:
        kb = base_kb()
        try:
            mutate(kb)
            new = [f for f in check_kb(kb) if f not in base]
        except Exception as exc:  # noqa: BLE001 - a crash on bad data is the failure reported
            check(f"plant {code}: {what} -- checker did not raise", False, repr(exc))
            continue
        codes = {f.code for f in new}
        fired = [f for f in new if f.code == code]
        check(f"plant {code}: {what} -- fires, and nothing else does",
              bool(fired) and codes == {code}, f"new findings: {[str(f) for f in new][:6]}")
        check(f"plant {code}: severity is {RULES[code][0]}",
              bool(fired) and all(f.severity == RULES[code][0] for f in fired),
              str([f.severity for f in fired]))
        if fired:
            covered.add(code)
    missing = sorted(set(RULES) - covered)
    check(f"every one of the {len(RULES)} rule codes has a planted violation that fires alone",
          not missing, f"rules with no firing plant: {missing}")
    planted = {p[0] for p in PLANTS}
    check("no plant names a code the checker does not register",
          planted <= set(RULES), str(sorted(planted - set(RULES))))
    share = [f for f in check_kb(_plant_kb("e-assumption-share")) if f.code == "e-assumption-share"
             and f not in base]
    check("plant e-assumption-share: the message carries the new count and percentage",
          bool(share) and "6 of 20 quantities (30.0%)" in share[0].message,
          str([str(f) for f in share]))


def test_every_documented_rule_is_implemented_or_deferred() -> None:
    """HREQ-V-16: check.py SHALL enforce every rule in the docs/health/03 §6 table and SHALL
    block a merge on any violation. The table is parsed here at run time, so a rule the
    document names and the code never implemented cannot hide behind a closed RULES list."""
    doc_rules = _section6_rules()
    check("03 §6 table parsed (16 rules)", len(doc_rules) == 16, str(doc_rules))
    neither = [r for r in doc_rules if not DOCUMENTED_RULES.get(r) and r not in DEFERRED_RULES]
    check("every 03 §6 rule has an implementing code or a DEFERRED_RULES entry", not neither,
          f"neither implemented nor deferred: {neither}")
    stale = sorted((set(DOCUMENTED_RULES) | set(DEFERRED_RULES)) - set(doc_rules))
    check("DOCUMENTED_RULES and DEFERRED_RULES name only rules the 03 §6 table has", not stale,
          str(stale))
    unknown = sorted({c for cs in DOCUMENTED_RULES.values() for c in cs} - set(RULES))
    check("every code DOCUMENTED_RULES maps to is registered in RULES", not unknown, str(unknown))
    nonblocking = sorted(c for cs in DOCUMENTED_RULES.values() for c in cs
                         if RULES.get(c, ("?",))[0] != "error")
    check("every code implementing a 03 §6 rule is error-severity (blocks a merge)",
          not nonblocking, f"not error: {nonblocking}")
    check("every DEFERRED_RULES entry says what it is waiting for",
          all(isinstance(why, str) and len(why) > 20 for why in DEFERRED_RULES.values()),
          str(DEFERRED_RULES))
    print(f"      deferred: {sorted(DEFERRED_RULES)}")
    # Behavioural plants for rules the first suite lacked (the rigor review's probes).
    got = new_findings(lambda kb: nephron_q(kb).update(grade="A-meta"))
    check("grade-ceiling: A-meta on a quantity citing only B-textbook evidence is an error",
          any(f.severity == "error" and f.code == "grade-ceiling" for f in got),
          str([str(f) for f in got]))

    def gut(kb: dict[str, Any]) -> None:
        kb["entities"]["entities"], kb["relations"]["relations"] = [], []
    got = new_findings(gut)
    check("append-only (partial): deleting every entity and relation is an error "
          "(entity-removed)", any(f.severity == "error" for f in got),
          str(sorted({(f.code, f.severity) for f in got})))
    js = parse_params_js(PARAMS_DATA_JS.read_text(encoding="utf-8"))
    check("params-mirror holds on the vendored data (params.data.js == params.json)",
          js == base_kb()["params"])


def _linked_new(mutate: Callable[[dict[str, Any]], Any]) -> list[Finding]:
    """The findings `mutate` adds to the shipped KB with its M1 fields filled in (`_linked`)."""
    kb = _linked(base_kb())
    before = set(check_kb(kb))
    mutate(kb)
    return [f for f in check_kb(kb) if f not in before]


def _plants(code: str) -> list[Plant]:
    return [p for p in PLANTS if p[0] == code]


def test_fixed_reason_is_required_on_every_row_monte_carlo_holds_fixed() -> None:
    """HREQ-U-01 (BUG-20261003-186): a parameter is held fixed in Monte Carlo only as a
    scenario condition, a classification threshold or an index-definition constant, and the
    reason is a field. Both engines hold a row fixed when `mc` is exactly false or its range
    is a single point (samplingMode), so both ways in need the field; a sampled row with a
    reason is a contradiction."""
    check("fixed-reason is enforced by an error code, not deferred",
          DOCUMENTED_RULES.get("fixed-reason") == ("fixed-reason",)
          and RULES["fixed-reason"][0] == "error" and "fixed-reason" not in DEFERRED_RULES)
    where = {
        "a sampled row turned mc: false with no fixedReason":
            f"params.json[{FREE_PARAM}].fixedReason",
        "an mc: false row whose fixedReason is not one of the three":
            "params.json[na_normal_low].fixedReason",
        "a sampled row carrying a fixedReason": f"params.json[{FREE_PARAM}].fixedReason",
        "a single-point range held fixed by samplingMode without mc: false":
            f"params.json[{FREE_PARAM}].range",
        "mc is the string 'false' (both engines sample the row)": "params.json[na_normal_low].mc",
    }
    check("fixed-reason: every planted case has its expected location here",
          {p[1] for p in _plants("fixed-reason")} == set(where))
    for _code, what, mutate in _plants("fixed-reason"):
        got = _linked_new(mutate)
        check(f"fixed-reason: {what} -- one error, naming the row",
              [(f.code, f.severity, f.where) for f in got]
              == [("fixed-reason", "error", where.get(what))], str([str(f) for f in got]))
    got = _linked_new(_plants("fixed-reason")[0][2])
    check("fixed-reason: the message says the reason is missing (not that some value is "
          "wrong) and names the three permitted reasons and HREQ-U-01",
          len(got) == 1 and "mc: false but no fixedReason" in got[0].message
          and all(r in got[0].message for r in FIXED_REASONS)
          and "HREQ-U-01" in got[0].message, str([str(f) for f in got]))
    got = _linked_new(_plants("fixed-reason")[1][2])
    check("fixed-reason: an unknown reason is quoted in the message",
          len(got) == 1 and '"convenience" is not a permitted reason' in got[0].message,
          str([str(f) for f in got]))
    for reason in FIXED_REASONS:
        got = _linked_new(lambda kb, r=reason: _params(
            kb, lambda p: p["na_normal_low"].update(fixedReason=r)))
        check(f"fixed-reason: {reason!r} on an mc: false row is accepted", not got,
              str([str(f) for f in got]))
    got = _linked_new(lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(mc=True)))
    check("fixed-reason: a sampled row with mc: true and no reason is legal", not got,
          str([str(f) for f in got]))
    got = _linked_new(lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(
        mc=False, fixedReason="scenario-condition")))
    check("fixed-reason: mc: false on a wide range is legal when it records a reason (mc "
          "governs, not the range)", not got, str([str(f) for f in got]))
    # The shipped data, at whatever stage the curator's fields have reached: the rule names
    # exactly the rows it should, and degrades by reporting, never by raising.
    def breaks_rule(row: dict[str, Any]) -> bool:
        if "mc" in row and not isinstance(row["mc"], bool):
            return True
        if row.get("mc") is False:
            return row.get("fixedReason") not in FIXED_REASONS
        return "fixedReason" in row or row["range"][0] == row["range"][1]
    kb = base_kb()
    found = sorted({f.where.split("]")[0] + "]" for f in check_kb(kb) if f.code == "fixed-reason"})
    want = sorted(f"params.json[{name}]" for name, row in kb["params"].items()
                  if breaks_rule(row))
    check(f"fixed-reason on the shipped data names exactly the rows held fixed without a "
          f"permitted reason ({len(want)} of {len(kb['params'])} rows)", found == want,
          f"found {found}, want {want}")


def test_calibration_link_holds_both_sides_to_the_expectation_registry() -> None:
    """HREQ-E-10 (BUG-20261003-187): a parameter whose value was chosen to make an output
    match a target lists that expectation in calibratedAgainst, the expectation lists the
    parameter in calibrates, and the two lists agree. The expectation registry is read from
    the reference's scenarios.js as text; a registry that cannot be had is an error."""
    check("calibration-link is enforced by error codes, not deferred",
          DOCUMENTED_RULES.get("calibration-link") == ("calibration-link",
                                                      "expectations-unavailable")
          and RULES["calibration-link"][0] == RULES["expectations-unavailable"][0] == "error"
          and "calibration-link" not in DEFERRED_RULES)
    cal_where = f"params.json[{FREE_PARAM}].calibratedAgainst"
    where = {
        "a row whose notes say it was calibrated carries no calibratedAgainst": cal_where,
        "a calibrated row lost its calibratedAgainst": "params.json[map_auto_tau_h].calibratedAgainst",
        "a calibratedAgainst entry that is not <scenario_id>/<NN>":
            "params.json[pn_gain].calibratedAgainst",
        "a calibratedAgainst entry naming no registered expectation":
            "params.json[map_auto_tau_h].calibratedAgainst",
        "calibratedAgainst names an expectation whose calibrates omits the row": cal_where,
        "an expectation's calibrates names a row whose calibratedAgainst omits it": cal_where,
        "an expectation's calibrates names a row whose calibratedAgainst names only other "
        "expectations": "params.json[map_auto_tau_h].calibratedAgainst",
        "an expectation's calibrates names no params.json row":
            "scenarios.js[chronic_high_salt_30d/02].calibrates",
        "an expectation's calibrates is not a literal array":
            "scenarios.js[chronic_high_salt_30d/02].calibrates",
        "scenarios.js registers an expectation id twice": "scenarios.js[no_water_24h/03]",
        "scenarios.js registers no expectation id": "scenarios.js",
        "scenarios.js cannot be tokenised (an unterminated string)": "scenarios.js",
        "no scenarios.js text at all": "scenarios.js",
    }
    plants = _plants("calibration-link") + _plants("expectations-unavailable")
    check("calibration-link: every planted case has its expected location here",
          {p[1] for p in plants} == set(where))
    messages: dict[str, str] = {}
    for code, what, mutate in plants:
        got = _linked_new(mutate)
        messages[what] = got[0].message if len(got) == 1 else ""
        check(f"{code}: {what} -- one error, naming the row or record",
              [(f.code, f.severity, f.where) for f in got] == [(code, "error", where.get(what))],
              str([str(f) for f in got]))
    lost = messages["a calibrated row lost its calibratedAgainst"]
    check("calibration-link: a calibrated row without its link is reported once, with both "
          "reasons (its notes and the expectation that lists it)",
          "notes say" in lost and "chronic_high_salt_30d/02" in lost, lost)
    unavailable = messages["scenarios.js registers no expectation id"]
    check("expectations-unavailable: names what went unchecked (the rows carrying "
          "calibratedAgainst)", all(p in unavailable for p in CALIBRATED), unavailable)

    kb = _linked(base_kb())
    codes = {f.code for f in check_kb(kb)}
    check("calibration-link: the linked data (four rows, two expectations) is clean",
          not codes & {"calibration-link", "expectations-unavailable"}, str(codes))
    text = kb.get("scenarios_js", _shipped_scenarios_text())
    back = {p: rec.id for rec in expectation_records(text) if isinstance(rec.calibrates, tuple)
            for p in rec.calibrates}
    check("calibration-link: the registry's calibrates, read as text, is the inverse of the "
          "four calibratedAgainst links", back == CALIBRATED, str(back))

    def unlinked(kb: dict[str, Any]) -> None:
        _params(kb, lambda p: [row.pop("calibratedAgainst", None) for row in p.values()])
        kb["scenarios_js"] = ""
    got = {f.where for f in _linked_new(unlinked) if f.code == "calibration-link"}
    check("calibration-link: the notes wording flags exactly the four calibrated rows and no "
          "other shipped row", got == {f"params.json[{p}].calibratedAgainst" for p in CALIBRATED},
          str(sorted(got)))
    # The shipped data, at whatever stage: a reference without ids is one loud error, and
    # each calibrated row without its link is named.
    kb = base_kb()
    found = check_kb(kb)
    ids = expectation_records(_shipped_scenarios_text())
    check(f"shipped data: expectations-unavailable fires exactly when the reference scenarios.js "
          f"registers no expectation id (it registers {len(ids)})",
          (sum(f.code == "expectations-unavailable" for f in found) == 1) == (not ids))
    missing = {f"params.json[{p}].calibratedAgainst" for p in CALIBRATED
               if "calibratedAgainst" not in kb["params"][p]}
    check(f"shipped data: each calibrated row without calibratedAgainst is named "
          f"({len(missing)} today)",
          missing <= {f.where for f in found if f.code == "calibration-link"})


def test_scenarios_js_is_read_as_text_never_imported() -> None:
    """calibration-link reads the expectation registry from the reference's scenarios.js by
    scanning its object literals; kb-v1 depends on reference-v1, never on engine-v1."""
    tricky = """// { id: 'commented/01', calibrates: ['x'] }
/* { id: 'block/01' } */
const t = `template ${ {a: '}'}['a'] } { id: 'templ/01' }`;
export const S = Object.freeze({
  s: makeScenario({ id: 's', title: `T ${1}`, dt: 1 / 60, validation: { expects: [
    { id: 's/01', metric: 'it\\'s "quoted"', calibrates: ['p1', "p2"], role: 'calibration',
      evidence: ['ev:a', "ev:b"] },
    { id: 's/02', metric: 'x', calibrates: CAL, evidence: EV },
    { id: 's/03', metric: 'y', calibrates: ['p', other] },
    { id: "s/04", note: flag ? 'a' : 'b', role: 'structural' },
    { metric: 'no id' }, { id: 's/5' },
  ] } }),
});
"""
    recs = {rec.id: (rec.calibrates, rec.role) for rec in expectation_records(tricky)}
    evidence = {rec.id: rec.evidence for rec in expectation_records(tricky)}
    check("scenarios.js scan: ids come from object literals only, never from comments or "
          "template text, and only in the <scenario_id>/<NN> form",
          list(recs) == ["s/01", "s/02", "s/03", "s/04"], str(list(recs)))
    check("scenarios.js scan: a literal calibrates array is read with both quote styles and "
          "escapes", recs.get("s/01") == (("p1", "p2"), "calibration"), str(recs.get("s/01")))
    check("scenarios.js scan: a calibrates that is not an array of string literals is never "
          "read as a list", all(not isinstance(recs[k][0], tuple) and recs[k][0] is not None
                                for k in ("s/02", "s/03")), str(recs))
    check("scenarios.js scan: a record without calibrates reads None; a ternary is not a key",
          recs.get("s/04") == (None, "structural"), str(recs.get("s/04")))
    check("scenarios.js scan: an expectation's evidence is read like calibrates (a literal "
          "array; anything else never as a list; absent is None)",
          evidence.get("s/01") == ("ev:a", "ev:b") and evidence.get("s/03") is None
          and evidence.get("s/02") is not None and not isinstance(evidence.get("s/02"), tuple),
          str(evidence))
    check("scenarios.js scan: tokens keep the division and drop the comments",
          ("w", "/") in js_tokens("a / b // c") and ("w", "c") not in js_tokens("a / b // c"))
    for bad in ("const x = 'open;\n", "/* never closed", "const t = `open ${x}", "{ id: 's/01' ]",
                "{ { }", "const s = 'bad \\u12G4';"):
        try:
            expectation_records(bad)
            refused = False
        except ScriptError:
            refused = True
        check(f"scenarios.js scan: refused, as ScriptError -- {bad[:24]!r}", refused)
    shipped = expectation_records(_shipped_scenarios_text())
    check(f"scenarios.js scan: the reference registers 0 ids (before model 1.1.0) or all 24 "
          f"(it registers {len(shipped)})", len(shipped) in (0, 24))
    linked = [rec.id for rec in expectation_records(_linked_scenarios_text())]
    by_scenario: dict[str, list[str]] = {}
    for eid in linked:
        by_scenario.setdefault(eid.split("/")[0], []).append(eid.split("/")[1])
    check("scenarios.js scan: the registry carries the 24 V1 expectations, ids unique and "
          "numbered 01.. in registration order per scenario",
          len(linked) == len(set(linked)) == 24
          and all(nn == [f"{i:02d}" for i in range(1, len(nn) + 1)]
                  for nn in by_scenario.values()), str(by_scenario))
    for g in (True, 1.5, [None], {"x": 1}, ""):
        got = _linked_new(lambda kb, g=g: kb.update(scenarios_js=g))
        check(f"scenarios.js text {g!r}: reported (expectations-unavailable), never raised",
              {f.code for f in got} == {"expectations-unavailable"}, str([str(f) for f in got]))
    got = _linked_new(lambda kb: kb["load_errors"].update(scenarios_js="planted: unreadable"))
    check("scenarios.js load error: reported with the loader's reason",
          [f.code for f in got] == ["expectations-unavailable"]
          and "planted: unreadable" in got[0].message, str([str(f) for f in got]))
    engine_imports = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "src" / "health" / "kb")
                            .rglob("*.py")
                            if re.search(r"^\s*(?:from|import)\s+(?:health\.engine|\.\.engine)\b",
                                         p.read_text(encoding="utf-8"), re.M))
    check("kb-v1 never imports the engine (kb-v1 depends on reference-v1, not engine-v1)",
          not engine_imports, str(engine_imports))


def _one(what: str, got: list[Finding], want: tuple[str, str, str]) -> None:
    check(f"{want[0]}: {what} -- one {want[1]}, naming the record",
          [(f.code, f.severity, f.where) for f in got] == [want], str([str(f) for f in got]))


def test_engine_only_evidence_and_curation_records_are_structured() -> None:
    """Tracker W-6 (the curator's ledger entry for it names this test): a source only the
    engine's scenario expectations cite says so in Evidence.engineOnly, and the marker
    exempts it from unused-evidence only as a claim the checker verifies -- no
    knowledge-base record or params.json row cites it, and citedBy is exactly the set of
    scenarios.js expectations that cite it. VERIFICATION_LOG.json curationRecords validate
    against #/definitions/CurationRecord (BUG-20261003-188). Then the curator's records."""
    check("engine-only-evidence is an error code; unused-evidence stays a warning",
          RULES["engine-only-evidence"][0] == "error" and RULES["unused-evidence"][0] == "warn")
    marker, cited_by = f"evidence.json[{ENGINE_ONLY}].engineOnly", \
        f"evidence.json[{ENGINE_ONLY}].engineOnly.citedBy"
    where = {
        "an engine-only record a relation cites": marker,
        "an engine-only record a params.json row cites": marker,
        "engineOnly.citedBy names an expectation scenarios.js does not register": cited_by,
        "engineOnly.citedBy names an expectation that does not cite the record": cited_by,
        "engineOnly.citedBy omits an expectation that cites the record": cited_by,
    }
    plants = _plants("engine-only-evidence")
    check("engine-only-evidence: every planted case has its expected location here",
          {p[1] for p in plants} == set(where))
    for _code, what, mutate in plants:
        _one(what, _linked_new(mutate), ("engine-only-evidence", "error", where.get(what, "?")))
    got = _linked_new(next(p[2] for p in PLANTS if p[0] == "pattern-mismatch"
                           and "engineOnly.citedBy" in p[1]))
    _one("a malformed citedBy entry is the schema's (pattern-mismatch), never also a registry "
         "mismatch", got, ("pattern-mismatch", "error", f"{cited_by}[4]"))

    kb = _linked(base_kb())
    text = kb.get("scenarios_js", _shipped_scenarios_text())
    want_cited = sorted(_citing(text).get(ENGINE_ONLY, []))
    check(f"engineOnly: {ENGINE_ONLY} is cited by the four drink_water_1L expectations the "
          f"curator listed ({want_cited})", want_cited == [
              "drink_water_1L/02", "drink_water_1L/03", "drink_water_1L/04", "drink_water_1L/06"])
    about = [str(f) for f in check_kb(kb) if ENGINE_ONLY in f.where]
    check("engineOnly: a marker whose citedBy is exactly the citing expectations is accepted, "
          "and exempts the record from unused-evidence", not about, str(about))
    got = _linked_new(lambda kb: ev(kb, ENGINE_ONLY).pop("engineOnly"))
    _one("the marker dropped: the record is unused again", got,
         ("unused-evidence", "warn", f"evidence.json[{ENGINE_ONLY}]"))
    got = _linked_new(lambda kb: _new_evidence(kb, "ev:planted-engine").update(engineOnly={
        "reason": "only the engine bibliography lists it", "citedBy": [],
        "recordedOn": "2026-10-03", "by": "planted"}))
    check("engineOnly: citedBy [] on a record no expectation cites (the bibliography-only case) "
          "is accepted and not reported unused", not got, str([str(f) for f in got]))
    anchor = "range: [0, 6], kind: 'quantitative', evidence: ['ev:crowe-1987']"

    def unreadable(kb: dict[str, Any]) -> None:
        text = _linked(kb).get("scenarios_js", _shipped_scenarios_text())
        kb["scenarios_js"] = text.replace(anchor, "range: [0, 6], kind: 'quantitative', "
                                                  "evidence: CROWE")
    got = _linked_new(unreadable)
    check("engineOnly: a listed expectation whose evidence is not a literal array is reported "
          "as unreadable, never as 'does not cite'",
          [(f.code, f.where) for f in got] == [("engine-only-evidence", cited_by)]
          and "not a literal array" in got[0].message and "do(es) not cite" not in got[0].message,
          str([str(f) for f in got]))
    got = _linked_new(lambda kb: kb.update(scenarios_js=""))
    check("engineOnly: with no registry the markers go unchecked, and expectations-unavailable "
          "names them", [f.code for f in got] == ["expectations-unavailable"]
          and ENGINE_ONLY in got[0].message, str([str(f) for f in got]))

    n = len(base_kb()["verification"].get("curationRecords", []))
    log = f"VERIFICATION_LOG.json.curationRecords[#{n}]"
    for code, what, mutate in PLANTS:
        if "curation record" in what or "curationRecords" in what:
            spot = {"bad-enum": f"{log}.action", "missing-field": log,
                    "date-pattern": f"{log}.checkedOn",
                    "file-shape": "VERIFICATION_LOG.json.curationRecords"}[code]
            _one(what, _linked_new(mutate), (code, "error", spot))
    got = _linked_new(_curation(lambda r: r.update(previous="x", reason="y", refs=["W-6"],
                                                   supersedes=1)))
    check("curation records: a complete record with every optional field is accepted", not got,
          str([str(f) for f in got]))

    # The shipped data. The rule half holds at every stage; the record half (the curator's
    # M1 data, W-6) holds once it has landed.
    kb = base_kb()
    shipped = check_kb(kb)
    check("shipped data: every engineOnly marker agrees with the registry and the knowledge base "
          "(no engine-only-evidence finding)",
          not [f for f in shipped if f.code == "engine-only-evidence"])
    marked = {v["id"] for v in records(kb, "evidence") if "engineOnly" in v}
    check("evidence: the five engine-only sources are marked (the curator's W-6 decision)",
          marked == {"ev:shafiee-2005", "ev:crowe-1987", "ev:heer-2000", "ev:rakova-2017",
                     "ev:uttamsingh-1985"}, str(sorted(marked)))
    gut = rel(kb, "rel:gut:na-absorption")
    check("evidence: ev:suckling-2012 is cited by the relation it supports, for direction only "
          "(not by its quantity)", "ev:suckling-2012" in gut.get("evidence", [])
          and "ev:suckling-2012" not in (gut.get("quantity") or {}).get("evidence", []))
    seqs = [r.get("seq") for r in kb["verification"].get("curationRecords", [])]
    check(f"log: the curation records validate and are numbered 1..n in append order "
          f"({len(seqs)})", bool(seqs) and seqs == list(range(1, len(seqs) + 1))
          and not [f for f in shipped if "curationRecords" in f.where], str(seqs[:5]))


def test_params_json_is_validated_against_the_schema_it_declares() -> None:
    """BUG-20261003-188: schema.json declares #/definitions/EngineParams (the parameter table)
    and CurationRecord, and kb-check validated neither, so a misspelt field
    (`calibratedAgaints`), a row without its description or a duplicated citation passed.
    The table is validated against the live definition when the schema declares it; a
    field the hand-written parameter rules already reported for that row is not reported a
    second time (PARAM_RULE_FIELDS)."""
    row = f"params.json[{FREE_PARAM}]"
    where = {
        "a params.json row carries a misspelt field (calibratedAgaints)":
            ("unknown-key", f"{row}.calibratedAgaints"),
        "a params.json row name that is not an identifier (EngineParams propertyNames)":
            ("unknown-key", "params.json[V ecf 0]"),
        "a params.json row without its description (EngineParams)": ("missing-field", row),
        "a params.json row's notes is a number (EngineParams)": ("wrong-type", f"{row}.notes"),
        "a params.json row cites the same evidence twice (EngineParams; no hand-written rule "
        "reads it)": ("not-unique", f"{row}.evidence"),
    }
    plants = [p for p in PLANTS if p[1] in where]
    check("EngineParams: every planted case is in PLANTS", {p[1] for p in plants} == set(where))
    for code, what, mutate in plants:
        _one(what, _linked_new(mutate), (code, "error", where[what][1]))
    check("EngineParams: the fixed-reason and calibration-link plants ran with the schema "
          "declaring EngineParams, so each of their single findings is the rule's alone",
          "EngineParams" in _linked(base_kb())["schema"]["definitions"])
    for label, mutate, want in (
            ("mc: false without fixedReason (the schema's if/then says it too)",
             lambda p: p["na_normal_low"].pop("fixedReason"), "fixed-reason"),
            ("fixedReason outside the enum (the schema's enum says it too)",
             lambda p: p["na_normal_low"].update(fixedReason="convenience"), "fixed-reason"),
            ("calibratedAgainst entry malformed (the schema's pattern says it too)",
             lambda p: p["pn_gain"].update(calibratedAgainst=["x"]), "calibration-link"),
            ("a row that is not an object (the schema's type says it too)",
             lambda p: p.update({FREE_PARAM: 5}), "param-field")):
        got = [f for f in _linked_new(lambda kb, m=mutate: _params(kb, m))
               if f.severity == "error"]
        check(f"EngineParams: {label} -- reported once, by {want}",
              [f.code for f in got] == [want], str([str(f) for f in got]))
    got = _linked_new(lambda kb: (kb["schema"]["definitions"].pop("EngineParams"), _params(
        kb, lambda p: p[FREE_PARAM].update(calibratedAgaints=["drink_water_1L/01"]))))
    check("EngineParams: a schema that does not declare it leaves params.json to the "
          "hand-written rules (the table is validated when declared)", not got,
          str([str(f) for f in got]))
    got = _linked_new(lambda kb: (kb["schema"]["definitions"].pop("CurationRecord"),
                                  kb["verification"].setdefault("curationRecords", []).append(
                                      {"seq": "one"})))
    check("CurationRecord: likewise, curation records are validated when the schema declares "
          "the definition", not got, str([str(f) for f in got]))
    params_findings = [str(f) for f in check_kb(base_kb())
                       if f.where.startswith("params.json") and f.code in DOCUMENTED_RULES["schema"]]
    check("shipped data: params.json validates against the shipped schema's EngineParams "
          "(where it declares one)", not params_findings, str(params_findings))


def _params_table() -> dict[str, Any]:
    return json.loads((ROOT / "src" / "health" / "engine" / "params.json").read_text(
        encoding="utf-8"))


def test_scenario_condition_rows_are_not_graded_as_pooled_estimates() -> None:
    """V1 audit F-12 (tracker W-7; the curator's ledger entry names this test):
    `naIn_base_mmold` was graded A-meta from He 2013 although 150 mmol/day is a scenario
    condition, not the meta-analysis's pooled estimate. grade-ceiling cannot see it (A-meta
    evidence admits an A-meta grade), so this pins the superseding record: no scenario
    condition carries A-meta, and the row keeps its old grade, the date and the reason in
    notes (04 §6.1)."""
    table = _params_table()
    conditions = {n: r for n, r in table.items() if r.get("fixedReason") == "scenario-condition"}
    check("params: the three V1 scenario conditions carry fixedReason scenario-condition",
          {"waterIn_base_Ld", "naIn_base_mmold", "kIn_base_mmold"} <= set(conditions),
          str(sorted(conditions)))
    check("params: no scenario-condition row is graded A-meta (a condition is not a pooled "
          "estimate)", all(r.get("grade") != "A-meta" for r in conditions.values()),
          str({n: r.get("grade") for n, r in conditions.items()}))
    na = table["naIn_base_mmold"]
    check("params: naIn_base_mmold's notes keep the superseded grade, the date and the reason",
          "SUPERSEDED 2026-10-03" in na["notes"] and "grade A-meta -> B-textbook" in na["notes"]
          and "scenario condition" in na["notes"], na["notes"][-300:])


def test_fixed_and_calibrated_rows_carry_structured_reasons() -> None:
    """W-18 data fields on the shipped table (HREQ-U-01, HREQ-E-10; the rules are
    BUG-20261003-186 and -187): every `mc: false` row names one of the three permitted
    reasons, the four calibrated parameters name the expectation ids they were tuned to,
    and the schema's EngineParams admits the table while refusing a fixed row without a
    reason."""
    table = _params_table()
    fixed = {n: r.get("fixedReason") for n, r in table.items() if r.get("mc") is False}
    check(f"params: every mc: false row ({len(fixed)}) has a fixedReason from the three "
          "permitted reasons", bool(fixed) and all(v in FIXED_REASONS for v in fixed.values()),
          str(fixed))
    check("params: no sampled row carries a fixedReason",
          not [n for n, r in table.items() if "fixedReason" in r and r.get("mc") is not False])
    cal = {n: r["calibratedAgainst"] for n, r in table.items() if "calibratedAgainst" in r}
    check("params: the four calibrated parameters name their expectations",
          cal == {n: [eid] for n, eid in CALIBRATED.items()}, str(cal))
    schema = base_kb()["schema"]
    check("schema: the shipped schema declares EngineParams, Param and fixedReason",
          all(k in schema.get("definitions", {}) for k in ("EngineParams", "Param", "fixedReason")))
    got = _linked_new(lambda kb: _params(kb, lambda p: p["na_normal_low"].pop("fixedReason")))
    check("schema + rule: a fixed row without fixedReason is refused, once",
          [(f.code, f.where) for f in got]
          == [("fixed-reason", "params.json[na_normal_low].fixedReason")],
          str([str(f) for f in got]))


def test_untyped_dispersions_are_marked_unverified() -> None:
    """V1 audit F-02/F-03 (tracker W-9; the curator's ledger entry names this test):
    Suckling 2012's +3.13 ± 0.75 mmol/L, and the untyped ± of three other water/salt
    expectation sources, are marked "SEM/SD unverified" in the evidence record itself, and
    Suckling's quote is recorded as the abstract's, until the full text settles the
    dispersion type (HREQ-E-09)."""
    kb = base_kb()
    for vid in ("ev:suckling-2012", "ev:shafiee-2005", "ev:crowe-1987", "ev:heer-2000"):
        check(f"evidence: {vid} says SEM/SD unverified",
              "SEM/SD unverified" in ev(kb, vid).get("notes", ""))
    check("evidence: ev:suckling-2012's quote is recorded as the abstract's",
          ev(kb, "ev:suckling-2012").get("verification", {}).get("quoteSource") == "abstract")


# ---------------------------------------------------------------------------
# Tests: boundaries and semantics (the rigor review's pinning checks)
# ---------------------------------------------------------------------------
def test_boundaries_kill_the_surviving_mutants() -> None:
    """Each check kills a mutant the first suite let survive: summary/quote `>` -> `>=`,
    range `lo > hi` -> `lo >= hi`, the value-in-range check made strict, word count split
    on " " only, params.json citations ignored by unused-evidence, meta-mismatch blind to
    one field."""
    def summary_of(text: str) -> Callable[[dict[str, Any]], Any]:
        return lambda kb: ent(kb, BODY).update(summary=text)

    sixty = "\t".join(["a"] * 30) + "\n" + "  ".join(["b"] * 30)        # 60 words, mixed spaces
    check("boundary: a 60-word summary (tabs, newline, double spaces) is legal",
          not new_codes(summary_of(sixty)))
    check("boundary: a 61-word summary (tabs, newline, double spaces) is summary-too-long",
          new_codes(summary_of(sixty + " c")) == {"summary-too-long"})
    check("boundary: words split on Unicode spaces too (60 NBSP-separated words + 1 = 61)",
          new_codes(summary_of(" ".join(["a"] * 61))) == {"summary-too-long"})
    check("boundary: U+200B is not whitespace in JavaScript either (61 joined words = 1)",
          not new_codes(summary_of("​".join(["a"] * 61))))
    q25 = " ".join(["w"] * 25)
    check("boundary: a 25-word quote is legal",
          not new_codes(lambda kb: ev(kb, PRIMARY).update(quote=q25)))
    check("boundary: a 26-word quote (tab-separated last word) is quote-too-long",
          new_codes(lambda kb: ev(kb, PRIMARY).update(quote=q25 + "\tw")) == {"quote-too-long"})
    for end in (0, 1):
        check(f"boundary: value equal to range[{end}] is inside its range",
              not new_codes(lambda kb, end=end: nephron_q(kb).update(
                  value=nephron_q(kb)["range"][end])))
    check("boundary: a degenerate range [v, v] holding v is legal (no range-order)",
          not new_codes(lambda kb: nephron_q(kb).update(value=5, range=[5, 5])))
    check("boundary: a params.json row with value == hi and a degenerate range, held fixed "
          "with mc: false and a fixedReason, is legal",
          not new_codes(lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(
              value=16, range=[16, 16], mc=False, fixedReason="scenario-condition"))))
    got = new_codes(lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(
        value=16, range=[16, 16])))
    check("boundary: the same degenerate range without mc: false breaks no range rule, only "
          "fixed-reason (samplingMode holds it fixed with no recorded reason)",
          got == {"fixed-reason"}, str(got))
    for key, field in (("evidence", "curator"), ("relations", "generated"),
                       ("entities", "kbVersion")):
        check(f"meta-mismatch fires on {field} alone",
              new_codes(lambda kb, key=key, field=field: kb[key].update({field: "planted"}))
              == {"meta-mismatch"})

    def cited_only_by_params(kb: dict[str, Any]) -> None:
        _new_evidence(kb, "ev:planted-params-only")
        _params(kb, lambda p: p[FREE_PARAM]["evidence"].append("ev:planted-params-only"))
    check("unused-evidence counts params.json citations: a record cited only by a params row "
          "is not unused", not new_codes(cited_only_by_params))
    check("word_count is JavaScript's split(/\\s+/) after trim",
          (word_count("  a\tb\nc　d﻿e  "), word_count(""), word_count(" \n ")) == (5, 0, 0))


def test_patterns_have_ecma_262_semantics() -> None:
    """schema.json patterns are ECMA-262 (JSON Schema). Python's `re` differs: Unicode
    \\d/\\w/\\s (and no U+FEFF in \\s), `$` before a trailing newline, `.` matching U+2028."""
    cases = [
        (r"^\d+$", "１２３", False), (r"^\d+$", "٤٥", False), (r"^\d+$", "123", True),
        (r"^\w+$", "é", False), (r"^\w+$", "a_1", True), (r"^\W$", "é", True),
        (r"^\s$", " ", True), (r"^\s$", "﻿", True), (r"^\s$", "　", True),
        (r"^\s$", " ", True), (r"^\s$", "\x1c", False), (r"^\s$", "\x85", False),
        (r"^\s$", "​", False), (r"^\S+$", "a b", False), (r"^\S+$", "ab", True),
        (r"^[\S]+$", "a　b", False), (r"^[^\s]+$", "a﻿b", False),
        (r"^[\d]+$", "١", False), (r"^[\D]+$", "١", True),
        (r"^a$", "a\n", False), (r"^a.c$", "a c", False), (r"^a.c$", "abc", True),
        (r"\bcat\b", "a cat!", True), (r"\bcat\b", "écat", True), (r"\Bcat", "xcat", True),
        (r"^10\.\d{4,9}/\S+$", "10.1016/s0889-8529(05)70207-3", True),
        (r"^(?<y>\d{4})-\d{2}$", "2026-10", True), (r"^\x41B$", "AB", True),
    ]
    bad = [(p, v, want) for p, v, want in cases if matches(p, v) != want]
    check(f"ecma patterns: {len(cases)} cases match as JavaScript's RegExp would", not bad,
          str(bad))
    refused = []
    for pattern in (r"\p{L}", r"(a)\1", r"(?i)a", r"[]", r"[^]", r"\cJ", r"\k<a>", "a\\"):
        try:
            ecma_to_python(pattern)
            refused.append(pattern)
        except PatternError:
            pass
    check("ecma patterns: constructs with no exact Python reading are refused (schema-drift), "
          "never read with Python's meaning", not refused, f"accepted: {refused}")


def test_verification_log_latest_record_governs() -> None:
    """HREQ-D-04 / HREQ-E-06: the log is append-only; a correction is a new record. A
    re-check that no longer resolves unverifies the id; a later re-check that resolves
    again verifies it; a malformed record is reported and does not govern."""
    def later(*flags: bool) -> Callable[[dict[str, Any]], None]:
        def mutate(kb: dict[str, Any]) -> None:
            first = log_record(kb, BODY)
            for k, flag in enumerate(flags):
                records(kb, "verification").append(
                    dict(first, resolved=flag, checkedOn=f"2026-10-0{k + 2}"))
        return mutate
    got = new_findings(later(False))
    check("latest says resolved: false -> external-id-unverified, naming the record and that "
          "an earlier one said true",
          {f.code for f in got} == {"external-id-unverified"}
          and "latest" in got[0].message and "earlier record" in got[0].message,
          str([str(f) for f in got]))
    check("false, then true again -> verified (the latest record governs)",
          not new_codes(later(False, True)))
    check("an in-place edit of the only record to resolved: false -> unverified",
          new_codes(lambda kb: log_record(kb, BODY).update(resolved=False))
          == {"external-id-unverified"})


def test_schema_edits_are_enforced_not_just_detected() -> None:
    """HREQ-D-03: the KB SHALL conform to schema.json. The checker reads the live schema,
    so tightening ANY node is enforced on the data; an annotation edit changes nothing."""
    got = new_findings(lambda kb: _schema_def(kb, "Entity", "properties", "name").update(
        maxLength=5))
    check("Entity.name maxLength 5: an out-of-bounds error on (nearly) every entity",
          {f.code for f in got} == {"out-of-bounds"} and len(got) > 90, f"{len(got)} findings")
    got = new_findings(lambda kb: _schema_def(kb, "quantity", "properties", "grade").update(
        {"enum": ["A-meta", "A-primary"]}))
    check("a quantity grade enum narrowed beside its $ref: draft-07 ignores the sibling, so the "
          "checker says so (schema-drift) instead of guessing",
          "schema-drift" in {f.code for f in got}, str({f.code for f in got}))
    got = new_findings(lambda kb: _schema_def(kb, "Entity", "properties", "scale").update(
        maximum=3))
    check("Entity.scale maximum 7 -> 3: every deeper entity is out-of-bounds",
          {f.code for f in got} == {"out-of-bounds"} and len(got) > 10, f"{len(got)} findings")
    check("annotation-only edits (description, title, $comment) change nothing",
          not new_codes(lambda kb: (kb["schema"].update(description="edited", title="x"),
                                    _schema_def(kb, "Entity").update({"$comment": "c"}))))
    got = new_findings(lambda kb: kb["entities"].update(relations=[{"junk": 1}, None]))
    check("an extra 'relations' array of garbage inside entities.json is validated (every item)",
          {f.code for f in got} >= {"missing-field", "unknown-key", "wrong-type"}
          and all(f.severity == "error" for f in got)
          and any("entities.json.relations[#1]" == f.where for f in got),
          str(sorted({(f.code, f.where) for f in got})))
    got = new_findings(lambda kb: _schema_def(kb, "quantity", "properties").pop("engineParam"))
    check("renaming away a field a cross-file rule reads (quantity.engineParam) is schema-drift "
          "(and the data that carries it now breaks the schema)",
          {"schema-drift", "unknown-key"} <= {f.code for f in got}, str({f.code for f in got}))


def _slim_kb() -> dict[str, Any]:
    """The shipped KB cut down to the records the garbage test corrupts (the checker's code
    paths for a record do not depend on how many others there are; this keeps 700+ runs
    fast)."""
    kb = base_kb()
    keep = {"entities": {BODY, HEART}, "relations": {ADH_SLOPE, GFR},
            "evidence": {PRIMARY, TEXTBOOK}}
    for key, ids in keep.items():
        kb[key][key] = [r for r in records(kb, key) if r["id"] in ids]
    kb["verification"]["records"] = [log_record(kb, BODY), log_record(kb, HEART)]
    return kb


def test_checker_never_raises_on_garbage() -> None:
    """Every field of one record of each kind, replaced by each wrong JSON type; then every
    top-level document of the full KB, replaced the same way."""
    kb = _slim_kb()
    garbage: list[Any] = [None, True, "", [None], {"x": 1}, -1.5, float("nan")]
    targets: list[tuple[str, dict[str, Any]]] = [
        ("entity", ent(kb, BODY)), ("entity", ent(kb, HEART)),
        ("relation", rel(kb, ADH_SLOPE)), ("quantity", rel(kb, ADH_SLOPE)["quantity"]),
        ("conflict", rel(kb, GFR)["quantity"]["conflicts"][2]),
        ("evidence", ev(kb, PRIMARY)), ("verification", ev(kb, PRIMARY)["verification"]),
        ("log", log_record(kb, BODY)), ("params-row", kb["params"]["adh_slope"]),
        ("params_js-row", kb["params_js"]["adh_slope"]),
        ("entities.json", kb["entities"]), ("evidence.json", kb["evidence"]),
        ("schema.Entity", kb["schema"]["definitions"]["Entity"]),
        ("schema.grade", kb["schema"]["definitions"]["grade"]),
    ]
    crashes: list[str] = []
    runs = 0
    for label, obj in targets:
        for key in list(obj):
            original = obj[key]
            for g in garbage:
                obj[key] = g
                runs += 1
                try:
                    check_kb(kb)
                except Exception as exc:  # noqa: BLE001 - any crash is the finding
                    crashes.append(f"{label}.{key}={g!r}: {exc!r}")
            obj[key] = original
    kb = base_kb()
    for key in ("entities", "relations", "evidence", "verification", "schema", "params",
                "params_js", "load_errors", "paths"):
        original = kb[key]
        for g in garbage:
            kb[key] = g
            runs += 1
            try:
                check_kb(kb)
                summary(kb)
            except Exception as exc:  # noqa: BLE001 - any crash is the finding
                crashes.append(f"kb[{key!r}]={g!r}: {exc!r}")
        kb[key] = original
    check(f"checker and summary survive {runs} garbage substitutions without raising",
          not crashes, "\n        ".join(crashes[:10]))


def test_strict_json_and_params_data_js() -> None:
    """HREQ-P-09: the reference app reads these files with JSON.parse, which refuses the
    NaN/Infinity tokens Python's json module accepts."""
    for token in ("NaN", "Infinity", "-Infinity"):
        try:
            parse_json(f'{{"value": {token}}}', "x.json")
            ok = False
        except KBLoadError as exc:
            ok = token.lstrip("-") in str(exc) or token in str(exc)
        check(f"strict JSON: the {token} token is refused with a message naming it", ok)
    js = 'export default {"a": {"value": 1}};\n'
    check("params.data.js: export default ... ; is read as JSON",
          parse_params_js("// header\n// more\n" + js) == {"a": {"value": 1}})
    for bad in ("const x = 1;\n" + js, '{"a": 1}', 'export default {"a": Infinity};'):
        try:
            parse_params_js(bad)
            refused = False
        except KBLoadError:
            refused = True
        check(f"params.data.js: refused -- {bad[:30]!r}", refused)


def test_parent_cycle_walk_is_linear() -> None:
    """A same-scale parent chain is legal; the old cycle walk was O(depth^3) (2.1 s at
    depth 1000, measured by the rigor review)."""
    def chain(kb: dict[str, Any], depth: int) -> None:
        prev = LEAF
        for i in range(depth):
            _new_entity(kb, f"molecule:probe-{i}", parent=prev)
            prev = f"molecule:probe-{i}"
    kb = base_kb()
    chain(kb, 1000)
    t0 = time.perf_counter()
    found = check_kb(kb)
    dt = time.perf_counter() - t0
    check(f"check_kb on a 1000-deep same-scale parent chain takes < 1 s ({dt:.2f} s)",
          dt < 1.0, f"{dt:.2f} s")
    check("the legal 1000-deep chain raises nothing new",
          not [f for f in found if f not in baseline()],
          str([str(f) for f in found if f not in baseline()][:3]))

    def three_cycle(kb: dict[str, Any]) -> None:
        a, b, c = (ent(kb, x) for x in ("molecule:water", "molecule:na", "molecule:k"))
        a["parent"], b["parent"], c["parent"] = "molecule:na", "molecule:k", "molecule:water"
    got = new_findings(three_cycle)
    check("a three-entity cycle is reported once, as parent-cycle",
          [f.code for f in got] == ["parent-cycle"], str([str(f) for f in got]))


def test_engine_param_mirrors_are_verified_against_params_json() -> None:
    kb = base_kb()
    base = baseline()
    mirrors = [(r["id"], r["quantity"]["engineParam"]) for r in records(kb, "relations")
               if isinstance(r.get("quantity"), dict) and "engineParam" in r["quantity"]]
    check("engine mirrors: the shipped KB has the 10 mirrored quantities summary() counts",
          len(mirrors) == 10 == summary(kb)["counts"]["engineMirrors"], str(mirrors))
    check("engine mirrors: none mismatches on the shipped data",
          not [f for f in base if f.code.startswith("engine-param")])

    hits = [f for f in new_findings(lambda kb: rel(kb, ADH_SLOPE)["quantity"].update(value=0.36))
            if f.code == "engine-param-mismatch"]
    check("engine mirrors: value 0.35 -> 0.36 on adh_slope is reported as a mismatch",
          len(hits) == 1, str([str(f) for f in hits]))
    check("engine mirrors: the finding names the param, the relation and both values",
          bool(hits) and "'adh_slope'" in hits[0].message and ADH_SLOPE in hits[0].where
          and "0.36" in hits[0].message and "0.35" in hits[0].message,
          str(hits[0]) if hits else "")

    missed = []
    for rid, param in mirrors:
        for field, bump in (("value", lambda v: v * 1.01 + 0.001),
                            ("range", lambda r: [r[0], r[1] + 1]),
                            ("unit", lambda u: u + " (edited)"),
                            ("grade", lambda g: "C-model" if g != "C-model" else "D-animal")):
            def mutate(kb: dict[str, Any], rid: str = rid, field: str = field,
                       bump: Callable[[Any], Any] = bump) -> None:
                q = rel(kb, rid)["quantity"]
                q[field] = bump(q[field])
            found = [f for f in new_findings(mutate)
                     if f.code == "engine-param-mismatch" and repr(param) in f.message
                     and field in f.message]
            if not found:
                missed.append(f"{rid}.{field} ({param})")
    check("engine mirrors: perturbing value, range, unit or grade of EVERY mirror is caught "
          f"({len(mirrors) * 4} perturbations)", not missed, str(missed))

    hits = [f for f in new_findings(lambda kb: _params(
        kb, lambda p: p["thirst_threshold"].update(unit="mmol/L")))
            if f.code == "engine-param-mismatch"]
    check("engine mirrors: a change on the params.json side is caught too, naming the param",
          len(hits) == 1 and "'thirst_threshold'" in hits[0].message, str([str(f) for f in hits]))
    hits = [f for f in new_findings(lambda kb: _params(kb, lambda p: p.pop("adh_thalf_h")))
            if f.code == "engine-param-unknown"]
    check("engine mirrors: a params.json row that disappears is reported by name",
          len(hits) == 1 and "'adh_thalf_h'" in hits[0].message, str([str(f) for f in hits]))
    got = new_findings(lambda kb: kb["params"]["adh_slope"].update(value=0.36))
    check("params-mirror: editing params.json without regenerating params.data.js is caught "
          "(beside the engine mirror it breaks)",
          {f.code for f in got} == {"params-mirror", "engine-param-mismatch"},
          str([str(f) for f in got]))


# ---------------------------------------------------------------------------
# Tests: the summary
# ---------------------------------------------------------------------------
def test_summary_counts_match_the_data() -> None:
    kb = base_kb()
    s = summary(kb)
    raw_entities = json.loads((DATA_DIR / "entities.json").read_text(encoding="utf-8"))
    by_type = Counter(e["type"] for e in raw_entities["entities"])
    check("summary: entitiesByType equals an independent count over entities.json",
          s["entitiesByType"] == dict(by_type), f"{s['entitiesByType']} vs {dict(by_type)}")
    check("summary: tissue count (independent) is 20", by_type["tissue"] == 20
          and s["entitiesByType"]["tissue"] == 20, str(by_type["tissue"]))

    raw_params = json.loads(Path(kb["paths"]["params"]).read_text(encoding="utf-8"))
    at_least_b = sum(1 for row in raw_params.values()
                     if row["grade"] in ("A-meta", "A-primary", "B-textbook"))
    share = s["paramsGradedAtLeastB"]
    check(f"summary: params graded >= B equals an independent count over params.json "
          f"({at_least_b} of {len(raw_params)})",
          (share["count"], share["total"]) == (at_least_b, len(raw_params))
          and len(raw_params) == s["counts"]["params"] and at_least_b > 0,
          f"{share} vs {at_least_b}/{len(raw_params)}")
    check("summary: >= B means exactly A-meta, A-primary, B-textbook",
          GRADE_B_OR_BETTER == ("A-meta", "A-primary", "B-textbook") == GRADES[:3],
          str(GRADE_B_OR_BETTER))
    raw_relations = json.loads((DATA_DIR / "relations.json").read_text(encoding="utf-8"))
    check("summary: relationsByType sums to the relation count",
          sum(s["relationsByType"].values()) == len(raw_relations["relations"]) == 203)
    check("summary: every grade table lists all six grades in rank order",
          all(list(s[k])[:6] == list(GRADES)
              for k in ("evidenceByDefaultGrade", "quantitiesByGrade", "paramsByGrade")))
    check("summary: JSON-serialisable", bool(json.dumps(s)))

    kb = base_kb()
    ent(kb, BODY)["type"] = ["organism"]
    ent(kb, HEART)["scale"] = {"x": 1}
    ent(kb, LEAF).pop("type")
    rel(kb, NEPHRON)["type"] = {"t": 1}
    ev(kb, PRIMARY)["sourceType"] = ["review"]
    ev(kb, TEXTBOOK)["defaultGrade"] = ["B"]
    try:
        s = summary(kb)
        crash = ""
    except Exception as exc:  # noqa: BLE001 - the crash is the finding
        s, crash = {}, repr(exc)
    check("summary: a list or object in type / scale / relation type / sourceType / grade is "
          "counted under '(invalid)', an absent one under '(none)', never raised on",
          not crash and s["entitiesByType"].get("(invalid)") == 1
          and s["entitiesByType"].get("(none)") == 1
          and s["entitiesByScale"].get("(invalid)") == 1
          and s["relationsByType"].get("(invalid)") == 1
          and s["evidenceBySourceType"].get("(invalid)") == 1
          and s["evidenceByDefaultGrade"].get("(invalid)") == 1 and bool(json.dumps(s)),
          crash or str({k: s[k] for k in ("entitiesByType", "entitiesByScale")}))

    for label, mutate, needle in (
            ("params.json null", lambda kb: kb.update(params=None), "null"),
            ("params.json unparsable", lambda kb: (kb.update(params=None), kb["load_errors"]
                                                   .update(params="p.json: invalid JSON")),
             "invalid JSON"),
            ("params.json an array", lambda kb: kb.update(params=[1, 2]), "list"),
            ("params.json empty", lambda kb: kb.update(params={}), "no rows"),
            ("a params row without a valid grade",
             lambda kb: kb["params"][FREE_PARAM].update(grade="F"), "no valid grade")):
        kb = base_kb()
        mutate(kb)
        s = summary(kb)
        check(f"summary: {label} -> the >= B share is unavailable (HREQ-N-01), with the reason",
              s["paramsGradedAtLeastB"].get("unavailable") is not None
              and needle in s["paramsUnavailable"] and s["paramsGradedAtLeastB"]["count"] is None,
              str(s["paramsGradedAtLeastB"]))


# ---------------------------------------------------------------------------
# Tests: the CLI
# ---------------------------------------------------------------------------
def test_cli_kb_check_exit_codes(tmp: Path) -> None:
    r = _run_cli("kb-check")
    check("cli kb-check: exit 0 on the shipped KB", r.returncode == 0,
          f"exit {r.returncode}\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    out = r.stdout
    check("cli kb-check: findings are grouped error, then warn, then info, then the summary",
          0 <= out.find("ERROR (") < out.find("WARN (") < out.find("INFO (")
          < out.find("entities by type"), out[:400])

    good = _kb_copy(tmp, "kb-good")
    r = _run_cli("kb-check", "--root", str(good))
    check("cli kb-check --root: exit 0 on an unmodified copy", r.returncode == 0,
          f"exit {r.returncode}\n{r.stdout[-800:]}{r.stderr[-800:]}")

    broken = _kb_copy(tmp, "kb-broken")
    _edit(broken / "entities.json", lambda d: d["entities"][0]["evidence"].append("ev:nope"))
    r = _run_cli("kb-check", "--root", str(broken))
    check("cli kb-check --root: exit 1 on a copy with a dangling evidence reference",
          r.returncode == 1 and "dangling-evidence" in r.stdout and "FAIL" in r.stdout,
          f"exit {r.returncode}\n{r.stdout[-800:]}{r.stderr[-800:]}")

    unparsable = _kb_copy(tmp, "kb-unparsable")
    (unparsable / "relations.json").write_text("{not json", encoding="utf-8")
    r = _run_cli("kb-check", "--root", str(unparsable))
    check("cli kb-check --root: exit 1 (not a traceback) on a file that is not JSON",
          r.returncode == 1 and "cannot load" in r.stderr and "Traceback" not in r.stderr,
          f"exit {r.returncode}\n{r.stderr[-800:]}")

    infinite = _kb_copy(tmp, "kb-infinity")

    def plant(d: dict[str, Any]) -> None:
        q = next(r for r in d["relations"] if r["id"] == NEPHRON)["quantity"]
        q["value"], q["range"] = float("inf"), [0, float("inf")]
    _edit(infinite / "relations.json", plant)
    r = _run_cli("kb-check", "--root", str(infinite))
    check("cli kb-check: exit 1 on a relations.json holding an Infinity token, saying why",
          "Infinity" in (infinite / "relations.json").read_text(encoding="utf-8")
          and r.returncode == 1 and "Infinity" in r.stderr and "Traceback" not in r.stderr,
          f"exit {r.returncode}; {r.stderr[-300:]}")

    listy = _kb_copy(tmp, "kb-listtype")
    _edit(listy / "entities.json", lambda d: d["entities"][0].update(type=["organism"]))
    _edit(listy / "evidence.json", lambda d: d["evidence"][0].update(sourceType={"x": 1}))
    for cmd in (("kb-check",), ("kb-summary",), ("kb-summary", "--json")):
        r = _run_cli(*cmd, "--root", str(listy))
        want = 1 if cmd == ("kb-check",) else 0
        check(f"cli {' '.join(cmd)}: no traceback on an array-typed entity type (exit {want})",
              r.returncode == want and "Traceback" not in r.stderr
              and (cmd != ("kb-check",) or ("kb-check: FAIL" in r.stdout
                                            and "wrong-type" in r.stdout)),
              f"exit {r.returncode}; {r.stderr[-300:]}")

    r = _run_cli("kb-summary", "--json")
    try:
        parsed = json.loads(r.stdout)
    except ValueError:
        parsed = None
    check("cli kb-summary --json: exit 0 and valid JSON with the headline share",
          r.returncode == 0 and isinstance(parsed, dict) and "paramsGradedAtLeastB" in parsed,
          f"exit {r.returncode}\n{r.stdout[:300]}{r.stderr[-300:]}")
    r = _run_cli("kb-summary")
    check("cli kb-summary: aligned text with the >= B line", r.returncode == 0
          and "engine params graded >= B" in r.stdout, r.stdout[-300:])


def _footer_ok(stdout: str) -> bool:
    lines = stdout.splitlines()
    return len(lines) >= 2 and lines[-2] == DISCLAIMER and lines[-1] == VALIDATION_STATUS


def test_status_prints_the_disclaimer(tmp: Path) -> None:
    r = _run_cli("status", "--fast")
    lines = r.stdout.splitlines()
    check("cli status: exit 0", r.returncode == 0, r.stderr[-800:])
    check("cli status: the disclaimer is the second-to-last line and the validation status the "
          "last, each on its own line",
          DISCLAIMER == "Educational model — not medical advice."
          and VALIDATION_STATUS == "Not clinically validated." and _footer_ok(r.stdout),
          r.stdout[-300:])
    order = [next((i for i, line in enumerate(lines) if line.startswith(prefix)), -1)
             for prefix in ("health 0.", "model ", "KB 1.0.0", "entities 107",
                            "engine params by grade", "engine params graded >= B", "KB contract",
                            "expectations", "modules (", DISCLAIMER, VALIDATION_STATUS)]
    check("cli status: HREQ-P-07 order -- package version, model version, KB version and "
          "curator, counts, grade distribution, >= B share, contract, expectations, modules, "
          "disclaimer, validation status",
          -1 not in order and order == sorted(order), f"{order}\n{r.stdout}")
    share_line = lines[order[5]] if order[5] >= 0 else ""
    raw = json.loads((ROOT / "src" / "health" / "engine" / "params.json")
                     .read_text(encoding="utf-8"))
    at_least_b = sum(1 for row in raw.values() if row["grade"] in GRADES[:3])
    n_e = sum(1 for row in raw.values() if row["grade"] == "E-assumption")
    want = (f"{at_least_b}/{len(raw)} ({100.0 * at_least_b / len(raw):.1f}%)",
            f"E-assumption: {n_e}")
    check(f"cli status: the >= B share and the E-assumption count come from the files "
          f"({want[0]}, {want[1]}, counted here from params.json)",
          all(w in share_line for w in want), share_line)

    r = _run_cli("status", "--fast", "--root", str(tmp / "no-such-kb"))
    check("cli status: an unloadable KB exits 1, says unavailable, and still ends with the "
          "disclaimer and the validation status",
          r.returncode == 1 and "KB unavailable" in r.stdout and _footer_ok(r.stdout),
          f"exit {r.returncode}\n{r.stdout}")

    listy = _kb_copy(tmp, "kb-status-listtype")
    _edit(listy / "entities.json", lambda d: d["entities"][0].update(type=["organism"]))
    r = _run_cli("status", "--fast", "--root", str(listy))
    check("cli status: an array-typed entity type -> no traceback, the error finding printed, "
          "exit 1, the footer last",
          "Traceback" not in r.stderr and r.returncode == 1 and _footer_ok(r.stdout)
          and "error  wrong-type  entities.json[organism:body].type" in r.stdout,
          f"exit {r.returncode}\n{r.stdout[-600:]}\n{r.stderr[-300:]}")

    for label, content in (("null", "null"), ("not JSON", '{"adh_slope": '), ("empty", "{}"),
                           ("an array", "[1, 2]")):
        bad = tmp / f"params-{len(content)}.json"
        bad.write_text(content, encoding="utf-8")
        r = _run_cli("status", "--fast", "--params", str(bad))
        share = next((ln for ln in r.stdout.splitlines()
                      if ln.startswith("engine params graded >= B")), "")
        check(f"cli status: params.json {label} -> '>= B: unavailable' and 'E-assumption: "
              "unavailable' (HREQ-N-01; never 0 or n/a), the reason on stderr, exit 1 (the "
              "contract error), the footer last",
              share == "engine params graded >= B: unavailable  E-assumption: unavailable"
              and "unavailable:" in r.stderr and r.returncode == 1 and _footer_ok(r.stdout),
              f"exit {r.returncode}; {share!r}; {r.stderr[-200:]}")


def test_status_expectations_line() -> None:
    """HREQ-D-06 / HREQ-P-07: status prints the expectation harness counts, computed at
    default parameters over every registered scenario -- the same numbers summarize() gives;
    `--fast` skips the run and says so; an engine that cannot be imported is shown as
    unavailable, never as zeros."""
    import health.cli as cli
    from health.engine import SCENARIOS, simulate
    from health.engine.validate import evaluate_expectations, summarize

    rows: list[dict[str, Any]] = []
    for sid in SCENARIOS:
        rows.extend(evaluate_expectations(sid, simulate(scenario=sid)))
    s = summarize(rows)
    kinds = Counter(r["kind"] for r in rows)
    r = _run_cli("status")
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("expectations")), "")
    m = re.fullmatch(r"expectations (\d+): counted pass (\d+) · counted fail (\d+) · "
                     r"not_checked (\d+) · calibration (\d+) · structural (\d+) · "
                     r"known-divergence (\d+) · unverified (\d+) · calibrated params (\d+)"
                     r"(?: \(([^()]*)\))? · independent (\d+) vs calibrated (\d+) \(HREQ-M-12\)"
                     r" · countable not_checked (\d+)(?: \(([^()]*)\))?"
                     r" · bands (\d+) of (\d+) counted(?: \(([^()]*)\))? · registry ([0-9a-f]{12})",
                     line)
    got = (tuple(int(m.group(k)) for k in (*range(1, 10), 11, 12, 13, 15, 16)) if m else None)
    counted = s["counted_pass"] + s["counted_fail"]
    want = (s["rows"], s["counted_pass"], s["counted_fail"], s["not_checked"],
            s["calibration"]["n"], s["structural"]["n"], kinds["known-divergence"],
            kinds["unverified"], len(s["calibrated_parameters"]),
            s["independent_vs_calibrated"]["independent_counted"],
            s["independent_vs_calibrated"]["calibrated_parameters"],
            len(s["not_checked_countable"]), counted - s["unbanded_counted"], counted)
    check("cli status: the expectations line equals summarize() over every scenario "
          f"({want})", got == want and r.returncode == 0, f"{line!r} vs {want}")
    check("cli status: the expectations line names the calibrated parameters",
          bool(m) and (m.group(10) or "") == ", ".join(s["calibrated_parameters"]), line)
    check("cli status: V1 prints 4 calibrated parameters against 10 independent counted rows, "
          "and 'bands 0 of 10 counted' with why (no Monte Carlo in status; n >= 256 needed) "
          "-- never hidden (HREQ-M-12, HREQ-U-08, HREQ-V-15)",
          bool(m) and m.group(9) == "4" and m.group(11) == "10" and m.group(12) == "4"
          and (m.group(15), m.group(16)) == ("0", "10")
          and m.group(17) == "no Monte Carlo in this run; n ≥ 256 required for a published band"
          and m.group(18) == str(s["registry_version"])[:12], line)
    check("cli status: 24 registered expectations (V1)", s["rows"] == 24, str(s["rows"]))
    lines = r.stdout.splitlines()
    check("cli status: the expectations line follows the KB contract line and precedes the "
          "modules line", bool(line) and next(i for i, ln in enumerate(lines)
                                              if ln.startswith("KB contract"))
          < lines.index(line) < next(i for i, ln in enumerate(lines)
                                     if ln.startswith("modules (")), r.stdout)
    check("cli status: the full run still ends with the disclaimer and the validation status",
          _footer_ok(r.stdout), r.stdout[-200:])
    r = _run_cli("status", "--fast")
    check("cli status --fast: prints 'expectations: skipped (--fast)'",
          "expectations: skipped (--fast)" in r.stdout.splitlines(), r.stdout)
    saved = {k: v for k, v in sys.modules.items() if k == "health.engine"
             or k.startswith("health.engine.")}
    for k in saved:
        sys.modules[k] = None  # type: ignore[assignment]  # makes `import` raise ImportError
    try:
        unavailable = cli.expectations_line()
    finally:
        sys.modules.update(saved)
    check("cli status: an engine that cannot be imported -> 'expectations: unavailable'",
          unavailable == "expectations: unavailable", unavailable)


def test_cli_constants_match_engine_and_config() -> None:
    """One text for the disclaimer and the validation status: the CLI's copies, the
    engine's and config/health/base.yaml (read with the stdlib reader, so this runs on the
    bare floor too)."""
    from health.engine import DISCLAIMER as ENGINE_DISCLAIMER
    from health.engine import VALIDATION_STATUS as ENGINE_STATUS
    from health.registry import parse_yaml

    cfg = parse_yaml(BASE_YAML.read_text(encoding="utf-8"))["model"]
    check("cli constants: disclaimer == engine == config/health/base.yaml",
          DISCLAIMER == ENGINE_DISCLAIMER == cfg["disclaimer"], repr(cfg["disclaimer"]))
    check("cli constants: validation status == engine == config model.validation_status",
          VALIDATION_STATUS == ENGINE_STATUS == cfg["validation_status"],
          repr(cfg["validation_status"]))


def test_status_lists_every_registered_module(tmp: Path) -> None:
    """HREQ-X-01: a flag nothing reads is a CFG defect; `status` is the minimum consumer.
    Every module in config/health/modules.yaml appears on the modules line with its
    state, before the disclaimer; a disabled module is named, never merely absent. No id
    is typed here: the registry is read, so removing a module cannot break this test."""
    from health.registry import ENV_PATH, load_modules

    mods = load_modules()
    ids = [m["id"] for m in mods]
    r = _run_cli("status", "--fast")
    lines = r.stdout.splitlines()
    mod_line = next((i for i, line in enumerate(lines) if line.startswith("modules (")), -1)
    disc = next((i for i, line in enumerate(lines) if line == DISCLAIMER), -1)
    check("cli status: a modules line exists and precedes the disclaimer",
          0 <= mod_line < disc, r.stdout)
    off = [m["id"] for m in mods if m.get("enabled") is not True]   # a module lands off
    disabled = [ln for ln in lines if ln.startswith("modules DISABLED: ")]
    check("cli status: every registered module is on the modules line with its state -- an "
          "enabled one as `<id> on`, a disabled one on the DISABLED line (BUG-20261003-198)",
          mod_line >= 0 and bool(ids)
          and all(f"{i} on" in lines[mod_line] for i in ids if i not in off)
          and disabled == ([f"modules DISABLED: {' · '.join(off)}"] if off else []),
          "\n".join([lines[mod_line] if mod_line >= 0 else "", *disabled]))
    check("cli status: the modules line is read from the registry file, not typed",
          mod_line >= 0 and lines[mod_line].startswith(f"modules ({len(mods)}):"),
          lines[mod_line] if mod_line >= 0 else "")
    first = ids[0] if ids else "?"
    alt = tmp / "modules-one-off.yaml"
    alt.write_text(re.sub(rf"(  - id: {re.escape(first)}\n    enabled: )true", r"\1false",
                          _registry_text(), count=1), encoding="utf-8")
    r = _run_cli("status", "--fast", env_extra={ENV_PATH: str(alt)})
    named = [ln[len("modules DISABLED: "):].split(" · ") for ln in r.stdout.splitlines()
             if ln.startswith("modules DISABLED: ")]
    check(f"cli status: a disabled module ({first}) is named on its own DISABLED line and "
          "not shown as on", len(named) == 1 and first in named[0]
          and f"{first} on" not in r.stdout, r.stdout[-400:])


def test_status_honours_the_module_flags(tmp: Path) -> None:
    """HREQ-X-01 / 07 "Flags and what reads them": the flag governs the surface. With
    engine-v1 off, status prints `model unavailable (engine-v1 disabled in <registry>)` and
    `expectations: skipped (engine-v1 disabled)`, never imports the engine, and still ends
    with the footer; kb-check reads no flag. A registry that cannot be read fails closed."""
    from health.registry import ENV_PATH

    shipped = _registry_text()
    off_text, n = re.subn(r"(  - id: engine-v1\n    enabled: )true", r"\1false", shipped)
    off = tmp / "modules-engine-off.yaml"
    off.write_text(off_text, encoding="utf-8")
    gone_text, n_gone = re.subn(r"  - id: engine-v1\n(?:(?!  - id: ).*\n)*", "", shipped)
    gone = tmp / "modules-engine-gone.yaml"
    gone.write_text(gone_text, encoding="utf-8")
    check("flags: the engine-v1 entry was found in the registry (the plants below are real)",
          n == 1 and n_gone == 1)

    r = _run_cli("status", "--fast")
    model = next((ln for ln in r.stdout.splitlines() if ln.startswith("model ")), "")
    check("flags: engine-v1 on -> the model line is the engine's version",
          re.fullmatch(r"model \d+\.\d+\.\d+", model) is not None, model)
    for args in (("status",), ("status", "--fast")):
        r = _run_cli(*args, env_extra={ENV_PATH: str(off)})
        lines = r.stdout.splitlines()
        check(f"flags: engine-v1 off -> `{' '.join(args)}` prints the model as unavailable "
              "and says why, the expectations as skipped and why, the module as DISABLED, "
              "exits 0 and ends with the footer",
              f"model unavailable (engine-v1 disabled in {off})" in lines
              and "expectations: skipped (engine-v1 disabled)" in lines
              and any(ln.startswith("modules DISABLED: ")
                      and "engine-v1" in ln[len("modules DISABLED: "):].split(" · ")
                      for ln in lines)
              and not any(ln.startswith("expectations ") for ln in lines)
              and r.returncode == 0 and _footer_ok(r.stdout),
              f"exit {r.returncode}\n{r.stdout}\n{r.stderr[-300:]}")
    probe = ("import sys\nfrom health.cli import main\nrc = main(['status'])\n"
             "print('ENGINE-IMPORTED', 'health.engine' in sys.modules)\n")
    r = _run_py("-c", probe, env_extra={ENV_PATH: str(off)})
    check("flags: engine-v1 off -> status does not import health.engine at all",
          "ENGINE-IMPORTED False" in r.stdout, r.stdout[-300:] + r.stderr[-300:])
    r = _run_py("-c", probe)
    check("flags: engine-v1 on -> status does import it (the probe can tell the difference)",
          "ENGINE-IMPORTED True" in r.stdout, r.stdout[-300:] + r.stderr[-300:])
    on, flagged = _run_cli("kb-check"), _run_cli("kb-check", env_extra={ENV_PATH: str(off)})
    check("flags: kb-check reads no flag -- same output and exit with engine-v1 off",
          on.returncode == flagged.returncode == 0 and on.stdout == flagged.stdout)

    r = _run_cli("status", "--fast", env_extra={ENV_PATH: str(gone)})
    check("flags: engine-v1 not in the registry -> model unavailable (not registered), "
          "expectations skipped",
          f"model unavailable (engine-v1 not registered in {gone})" in r.stdout.splitlines()
          and "expectations: skipped (engine-v1 not registered)" in r.stdout.splitlines(),
          r.stdout)
    missing = tmp / "no-registry-here.yaml"
    r = _run_cli("status", env_extra={ENV_PATH: str(missing)})
    lines = r.stdout.splitlines()
    check("flags: a registry that cannot be read fails closed -- model unavailable, "
          "expectations skipped, the registry named missing, the footer last",
          any(ln.startswith("model unavailable (module registry unreadable: "
                            "FileNotFoundError") for ln in lines)
          and "expectations: skipped (module registry unreadable)" in lines
          and any(ln.startswith("modules: registry not found") for ln in lines)
          and _footer_ok(r.stdout), r.stdout)
    r = _run_cli("status", "--fast", "--root", str(tmp / "no-such-kb"),
                 env_extra={ENV_PATH: str(off)})
    check("flags: an unloadable KB with engine-v1 off -> 'KB unavailable', expectations "
          "skipped (engine-v1 disabled), exit 1, the footer last",
          r.returncode == 1 and "KB unavailable" in r.stdout
          and "expectations: skipped (engine-v1 disabled)" in r.stdout.splitlines()
          and _footer_ok(r.stdout), r.stdout)


# ---------------------------------------------------------------------------
# Tests: the module registry, the ledger's removal field, and the gates (process)
# ---------------------------------------------------------------------------
def _registry_text() -> str:
    from health.registry import DEFAULT_PATH

    return DEFAULT_PATH.read_text(encoding="utf-8")


def _run_py(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """`python <args>` from the repository root with src/ on the path."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=300,
                          check=False)


def _by_id(mods: list[dict[str, Any]], mid: str) -> dict[str, Any]:
    return next(m for m in mods if m.get("id") == mid)


def test_registry_entries_are_well_formed(tmp: Path) -> None:
    """HREQ-X-01 / X-02, by structure and never by a typed list of ids (a typed list is what
    turned the gates red the moment a module was removed): every entry has the fields;
    ids are unique; every depends_on names a registered module and the edges have no
    cycle; every paths and tests entry exists (a `file::function` test is defined); every
    file under src/health/ and every health test, fixture and tool belongs to one module;
    every health.* import in src/ follows an edge; every recipe sets the flag off, removes
    its entry and runs the gates; docs/health/07's generated block is the rendering.
    Each rule is shown to fire on a planted violation, and the shipped registry passes."""
    from health.registry import (
        PAGE,
        REQUIRED_FIELDS,
        _edge_problems,
        _existence_problems,
        _field_problems,
        _ownership_problems,
        format_modules,
        is_enabled,
        load_modules,
        module_state,
        page_problems,
        render_page_block,
        validate_modules,
        write_page,
    )

    mods = load_modules()
    check("registry: the stdlib reader gives the same modules as the default reader",
          load_modules(reader="stdlib") == mods)
    check(f"registry: every entry carries {', '.join(REQUIRED_FIELDS)} (HREQ-X-01)",
          bool(mods) and all(all(f in m for f in REQUIRED_FIELDS) for m in mods),
          str([(m.get("id"), [f for f in REQUIRED_FIELDS if f not in m]) for m in mods]))
    problems = validate_modules(mods)
    check("registry: the shipped registry passes every --check rule", not problems,
          "\n        ".join(problems))
    r = _run_py("-m", "health.registry", "--check")
    check("registry: `python -m health.registry --check` exits 0 and says PASS",
          r.returncode == 0 and "PASS" in r.stdout, r.stdout[-800:] + r.stderr[-300:])

    def plant(label: str, mutate: Callable[[list[dict[str, Any]]], Any],
              rule: Callable[[list[dict[str, Any]]], list[str]], want: str) -> None:
        planted = copy.deepcopy(mods)
        mutate(planted)
        got = rule(planted)
        check(f"registry plant ({label}): reported", any(want in p for p in got),
              f"wanted {want!r} in {got}")

    def rid(mid: str) -> Callable[[list[dict[str, Any]]], dict[str, Any]]:
        return lambda ms: _by_id(ms, mid)

    some, other = mods[0]["id"], mods[-1]["id"]
    files = lambda ms: _existence_problems(ms, ROOT)    # noqa: E731
    owned = lambda ms: _ownership_problems(ms, ROOT)    # noqa: E731
    plant("a field missing", lambda ms: rid(some)(ms).pop("tests"), _field_problems,
          "missing tests")
    plant("a flag that is not a boolean", lambda ms: rid(some)(ms).update(enabled="yes"),
          _field_problems, "it must be true or false")
    plant("tests not a list", lambda ms: rid(some)(ms).update(tests="tests/x.py"),
          _field_problems, "tests must be a list")
    plant("a duplicate id", lambda ms: ms.append(copy.deepcopy(rid(some)(ms))),
          _field_problems, f"module {some!r}: registered 2 times")
    for phrase in ("enabled: false", "this entry", "tools/gates.py"):
        plant(f"a recipe that never says {phrase!r}",
              lambda ms, ph=phrase: rid(some)(ms).update(
                  removal=rid(some)(ms)["removal"].replace(ph, "...")),
              _field_problems, f"never says {phrase!r}")
    plant("a dependency that is not registered",
          lambda ms: rid(some)(ms)["depends_on"].append("no-such-module"), _edge_problems,
          "depends on 'no-such-module', which is not registered")
    plant("a dependency on a removed module (its entry gone, the edge left behind)",
          lambda ms: ms.remove(rid(other)(ms)), _edge_problems, "which is not registered")
    plant("a cycle", lambda ms: rid(other)(ms)["depends_on"].append(some), _edge_problems,
          "depends_on cycle")
    plant("a path that does not exist",
          lambda ms: rid(some)(ms)["paths"].append("src/health/no_such_dir/"), files,
          "path src/health/no_such_dir/ does not exist")
    plant("a tests file that does not exist (a module removed with it)",
          lambda ms: rid(some)(ms)["tests"].append("tests/no_such_selftest.py"), files,
          "tests/no_such_selftest.py does not exist")
    never = "_".join(("test", "that", "was", "never", "written"))   # not spelled out: the
    plant("a tests function that is not defined",                    # check reads this file
          lambda ms: rid(some)(ms)["tests"].append(f"tests/health_kb_selftest.py::{never}"),
          files, f"`def {never}(` is not in tests/health_kb_selftest.py")
    owner_of = lambda rel: next(m["id"] for m in mods    # noqa: E731
                                if any(rel.startswith(p.rstrip("/")) for p in m["paths"]))
    reg_owner = owner_of("src/health/registry.py")
    plant("a file no module owns",
          lambda ms: rid(reg_owner)(ms)["paths"].remove("src/health/registry.py"), owned,
          "src/health/registry.py: in no module's paths")
    plant("a file two modules own",
          lambda ms: rid(some if some != reg_owner else other)(ms)["paths"].append(
              "src/health/registry.py"), owned, "a file belongs to one module")
    importer = next((m for m in mods if "errors" in m["depends_on"]
                     and any(p.startswith("src/") for p in m["paths"])), None)
    if importer is not None:
        plant("an import with no depends_on edge (U4)",
              lambda ms: rid(importer["id"])(ms)["depends_on"].remove("errors"), owned,
              "imports health.errors")

    page_copy = tmp / "07.md"
    page_copy.write_text(PAGE.read_text(encoding="utf-8"), encoding="utf-8")
    check("07: the generated block is the registry's rendering", not page_problems(mods),
          str(page_problems(mods)))
    changed = copy.deepcopy(mods)
    changed[0]["owner"] = "someone-else"
    check("07 plant (the registry changed, the page did not): reported",
          bool(page_problems(changed, page_copy)), "")
    check("07: --write-07 brings the page back to the registry, and only the block changes",
          write_page(changed, page_copy) and not page_problems(changed, page_copy)
          and page_copy.read_text(encoding="utf-8").replace(render_page_block(changed), "")
          == PAGE.read_text(encoding="utf-8").replace(render_page_block(mods), ""))

    check("flags: module_state reads enabled, disabled and unregistered",
          module_state(some, [{"id": some, "enabled": True}]) == "enabled"
          and module_state(some, [{"id": some, "enabled": False}]) == "disabled"
          and module_state(some, [{"id": some, "enabled": "true"}]) == "disabled"
          and module_state(some, []) == "unregistered")
    check("flags: two entries under one id read as disabled unless both are on (fails closed)",
          not is_enabled(some, [{"id": some, "enabled": True}, {"id": some, "enabled": False}])
          and is_enabled(some, [{"id": some, "enabled": True}]))
    alt = tmp / "modules.yaml"
    alt.write_text(re.sub(rf"(  - id: {re.escape(some)}\n    enabled: )true", r"\1false",
                          _registry_text(), count=1), encoding="utf-8")
    for reader in ("auto", "stdlib"):
        out = format_modules(load_modules(alt, reader=reader))
        check(f"registry ({reader} reader): a disabled module is named on its own DISABLED line",
              f"modules DISABLED: {some}" in out and f"{some} on" not in out, out)


def test_removal_recipes_name_every_live_reference() -> None:
    """U7 / D8 / D18: a recipe that forgets a file leaves a stale reference or a red gate.
    For every module, each live file (code, tests, tools, config, CI, packaging, READMEs, 04,
    07) that names one of its paths and is not removed before it must be named in its
    recipe. Planted: no module depending on process (the false "process can go at any
    time": the engine's self-test reads process's files), and an engine-v1 recipe that
    forgets config/health/base.yaml or the root README."""
    from health.registry import dependants, load_modules, recipe_gaps

    mods = load_modules()
    gaps = recipe_gaps(mods)
    check("recipes: every recipe names every live file that names its paths", not gaps,
          "\n        ".join(gaps))
    ids = {m["id"] for m in mods}
    check("recipes: process is depended on by every other module (it goes last)",
          "process" not in ids or dependants(mods, "process") == ids - {"process"},
          str(sorted(dependants(mods, "process"))))
    if "engine-v1" not in ids:
        return
    no_edge = copy.deepcopy(mods)
    for m in no_edge:                      # the registry as it was: nothing depends on process
        if "process" in m["depends_on"]:
            m["depends_on"].remove("process")
    engine_test = next(p for p in _by_id(mods, "engine-v1")["paths"]
                       if p.startswith("tests/") and p.endswith(".py"))
    got = [g for g in recipe_gaps(no_edge) if g.startswith("module 'process'")]
    check("recipes plant (no module depends on process -- \"process can go at any time\"): "
          "process's recipe is shown to miss the engine's self-test, which reads "
          "process's configuration and documents",
          any(f"{engine_test} names" in g for g in got), str(got))
    for name in ("config/health/base.yaml", "README.md"):
        forgot = copy.deepcopy(mods)
        e = _by_id(forgot, "engine-v1")
        e["removal"] = re.sub(rf"(?<![\w./-]){re.escape(name)}(?![\w/-])", "(a file)",
                              e["removal"])
        got = recipe_gaps(forgot)
        check(f"recipes plant (engine-v1's recipe forgets {name}): reported by name",
              any(g.startswith(f"module 'engine-v1': {name} names") for g in got), str(got))


def _buglog() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("buglog_probe", ROOT / "tools" / "buglog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_buglog_removed_with_module_exempts_deleted_files(tmp: Path) -> None:
    """U0: a removal recipe deletes files on purpose and the ledger is append-only, so the
    entries citing them carry `removed_with_module` -- and only those entries skip the
    existence checks. Planted on a scratch tree: the same missing location and test pass
    with the field and fail without it; the field cannot name a module that is still
    registered; a fixed entry still needs its test named and its resolved_at; and
    --mark-removed adds exactly one line per matching entry and nothing else."""
    bl = _buglog()
    (tmp / "ledger").mkdir(exist_ok=True)
    md = tmp / "ledger" / "BUGS.md"

    def bug(bid: str, **extra: Any) -> dict[str, Any]:
        b = {"id": bid, "status": "fixed", "resolved_at": "2026-10-03T12:00:00-05:00",
             "locations": [{"file": "src/health/gone_with_the_module.py", "line": 1}],
             "regression_test": "tests/gone_selftest.py::test_gone"}
        b.update(extra)
        return b

    ids = [f"BUG-20991231-{n:03d}" for n in range(1, 7)]
    bugs = [bug(ids[0], removed_with_module="gone-v1"),
            bug(ids[1]),
            bug(ids[2], removed_with_module="still-here"),
            bug(ids[3], removed_with_module=["gone-v1", "gone-v2"]),
            bug(ids[4], removed_with_module="gone-v1", regression_test=None),
            bug(ids[5], removed_with_module="Not An Id")]
    md.write_text("\n".join(ids) + "\n", encoding="utf-8")
    old = bl.ROOT, bl.MARKDOWN
    try:
        bl.ROOT, bl.MARKDOWN = tmp / "ledger", md
        problems = bl.check({"repo": "x/y", "bugs": bugs}, registered={"still-here"})
    finally:
        bl.ROOT, bl.MARKDOWN = old

    def about(bid: str) -> list[str]:
        return [p for p in problems if p.startswith(bid)]

    check("ledger: with removed_with_module, a missing location and a missing test pass",
          about(ids[0]) == [] and about(ids[3]) == [], str(about(ids[0]) + about(ids[3])))
    check("ledger: the same entry without the field fails on both",
          any("location src/health/gone_with_the_module.py does not exist" in p
              for p in about(ids[1]))
          and any("regression test file tests/gone_selftest.py does not exist" in p
                  for p in about(ids[1])), str(about(ids[1])))
    check("ledger: the field naming a module that is still registered is itself a failure",
          any("names a module that is still in" in p for p in about(ids[2])), str(about(ids[2])))
    check("ledger: a fixed entry with the field must still name its regression test",
          any("NO regression test" in p for p in about(ids[4])), str(about(ids[4])))
    check("ledger: the field must be a module id",
          any("is not a module id" in p for p in about(ids[5])), str(about(ids[5])))

    text = (f"bugs:\n- id: {ids[0]}\n  summary: one\n  status: open\n  locations:\n"
            f"  - {{file: src/health/example/model.py, line: 3}}\n\n"
            f"- id: {ids[1]}\n  summary: two\n  status: fixed\n"
            f"  regression_test: tests/health_kb_selftest.py::test_a\n\n"
            f"- id: {ids[2]}\n  summary: three\n  status: fixed\n"
            f"  regression_test: tests/health_kb_selftest.py::test_b\n")
    parsed = [{"id": ids[0], "locations": [{"file": "src/health/example/model.py", "line": 3}]},
              {"id": ids[1], "regression_test": "tests/health_kb_selftest.py::test_a"},
              {"id": ids[2], "regression_test": "tests/health_kb_selftest.py::test_b"}]
    new, marked = bl.mark_removed(text, parsed, "example-v1", ["src/health/example/"],
                                  ["tests/health_kb_selftest.py::test_a"])
    check("--mark-removed: marks the entries citing the module's paths or its tests, only",
          marked == [ids[0], ids[1]], str(marked))
    check("--mark-removed: one line after each marked entry's status line, nothing else changed",
          new.replace("  removed_with_module: example-v1\n", "") == text
          and new.count("removed_with_module") == 2
          and f"- id: {ids[0]}\n  summary: one\n  status: open\n  removed_with_module: example-v1\n"
          in new, new)
    parsed[0]["removed_with_module"] = parsed[1]["removed_with_module"] = "example-v1"
    again, marked_again = bl.mark_removed(new, parsed, "example-v1", ["src/health/example/"],
                                          ["tests/health_kb_selftest.py::test_a"])
    check("--mark-removed: running it twice marks nothing more", marked_again == []
          and again == new, str(marked_again))


def test_registry_live_scan_reads_tracked_files_only(tmp: Path) -> None:
    """The registry's live-reference scan (every file that names a module's path must be
    named by that module's recipe) walked LIVE_SCOPE on disk, so an ignored build artefact
    (src/navanax.egg-info/SOURCES.txt) made `health.registry --check` fail in the main
    checkout (BUG-20261003-170). It reads `git ls-files` now; outside a checkout it walks,
    so a copy with no .git is still checked."""
    from health.registry import _live_files
    git = shutil.which("git")
    check("registry live scan: git is available here (the scan reads tracked files through it)",
          git is not None)
    if git is None:
        return
    repo = tmp / "live-repo"
    (repo / "src" / "navanax.egg-info").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / ".gitignore").write_text("*.egg-info/\n", encoding="utf-8")
    (repo / "README.md").write_text("tracked: names src/health/cli.py\n", encoding="utf-8")
    (repo / "src" / "navanax.egg-info" / "SOURCES.txt").write_text(
        "ignored: names src/health/cli.py\n", encoding="utf-8")
    (repo / "tools" / "scratch.py").write_text("untracked: names src/health/cli.py\n",
                                               encoding="utf-8")
    for args in (["init", "-q"], ["add", ".gitignore", "README.md"]):
        subprocess.run([git, "-C", str(repo), *args], check=True, capture_output=True)
    names = [rel for rel, _ in _live_files(repo)]
    check("registry live scan: in a checkout only the tracked file is read (ignored and "
          "untracked ones are not)", names == ["README.md"], str(names))
    plain = tmp / "live-plain"
    shutil.copytree(repo, plain, ignore=shutil.ignore_patterns(".git"))
    walked = sorted(rel for rel, _ in _live_files(plain))
    check("registry live scan: outside a checkout every file is walked (a copy with no .git "
          "is still checked)", walked == ["README.md", "src/navanax.egg-info/SOURCES.txt",
                                           "tools/scratch.py"], str(walked))
    real = [rel for rel, _ in _live_files(ROOT)]
    stray = [r for r in real if ".egg-info" in r or ".claude/worktrees" in r]
    check("registry live scan: the repository scan holds no ignored path", real and not stray,
          str(stray[:5]))


def test_registry_one_segment_directory_is_matched_with_its_slash(tmp: Path) -> None:
    """BUG-20261003-199: the live-reference scan searched for a path with its slash
    stripped, so a one-segment directory was "named" by any file that said the word --
    prose, or a module id spelled like it -- and the gate failed. Planted on a scratch tree
    (no .git, so it is walked) with `kiosk/`: prose is not a reference; `kiosk/...` is."""
    from health.registry import recipe_gaps
    tree = tmp / "one-segment"
    (tree / "docs").mkdir(parents=True)
    (tree / "README.md").write_text("Runs as a kiosk app (module kiosk-app).\n",
                                    encoding="utf-8")
    (tree / "docs" / "README.md").write_text("Double-click kiosk/Kiosk.app.\n",
                                             encoding="utf-8")
    mod = {"id": "planted", "paths": ["kiosk/"], "depends_on": [],
           "removal": "Set `enabled: false`; remove this entry; run tools/gates.py."}
    got = recipe_gaps([mod], tree)
    check("registry one-segment path: prose and the module id are not references",
          not any(" README.md names" in g for g in got), str(got))
    check("registry one-segment path: `kiosk/...` is, and the recipe must name its file",
          any(g.startswith("module 'planted': docs/README.md names kiosk/") for g in got),
          str(got))


def test_buglog_rule_4_reads_tracked_files_only(tmp: Path) -> None:
    """Rule (4) -- every BUG id mentioned in the repository is in the ledger -- walked the
    whole directory tree, so `buglog.py --check` in the main checkout failed on ids in the
    builders' git-ignored worktrees under .claude/worktrees/. It reads `git ls-files` now.
    Planted in a scratch git repository: an id in a tracked file is reported, the same kind
    of id in an ignored or an untracked file is not; outside a checkout the walk is the
    fallback (so a copy with no .git is still checked)."""
    git = shutil.which("git")
    check("ledger rule 4: git is available here (the gate reads tracked files through it)",
          git is not None)
    if git is None:
        return
    bl = _buglog()
    repo = tmp / "rule4-repo"
    (repo / "ignored").mkdir(parents=True)
    (repo / "docs").mkdir()
    tracked, ignored, untracked = (f"BUG-20991231-{n}" for n in ("701", "702", "703"))
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repo / "docs" / "notes.md").write_text(f"cites {tracked}\n", encoding="utf-8")
    (repo / "ignored" / "BUGS.md").write_text(f"a worktree's copy cites {ignored}\n",
                                              encoding="utf-8")
    (repo / "scratch.md").write_text(f"untracked, cites {untracked}\n", encoding="utf-8")
    (repo / "BUGS.md").write_text("", encoding="utf-8")
    for args in (["init", "-q"], ["add", ".gitignore", "docs/notes.md"]):
        subprocess.run([git, "-C", str(repo), *args], check=True, capture_output=True)
    plain = tmp / "rule4-plain"
    plain.mkdir()
    (plain / "x.md").write_text(f"cites {ignored}\n", encoding="utf-8")
    (plain / "BUGS.md").write_text("", encoding="utf-8")
    old = bl.ROOT, bl.MARKDOWN
    try:
        bl.ROOT, bl.MARKDOWN = repo, repo / "BUGS.md"
        problems = bl.check({"repo": "x/y", "bugs": []}, registered=set())
        listed = sorted(f.relative_to(repo).as_posix() for f in bl.repo_files())
        bl.ROOT, bl.MARKDOWN = plain, plain / "BUGS.md"
        walked = bl.check({"repo": "x/y", "bugs": []}, registered=set())
    finally:
        bl.ROOT, bl.MARKDOWN = old
    check("ledger rule 4: the files read are the tracked ones", listed == [".gitignore",
                                                                         "docs/notes.md"],
          str(listed))
    check("ledger rule 4: an id in a tracked file that the ledger lacks is reported",
          any(p.startswith(tracked) and "docs/notes.md" in p for p in problems), str(problems))
    check("ledger rule 4: the same in an ignored path (a worktree) or an untracked file is not",
          not any(ignored in p or untracked in p for p in problems), str(problems))
    check("ledger rule 4: outside a git checkout the directory walk still reads every file",
          any(p.startswith(ignored) for p in walked), str(walked))


def test_buglog_rule_3_refuses_placeholder_tests(tmp: Path) -> None:
    """Rule (3) skipped every regression_test without `::`, so a fixed entry whose test read
    "none -- doc-only; re-verified by the qa-auditor sweep" passed. A fixed entry's test is
    `file::function` or exactly the literal DOCS_ONLY, and the literal holds only when every
    location is documentation (docs/, .md/.mmd/.svg, a comment line of a .yaml). Planted on
    a scratch tree, both sides of each rule; removed_with_module does not bypass it."""
    bl = _buglog()
    root = tmp / "rule3"
    for d in ("docs/health", "config", "src", "tests"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "docs" / "health" / "page.md").write_text("text\n", encoding="utf-8")
    (root / "config" / "base.yaml").write_text("# a comment\nkey: 1\n", encoding="utf-8")
    (root / "src" / "code.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "t_selftest.py").write_text("def test_real():\n    pass\n",
                                                  encoding="utf-8")
    placeholder = "none — doc-only; re-verified by the qa-auditor sweep"
    doc, cfg_comment, cfg_value, code = (
        {"file": "docs/health/page.md", "line": 1}, {"file": "config/base.yaml", "line": 1},
        {"file": "config/base.yaml", "line": 2}, {"file": "src/code.py", "line": 1})
    legacy = sorted(bl.PRE_STRICT_PROSE_TESTS)[0]
    cases = {   # id suffix: (entry fields, should it pass?)
        "801": ({"regression_test": placeholder, "locations": [code]}, False),
        "802": ({"regression_test": placeholder, "locations": [doc]}, False),
        "803": ({"regression_test": bl.DOCS_ONLY, "locations": [doc, cfg_comment]}, True),
        "804": ({"regression_test": bl.DOCS_ONLY, "locations": [doc, code]}, False),
        "805": ({"regression_test": bl.DOCS_ONLY, "locations": [cfg_value]}, False),
        "806": ({"regression_test": bl.DOCS_ONLY, "locations": []}, False),
        "807": ({"regression_test": [placeholder], "locations": [code],
                 "removed_with_module": "gone-v1"}, False),
        "808": ({"regression_test": "tests/gone_selftest.py::test_x", "locations": [code],
                 "removed_with_module": "gone-v1"}, True),
        "809": ({"regression_test": "tests/t_selftest.py::test_real", "locations": [code]},
                True),
        "810": ({"regression_test": "tests/t_selftest.py", "locations": [code]}, False),
        "811": ({"regression_test": placeholder, "locations": [code], "status": "open"}, True),
    }
    bugs: list[dict[str, Any]] = []
    for n, (fields, _) in cases.items():
        b: dict[str, Any] = {"id": f"BUG-20991231-{n}", "status": "fixed",
                             "resolved_at": "2026-10-03T12:00:00-05:00"}
        b.update(fields)
        bugs.append(b)
    bugs.append({"id": legacy, "status": "fixed", "resolved_at": "2026-09-09T12:00:00-05:00",
                 "regression_test": "CI itself", "locations": [code]})
    (root / "BUGS.md").write_text("\n".join(b["id"] for b in bugs) + "\n", encoding="utf-8")
    old = bl.ROOT, bl.MARKDOWN
    try:
        bl.ROOT, bl.MARKDOWN = root, root / "BUGS.md"
        problems = bl.check({"repo": "x/y", "bugs": bugs}, registered=set())
    finally:
        bl.ROOT, bl.MARKDOWN = old
    for n, (fields, ok) in cases.items():
        mine = [p for p in problems if p.startswith(f"BUG-20991231-{n}")]
        label = (f"{'passes' if ok else 'fails, naming the entry'}: {fields.get('status', 'fixed')}"
                 f", test {str(fields['regression_test'])[:40]!r}, locations "
                 f"{[loc['file'] + ':' + str(loc['line']) for loc in fields['locations']]}"
                 + (", removed_with_module" if "removed_with_module" in fields else ""))
        check(f"ledger rule 3 ({n}) {label}", (not mine) if ok else bool(mine), str(mine))
    check(f"ledger rule 3: a pre-strict entry ({legacy}, prose naming a CI step) is the "
          "listed exception, not a pattern", not [p for p in problems if p.startswith(legacy)]
          and len(bl.PRE_STRICT_PROSE_TESTS) == 4, str(sorted(bl.PRE_STRICT_PROSE_TESTS)))
    check("ledger rule 3: the literal is exactly 'docs-only (no mechanical guard)'",
          bl.DOCS_ONLY == "docs-only (no mechanical guard)", bl.DOCS_ONLY)


def test_gates_health_steps_remove_cleanly() -> None:
    """U2: a removal recipe deletes a module's statements from tools/gates.py. Nothing it
    leaves behind may be an unused import (ruff F401 turns the gate red, as `import os` did
    when the kb-check step went). Each health statement of main(), and all of them with the
    health helpers, are removed from the syntax tree and the imports checked for use."""
    import ast

    src = (ROOT / "tools" / "gates.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")

    def steps(body: list[ast.stmt]) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        for st in body:
            if isinstance(st, ast.If):
                out += steps(st.body) + steps(st.orelse)
            elif "health" in (ast.get_source_segment(src, st) or "").lower():
                out.append(st)
        return out

    def unused_imports(t: ast.Module) -> list[str]:
        bound: list[str] = []
        for n in t.body:
            if isinstance(n, ast.Import):
                bound += [(a.asname or a.name).split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module != "__future__":
                bound += [a.asname or a.name for a in n.names]
        used = {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        return sorted(set(bound) - used)

    def _is_health_helper(st: ast.stmt) -> bool:
        names = [st.name] if isinstance(st, ast.FunctionDef) else [
            t.id for t in getattr(st, "targets", [getattr(st, "target", None)])
            if isinstance(t, ast.Name)]
        return any(w in n.lower() for n in names for w in ("health", "golden", "node"))

    class Drop(ast.NodeTransformer):
        """Remove the statements starting on `lines` (and, with helpers, the health
        functions and constants) wherever they sit; an emptied body becomes `pass`."""

        def __init__(self, lines: set[int], helpers: bool) -> None:
            self.lines, self.helpers = lines, helpers

        def generic_visit(self, node: ast.AST) -> ast.AST:
            super().generic_visit(node)
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and all(isinstance(s, ast.stmt) for s in body):
                kept = [s for s in body if getattr(s, "lineno", None) not in self.lines
                        and not (self.helpers and _is_health_helper(s))]
                node.body = kept or [ast.Pass()]
            return node

    found = steps(main.body)    # how many depends on which modules are still registered
    check(f"gates: main() has health steps to remove ({len(found)})", bool(found),
          str([ast.get_source_segment(src, s) for s in found]))
    before = unused_imports(tree)
    check("gates: no unused import as shipped", not before, str(before))
    for st in found:
        label = (ast.get_source_segment(src, st) or "").splitlines()[0][:70]
        left = unused_imports(Drop({st.lineno}, helpers=False).visit(ast.parse(src)))
        check(f"gates: removing `{label}` leaves no unused import", not left, str(left))
    left = unused_imports(Drop({s.lineno for s in found}, helpers=True).visit(ast.parse(src)))
    check("gates: removing every health step and helper (the whole subsystem) leaves no "
          "unused import", not left, str(left))


def test_gates_never_reads_green_on_a_skip() -> None:
    """D16: the golden fixture is checked by a gate when Node exists, and when it does not
    the gate says SKIPPED loudly and the verdict line cannot read ALL GATES GREEN. The
    generator exists exactly when gates.py runs its --check (removing engine-v1 removes
    both, so this holds before and after)."""
    import contextlib
    import importlib.util
    import io

    spec = importlib.util.spec_from_file_location("gates_probe", ROOT / "tools" / "gates.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    check("gates: nothing skipped -> ALL GATES GREEN", g.verdict() == "ALL GATES GREEN")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        g.run("probe", ["no-such-binary-anywhere-health-probe"], optional=True)
    check("gates: an optional gate whose tool is missing is SKIPPED and named in the verdict",
          "SKIPPED" in buf.getvalue() and "ALL GATES GREEN" not in g.verdict()
          and "1 SKIPPED" in g.verdict() and "probe" in g.verdict(), g.verdict())
    g.SKIPPED.clear()
    generator = ROOT / "tools" / "health_golden.mjs"
    main_src = (ROOT / "tools" / "gates.py").read_text(encoding="utf-8").split("def main(")[1]
    wired = hasattr(g, "health_golden_check") and "health_golden_check()" in main_src
    check("gates: the golden generator exists exactly when main() runs its --check",
          generator.exists() == wired, f"generator {generator.exists()}, wired {wired}")
    if not wired:
        return
    g.node_binary = lambda: None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        g.health_golden_check()
    check("gates: no Node -> 'SKIPPED (no Node binary): golden --check not run', counted",
          "SKIPPED (no Node binary): golden --check not run" in buf.getvalue()
          and len(g.SKIPPED) == 1 and g.verdict().startswith("GATES GREEN, 1 SKIPPED"),
          buf.getvalue() + g.verdict())
    g.SKIPPED.clear()
    calls: list[tuple[str, list[str]]] = []
    g.node_binary = lambda: "/somewhere/node"
    g.run = lambda label, cmd, **kw: calls.append((label, cmd)) or ""
    g.health_golden_check()
    check("gates: with Node, the generator runs with --check from the repository root",
          calls == [("health golden fixture", ["/somewhere/node",
                                               generator.relative_to(ROOT).as_posix(),
                                               "--check"])] and not g.SKIPPED, str(calls))


def test_docs_say_what_gates_runs() -> None:
    """D23: "gates.py runs all of the above" was false -- it never runs `health.cli status`.
    In each README run block that names tools/gates.py, every command above the gates line
    is either run by tools/gates.py or named on the gates line as not run."""
    gates = (ROOT / "tools" / "gates.py").read_text(encoding="utf-8")
    seen = 0
    for doc in (ROOT / "README.md", ROOT / "docs" / "health" / "README.md"):
        if not doc.exists():
            continue
        for block in re.findall(r"```bash\n(.*?)```", doc.read_text(encoding="utf-8"), re.S):
            lines = block.splitlines()
            at = next((i for i, ln in enumerate(lines) if "tools/gates.py" in ln), None)
            if at is None or "health" not in block:
                continue
            seen += 1
            claim = lines[at].split("#", 1)[1] if "#" in lines[at] else ""
            for ln in lines[:at]:
                cmd = ln.split("#", 1)[0].strip()
                if not cmd:
                    continue
                m = re.search(r"-m (health\.\w+) (\S+)", cmd)
                if m:
                    key, name = f'"{m.group(1)}", "{m.group(2)}"', m.group(2)
                else:
                    script = next((w for w in cmd.split() if w.endswith((".py", ".mjs"))), cmd)
                    key, name = f'"{script}"', script
                ran = key in gates
                check(f"{doc.relative_to(ROOT)}: `{cmd}` is run by tools/gates.py or the gates "
                      "line says it is not", ran or f"`{name}`" in claim,
                      f"gates line: {lines[at]!r}")
            check(f"{doc.relative_to(ROOT)}: the gates line does not claim 'all of the above'",
                  "all of the above" not in claim.lower(), lines[at])
    check("docs: at least one README run block names tools/gates.py", seen >= 1, str(seen))


# Registry cases with their PyYAML answer written out, so the stdlib reader is tested on
# the bare floor too. (id, enabled, owner, phase, adr, depends_on) per module.
_REGISTRY_CASES: list[tuple[str, str, Any]] = [
    ("YAML 1.1 booleans yes / on / TRUE / Off",
     "modules:\n  - id: a\n    enabled: yes\n  - id: b\n    enabled: on\n"
     "  - id: c\n    enabled: TRUE\n  - id: d\n    enabled: Off\n",
     [("a", True), ("b", True), ("c", True), ("d", False)]),
    ("mixed-case 'tRuE' is a string (PyYAML), so the module reads as disabled",
     "modules:\n  - id: a\n    enabled: tRuE\n", [("a", "tRuE")]),
    ("trailing comment after a flag",
     "modules:\n  - id: a\n    enabled: false   # temporarily off\n", [("a", False)]),
    ("four-space indentation", "modules:\n    - id: a\n      enabled: true\n      owner: x\n",
     [("a", True, "x")]),
    ("sequence at its key's indentation", "modules:\n- id: a\n  enabled: false\n",
     [("a", False)]),
    ("id not the first key", "modules:\n  - enabled: false\n    id: a\n", [("a", False)]),
    ("flow mapping item", "modules:\n  - {id: a, enabled: false}\n", [("a", False)]),
    ("quoted id", 'modules:\n  - id: "kb-v1"\n    enabled: true\n', [("kb-v1", True)]),
    ("inline list with spaces and quotes",
     'modules:\n  - id: a\n    depends_on: [ reference-v1 , "errors" ]\n',
     [("a", None, None, None, None, ["reference-v1", "errors"])]),
    ("inline list with a quoted comma",
     'modules:\n  - id: a\n    depends_on: ["a,b", c]\n',
     [("a", None, None, None, None, ["a,b", "c"])]),
    ("block list indented under its key",
     "modules:\n  - id: a\n    depends_on:\n        - x\n        - y\n",
     [("a", None, None, None, None, ["x", "y"])]),
    ("block list at its key's indentation",
     "modules:\n  - id: a\n    depends_on:\n    - x\n    - y\n",
     [("a", None, None, None, None, ["x", "y"])]),
    ("hash inside a value vs a comment",
     'modules:\n  - id: a\n    owner: platform-engineer # TODO\n    adr: "ADR#7"\n',
     [("a", None, "platform-engineer", None, "ADR#7")]),
    ("null owner (~) and an empty value",
     "modules:\n  - id: a\n    owner: ~\n    depends_on:\n    phase: 2\n",
     [("a", None, None, 2, None, None)]),
    ("YAML 1.1 ints: octal 010, float 0.5",
     "modules:\n  - id: a\n    phase: 010\n  - id: b\n    phase: 0.5\n",
     [("a", None, None, 8), ("b", None, None, 0.5)]),
    ("a second top-level list is not modules",
     "modules:\n  - id: a\n    enabled: true\nretired:\n  - id: old\n    enabled: false\n",
     [("a", True)]),
    ("CRLF line endings", "modules:\r\n  - id: a\r\n    enabled: false\r\n", [("a", False)]),
    ("folded block scalar, stripped",
     "modules:\n  - id: a\n    owner: >-\n      two\n      words\n", [("a", None, "two words")]),
]
_REGISTRY_REFUSED: list[tuple[str, str]] = [
    ("a tab in indentation", "modules:\n  - id: a\n\tenabled: true\n"),
    ("a tab after the colon", "modules:\n  - id: a\n    enabled:\ttrue\n"),
    ("a tab after a value", "modules:\n  - id: a\n    enabled: true\t\n"),
    ("an anchor (outside the subset)", "modules:\n  - &m id: a\n"),
    ("two documents", "modules: []\n---\nmodules: []\n"),
    ("a continuation line holding ': '", "modules:\n  - id: a\n    owner: x\n      y: z\n"),
]
_FIELDS = ("id", "enabled", "owner", "phase", "adr", "depends_on")


def _four_space(text: str) -> str:
    """`text` with every indentation level doubled, including the column after `- `,
    so the structure is unchanged (doubling leading spaces alone breaks `- key:` items)."""
    out = []
    for line in text.splitlines(True):
        m = re.match(r"^( *)(- )?(.*)", line, re.S)
        ind, dash, rest = m.groups() if m else ("", None, line)
        out.append(" " * (2 * len(ind)) + ("-   " if dash else "") + rest)
    return "".join(out)


def _fields(mods: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [tuple(m.get(k) for k in _FIELDS) for m in mods]


def test_registry_reader_reads_the_yaml_subset() -> None:
    """The stdlib reader on the cases where the 2026-10-03 rigor review found the old
    scanner diverging from PyYAML in 17 of 20, answers written out (no PyYAML needed)."""
    from health.registry import YamlSubsetError, _scan

    wrong = []
    for label, text, expected in _REGISTRY_CASES:
        want = [tuple(list(e) + [None] * (len(_FIELDS) - len(e))) for e in expected]
        try:
            got: Any = _fields(_scan(text))
        except Exception as exc:  # noqa: BLE001 - reported as a wrong answer
            got = repr(exc)
        if got != want:
            wrong.append(f"{label}: {got} != {want}")
    check(f"registry reader: {len(_REGISTRY_CASES)} subset cases read as PyYAML reads them",
          not wrong, "\n        ".join(wrong))
    accepted = []
    for label, text in _REGISTRY_REFUSED:
        try:
            _scan(text)
            accepted.append(label)
        except YamlSubsetError:
            pass
    check("registry reader: tabs where PyYAML refuses them, anchors, several documents and "
          "bad continuations are refused, not read differently", not accepted, str(accepted))


@needs("yaml")
def test_registry_reader_agrees_with_pyyaml(tmp: Path) -> None:
    """The stdlib reader must give PyYAML's answer -- otherwise the pre-install gate and
    the post-install gate would read two different registries. Compared on the shipped
    registry and on four re-writings of it, field for field and as whole documents."""
    import yaml

    from health.registry import (
        DEFAULT_PATH,
        YamlSubsetError,
        format_modules,
        load_modules,
        parse_yaml,
    )

    shipped = DEFAULT_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(shipped)
    variants = {
        "the shipped file": shipped,
        "yaml.safe_dump round-trip (sorted keys)": yaml.safe_dump(data),
        "yaml.safe_dump round-trip (file order)": yaml.safe_dump(data, sort_keys=False),
        "yaml.safe_dump at width 40 (folded long scalars)":
            yaml.safe_dump(data, sort_keys=False, width=40),
        "yaml.safe_dump in flow style": yaml.safe_dump(data, default_flow_style=True),
        "a trailing comment after every flag":
            re.sub(r"(enabled: \w+)", r"\1   # flag", shipped),
        "4-space indentation (every level doubled, '-   ' entries)": _four_space(shipped),
        "id as the second key": re.sub(r"  - id: (\S+)\n    (enabled: \w+)",
                                       r"  - \2\n    id: \1", shipped),
        "config/health/base.yaml": BASE_YAML.read_text(encoding="utf-8"),
    }
    check("registry variants: the comment, indentation and id-second rewrites really changed "
          "the text", len({variants[k] for k in ("the shipped file",
                                                  "a trailing comment after every flag",
                                                  "4-space indentation (every level "
                                                  "doubled, '-   ' entries)",
                                                  "id as the second key")}) == 4)
    for label, text in variants.items():
        want = yaml.safe_load(text)
        try:
            got = parse_yaml(text)
        except YamlSubsetError as exc:
            got = f"refused: {exc}"
        check(f"registry ({label}): the stdlib reader's document == PyYAML's", got == want,
              f"\n  stdlib: {str(got)[:300]}\n  pyyaml: {str(want)[:300]}")
        if label == "config/health/base.yaml":
            continue
        path = tmp / "modules.yaml"
        path.write_text(text, encoding="utf-8")
        a = load_modules(path, reader="stdlib")
        b = load_modules(path, reader="pyyaml")
        check(f"registry ({label}): identical (id, enabled, owner, phase, adr, depends_on) and "
              "status line", _fields(a) == _fields(b) and format_modules(a) == format_modules(b),
              f"\n  stdlib: {_fields(a)}\n  pyyaml: {_fields(b)}")
    for label, text, _ in _REGISTRY_CASES:
        check(f"registry case ({label}): the written-out answer is PyYAML's",
              parse_yaml(text) == yaml.safe_load(text))
    for label, text in _REGISTRY_REFUSED:
        refused_by_pyyaml = False
        try:
            yaml.safe_load(text)
        except yaml.YAMLError:
            refused_by_pyyaml = True
        if "outside the subset" in label:
            continue
        check(f"registry refusal ({label}): PyYAML refuses it too", refused_by_pyyaml)


# ---------------------------------------------------------------------------
# Runner (same conventions as tests/selftest.py)
# ---------------------------------------------------------------------------
def discover() -> list[tuple[str, Any]]:
    """Every `test_*` function in this module, in definition order."""
    import inspect

    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    tests.sort(key=lambda nf: inspect.getsourcelines(nf[1])[1])
    return tests


def exit_code(n_failed: int, n_skipped: int, *, strict: bool) -> int:
    if n_failed:
        return 1
    if strict and n_skipped:
        return 2
    return 0


def summary_lines(n_tests: int, n_passed: int, n_failed: int,
                  skipped: list[tuple[str, tuple[str, ...]]]) -> list[str]:
    lines: list[str] = []
    if skipped:
        lines.append(f"SKIPPED ({len(skipped)}) -- not run here; `--no-skips` runs them for real:")
        for name, mods in skipped:
            lines.append(f"  {name}  (needs: {', '.join(mods)})")
        lines.append("")
    mods = sorted({m for _, ms in skipped for m in ms})
    tail = f", {len(skipped)} skipped (needs: {', '.join(mods)})" if skipped else ", 0 skipped"
    lines.append(f"{n_tests} test functions, {n_passed} passed, {n_failed} failed{tail}")
    return lines


def run_suite(tests: list[tuple[str, Any]], *, strict: bool = False) -> int:
    import inspect

    tmp = Path(tempfile.mkdtemp(prefix="health-kb-selftest-"))
    try:
        for name, fn in tests:
            missing = missing_modules(fn)
            if missing:
                SKIPPED.append((name, missing))
                print(f"\n--- {name} ---")
                print(f"SKIP  {name} -- needs {', '.join(missing)}, not importable here")
                continue
            print(f"\n--- {name} ---")
            try:
                if inspect.signature(fn).parameters:
                    fn(tmp)
                else:
                    fn()
            except Exception as exc:  # noqa: BLE001 - a crashing test is a FAIL, not an abort
                check(f"{name} ran to completion", False, repr(exc))
        print("\n" + "=" * 72)
        for line in summary_lines(len(tests), len(PASS), len(FAIL), SKIPPED):
            print(line)
        if FAIL:
            print("\nFAILURES:")
            for f in FAIL:
                print("  " + f)
        if strict and SKIPPED:
            print("\n--no-skips: a skipped test is a test that did not run. Install the "
                  f"missing module(s) -- {', '.join(sorted({m for _, ms in SKIPPED for m in ms}))} "
                  "-- and run again.")
        print("=" * 72)
        return exit_code(len(FAIL), len(SKIPPED), strict=strict)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-skips", action="store_true",
                    help="fail if ANY test was skipped for a missing module")
    a = ap.parse_args(argv)
    print("=" * 72)
    print("HEALTH KB SELF-TEST  (stdlib only: no jsonschema, no pytest)"
          + ("  [--no-skips]" if a.no_skips else ""))
    print("=" * 72)
    return run_suite(discover(), strict=a.no_skips)


if __name__ == "__main__":
    raise SystemExit(main())
