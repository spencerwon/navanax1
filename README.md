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
python -m navanax.cli audit-token-ids   # read-only: did any stored row take its
                                        # token_id from a Seaport criteria item?
                                        # (BUG-20260909-057; expected answer 0)
python3 tests/selftest.py       # stdlib-only self-test, no dependencies needed
python3 tools/pushgate.py       # is this branch allowed to go to the remote? (never pushes)
```

**Sending a branch to GitHub (macOS):** double-click `push.command`. It re-runs
every check first — clean tree, nothing forbidden tracked, sign-offs from
`tech-lead`, `pm` and `validator` bound to this exact commit, secrets, the bug
ledger, lint, the test suites — prints what would be pushed, and pushes only
after you type `PUSH`. It never merges, never touches `main`, and never uses
`--force`; opening and approving the pull request stays yours. `pull.command`
brings this Mac up to date, fast-forward only. See
[`docs/04_ENVIRONMENTS.md` §4.2](docs/04_ENVIRONMENTS.md#42-getting-a-branch-to-the-remote)
for why the push is a click and not something an agent does.

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
docs/         the contract: requirements, methodology, agents, validation,
              environments, bug taxonomy, time/units, storage
.claude/      agent definitions (11), with model tiering for cost control
src/navanax/  errors · codec · landing · governor · opstore · stream · cli
tests/        selftest.py runs with zero third-party dependencies
data/         landing zone + stores (gitignored -- irreplaceable, back up separately)
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

`tests/selftest.py` runs on the standard library alone and covers the logic that is genuinely tricky: frame-flush crash recovery, manifest integrity and tamper detection, event-time range resolution across arrival-hour partitions, budget arithmetic, priority starvation, and out-of-order stream handling. It is the first thing CI runs, before any dependency is installed.
