# Navanax

Backtester and analysis platform for illiquid NFT markets — data consolidation,
visualization, statistical analysis, and trade discovery on OpenSea.

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
python3 tests/selftest.py       # stdlib-only self-test, no dependencies needed
```

## The six things that shape every decision here

1. **Stream-first, REST-rationed.** The free tier allows 600 REST reads/hour, shared account-wide — about one every six seconds. The WebSocket stream is unmetered. So the stream is the primary path and REST is a budgeted, priority-queued resource behind a governor that every call passes through.
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
