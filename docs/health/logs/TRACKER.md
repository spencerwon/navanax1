# Tracker — Health

Open decisions for the Operator, open work by milestone, and what is blocked on whom.
Updated by the orchestrator at every PR and every Friday briefing. Closed items move to
the bottom with the date and the ADR or commit that closed them.

## Decisions for Spencer

| Id | Question | Options and recommendation | Needed by | Status |
|---|---|---|---|---|
| **Q1** | Home repository: `navanax1` (this branch), the Mac repo `Desktop/The Human Body`, or a new `spencerwon/metabolic-map` | Recommend: publish the Mac repo as `metabolic-map`, then migrate this subsystem into it by ADR; until then this branch is the platform | Before M1 | **Open** |
| **D-1** | Kidney strain colour rule A/B/C | Rendered in `reference/metabolic-map-v1/app/design-options.html`; the design-lead will screenshot the three on your machine | M1 | **Open** (carried from the V1 session) |
| **D-4** | Bladder: grow-and-void at 0.4 L, or hide | Recommend keep, labelled "display rule, not model" | M1 | **Open** (carried) |
| **D-6** | Repository visibility and licence | Recommend private until M1 review; licence to decide with the publication decision | Before publishing | **Open** (carried) |
| **D-7** | Theme: dark always, or follow the system | Recommend follow the system; both themes are designed and the summary page shows both | M1 | **Open** (carried) |
| **A-1** | Approve the M0 pull request | — | Now | **Open** |
| **A-2** | BUG-20261003-100 (S0a): the published V1 artifact shows the strain index unlabelled on the dose panel and the HUD gauge. Patch the artifact now (orchestrator can republish it with the two labels and the dose-panel heading from the ledger entry), or accept until M1's viewer review | Patch now: the fix is two strings and a heading | Now | **Open** |
| **D-8** | Chronic trajectory label: keep V1's "modeled risk trajectory — not a prediction" or drop "risk" as the safety review recommends ("modelled trajectory for a 70 kg reference adult — not a prediction or a risk estimate for any person") | Recommend dropping "risk": the label sits on a MAP trajectory and the model computes no outcome | M1 | **Open** |
| **D-9** | BUG-20261003-115: after 1 L of water the modelled sodium recovers in 7.05 h; the registered Crowe 1987 range is 6 h (in-range share 0.32 at n = 256). Supersede the row as a known divergence with the reason, or recalibrate the model through WF-H-01 with the row kept as a counted fail until it passes on its own | Recommend recalibration at M1 via WF-H-01 (the extractor and the reference agree; the curator first confirms the Crowe 1987 protocol: 20 mL/kg in young water-replete men vs the model's 1 L) | M1 | **Open** |
| **D-10** | `drink_water_1L/05` (minimum urine osmolality, 40–150 mOsm/kg) cites Baylis 1986, and so does `U_osm_min` (50 mOsm/kg, range 40–80), whose notes quote Baylis's 69 ± 3 mOsm/kg. Is that row a calibration target counted as validation? | Curator to decide from the parameter's history: if `U_osm_min` was chosen with Baylis in view, the row takes role calibration and leaves the validation count (9 counted passes would become 8); if not, record why the shared source is independent | M1 | **Open** (curator; ledger entry open) |
| **D-11** | Extends D-9: the registered "< 6 h" for `drink_water_1L/02` has no traceable source value: the `ev:crowe-1987` record (quote and notes) reports water excreted at 2 h and peak free-water clearance, no plasma-sodium recovery time, and the expectation carries no note saying where 6 h came from | Curator traces the bound to a figure or table of Crowe 1987, or the row is superseded as `unverified` with the attempt logged; decide together with D-9 | M1 | **Open** (curator; ledger entry open) |

## Open work

| Id | Item | Milestone | Owner | Status |
|---|---|---|---|---|
| W-2 | `01_METHODOLOGY.md`, `03_VALIDATION_AND_TESTING.md` | M0 | docs builder | in progress |
| W-3 | Rigor-lead attack on the knowledge-base checker | M0 | health-rigor-lead | in progress |
| W-4 | Safety review of the document set and agent definitions | M0 | health-safety-reviewer | in progress |
| W-5 | Push the branch to GitHub | M0 | orchestrator | **blocked**: the session's git credential has no access to `spencerwon/navanax1`; fix at claude.ai/connect-github |
| W-6 | Six `unused-evidence` warnings (`ev:shafiee-2005`, `crowe-1987`, `heer-2000`, `rakova-2017`, `suckling-2012`, `uttamsingh-1985` are cited only by scenario expectations or the engine evidence file) | M1 | health-literature-curator | open — decide: cite from the KB, or exempt scenario-only evidence by rule |
| W-7 | V1 audit F-12: `naIn_base_mmold` grade A-meta for a scenario condition | M1 | health-literature-curator | open |
| W-8 | V1 audit F-06: He 2013 MAP band is approximate; three parameters calibrated to it | M1 | health-literature-curator + modeler | open |
| W-9 | V1 audit F-02/F-03 (Q4): Suckling 2012 SEM vs SD | M1 | health-literature-curator | open |
| W-10 | Narratives for the seven registered scenarios (HREQ-P-15) | M1 | ui-designer + docs-explainer | open |
| W-11 | Grade-E red pill and chart banner audit against HREQ-S-06 on the reference app | M1 | design-lead + health-safety-reviewer | open |
| W-12 | Stress-test measurements recorded from the Operator's machine (HREQ-N-07) | M1 | Spencer runs; orchestrator records | open |
| W-13 | Mirror the harness `role` / `calibrates` fields into `reference/metabolic-map-v1/engine/scenarios.js` with the next MODEL_VERSION bump (BUG-20261003-097, Python-side only this phase) | M1 | health-physiology-modeler | open |
| W-14 | BUG-20261003-095: three strain-index constants into params.json rows (grade E, mc false) in both implementations; version bump; golden regenerated | M1 | health-literature-curator + modeler | open |
| W-15 | BUG-20261003-096: influence screen per scenario, both directions, union (HREQ-U-12); feeder-list diff in every model-change review (HREQ-V-19) | M1 | health-physiology-modeler | open |
| W-16 | BUG-20261003-099: stale "52 parameters" comment and missing M9 label in the reference | M1 | health-physiology-modeler | open |
| W-18 | DEFERRED knowledge-base rules that need data fields (range-kind, dispersion, calibration-link, fixed-reason, append-only snapshot) — add the fields through WF-H-01 and promote each rule to an error (BUG-20261003-112) | M1 | health-literature-curator + modeler | open |
| W-19 | Display-title map keyed by scenario id in the surface layer (BUG-20261003-102), thresholds in neutral tone with the config label (BUG-101), divergence and calibration text under the chronic chart (BUG-103) | M1 | platform-engineer + design-lead | open |
| W-20 | Reference engine gains `validation_status`, the `role`/`calibrates` fields and the title/description fixes with the next MODEL_VERSION bump (BUG-104, W-13) | M1 | health-physiology-modeler | open |
| W-21 | Step-halving convergence, ±20 % perturbation, seed-stability and rejection-share runs (HREQ-V-05, V-20, V-21, V-22) as a robustness script and a scheduled check, not in the two-minute suite | M1 | health-rigor-lead | open |
| W-22 | Re-verify every line marked `[VERIFY-AFTER-MERGE]` in `docs/health/` and `config/health/base.yaml` on the merged tree, then strip the markers | M0 | orchestrator | open |

## Blocked on

- **Spencer:** A-1, A-2, Q1, D-1, D-4, D-6, D-7, D-8, D-9, W-12, and GitHub access for W-5.
- **Curator:** D-10, D-11 (D-11 before D-9 is decided).

## Closed

| Id | Item | Closed | By |
|---|---|---|---|
| W-1 | Engine port with golden equivalence; expectation harness; `tests/health_selftest.py`. Measured on the port: 12,822 golden values compared; trajectories 9,512 values, 97.9 % bit-identical; worst overall 1.24e-12 relative (`computeInfluence` `effects.strain_excretion.glucose_mgdl`, 0.00124 × tolerance); worst trajectory 3.84e-14 (`strain_concentrating`, stiff corner) | 2026-10-03 | b810ed6 |
| W-17 | Enforcement status of the HREQ-M/E/U appendix (`01` Appendix A) and the HREQ-V appendix (`03` Appendix B): every row carries a Status of Enforced, Partial, Planned or Not enforced, checked against the code | 2026-10-03 | The documents-against-code round (BUG-20261003-142 to -153) |
| — | Origin context recovered (vision, prior state, open decisions) | 2026-10-03 | ADR-0006 |
| — | D-2 slow blood-pressure state; D-3 GFR two ways with a named reference person | 2026-10-01 (V1 session) | V1 `MODEL_VERSION` 1.0.1 |
