# Requirements Document
## Health — Evidence-Graded Mechanistic Modelling Platform

**Version:** 0.1 (Draft for approval)
**Date:** 2026-10-03
**Owner:** Spencer
**Status:** Awaiting sign-off. The V1 slice already exists and is vendored; new modules are not built against this document until §12 is agreed.
**Companion to:** `01_METHODOLOGY.md`, `02_AGENT_HIERARCHY.md`, `03_VALIDATION_AND_TESTING.md`, `04_ENVIRONMENTS_AND_UNDO.md`, `05_BUG_TAXONOMY.md`, `06_ARCHITECTURE.md`, `07_MODULE_REGISTRY.md`

---

## 1. Purpose and Thesis

### 1.1 Problem statement

The diseases that kill and disable the most people — ischaemic heart disease, stroke,
chronic kidney disease, type 2 diabetes and the obesity that drives them — share one
physiological substrate: the regulation of body fluid, sodium, arterial pressure, renal
function and energy metabolism. (The ranking is the Global Burden of Disease study's;
the specific figures belong in the knowledge base with a graded citation before any
document quotes them, per HREQ-D-01. This document deliberately quotes none.)

The understanding of that substrate is scattered across textbooks, trials, reviews and
models, in different units, with different levels of evidence, and with assumptions
that are rarely labelled as assumptions. Anyone who wants to answer "if sodium intake
changes by this much, what happens to pressure, over what time, with what confidence,
and which parts of that answer are evidence versus guess?" cannot do so from the
literature in a reasonable time, and cannot audit the answer once given.

### 1.2 The Operator's goals, verbatim

These are the reference points every requirement below traces to (ADR-0006):

> "a 3D intractable model … visualize at various scale a full technical repository of
> metabolic and molecular biology effects … an accurate and digestable model of the human
> body — better than any one system that has been created so far … educate, test and
> have our own accurate virtual lab … What does drinking water do to blood electrolyte
> levels, how much salt does it take to strain the kidney, how can we model acute effects
> generally. How can we see chronic risks grow in the body in real time … an educational
> resource for the ages … research insights and disease cure as well as better trained
> doctors, nurses, and scientists … heavily relying upon published papers and showing all
> uncertainty. Extreme data rigor, efficient system design, use any resource … this new
> laptop of mine can process and store. Test its very limits."

Four product ideas live in that paragraph, and each is a surface or a layer here:

| Idea | What it is in this system |
|---|---|
| **The body, at every scale** | The 3D viewer with its scale ladder (Body → Organ → Tissue → Cell → Pathway → Molecule) bound to the knowledge base's entities and relations |
| **A technical repository of effects** | The knowledge base (entities, relations, evidence, verification) plus the engine's modules, each a graded, cited mechanism |
| **A virtual lab** | Scenarios and the intervention builder: acute loads (water, salt), chronic exposures (30 days of salt), dose sweeps, with Monte Carlo bands and literature expectations |
| **Chronic risk in real time** | Long-horizon states (the slow autoregulation state today; pressure→outcome and kidney-progression modules in Phase 2) rendered as modelled trajectories, labelled as such |

### 1.3 Thesis

**Auditable understanding is the product.** A mechanistic model whose every parameter
carries a source and a grade, whose every output carries an uncertainty band, whose
every claim is tested against expectations registered before the result was seen, and
whose every decision is written down, is a tool a reader can trust *exactly as far as
the evidence goes and no further* — and can see where that is.

The Operator's Metabolic Map V1 — a 13-state model of body water, sodium, vasopressin,
aldosterone, atrial natriuretic peptide, arterial pressure and the kidney, with 54
graded parameters, 57 verified sources, 107 ontology-linked entities and literature
expectations on six scenarios — is the first slice. It is vendored verbatim under
`reference/metabolic-map-v1/` and is the thing everything here is measured against.

The platform grows by **modules**, each a small, removable, independently validated
extension of the model, the knowledge base, or the tooling, in the order the evidence
supports (§10).

### 1.4 Non-goals (explicit)

The following are **out of scope** and any proposal to add them requires a written
amendment and the clinical-safety reviewer's sign-off:

- Individual diagnosis, prognosis, treatment or dosing recommendations of any kind.
- Anything that would require regulatory clearance as a medical device.
- Collection, storage or processing of any person's health data. V1 has no personal data; a future module that needs any must be a separate amendment with its own safety document.
- Presenting a model index (the kidney strain index) as a clinical measure.
- Claims of clinical validation. The V1 app's own disclaimer — "Educational model — not medical advice. Not clinically validated." — is the standard.

### 1.5 Honest statement of difficulty

Thirty of the fifty-four V1 parameters are assumptions (grade `E-assumption`). Three of
them — the ones that set the chronic pressure response to salt — were calibrated to a
single meta-analysis and are therefore not validated by it. Several expectations are
qualitative, one is explicitly unverified pending a full-text check, and one is a known
divergence (the model expands extracellular volume under chronic salt where one human
study found no total-body-water gain). None of this is hidden; all of it is in the
files. The project's standard is that this *stays* visible as the model grows, and that
a later reader can tell measured from assumed from calibrated in one glance.

---

## 2. Scope

### 2.1 In scope (v1 of the platform, this pull request)

| In scope | Out of scope (for now) |
|---|---|
| The V1 model, vendored and re-implemented in Python with proof of equivalence | New physiology (potassium dynamics, glucose, body-size scaling) — Phase 2+ |
| The knowledge base with its contract enforced by code | New knowledge-base content — requires the literature-curator workflow |
| A literature-expectation harness that evaluates every registered expectation | New expectations — requires the pre-registration workflow |
| Process: documents, agents, bug ledger, gates, CI, module registry, ADRs | A Python dashboard — the reference app is the V1 surface until a design spec exists |

### 2.2 Users and modes

One human Operator (Spencer), who approves everything, and three audiences the product
is for. The audiences never get a different model; they get the same model with the same
bands and the same labels.

| User | Need | Surface |
|---|---|---|
| **Operator — Audit** | Which numbers are evidence, which are assumptions, what changed and why | `health.cli status`, `kb-check`, the bug ledger, the process log |
| **Operator — Explore** | Run a scenario, see the band, see what drives it, see the body respond at every scale | The 3D app (viewer, charts, evidence drawer) |
| **Operator — Extend** | Add a parameter, a scenario, an expectation, a module, safely and reversibly | The workflows in `02` and the registry in `07` |
| **Learner** (student, nurse, clinician in training) | Understand a mechanism by watching it: drink a litre, follow the water from gut to urine, see why sodium dips and recovers; know which parts are textbook and which are guesses | The 3D app's scale ladder, scenario descriptions, the evidence drawer with grades |
| **Researcher** | A reproducible, cited, perturbable model to form and test hypotheses against; every assumption visible; every expectation's provenance one click away | The engine API, the expectation harness, the knowledge base, the sensitivity screen |

The Operator conceptualises data visually and asked to be shown choices as pictures
(ADR-0006). Design questions go to him with rendered options, never as prose alone
(`02 §3`, design-lead).

### 2.3 Deployment scope

Local-first, single-operator, no network listener beyond localhost, no accounts, no
personal data. The published reference app is static files. This removes an entire
class of security and privacy obligations from v1 and is deliberate.

---

## 3. Safety Requirements

- **HREQ-S-01** Every result object the engine returns SHALL carry the disclaimer string as a field (`meta.disclaimer`), and every user-facing surface SHALL render it where the result is shown. A surface that drops it is an S1 defect (`05 §2`).
- **HREQ-S-02** No component SHALL produce an individual recommendation. A scenario describes a population reference person; the output is a model trajectory with a band, never advice.
- **HREQ-S-03** Classification thresholds (for example the 135–145 mmol/L plasma sodium range) SHALL be stored as `mc: false` parameters labelled *classification threshold, not a physiological parameter*, and SHALL be rendered as reference lines with that label.
- **HREQ-S-04** Any index that is not a clinical measure SHALL be named as an index, SHALL carry the sentence "an index, not a clinical measure" in its definition and on every surface, and SHALL have its weights and scales listed as `E-assumption` parameters.
- **HREQ-S-05** The clinical-safety reviewer (`02 §3.4`) SHALL review every user-facing claim before it reaches the Operator. Their sign-off is recorded in the PR description.
- **HREQ-S-06** Grade-E (assumption) parameters SHALL be loud on every surface: a red pill on the parameter and a banner on every chart they influence (per the sensitivity screen), with the count visible. Decided by the Operator (ADR-0006); V1 ships at 30 of 54 and the number is never hidden.

## 4. Data and Knowledge Requirements

- **HREQ-D-01** Every numeric claim in the knowledge base, the parameter table, a scenario expectation, or a document SHALL cite an evidence record that resolves, with a grade. A number without a source is an S2 defect (provenance loss).
- **HREQ-D-02** Evidence records SHALL be verified (DOI/PMID resolves, title matches, retraction status checked, date of check recorded) before any quantity cites them. The verification method and date are fields, not comments.
- **HREQ-D-03** The knowledge base SHALL conform to `src/health/kb/data/schema.json` and to the cross-file rules it describes (references resolve; parent scale ≤ child scale; `engineParam` mirrors `params.json`; external identifiers verified in the verification log; word limits), enforced by `health.cli kb-check` in CI.
- **HREQ-D-04** Entities, relations, evidence and verification records are append-only. A correction is a new record that supersedes, and a disagreeing source is kept in `conflicts` beside the adopted value.
- **HREQ-D-05** Every parameter SHALL carry `value`, `unit`, `range`, `evidence`, `grade`, and `notes` that say what kind of range it is (reported interval, mean ± 2 SEM, curator assumption). The methodology (`01 §4`) defines the range kinds.
- **HREQ-D-06** The share of parameters graded ≥ B, the count of `E-assumption` parameters, and the counts of expectations by status SHALL be computed by code (`health.cli status`) and never hand-typed into a document.

## 5. Functional Requirements

### 5.1 Model and engine

- **HREQ-P-01** The Python engine SHALL implement the V1 model exactly: the same state vector, derived quantities, ledger, scenarios, Monte Carlo sampler and sensitivity screen, with the same identifiers, so a reader can audit the port against `reference/metabolic-map-v1/engine/model.js` block by block (M0–M10).
- **HREQ-P-02** The Python engine SHALL reproduce the JavaScript reference to 1e-9 relative on the golden fixture (`03 §4`). Divergence halts the release (`05 §2`, S0b).
- **HREQ-P-03** Every simulation result SHALL be reproducible from (model version, parameter set, scenario, seed, dt). Those fields are part of the result object.
- **HREQ-P-04** The engine SHALL refuse infeasible parameter sets loudly (`initial_state` raises) and SHALL count rejected Monte Carlo draws rather than silently resampling without a record.

### 5.2 Validation harness

- **HREQ-P-05** `evaluate_expectations` SHALL return exactly one row per registered expectation, with status `pass`, `fail`, or `not_checked` and the reason, and SHALL never omit a row.
- **HREQ-P-06** The harness SHALL evaluate the default-parameter run and SHALL be able to evaluate the Monte Carlo median; the surface SHALL say which was evaluated.

### 5.3 Tooling and surfaces

- **HREQ-P-07** `health.cli status` SHALL print, in this order: package version, model version, knowledge-base version and curator, counts (entities, relations, evidence, verification records), parameter grade distribution and the ≥ B share, and the disclaimer on its own line.
- **HREQ-P-08** `health.cli kb-check` SHALL exit non-zero on any `error`-severity finding and SHALL print `warn` and `info` findings without failing.
- **HREQ-P-09** The reference app SHALL remain runnable from `reference/metabolic-map-v1/index.html` unchanged; it is the V1 surface and the fixture for design review.

### 5.4 The 3D body and the virtual lab

- **HREQ-P-10** The viewer SHALL present the body on a scale ladder — Body, Organ, Tissue, Cell, Pathway, Molecule — where every rung is a knowledge-base entity with its ontology id, summary, evidence and the live quantities the engine computes for it. A rung with no entity behind it is not shown.
- **HREQ-P-11** Every live overlay on the body (a colour, a badge, a pulse) SHALL be a display rule over an engine quantity, named in config, labelled on screen with the quantity and its unit, and never a value with no model behind it. Colour breakpoints on an index are display choices and SHALL say so (V1 decision D-1).
- **HREQ-P-12** The virtual lab SHALL let a user compose an intervention — amounts, timing, duration, chronic overrides — from the same primitives the registered scenarios use, and SHALL run it with Monte Carlo bands; a custom run is labelled *custom, no registered expectations*.
- **HREQ-P-13** Chronic trajectories SHALL be rendered with the label "modelled risk trajectory, not a prediction" in-band, the horizon stated, and the slow states that produce them named (today `R_auto`).
- **HREQ-P-14** Every chart SHALL offer the evidence drawer: the parameters that influence it (from the sensitivity screen), each with grade, range, source and verification date.

### 5.5 Education

- **HREQ-P-15** Each registered scenario SHALL carry a plain-language narrative of the mechanism ("the water is absorbed within minutes; plasma sodium dips; vasopressin is suppressed; urine dilutes; the load is excreted in three to four hours") whose every sentence maps to a block of the model and to an expectation row, so a learner can click from the sentence to the evidence.
- **HREQ-P-16** A learner-facing surface SHALL show the same bands, grades and labels as the Operator's; there is no simplified mode that hides uncertainty.

## 6. Non-Functional Requirements

- **HREQ-N-01** Correctness over completeness. Where a number cannot be supported, the system shows "unavailable", never a plausible substitute (as in `docs/00` REQ-N-01).
- **HREQ-N-02** Pure standard library for the engine, the knowledge-base checker and the tests, so the gates run before any dependency is installed (the stdlib-only floor, `docs/03 §4.6`). Optional dependencies are declared with `@needs`.
- **HREQ-N-03** Python 3.10 compatibility (the Operator's Linux workspace, `pyproject.toml`).
- **HREQ-N-04** The engine self-test SHALL complete in under two minutes in pure Python; Monte Carlo tests use small `n` and say so.
- **HREQ-N-05** No network listener beyond localhost; no credentials of any kind in v1; CI's secret scan and transaction-capability checks apply to `src/health` unchanged.
- **HREQ-N-06** Every threshold, tolerance, seed default and flag lives in `config/health/*.yaml` or `params.json`, never in code (as `docs/00` REQ-N-09).
- **HREQ-N-07** Compute SHALL scale to the Operator's machine: Monte Carlo and sweeps run on a worker pool sized to the available cores, with a stress test that reports samples per second and frame rate at 64, 256 and 1,024 samples (V1 `app/worker.js` and the stress panel), and the numbers are recorded in `docs/health/measurements/` when the Operator runs it. "Use any resource this laptop can process and store" is a budget, not a licence: nothing runs on the Operator's machine that he has not launched.

## 7. Extensibility and Undo Requirements

- **HREQ-X-01** Every module SHALL have an entry in `config/health/modules.yaml` with `enabled`, `owner`, `paths`, `depends_on`, `tests`, and `removal` (the written recipe). `07_MODULE_REGISTRY.md` is generated from, or kept identical to, that file.
- **HREQ-X-02** A module SHALL be removable by: setting `enabled: false`, deleting its paths, removing its registry entry, and running `tools/gates.py` green. If any other step is needed, the module is not finished.
- **HREQ-X-03** A new module SHALL land in its own pull request with its own ADR, its own tests, and its registry entry, so that `git revert` of that merge is a complete undo.
- **HREQ-X-04** Decisions are recorded as ADRs under `docs/health/decisions/`. An ADR is never edited after acceptance; it is superseded by a new one that names it.
- **HREQ-X-05** Model changes SHALL bump `MODEL_VERSION` and regenerate the golden fixture in the same change, with the regeneration command in the PR description.

---

## 8. Data Model (logical)

```
Evidence ──< Entity ──< Relation ──> Entity
    │           │
    │           └── externalIds ──> VerificationRecord
    │
    ├──< Quantity (value, unit, range, grade, conflicts[]) ──> Parameter (params.json row)
    │
    └──< Expectation ──< Scenario ──< Result (t, states, derived, ledger, meta{seed, version, disclaimer})
                                        │
                                        └──< ExpectationRow (pass | fail | not_checked, actual, reason)

Decision (ADR) ──> Module ──> {paths, tests, flag, removal}
Bug (ledger) ──> {severity, class, regression_test, data_impact, monitor_gap}
```

Cross-cutting on every record: who verified it, when, by what method; which version of
the model or knowledge base it belongs to.

---

## 9. Architecture (summary; the full picture with diagrams is `06_ARCHITECTURE.md`)

```
┌──────────────────────────────────────────────────────────────┐
│ PROCESS   agents · bug ledger · process log · ADRs · gates    │
├──────────────────────────────────────────────────────────────┤
│ SURFACES  reference app (V1) · health.cli · future dashboard  │
├──────────────────────────────────────────────────────────────┤
│ VALIDATION golden equivalence · expectations · kb-check · CI  │
├──────────────────────────────────────────────────────────────┤
│ ENGINE    solver · model · scenarios · mc · validate          │
├──────────────────────────────────────────────────────────────┤
│ PARAMETERS params.json — value · unit · range · grade · source│
├──────────────────────────────────────────────────────────────┤
│ KNOWLEDGE  entities · relations · evidence · verification log │
└──────────────────────────────────────────────────────────────┘
```

| Layer | Choice | Rationale |
|---|---|---|
| Engine language | Python 3.10+, stdlib only | Runs in the repo's existing gates with zero dependencies; the JavaScript reference stays authoritative for the browser |
| Numerics | Fixed-step RK4 with event breakpoints, step bounded by the fastest time constant | Exactly what V1 does; linear invariants hold to round-off, which the mass-balance test relies on |
| Uncertainty | Seeded Monte Carlo over parameter ranges (mulberry32, identical draws in both implementations) | Deterministic across languages, so bands are comparable |
| Knowledge base | JSON with a JSON-Schema contract plus code-enforced cross-file rules | Human-readable, diffable, append-only friendly; no database to administer |
| Configuration | YAML under `config/health/` | Same rule as the rest of the repo (REQ-N-09) |

---

## 10. Phased Delivery

**Phase 0 — Foundation (this pull request).** Vendor V1; Python port with golden
equivalence; knowledge-base checker; expectation harness; documents; agents; gates;
module registry; ADRs; process log. *Exit:* `tools/gates.py` green including both health
self-tests; `kb-check` clean; every V1 quantitative expectation evaluated and its status
recorded.

**Phase 1 — Scope the body.** Body-size scaling of volumes and GFR (the V2 note in
`model.js` `REFERENCE_PERSON`); potassium as a real state with plasma K; the sodium
storage compartment that the chronic-salt known-divergence points at. Each is a module
with its own expectations registered first. *Exit:* the known-divergence row for
`chronic_high_salt_30d` is either resolved or re-stated with the new evidence.

**Phase 2 — Pressure and the kidney over years.** A chronic blood-pressure module
linked to outcome evidence from trials and cohorts, and a CKD-progression module.
*Exit:* a pre-registered expectation set drawn from at least one meta-analysis and one
cohort, with the calibration/validation split stated per parameter.

**Phase 3 — Energy metabolism.** Glucose–insulin dynamics and the obesity–hypertension
coupling, the bridge to type 2 diabetes. *Exit:* the same standard as Phase 2.

**Phase 4 — Populations and interventions.** A population layer (distributions over the
reference person) and intervention comparison with uncertainty — the first point at
which the platform can say something about a *policy* rather than a person. *Exit:*
independent review by the rigor-lead and the clinical-safety reviewer, and an Operator
decision on publication.

**The viewer in every phase.** The 3D app is not a late phase; it is the surface every
phase lands on. Each phase adds its entities to the scale ladder, its quantities to the
overlays, its scenarios to the lab and its narrative to the education layer, and passes
design review on the Operator's machine before it is enabled. The full plan with
milestones, owners and exit criteria is `08_PROJECT_PLAN.md`.

**Dependency note.** Phase 0 is the only phase that may not be deferred: every later
module is validated against the equivalence and expectation harness it delivers. Phases
1 and 3 can overlap; Phase 2 depends on Phase 1's sodium compartment; Phase 4 depends on
2 and 3.

---

## 11. Open Questions

| # | Question | Needed by | Default if unanswered |
|---|---|---|---|
| Q1 | Which repository is home: this one (`navanax1`, branch `claude/health-system-architecture-i9qgip`), the Mac repo `Desktop/The Human Body`, or a new `spencerwon/metabolic-map`? The Operator intended `metabolic-map` (ADR-0006) | Before Phase 1 | Stays here until the Operator publishes the Mac repo; then its history is attached or this subsystem migrates, by ADR |
| Q2 | Which disease module comes first after the body is scoped — pressure/kidney or energy metabolism? | Phase 2 | Pressure/kidney: it is continuous with V1 and its evidence base is the largest |
| Q3 | Will any future module need personal data (for example a user's own labs)? | Before Phase 4 | Assume no; a yes is a separate amendment with its own safety document |
| Q4 | Is the Suckling 2012 dispersion SEM or SD? (V1 audit items F-02/F-03) | Phase 1 | The expectation stays `unverified` and is not counted |
| Q5 | Numerical tolerance for golden equivalence if a future module needs an adaptive solver | Phase 2 | 1e-9 relative stays; an adaptive solver is a new ADR |
| D-1 | Kidney strain colour rule — A, B or C (`reference/metabolic-map-v1/app/design-options.html`) | Phase 1 viewer work | The rule currently in `viewer.js strainColor()`; breakpoints are display choices and say so |
| D-4 | Bladder display: grow-and-void at 0.4 L (display rule, not model) or hide | Phase 1 viewer work | Keep, labelled as a display rule |
| D-6 | Repository visibility and licence | Before publishing | Private until the Operator decides; licence undecided |
| D-7 | Theme: dark always, or follow the system | Phase 1 viewer work | Follow the system (both themes are designed) |

---

## 12. Acceptance Criteria

The Phase 0 platform is successful when all of the following hold:

1. **Equivalence** — the Python engine matches the JavaScript reference on every golden trajectory, sample set, band and metric to the stated tolerance, proven by a test that fails when any model constant is perturbed.
2. **Conservation** — the baseline scenario is a steady state (drift < 1e-6) and water and sodium balances close to 1e-9 over every shipped scenario.
3. **Expectations** — every registered expectation on every shipped scenario has a recorded status, and every `quantitative` expectation of `drink_water_1L` and `salt_load_10g` passes with default parameters.
4. **Knowledge base** — `kb-check` reports zero errors on the shipped data, and every rule it implements has a test that plants a violation and sees it reported.
5. **Visibility** — `health.cli status` prints the parameter grade share, the assumption count and the disclaimer, computed from the files.
6. **Process** — gates and CI run both health self-tests and the knowledge-base check before any dependency is installed; the module registry lists every module with a removal recipe; every decision of this build has an ADR; the process log records what was tried, including what failed.
7. **The honest test** — the Operator can, from the status line and the documents alone, say which parts of the V1 model are evidence, which are assumptions, and which are calibrated — without opening a source file.

---

## 13. Change Control

This document is the contract. Amendments require: a written statement of what
changes, why, what it invalidates, and re-approval. Requirement IDs are permanent;
superseded ones are marked `DEPRECATED` with a pointer, never deleted. Requirements
minted in `01_METHODOLOGY.md` (HREQ-M, HREQ-E, HREQ-U) and `03_VALIDATION_AND_TESTING.md`
(HREQ-V) are imported into Appendix A by `docs-steward` after each merge.

---

## Appendix A — Requirement index

Maintained by `docs-steward`. Until the first sweep, the authoritative lists are the
appendices of `01_METHODOLOGY.md` and `03_VALIDATION_AND_TESTING.md`.

| Prefix | Defined in | Covers |
|---|---|---|
| HREQ-S | this document §3 | safety and framing |
| HREQ-D | this document §4 | data and knowledge |
| HREQ-P | this document §5 | functional: engine, harness, tooling |
| HREQ-N | this document §6 | non-functional |
| HREQ-X | this document §7 | extensibility and undo |
| HREQ-M, HREQ-E, HREQ-U | `01_METHODOLOGY.md` | model, evidence, uncertainty |
| HREQ-V | `03_VALIDATION_AND_TESTING.md` | validation |
