# Module Registry — Health

**Version:** 0.1 · **Date:** 2026-10-03 · **Status:** Draft for Operator review
**Source of truth:** `config/health/modules.yaml`. This page is a readable rendering of
that file and must say the same thing; `qa-auditor` checks the two against each other.

A module is the unit of adding and undoing. Every module has an owner, a flag, paths,
dependencies, tests, an ADR and a removal recipe (`00 §7`, `04 §6.5`). A module whose
removal recipe has not been tried on a scratch branch is not finished.

| id | enabled | phase | owner | depends on | paths | tests | ADR |
|---|---|---|---|---|---|---|---|
| `reference-v1` | true | 0 | Operator (read-only) | — | `reference/metabolic-map-v1/` | `tests/health_selftest.py` (golden generated from it) | ADR-0001 |
| `engine-v1` | true | 0 | health-physiology-modeler | reference-v1 | `src/health/engine/`, `tests/health_selftest.py`, `tests/fixtures/health/golden_v1.json`, `tools/health_golden.mjs` | `tests/health_selftest.py` | ADR-0002 |
| `kb-v1` | true | 0 | health-literature-curator | reference-v1 | `src/health/kb/`, `tests/health_kb_selftest.py` | `tests/health_kb_selftest.py` | ADR-0003 |
| `cli` | true | 0 | platform-engineer | engine-v1, kb-v1 | `src/health/cli.py` | `tests/health_kb_selftest.py::test_cli_kb_check_exit_codes`, `::test_status_prints_the_disclaimer` | ADR-0003 |
| `errors` | true | 0 | health-physiology-modeler | — | `src/health/errors.py` | `tests/health_selftest.py` (import and halt-flag checks) | ADR-0001 |
| `process` | true | 0 | orchestrator | — | `docs/health/`, `.claude/agents/health-*.md`, `config/health/`, the health steps in `tools/gates.py` and `.github/workflows/ci.yml` | `tools/buglog.py --check` | ADR-0001 |

## Removal recipes

**`cli`** — set `enabled: false`; delete `src/health/cli.py`; delete the two CLI tests;
remove the `kb-check` step from `tools/gates.py` and `.github/workflows/ci.yml`; remove
this entry; run `tools/gates.py`.

**`engine-v1`** — remove `cli` first; set `enabled: false`; delete `src/health/engine/`,
`tests/health_selftest.py`, `tests/fixtures/health/golden_v1.json`,
`tools/health_golden.mjs`; remove the `health_selftest` steps from `tools/gates.py` and
CI; remove this entry; run `tools/gates.py`.

**`kb-v1`** — remove `cli` first; set `enabled: false`; delete `src/health/kb/` and
`tests/health_kb_selftest.py`; remove the `health_kb_selftest` steps from gates and CI;
remove this entry; run `tools/gates.py`.

**`errors`** — only after `engine-v1`, `kb-v1` and `cli` are gone (they import it);
delete `src/health/errors.py`; remove the `SurfaceIntegrityError` CI grep; remove this
entry.

**`reference-v1`** — only after `engine-v1` and `kb-v1` are gone (the golden fixture and
the KB data derive from it); delete `reference/metabolic-map-v1/`; remove this entry.
The original remains the Operator's artifact (`reference/metabolic-map-v1/README.md`
records its URL and version).

**`process`** — delete `docs/health/`, `.claude/agents/health-*.md`, `config/health/`;
remove the health steps from gates and CI; remove health entries from
`docs/logs/bugs.yaml` only by marking them `wont_fix` with a reason (ledger entries are
never deleted); remove the health rows from `docs/README.md` and the root `README.md`.

## Adding a module

1. Branch `module/<id>`.
2. ADR under `docs/health/decisions/` (Status: Proposed).
3. Entry in `config/health/modules.yaml` with `enabled: false` and the removal recipe written **before** the code.
4. Expectations registered (WF-H-02) if the module changes what the model predicts.
5. Code, tests, docs; `MODEL_VERSION` bump and golden regeneration if the model changed.
6. `tools/gates.py` green.
7. On a scratch branch, **run the removal recipe** and confirm gates stay green; note the result in the PR.
8. Pull request; Spencer approves; merge with the flag still off.
9. `enable/<id>` pull request flipping the flag; Spencer approves.

## Flags and what reads them

`config/health/modules.yaml` is read by `health.cli status`, which prints every module
with its state so a disabled module is visible rather than merely absent. A flag that
nothing reads is a `CFG` defect (`05 §3`), the same rule as `docs/02 §3.6` item 4.
