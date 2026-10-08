# ADR-0002 — A Python port held to the JavaScript reference by a golden fixture

**Status:** Accepted · **Date:** 2026-10-03 · **Deciders:** orchestrator session; Operator review pending
**Supersedes:** — · **Superseded by:** —

## Context

The V1 engine is JavaScript (ES modules, runs in the browser and Node). The repository's
gates are Python and run before any dependency is installed. The model must be testable
in the gates, and the browser app must keep working unchanged.

Options considered:

1. **Keep JavaScript only**, run Node in CI. Cheapest; but the model is then outside every Python gate (ledger, selftest conventions, `@needs`), and a second implementation is the cheapest independent check of the first.
2. **Port to Python and retire the JavaScript.** Breaks the published app and loses the V1 verification trail.
3. **Port to Python, keep JavaScript as the reference, prove equivalence by a golden fixture.** Two implementations to maintain; every model change is made twice.

## Decision

Option 3. `src/health/engine/` is a pure-stdlib port with the same identifiers and the
same M0–M10 block structure. `tools/health_golden.mjs` runs the vendored JavaScript under
Node to produce `tests/fixtures/health/golden_v1.json`; `tests/health_selftest.py` holds
the Python port to it at the tolerances in `config/health/base.yaml` (1e-9 relative,
1e-12 absolute near zero). The PRNG (mulberry32) is reproduced bit-exactly so Monte Carlo
draws are identical across languages.

A model change bumps `MODEL_VERSION` in both implementations and in
`config/health/base.yaml`, regenerates the fixture, and explains every difference in the
pull request (HREQ-X-05). Divergence beyond tolerance is `ReferenceDivergenceError`, S0b.

## Consequences

- Every equation exists twice and is reviewed twice. Accepted: the duplication is the test.
- Node is needed only to regenerate the fixture, never to run the gates; the fixture is committed.
- Pure-Python RK4 over 744 h is slow; tests use small Monte Carlo `n` and the suite is budgeted at two minutes (HREQ-N-04).
- If a future module needs an adaptive solver, both implementations change and this ADR is superseded.

## How to undo

Remove module `engine-v1` per `config/health/modules.yaml` (delete the engine, the
selftest, the fixture and the generator; remove the gate and CI steps). The JavaScript
reference remains the only implementation.

## Errata (2026-10-03)

The decision stands. Three sentences above say more than the code does; the accepted
text is left as written (`04 §6.4`) and corrected here.

1. **"the same identifiers".** The port keeps the JavaScript's identifiers for every
   state, derived quantity, ledger entry, parameter and scenario, and the JavaScript's
   keys in the result dictionaries the two share (`maxStep`, `outEvery`, `modelVersion`,
   `q05`, ...); `test_results_carry_the_disclaimer_and_reproducibility_fields` checks the
   `simulate()` keys. Its functions and arguments are snake_case: `simulate_mc` for
   `simulateMC`, `draw_samples` for `drawSamples`, `t_end` for `tEnd`. `00` HREQ-P-01 is
   reworded to say so.
2. **"at the tolerances in `config/health/base.yaml`".** The tolerances are constants in
   `tests/health_selftest.py` (`REL_TOL = 1e-9`, `ABS_TOL = 1e-12`,
   `SAMPLE_REL_TOL = 1e-12`). `config/health/base.yaml` mirrors the first two, and
   `test_config_agrees_with_engine_and_reference` fails if the mirror and the constants
   differ. The suite does not read its tolerances from the configuration.
3. **"Divergence beyond tolerance is `ReferenceDivergenceError`, S0b".** The class exists
   in `src/health/errors.py` and nothing raises it. A divergence fails the golden
   comparison tests of `tests/health_selftest.py`, and so the CI step and
   `tools/gates.py`, and is treated as S0b under the stop-the-line procedure of
   `03 §4.4`.
