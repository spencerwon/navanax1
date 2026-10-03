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
