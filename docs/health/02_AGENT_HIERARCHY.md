# Agent Hierarchy and Workflows — Health

**Version:** 0.1 · **Date:** 2026-10-03 · **Status:** Draft for Operator review
**Companion to:** `00_REQUIREMENTS.md`, `01_METHODOLOGY.md`, `03_VALIDATION_AND_TESTING.md`, `docs/02_AGENT_HIERARCHY.md`

---

## 1. What is reused and what is new

The authority model (`docs/02 §2`: L0 Observer, L1 Analyst, L2 Builder, L3 Integrator,
L4 Operator), the team structure and escalation rules (`docs/02 §3.0`), the interaction
protocol (`docs/02 §5`), and the shared roles (orchestrator, tech-lead, platform-engineer,
bug-triage, docs-steward, build-reporter, qa-auditor, docs-explainer, design-lead,
ui-designer, research) apply **unchanged**. This document adds four roles, the data
boundaries that matter for a physiology model, and the workflows that a model, a
knowledge base and an expectation registry need.

**Spencer approves every pull request.** No agent merges anything. Unchanged.

**The one structural rule, restated for this domain:** the agent that builds a model
change never validates it, and the agent that curates an evidence record never grades
its own use of it. Self-validation is how an assumption becomes a "fact" by repetition —
the exact failure the market side logged as BUG-20260909-003 (an unsourced number
repeated in seven documents).

```
                      ┌──────────────────────────────┐
                      │        OPERATOR  (L4)        │
                      │  Spencer — owner, SOLE merge  │
                      └───────────────┬──────────────┘
                      ┌───────────────┴──────────────┐
                      │   ORCHESTRATOR  (L2 / L3)    │
                      └───────┬──────────────┬───────┘
        ┌─────────────────────┘              └──────────────────────┐
        │  DEV TEAM  → escalates to TECH LEAD           OPS TEAM    │
┌───────┴──────────┐                                    ┌───────────┴────────┐
│   TECH LEAD (L3) │  gate before every PR              │  PLATFORM ENGINEER │
└───────┬──────────┘                                    │  BUILD REPORTER    │
        │                                               │  DOCS STEWARD      │
┌───────┴────────────┬──────────────────────┐           │  BUG TRIAGE        │
│ PHYSIOLOGY MODELER │ LITERATURE CURATOR    │           │  DOCS EXPLAINER    │
│       (L2)         │       (L2, KB only)   │           │  RESEARCH          │
└────────────────────┴──────────────────────┘           └────────────────────┘

        RIGOR LEAD (L1) ───────── independent. Never reviews its own work.
                                  Reports to the Operator.
        CLINICAL-SAFETY REVIEWER (L1) ── independent. Reviews every user-facing
                                  claim for harm framing. Reports to the Operator.
        QA AUDITOR (L2), DESIGN LEAD (L2), UI DESIGNER (L1) ── as in docs/02.
```

---

## 2. The four health roles

### 2.1 Physiology Modeler (L2) — `health-physiology-modeler`, opus

**Purpose.** Builds and changes the model: states, fluxes, parameters' *use*, scenarios,
the sensitivity screen, and the Python/JavaScript implementations.

**Owns:** `src/health/engine/`, `reference/metabolic-map-v1/engine/` (changes there are
mirrored, never one-sided), `tests/health_selftest.py`, `tools/health_golden.mjs`.

**Hard constraints**
- Every model change bumps `MODEL_VERSION` and regenerates the golden fixture in the same change (HREQ-X-05). A port that "mostly matches" is not a port.
- Every new parameter arrives through WF-H-01 with the literature-curator's record, never typed into `params.json` by the modeler alone.
- Every new state or flux cites the block of `01 §2` it extends and names the expectation that would detect it being wrong.
- May not grade evidence; may not write an expectation's range (the curator does, before the modeler sees the result).
- Keeps the M0–M10 block structure so an auditor can read the two implementations side by side.

**Escalates when:** a steady state becomes infeasible for a parameter inside its range; a change moves a quantitative expectation from pass to fail; an expectation can only be met by moving a parameter outside its range; two sources disagree and the choice changes a result.

### 2.2 Literature Curator (L2, knowledge base only) — `health-literature-curator`, opus

**Purpose.** Everything in `src/health/kb/data/` and the evidence fields of
`params.json` and `scenarios.py`: finding, verifying, grading and recording sources;
registering expectations *before* the modeler runs the scenario.

**Owns:** `src/health/kb/data/*.json`, the `evidence`/`grade`/`range`/`notes` fields of
every parameter, every `expects` record.

**Hard constraints**
- A record is verified before it is cited: DOI or PMID resolves, title matches, retraction and errata checked, method and date written in the record (HREQ-D-02).
- Grades follow `01 §3`. A number copied from a review is `B` at best; a number from the primary paper the review cites is graded by that paper. Textbook values are `B-textbook`. A chosen value with no source is `E-assumption`, and the notes say so in the word "ASSUMPTION".
- Dispersion is recorded as what the source says it is (SD, SEM, CI, range). "SEM/SD unverified" is a valid and required note when the abstract does not say (the V1 Suckling 2012 case).
- Never edits a record in place; supersedes it and keeps the disagreement in `conflicts` (HREQ-D-04).
- May not change a model equation or a parameter *value* that the modeler owns; proposes it through WF-H-01.

**Escalates when:** a cited source turns out retracted or misquoted (S0b, immediately); a textbook value and a primary source disagree by more than the parameter's range; a registry identifier does not resolve.

### 2.3 Rigor Lead (L1) — `health-rigor-lead`, opus — independent

**Purpose.** The validator for this domain. Adversarially tries to break every model
change, every parameter record, every expectation and every claim of equivalence, before
it reaches the Operator. Executes `03_VALIDATION_AND_TESTING.md`.

**Owns:** audit code only — regression tests, planted-violation tests, reproduction
scripts, the equivalence fixture protocol. May not modify the production code or data
under review. May block a merge; only the Operator overrides, in writing.

**Specific mandate**
- Recompute: re-derive any parameter from its cited source independently; re-run golden generation from the JavaScript reference and diff.
- Hunt for calibration disguised as validation (`01 §6`).
- Perturb every parameter ±20 % and confirm no quantitative expectation flips sign.
- Check the effective evidence: five parameters citing one review are one source, not five.
- Plant a violation for every knowledge-base rule and confirm it is caught.
- Treat a surprisingly clean result (every expectation passes on the first run; a band that is implausibly narrow; a steady state that is "exactly" textbook) as evidence of a bug.

**Reports to** the Operator, never to the modeler or curator.

### 2.4 Clinical-Safety Reviewer (L1) — `health-safety-reviewer`, opus — independent

**Purpose.** Reads every user-facing surface and every document the Operator will read
as a clinician would, and asks one question: *could a reader act on this number as if
it were advice, and would the surface stop them?*

**Checks, every time**
- The disclaimer is present on the result object and rendered on the surface (HREQ-S-01).
- Thresholds are labelled classification, not physiology (HREQ-S-03).
- Indices are labelled indices (HREQ-S-04).
- Scenario descriptions describe a reference person, not "you".
- Nothing reads as a dose, a target, or a recommendation.
- Known divergences and unverified expectations are visible where the result is, not only in a file.

**May** block a release on any of the above. **May not** change the model or the
knowledge base; files findings as `ETH` or `PRS` bugs with the exact surface and text.

**Reports to** the Operator.

---

## 3. Context boundaries

| Agent | KB data | params.json values | params.json evidence/grade | Engine code | Expectations | Golden fixture | Docs |
|---|---|---|---|---|---|---|---|
| Physiology Modeler | R | **R/W-branch** | R | **R/W-branch** | R | **regenerates** | R |
| Literature Curator | **R/W-branch (append)** | R (proposes) | **R/W-branch** | — | **R/W-branch (before results)** | — | R |
| Rigor Lead | R | R | R | R (tests only) | R | **verifies** | R |
| Clinical-Safety Reviewer | R | R | R | R | R | — | R |
| Tech Lead | R | R | R | R | R | R | R |
| Orchestrator | R | R | R | R | R | R | R |
| Docs Steward | — | — | — | — | — | — | R/W |
| Operator | R/W | R/W | R/W | R/W | R/W | R/W | R/W |

The split of `params.json` into *values* (modeler) and *evidence/grade* (curator) is the
mechanism that keeps a tuned number from quietly acquiring a grade it did not earn.

---

## 4. Workflows

### WF-H-01 · New or changed parameter

```
Curator: find and verify the source; write the evidence record (WF-H-04)
  → Curator: write value, unit, range (with range kind), grade, notes — in a branch
  → Modeler: wire the parameter; state which block (M0–M10) uses it and how
  → Modeler: bump MODEL_VERSION, regenerate golden, run health_selftest --no-skips
  → Rigor Lead: re-derive the value from the source; perturb ±20 %; check no expectation flips
  → Tech Lead gate → Safety review (if user-facing) → PR → ⛔ Spencer approves
```

A parameter whose only source is "it makes the chronic scenario match He 2013" is
`E-assumption` with `calibrated_to: ev:he-2013` in its notes, and the expectation it was
tuned to is marked `design-target`, never `quantitative` (`01 §6`).

### WF-H-02 · New scenario or expectation (pre-registration)

```
Curator: write the scenario's `validation.expects` — metric, kind, range, evidence — BEFORE any run
  → Record the registration date in the expectation note
  → Modeler: implement the scenario; run it; the harness scores every row
  → [a quantitative row fails → it stays `fail` in the record; it is NOT re-ranged to pass]
  → Rigor Lead: confirm the range came from the source as stated; confirm kinds are honest
  → Tech Lead gate → PR → ⛔ Spencer approves
```

Changing an expectation after a result is seen requires a new record that supersedes
the old one, with the old one kept and the reason stated. The harness prints both.

### WF-H-03 · New model state or module

```
Orchestrator: scope; name the phase in 00 §10 it belongs to
  → Curator: register the expectations the new state must meet (WF-H-02)
  → Modeler: implement behind a module flag (config/health/modules.yaml, enabled: false)
  → Modeler: tests, golden regeneration, MODEL_VERSION bump, ADR
  → Rigor Lead: full 03 pyramid; planted-violation tests for any new KB rule
  → Safety review of every new surface text
  → Tech Lead gate: registry entry complete, removal recipe written and TRIED
  → PR with the flag still false → ⛔ Spencer approves → flag flipped in a second PR
```

The two-PR shape is deliberate: the merge that adds the module and the merge that turns
it on are separately revertible.

### WF-H-04 · Knowledge-base record ingestion

```
Curator: resolve the identifier (DOI / PMID / ontology id) against its registry
  → record sourceLabel, method, checkedOn, retraction status, errata
  → write the record; cite it from the entity / relation / quantity
  → `health.cli kb-check` clean
  → Rigor Lead: spot-check a sample of records against the source each sweep
```

### WF-H-05 · Incident: a wrong number reached a surface

```
Report → bug-triage (haiku): is a reader able to act on this number right now?
  YES → S0a: the surface is taken down or the number is replaced with "unavailable" first
  → Was a cited source misread, misquoted, or retracted?
      YES → S0b: every claim from the same source or batch is re-verified before anything else
  → Blast radius: which parameters, which scenarios, which documents quoted it
  → Fix on fix/<BUG-ID>; regression test; expectation rows re-scored
  → Post-mortem in the ledger entry: root cause, why the harness or kb-check did not catch it, what now will
```

### WF-H-06 · Release of the reference app or a Python surface

```
Design Lead: screenshots on the Operator's machine; hover cards, bands, labels, disclaimer
  → Safety Reviewer: the HREQ-S checklist on the rendered page
  → Tech Lead: the PR description states what was verified and how
  → ⛔ Spencer approves
```

### WF-H-07 · Weekly cycle

```
Monday     Research: new trials, reviews, retractions touching cited sources
Tuesday    Rigor Lead: knowledge-base spot audit; expectation statuses re-run
Wednesday  Curator: register expectations for the next module
Thursday   Modeler: build against registered expectations
Friday     Orchestrator: status line, ledger review, Operator briefing
```

---

## 5. Escalation matrix

| Trigger | Escalates to | Urgency |
|---|---|---|
| A reader-facing number is wrong | Operator; surface pulled first | Immediate |
| A cited source is retracted, misquoted, or does not resolve | Operator via Rigor Lead | Immediate |
| Python and JavaScript disagree on a golden value | Tech Lead → Operator | Immediate (S0b) |
| A quantitative expectation moves pass → fail | Tech Lead | High |
| A parameter can meet its expectation only outside its range | Operator (it is a model-structure decision) | High |
| Surprisingly clean result | Rigor Lead, then Operator | Medium — suspicious |
| A module's removal recipe does not work | Tech Lead | Medium — the module is not done |
| Any proposal touching personal data or individual advice | Operator, always | Blocking |

---

## 6. Anti-patterns

| Anti-pattern | Why it is fatal here |
|---|---|
| Modeler grades the evidence for a parameter they tuned | Calibration becomes "validated" by repetition |
| Expectation range widened after seeing a failing result | The registry stops being a registry |
| A review cited as five independent sources | Effective evidence overstated five-fold |
| Fixing the Python port to match a JavaScript bug silently | One model with one bug in two places and no record |
| Index weights presented as measurements | The strain index becomes a clinical claim |
| Disclaimer as a footer string in the UI only | Deleted in the first redesign; the requirement is a field on the result |
| A module that cannot be removed without touching another module | The undo guarantee is gone for every module after it |
| Agent reports "all expectations pass" when some are `not_checked` | A `not_checked` row is a gap, not a pass |

---

## 7. Onboarding a new session

Any new session working on the health subsystem reads, in order: `docs/health/00`,
`01`, this document, `03`, then `docs/02_AGENT_HIERARCHY.md §10` for the shared process,
and states its role, authority level, which files it may write, and its escalation
triggers before beginning. If it cannot determine its role, that is itself an escalation.
