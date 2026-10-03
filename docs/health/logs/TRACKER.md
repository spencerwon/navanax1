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

## Open work

| Id | Item | Milestone | Owner | Status |
|---|---|---|---|---|
| W-1 | Engine port with golden equivalence; expectation harness; `tests/health_selftest.py` | M0 | health-physiology-modeler | in progress (builder) |
| W-2 | `01_METHODOLOGY.md`, `03_VALIDATION_AND_TESTING.md` | M0 | docs builder | in progress |
| W-3 | Rigor-lead attack on the knowledge-base checker | M0 | health-rigor-lead | in progress |
| W-4 | Safety review of the document set and agent definitions | M0 | health-safety-reviewer | in progress |
| W-5 | Push the branch to GitHub | M0 | orchestrator | **blocked**: the session's git credential has no access to `spencerwon/navanax1`; fix at claude.ai/connect-github |
| W-6 | Six `unused-evidence` warnings (`ev:shafiee-2005`, `crowe-1987`, `heer-2000`, `rakova-2017`, `suckling-2012`, `uttamsingh-1985` are cited only by scenario expectations or the engine evidence file) | M1 | health-literature-curator | open — decide: cite from the KB, or exempt scenario-only evidence by rule |
| W-7 | V1 audit F-12: `naIn_base_mmold` grade A-meta for a scenario condition | M1 | health-literature-curator | open |
| W-8 | V1 audit F-06: He 2013 MAP band is approximate; three parameters calibrated to it | M1 | health-literature-curator + modeler | open |
| W-9 | V1 audit F-02/F-03 (Q4): Suckling 2012 SEM vs SD | M1 | health-literature-curator | open |
| W-10 | Narratives for the six registered scenarios (HREQ-P-15) | M1 | ui-designer + docs-explainer | open |
| W-11 | Grade-E red pill and chart banner audit against HREQ-S-06 on the reference app | M1 | design-lead + health-safety-reviewer | open |
| W-12 | Stress-test measurements recorded from the Operator's machine (HREQ-N-07) | M1 | Spencer runs; orchestrator records | open |

## Blocked on

- **Spencer:** A-1, Q1, D-1, D-4, D-6, D-7, W-12, and GitHub access for W-5.

## Closed

| Id | Item | Closed | By |
|---|---|---|---|
| — | Origin context recovered (vision, prior state, open decisions) | 2026-10-03 | ADR-0006 |
| — | D-2 slow blood-pressure state; D-3 GFR two ways with a named reference person | 2026-10-01 (V1 session) | V1 `MODEL_VERSION` 1.0.1 |
