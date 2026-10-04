# Navanax

OpenSea data consolidation, analysis and trade-discovery platform for illiquid NFT markets.

**Status:** Phase 0 — starting the historical record.

## Why Phase 0 is urgent

OpenSea publishes **no historical floor-price series**. The record exists only because we recorded it, and it can only be built forward from the day ingestion starts. Every day this is not running is a day of history that cannot be bought back at any price.

That is the whole reason this repo exists before the dashboard does.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # add your OpenSea API key -- .env is gitignored
python -m navanax.cli ingest
```

Other commands:

```bash
python -m navanax.cli status    # ingestion health, gaps, onboarding progress
python -m navanax.cli verify    # re-verify landing-zone checksums (S0a on mismatch)
python3 tests/selftest.py       # self-test; runs with no dependencies (yaml tests are SKIPPED and listed)
python3 tests/selftest.py --no-skips   # strict: a skip is a failure. What gates.py and CI-after-install run
```

**Viewing the dashboard:** double-click `open-dashboard.command` (or the `Navanax Dashboard.webloc` bookmark). Neither ever starts a dashboard — the background job does that; `dashboard.command` is only for a machine without the background job and refuses if one is already running.

**Deploying merged code:** double-click `update.command`. It fast-forwards `main`, reinstalls only if `pyproject.toml` moved, restarts the three background jobs, and prints the dashboard's own health reading. It refuses — changing nothing — on uncommitted edits, on a checkout that is not on `main` (it prints a sentence to send Claude; it never switches branches itself), or when the pull would not be a fast-forward. It never touches `data/`.

**Settling two assumptions (PR-0):** double-click `probe-events-page.command` — it spends exactly **one** REST read to find out how many events the endpoint really returns for `limit=200`, and refuses if the budget is low — or `probe-two-sockets.command`, which spends **zero** reads and holds two stream connections on one key for ten minutes to find out whether that is permitted and how much a single socket drops. Neither writes to the landing zone or the stores; each appends a dated entry under `docs/measurements/`. See [`docs/04_ENVIRONMENTS.md` §8.12](docs/04_ENVIRONMENTS.md#812-the-two-pr-0-probes).

**Running unattended (macOS):** double-click `autostart-install.command` and the recorder, dashboard and daily trait job start at login and restart themselves after a crash — see [`docs/04_ENVIRONMENTS.md` §8](docs/04_ENVIRONMENTS.md#8-running-unattended) for what it installs, where the logs go, the exit-code contract, and the sleep caveat launchd cannot fix.

## The six things that shape every decision here

1. **Stream-first, REST-rationed.** The free tier allows **120 REST reads/hour**, shared account-wide — one every thirty seconds. (600 was an unsourced assumption repeated across seven documents until one live response falsified it: BUG-20260909-003. The 120 came from `x-ratelimit-limit` on a real reply, and even that arrived on a Cloudflare cache HIT, so it is a measurement with a caveat rather than ground truth.) The WebSocket stream is unmetered. So the stream is the primary path and REST is a budgeted resource behind a governor every call passes through. *Priority ordering between classes is currently emergent from reserve floors rather than a queue — BUG-20260909-016.*
2. **Land before you parse.** Every frame hits the landing zone verbatim before anything reads it. A normalizer bug is then repairable by reprocessing; without this, a parsing bug means data you cannot re-fetch.
3. **Gaps are gaps.** Never interpolated, never forward-filled. Some event classes (cancellations, order invalidate/revalidate, metadata updates) cannot be backfilled at all after a disconnect and are recorded as permanently lost.
4. **The historical record is append-only.** Corrections supersede; they never edit. No agent at any authority level may modify it.
5. **Spencer approves every pull request.** No auto-merge, ever.
6. **A surprisingly good result is evidence of a bug, not an edge.**

## Layout

```
config/       thresholds, watchlist, intervals -- never in code (REQ-N-09)
config/health/   the health subsystem's tolerances, seeds and module registry
docs/         the contract: requirements, methodology, agents, validation,
              environments, bug taxonomy, time/units, storage
docs/health/  the health subsystem's contract, architecture diagrams, ADRs, process log
.claude/      agent definitions (15 shared + 4 health), with model tiering for cost control
src/navanax/  errors · codec · landing · governor · opstore · stream · cli
src/health/   the health subsystem's modules, one row each in docs/health/07
reference/metabolic-map-v1/   the Operator's Metabolic Map V1, vendored verbatim
tests/        selftest.py runs with zero third-party deps; tests that need one skip loudly
              the health_*_selftest.py suites follow the same rule
data/         landing zone + stores (gitignored -- irreplaceable, back up separately)
```

## The health subsystem

The same discipline applied to mechanistic models of human physiology, beginning with
the Metabolic Map V1 (water, sodium, the kidney, arterial pressure): every parameter
with a source and a grade, every output with an uncertainty band, every new expectation
registered before its result (V1's were co-developed with the model), one model in two
implementations held equal to 1e-9 on golden trajectories, and every module removable by
a written recipe. **Educational and research only; never
medical advice.** Start at [`docs/health/README.md`](docs/health/README.md).

```bash
PYTHONPATH=src python3 -m health.cli status      # versions, KB counts, grade share, KB contract, expectations, module flags, the disclaimer
PYTHONPATH=src python3 -m health.cli kb-check    # every knowledge-base contract rule; exit 1 on an error
PYTHONPATH=src python3 -m health.registry --check   # every module: fields, paths, tests, dependencies, removal recipe
python3 tests/health_selftest.py --no-skips      # engine: golden equivalence, conservation, expectations
python3 tests/health_kb_selftest.py --no-skips   # knowledge base: every rule, each with a planted violation
python3 tests/health_errors_selftest.py --no-skips   # error hierarchy: severities, halt flags, never swallowed
python3 tests/health_app_selftest.py --no-skips      # desktop app: loopback only, the flag, the launchers, the icon
python3 tools/gates.py                           # every line above except `status` (a surface, not a gate), plus golden --check (a counted SKIPPED without Node), plus the repository's own gates
```

Every health module has a flag and a written removal recipe, held to the repository by a
gate: [`docs/health/07_MODULE_REGISTRY.md`](docs/health/07_MODULE_REGISTRY.md).

**Launch the viewer as a desktop app.** Double-click `desktop/MetabolicMap.app` (a black
icon with a white stick figure) or `MetabolicMap.command`. Both run
`python3 -m health.app`, standard library only: it serves `reference/metabolic-map-v1/` to this
computer alone (127.0.0.1), opens it in your browser (or in its own window when the
optional `pywebview` is installed) and prints the disclaimer and the validation status
last. It lands switched off: until the Operator approves the one-line `enable/desktop-app`
pull request, it refuses and says why ([ADR-0007](docs/health/decisions/ADR-0007-desktop-app-packaging.md)).
To keep it in the Dock, drag it there from `desktop/`; moved out of the checkout it cannot
find the viewer.

```bash
PYTHONPATH=src python3 -m health.app   # the same from a terminal; --no-open serves only, --browser skips the window
```

## Storage, in one line each

| Store | Tech | Holds |
|---|---|---|
| Landing zone | `.jsonl.zst` | Every raw frame, verbatim. Also the replay corpus |
| Analytical | DuckDB + Parquet | Normalized events, metrics, series — everything queried |
| Operational | SQLite | Disposable bookkeeping only. No market data, no primary records |

## Reading order

Start at [`docs/README.md`](docs/README.md). Any new agent session reads docs 00–03 before working, then states its role, authority level, and permitted data partitions.

## Testing

`tests/selftest.py` is the real suite and covers the logic that is genuinely tricky: frame-flush crash recovery, manifest integrity and tamper detection, event-time range resolution across arrival-hour partitions, budget arithmetic, priority starvation, and out-of-order stream handling. It is the first thing CI runs, before any dependency is installed.

The *runner* needs only the standard library; some tests do not. 88 of 202 test functions read `config/*.yaml` through PyYAML, declare it with `@needs("yaml")`, and are reported as **SKIPPED** — printed by name, counted in their own column, never counted as passed — when the module is absent. Run with `--no-skips` and a skip becomes a failure: that is the mode `tools/gates.py` uses, and the mode of the CI step that runs *after* `pip install`, so those 88 tests are genuinely executed on every push. Details and the rule against widening a `@needs` declaration: [`docs/03_VALIDATION_AND_TESTING.md` §4.6](docs/03_VALIDATION_AND_TESTING.md).
