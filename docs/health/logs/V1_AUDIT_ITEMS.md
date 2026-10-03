# V1 audit items — catalogue

The Metabolic Map V1 artifact carries its own audit trail in code comments and data
notes: audit items `F-nn`, bug ids `BUG-nnnn`, tracker items `T-nnn`, and Operator
decisions `D-n`. They are **not** ids in this repository's ledger (ADR-0005). This page
catalogues every one the vendored files mention, with where it appears and its status
as far as the files show it. Items are added as they are found; a status changes only
when a ledger entry here resolves it.

| V1 id | What the files say | Where | Status in V1 files |
|---|---|---|---|
| `BUG-0002` / `T-103` | `KB_AVAILABLE` switch so a copy served without `kb/` never requests it (no 404 in the console) | `app/config.js` | Fixed (switch present) |
| `BUG-0003` | Knowledge-base entities with no 3D mesh are labelled by their `scale`; seed entities keep their V1 level | `app/ui.js` `levelOf()` | Fixed |
| `F-01` / `BUG-0049` | Which parameters feed each quantity is computed by a one-at-a-time sensitivity screen instead of hand-written lists; evidence chips use it | `engine/index.js` `computeInfluence`, `app/worker.js` | Fixed |
| `F-02` / `F-03` / `BUG-0050` / `BUG-0051` | Suckling 2012: +3.13 ± 0.75 mmol/L is salted-minus-control, a different quantity from the chart's rise-from-own-baseline; SEM vs SD unverified from the abstract. Recorded as kind `unverified`, not counted; the comparison is a skipped test pending the full text | `engine/scenarios.js` `salt_load_sweep`, `salt_load_10g` | **Open** — needs the full text (`docs/health/00 §11` Q4) |
| `F-04` / `BUG-0052` | On narrow screens the footer disclaimer was ~6,000 px down; a compact strip now rides in a sticky header | `app/index.html` CSS `.top-disclaimer` | Fixed (styling marked provisional) |
| `F-06` / `BUG-0054` | The He 2013 MAP band [0.7, 3.2] is derived by combining SBP and DBP CI end-points, which is not a true MAP CI; recomputed with the normotensive subgroup's own urinary Na change (−75 mmol/24 h) and unchanged | `engine/scenarios.js` `chronic_high_salt_30d`, `engine/params.json` `map_vol_exp`, `pn_gain`, `aldo_vol_exp` | **Open** — band is approximate; the three parameters are calibrated, not validated |
| `F-10` | Range kinds made explicit in parameter notes: "curator assumption around a textbook point value" vs reported intervals | `engine/params.json` (many rows) | Fixed (notes present) |
| `F-12` | `naIn_base_mmold` is graded `A-meta` from He 2013 but 8.8 g/day is a scenario condition, not a measured trial intake; grade B would be more honest; left unchanged until a source is chosen | `engine/params.json` `naIn_base_mmold` | **Open** — a `PRM` candidate for the ledger |
| `D-1` / `T-120` | Kidney strain colour rule: breakpoints are display choices only (the index has no clinical thresholds); three rules compared in a review page | `app/viewer.js` `strainColor()`, `app/design-options.html` | Decided (display rule chosen) |
| `D-2` | Slow whole-body autoregulation: `map_vol_exp` split into a fast share and a slow share delivered through state `R_auto` with `map_auto_frac`, `map_auto_tau_h`; steady state unchanged, time course changed; approved by Spencer 2026-10-01; `MODEL_VERSION` 1.0.1 | `engine/model.js` M6, `engine/params.json`, `engine/scenarios.js` | Decided and implemented |
| `D-3` | The reference person: 70 kg, 1.73 m² BSA; absolute GFR and BSA-normalised GFR both reported and explained; approved by Spencer 2026-10-01 | `engine/model.js` `REFERENCE_PERSON`, `app/index.html` `.kb-note` | Decided and implemented |

## What this catalogue is for

- The three **Open** items are the first candidates for ledger entries once a curator or modeler picks them up (WF-H-01 for F-12; WF-H-02 for F-02/F-03; a methodology note for F-06).
- The two Operator decisions (D-2, D-3) are the precedent for how a decision is recorded here: an ADR with status, context, decision, consequences and how to undo.
