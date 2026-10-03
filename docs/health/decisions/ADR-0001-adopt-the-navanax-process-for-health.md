# ADR-0001 — Adopt the Navanax process for the health subsystem

**Status:** Accepted · **Date:** 2026-10-03 · **Deciders:** Spencer (direction), orchestrator session
**Supersedes:** — · **Superseded by:** —

## Context

The Operator's Metabolic Map V1 (an educational water/sodium/kidney model with graded
evidence and Monte Carlo bands) is the beginning of a broader effort aimed at the
largest causes of death and disability. The Operator asked for logs, a recorded process,
errors mitigated permanently, every piece of architecture diagrammed, and a system that
is easy to undo and add onto.

This repository already runs a process with exactly those properties for market data:
requirement IDs, severity by consequence, an append-only bug ledger with a CI gate,
stdlib-only self-tests that run before any install, config-not-code, independent
validation, and an Operator who approves every pull request.

## Decision

Build the health subsystem **inside this repository**, reusing the process unchanged
and replacing the content:

- `docs/health/00–07` mirror `docs/00–07` in structure and voice; where a health document says "as in `docs/NN`", that section applies unchanged.
- Shared roles (orchestrator, tech-lead, bug-triage, docs-steward, build-reporter, qa-auditor, docs-explainer, design-lead, ui-designer, platform-engineer) are reused; four health roles are added (`health-physiology-modeler`, `health-literature-curator`, `health-rigor-lead`, `health-safety-reviewer`).
- The bug ledger is shared (`docs/logs/bugs.yaml`, entries tagged `area: health`), so one gate (`tools/buglog.py --check`) covers both.
- The gates and CI run the health self-tests beside the market ones, stdlib-first.
- The V1 artifact is vendored verbatim under `reference/metabolic-map-v1/` as the origin and the reference implementation.

## Consequences

- One process to learn and one to drift. Two subsystems in one repo means a market-side CI failure blocks a health merge and vice versa; accepted, because a red main is a red main.
- The severity principle is re-grounded: downstream of a wrong number here is a reader, not a backtest (`docs/health/05 §1`).
- Documents cross-reference `docs/NN`; `docs-steward` must sweep both sets.

## How to undo

Delete `docs/health/`, `src/health/`, `tests/health_*`, `tests/fixtures/health/`,
`tools/health_golden.mjs`, `config/health/`, `reference/metabolic-map-v1/`,
`.claude/agents/health-*.md`; remove the health steps from `tools/gates.py` and
`.github/workflows/ci.yml`; mark health ledger entries `wont_fix` with a reason. The
market subsystem is untouched by construction (`docs/health/07`).
