# Health — Foundational Documents

**Version:** 0.1 · **Date:** 2026-10-03 · **Status:** Draft, awaiting Operator sign-off

The health subsystem applies the discipline this repository already enforces for market
data — every number with a source, severity by consequence, append-only records,
independent validation, config not code — to mechanistic models of human physiology,
starting from the Operator's **Metabolic Map V1** (water, sodium, the kidney and
arterial pressure) and growing, module by module, toward the largest causes of death
and disability in the world.

**It is an educational and research platform. It never issues an individual clinical
recommendation.** That sentence is a requirement (HREQ-S-01), is carried in-band by every
result the engine returns, and is the first thing a reviewer checks.

## Read in this order

| # | Document | Answers |
|---|---|---|
| 0 | [`00_REQUIREMENTS.md`](00_REQUIREMENTS.md) | What is being built, what is out of scope, what "done" means, the phases |
| 1 | [`01_METHODOLOGY.md`](01_METHODOLOGY.md) | How the modelling works: evidence grades, parameter discipline, uncertainty, calibration vs validation |
| 2 | [`02_AGENT_HIERARCHY.md`](02_AGENT_HIERARCHY.md) | Who does what, with what authority, through which workflows |
| 3 | [`03_VALIDATION_AND_TESTING.md`](03_VALIDATION_AND_TESTING.md) | How we find out we are wrong before a reader acts on a number |
| 4 | [`04_ENVIRONMENTS_AND_UNDO.md`](04_ENVIRONMENTS_AND_UNDO.md) | Environments, branching, and the rules that make every module removable |
| 5 | [`05_BUG_TAXONOMY.md`](05_BUG_TAXONOMY.md) | Severity hierarchy for a system whose failure mode is a plausible wrong number |
| 6 | [`06_ARCHITECTURE.md`](06_ARCHITECTURE.md) | The layers, the data flow from citation to chart, every diagram |
| 7 | [`07_MODULE_REGISTRY.md`](07_MODULE_REGISTRY.md) | Every module: files, owner, flag, dependencies, how to remove it |

Supporting records:

- [`decisions/`](decisions/) — architecture decision records (ADR-0001 onward). A decision is reversed by a new ADR, never by editing an old one.
- [`logs/PROCESS_LOG.md`](logs/PROCESS_LOG.md) — append-only record of what was done, what was tried, what failed, and why.
- [`diagrams/`](diagrams/) — the Mermaid sources and the SVG for every figure in `06_ARCHITECTURE.md`.
- The bug ledger is shared with the rest of the repository: `docs/logs/bugs.yaml` (entries carry `area: health`).
- The V1 origin, byte-for-byte: [`reference/metabolic-map-v1/`](../../reference/metabolic-map-v1/README.md).

Agent definitions live in `.claude/agents/health-*.md`; the shared roles (orchestrator,
tech-lead, bug-triage, docs-steward, build-reporter, qa-auditor, docs-explainer,
design-lead, ui-designer, platform-engineer) are reused unchanged.

## The eight decisions everything else follows from

1. **Not medical advice, structurally.** The platform models mechanisms and populations. It never diagnoses, never doses, never recommends. The disclaimer is a field on every result object, not a footer somebody can delete (HREQ-S-01..03).
2. **Every number has a source and a grade.** `A-meta`, `A-primary`, `B-textbook`, `C-model`, `D-animal`, `E-assumption`. Assumptions are counted and shown, never hidden. The share of parameters graded ≥ B is a headline metric of the whole project. V1 ships at 24 of 54 (44 %); the number is on the status line so nobody can forget it.
3. **Calibration is never validation.** A parameter tuned to hit a literature target is recorded as *calibrated to* that target and can never be *validated by* it. The V1 chronic-salt pressure slope is the standing example.
4. **No point estimate without a band.** Every quantity is a Monte Carlo distribution over the parameter ranges. A number without its band is an S1 defect, not a style issue.
5. **Expectations are pre-registered.** Each scenario carries literature expectations written before results are examined, with kinds (`quantitative`, `semi-quantitative`, `qualitative`, `design-target`, `known-divergence`, `unverified`). Only the first two can count as passes; the harness never silently skips one.
6. **One model, two implementations.** The JavaScript reference and the Python port must agree to 1e-9 relative on golden trajectories. Divergence stops the line.
7. **Append-only knowledge.** Entities, relations, evidence and verification records are never edited in place; corrections supersede, and the disagreeing source stays in `conflicts`.
8. **Everything is removable.** Every module has a flag, a registry entry, an owner, tests, and a written removal recipe. Adding a module is a checklist; undoing one is a shorter checklist. Spencer approves every pull request; nothing auto-merges.

## Where things are

```
docs/health/            this contract
config/health/          base.yaml (every threshold), modules.yaml (every flag)
src/health/             errors · engine (solver, model, scenarios, mc, validate) · kb (data, check, report) · cli
tests/                  health_selftest.py (engine) · health_kb_selftest.py (knowledge base)
tests/fixtures/health/  golden_v1.json — generated from the JavaScript reference by tools/health_golden.mjs
reference/metabolic-map-v1/   the V1 artifact, vendored verbatim (engine, kb, app)
.claude/agents/health-*.md    the four health-specific roles
```

Run it:

```bash
PYTHONPATH=src python3 -m health.cli status        # version, KB counts, parameter grade share, the disclaimer
PYTHONPATH=src python3 -m health.cli kb-check      # every knowledge-base contract rule; exit 1 on an error
python3 tests/health_selftest.py --no-skips        # engine: golden equivalence, steady state, mass balance, expectations
python3 tests/health_kb_selftest.py --no-skips     # knowledge base: every rule, each with a planted violation
python3 tools/gates.py                             # all of the above plus the repository's own gates
```

## Relationship to the market-data documents

The documents under `docs/` numbered 00–08 describe the OpenSea platform. The health
documents reuse their *process* — authority levels, the severity principle, the bug
ledger, the gates, the branching model — and replace their *content*. Where a health
document says "as in `docs/NN`", the referenced section applies unchanged. Where the two
disagree about process, the health document is wrong and should be fixed to match;
the process is shared on purpose, because a second process is a second place to drift.
