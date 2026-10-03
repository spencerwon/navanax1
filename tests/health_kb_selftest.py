"""Standard-library-only self-test for the health knowledge base (KB) tooling.

Runnable with a bare `python3 tests/health_kb_selftest.py` -- no pytest, no
jsonschema. It covers health.kb (loader), health.kb.check (the contract
checker), health.kb.report (the summary) and health.cli (kb-check, kb-summary,
status), with the same conventions as tests/selftest.py: module-level `test_*`
functions are discovered in definition order, `check()` records each assertion,
`@needs(...)` declares third-party modules (none are needed today), and
`--no-skips` refuses to exit 0 if anything was skipped.

The central claim is "a rule with no planted-violation test is not a rule":
test_every_contract_rule_fires_on_a_planted_violation mutates a copy of the
shipped KB to break exactly one rule at a time, asserts the checker reports
that rule's code (and nothing it did not expect), and finally asserts that the
plants covered every code in health.kb.check.RULES.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from health.cli import DISCLAIMER  # noqa: E402
from health.kb import load_kb, records  # noqa: E402
from health.kb.check import RULES, SEVERITIES, Finding, check_kb  # noqa: E402
from health.kb.report import GRADE_B_OR_BETTER, summary  # noqa: E402

DATA_DIR = ROOT / "src" / "health" / "kb" / "data"

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


def base_kb() -> dict[str, Any]:
    if not _BASE:
        _BASE.update(load_kb())
    return copy.deepcopy(_BASE)


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


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def _new_evidence(kb: dict[str, Any], vid: str) -> dict[str, Any]:
    v = copy.deepcopy(records(kb, "evidence")[1])
    v["id"] = vid
    records(kb, "evidence").append(v)
    return v


def _two_cycle(kb: dict[str, Any]) -> None:
    """A -> B -> A between two same-scale entities, so no scale rule fires too."""
    by_id = {e["id"]: e for e in records(kb, "entities")}
    child = next(e for e in records(kb, "entities")
                 if e.get("parent") and by_id[e["parent"]]["scale"] == e["scale"])
    by_id[child["parent"]]["parent"] = child["id"]


def _drop_key(path: list[str]) -> Callable[[dict[str, Any]], None]:
    def mutate(kb: dict[str, Any]) -> None:
        node = kb["schema"]
        for part in path[:-1]:
            node = node[part]
        del node[path[-1]]
    return mutate


# (expected code, what was planted, mutation (mutates the KB in place; any return value is
# ignored), other codes the plant legitimately cascades into)
Plant = tuple[str, str, Callable[[dict[str, Any]], Any], frozenset[str]]
NONE: frozenset[str] = frozenset()

PLANTS: list[Plant] = [
    # -- document / record structure ---------------------------------------------
    ("file-shape", "VERIFICATION_LOG records is an object, not an array",
     (lambda kb: kb["verification"].update(records={})),
     frozenset({"external-id-unverified"})),
    ("file-shape", "VERIFICATION_LOG document is an array, not an object",
     (lambda kb: kb.update(verification=[])), frozenset({"external-id-unverified"})),
    ("unknown-key", "entity carries an undeclared key",
     (lambda kb: ent(kb, BODY).update(colour="red")), NONE),
    ("unknown-key", "quantity carries an undeclared key",
     (lambda kb: rel(kb, NEPHRON)["quantity"].update(confidence=0.9)), NONE),
    ("unknown-key", "conflict carries an undeclared key",
     (lambda kb: rel(kb, GFR)["quantity"]["conflicts"][0].update(weight=1)), NONE),
    ("unknown-key", "evidence.verification carries an undeclared key",
     (lambda kb: ev(kb, "ev:guyton-hall-2021")["verification"].update(reviewer="x")), NONE),
    ("unknown-key", "externalIds names an undeclared registry",
     (lambda kb: ent(kb, BODY)["externalIds"].update(MESH="D000001")), NONE),
    ("unknown-key", "relations.json carries an undeclared top-level key",
     (lambda kb: kb["relations"].update(comment="x")), NONE),
    ("missing-field", "relation without type",
     (lambda kb: rel(kb, NEPHRON).pop("type")), NONE),
    ("missing-field", "evidence without notes",
     (lambda kb: ev(kb, "ev:guyton-hall-2021").pop("notes")), NONE),
    ("missing-field", "quantity without unit",
     (lambda kb: rel(kb, NEPHRON)["quantity"].pop("unit")), NONE),
    ("missing-field", "evidence.verification without retraction",
     (lambda kb: ev(kb, "ev:guyton-hall-2021")["verification"].pop("retraction")), NONE),
    ("missing-field", "VERIFICATION_LOG record without resolved",
     (lambda kb: log_record(kb, BODY).pop("resolved")),
     frozenset({"external-id-unverified"})),
    ("wrong-type", "entity name is a number",
     (lambda kb: ent(kb, BODY).update(name=5)), NONE),
    ("wrong-type", "a relations.json record is null",
     (lambda kb: records(kb, "relations").append(None)), NONE),
    ("wrong-type", "relation conflicts is a string",
     (lambda kb: rel(kb, "rel:expr:cyp11b2-zg").update(conflicts="x")), NONE),
    ("wrong-type", "evidence verification.titleMatch is a string",
     (lambda kb: ev(kb, "ev:robertson-athar-1976")["verification"].update(titleMatch="yes")),
     NONE),
    ("wrong-type", "entities.json kbVersion is a number",
     (lambda kb: kb["entities"].update(kbVersion=1)), NONE),
    ("wrong-type", "entity synonyms is a string",
     (lambda kb: ent(kb, BODY).update(synonyms="organism")), NONE),
    ("wrong-type", "entity scale is a string",
     (lambda kb: ent(kb, LEAF).update(scale="7")), NONE),
    ("wrong-type", "quantity value is a string",
     (lambda kb: rel(kb, NEPHRON)["quantity"].update(value="950000")), NONE),
    ("empty", "entity name is empty",
     (lambda kb: ent(kb, BODY).update(name="")), NONE),
    ("empty", "entity cites no evidence",
     (lambda kb: ent(kb, BODY).update(evidence=[])), NONE),
    ("empty", "quantity unit is empty",
     (lambda kb: rel(kb, NEPHRON)["quantity"].update(unit="")), NONE),
    ("empty", "UniProt array is empty",
     (lambda kb: ent(kb, BODY)["externalIds"].update(UniProt=[])), NONE),
    ("empty", "externalIds object is empty",
     (lambda kb: ent(kb, BODY).update(externalIds={})),
     frozenset({"orphan-verification"})),
    ("not-unique", "entity synonym repeated",
     (lambda kb: ent(kb, BODY)["synonyms"].append(ent(kb, BODY)["synonyms"][0])), NONE),
    ("not-unique", "entity evidence id repeated",
     (lambda kb: ent(kb, BODY)["evidence"].append(ent(kb, BODY)["evidence"][0])), NONE),
    ("bad-enum", "entity type outside the enum",
     (lambda kb: ent(kb, BODY).update(type="widget")), NONE),
    ("bad-enum", "relation type outside the enum",
     (lambda kb: rel(kb, NEPHRON).update(type="inhibits")), NONE),
    ("bad-enum", "evidence kind outside the enum",
     (lambda kb: ev(kb, "ev:guyton-hall-2021").update(kind="website")), NONE),
    ("bad-enum", "evidence sourceType outside the enum",
     (lambda kb: ev(kb, "ev:guyton-hall-2021").update(sourceType="blog")), NONE),
    ("bad-enum", "evidence defaultGrade outside the enum",
     (lambda kb: ev(kb, "ev:guyton-hall-2021").update(defaultGrade="A+")), NONE),
    ("bad-enum", "quantity grade outside the enum",
     (lambda kb: rel(kb, NEPHRON)["quantity"].update(grade="F")), NONE),
    ("out-of-bounds", "entity scale 8",
     (lambda kb: ent(kb, LEAF).update(scale=8)), NONE),
    ("out-of-bounds", "evidence year 1700",
     (lambda kb: ev(kb, "ev:guyton-hall-2021").update(year=1700)), NONE),
    ("range-shape", "quantity range has one number",
     (lambda kb: rel(kb, NEPHRON)["quantity"].update(range=[1])), NONE),
    ("range-shape", "conflict range has three numbers",
     (lambda kb: next(c for c in rel(kb, GFR)["quantity"]["conflicts"] if "range" in c)
          .update(range=[1, 2, 3])), NONE),
    ("range-shape", "quantity range is a string",
     (lambda kb: rel(kb, NEPHRON)["quantity"].update(range="wide")), NONE),
    # -- patterns ------------------------------------------------------------------------
    ("id-pattern", "relation from is not a well-formed id",
     (lambda kb: rel(kb, NEPHRON).update({"from": "Tissue:Nephron"})), NONE),
    ("id-pattern", "entity parent is not a well-formed id",
     (lambda kb: ent(kb, HEART).update(parent="Cardio System")), NONE),
    ("evidence-id-pattern", "entity cites a malformed evidence id",
     (lambda kb: ent(kb, BODY)["evidence"].append("ev:Bad_Id")), NONE),
    ("evidence-id-pattern", "an evidence record id is malformed",
     (lambda kb: _new_evidence(kb, "EV:planted")), NONE),
    ("relation-id-pattern", "relation id has a space and capitals",
     (lambda kb: rel(kb, NEPHRON).update(id="rel:Bad Id")), NONE),
    ("external-id-pattern", "CL id with too few digits",
     (lambda kb: ent(kb, BODY)["externalIds"].update(CL="CL:12")), NONE),
    ("external-id-pattern", "UniProt array item malformed",
     (lambda kb: ent(kb, BODY)["externalIds"].update(UniProt=["bad1"])), NONE),
    ("doi-pattern", "doi with a 2-digit registrant",
     (lambda kb: ev(kb, "ev:robertson-athar-1976").update(doi="10.12/x")), NONE),
    ("pmid-pattern", "pmid with a prefix",
     (lambda kb: ev(kb, "ev:robertson-athar-1976").update(pmid="PMID:1262438")), NONE),
    ("url-https", "plain http url",
     (lambda kb: ev(kb, "ev:robertson-athar-1976").update(url="http://doi.org/x")), NONE),
    ("date-pattern", "accessed with slashes",
     (lambda kb: ev(kb, "ev:robertson-athar-1976").update(accessed="2026/10/01")), NONE),
    ("date-pattern", "verification.checkedOn as words",
     (lambda kb: ev(kb, "ev:robertson-athar-1976")["verification"]
          .update(checkedOn="Oct 2026")), NONE),
    ("date-pattern", "accessed with a trailing newline (Python `$` would accept it)",
     (lambda kb: ev(kb, "ev:robertson-athar-1976").update(accessed="2026-10-01\n")), NONE),
    ("date-pattern", "VERIFICATION_LOG checkedOn as a word",
     (lambda kb: log_record(kb, BODY).update(checkedOn="yesterday")), NONE),
    ("doi-required", "kind doi without a doi",
     (lambda kb: ev(kb, "ev:robertson-athar-1976").pop("doi")), NONE),
    ("pmid-required", "kind pmid without a pmid",
     (lambda kb: (ev(kb, "ev:robertson-athar-1976").update(kind="pmid"),
                      ev(kb, "ev:robertson-athar-1976").pop("pmid"))), NONE),
    # -- cross-file contract -------------------------------------------------------------
    ("duplicate-id", "an evidence record appears twice",
     (lambda kb: records(kb, "evidence").append(copy.deepcopy(ev(kb, "ev:thompson-1986")))),
     NONE),
    ("duplicate-id", "an entity appears twice",
     (lambda kb: records(kb, "entities").append(copy.deepcopy(ent(kb, LEAF)))), NONE),
    ("duplicate-id", "a relation (one without a quantity) appears twice",
     (lambda kb: records(kb, "relations").append(copy.deepcopy(
         next(r for r in records(kb, "relations") if "quantity" not in r)))), NONE),
    ("dangling-evidence", "entity cites an evidence id with no record",
     (lambda kb: ent(kb, BODY)["evidence"].append("ev:does-not-exist")), NONE),
    ("dangling-evidence", "quantity cites an evidence id with no record",
     (lambda kb: rel(kb, NEPHRON)["quantity"]["evidence"].append("ev:does-not-exist")), NONE),
    ("dangling-evidence", "conflict cites an evidence id with no record",
     (lambda kb: rel(kb, GFR)["quantity"]["conflicts"][0]["evidence"]
          .append("ev:does-not-exist")), NONE),
    ("dangling-endpoint", "relation to names no entity",
     (lambda kb: rel(kb, NEPHRON).update(to="organ:nonexistent")), NONE),
    ("dangling-parent", "entity parent names no entity",
     (lambda kb: ent(kb, HEART).update(parent="system:nonexistent")), NONE),
    ("parent-scale", "entity placed above its parent's scale",
     (lambda kb: ent(kb, HEART).update(scale=0)), NONE),
    ("parent-cycle", "entity is its own parent",
     (lambda kb: ent(kb, LEAF).update(parent=LEAF)), NONE),
    ("parent-cycle", "two same-scale entities are each other's parent", _two_cycle, NONE),
    ("engine-params-unavailable", "no params.json loaded",
     (lambda kb: kb.update(params=None)), frozenset({"unused-evidence"})),
    ("engine-param-unknown", "engineParam names no params.json row",
     (lambda kb: rel(kb, ADH_SLOPE)["quantity"].update(engineParam="no_such_param")), NONE),
    ("engine-param-mismatch", "mirrored value differs from params.json",
     (lambda kb: rel(kb, ADH_SLOPE)["quantity"].update(value=0.36)), NONE),
    ("external-id-unverified", "log record says resolved: false",
     (lambda kb: log_record(kb, BODY).update(resolved=False)), NONE),
    ("external-id-unverified", "externalIds value has no log record",
     (lambda kb: ent(kb, BODY)["externalIds"].update(CL="CL:0000000")), NONE),
    ("summary-too-long", "61-word summary",
     (lambda kb: ent(kb, BODY).update(summary=_words(61))), NONE),
    ("quote-too-long", "26-word quote",
     (lambda kb: ev(kb, "ev:robertson-athar-1976").update(quote=_words(26))), NONE),
    ("schema-drift", "schema grade enum gains a value",
     (lambda kb: kb["schema"]["definitions"]["grade"]["enum"].append("F-anecdote")), NONE),
    ("schema-drift", "schema drops an Entity property",
     _drop_key(["definitions", "Entity", "properties", "system"]), NONE),
    ("schema-drift", "schema description edited (unmirrored part -> warn)",
     (lambda kb: kb["schema"].update(description="edited")), NONE),
    # -- warnings and info --------------------------------------------------------------------
    ("unused-evidence", "an evidence record nothing cites",
     (lambda kb: _new_evidence(kb, "ev:planted-unused")), NONE),
    ("conflict-unsourced-note", "empty-evidence conflict whose note does not say unsourced",
     (lambda kb: next(c for c in rel(kb, UNSOURCED)["quantity"]["conflicts"]
                          if not c["evidence"]).update(note="Different cohort.")), NONE),
    ("orphan-verification", "log record for an entity that does not exist",
     (lambda kb: records(kb, "verification").append(
         dict(log_record(kb, BODY), entity="organ:nonexistent"))), NONE),
    ("orphan-verification", "log record for an id the entity no longer carries",
     (lambda kb: records(kb, "verification").append(
         dict(log_record(kb, BODY), id="UBERON:9999999"))), NONE),
    ("range-order", "quantity range reversed",
     (lambda kb: rel(kb, NEPHRON)["quantity"]["range"].reverse()), NONE),
    ("range-order", "conflict range reversed",
     (lambda kb: next(c for c in rel(kb, GFR)["quantity"]["conflicts"] if "range" in c)
          ["range"].reverse()), NONE),
    ("value-outside-range", "quantity value above its range",
     (lambda kb: rel(kb, NEPHRON)["quantity"].update(value=1e9)), NONE),
    ("meta-mismatch", "relations.json kbVersion differs",
     (lambda kb: kb["relations"].update(kbVersion="1.0.1")), NONE),
    ("param-evidence-unresolved", "params.json row cites an unknown evidence id",
     (lambda kb: kb["params"]["V_ecf_0"]["evidence"].append("ev:nope")), NONE),
    ("e-assumption-share", "one more quantity graded E-assumption changes the reported share",
     (lambda kb: rel(kb, NEPHRON)["quantity"].update(grade="E-assumption")), NONE),
]


def _new_findings(kb: dict[str, Any], baseline: set[Finding]) -> list[Finding]:
    return [f for f in check_kb(kb) if f not in baseline]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_shipped_kb_passes_every_contract_rule() -> None:
    kb = base_kb()
    findings = check_kb(kb)
    by_sev = Counter(f.severity for f in findings)
    errors = [f for f in findings if f.severity == "error"]
    print(f"      shipped KB: {by_sev.get('error', 0)} error, {by_sev.get('warn', 0)} warn, "
          f"{by_sev.get('info', 0)} info  (params from {kb['paths']['params']})")
    for f in findings:
        if f.severity != "error":
            print(f"      {f}")
    check("shipped KB: zero error findings", not errors, "\n        ".join(map(str, errors)))
    check("shipped KB: params.json was found, so the engine mirrors were really compared",
          kb["params"] is not None
          and "engine-params-unavailable" not in {f.code for f in findings})
    check("shipped KB: the E-assumption share is reported for KB quantities and engine params",
          {f.where for f in findings if f.code == "e-assumption-share"}
          == {"relations.json quantities", "params.json"},
          str([f for f in findings if f.code == "e-assumption-share"]))
    shipped = {name: (DATA_DIR / name).stat().st_size for name in
               ("entities.json", "relations.json", "evidence.json", "VERIFICATION_LOG.json",
                "schema.json")}
    check("shipped KB: all five data files are packaged", all(shipped.values()), str(shipped))
    s = summary(kb)
    check("shipped KB: record counts are the curated 107 / 203 / 57 / 154",
          (s["counts"]["entities"], s["counts"]["relations"], s["counts"]["evidence"],
           s["counts"]["verificationRecords"]) == (107, 203, 57, 154), str(s["counts"]))


def test_every_contract_rule_fires_on_a_planted_violation() -> None:
    baseline_kb = base_kb()
    baseline = set(check_kb(baseline_kb))
    covered: set[str] = set()
    for code, what, mutate, also in PLANTS:
        kb = copy.deepcopy(baseline_kb)
        mutate(kb)
        try:
            new = _new_findings(kb, baseline)
        except Exception as exc:  # noqa: BLE001 - a crash on bad data is the failure reported
            check(f"plant {code}: {what} -- checker did not raise", False, repr(exc))
            continue
        codes = {f.code for f in new}
        fired = [f for f in new if f.code == code]
        check(f"plant {code}: {what}", bool(fired) and codes <= {code} | also,
              f"new findings: {[str(f) for f in new]}")
        expected_sev = RULES[code][0]
        if code == "schema-drift" and "unmirrored" in what:
            expected_sev = "warn"
        check(f"plant {code}: severity is {expected_sev}",
              bool(fired) and all(f.severity == expected_sev for f in fired),
              str([f.severity for f in fired]))
        unknown = (({f.code for f in new} - set(RULES))
                   | ({f.severity for f in new} - set(SEVERITIES)))
        check(f"plant {code}: only registered codes and severities are emitted", not unknown,
              str(unknown))
        if fired:
            covered.add(code)
    missing = sorted(set(RULES) - covered)
    check(f"every one of the {len(RULES)} rule codes has a planted violation that fires",
          not missing, f"rules with no firing plant: {missing}")
    planted_codes = {p[0] for p in PLANTS}
    check("no plant names a code the checker does not register",
          planted_codes <= set(RULES), str(sorted(planted_codes - set(RULES))))
    share = [f for f in _new_findings(_plant_kb("e-assumption-share"), baseline)
             if f.code == "e-assumption-share"]
    check("plant e-assumption-share: the message carries the new count and percentage",
          bool(share) and "6 of 20 quantities (30.0%)" in share[0].message,
          str([str(f) for f in share]))


def _plant_kb(code: str) -> dict[str, Any]:
    """A fresh copy of the shipped KB with the first plant for `code` applied."""
    kb = base_kb()
    next(p for p in PLANTS if p[0] == code)[2](kb)
    return kb


def test_checker_never_raises_on_garbage() -> None:
    """Every field of one record of each kind, replaced by each wrong JSON type."""
    kb = base_kb()
    garbage: list[Any] = [None, True, "", [None], {"x": 1}, -1.5]
    targets: list[tuple[str, dict[str, Any]]] = [
        ("entity", ent(kb, BODY)), ("entity", ent(kb, HEART)),
        ("relation", rel(kb, ADH_SLOPE)), ("quantity", rel(kb, ADH_SLOPE)["quantity"]),
        ("conflict", rel(kb, GFR)["quantity"]["conflicts"][2]),
        ("evidence", ev(kb, "ev:robertson-athar-1976")),
        ("verification", ev(kb, "ev:robertson-athar-1976")["verification"]),
        ("log", log_record(kb, BODY)), ("params-row", kb["params"]["adh_slope"]),
        ("entities.json", kb["entities"]), ("evidence.json", kb["evidence"]),
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
    for key in ("entities", "relations", "evidence", "verification", "schema", "params"):
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
    check(f"checker survives {runs} garbage substitutions without raising", not crashes,
          "\n        ".join(crashes[:10]))


def test_engine_param_mirrors_are_verified_against_params_json() -> None:
    kb = base_kb()
    baseline = set(check_kb(kb))
    mirrors = [(r["id"], r["quantity"]["engineParam"]) for r in records(kb, "relations")
               if isinstance(r.get("quantity"), dict) and "engineParam" in r["quantity"]]
    check("engine mirrors: the shipped KB has the 10 mirrored quantities summary() counts",
          len(mirrors) == 10 == summary(kb)["counts"]["engineMirrors"], str(mirrors))
    check("engine mirrors: none mismatches on the shipped data",
          not [f for f in baseline if f.code.startswith("engine-param")])

    kb2 = base_kb()
    rel(kb2, ADH_SLOPE)["quantity"]["value"] = 0.36
    hits = [f for f in _new_findings(kb2, baseline) if f.code == "engine-param-mismatch"]
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
            kb3 = base_kb()
            q = rel(kb3, rid)["quantity"]
            q[field] = bump(q[field])
            found = [f for f in _new_findings(kb3, baseline)
                     if f.code == "engine-param-mismatch" and repr(param) in f.message
                     and field in f.message]
            if not found:
                missed.append(f"{rid}.{field} ({param})")
    check("engine mirrors: perturbing value, range, unit or grade of EVERY mirror is caught "
          f"({len(mirrors) * 4} perturbations)", not missed, str(missed))

    kb4 = base_kb()
    kb4["params"]["thirst_threshold"]["unit"] = "mmol/L"
    hits = [f for f in _new_findings(kb4, baseline) if f.code == "engine-param-mismatch"]
    check("engine mirrors: a change on the params.json side is caught too, naming the param",
          len(hits) == 1 and "'thirst_threshold'" in hits[0].message,
          str([str(f) for f in hits]))
    kb5 = base_kb()
    del kb5["params"]["adh_thalf_h"]
    hits = [f for f in _new_findings(kb5, baseline) if f.code == "engine-param-unknown"]
    check("engine mirrors: a params.json row that disappears is reported by name",
          len(hits) == 1 and "'adh_thalf_h'" in hits[0].message, str([str(f) for f in hits]))


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
          (share["count"], share["total"]) == (at_least_b, len(raw_params)),
          f"{share} vs {at_least_b}/{len(raw_params)}")
    check("summary: >= B means exactly A-meta, A-primary, B-textbook",
          GRADE_B_OR_BETTER == ("A-meta", "A-primary", "B-textbook"), str(GRADE_B_OR_BETTER))
    raw_relations = json.loads((DATA_DIR / "relations.json").read_text(encoding="utf-8"))
    check("summary: relationsByType sums to the relation count",
          sum(s["relationsByType"].values()) == len(raw_relations["relations"]) == 203)
    check("summary: every grade table lists all six grades in rank order",
          all(list(s[k])[:6] == ["A-meta", "A-primary", "B-textbook", "C-model", "D-animal",
                                 "E-assumption"]
              for k in ("evidenceByDefaultGrade", "quantitiesByGrade", "paramsByGrade")))
    check("summary: JSON-serialisable", bool(json.dumps(s)))


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-m", "health.cli", *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          check=False)


def test_cli_kb_check_exit_codes(tmp: Path) -> None:
    r = _run_cli("kb-check")
    check("cli kb-check: exit 0 on the shipped KB", r.returncode == 0,
          f"exit {r.returncode}\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    out = r.stdout
    check("cli kb-check: findings are grouped error, then warn, then info, then the summary",
          0 <= out.find("ERROR (") < out.find("WARN (") < out.find("INFO (")
          < out.find("entities by type"), out[:400])

    good = tmp / "kb-good"
    shutil.copytree(DATA_DIR, good)
    r = _run_cli("kb-check", "--root", str(good))
    check("cli kb-check --root: exit 0 on an unmodified copy", r.returncode == 0,
          f"exit {r.returncode}\n{r.stdout[-800:]}{r.stderr[-800:]}")

    broken = tmp / "kb-broken"
    shutil.copytree(DATA_DIR, broken)
    doc = json.loads((broken / "entities.json").read_text(encoding="utf-8"))
    doc["entities"][0]["evidence"].append("ev:does-not-exist")
    (broken / "entities.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    r = _run_cli("kb-check", "--root", str(broken))
    check("cli kb-check --root: exit 1 on a copy with a dangling evidence reference",
          r.returncode == 1 and "dangling-evidence" in r.stdout and "FAIL" in r.stdout,
          f"exit {r.returncode}\n{r.stdout[-800:]}{r.stderr[-800:]}")

    unparsable = tmp / "kb-unparsable"
    shutil.copytree(DATA_DIR, unparsable)
    (unparsable / "relations.json").write_text("{not json", encoding="utf-8")
    r = _run_cli("kb-check", "--root", str(unparsable))
    check("cli kb-check --root: exit 1 (not a traceback) on a file that is not JSON",
          r.returncode == 1 and "cannot load" in r.stderr and "Traceback" not in r.stderr,
          f"exit {r.returncode}\n{r.stderr[-800:]}")

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


def test_status_prints_the_disclaimer(tmp: Path) -> None:
    r = _run_cli("status")
    lines = r.stdout.splitlines()
    check("cli status: exit 0", r.returncode == 0, r.stderr[-800:])
    check("cli status: the disclaimer is a line of its own",
          DISCLAIMER == "Educational model — not medical advice." and DISCLAIMER in lines,
          r.stdout)
    order = [next((i for i, line in enumerate(lines) if line.startswith(prefix)), -1)
             for prefix in ("health 0.", "model ", "KB 1.0.0", "entities 107",
                            "engine params by grade", "engine params graded >= B", DISCLAIMER)]
    check("cli status: HREQ-P-07 order -- package version, model version, KB version and "
          "curator, counts, grade distribution, >= B share, disclaimer",
          -1 not in order and order == sorted(order), f"{order}\n{r.stdout}")
    share_line = lines[order[5]] if order[5] >= 0 else ""
    check("cli status: the >= B share and the E-assumption count come from the files",
          "24/54 (44.4%)" in share_line and "E-assumption: 30" in share_line, share_line)

    r = _run_cli("status", "--root", str(tmp / "no-such-kb"))
    check("cli status: an unloadable KB exits 1, says unavailable, and still prints the "
          "disclaimer on its own line",
          r.returncode == 1 and "KB unavailable" in r.stdout
          and DISCLAIMER in r.stdout.splitlines(), f"exit {r.returncode}\n{r.stdout}")


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
    r = _run_cli("status")
    lines = r.stdout.splitlines()
    mod_line = next((i for i, line in enumerate(lines) if line.startswith("modules (")), -1)
    disc = next((i for i, line in enumerate(lines) if line == DISCLAIMER), -1)
    check("cli status: a modules line exists and precedes the disclaimer",
          0 <= mod_line < disc, r.stdout)
    check("cli status: every registered module is on the modules line with its state",
          mod_line >= 0 and all(f"{i} on" in lines[mod_line] for i in ids), lines[mod_line] if mod_line >= 0 else "")
    check("cli status: the modules line is read from the registry file, not typed",
          mod_line >= 0 and lines[mod_line].startswith(f"modules ({len(mods)}):"),
          lines[mod_line] if mod_line >= 0 else "")
    # A disabled module is VISIBLE: write a registry with one flag off and format it.
    text = DEFAULT_PATH.read_text(encoding="utf-8").replace(
        "  - id: cli\n    enabled: true", "  - id: cli\n    enabled: false", 1)
    alt = tmp / "modules.yaml"
    alt.write_text(text, encoding="utf-8")
    out = format_modules(load_modules(alt))
    check("registry: a disabled module is named on its own DISABLED line",
          "modules DISABLED: cli" in out and "cli on" not in out, out)
    # A missing registry is said plainly, never shown as "no modules".
    r2 = _run_cli("status")
    check("cli status: still exits 0 and prints the disclaimer last with the registry present",
          r2.returncode == 0 and r2.stdout.splitlines()[-1] == DISCLAIMER, r2.stdout[-300:])


@needs("yaml")
def test_registry_scanner_agrees_with_pyyaml(tmp: Path) -> None:
    """The stdlib-only scanner in health.registry must give the same answer as PyYAML on
    the shipped registry for every field `status` uses -- otherwise the pre-install
    gate and the post-install gate would read two different registries."""
    import yaml

    from health.registry import DEFAULT_PATH, _scan

    text = DEFAULT_PATH.read_text(encoding="utf-8")
    scanned = _scan(text)
    parsed = (yaml.safe_load(text) or {}).get("modules") or []
    fields = ("id", "enabled", "owner", "phase", "adr", "depends_on")
    a = [{k: m.get(k) for k in fields} for m in scanned]
    b = [{k: m.get(k) for k in fields} for m in parsed]
    check("registry: the scanner and PyYAML agree on id, enabled, owner, phase, adr and "
          "depends_on for every module", a == b, f"\nscanner: {a}\npyyaml:  {b}")
    check("registry: the scanner reads the removal recipe block scalars too",
          all(isinstance(m.get("removal"), str) and len(m["removal"]) > 40 for m in scanned),
          str([(m["id"], len(m.get("removal") or "")) for m in scanned]))


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
