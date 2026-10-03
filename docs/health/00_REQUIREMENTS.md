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

Several of the largest contributors to the global burden of death and disability —
ischaemic heart disease and stroke, chronic kidney disease and type 2 diabetes, with high
body-mass index a major risk factor for each — involve, among many other mechanisms, the
regulation of body fluid, sodium, arterial pressure, renal function and energy
metabolism. (Rankings and figures will come from the Global Burden of Disease study
through a graded evidence record before any document quotes them, per HREQ-D-01; until
then this document makes no ranking claim and quotes no figure.)

The understanding of that substrate is scattered across textbooks, trials, reviews and
models, in different units, with different levels of evidence, and with assumptions
that are rarely labelled as assumptions. Anyone who wants to answer "if sodium intake
changes by this much, what happens to pressure, over what time, with what confidence,
and which parts of that answer are evidence versus guess?" cannot do so from the
literature in a reasonable time, and cannot audit the answer once given.

### 1.2 The Operator's goals, in his words

These are the aspirations every requirement below traces to, bounded by §1.4 (ADR-0006):

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

These are the Operator's aspirations for the project, quoted as he wrote them (spelling
lightly corrected; the unedited text is in ADR-0006). They are not claims about this
software. Where they reach past §1.4, the non-goals govern. "Accurate" means accurate as
far as the graded evidence and the expectation record show, and no further (§1.3; for
V1, zero pre-registered independent expectations against four calibrated parameters,
`03 §12`). "How much salt does it take to strain the kidney" is shown only as a model
index for a reference adult, never as an amount for a person. "Chronic risks" are
modelled trajectories, not anyone's risk. "Disease cure" and "better trained doctors,
nurses, and scientists" are outcomes the project hopes its research and teaching may
contribute to, not functions it performs: it does not diagnose, treat, or train anyone
to treat.

Four product ideas live in that paragraph, and each is a surface or a layer here:

| Idea | What it is in this system |
|---|---|
| **The body, at every scale** | The 3D viewer with its scale ladder (Body → Organ → Tissue → Cell → Pathway → Molecule) bound to the knowledge base's entities and relations |
| **A technical repository of effects** | The knowledge base (entities, relations, evidence, verification) plus the engine's modules, each a graded, cited mechanism |
| **A virtual lab** | Scenarios and the intervention builder: acute loads (water, salt), chronic exposures (30 days of salt), salt-load sweeps, with Monte Carlo bands and literature expectations |
| **"Chronic risks" as modelled trajectories** | Long-horizon states (the slow autoregulation state today; chronic pressure and kidney-function modules in Phase 2, with any outcome layer population-only, §10) rendered as modelled trajectories for the reference adult, labelled as such and never as a person's risk |

### 1.3 Thesis

**Auditable understanding is the product.** A mechanistic model whose every parameter
carries a source and a grade, whose every output carries an uncertainty band, whose
every claim is tested against expectations registered before the result was seen, and
whose every decision is written down, is a tool a reader can trust *exactly as far as
the evidence goes and no further* — and can see where that is.

The Operator's Metabolic Map V1 — a 13-state model of body water, sodium, vasopressin,
aldosterone, atrial natriuretic peptide, arterial pressure and the kidney, with 54
graded parameters, 57 verified sources, 107 ontology-linked entities and literature
expectations on seven scenarios — is the first slice. It is vendored verbatim under
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
- Presenting a classification threshold as a physiological or safety limit.
- Any "safe", "maximum" or "recommended" amount of water, salt or any other intake for a person.
- Individual risk estimates or outcome probabilities (outcome layers report population-level quantities only, `01` HREQ-M-13).
- Use as a clinical reference for decisions about, or instructions to, any patient — including by clinicians in training.
- Drug or insulin dosing of any kind.
- Claims of clinical validation. The surface standard is the two-sentence disclaimer "Educational model — not medical advice. Not clinically validated." The V1 app renders only the first sentence (`engine/index.js` `DISCLAIMER`; the second exists only as a comment in `engine/model.js`), so V1 falls short of this standard; recorded as a V1 audit item.

**Intended use.** Education and research about mechanisms in a modelled reference adult
and parameter-range populations. Not intended for use with any individual's data, or for
diagnosis, prognosis, treatment or any clinical decision. Every surface carries this
statement with the disclaimer.

### 1.5 Honest statement of difficulty

Thirty of the fifty-four V1 parameters are assumptions (grade `E-assumption`). Four were
tuned to targets: three jointly to one meta-analysis (He 2013, the chronic pressure
response to salt) and one (`map_auto_tau_h`) to a design target for that response's
time course; none of those targets can validate them. None of the 24 V1 expectations was
demonstrably registered before results were seen; of the 12 countable ones, one is that
calibration target and one a structural check, so at most 10 can count, all
co-developed. Eight expectations are qualitative, one is explicitly unverified pending a
full-text check (Suckling 2012), and one is a known divergence (the model expands
extracellular volume under chronic salt where one human study found no total-body-water
gain). One baseline intake is graded higher than its own record says it should be
(V1 audit F-12), the He 2013 band is itself approximate (F-06), parameters are sampled as
if independent, and much of the source literature describes small groups of young
healthy men. None of this is hidden; all of it is in the files. The project's standard
is that this *stays* visible as the model grows, and that a later reader can tell
measured from assumed from calibrated in one glance.

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
| **Learner** (student, nurse, clinician in training) | Understand a mechanism by watching it: run the 1 L water scenario on the 70 kg reference adult, follow the water from gut to urine, see why the modelled sodium dips and recovers; know which parts are textbook and which are guesses. Education only: not a reference for clinical values or thresholds, and not for decisions about any patient | The 3D app's scale ladder, scenario descriptions, the evidence drawer with grades |
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

- **HREQ-S-01** Every result object the engine returns SHALL carry the disclaimer as a field (`meta.disclaimer`, the V1 string) and the validation status as a field (`meta.validation_status`, "Not clinically validated."), and every user-facing surface SHALL render both where the result is shown. A rendered result without them is S0a: the surface withholds the value (`05 §6`, `MissingDisclaimerError`); a document without them is S1.
- **HREQ-S-02** No component SHALL produce an individual recommendation. A scenario describes a population reference person; the output is a model trajectory with a band, never advice.
- **HREQ-S-03** Classification thresholds (for example the 135–145 mmol/L plasma sodium range) SHALL be stored as `mc: false` parameters and SHALL be rendered as reference lines, in a neutral tone, with the label `display.threshold_label` from `config/health/base.yaml`.
- **HREQ-S-04** Any index that is not a clinical measure SHALL be named as an index, SHALL carry the label `display.index_label` from `config/health/base.yaml` in its definition and on every surface, and SHALL have its weights and scales listed as `E-assumption` parameters.
- **HREQ-S-05** The clinical-safety reviewer (`02 §2.4`) SHALL review every user-facing claim before it reaches the Operator. Their sign-off is recorded in the PR description.
- **HREQ-S-06** Grade-E (assumption) parameters SHALL be loud on every surface: a red pill on the parameter and a banner on every chart they influence (per the sensitivity screen), with the count visible. Decided by the Operator (ADR-0006); V1 ships at 30 of 54 and the number is never hidden.
- **HREQ-S-07** Known divergences, unverified expectations and calibration targets SHALL be shown on every surface that shows an output they affect, not only in a file.

## 4. Data and Knowledge Requirements

- **HREQ-D-01** Every numeric claim in the knowledge base, the parameter table, a scenario expectation, or a document SHALL cite an evidence record that resolves, with a grade. A number without a source is an S2 defect (provenance loss).
- **HREQ-D-02** Evidence records SHALL be verified (DOI/PMID resolves, title matches, retraction status checked, date of check recorded) before any quantity cites them. The verification method and date are fields, not comments.
- **HREQ-D-03** The knowledge base SHALL conform to `src/health/kb/data/schema.json` and to the cross-file rules it describes (references resolve; parent scale ≤ child scale; `engineParam` mirrors `params.json`; external identifiers verified in the verification log; word limits), enforced by `health.cli kb-check` in CI.
- **HREQ-D-04** Entities, relations, evidence and verification records are append-only. A correction is a new record that supersedes, and a disagreeing source is kept in `conflicts` beside the adopted value.
- **HREQ-D-05** Every parameter SHALL carry `value`, `unit`, `range`, `evidence`, `grade`, and `notes` that say what kind of range it is (reported interval, mean ± 2 SEM, curator assumption). The methodology (`01 §4`) defines the range kinds.
- **HREQ-D-06** The share of parameters graded ≥ B, the count of `E-assumption` parameters, and the counts of expectations by status SHALL be computed by code (`health.cli status`). A document may quote such a figure only as a snapshot marked with the model version it describes (all figures in this document set are as of model 1.0.1).

## 5. Functional Requirements

### 5.1 Model and engine

- **HREQ-P-01** The Python engine SHALL implement the V1 model exactly: the same state vector, derived quantities, ledger, scenarios, Monte Carlo sampler and sensitivity screen, with the JavaScript's identifiers for every state, derived quantity, ledger entry, parameter and scenario and the JavaScript's keys in the result dictionaries the two share (function and argument names follow Python convention: `simulate_mc` for `simulateMC`, `t_end` for `tEnd`), so a reader can audit the port against `reference/metabolic-map-v1/engine/model.js` block by block (M0–M10).
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
- **HREQ-P-12** The virtual lab SHALL let a user compose an intervention — amounts, timing, duration, chronic overrides — from the same primitives the registered scenarios use, and SHALL run it with Monte Carlo bands. Every custom run SHALL be labelled "custom intervention on the 70 kg reference adult — no registered expectations", SHALL carry the disclaimer and the grade-E banner in-band, SHALL mark any input outside the range covered by the cited human studies as "extrapolation beyond the evidence", SHALL NOT accept body size, laboratory values or other personal data (§1.4), and SHALL NOT report a "safe", "maximum" or "recommended" amount.
- **HREQ-P-13** Chronic trajectories SHALL be rendered with the label `display.trajectory_label` from `config/health/base.yaml` in-band (today the V1 string; see tracker D-8 on its wording), the horizon stated, the slow states that produce them named (today `R_auto`), and never as a risk estimate for any person.
- **HREQ-P-14** Every chart SHALL offer the evidence drawer: the parameters that influence it (from the sensitivity screen), each with grade, range, source and verification date.

### 5.5 Education

- **HREQ-P-15** Each registered scenario SHALL carry a plain-language narrative of the mechanism ("in the model's 70 kg reference adult, the water is absorbed over roughly the first hour (half-time about 12 minutes); plasma sodium dips; vasopressin falls; the urine dilutes; most of the load is excreted within about three to four hours (band shown)") whose every sentence is phrased about the model's reference adult, carries the scenario's band, and maps to a block of the model and to an expectation row, so a learner can click from the sentence to the evidence.
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

**Phase 2 — Pressure and the kidney over years (research).** A chronic blood-pressure
module and a kidney-function-decline module for the reference adult and the
parameter-range population. Any link from a model state (MAP, GFR) to an outcome
(stroke, kidney failure, death) is a separate outcome layer: it reports population-level
quantities only, is validated against cohort or trial outcome data and graded separately
(`01` HREQ-M-13), is never computed from a user-composed intervention, and is never
presented as a person's risk. *Exit:* a pre-registered expectation set drawn from at
least one meta-analysis and one cohort, with the calibration/validation split stated per
parameter; independent review by the rigor-lead and the clinical-safety reviewer; the
§1.4 non-goals re-affirmed in writing; an Operator decision before any outcome-linked
view is enabled.

**Phase 3 — Energy metabolism.** Glucose–insulin dynamics and the obesity–hypertension
coupling, the bridge to type 2 diabetes. No insulin or drug dosing and no glycaemic
targets are produced. *Exit:* the same standard as Phase 2.

**Phase 4 — Populations and interventions.** A population layer (distributions over the
reference person) and intervention comparison with uncertainty — the first point at
which the platform compares modelled population-level scenarios: research for
independent review, not a policy recommendation and not about any individual. *Exit:*
independent review by the rigor-lead and the clinical-safety reviewer, the §1.4
non-goals re-affirmed in writing, and an Operator decision on publication.

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
| D-8 | The chronic trajectory label: keep V1's "modeled risk trajectory — not a prediction" or drop "risk" ("modelled trajectory for a 70 kg reference adult — not a prediction or a risk estimate for any person"), as the safety review recommends | M1 | Keep V1's string until decided; both implementations change together |
| A-2 | The published V1 artifact shows the strain index unlabelled on the dose panel and the HUD gauge (S0a, BUG-20261003-100): patch the artifact, or accept until M1's viewer review | Now | The vendored copy stays unchanged (HREQ-P-09); the artifact is the Operator's to patch |

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
