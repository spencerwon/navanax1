# Project Plan — Health

**Version:** 0.1 · **Date:** 2026-10-03 · **Status:** Draft for Operator review
**Accountable:** the orchestrator session (this one) holds responsibility for delivery; **Spencer** is the sole approver of every pull request, every decision marked *his*, and every publication.
**Companion to:** `00_REQUIREMENTS.md` §10, `02_AGENT_HIERARCHY.md`, `07_MODULE_REGISTRY.md`, `logs/TRACKER.md`

---

## 1. Mission, and what "better than any one system" means here

The mission is the Operator's paragraph in `00 §1.2`. The standard it sets is not "more
features than other anatomy or physiology tools"; it is **auditable**: every number
sourced and graded, every output banded, every expectation registered before the result,
every assumption loud, every decision written, every module removable. No comparable
system publishes its assumption count on the status line. That is the bar, and it is
measurable.

## 2. Workstreams and their owners

| Workstream | Owner agent | Independent reviewer | What it ships |
|---|---|---|---|
| **Engine** | health-physiology-modeler | health-rigor-lead | States, fluxes, scenarios, Monte Carlo, sensitivity, the Python/JS pair |
| **Knowledge** | health-literature-curator | health-rigor-lead | Entities, relations, evidence, verification, parameter records, expectations |
| **Viewer and lab** | platform-engineer with ui-designer and design-lead | health-safety-reviewer, design-lead on the Operator's machine | The 3D body, scale ladder, overlays, charts, evidence drawer, intervention builder |
| **Validation** | health-rigor-lead | tech-lead | Golden equivalence, expectation harness, kb-check, planted violations, gates |
| **Education** | ui-designer with docs-explainer | health-safety-reviewer | Scenario narratives mapped to blocks and expectations; learner surfaces |
| **Platform and process** | orchestrator with build-reporter, docs-steward, bug-triage, qa-auditor | tech-lead | Registry, ADRs, process log, ledger, CI, launchers, measurements |

Every workstream's work passes `02 §4` workflows; nothing merges without the Operator.

## 3. Milestones

Each milestone is a set of modules (`07`), each landing with its flag off, its
expectations registered first, its own ADR, and its removal recipe rehearsed.

### M0 — Foundation (this pull request)

Deliverables: vendored V1; Python engine with golden equivalence; knowledge-base
checker; expectation harness; documents 00–08; agents; config; gates and CI; registry;
ADRs; process log; tracker.
**Exit:** `tools/gates.py` green including both health suites; `kb-check` clean; every
registered expectation has a status; the status line prints the grade share, the
assumption count, the modules and the disclaimer. **Decision for Spencer:** approve the
PR; answer Q1 (home repository).

### M1 — The body is scoped, and the viewer is reviewed (Phase 1)

Modules: `body-scaling` (volumes and GFR scale with mass and BSA; the reference person
becomes a parameter set), `potassium` (plasma K as a state with its own expectations),
`sodium-storage` (the compartment the chronic-salt known divergence points at), and
`viewer-review-1` (D-1, D-4, D-7 applied; evidence drawer shows grade E as a red pill
and a banner; narratives for the six registered scenarios).
**Exit:** the known-divergence row for `chronic_high_salt_30d` is resolved or re-stated
with new evidence; design review on the Operator's machine passed; the Suckling SEM/SD
question (Q4) answered from the full text or left `unverified` with the attempt logged.
**Decisions for Spencer:** D-1, D-4, D-7; D-6 if publishing.

### M2 — Pressure and the kidney over years (Phase 2)

Modules: `pressure-outcomes` (chronic blood pressure linked to outcome evidence from
trials and cohorts, with the calibration/validation split per parameter) and
`ckd-progression`. The lab gains year-scale horizons; the viewer gains the "chronic risk
grows in real time" view with its label.
**Exit:** a pre-registered expectation set drawn from at least one meta-analysis and
one cohort; the rigor-lead's ±20 % perturbation flips no quantitative expectation.

### M3 — Energy metabolism (Phase 3)

Modules: `glucose-insulin`, `obesity-hypertension-coupling`. The scale ladder gains the
pathways and molecules these need; the knowledge base grows under the same contract.
**Exit:** the same standard as M2.

### M4 — Populations and interventions (Phase 4)

Modules: `population-layer` (distributions over the reference person), `interventions`
(comparison with bands). This is the first point at which the platform can say
something about a policy rather than a person; it is also the first point at which the
clinical-safety reviewer's scope widens to population claims.
**Exit:** independent review by rigor-lead and safety-reviewer; an Operator decision on
publication; `00 §1.4` non-goals re-affirmed in writing.

### M5 — The surface for the ages (Phase 5)

A surface built from an approved design spec, on the Operator's machine, with the
reference app retired only when the new one passes design review. Learner mode is the
same model with the same labels (HREQ-P-16).

## 4. Cadence

- **Per change:** the workflow in `02 §4`; gates green; PR; Operator approval.
- **Weekly:** `02 WF-H-07` — research Monday, rigor audit Tuesday, curation Wednesday, build Thursday, Operator briefing Friday with the status line and the tracker.
- **Per milestone:** ADR for each module; design review on the Operator's machine; the process log entry; measurements recorded when the Operator runs the stress test.

## 5. Compute plan

The engine is pure Python for the gates and JavaScript for the browser. Heavy work —
Monte Carlo at `n` in the hundreds, dose sweeps, year-scale chronic runs — runs in the
browser's worker pool sized to the Operator's cores (V1 `app/worker.js`; the stress
panel reports samples per second and frame rate at 64, 256 and 1,024 samples). The
rule of HREQ-N-07: nothing runs on the Operator's machine that he has not launched
himself. When the Operator runs `Stress_Test`, the numbers go into
`docs/health/measurements/<date>_stress.md`, append-only, and future sizing decisions
cite them rather than guess.

## 6. Risks, and what is already done about each

| Risk | Mitigation in place | Owner |
|---|---|---|
| A plausible wrong number reaches a learner | Safety review on every surface; disclaimer in-band; indices and thresholds labelled; S0a halts the surface | health-safety-reviewer |
| Assumptions quietly become "facts" | Grade E loud (HREQ-S-06); curator and modeler own different fields of the same row; calibration never counts as validation | health-literature-curator |
| The two implementations drift | Golden fixture, 1e-9, regenerated on every model change, S0b on divergence | health-physiology-modeler |
| The knowledge base rots | 40 contract rules, each with a planted-violation test; append-only records | health-rigor-lead |
| Scope grows without a record | Module registry, two-PR enable, ADR per module | orchestrator |
| The project lives in two places (this repo and the Mac) | Q1 decided before M1; until then the Mac repo is the Operator's and this branch is the platform | Spencer |

## 7. What the Operator sees, and when

- Every Friday: the status line, the tracker's open decisions with rendered options where the question is visual, the ledger's open S0/S1 items, and what is blocked on him.
- Every PR: what changed and why in plain language, what was verified and how, what is deferred, the risk of merging, and the decisions that are his.
- Never: a question with no recommendation, or a design choice described in prose only.
