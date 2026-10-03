# Methodology — Health Subsystem
## Mechanistic Modelling of Physiology with Graded Evidence

**Version:** 0.1 · **Date:** 2026-10-03 · **Status:** Draft for Operator review
**Companion to:** `00_REQUIREMENTS.md`, `02_AGENT_HIERARCHY.md`, `03_VALIDATION_AND_TESTING.md`, `04_ENVIRONMENTS_AND_UNDO.md`, `05_BUG_TAXONOMY.md`, `06_ARCHITECTURE.md` (all in `docs/health/`)
**Inherits discipline from:** `docs/01_METHODOLOGY.md` (NFT platform). The rules carry over; the content does not.

---

## 1. Purpose and the Adversarial Stance

The requirements document says *what* the health subsystem must do. This document says *how the modelling works*: what the first model represents, where each of its numbers comes from and how much weight each can bear, how uncertainty reaches the reader, and what a result must survive before it is shown.

### 1.1 Why it is written against itself

A mechanistic model fails differently from a statistical one. The NFT platform's default failure is a false positive found by scanning noise. This model's default failure is a **plausible curve**. An ODE with 13 states and 54 parameters produces smooth, textbook-shaped trajectories almost whatever its numbers are: plasma sodium dips after water, rises after salt, and recovers. The structure fixes the shape; the parameters fix the magnitudes and timings, and 30 of the 54 parameters in `reference/metabolic-map-v1/engine/params.json` are curator assumptions (grade E). A curve that looks right is weak evidence that its numbers are right, and the numbers are what a reader acts on.

Three facts set the stance:

1. **The reader may act on a number.** "Does 3 L of water in 30 minutes take plasma sodium below 135 mmol/L?" and "how much salt strains the kidney?" are the questions the V1 scenarios answer. A wrong answer delivered confidently is the worst outcome this subsystem can produce, and it produces no error message.
2. **The model was partly built to hit its own checks.** Three parameters were chosen so that `chronic_high_salt_30d` lands inside the He 2013 band it is then compared with (§6). Without bookkeeping, that comparison reads as validation.
3. **Much of the source literature describes small groups of young, healthy men** (Crowe 1987: "young water-replete men"; Baylis 1986: n = 8 men). A number that is right for them is not thereby right for the reference adult.

### 1.2 Scope, fixed

* **Educational mechanistic modelling of populations and mechanisms; never an individual clinical recommendation.** Every number describes the reference adult of decision D-3 (70 kg, 1.73 m², `REFERENCE_PERSON` in `engine/model.js`) or the population of parameter sets drawn from the ranges in §4.
* **The disclaimer travels in-band.** V1 puts `DISCLAIMER = 'Educational model — not medical advice.'` (`engine/index.js`) into `meta` of every deterministic run and onto every Monte Carlo and sweep result. That is the pattern kept here: the disclaimer is a field of the result, not a page footer that a new view can forget.

- **HREQ-M-01** Every result object the engine returns (deterministic run, Monte Carlo, sweep, influence screen, expectation row) SHALL carry the disclaimer string and `MODEL_VERSION` as fields. A result without them is a defect of at least S1.
- **HREQ-M-02** No output SHALL be framed as a recommendation to an individual: no imperatives, no "safe for you" amounts, no second-person physiology ("your kidney"). Outputs describe the reference adult or the parameter-range population.

**Severity follows consequence.** Severity (`05_BUG_TAXONOMY.md`) tracks the consequence to a reader who might act on a number, not how loudly a failure announces itself. A crash in the salt-dose sweep is visible and harmless. A band silently missing from the same chart, so that a median looks like a fact, is S1. A colour scale that turns red at 0.66 on an index with no clinical thresholds is not cosmetic if a reader takes red to mean damage (§9).

### 1.3 What is inherited from the NFT discipline

Requirement ids and SHALL statements (HREQ-M/E/U here, HREQ-V in `03`); every number with a source and a grade (§3); registration before results, applied to expectations rather than trading hypotheses (§7); append-only registers, applied to the knowledge base and evidence ledger (§3.3); independent review, with `rigor-lead` never reviewing its own work (`03` §8); every non-physical constant in configuration, not code (§4.7); and no point estimate without its uncertainty, here a Monte Carlo band (§5).

---

## 2. What the First Slice Models

### 2.1 The loop in one paragraph

V1 is the cardio-renal-fluid loop of one reference adult on a continuous average diet: 2.1 L/day water, 150 mmol/day sodium, 80 mmol/day potassium (`params.json`). Water and salt are absorbed from the gut into the extracellular fluid (ECF). Water moves between the ECF and the cells to equalise osmolality. Plasma osmolality sets antidiuretic hormone (ADH, vasopressin), which sets how concentrated the urine is. ECF volume sets aldosterone, atrial natriuretic peptide (ANP) and mean arterial pressure (MAP). The kidney reads all of them and sets how much sodium and water leave. Every block in `engine/model.js` is labelled M0–M10 so the code can be audited against this section.

### 2.2 State, units and conventions

Units (`model.js` header): time h; volume L; amount mmol (mOsm for osmoles); concentration mmol/L; pressure mmHg; osmolality mOsm/kg, with 1 kg of water taken as 1 L; ADH pg/mL.

| # | State | Unit | Meaning | Block |
|---|---|---|---|---|
| 0 | `V_ecf` | L | Extracellular fluid volume | M1 |
| 1 | `V_icf` | L | Intracellular fluid volume | M1, M3 |
| 2 | `Na_ecf` | mmol | Total ECF sodium | M1 |
| 3 | `K_icf` | mmol | Total cell potassium | M1 |
| 4 | `osm_icf_solute` | mOsm | Osmotically active cell solute | M1 |
| 5 | `ADH` | pg/mL | Plasma vasopressin | M4 |
| 6 | `V_gut_water` | L | Unabsorbed water in the gut | M2 |
| 7 | `Na_gut` | mmol | Unabsorbed sodium in the gut | M2 |
| 8 | `Thirst` | 0–1 | Thirst drive (output only in V1) | M5 |
| 9 | `Aldo` | relative, 1 = baseline | Aldosterone activity | M6 |
| 10 | `ANP` | relative, 1 = baseline | Atrial natriuretic peptide | M6 |
| 11 | `MAP` | mmHg | Mean arterial pressure | M6 |
| 12 | `R_auto` | relative, 1 = baseline | Slow whole-body autoregulation (decision D-2) | M6 |

Alongside the 13 states the solver integrates a six-entry flux ledger (`water_in`, `water_out`, `na_in`, `na_out`, `k_in`, `k_out`) used only for mass-balance audits. `derived()` returns 21 non-integrated quantities: plasma sodium and osmolality, GFR (absolute and per 1.73 m²), filtered load, FE_Na, urine flow and osmolality, free-water clearance, and the five strain-index fields.

- **HREQ-M-03** Every parameter, state, derived quantity and ledger entry SHALL declare its unit, and every unit conversion in engine code SHALL go through a named constant or function whose basis is stated beside it.

### 2.3 Block by block

- **M0 — Analytic steady state.** The baseline is solved in closed form, not by running the model until it settles. Urine flow must equal net water intake. Urine solute must equal solute intake, and their ratio is the urine osmolality. Inverting the ADH-to-urine-osmolality curve gives baseline ADH; inverting the osmolality-to-ADH line gives baseline osmolality and so plasma sodium. The baseline fractional sodium excretion FE0 is whatever makes excretion equal intake. At default parameters this gives plasma sodium 138.1 mmol/L, osmolality 286.2 mOsm/kg, ADH 1.83 pg/mL, urine 1.5 L/day at 573 mOsm/kg and FE_Na 0.60 % (computed by `constants()` from `params.json`, reproduced 2026-10-03). The construction has a cost and a guard. A parameter set can demand a urine osmolality outside the kidney's range; then no steady state exists and `initialState()` throws rather than return a state. `osm_icf_solute − 2·K_icf` is an exact invariant of the model, which is why the steady state is unique only once `V_icf_0` is fixed.
- **M1 — Balances.** Water: gut absorption plus metabolic water, minus the shift into cells, urine, and insensible, faecal and sweat losses. Sodium: absorption minus urine and sweat sodium. Potassium is absorbed straight into cells and counted with its anion as 2 mOsm (Edelman relation).
- **M2 — Gut.** First-order absorption. Water half-time 0.2 h (12 min; D2O tracer study, A-primary). Sodium half-time 0.5 h (E: no verified human value for oral NaCl taken with food).
- **M3 — Water shift.** A linear flux driven by the ECF–ICF osmolality difference. Its conductance is derived from an equilibration time constant of 0.083 h (5 min, E).
- **M4 — ADH.** The secretion target is a straight line above an osmotic threshold: 0.35 pg/mL per mOsm/kg above 281.0 mOsm/kg (Robertson & Athar 1976, A-primary). The threshold shifts by 0.3 mOsm/kg per 1 % change in ECF volume (magnitude E). Plasma ADH approaches the target with a 0.4 h half-life (24.1 min, Baumann & Dingman 1976).
- **M5 — Thirst.** Osmotic threshold 285.2 mOsm/kg and slope 0.054 per mOsm/kg (systematic review, A-meta), plus a hypovolemic term (E). **Thirst is an output in V1: it does not change how much is drunk.** Shown next to intake, it invites the reader to assume a feedback the model does not have.
- **M6 — Volume-driven hormones and pressure.** Aldosterone, ANP and MAP targets are power laws of relative ECF volume `vr = V_ecf / V_ecf_0`: aldosterone `vr^−10`, ANP `vr^4`, MAP `MAP_0·vr^0.9` at steady state (all three exponents E). Since decision D-2, 70 % of the MAP–volume elasticity is delivered slowly through `R_auto` with a 240 h (10-day) time constant, Guyton's whole-body autoregulation, so a chronic salt load raises pressure over weeks rather than hours. Neither number was taken from a paper; both were chosen to give that shape (§6).
- **M7 — Kidney.** GFR = 125 mL/min × (MAP/93)^0.3 × vr^0.5. Filtered sodium = GFR × plasma sodium. Fractional excretion is FE0 times four factors: aldosterone (retains), ANP (natriuretic), pressure natriuresis `exp(0.05·ΔMAP)` and osmotic natriuresis `exp(0.061·Δ[Na])`, the last derived from Andersen 2002 (A-primary). Urine osmolality is a Hill curve of ADH between 50 and 1,200 mOsm/kg; urine flow is urine solute divided by urine osmolality.
- **M8 — Inputs and non-renal losses.** Piecewise-constant intake (drinks, salt and the baseline diet); metabolic water 0.2 L/day; insensible loss 0.7 L/day; faecal water 0.1 L/day; optional sweat at 40 mmol/L sodium, which no V1 scenario uses.
- **M9 — unlabelled.** `model.js` has no block labelled M9, and `engine/README.md`, which `model.js` names as its line-by-line companion, is not in `reference/`. The derived quantities and the flux ledger sit where M9 would be; this document treats them as M9 until the port labels them.
- **M10 — Kidney strain index.** A dimensionless 0–1 summary of four renal workloads above baseline (§9). It is an index, not a clinical measure.

**Two loops, in words.** Drinking 1 L in 10 min: the water is absorbed within about half an hour. The ECF dilutes and water moves into cells; osmolality falls below the ADH threshold; ADH decays with its 24-minute half-life; the urine dilutes toward 50 mOsm/kg and its flow rises; the load clears over hours. Six quantitative ranges are registered against this scenario (`scenarios.js`, `drink_water_1L`).

Eating 10 g of salt: 171 mmol of sodium is absorbed more slowly. Osmolality rises, ADH rises and the urine concentrates; water is drawn from cells into the ECF; the expanded ECF lowers aldosterone and raises ANP and MAP; fractional excretion rises and most of the load leaves over 24–48 h. Held for 30 days at 15 g/day (`chronic_high_salt_30d`), `R_auto` turns the volume rise into a pressure rise over weeks.

### 2.4 What V1 does not model

Each row is a place where a reader could over-read an output, and a known direction in which a result can be wrong.

| Not modelled | Consequence | Recorded at |
|---|---|---|
| Sodium storage outside ECF water | Model expands ECF on chronic salt; Heer 2000 found no gain in total body water | `chronic_high_salt_30d`, kind `known-divergence` |
| Thirst driving intake | Thirst rises; nobody drinks more | `STATE_KEYS` comment |
| Glucose and urea dynamics | Held at 90 and 14 mg/dL | `glucose_mgdl`, `bun_mgdl` |
| Body size | One 70 kg, 1.73 m² adult (D-3) | `REFERENCE_PERSON` |
| Steep ADH response to > 10 % hypovolemia | Linear threshold shift only | `adh_vol_shift` notes |
| Multi-day rhythms of sodium excretion | Smooth excretion curve | `salt_load_10g` expectation note |
| Exercise | Input accepted, unused | `baselineInputs()` |

- **HREQ-M-04** Every equation block in engine code SHALL carry a block label that matches a section of this document or of the engine documentation. An unlabelled block, or a label with no matching section, is a defect.
- **HREQ-M-05** The reference steady state SHALL be computed analytically, and the function that builds the initial state SHALL raise on a parameter set that has no feasible steady state rather than return a state.
- **HREQ-M-06** A state that does not feed back in the current model version (V1: `Thirst`) SHALL be labelled "output only, no feedback" wherever it is displayed.

---

## 3. The Evidence Grade System

### 3.1 The six grades

| Grade | Means | May be used for | Displayed as |
|---|---|---|---|
| `A-meta` | Pooled estimate from a meta-analysis or systematic review | Parameter value; expectation range; independent validation source | Grade beside the value; counts toward the ≥ B share |
| `A-primary` | A number reported by a primary human study, read at the level of that number | As A-meta | As A-meta |
| `B-textbook` | Textbook, narrative review or guideline: secondary, often rounded | Parameter value; expectation range | As A-meta |
| `C-model` | A parameter or output of another published model | Structure and starting values. **Never** a validation source: a model agreeing with a model says nothing about people | Grade beside the value; outside the ≥ B share |
| `D-animal` | Animal or in-vitro study | Direction and mechanism. Magnitude only through an explicit E-graded scaling. Never validation of a human quantity | As C-model |
| `E-assumption` | Curator assumption with stated reasoning | Whatever closes the model. Never validation | Grade visibly marked; counted in the E total shown on every view |

**The grade belongs to the value, not to the range.** `gut_water_thalf_h` is A-primary because 0.2 h is the D2O study's half-life; its bounds were widened by assumption to cover drink volume and temperature. That is why the range carries its own kind (§4.2).

The headline metric is the share of parameters graded ≥ B. V1: 24 of 54 (44.4 %): A-meta 3, A-primary 6, B-textbook 15, C-model 0, D-animal 0, E-assumption 30 (`paramSummary()` in `engine/index.js`).

### 3.2 Default grade per source type

Each evidence record carries `sourceType`, assigned from its PubMed publication type and abstract, and `defaultGrade`, the grade a number taken directly from that source receives (`kb/schema.json`). The mapping as observed in the 57 records of `kb/evidence.json`:

| `sourceType` | `defaultGrade` | V1 records |
|---|---|---|
| meta-analysis; systematic-review | A-meta | 1; 1 |
| primary-human; rct | A-primary | 19; 0 (rct mapped by analogy with primary-human; no V1 record) |
| review; textbook; guideline | B-textbook | 22; 1; 1 |
| model | C-model | 5 |
| primary-animal; primary-in-vitro | D-animal | 6; 1 |

The default grade is a ceiling, not an entitlement. A number may be graded below its source (a value read off a figure, a derived quantity), never above it. V1 passes that ceiling with zero violations across its 54 parameters (checked 2026-10-03). The ceiling is necessary but not sufficient: `naIn_base_mmold` is graded A-meta from He 2013, but 150 mmol/day is a scenario condition and He 2013's 9–12 g/day is a policy statement in its conclusions, and the record itself says B would be more honest (audit F-12).

### 3.3 Conflicts are kept, not resolved away

When sources disagree, the adopted value carries the others in `conflicts` (claim, value, unit, evidence, note; `kb/schema.json`). Basal AVP is adopted as 1.4 pg/mL (Baylis 1986, immunoassay). Two conflicts are kept beside it: the same study's bioassay at 1.04 pg/mL, and the Robertson regression's 2.1 pg/mL at 287 mOsm/kg. Seven of the 20 quantities in the V1 knowledge base carry conflicts. A conflict with no evidence is allowed only for an unsourced claim kept for traceability, and its note must say so. Cost: the reader sees disagreement instead of a clean number, and the model still has to pick one. That is the actual state of the literature. Hiding it would make the adopted value look stronger than it is.

**Append-only.** The knowledge base and the evidence ledger are append-only. A correction is a new record that names the record it supersedes and says why. The old record stays, marked superseded; if a source caused the disagreement, that source stays in `conflicts`. Cost: the files grow, and a reader of raw JSON must follow supersession chains. Benefit: every number ever shown can be traced to the record that was current when it was shown.

- **HREQ-E-01** Every parameter and every knowledge-base quantity SHALL carry a value, a unit, a range, at least one resolvable evidence id and a grade from the six-grade enum.
- **HREQ-E-02** Every evidence record SHALL carry `sourceType` and `defaultGrade`, and `defaultGrade` SHALL follow the §3.2 mapping.
- **HREQ-E-03** No number's grade SHALL exceed the best `defaultGrade` among the evidence it cites.
- **HREQ-E-04** The E-assumption count and the share of parameters graded ≥ B SHALL be computed from the parameter table when an output is rendered, never typed into prose or code, and SHALL be displayed with every output view.
- **HREQ-E-05** A disagreeing source SHALL be kept in `conflicts` beside the adopted value. A conflict without evidence SHALL state in its note that it is unsourced.
- **HREQ-E-06** The knowledge base and the evidence ledger SHALL be append-only. A correction SHALL be a new record naming the record it supersedes and the reason; no record is deleted or edited in place.
- **HREQ-E-07** An external identifier (UBERON, CL, GO, KEGG, Reactome, UniProt, ChEBI, HGNC) SHALL appear on an entity only if `VERIFICATION_LOG.json` holds a record for that entity, database and identifier with `resolved: true`. V1: 154 of 154 records resolved.

---

## 4. Parameter Discipline

### 4.1 The record

Every row of `params.json` has `value`, `unit`, `range [lo, hi]`, `description`, `evidence`, `grade` and `notes`, and optionally `mc: false`. Knowledge-base quantities add `measure` (what was measured, in whom, how) and `engineParam` (the `params.json` row they mirror). Each field answers a reviewer's question: what number, in what unit, how wrong it could be, who says so, how much weight it bears, in whom it was measured, and what was done to it on the way in. `na_osm_gain` shows the last one. Its value 0.061 per mmol/L is derived from Andersen 2002 (sodium excretion 291 vs 199 µmol/min at plasma sodium +4.2 vs −2.0 mmol/L, so ln(291/199)/6.2); its bounds, half and 1.5 times the value, are assumptions, and the notes say so.

### 4.2 Range kinds

Monte Carlo treats each range as "the plausible span", but ranges are built in different ways and mean different things:

| Kind | Built from | Represents | V1 example |
|---|---|---|---|
| `reported-ci` | A confidence interval stated by the source | Uncertainty about a population mean | None among parameters; the He 2013 expectation band is CI-derived |
| `mean-2sem` | Mean ± 2 × standard error, as reported | Uncertainty about a group mean, not the spread of people | `thirst_slope` 0.054 ± 2 × 0.007 |
| `curator-assumption` | The curator's judgement around a point value | The curator's ignorance | `GFR_0`, `MAP_0`, `V_icf_0`, `map_auto_tau_h` |

V1 records a range kind for only 11 of its 54 parameters, in free-text notes (audit F-10). Two V1 constructions fit none of the three kinds: a span across conflicting study values (`adh_threshold` 278.0–285.5 mOsm/kg, from 277.8, 281.0 and 285) and a textbook reference interval, which is a spread of individuals (`bun_mgdl` 8–20 mg/dL). Until the Operator decides whether to add kinds, both are recorded as `curator-assumption` with the construction stated in the notes. A range whose two ends have different bases takes the weaker kind: `anp_thalf_h` has a reported lower end and point value and an assumed upper end, so it is `curator-assumption`.

### 4.3 Dispersion type: SD, SEM or CI

Mean ± 2 SEM and mean ± 2 SD differ by a factor of √n. A band built from an SEM where an SD was meant is too narrow by √n; the reverse is too wide by the same factor. Sources often print "± x" without saying which. The V1 example is Suckling 2012: +3.13 ± 0.75 mmol/L after 6 g of salt, with SEM versus SD unconfirmed from the full text. V1 therefore records it as `unverified` and does not let it set a band (audit F-02/F-03, §11).

### 4.4 Parameters held fixed

Twelve of the 54 parameters are `mc: false` and held at their value. There are three permitted reasons:

| Reason | V1 parameters | Why not sampled |
|---|---|---|
| Scenario condition | `waterIn_base_Ld`, `naIn_base_mmold`, `kIn_base_mmold` | They define the experiment, not the person. Sampling them would blur "what happens at 150 mmol/day" into "what happens at some intake" |
| Classification threshold | `na_normal_low`, `na_normal_high` | 135 and 145 mmol/L are definitions, used to label hyponatremia and to reject baselines, not physiology |
| Index-definition constant | the four strain weights and three strain scales | They define what the index means; sampling them would make it a different quantity in every sample |

Cost: holding the index constants fixed removes definitional uncertainty from the strain-index band, so the band is narrower than the honest uncertainty about "strain" (§9). The influence screen still includes them (§8), so the reader can see that they feed the index.

### 4.5 Sampling rule

Uniform on [lo, hi]; log-uniform when lo > 0 and hi/lo > 5 (`samplingMode()` in `engine/mc.js`). V1 samples 42 parameters: 35 uniform and 7 log-uniform (`gut_na_thalf_h`, `osm_eq_tau_h`, `adh_vol_shift`, `gfr_map_exp`, `map_tau_h`, `aldo_tau_h`, `anp_vol_exp`). A range spanning more than five-fold is a statement about order of magnitude. Sampled uniformly, `osm_eq_tau_h` on [0.03, 0.25] h would put 74 % of draws above its geometric midpoint of 0.087 h. Cost: the threshold of 5 is a V1 convention with no source, so a range at 4.9-fold is sampled uniformly and one at 5.1-fold log-uniformly. Ranges with lo = 0 (`gfr_vol_exp`, `thirst_vol_gain`, `anp_effect_exp`) are uniform whatever their width.

### 4.6 The independence assumption and its cost

Every sampled parameter is drawn independently (`mc.js`: "no correlation data in V1"). In people, linked parameters move together. `V_ecf_0`, `V_icf_0` and `GFR_0` all scale with body size, and `adh_threshold` and `thirst_threshold` are reported for the same subjects. Independent draws produce combinations no person has, such as a 12 L ECF with a 32 L ICF. The effect on a band can go either way: correlated inputs whose effects add would widen the true band, and offsetting ones would narrow it. V1 cannot say which, so the assumption is stated with every output until correlation evidence enters the ledger.

### 4.7 Constants in code

Model code may contain physical constants and unit conversions only, each named with its basis: 58.44 g/mol for NaCl (`MMOL_NA_PER_G_NACL`, `scenarios.js`), glucose/18 and BUN/2.8 for mg/dL to mOsm/kg, ×60/1000 for mL/min to L/h, ×100 for per cent. Everything else is a parameter with a row. V1 has one known violation. The glomerular component of the strain index averages two terms with weights 0.5 and 0.5, and divides the relative GFR rise by 0.1 (`model.js` lines 335–336). None of the three numbers is in `params.json`, although the block comment says the index's weights and scales are.

- **HREQ-E-08** Every sampled parameter SHALL record its range kind as a structured field (`reported-ci`, `mean-2sem` or `curator-assumption`). A range whose ends have different bases SHALL take the weaker kind.
- **HREQ-E-09** A dispersion taken from a source SHALL be recorded with its type (SD, SEM, CI, IQR or range) and n as the source states them. A dispersion whose type has not been confirmed from the full text SHALL NOT be used to build a parameter range or an expectation band.
- **HREQ-U-01** A parameter SHALL be held fixed in Monte Carlo only as a scenario condition, a classification threshold or an index-definition constant, and the reason SHALL be recorded as a field.
- **HREQ-U-02** The sampling mode SHALL be derived from the range by the §4.5 rule and never set by hand.
- **HREQ-U-03** Every Monte Carlo result SHALL state in its metadata that parameters are sampled independently, until correlation evidence is entered in the ledger and used.
- **HREQ-M-07** Engine code SHALL contain no numeric constant other than a named physical constant or unit conversion. Every other number SHALL be a parameter row with value, range, evidence and grade.

---

## 5. Uncertainty

### 5.1 Design

`simulateMC()` (`engine/index.js`) draws n accepted parameter sets (V1 default 64, seed 1), simulates each, and reports per-time-point quantiles q05, q50 and q95 (R type 7) of every quantity. It reports two families: absolute values, and the change from each sample's own value at t = 0. The PRNG is mulberry32, a 32-bit generator that is deterministic across Node and browsers. Draws follow table key order, and fixed parameters consume no draw. Cost: adding or reordering a *sampled* parameter changes every sample for a given seed, so results from before and after such a change cannot be compared draw by draw. The golden fixture's staleness check (`03` §4) catches such a change. The same model version, parameter table, seed and n give bit-identical samples and bands in the JavaScript reference, and the Python port matches them to 1e-9 (`03` §4). A seed is fixed in configuration before a run and never chosen after its results are seen.

### 5.2 Rejection rules

A draw is rejected and counted when its analytic steady state is infeasible (M0), or when its baseline plasma sodium falls outside [`na_normal_low`, `na_normal_high`] = [135, 145] mmol/L. Such a draw describes no healthy adult. After 50·n draws without n acceptances the run fails rather than returning a smaller sample. Measured 2026-10-03: seed 1 at n = 64 rejected 0 draws; seeds 1–5 at n = 256 rejected 1, 3, 2, 1 and 1 (0.4–1.2 %). Cost: the accepted distribution is not the stated ranges. Rejection reshapes them toward combinations that produce a normal plasma sodium. At about 1 % the reshaping is small. At a high rejection share, the ranges or the structure describe people who cannot exist, and the band is a band over something other than what the table says.

### 5.3 What n is enough

A sample 5th percentile estimates the true 5th percentile with a standard error, in probability, of √(0.05 × 0.95 / n). At n = 64 that is 0.027: the reported "5th percentile" lies somewhere between the 2.3rd and 7.7th at ±1 SE. At n = 256 it is 0.014 (3.6th–6.4th); at n = 1024, 0.007. Measured on the plasma-sodium nadir of `drink_water_1L` across seeds 1–5 (2026-10-03): at n = 64 the lower band edge moved by 0.83 mmol/L, half of the median band width of 1.60 mmol/L; at n = 256 it moved by 0.23 mmol/L, 14 % of the median width of 1.67 mmol/L. The V1 default of 64 is a speed default for the interactive viewer. Any band used to decide an expectation status or to support a written claim uses n ≥ 256 (chosen: on the measured case this brings seed-to-seed movement of the band edge under the 20 % gate of `03` §7). Cost: four times the computation. The 30-day scenario takes 159 ms per run in Node (measured), so about 41 s at n = 256.

### 5.4 What the band is, and what it is not

The band mixes three things: uncertainty about group means (`mean-2sem` ranges), spread between people (reference intervals, the inter-individual range on `V_ecf_0`), and curator ignorance (`curator-assumption` ranges, 30 E-graded parameters). It is therefore neither a confidence interval, nor a prediction interval for a person, nor the range seen across people. It is the 5th–95th percentile of model output over N accepted parameter sets, and it is labelled that way. It is also **pointwise**. The median curve is not the trajectory of any sample, and the peak of the median is not the median of the peaks. Summary metrics (peak, nadir, time to peak, amount excreted by a time) are computed per sample and then summarised, as `saltLoadMetrics()` does for the dose sweep. A change from baseline is computed per sample before quantiles are taken, never as a difference of two quantiles.

**Sweeps use common random numbers.** `simulateSweep()` runs the same accepted samples at every salt dose, so differences between doses come from the dose and not from resampling. Cost: the whole curve shares one sample set's luck, and an unlucky set shifts all doses together. The seed-stability gate (`03` §7) is the check on that.

- **HREQ-U-04** Every number shown to a reader SHALL carry a Monte Carlo band (q05, q50, q95) together with the accepted n, the rejected count and the seed. A band missing from an output is an S1 defect.
- **HREQ-U-05** Monte Carlo SHALL use the seeded mulberry32 generator, drawing in parameter-table order, so that the same model version, table, seed and n give bit-identical samples. Seeds SHALL be fixed in configuration before a run.
- **HREQ-U-06** A draw with an infeasible steady state, or with baseline plasma sodium outside [`na_normal_low`, `na_normal_high`], SHALL be rejected and counted, and the count SHALL accompany every band. A rejection share above 5 % (chosen: above it, the accepted sample is materially not the stated ranges) SHALL be raised as a finding.
- **HREQ-U-07** Bands SHALL be labelled as parameter-range bands ("5th–95th percentile of N accepted parameter sets") and SHALL NOT be labelled as confidence intervals, prediction intervals or ranges across people.
- **HREQ-U-08** A band used to compute an expectation status or to support a written claim SHALL use n ≥ 256. A band at a smaller n SHALL display its n.
- **HREQ-U-09** Summary metrics and changes from baseline SHALL be computed per sample and then summarised, never read off a quantile curve or formed as a difference of quantiles.
- **HREQ-U-10** A sweep across an input SHALL use the same accepted samples at every input value.

---

## 6. Validation Versus Calibration

### 6.1 Definitions

| Term | Meaning | Counts as validation? |
|---|---|---|
| **Calibration** | Choosing a parameter value so that model output matches a target | Never |
| **Validation** | Comparing output with a target that played no part in choosing any parameter, registered before the output was examined | Yes, if its kind is quantitative or semi-quantitative |
| **Design target** | A property chosen as a requirement of the model and met by construction | Never |
| **Structural check** | A property any parameter set satisfies once the model reaches steady state | Never |

A target used to choose a parameter is spent. It stays in the registry and keeps being evaluated, because missing it later is a real regression, but its role is `calibration` for good.

### 6.2 The worked example: `chronic_high_salt_30d`

Salt rises from 8.8 to 15 g/day at t = 24 h and is held for 30 days. The scenario carries four expectations (`scenarios.js`):

| Expectation | Kind | Role | Why |
|---|---|---|---|
| ΔMAP at day 30 per +100 mmol/day sodium: 0.7–3.2 mmHg (He 2013 normotensive band) | quantitative | **calibration** | `map_vol_exp`, `pn_gain` and `aldo_vol_exp` were tuned jointly so the model lands in this band |
| MAP still rising at day 14; ≥ 95 % of plateau by day 30; < 60 % by day 3 | design-target | design target | `map_auto_tau_h` was chosen for this shape (D-2; its record says "calibration, not independent validation") |
| Sodium excretion within 2 % of intake by day 30 | quantitative | structural | Holds for any parameter set that reaches balance within 30 days |
| ECF expands (model) versus no gain in total body water (Heer 2000) | known-divergence | — | No sodium storage compartment in V1 |

On the scenario a reader is most likely to act on, chronic salt and blood pressure, **none of the four is independent validation**. The band is itself approximate: SBP and DBP confidence-interval end-points were combined into a MAP band via MAP ≈ DBP + (SBP − DBP)/3 and scaled from 75 to 100 mmol/day, which is not a true MAP interval (audit F-06). Three free parameters tuned against one band are not identified, because many combinations hit it. Monte Carlo samples each of the three over its own range, so the band shows how far the output moves when they are not at their tuned values; a band that covers the calibration target is not evidence of correctness.

Independent validation of the MAP–salt slope would need a source not used in tuning, registered before the run. It must not share trials with He 2013: a meta-analysis pools trials, and a second analysis drawing on the same trials is the same evidence counted twice.

### 6.3 The record format

V1 states calibration only in free-text notes. The port makes it structural, on both sides:

```json
{ "id": "chronic_high_salt_30d:0", "metric": "dMAP at day 30 per +100 mmol/day Na", "unit": "mmHg",
  "kind": "quantitative", "range": [0.7, 3.2], "evidence": ["ev:he-2013"], "role": "calibration",
  "calibrates": ["map_vol_exp", "pn_gain", "aldo_vol_exp"],
  "registered": "V1: co-developed with the model; pre-registration not demonstrable" }
```

and each named parameter carries `"calibratedAgainst": ["chronic_high_salt_30d:0"]`. The knowledge-base gates (`03` §6) check that the two sides agree.

- **HREQ-E-10** A parameter whose value was chosen to make an output match a target SHALL list that expectation in `calibratedAgainst`, and the expectation SHALL list the parameter in `calibrates`. The two lists SHALL agree.
- **HREQ-E-11** An expectation that any parameter was calibrated against SHALL carry role `calibration` and SHALL NOT be counted as a validation pass, whatever its outcome.
- **HREQ-E-12** Design targets and structural checks SHALL be recorded with those roles and SHALL NOT be counted as validation passes.

---

## 7. Expectation Registry Protocol

### 7.1 Pre-registration

An expectation is registered when it is committed to the registry with its kind, range, unit, evidence and role, before the metric extractor that reads it is first run against the model. The proof is commit order: the expectation's commit precedes the first commit that produces a result for it. V1's 24 expectations were written alongside the model, so their pre-registration cannot be shown. They are carried over marked as such and reported separately from expectations registered afterwards, because a pass on a co-developed expectation is weaker evidence than a pass on a registered one.

### 7.2 Kinds

V1 holds 24 expectations across 7 scenarios: 11 quantitative, 1 semi-quantitative, 8 qualitative, 1 design-target, 1 known-divergence, 1 unverified and 1 numerical (`scenarios.js`).

| Kind | States | Status in the harness | Counts? |
|---|---|---|---|
| `quantitative` | A numeric range from a cited source | pass / fail | Yes, unless its role is calibration or structural |
| `semi-quantitative` | A numeric range whose source values are not fully verified (`no_water_24h` osmolality rise 2–15 mOsm/kg) | pass / fail | Yes, same exclusions |
| `qualitative` | A direction ("ADH rises") | not_checked; the direction is asserted by a software test | No |
| `design-target` | A shape chosen as a requirement | not_checked; asserted by a software test | No |
| `known-divergence` | Where the model is knowingly wrong | not_checked, reason = the divergence | No |
| `unverified` | A source value whose basis is unconfirmed | not_checked, reason = what is unverified | No |
| `numerical` | A solver property (V1 `baseline`: drift < 1e-6) | not_checked; evaluated by the numerical gates (`03` §3) | No: it is not a literature claim |

Of V1's 12 countable expectations, one is calibration and one structural (§6.2). At most 10 can count as validation, and none of those 10 was demonstrably pre-registered.

### 7.3 How a status is computed

The metric is computed on the default-parameter run. It is a **pass** if lo ≤ value ≤ hi, and a **fail** if it lies outside the range, or if the model produced no finite value or no feasible steady state: a model that cannot answer has failed, not abstained. **not_checked** is used only for non-countable kinds, or for a countable expectation whose extractor does not yet exist, and always carries a reason. The row also carries the Monte Carlo band of the same metric (per-sample, n ≥ 256), the share of accepted samples inside the range, and the seed. The status is deterministic and reproducible across both implementations. The band beside it shows whether a pass is robust: a pass with an in-range share of 0.3 means that most plausible parameter sets fail it, and the row says so.

**Recording a known divergence.** A known divergence names the mechanism the model lacks, the sources on each side, and the outputs it affects. In V1, `chronic_high_salt_30d` cites `ev:heer-2000` and `ev:wiig-2018` for no total-body-water gain at very high intake, against the model's Guyton-style ECF expansion with no sodium storage compartment. The divergence is displayed with every output it affects. When the missing mechanism is added, the divergence is re-evaluated, and if it is gone the record is superseded, not deleted.

### 7.4 Changing an expectation

An expectation is changed only by superseding it. The new record names the old one and gives the reason, the author, the date, and the model's value for that metric at the time of the change, so the direction of the change relative to the model can be audited. Permitted reasons: the source was misread (citation, unit or dispersion error); a better source was found; the scope changed. **Widening a range so that a failing result passes is not permitted**; that result is a fail or a known divergence. A supersession made while the model's result was known counts only after independent review confirms that the source, not the result, drove it.

- **HREQ-E-13** Every expectation SHALL have a stable identifier and SHALL be committed to the registry, with kind, range, unit, evidence and role, before any result for it is computed. Expectations whose registration cannot be shown to precede their results SHALL be marked so and reported separately.
- **HREQ-E-14** Only expectations of kind `quantitative` or `semi-quantitative` SHALL count toward pass and fail totals.
- **HREQ-E-15** An expectation SHALL be changed only by a superseding record that names its predecessor and gives the reason, the author, the date and the model value at the time. A range SHALL NOT be widened to admit a failing result.
- **HREQ-E-16** A known divergence SHALL name the missing mechanism, the evidence on each side and the outputs it affects, and SHALL be displayed with each of those outputs.

---

## 8. Sensitivity and Influence

### 8.1 The screen

`computeInfluence()` (`engine/index.js`, audit F-01 / BUG-0049) answers "which parameters move this quantity?" with a one-at-a-time screen instead of hand-written lists. Reference run: `salt_load_10g`, 24 h, dt = 1/20 h, default parameters. Each of the 54 parameters, including those held fixed in Monte Carlo, is raised by 10 % and the run repeated from its own steady state. Effect = max over time of |Δx| divided by the peak-to-peak range of the default trajectory, floored at 1e-6·max|x|. A parameter "feeds" a quantity when its effect exceeds 1 %. Cost: 55 simulations, under 2 s in Node by the code's own estimate. The code comment still says "all 52" and "~53 simulations"; the table has had 54 rows since D-2 added `map_auto_frac` and `map_auto_tau_h` (an instance of the count drift in §11).

**Why it over-counts within its design.** A perturbation that makes the steady state infeasible counts as influencing every quantity, and a non-finite difference counts as influence. Failures therefore add parameters to lists and never remove them. A threshold of 1 % of each quantity's own response range is also deliberately low.

### 8.2 Where the over-count claim stops holding

The claim holds for how failures are handled, not for the screen as a whole. Measured 2026-10-03:

- **Other scenarios and horizons.** Over `chronic_high_salt_30d` (30 days, +10 %), 15 parameters feed MAP. Six of them are missing from the default screen's 12, including `pn_gain`, one of the three parameters calibrated against He 2013: its effect is 0.65 % at 24 h and 2.3 % over 30 days.
- **Direction and kinks.** `thirst_vol_gain` has exactly zero effect on Thirst in the salt screen, because the hypovolemic term is max(0, 1 − vr) and salt expands the ECF. In `no_water_24h` at −10 % it feeds Thirst. For `V_ecf`, the dehydration screen adds 9 parameters the default screen does not list.
- **Interactions.** A one-at-a-time screen cannot see a parameter that matters only when another has also moved.

The screen therefore runs for every registered scenario at its own horizon, in both directions, and reports the union. Cost: with 54 parameters, 2 directions and 7 scenarios, 756 runs instead of 55; the 30-day scenario dominates, at about 17 s for its 108 runs (estimate from the measured 159 ms per run). This is still a screen, not a global sensitivity analysis: it ranks effects at ±10 % and does not decompose variance. Variance-based analysis is deferred, and until it exists interactions are unseen.

- **HREQ-U-11** Lists of the parameters that feed a displayed quantity SHALL come from the influence screen and never from hand-written lists.
- **HREQ-U-12** The influence screen SHALL run for every registered scenario at that scenario's horizon, with perturbations in both directions, and SHALL report the union.

---

## 9. Indices That Are Not Clinical Measures

### 9.1 What the kidney strain index is

`strain_index` (M10) summarises how far four renal workloads are pushed **above the model's own baseline**:

| Component | Load | Rationale (mechanism, not calibration) |
|---|---|---|
| transport | (tubular sodium reabsorption / baseline − 1) / 0.1 | Tubular O2 use scales with sodium reabsorption (Brezis & Rosen 1995; Sejersted 1982, dog) |
| excretion | (sodium excretion / baseline − 1) / 3.0 | Excretory burden |
| glomerular | 0.5 × (MAP − MAP_0)/10 mmHg + 0.5 × (GFR/GFR_0 − 1)/0.1 | Glomerular pressure and hyperfiltration (Brenner 1982) |
| concentrating | share of the remaining urine-concentrating range in use | Medullary transport demand (Brezis & Rosen 1995) |

Each load is floored at zero; raw = Σ 0.25 × load; index = raw / (1 + raw). It is 0 at baseline and 0.5 when the weighted load equals one unit. All seven constants in `params.json` are E-assumptions held fixed. The three constants in the glomerular term that sit in code (§4.7) are assumptions too, and are not yet in the table. Every load is relative to each parameter set's own baseline.

**What it is not.** It has no units, no validated thresholds and no diagnostic meaning. It is not GFR, not a marker of injury and not a risk score. Because each sample is measured against its own baseline, it does not compare one person with another, and it does not compare across model versions unless its definition constants are unchanged. It exists so that the salt-dose question can be shown as one curve with a band. The curve is a summary of model workloads and nothing more.

### 9.2 Display rules

The viewer colours the kidney by the index with breakpoints at 0.33 and 0.66 (`STRAIN_COLOR_RULE = 'thirds'`, `app/viewer.js`, decision D-1). The code says these "are display choices only". The reader must be told the same, because red reads as damage.

- **HREQ-M-08** An index SHALL be displayed with the label "index (model construct), not a clinical measure", with no units, and any colour breakpoints SHALL be labelled as display choices, not thresholds.
- **HREQ-M-09** An index SHALL NOT be named or described with clinical terms (damage, injury, risk, function, health), and SHALL NOT be compared across model versions unless its definition constants are shown to be unchanged.
- **HREQ-M-10** The band of an index SHALL state that its definition constants are held fixed, so that the band excludes uncertainty about what the index measures.

---

## 10. Roadmap of Scientific Scope

### 10.1 Grow along the couplings V1 already has

Hypertension and cardiovascular disease, chronic kidney disease, type 2 diabetes and obesity are among the largest contributors to global disease burden. They form one cardio-renal-metabolic cluster, and V1 already touches each of them: MAP and pressure natriuresis (hypertension), GFR and glomerular load (kidney), glucose held constant inside plasma osmolality (diabetes), and a fixed 70 kg body (obesity). Burden figures will enter the evidence ledger from the Global Burden of Disease study, graded and dated, before any document cites a number. This section cites none.

| Module | Builds on | New states (indicative) | New evidence classes | New validation sources |
|---|---|---|---|---|
| Hypertension and CVD | M6–M7, `R_auto` | Cardiac output and resistance as separate states; renin, angiotensin II and aldosterone split from the lumped `aldo_vol_exp`; sympathetic tone; a sodium storage compartment (closes the Heer 2000 divergence); SBP and DBP rather than MAP | Salt-sensitivity heterogeneity; drug-response trials | Salt-reduction and antihypertensive RCTs reporting SBP/DBP directly (removing the F-06 conversion); time-course data |
| Chronic kidney disease | M7, M10 | Nephron number, single-nephron GFR, albuminuria, decline over years | Cohort eGFR trajectories; kidney-outcome RCTs | eGFR slope in cohorts and trials, registered before the run |
| Type 2 diabetes | `glucose_mgdl` (constant in V1), M7 | Glucose–insulin dynamics; glycosuria coupled to proximal sodium transport | Clamp and oral-glucose-tolerance studies (A-primary); published glucose–insulin models (C, structure only) | Glycaemic trial data; renal glucose-handling studies |
| Obesity | `REFERENCE_PERSON` (D-3) | Body mass and composition; scaling of volumes and GFR with size; energy balance | Body-composition cohorts | Weight-change trials; size-scaling studies |

**Outcomes are a separate layer.** A mechanistic state such as MAP or GFR is not an outcome. Linking it to strokes, kidney failure or death needs cohort and trial outcome data, its own grading and its own validation, and it moves the subsystem toward individual risk prediction, which the scope in §1.2 excludes. Any such layer reports population-level quantities only.

**Numerical consequence.** Explicit RK4 takes steps no longer than a quarter of the fastest time constant (`03` §3). V1's fastest is 0.075 h (ANP), and the 30-day scenario takes 41,664 steps (measured). A decade-scale kidney module with the same 5-minute osmotic equilibration would need on the order of 4 million steps per run, and about a billion for a 256-sample band (arithmetic from V1 figures). Decade-scale modules therefore need a stiff solver or a quasi-steady-state reduction of the fast states first, validated by the stiff-case test of `03` §3.

### 10.2 Order of evidence before a module is enabled

1. Mechanism map in the knowledge base: entities and relations with evidence and verified identifiers.
2. Parameter table: every row graded, with range kinds and dispersion types; E-assumptions counted.
3. Expectations registered, before any run, from sources not used to choose parameters.
4. Numerical gates passed at the module's time scales, including the stiff case.
5. JavaScript reference and Python port agree on new golden trajectories.
6. Literature harness run, with calibration rows excluded from the count.
7. Independent review by an agent that did not build it, and clinical-safety review of every new user-facing claim.
8. Operator enablement decision, recorded with the module's ≥ B share and its E count.

- **HREQ-M-11** No new module SHALL be shown to readers until every step of §10.2 is complete and recorded.
- **HREQ-M-12** A new module SHALL have at least as many independent, countable expectations as it has calibrated parameters.
- **HREQ-M-13** Any layer linking model states to clinical outcomes SHALL be validated against cohort or trial outcome data, graded separately, and SHALL report population-level quantities only.

---

## 11. Known Failure Modes

| Failure | How it happens here | Defence |
|---|---|---|
| **Overfitting to targets** | Three E-graded gains tuned jointly to one He 2013 band; the comparison then reads as success | `calibration` role, never counted (§6); Monte Carlo over their ranges |
| **Tautological expectations** | Sodium excretion ≈ intake at day 30 holds for any parameter set that reaches balance | `structural` role, never counted (§6.1) |
| **Citation drift** | The sodium bands for `drink_water_1L` and `salt_load_10g` cite "Spec §8", an internal document, with Suckling 2012 kept as context only; the influence-screen comment says 52 parameters when there are 54 | Evidence ids must resolve; every count is computed from data (HREQ-E-04) |
| **Unit errors** | mmol vs mOsm (×2 for NaCl); AVP pmol/L vs pg/mL (molar mass 1084.25 g/mol, ChEBI); mg/dL to mOsm/kg; mL/min vs L/h; per-1.73 m² normalisation | Unit on every row (HREQ-M-03); named conversions; golden trajectories |
| **Stiff-solver artefacts** | Explicit RK4 is unstable once a step exceeds about 2.8 time constants; measured NaN within 48 h at a step of 3.3 × the fastest time constant; clamps and max(0,·) lower the order of convergence (measured: error ratio 3 instead of about 16 per step halving for `Thirst`) | Step rule, convergence and stiff-case gates (`03` §3) |
| **Hidden assumptions** | Constants 0.5, 0.5 and 0.1 in code; 1 kg water = 1 L; Thirst with no feedback; parameter independence; a single reference adult | HREQ-M-07, HREQ-M-06, HREQ-U-03, §2.4 table |
| **Survivorship in the literature** | Small studies of young healthy men; textbooks repeating round numbers (125 mL/min, 93 mmHg); null results less often published | `measure` field (who, how); grade ceiling; E-assumption count shown |
| **SEM/SD confusion** | Suckling 2012 "+3.13 ± 0.75" with type unconfirmed (audit F-02/F-03, BUG-0050/0051): recorded `unverified`, the salted-minus-control comparison kept as a skipped test until the full text confirms it. `thirst_slope` is mean ± 2 SEM, which is uncertainty in a mean, not spread across people | HREQ-E-09; `unverified` kind never counted |
| **Interval arithmetic on end-points** | SBP and DBP CI end-points combined into a MAP band (audit F-06, BUG-0054) | Note on the band; prefer sources reporting the modelled quantity (§10.1) |
| **Grade inflation** | A scenario-condition intake graded A-meta (audit F-12) | Grade ceiling (HREQ-E-03) plus review; the ceiling alone does not catch it |
| **Under-counted influence** | Default screen misses `pn_gain` for MAP over 30 days | HREQ-U-12 |
| **Band misread** | A parameter-range band taken as a confidence interval, or as the range seen in people | HREQ-U-07 |

---

## 12. Interpretation Discipline

Every rule below is enforced in code or schema. None relies on goodwill.

| # | Rule | Enforced by |
|---|---|---|
| 1 | No number without its band, accepted n, rejected count and seed | Result schema in `src/health/engine/` rejects a numeric output lacking them (HREQ-U-04) |
| 2 | Disclaimer and model version travel with every result | Result constructor (HREQ-M-01); checked in `tests/health_selftest.py` |
| 3 | Grade summary (≥ B share, E count) on every view | Computed from the table at render (HREQ-E-04) |
| 4 | Modelled trajectories carry their label ("modeled risk trajectory — not a prediction") | Scenario `label` copied into result metadata, as `meta.label` in V1 |
| 5 | Classification thresholds are labelled as definitions, not physiology | `mc: false` reason field (HREQ-U-01) drives the label (HREQ-M-14) |
| 6 | Indices are labelled as indices | HREQ-M-08, HREQ-M-09 |
| 7 | Known divergences are shown with the outputs they affect | HREQ-E-16 |
| 8 | Calibration is never shown as validation | `role` field; count excludes it (HREQ-E-11) |
| 9 | No direction is claimed when the band spans zero; "the model does not distinguish these" is a legitimate result | Text generator refuses a signed statement when the change band's q05 < 0 < q95 (HREQ-M-15) |
| 10 | No imperatives, no individual framing | Clinical-safety lint over user-facing strings (`03` §8, HREQ-M-02) |
| 11 | An E-assumption is not evidence | E-graded values carry a visible marker; never used as a validation source (§3.1) |

- **HREQ-M-14** A classification threshold SHALL be displayed with the label "classification threshold (definition), not a physiological parameter".
- **HREQ-M-15** No output text SHALL state the direction of a change whose band spans zero (q05 < 0 < q95).

---

## Appendix A. Requirements Minted in This Document

For import into `00_REQUIREMENTS.md`. "Enforced by" names the module or test expected to carry the check.

| ID | Requirement (short form) | § | Enforced by |
|---|---|---|---|
| HREQ-M-01 | Disclaimer and `MODEL_VERSION` are fields of every result object; absence is ≥ S1 | 1.2 | Result constructor; `tests/health_selftest.py` |
| HREQ-M-02 | No output framed as a recommendation to an individual | 1.2 | Clinical-safety lint (`03` §8) |
| HREQ-M-03 | Units declared on every parameter, state, derived and ledger key; conversions named | 2.2 | Schema; `tests/health_selftest.py` |
| HREQ-M-04 | Every equation block labelled and documented | 2.4 | Review checklist (`03` §10) |
| HREQ-M-05 | Analytic steady state; raise on infeasible parameters | 2.4 | Engine; infeasible-parameter fixtures (`03` §9) |
| HREQ-M-06 | Output-only states labelled "no feedback" | 2.4 | Display schema; clinical-safety review |
| HREQ-M-07 | No numeric constant in engine code except named physical constants and unit conversions | 4.7 | Lint over engine source; review |
| HREQ-M-08 | Index labelled as model construct, unitless; colour breakpoints labelled as display choices | 9.2 | Display schema; clinical-safety review |
| HREQ-M-09 | No clinical terms for an index; no cross-version comparison unless its constants are unchanged | 9.2 | Clinical-safety lint |
| HREQ-M-10 | Index band states that definition constants are fixed | 9.2 | Result metadata |
| HREQ-M-11 | No module shown to readers before the §10.2 sequence is complete | 10.2 | Module enablement record |
| HREQ-M-12 | Independent countable expectations ≥ calibrated parameters per module | 10.2 | `src/health/engine/validate.py` summary |
| HREQ-M-13 | Outcome layers validated on cohort/trial outcomes, population-level only | 10.2 | Module review |
| HREQ-M-14 | Classification thresholds labelled as definitions | 12 | Display schema |
| HREQ-M-15 | No direction stated when the change band spans zero | 12 | Text generator; `tests/health_selftest.py` |
| HREQ-E-01 | Value, unit, range, resolvable evidence and grade on every parameter and quantity | 3.3 | `src/health/kb/check.py` |
| HREQ-E-02 | `sourceType` and `defaultGrade` on every evidence record, per the §3.2 mapping | 3.3 | `src/health/kb/check.py` |
| HREQ-E-03 | Grade ceiling: no grade above the best `defaultGrade` of its evidence | 3.3 | `src/health/kb/check.py` |
| HREQ-E-04 | E count and ≥ B share computed at render and shown on every view | 3.3 | Engine summary function; display schema |
| HREQ-E-05 | Conflicts kept beside adopted values; unsourced conflicts say so | 3.3 | `src/health/kb/check.py` |
| HREQ-E-06 | KB and evidence ledger append-only; corrections supersede | 3.3 | `src/health/kb/check.py` against the last released snapshot |
| HREQ-E-07 | External ids only when verified with `resolved: true` | 3.3 | `src/health/kb/check.py` |
| HREQ-E-08 | Structured range kind on every sampled parameter; the weaker kind governs | 4.7 | `src/health/kb/check.py` |
| HREQ-E-09 | Dispersion type and n recorded; unconfirmed types not used | 4.7 | `src/health/kb/check.py`; review |
| HREQ-E-10 | `calibrates` and `calibratedAgainst` recorded and in agreement | 6.3 | `src/health/kb/check.py` |
| HREQ-E-11 | Calibration role never counted as validation | 6.3 | `src/health/engine/validate.py` |
| HREQ-E-12 | Design targets and structural checks never counted | 6.3 | `src/health/engine/validate.py` |
| HREQ-E-13 | Stable ids; registration before results; unproven registrations marked and reported separately | 7.4 | Registry; git history audit |
| HREQ-E-14 | Only quantitative and semi-quantitative kinds count | 7.4 | `src/health/engine/validate.py` |
| HREQ-E-15 | Expectations change only by supersession; no widening to admit a fail | 7.4 | Registry append-only check |
| HREQ-E-16 | Known divergences name mechanism, evidence and affected outputs, and are displayed with them | 7.4 | Registry schema; display schema |
| HREQ-U-01 | Fixed only as scenario condition, classification threshold or index constant; reason recorded | 4.7 | `src/health/kb/check.py` |
| HREQ-U-02 | Sampling mode derived from the range | 4.7 | Engine; `tests/health_selftest.py` |
| HREQ-U-03 | Independence assumption stated in every Monte Carlo result | 4.7 | Result metadata |
| HREQ-U-04 | Band, n, rejected count and seed with every number; missing band is S1 | 5.4 | Result schema |
| HREQ-U-05 | Seeded mulberry32 in table order; seeds fixed before the run | 5.4 | Golden fixture (`03` §4) |
| HREQ-U-06 | Rejection rules; count reported; > 5 % rejected raised as a finding | 5.4 | Engine; `03` §7 |
| HREQ-U-07 | Bands labelled as parameter-range bands, never as CI or population range | 5.4 | Display schema |
| HREQ-U-08 | n ≥ 256 for status and claims; smaller n displayed | 5.4 | `src/health/engine/validate.py` |
| HREQ-U-09 | Summaries and changes computed per sample, never from quantile curves | 5.4 | Engine; `tests/health_selftest.py` |
| HREQ-U-10 | Common random numbers across sweep values | 5.4 | Engine; golden fixture |
| HREQ-U-11 | Parameter lists for displayed quantities come from the influence screen | 8.2 | Display code review |
| HREQ-U-12 | Influence screen per scenario, at its horizon, both directions, union reported | 8.2 | Engine; `03` §7 |
