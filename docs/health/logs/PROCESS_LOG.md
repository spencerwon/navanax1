# Process Log — Health

Append-only. One entry per working session, newest at the bottom. An entry records
what was asked, what was done, what was tried and failed, what was decided (with the
ADR), and what is left. Nothing here is edited after the session ends; a correction is
a later entry.

---

## 2026-10-03 · Session 1 · Foundation (Phase 0)

**Operator ask (verbatim intent).** "Deploy agents, validate, track errors, improve on
the system … Logs needed, process recorded, errors mitigated and removed permanently …
Diagram all architecture well … make it easy for me to undo and add onto parts … Use
this chat log for context. It's a way narrower scope version of the beginnings of this
project." Branch: `claude/health-system-architecture-i9qgip`.

**Context recovery.**
- Read the repository's foundation (`docs/00–08`, the fifteen agent definitions, CI, `tools/buglog.py`, `tools/gates.py`, the bug ledger, `config/*.yaml`, the self-test runner).
- Tried to read the shared chat. Failed, for the reasons recorded in ADR-0004 (asset and challenge hosts denied by the environment's network policy; the one remaining route blocked by the session's safety classifier; no device browser exposed). Time spent: roughly the first third of the session. Lesson recorded: when a link is unreadable from the container, check the Operator's artifact gallery first — it held the origin.
- Found and read the Operator's **Metabolic Map** artifact (22 files): engine, parameters, scenarios with pre-registered expectations, knowledge base, verification log, viewer. Adopted it as the origin (ADR-0004).

**Built.**
- `reference/metabolic-map-v1/` — the artifact vendored byte for byte, with a README recording URL and version.
- `docs/health/README.md`, `00_REQUIREMENTS.md`, `02_AGENT_HIERARCHY.md`, `04_ENVIRONMENTS_AND_UNDO.md`, `05_BUG_TAXONOMY.md`, `06_ARCHITECTURE.md`, `07_MODULE_REGISTRY.md`; `01_METHODOLOGY.md` and `03_VALIDATION_AND_TESTING.md` drafted by a builder agent from an outline.
- `docs/health/diagrams/` — eight Mermaid sources and one SVG.
- `docs/health/decisions/ADR-0001..0005`.
- `config/health/base.yaml`, `config/health/modules.yaml`.
- `src/health/errors.py`; `src/health/engine/` (Python port with golden equivalence to the JavaScript reference) and `src/health/kb/` (contract checker, report) and `src/health/cli.py` built by two builder agents in parallel.
- `tests/health_selftest.py`, `tests/health_kb_selftest.py`, `tests/fixtures/health/golden_v1.json`, `tools/health_golden.mjs`.
- `.claude/agents/health-physiology-modeler.md`, `health-literature-curator.md`, `health-rigor-lead.md`, `health-safety-reviewer.md`.
- Gates and CI wired to run both health self-tests stdlib-first and strict after install, plus `kb-check`.

**Decisions.** ADR-0001 (reuse the process), ADR-0002 (Python port + golden
equivalence), ADR-0003 (layout, registry, two-PR enable), ADR-0004 (origin context),
ADR-0005 (shared ledger).

**Validation.** Recorded below in the session's closing entry: test summary lines,
gate output, findings from the adversarial review, ledger entries opened.

**Left open.** `docs/health/00 §11` Q1–Q5; the shared chat's content (ADR-0004); Phase 1
scoping.

---

## 2026-10-03 · Session 1 · Closing entry (validation, review, fix round)

**What ran.** After the foundation and the two builds (engine port, knowledge-base
checker), four reviews ran before anything was called done:

1. **Clinical-safety review** (health-safety-reviewer, Opus): BLOCK, 24 findings on the
   documents and the published V1 surface. Document findings fixed in this session;
   the two on the published surface are Operator decisions (A-2, D-9). Ledger
   BUG-20261003-100..116.
2. **Rigor-lead review of the checker** (health-rigor-lead, Opus): 10 confirmed defects
   and 7 documented-but-unimplemented rules; fixed with regression tests and the 03 §6
   table gained a Status column parsed by a meta-test. Ledger BUG-20261003-105..113.
3. **Adversarial review, four lenses in parallel** (Workflow `wf_6df091b0-a1e`):
   Python/JavaScript equivalence by mutation (45 engine mutants), expectation-harness
   honesty by mutation (20 harness mutants) and independent recomputation of every
   metric, documents-against-code (every number re-measured), and an undo rehearsal
   (every removal recipe executed on a copy). 48 findings, all with a reproduction:
   12 harness, 4 equivalence, 24 documents, 8 undo. The refuter stage (3 refuters per
   finding) agreed with every finding it reached before the stage was stopped to free
   the machine for the fix round.
4. **Fix round, four builders in parallel worktrees by file ownership** (Opus), each with
   failing-first evidence: A harness honesty, B equivalence and numerics, C documents,
   D undo recipes, registry and gates. Merged by cherry-pick in the order B, A, C, D.

**What the fix round changed (measured on the merged head).**
- Engine suite: 23 test functions / 246 checks → 31 test functions / 385 checks (25 s) in the
  default run (`--robust` adds the n = 256 drift gate and chronic convergence, about 2 min).
- Mutation kill rate: harness 7 surviving extractor mutants → 0 (one equivalent mutant
  remains and is explained); engine 8 potassium/sweat, 2 thinning, 3 non-finite and 1
  breakpoint-sorting mutants → all killed; 8 engine mutants remain and each is shown
  equivalent (masked 32-bit arithmetic, guarded branches).
- The harness refuses what it used to score quietly: unknown kinds, malformed or reversed
  ranges, duplicate ids, runs shorter than the scenario, non-default parameters or dt,
  Monte Carlo sets for another scenario or with n < 256. Every row now carries its band
  fields (or the reason there is none), a registry version and a parameter digest. The
  status line says "bands 0 of 10 counted" in plain words when no Monte Carlo ran.
- `simulate()` refuses a non-finite trajectory (`NonFiniteTrajectoryError`, HREQ-V-07); the
  JavaScript reference returns it, a documented deviation.
- Golden fixture: 22 → 24 sections (solver coverage: potassium and sweat runs, outEvery 7
  and 2.5, dt 0.1, unsorted breakpoints; non-finite semantics); every pre-existing section
  byte-identical; 403,140 of 409,600 bytes. Model version unchanged at 1.0.1.
- Documents: every figure re-measured on the port; 01 Appendix A and 03 Appendix B carry
  an enforcement Status on every requirement (W-17); the 03 §12 metrics, the fixture
  table, the cost table (Node and Python columns), ADR-0002 errata.
- Registry and undo: every removal recipe (cli, engine-v1, errors, kb-v1,
  reference-v1, process) executed literally on a copy with the full gates green after each;
  the registry checks itself (`health.registry --check`: one owner per health file, every
  import a declared dependency, every live reference named in a recipe, every test path
  real) and generates `07`; `health.cli status` honours the module flag (a disabled engine is
  never imported); the errors module has its own self-test; the ledger gate gained
  `removed_with_module`, reads tracked files only and rejects placeholder regression
  tests; both "never swallowed" CI steps became a syntax-tree check after the regular
  expressions were shown to flag nothing (BUG-168/169).
- Ledger: 116 → 170 entries; 156 fixed, 14 open. Every fixed
  entry names an existing regression test; the ledger gate now rejects placeholders and
  scans tracked files only.

**Gates on the merged head.** `python3 tools/gates.py` → ALL GATES GREEN at c8bcb5e:
market selftest 202 functions / 1,557 checks; health selftest 31 / 385; kb selftest 29 /
524; errors selftest 5 / 134; ruff clean; ledger 169 entries consistent; secrets clean;
module registry 6 modules, 0 problems; kb-check 0 errors, 6 warnings; golden fixture up
to date (403,140 bytes). `--robust` adds the n = 256 drift gate and chronic convergence
(about 2 min).

**Decisions for the Operator (tracker).** A-1, A-2 (patch the published V1 surface — the
patched page is ready), Q1, D-1, D-4, D-6, D-7, D-8, D-9 (the 1 L water recovery row:
supersede as a known divergence or recalibrate; the range is not widened), D-10 (Baylis
1986 is both the evidence for a counted row and for the parameter that floors it — the
curator decides whether the row is validation), D-11 (Crowe 1987 does not state the
"< 6 h" recovery bound the row registers — the curator finds the source or supersedes the
row), W-12.

**Reconciliation.** After the merge an independent reviewer re-verified the 36 document
lines written ahead of the merge against the merged code, reworded 25 of them where the
merged truth differed (measured figures, test names, what `--robust` alone runs), re-judged
the enforcement statuses (01 Appendix A: 12 Enforced, 12 Partial, 5 Planned, 14 Not
enforced; 03 Appendix B: 9 / 9 / 5 / 2) and committed 78a9966.

**Pull request.** The Operator opened spencerwon/navanax1#21 from the app; its generated
description was replaced with a measured one. Its first CI run went red at the golden
check (BUG-20261003-171: the fixture's recorded Node version was compared as a value);
fixed in the generator with a planted test and pushed.

**Tried and failed, recorded so it is not retried.** The shared chat link (ADR-0004);
the first pushes (403 while the GitHub connection was re-authorised; access returned
mid-session and every commit is on the branch); a timing-sensitive market test
(`test_degraded_reopen_probe_is_not_throttled_by_refresh_seconds`) fails under load
average above about 8 on this 4-CPU box and passes on re-run — flagged, not touched.

**Left open.** M1 scope in `08`: viewer surfaces (BUG-101..104), per-scenario influence
screen (BUG-096), strain-index constants into the table (BUG-095), the JavaScript mirror
of roles and ids (W-13, W-18), reference bump (W-20), robustness run in CI (W-21).

---
