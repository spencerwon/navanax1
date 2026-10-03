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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from health.cli import DISCLAIMER, VALIDATION_STATUS  # noqa: E402
from health.kb import KBLoadError, load_kb, parse_json, parse_params_js, records  # noqa: E402
from health.kb.check import (  # noqa: E402
    DEFERRED_RULES,
    DOCUMENTED_RULES,
    GRADES,
    RULES,
    Finding,
    PatternError,
    check_kb,
    ecma_to_python,
    matches,
    word_count,
)
from health.kb.report import GRADE_B_OR_BETTER, summary  # noqa: E402

DATA_DIR = ROOT / "src" / "health" / "kb" / "data"
DOC_03 = ROOT / "docs" / "health" / "03_VALIDATION_AND_TESTING.md"
PARAMS_DATA_JS = ROOT / "reference" / "metabolic-map-v1" / "engine" / "params.data.js"
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
SHIPPED_UNUSED = {"ev:shafiee-2005", "ev:crowe-1987", "ev:uttamsingh-1985", "ev:heer-2000",
                  "ev:rakova-2017", "ev:suckling-2012"}


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
    expected = sorted(("unused-evidence", f"evidence.json[{v}]") for v in SHIPPED_UNUSED)
    check("shipped KB: the warning set is pinned -- exactly the six unused-evidence records, "
          "no other code (a checker that stopped counting params.json citations gave 12)",
          warn == expected, f"got {warn}")
    info = sorted(f.message for f in findings if f.severity == "info")
    check("shipped KB: info is exactly the two E-assumption shares (5 of 20; 30 of 54)",
          len(info) == 2 and any("5 of 20 quantities (25.0%)" in m for m in info)
          and any("30 of 54 engine params (55.6%)" in m for m in info), str(info))
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
    check("boundary: a params.json row with value == hi and a degenerate range is legal",
          not new_codes(lambda kb: _params(kb, lambda p: p[FREE_PARAM].update(
              value=16, range=[16, 16]))))
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
    check("summary: params graded >= B equals an independent count over params.json",
          (share["count"], share["total"]) == (at_least_b, len(raw_params)) == (24, 54),
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
    check("cli status: the >= B share and the E-assumption count come from the files",
          "24/54 (44.4%)" in share_line and "E-assumption: 30" in share_line, share_line)

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
                     r"(?: \((.*)\))?", line)
    got = tuple(int(x) for x in m.groups()[:9]) if m else None
    want = (s["rows"], s["counted_pass"], s["counted_fail"], s["not_checked"],
            s["calibration"]["n"], s["structural"]["n"], kinds["known-divergence"],
            kinds["unverified"], len(s["calibrated_parameters"]))
    check("cli status: the expectations line equals summarize() over every scenario "
          f"({want})", got == want and r.returncode == 0, f"{line!r} vs {want}")
    check("cli status: the expectations line names the calibrated parameters",
          bool(m) and (m.group(10) or "") == ", ".join(s["calibrated_parameters"]), line)
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
    state, before the disclaimer; a disabled module is named, never merely absent."""
    from health.registry import DEFAULT_PATH, format_modules, load_modules

    mods = load_modules()
    ids = [m["id"] for m in mods]
    check("registry: the shipped registry names the Phase 0 modules",
          {"reference-v1", "engine-v1", "kb-v1", "cli", "errors", "process"} <= set(ids), str(ids))
    check("registry: every shipped module carries a flag, an owner and a removal recipe",
          all(isinstance(m.get("enabled"), bool) and m.get("owner") and m.get("removal")
              for m in mods),
          str([(m["id"], m.get("enabled"), m.get("owner"), bool(m.get("removal"))) for m in mods]))
    check("registry: the stdlib reader gives the same modules as the default reader",
          load_modules(reader="stdlib") == mods)
    r = _run_cli("status", "--fast")
    lines = r.stdout.splitlines()
    mod_line = next((i for i, line in enumerate(lines) if line.startswith("modules (")), -1)
    disc = next((i for i, line in enumerate(lines) if line == DISCLAIMER), -1)
    check("cli status: a modules line exists and precedes the disclaimer",
          0 <= mod_line < disc, r.stdout)
    check("cli status: every registered module is on the modules line with its state",
          mod_line >= 0 and all(f"{i} on" in lines[mod_line] for i in ids),
          lines[mod_line] if mod_line >= 0 else "")
    check("cli status: the modules line is read from the registry file, not typed",
          mod_line >= 0 and lines[mod_line].startswith(f"modules ({len(mods)}):"),
          lines[mod_line] if mod_line >= 0 else "")
    text = DEFAULT_PATH.read_text(encoding="utf-8").replace(
        "  - id: cli\n    enabled: true", "  - id: cli\n    enabled: false", 1)
    alt = tmp / "modules.yaml"
    alt.write_text(text, encoding="utf-8")
    for reader in ("auto", "stdlib"):
        out = format_modules(load_modules(alt, reader=reader))
        check(f"registry ({reader} reader): a disabled module is named on its own DISABLED line",
              "modules DISABLED: cli" in out and "cli on" not in out, out)


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
