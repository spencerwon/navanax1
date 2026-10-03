# Validation and Testing — Health Subsystem

**Version:** 0.1 · **Date:** 2026-10-03 · **Status:** Draft for Operator review
**Companion to:** `00_REQUIREMENTS.md`, `01_METHODOLOGY.md`, `02_AGENT_HIERARCHY.md`, `04_ENVIRONMENTS_AND_UNDO.md`, `05_BUG_TAXONOMY.md`, `06_ARCHITECTURE.md` (all in `docs/health/`)
**Inherits discipline from:** `docs/03_VALIDATION_AND_TESTING.md` (NFT platform). The rules carry over; the content does not.

---

## 1. Purpose

### 1.1 What this process is for

The purpose of validation here is to **find how we are wrong before a reader acts on a number**. It is not to show that the model works.

A trading platform that is subtly wrong produces a beautiful backtest. A physiology model that is subtly wrong produces a smooth, textbook-shaped curve with a tidy band around it. Nothing crashes. Sodium still dips after water and rises after salt. The error is in the magnitude, the timing or the width of the band, and those are what a reader takes away. Every check below is ordered by how cheaply it can find that kind of error.

### 1.2 The two failure classes

| Class | Question | Layers | Sections |
|---|---|---|---|
| **Computational correctness** | Does the code compute the model we wrote down, identically in both implementations? | Software, numerical, equivalence, steady state and conservation | §3, §4, §6 |
| **Scientific validity** | Does the model we wrote down describe human physiology well enough for the claims made from it? | Literature expectations, sensitivity, independent and safety review | §5, §7, §8 |

Standard software testing covers only the first class. Most of the risk to a reader sits in the second.

### 1.3 Governing principles

1. **Validation is independent.** `rigor-lead` never reviews its own work (`02_AGENT_HIERARCHY.md`). This is enforced on commit metadata (§8), not left to aspiration.
2. **Suspicion is proportional to good news.** A pass on a calibration target is not news (`01` §6). A new expectation that passes on its first run gets its extractor checked by hand before the pass is believed.
3. **Cheap tests first.** A model that leaks mass is not worth comparing with the literature.
4. **Nothing is silently skipped.** A test that did not run is never counted as passed; an expectation that was not checked carries a reason (§5). The NFT rule (`docs/03` §4.6) carries over: a test that needs a third-party module declares it with `@needs`, a skip is listed by name, and the strict run treats any skip as a failure.
5. **Tests are permanent.** Every bug becomes a regression test. Every knowledge-base rule has a planted violation that proves it fires (§6).
6. **Severity follows consequence to a reader** (`05_BUG_TAXONOMY.md`). A missing band is S1; a crash is not.

---

## 2. The Validation Pyramid

```
                         ┌───────────────────────┐
                         │  SAFETY REVIEW        │  hours, every user-facing change
                         └───────────┬───────────┘
                     ┌───────────────┴───────────────┐
                     │  INDEPENDENT REVIEW           │  hours to days, every change
                     └───────────────┬───────────────┘
                 ┌───────────────────┴───────────────────┐
                 │  SENSITIVITY AND ROBUSTNESS           │  minutes
                 └───────────────────┬───────────────────┘
             ┌───────────────────────┴───────────────────────┐
             │  LITERATURE EXPECTATIONS                      │  about a minute
             └───────────────────────┬───────────────────────┘
         ┌───────────────────────────┴───────────────────────────┐
         │  STEADY STATE AND CONSERVATION                        │  seconds
         └───────────────────────────┬───────────────────────────┘
     ┌───────────────────────────────┴───────────────────────────────┐
     │  REFERENCE-IMPLEMENTATION EQUIVALENCE (JS ↔ Python)           │  seconds
     └───────────────────────────────┬───────────────────────────────┘
 ┌───────────────────────────────────┴───────────────────────────────────┐
 │  NUMERICAL CORRECTNESS: step control, convergence, stiff case         │  seconds
 └───────────────────────────────────┬───────────────────────────────────┘
┌────────────────────────────────────┴────────────────────────────────────┐
│  SOFTWARE CORRECTNESS: unit · property · schema                         │  continuous
└─────────────────────────────────────────────────────────────────────────┘
```

| Layer | Question | Cost (basis) | A failure blocks |
|---|---|---|---|
| Software correctness | Do functions do what they say? | Seconds, every commit | Everything above |
| Numerical correctness | Does the solver integrate the equations accurately and stably? | Seconds: the 7 scenarios at three step sizes, with default and stiff parameters (per-run times measured 12–159 ms) | Equivalence, expectations |
| Reference equivalence | Do the JavaScript reference and the Python port agree to 1e-9? | Seconds to compare; regeneration only on a model change | All published results |
| Steady state and conservation | Does the model sit still at baseline and conserve water, sodium and potassium? | Seconds; about 11 s to cover 256 accepted samples at 42 ms each | Expectations |
| Literature expectations | Does the output fall inside pre-registered literature ranges? | About a minute at n = 256 for the 4 scenarios with countable expectations; the 30-day scenario alone takes about 41 s | Claims based on those expectations |
| Sensitivity and robustness | Do conclusions survive ±20 %, other seeds, other scenarios? | Minutes: about 6 min for seed stability, under 1 min for the perturbation set (estimates from measured per-run times) | Claims marked robust |
| Independent review | Did someone other than the author check the reasoning? | Hours to days | Merge |
| Safety review | Could a reader take a user-facing claim as advice or as a clinical measure? | Hours per user-facing change | Release to readers |

Run bottom-up. A failure in a lower layer makes every result above it unpublishable until it is fixed, because an expectation "pass" computed by a solver that leaks sodium is not a pass.

- **HREQ-V-01** The layers SHALL run bottom-up, and a failure in any layer SHALL block publication of every result that depends on a higher layer until it is resolved.

---

## 3. Numerical Gates

### 3.1 Steady state

The `baseline` scenario (`scenarios.js`) runs 24 h with nothing happening. Gate: for every state i, max over t of |y_i(t) − y_i(0)| / max(|y_i(0)|, 1e-12) < 1e-6, the `numerical` expectation in `scenarios.js`. Measured 2026-10-03 at default parameters: 9.7e-16 (ADH). The gate is nine orders above round-off on purpose. Its job is to catch a "steady state" that is not one (a wrong closed form in M0, or a flux missing from `rhs()`), not to measure round-off. Because the closed form must hold for every feasible parameter set, not only the default, the gate also runs on every accepted sample of the reporting set (n = 256).

### 3.2 Mass balance

RK4 preserves linear invariants to round-off (`solver.js`), and the model has four of them, audited against the flux ledger:

| Invariant | Quantity held constant |
|---|---|
| Water | `V_ecf + V_icf + V_gut_water − (water_in − water_out)` |
| Sodium | `Na_ecf + Na_gut − (na_in − na_out)` |
| Potassium | `K_icf − (k_in − k_out)` |
| Cell solute | `osm_icf_solute − 2·K_icf` (exact in V1; `model.js` M0 note) |

Gate: each is constant to 1e-9 relative to its value at t = 0, on every scenario. The tolerance is chosen. A violation above round-off is a bookkeeping bug, such as a flux added to a balance but not to the ledger, not a numerical error. 1e-9 sits far above accumulated round-off and far below anything physiological: 1e-9 of 42 L of body water is 42 nL. Measured worst case: 2.75e-12 (sodium, `chronic_high_salt_30d`, 41,664 steps), about 360 times inside the gate. Every other scenario is ≤ 2.4e-14, and the potassium and cell-solute invariants are exactly 0.

### 3.3 Step control and breakpoints

The internal step is `maxStep = min(dt, 0.25·τ_min)` (`engine/index.js`), where τ_min is the fastest of the eight time constants in `constants()`: 0.075 h (ANP) at default parameters. On the negative real axis, RK4 is stable up to hλ ≈ 2.785, so a step of a quarter of the fastest time constant sits more than eleven times inside that limit. Inputs are piecewise constant, and every time they jump is a breakpoint that no step may straddle (`solver.js`).

| Test | Pass criterion | Measured 2026-10-03 |
|---|---|---|
| Step bound | Instrumented solver: every step ≤ `maxStep`; step count = Σ over segments of ⌈length / maxStep⌉ | 41,664 steps on `chronic_high_salt_30d` |
| Breakpoint exactness | Off-grid bolus (1 L over 7.3 min starting at 1.0037 h) delivers its intake to 1e-12 relative (chosen) | Error 7.6e-15 |
| Breakpoint trap | The same bolus with breakpoints removed **must fail** the exactness test | 9.6 % of the bolus misplaced |
| Instability trap | Default parameters, `chronic_high_salt_30d`, step factor raised to 3.5 (capped by dt at 0.25 h = 3.3·τ_min): the finite-value gate **must fail** | Non-finite ANP within 48 h |

The two traps prove that the gates can see the failures they exist for. A gate that has never failed has not been shown to work.

### 3.4 Convergence

Each scenario runs at the production step factor of 0.25 and at a quarter of it (0.0625). Error is the maximum difference per state divided by that state's peak magnitude over the run. Gate (chosen): ≤ 1e-5 for every state except ADH and Thirst, which are held to ≤ 1e-3. Their targets pass through max(0, ·) or a [0, 1] clamp, and the solver does not locate those kinks, so convergence drops below fourth order near them.

| Measured worst (default and stiff parameters, all 7 scenarios) | Value | Order seen |
|---|---|---|
| Smooth states (worst: ANP, `drink_water_1L`) | 7.2e-7 | Error ratio 16–19 per halving: fourth order |
| ADH | 2.0e-5 | — |
| Thirst (`drink_water_1L`) | 3.2e-4 | Ratio about 3 |

The error is peak-scaled because pointwise relative error is meaningless when a state passes near zero. After a 3 L water load Thirst decays to about 1e-18; its pointwise figure there was 3.5e-2, and its peak-scaled figure 3.2e-4. Cost: an error that is small relative to a state's peak but large relative to its momentary value goes unflagged. V1 accepts this, and event location at kinks is deferred.

### 3.5 The stiff case

A parameter corner with five fast time constants at the lower ends of their ranges and the slowest at its upper end: `osm_eq_tau_h` 0.03, `anp_thalf_h` 0.042, `thirst_tau_h` 0.1, `aldo_tau_h` 0.5, `map_tau_h` 1, `map_auto_tau_h` 480. This gives τ_min = 0.030 h against a 480 h slow mode, a stiffness ratio of 16,000. `chronic_high_salt_30d` at this corner takes 101,184 steps (measured). The corner must pass §3.1–§3.4 and the finite-value gate. Measured: chronic convergence 1.4e-11; worst over all scenarios 3.0e-5 (Thirst). This is the test a future stiff or implicit solver must also pass (`01` §10).

### 3.6 Non-finite values

Any NaN or infinite value in a trajectory fails the run. A non-finite value is never plotted, summarised, quantiled or turned into an expectation status other than fail.

- **HREQ-V-02** The baseline scenario SHALL hold every state within 1e-6 relative of its initial value over 24 h, at default parameters and for every accepted sample of the reporting set.
- **HREQ-V-03** The water, sodium, potassium and cell-solute invariants SHALL hold to 1e-9 relative on every scenario.
- **HREQ-V-04** No integration step SHALL exceed `min(dt, 0.25·τ_min)` or straddle an input breakpoint, and an off-grid bolus SHALL be delivered to 1e-12 relative. The breakpoint trap and the instability trap SHALL be run and SHALL fail.
- **HREQ-V-05** Halving the step twice SHALL change no state by more than 1e-5 of its peak magnitude (1e-3 for ADH and Thirst).
- **HREQ-V-06** The stiff-corner parameter set SHALL pass every numerical gate on every scenario.
- **HREQ-V-07** A trajectory containing a non-finite value SHALL fail its run and SHALL NOT be displayed or summarised.

---

## 4. Reference Equivalence

### 4.1 One model, two implementations

The JavaScript reference (`reference/metabolic-map-v1/engine/`) runs in the browser. The Python port (`src/health/engine/`) runs the harness and the pipeline. Neither is ground truth by seniority; when they disagree, at least one is wrong. Cost: every model change is made twice. Benefit: a second implementation written from the same equations finds the bugs a single implementation hides, such as an operator-precedence slip, a missed breakpoint, or a unit converted in one place and not another.

### 4.2 The golden fixture

`tests/fixtures/health/golden_v1.json` is generated only by `tools/health_golden.mjs`, which imports the JavaScript reference and writes:

| Section | Content | Compared |
|---|---|---|
| Header | Generator version; `MODEL_VERSION` (V1: `1.0.1`); SHA-256 of every engine file and of `params.json`; Node version; date | Exact. A hash mismatch with the reference means the fixture is stale |
| Constants | `constants()` at default parameters (steady state, FE0, conductance, τ_min) | 1e-9 |
| Checkpoints | Initial state; `rhs()` at the initial state with baseline inputs; state after one RK4 step | 1e-9. These exist to localise a divergence (§4.4) |
| Trajectories | Every registered scenario at default parameters: t, 13 states, 21 derived, 6 ledger entries; `meta.steps` | 1e-9; step counts exact |
| Stiff corner | `chronic_high_salt_30d` and `drink_water_3L_fast` at the §3.5 corner | 1e-9 |
| PRNG | First 1,000 outputs of mulberry32 for seeds 1–5 (chosen) | Exact: integer arithmetic |
| Samples | `drawSamples(n = 64, seed = 1)`: accepted parameter sets and rejected count | Values 1e-9; count exact |
| Monte Carlo | `simulateMC` on `drink_water_1L`, n = 64, seed 1: absolute and change bands | 1e-9 |
| Sweep | `simulateSweep`, n = 16, seed 1, all 13 doses | 1e-9 |
| Influence | `computeInfluence()` at its defaults: effects and feeder lists | Effects 1e-9; infinities exact; lists exact except for parameters within 1e-9 of the 1 % threshold |
| Summary | `paramSummary()` | Exact |

Trajectories are recorded with `outEvery` chosen so that none exceeds 1,000 points (chosen, to keep the file reviewable in a diff). Thinning changes only what is recorded, not how the run is integrated, and the exact step count checks that. For every Monte Carlo draw the generator also records the margin to each rejection boundary: the urine-osmolality fraction h to 0 and 1, and baseline sodium to 135 and 145 mmol/L. It refuses to write a fixture in which any relative margin is below 1e-6 (chosen). An ulp-level difference at a knife-edge could flip one acceptance and shift every later sample, which would turn a rounding difference into an apparent model divergence.

### 4.3 Tolerance

For each key k and time t: |py_k(t) − js_k(t)| ≤ 1e-9 × max over t of |js_k(t)|. Decision 6 fixes 1e-9; the scale is chosen. Pointwise relative error is undefined at zero crossings and on keys that sit at zero, and V1 has both: free-water clearance crosses zero, every change band starts at zero, and the strain components are zero at baseline. Cost: where a key is small relative to its own peak, the check is looser than a pointwise 1e-9. A key that is identically zero in the reference must be identically zero in the port. Integers (step counts, rejected counts, lengths, PRNG outputs), strings (`MODEL_VERSION`, the disclaimer, scenario labels) and the positions of NaN must match exactly.

1e-9 is achievable. Both sides use IEEE-754 doubles in the same order of operations. What remains is the maths library: `exp`, `log` and `pow` may differ by an ulp between V8 and the platform libm. In a dissipative system such differences do not grow. Estimate: steps × machine epsilon ≈ 41,664 × 2.2e-16 ≈ 9e-12 on the longest scenario. That is consistent with the 2.75e-12 mass-balance round-off measured over the same run, and two orders inside 1e-9.

### 4.4 On divergence: stop the line

1. **Stop.** No merge that touches `src/health/engine/` or the reference engine until the divergence is resolved. Severity follows `05_BUG_TAXONOMY.md`; the line stops regardless.
2. **Localise** in fixture order: constants → initial state → `rhs()` at t = 0 → one step → step count → first diverging time and key. The checkpoints exist to make this mechanical.
3. **Decide which side is wrong** by a third computation, such as a hand calculation or an exact-arithmetic evaluation of the single diverging step. Never by majority, and never by which implementation is older.
4. **Fix the wrong side.** If it is the JavaScript reference, that is a model change: bump `MODEL_VERSION`, regenerate the fixture with `tools/health_golden.mjs`, record the change, then compare the port to the new fixture.
5. **Keep the case.** The diverging input joins the fixture as a permanent regression.

Never: loosen the tolerance; regenerate the fixture to match the port; edit the fixture by hand; mark the comparison skipped.

- **HREQ-V-08** `golden_v1.json` SHALL be produced only by `tools/health_golden.mjs` from the JavaScript reference, SHALL record the hashes of the files it was generated from, and SHALL NOT be edited by hand.
- **HREQ-V-09** The Python port SHALL match every golden floating-point value to 1e-9 of that key's peak magnitude, and every integer, string and NaN position exactly.
- **HREQ-V-10** A divergence SHALL stop merges to engine code until it is resolved by the §4.4 procedure. It SHALL NOT be resolved by changing the tolerance or the fixture to match the port.
- **HREQ-V-11** The fixture generator SHALL refuse to write Monte Carlo samples whose relative margin to any rejection boundary is below 1e-6.

---

## 5. Literature-Expectation Harness

### 5.1 The `evaluate_expectations` contract

`evaluate_expectations` (`src/health/engine/validate.py`) takes the expectation registry (in V1, the `validation.expects` entries of every scenario), the model version and the Monte Carlo settings (n ≥ 256, seed fixed in configuration). It returns one row per registered expectation plus a summary.

| Row field | Content |
|---|---|
| `id`, `scenario`, `metric` | Stable expectation id (`01` HREQ-E-13); where it applies; what is measured |
| `kind`, `role` | One of the seven kinds (`01` §7.2); `validation`, `calibration` or `structural` |
| `range`, `unit`, `evidence`, `registered` | The registered claim, and whether it was pre-registered or co-developed with V1 |
| `value` | The metric on the default-parameter run |
| `band`, `in_range_share`, `n`, `rejected`, `seed` | Per-sample q05, q50 and q95 of the metric; the share of accepted samples inside the range |
| `status` | `pass`, `fail` or `not_checked` |
| `counted`, `reason` | Whether it enters the validation totals; a reason whenever it is not_checked or not counted |
| `model_version`, `disclaimer` | As on every result (`01` HREQ-M-01) |

### 5.2 Rules

1. **One row per `expects` entry, never silently skipped.** The harness asserts that the number of rows equals the number of registered entries. V1: 24 rows.
2. **pass** if lo ≤ value ≤ hi. **fail** if outside, or if the value is non-finite, or the parameter set has no steady state. A model that cannot answer has failed, not abstained.
3. **not_checked** only for a kind that does not count (qualitative, design-target, known-divergence, unverified, numerical), or for a countable expectation whose extractor does not exist yet. Always with a reason. Missing extractors are listed by name above the summary, the way the NFT suite lists skips.
4. **counted** = kind ∈ {quantitative, semi-quantitative} and role = validation and status ∈ {pass, fail}.
5. **Calibration targets are evaluated but excluded.** They are run, because missing a calibration target later is a real regression, and reported in their own summary column with the calibrated parameters named in the reason. They never enter the validation pass count. Structural checks are handled the same way.
6. The summary reports counted passes, counted fails, not_checked, calibration and structural results, and splits counted results into pre-registered and co-developed. V1: 12 countable rows, of which 1 is calibration and 1 structural, so at most 10 are counted and all 10 are co-developed.

Planted registry fixtures prove each rule fires: a calibration-role expectation that passes (the validation pass count must not move); a quantitative expectation with no extractor (a not_checked row with a reason, row count unchanged); an extractor that returns NaN (fail); a qualitative expectation (not_checked, not counted); a duplicated id (the harness refuses the registry).

- **HREQ-V-12** `evaluate_expectations` SHALL return exactly one row per registered expectation and SHALL assert that count.
- **HREQ-V-13** Status SHALL be one of pass, fail and not_checked. A non-finite value or infeasible steady state SHALL be a fail. not_checked SHALL carry a reason and SHALL be used only for non-countable kinds or a missing extractor.
- **HREQ-V-14** A row SHALL be counted only if its kind is quantitative or semi-quantitative, its role is validation and its status is pass or fail. Calibration and structural rows SHALL be evaluated and reported separately.
- **HREQ-V-15** Every expectation status SHALL be published with its band, in-range share, n, seed, model version and registry version.

---

## 6. Knowledge-Base Gates

`src/health/kb/check.py` enforces every rule in `kb/schema.json` and the cross-file rules that JSON Schema cannot express (V1 enforced the latter in `tests/kb.test.mjs`, per the schema's own description).

| Rule | Statement | Origin |
|---|---|---|
| `schema` | Every record validates against `kb/schema.json`: id patterns; the six-grade enum; required fields on quantity, Entity, Relation and Evidence; entity type enum and scale 0–7; relation type enum; evidence kind, with `doi` or `pmid` required to match; https URLs; ISO dates; verification fields; per-database external-id patterns; no additional properties | `kb/schema.json` |
| `ref-resolves` | Every entity, relation endpoint, parent and evidence id cited anywhere (including `params.json`) resolves | Schema description |
| `parent-scale` | A parent's scale ≤ its child's scale (organism 0 … molecule 7) | Schema description |
| `engine-mirror` | A quantity with `engineParam` matches that `params.json` row in value, range, unit and grade | `quantity.engineParam` |
| `params-mirror` | `params.data.js` equals `params.json` | `params.data.js` header |
| `xid-verified` | Every external id has a `VERIFICATION_LOG.json` record for that entity, database and id with `resolved: true` | `01` HREQ-E-07 |
| `word-limits` | Summary ≤ 60 words; quote ≤ 25 words | Schema description |
| `conflict-unsourced` | A conflict with empty evidence says so in its note | `conflict` definition |
| `grade-ceiling` | No grade above the best `defaultGrade` of its evidence (V1: 0 violations) | `01` HREQ-E-03 |
| `range-contains-value` | lo ≤ value ≤ hi | `01` §4 |
| `unique-ids` | No id appears twice in a file | — |
| `range-kind`, `dispersion` | Structured range kind on every sampled parameter; dispersion type and n where a dispersion is used | `01` HREQ-E-08, E-09 |
| `calibration-link` | `calibrates` and `calibratedAgainst` agree on both sides | `01` HREQ-E-10 |
| `fixed-reason` | Every `mc: false` row carries one of the three permitted reasons | `01` HREQ-U-01 |
| `append-only` | Against the last released snapshot, no record is removed or changed except through a superseding record | `01` HREQ-E-06 |

V1 scale, for sizing the gate: 107 entities, 203 relations, 57 evidence records, 20 quantities (10 mirroring engine parameters, 7 carrying conflicts) and 154 verification records, all resolved.

**Every rule has a planted-violation test.** For each rule there is a minimal copy of the knowledge base with exactly one violation. The checker must report that rule and no other. A meta-test compares the checker's rule list with the planted fixtures and fails if either has an entry the other lacks. A rule with no planted violation has never been shown to fire, and counts as absent.

- **HREQ-V-16** `src/health/kb/check.py` SHALL enforce every rule in the §6 table and SHALL block a merge on any violation.
- **HREQ-V-17** Every knowledge-base rule SHALL have a planted-violation fixture that triggers that rule and no other, and a meta-test SHALL fail if any rule lacks one.
- **HREQ-V-18** The append-only check SHALL compare against the last released snapshot of the knowledge base and evidence ledger.

---

## 7. Sensitivity and Robustness

### 7.1 Influence screen

The screen runs per scenario, at each scenario's horizon, in both directions (`01` §8, HREQ-U-12). On every model change the difference in feeder lists between versions is part of the review. A parameter that newly feeds a displayed quantity is a claim that the rigor review must accept.

### 7.2 ±20 % perturbation

Each of the 42 sampled parameters is set to 0.8 and 1.2 times its value, **clipped to its stated range**, one at a time, on every scenario with countable expectations (V1: 4 scenarios, 336 runs). Clipping keeps the test inside the people the table claims exist: ±20 % on `adh_threshold` would be 56 mOsm/kg, far beyond its 278.0–285.5 range. Gate: for every countable expectation whose range excludes zero, the perturbed metric keeps the sign of the range. Changes of status are reported in a fragility table that names the parameter; they are a finding, not a gate failure. A perturbation that makes the steady state infeasible is also reported as a finding. For metrics that are positive by construction (times, fractions) the sign test is vacuous, and the fragility table carries the information.

### 7.3 Seed stability

Each scenario with countable expectations runs at n = 256 with seeds 1–5. Gate (chosen): for every countable metric, the spread of each band edge (q05, q50, q95) across seeds is ≤ 20 % of the median band width. Measured on the `drink_water_1L` sodium nadir: 14 % at n = 256, which passes, and 52 % at n = 64, which fails. That measurement is the basis for `01` HREQ-U-08. Statuses come from the default run and do not depend on the seed; in-range shares do, and are reported per seed.

### 7.4 Rejection share

The rejection share stays ≤ 5 % (`01` HREQ-U-06). Measured 0.4–1.2 % across seeds 1–5 at n = 256.

- **HREQ-V-19** The influence screen SHALL be rerun on every model change, and the difference in feeder lists SHALL be part of the review record.
- **HREQ-V-20** One-at-a-time perturbation of each sampled parameter by ±20 %, clipped to its range, SHALL NOT flip the sign of any countable expectation whose range excludes zero. Status changes and infeasible perturbations SHALL be reported.
- **HREQ-V-21** Across seeds 1–5 at n = 256, each band edge of every countable metric SHALL vary by no more than 20 % of the median band width.
- **HREQ-V-22** A Monte Carlo run rejecting more than 5 % of draws SHALL fail the robustness layer until the finding is resolved.

---

## 8. Independent Review

### 8.1 Rigor review

`rigor-lead` never reviews its own work. Author and reviewer are recorded on every change, and a change whose author and reviewer coincide fails the gate. The same applies down the chain: the curator who entered a number does not verify it, and the agent who registered an expectation does not approve its supersession. The rigor review checks what the gates cannot:

- calibration links are complete (a parameter tuned to a target with no `calibratedAgainst` is invisible to every gate);
- grades sit at or below their ceiling *and* are honest (the ceiling did not catch F-12);
- range kinds and dispersion types are as the source states them, checked against the full text where the record says it was;
- no new numeric constant entered engine code;
- the influence-list difference is explained;
- new expectations were committed before their first result (commit order);
- known divergences are recorded where the model is knowingly wrong.

### 8.2 Clinical-safety review

Every user-facing claim, string, chart and label gets a clinical-safety review before release, by a reviewer who did not write it.

| Check | Rule |
|---|---|
| Disclaimer | Present in the result object and visible on the view (`01` HREQ-M-01) |
| Thresholds | 135 mmol/L is labelled as a classification threshold (the exercise-associated hyponatremia consensus definition), not a physiological limit (`01` HREQ-M-14) |
| Indices | Labelled "index (model construct), not a clinical measure"; colour breakpoints labelled as display choices (`01` HREQ-M-08) |
| Trajectories | Long-horizon scenarios carry "modeled risk trajectory — not a prediction" |
| Bands | Shown, with n, labelled as parameter-range bands (`01` HREQ-U-07) |
| Framing | No imperatives, no "safe" amounts, no second person (`01` HREQ-M-02) |
| Direction | No direction claimed where the change band spans zero (`01` HREQ-M-15) |
| Divergences and grades | Known divergences shown on affected outputs; E count and ≥ B share visible |

An automated lint over user-facing strings supports this review and does not replace it. It flags imperatives, second-person physiology, "safe", and clinical terms applied to an index. The clinical-term check is scoped to index names and descriptions, because the V1 trajectory label legitimately contains "risk".

- **HREQ-V-23** No change SHALL be merged whose recorded reviewer is its author.
- **HREQ-V-24** Every user-facing claim SHALL pass the §8.2 clinical-safety checklist, reviewed by someone other than its author, before release to readers.

---

## 9. Fixture Datasets

Version-controlled and permanent. Tests read fixtures and never write them.

| Fixture | Purpose | Construction |
|---|---|---|
| `tests/fixtures/health/golden_v1.json` | JS ↔ Python equivalence (§4) | `tools/health_golden.mjs` from the reference |
| Planted KB violations, one file per rule | Each rule fires, alone (§6) | Hand-built minimal copies |
| Infeasible: `waterIn_base_Ld` = 1.3 L/day | `initialState()` must raise: required urine 1,229 mOsm/kg exceeds `U_osm_max` 1,200 | Hand-built; verified on the reference 2026-10-03 |
| Infeasible: `waterIn_base_Ld` = 0.5 L/day | Must raise: non-positive urine flow | As above |
| Non-normal: `adh_threshold` = 296 mOsm/kg | Feasible, baseline sodium 145.6 mmol/L: Monte Carlo must reject it | As above |
| Stiff corner (§3.5) | Numerical gates at a stiffness ratio of 16,000 | Parameter override plus `chronic_high_salt_30d` |
| Off-grid bolus and breakpoint trap (§3.3) | Intake exactness; must fail without breakpoints | 1 L over 7.3 min from 1.0037 h |
| Instability trap (§3.3) | The finite-value gate must fail | Step factor 3.5, default parameters |
| Expectation-registry traps (§5.2) | Each harness rule fires | Calibration pass, missing extractor, NaN extractor, qualitative row, duplicate id |

- **HREQ-V-25** Every fixture in the §9 table SHALL exist under version control, and tests SHALL NOT write to fixtures.

---

## 10. Definition of Done

Work is complete only when every applicable row passes.

### 10.1 Any code change
- [ ] Unit and property tests, including infeasible and boundary parameter sets
- [ ] Numerical gates (§3) and golden equivalence (§4) green
- [ ] Every `rhs()` or solver change rerun against the stiff corner
- [ ] Requirement id referenced; reviewer is not the author (HREQ-V-23)

### 10.2 A new parameter
- [ ] Value, unit, range, evidence, grade, range kind and dispersion type; `measure` where it comes from a study
- [ ] Grade at or below its ceiling and honest; E-assumptions say "ASSUMPTION" with reasoning
- [ ] `mc: false` only with one of the three reasons
- [ ] `calibratedAgainst` filled if it was tuned to anything
- [ ] Influence screen rerun; feeder-list difference explained; golden fixture regenerated with a `MODEL_VERSION` bump

### 10.3 A new scenario or expectation
- [ ] Expectation committed with kind, range, unit, evidence and role **before** its extractor first runs
- [ ] Range built from a dispersion of confirmed type
- [ ] Role is calibration if any parameter was chosen with it in mind
- [ ] Breakpoints listed for every input jump
- [ ] Harness row count updated; seed stability and ±20 % run

### 10.4 A new model state
- [ ] Unit, block label and documentation section (`01` HREQ-M-03, M-04)
- [ ] Analytic steady state extended; every invariant in §3.2 still holds or is restated
- [ ] Its time constant included in τ_min if it is fast
- [ ] Output-only states labelled (`01` HREQ-M-06)
- [ ] JavaScript reference and Python port changed together; golden regenerated

### 10.5 A new knowledge-base record
- [ ] Validates against the schema and every cross-file rule
- [ ] External ids verified in `VERIFICATION_LOG.json`
- [ ] Disagreeing sources in `conflicts`; corrections as superseding records
- [ ] A new rule, if any, ships with its planted violation

### 10.6 A new module
- [ ] Every step of `01` §10.2, recorded
- [ ] Independent countable expectations ≥ calibrated parameters (`01` HREQ-M-12)
- [ ] Stiff-case test at the module's time scales
- [ ] Clinical-safety review of every new user-facing claim
- [ ] Operator enablement decision recorded with the module's ≥ B share and E count

---

## 11. Test Environments

| Environment | Runs | Purpose |
|---|---|---|
| **Fixture** | `tests/health_selftest.py` with the Python port, golden fixture, planted KB fixtures and registry traps | Every commit. Fast and deterministic. Follows the NFT `@needs` and `--no-skips` rules (`docs/03` §4.6) |
| **Reference** | Node running the JavaScript reference; `tools/health_golden.mjs` | Fixture generation, and measurement of the reference itself. The Node version is recorded in the fixture header; the figures in this document were measured on v22.22.0 |
| **Browser** | `reference/metabolic-map-v1/index.html`, with the engine in a Web Worker (`reference/metabolic-map-v1/app/worker.js`) | What a reader sees. Worker trajectories are compared with the golden fixture at the same 1e-9, because each browser's maths library may differ from Node's. The checklist of §8.2 is verified on the rendered page |

---

## 12. Metrics on the Process

Computed from data, never typed, recorded on every release:

| Metric | V1 baseline | Meaning |
|---|---|---|
| Share of parameters graded ≥ B | 24 / 54 = 44.4 % | Evidence strength of the parameter table |
| E-assumption count, and its trend | 30 | Should fall release on release; a rise must be explained |
| Expectations: counted pass / counted fail / not_checked | 24 rows, at most 10 counted (12 countable kinds, less 1 calibration and 1 structural); harness not yet run on the port | The validation record, read with the next row |
| Calibrated parameters vs independent countable expectations | 4 calibrated (3 to the He 2013 band; `map_auto_tau_h` to the D-2 design target); 0 pre-registered independent expectations | Whether validation outnumbers tuning |
| Rejection share | 0.4–1.2 % at n = 256 | Whether the ranges describe possible people |
| JS/Python divergence incidents | None yet (no port) | Each is a stop-the-line event |
| Bugs by class and severity | Per `05_BUG_TAXONOMY.md` | Where errors come from |

**The one that matters most:** the number of **pre-registered, independent, countable expectations that pass**, set against the number of calibrated parameters. For V1 it is zero against four. Everything in this document exists to make that first number grow honestly. A model that hits only the targets it was tuned to hit has shown that its tuning works, and nothing about physiology.

---

## Appendix B. Requirements Minted in This Document

For import into `00_REQUIREMENTS.md`.

| ID | Requirement (short form) | § | Enforced by |
|---|---|---|---|
| HREQ-V-01 | Layers run bottom-up; a lower failure blocks publication above it | 2 | CI ordering in `tests/health_selftest.py` |
| HREQ-V-02 | Baseline drift < 1e-6 at default and every reporting sample | 3.6 | `tests/health_selftest.py` |
| HREQ-V-03 | Water, sodium, potassium and cell-solute invariants to 1e-9 | 3.6 | `tests/health_selftest.py` |
| HREQ-V-04 | Step bound, no straddled breakpoint, bolus exact to 1e-12; traps fail | 3.6 | Instrumented solver; trap fixtures |
| HREQ-V-05 | Step-halving change ≤ 1e-5 of peak (1e-3 for ADH, Thirst) | 3.6 | `tests/health_selftest.py` |
| HREQ-V-06 | Stiff corner passes every numerical gate | 3.6 | Stiff fixture |
| HREQ-V-07 | Non-finite trajectory fails and is never displayed | 3.6 | Engine; result schema |
| HREQ-V-08 | Golden fixture only from `tools/health_golden.mjs`, with source hashes, never hand-edited | 4.4 | Header hash check |
| HREQ-V-09 | Port matches golden to 1e-9 of peak; integers, strings and NaN positions exact | 4.4 | `tests/health_selftest.py` |
| HREQ-V-10 | Divergence stops engine merges; never resolved by changing tolerance or fixture | 4.4 | Merge gate |
| HREQ-V-11 | Fixture generator refuses samples within 1e-6 of a rejection boundary | 4.4 | `tools/health_golden.mjs` |
| HREQ-V-12 | One harness row per registered expectation, count asserted | 5.2 | `src/health/engine/validate.py` |
| HREQ-V-13 | Status pass/fail/not_checked; non-finite is fail; not_checked has a reason | 5.2 | `src/health/engine/validate.py` |
| HREQ-V-14 | Counted only if quantitative or semi-quantitative, role validation, status pass or fail | 5.2 | `src/health/engine/validate.py` |
| HREQ-V-15 | Status published with band, in-range share, n, seed and versions | 5.2 | `src/health/engine/validate.py` |
| HREQ-V-16 | KB checker enforces every §6 rule and blocks merge | 6 | `src/health/kb/check.py` |
| HREQ-V-17 | Every KB rule has a planted violation; meta-test enforces it | 6 | `tests/health_selftest.py` |
| HREQ-V-18 | Append-only check against the last released snapshot | 6 | `src/health/kb/check.py` |
| HREQ-V-19 | Influence screen rerun on every model change; difference reviewed | 7.4 | Review record |
| HREQ-V-20 | ±20 % (range-clipped) perturbation never flips a countable sign | 7.4 | Robustness run |
| HREQ-V-21 | Seed stability: band edges vary ≤ 20 % of median width at n = 256 | 7.4 | Robustness run |
| HREQ-V-22 | Rejection share > 5 % fails the robustness layer | 7.4 | Robustness run |
| HREQ-V-23 | No merge where reviewer is author | 8.2 | Commit metadata check |
| HREQ-V-24 | Clinical-safety checklist passed before release, by a non-author | 8.2 | Review record; string lint |
| HREQ-V-25 | Every §9 fixture under version control; tests never write fixtures | 9 | `tests/health_selftest.py` |
