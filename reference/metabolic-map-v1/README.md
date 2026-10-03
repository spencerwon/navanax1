# Metabolic Map V1 — vendored reference

**Origin:** the Operator's Claude artifact "Metabolic Map", https://claude.ai/artifact/MhoyGfG7qHJMXDP61gA6pD, version `1790893481-d866` (published 2026-10-01), fetched 2026-10-03.
**Status:** read-only. Nothing under this directory is edited; a change to the model is made in `src/health/engine/` **and** mirrored here in the same pull request, with the golden fixture regenerated (`docs/health/04 §6`, HREQ-X-05).

This is the narrower-scope beginning of the health platform: an educational body-fluid,
sodium and kidney model with a 3D body viewer and Monte Carlo uncertainty bands. The
app renders "Educational model — not medical advice." on every surface; "Not clinically
validated." exists only as a comment in `engine/model.js`, which is short of the
two-sentence standard in `docs/health/00 §1.4` (V1 audit item, `docs/health/logs/V1_AUDIT_ITEMS.md`).

## What is in it

| Path | What | Notes |
|---|---|---|
| `engine/model.js` | 13-state ODE: ECF/ICF volume, Na, K, ICF solute, ADH, gut pools, thirst, aldosterone, ANP, MAP, slow autoregulation | Blocks M0–M10; the kidney strain index (M10) is an index, not a clinical measure |
| `engine/solver.js` | Fixed-step RK4 with event breakpoints and a step bound from the fastest time constant | Linear invariants preserved to round-off |
| `engine/mc.js` | Seeded Monte Carlo (mulberry32) over parameter ranges; log-uniform when hi/lo > 5; rejection of infeasible baselines | Independence between parameters is an assumption |
| `engine/scenarios.js` | baseline · drink_water_1L · drink_water_3L_fast · salt_load_10g · salt_load_sweep · chronic_high_salt_30d · no_water_24h; six carry literature expectations written alongside the model (co-developed, `docs/health/01 §7.1`) | Expectation kinds: quantitative, semi-quantitative, qualitative, design-target, known-divergence, unverified |
| `engine/params.json` | 54 parameters: value, unit, range, evidence, grade, notes | Grades: 3 A-meta · 6 A-primary · 15 B-textbook · 30 E-assumption |
| `engine/index.js` | Public API: simulate, drawSamples, simulateMC, simulateSweep, computeInfluence; MODEL_VERSION 1.0.1 | `params.data.js` is the same table as `params.json` for the browser |
| `kb/schema.json` | Entity / Relation / Evidence contract with cross-file rules | Extensions marked EXTENSION |
| `kb/entities.json` | 107 entities, scales 0–7, ontology ids | |
| `kb/relations.json` | 203 relations | |
| `kb/evidence.json` | 57 verified sources with sourceType, defaultGrade, verification | |
| `kb/VERIFICATION_LOG.json` | 154 external-id resolutions against their registries | |
| `app/` + `index.html` | The viewer: scale ladder, organ-system tree, 3D body, Monte Carlo charts, evidence drawer, dose sweep, stress test | Workers in `app/worker.js` |

## The V1 team's own audit trail

The files reference the V1 build's audit items (`F-01` … `F-12`), its bug ids
(`BUG-0002`, `BUG-0049` … `BUG-0054`), tracker items (`T-103`) and Operator decisions
(`D-2`, `D-3`). These are **V1 audit items**, not entries in this repository's ledger;
they are catalogued with their current status in `docs/health/logs/V1_AUDIT_ITEMS.md`.

## How this directory is used

- `tools/health_golden.mjs` runs `engine/index.js` under Node to produce `tests/fixtures/health/golden_v1.json`, which `tests/health_selftest.py` holds the Python port to.
- `src/health/kb/data/` is a byte-identical copy of `kb/`, checked by `health.cli kb-check`.
- `src/health/engine/params.json` is a byte-identical copy of `engine/params.json`.
- Open `index.html` in a browser for the app itself (module scripts need a local server for some browsers: `python3 -m http.server` from this directory).
