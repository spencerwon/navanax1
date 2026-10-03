# Bug Taxonomy, Error Hierarchy, and Logging — Health

**Version:** 0.1 · **Date:** 2026-10-03 · **Status:** Draft for Operator review
**Companion to:** `03_VALIDATION_AND_TESTING.md`, `docs/05_BUG_TAXONOMY.md`

---

## 1. The principle that sets severity here

`docs/05 §1` says it for market data: a wrong number that looks right is worse than a
crash, because nothing downstream will catch it. For a physiology model the principle
is the same and the stakes are different in kind: downstream of a plausible wrong number
is not a backtest, it is **a reader**. So the first question in triage is:

> **Could a reader act on this number, and would they have any way to know it was wrong?**

If yes, severity goes up. A crash in the solver is S3. A kidney strain index rendered
without the words "an index, not a clinical measure" is S0a on a surface (the value is
withheld, `§6`) and S1 in a document — nothing crashed, and somebody could read it as a
diagnosis.

---

## 2. Severity hierarchy

| Level | Name | Definition | First action | Target |
|---|---|---|---|---|
| **S0a** | **Wrong number on a surface** | A value a reader could act on is wrong, or is right but framed as advice, on a published or Operator-facing surface | **Pull the surface or replace the value with "unavailable" first**, then diagnose | Immediate |
| **S0b** | **Evidence or equivalence integrity failure** | A cited source is misquoted, misread or retracted; or the Python and JavaScript implementations disagree on a golden value | **Halt releases.** Re-verify every claim from the same source or batch; mark every result since the last passing golden run suspect | Immediate |
| **S1** | **Silent wrongness** | A number is wrong but plausible; a band is missing; calibration is counted as validation; a disclaimer or label is absent; an expectation is `not_checked` but reported as a pass | Determine blast radius: which parameters, scenarios, documents | Same day |
| **S2** | **Provenance loss** | A number without a resolvable source or grade; a record edited in place; a golden fixture regenerated without a version bump | Restore the record; supersede, never overwrite | Same day |
| **S3** | **Functional break** | Solver throws, CLI errors, test harness crashes, app fails to load | Normal debugging | Within days |
| **S4** | **Cosmetic** | Layout, labels, ordering, slow test | Backlog | When convenient |

### 2.1 Escalation rules

- An S3 that turns out to have been producing a wrong value before it crashed → **S1 or S0a**.
- An S4 "label" issue that turns out to be a missing disclaimer, a threshold shown as physiology, or an index shown as a measure → **S1**.
- An S2 missing source on a parameter that a quantitative expectation depends on → **S1**.

Never lower a severity to make the ledger look better. Corrections are logged with a reason.

---

## 3. Error class taxonomy

| Class | Code | Examples | Which gate should catch it |
|---|---|---|---|
| **Model** | `MDL` | Wrong flux sign, missing term, wrong block semantics, steady state not steady | Steady-state test, mass balance, expectation harness |
| **Parameter** | `PRM` | Wrong unit, wrong range kind, value outside its own range, `mc` flag wrong | `kb-check` engineParam mirror, parameter tests |
| **Evidence** | `EVD` | Misquote, wrong year, SEM/SD confusion, retracted source, review cited as primary | Verification record, curator audit, rigor-lead spot check |
| **Numerics** | `NUM` | Step too large for a time constant, breakpoint missed, quantile off-by-one, PRNG drift | Golden equivalence, stiff-case test |
| **Knowledge base** | `KBI` | Dangling reference, parent scale > child scale, unverified external id, word limit | `kb-check` |
| **Validation harness** | `VAL` | Expectation silently skipped, metric computed on the wrong window, calibration counted as validation | Row-count test, harness tests |
| **Presentation** | `PRS` | Band dropped, hole interpolated, index without its label, result without disclaimer | Design Lead screenshot, Safety review |
| **Ethics / framing** | `ETH` | Any text that reads as individual advice; "you" in a scenario; a dose | Safety review |
| **Configuration** | `CFG` | Threshold in code not config; module flag not read | Tech Lead gate |
| **Infrastructure** | `INF` | CI step missing, fixture not committed, Node unavailable for golden | CI |
| **Security** | `SEC` | Listener beyond localhost; credential; personal data | Secret scan, port check |

**`EVD`, `VAL` and `ETH` deserve standing paranoia.** They produce confident, well-formatted,
completely wrong results, and nothing in a passing test suite reveals them.

**Any `ETH` finding is minimum S1.**

---

## 4. Bug log

The health subsystem shares the repository ledger: `docs/logs/bugs.yaml` is the source
of truth, `docs/logs/BUGS.md` the narrative, `tools/buglog.py --check` the CI gate.
Health entries carry `area: health` and use the health classes above. The rules of
`docs/05 §4` apply unchanged: entries are appended, never reordered; `data_impact`
becomes **`reader_impact`** in meaning (which surfaces, which numbers, for how long)
and is never blank; `monitor_gap` names the gate that should have caught it;
`regression_test` is mandatory to close.

Legacy ids from the V1 artifact's own audit (`BUG-0049`…`BUG-0054`, audit items
`F-01`…`F-12`, decisions `D-2`, `D-3`) are referenced as **V1 audit items** and are not
ledger ids. They are listed, with their current status, in `docs/health/logs/V1_AUDIT_ITEMS.md`.

### 4.1 Entry format

As `docs/05 §4.1`, plus:

| Field | Notes |
|---|---|
| `area` | `health` |
| `reader_impact` | Which surface showed the number, over what window, and whether the disclaimer and labels were present. "None — never reached a surface" is a valid answer and must be stated |
| `evidence_impact` | Which evidence records or parameters are now suspect |
| `expectations_impact` | Which expectation rows changed status |

---

## 5. Triage flow

```
Something looks wrong
  → bug-triage (haiku)
  → Could a reader act on this number right now?
      YES → S0a. Pull the surface. Notify Spencer.
  → Is a cited source misquoted / retracted, or do the two implementations disagree?
      YES → S0b. Halt releases. Re-verify the batch. Notify Spencer.
  → Classify: severity + class
  → reader_impact, evidence_impact, expectations_impact
  → Route:
      S0a/S0b/S1 → Spencer + owning agent, immediately
      S2/S3 → owning agent, queued
      S4 → backlog
  → Fix on fix/<BUG-ID>; regression test; expectation rows re-scored
  → health-rigor-lead reviews anything S0a/S0b/S1 or touching MDL/PRM/EVD/VAL
  → health-safety-reviewer reviews anything PRS/ETH
  → Merge; update the ledger in the same change
  → Post-mortem for S0a/S0b/S1
```

### 5.1 Who fixes what

| Class | Owner |
|---|---|
| `MDL`, `NUM`, `VAL` | `health-physiology-modeler`; `health-rigor-lead` reviews |
| `PRM`, `EVD`, `KBI` | `health-literature-curator`; `health-rigor-lead` reviews |
| `PRS`, `ETH` | `platform-engineer` / `design-lead`; `health-safety-reviewer` reviews |
| `CFG`, `INF`, `SEC` | by location; `SEC` always gets review |

---

## 6. Runtime error hierarchy (code)

Implemented in `src/health/errors.py`, mirroring `src/navanax/errors.py` so the
halt rules execute without a human deciding anything.

```
HealthError                              (base — never raised directly)
│
├── SurfaceIntegrityError                → S0a. Handler WITHHOLDS the value ("unavailable").
│   ├── MissingDisclaimerError
│   └── UnlabelledIndexError
│
├── EvidenceIntegrityError               → S0b. Handler HALTS RELEASE.
│   ├── UnresolvedEvidenceError          (a cited record does not exist or does not resolve)
│   ├── MisquotedSourceError
│   └── ReferenceDivergenceError         (Python and JavaScript disagree beyond tolerance; defined in src/health/errors.py, raised nowhere yet — a divergence fails the golden tests, ADR-0002 errata)
│
├── SilentWrongnessError                 → S1. Alert, non-suppressible.
│   ├── MissingUncertaintyError          (a point without its band)
│   ├── CalibrationAsValidationError
│   ├── UnitMismatchError
│   └── ExpectationSkippedError          (a registered expectation produced no row)
│
├── ProvenanceError                      → S2.
│   ├── UngradedValueError
│   └── InPlaceEditError
│
├── NumericalError                       → S3, or S1 if a value was produced anyway.
│   ├── InfeasibleParametersError        (no steady state inside the ranges)
│   ├── StepControlError
│   └── NonFiniteTrajectoryError         (a trajectory holds a NaN or infinite value; HREQ-V-07)
│
├── OperationalError                     → S3.
│   └── PersistenceError
│
└── ConfigurationError                   → S3, or S1 if it silently changed a result.
```

### 6.1 Rules the hierarchy enforces

- **`SurfaceIntegrityError` is never caught and swallowed.** A surface that cannot render the disclaimer renders "unavailable". CI greps for an `except SurfaceIntegrityError` that does not re-raise.
- **`ReferenceDivergenceError` halts release, not computation.** The engine keeps working; nothing is published until the divergence is explained. Phase 0: the class is defined in `src/health/errors.py` and raised nowhere yet; a divergence fails the golden tests in `tests/health_selftest.py` (ADR-0002 errata).
- **`InfeasibleParametersError` is an error, not a warning.** The sampler counts the rejection; the deterministic path refuses.
- **`ExpectationSkippedError` exists so the harness cannot shrink.** The number of rows equals the number of registered expectations, or the harness is wrong.
- **Every raise carries context**: which parameter, which scenario, which evidence id, which model version.
