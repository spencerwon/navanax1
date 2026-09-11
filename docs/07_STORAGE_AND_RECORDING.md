# Storage Architecture and Stream Recording

**Version:** 1.0 · **Date:** 2026-09-09
**Companion to:** `00_REQUIREMENTS.md`, `04_ENVIRONMENTS.md`

---

## 1. A correction: SQLite is not where the market data goes

You asked whether we connect the streams to SQLite. Not quite — and the distinction matters, because getting it wrong is one of the few decisions that is expensive to reverse.

**There are three stores, and they do different jobs.** SQLite is the smallest of the three and holds none of the market data.

| Store | Technology | Holds | Size |
|---|---|---|---|
| **Landing zone** | Compressed JSONL files on disk | Every raw event exactly as it arrived | Large, grows forever |
| **Analytical store** | DuckDB + Parquet | Normalized events, metrics, time series — everything you query and chart | Large |
| **Operational store** | SQLite | Disposable bookkeeping only: ingestion checkpoints, gap register, alert dedup state, REST budget ledger | Small, a few MB |

The stream writes to the **landing zone** first, always. A separate normalizer reads from there and writes to the **analytical store**. SQLite records *where the ingestion got to* and other small mutable state — it never sees a sale or a listing.

**What does NOT go in SQLite:** trade ideas, idea outcomes, paper positions, paper fills, and the config change log. Those look like "operational state" but they are **primary records that cannot be reconstructed from anything**. REQ-F-26 requires every idea logged permanently with its outcome tracked; REQ-F-36 requires paper positions immutable at creation with post-hoc editing prohibited; §8's data model puts all of them among the bitemporal fact tables. They live in the **analytical store** with the same append-only, bitemporal treatment as market data. The test is simple: *if losing it means losing evidence rather than losing a bookmark, it is not operational state.*

### 1.1 Why not put events in SQLite

SQLite is excellent at what it is for: small transactional reads and writes, one row at a time, with real durability guarantees. That describes the operational store exactly.

It is the wrong tool for the analytical work here, which looks like *"scan forty million events, group by collection, compute the 25th percentile of listing price per trait tier, over eighteen months."* SQLite stores data row by row, so answering that means reading every field of every row even though the query touches four columns. DuckDB stores data column by column and reads only the columns asked for — on this shape of query it is routinely 10–100× faster, and the difference is what decides whether your screener meets the 2-second budget in REQ-N-05 or takes a minute.

DuckDB is also a single file with no server to run. There **is** one real tradeoff, and it needs a stated pattern rather than a hand-wave: DuckDB allows a single read-write process at a time, whereas SQLite in WAL mode supports concurrent readers alongside a writer. This system has a normalizer writing continuously while the dashboard queries.

**The pattern: one writer process owns the read-write connection; every reader attaches read-only.** The normalizer is that writer. The dashboard, screener, and backtester open the database read-only and are unaffected by it. Where a reader must see writes that have not yet been committed, it queries through the writer process rather than opening a second connection. This is a constraint to design around, not a reason to choose otherwise — and it is why REQ-N-07's "<5s stream event to queryable state" is a requirement on the writer's commit cadence.

**One writer is now enforced, not merely documented — and the enforcement has two halves (BUG-20260910-067).** The rule above was a pattern with nothing behind it, and on 2026-09-10 two dashboard processes folded into `data/analytics.sqlite` alternately, ten seconds apart, with launchd killing one of them mid-write each time; the 2.8 GB store ended as `database disk image is malformed`. The first half is a **writer lock**: `Normalizer(..., writer=True)` — the folding writer — takes an exclusive `flock` on `<store>.lock`, next to the sqlite file, for its whole lifetime, and a second process that tries to become the folding writer is refused with `StoreWriterBusyError` naming the holder's pid and how to stop it. A reader passes `writer=False`, opens `mode=ro` (enforced by SQLite, not by intention), takes no lock, and refuses to fold. The traits job is deliberately *not* covered: it writes `tokens`/`traits` from metered REST reads and folds no events, so it takes no lock and instead opens with an explicit `busy_timeout`, waiting out a fold's batch insert rather than failing spuriously. The errno rules are shared with `cli._single_instance`, so a filesystem that cannot `flock` (SMB, some NFS) warns and proceeds rather than stopping the fold. The second half is the **bind-first rule**: `dashboard.serve()` binds its listening socket *before* it constructs the `Dashboard` that opens the store. Ordering is what makes the whole class of failure cheap — a process that cannot get its port exits before it has opened the store at all, so a doomed supervisor retry writes nothing, whatever the lock does. The store is *derived*, which is the only reason 2.8 GB of corruption was survivable: it was rebuilt from the landing zone by renaming the bad file aside, and not one landing byte was touched.

**Because the store is derived, the answer to corrupting it is a rebuild, never a repair — and that is now one click.** `rebuild-store.command` moves `data/analytics.sqlite` aside as `analytics.sqlite.corrupt-<date>` (a rename; it deletes nothing, and binning the old file is the Operator's call) and the dashboard folds a fresh store from the landing zone. It **refuses** while a live pid holds the fold-writer lock, and names that pid: moving a store out from under a writer is how one corrupt store becomes two. The dashboard does not have to be restarted — it comes up *degraded* rather than dying on an unreadable store, retries the open every 60 s, and picks the rebuilt store up on its own (docs/08 §2). The rule underneath all of it: a derived store may be discarded and rebuilt at any time; the landing zone may never be modified, deleted, or forward-filled.

### 1.2 The flow

```
OpenSea Stream (WebSocket)
        │
        ▼
  ┌─────────────────────────────────────────────┐
  │  LANDING ZONE          .jsonl.zst files      │  ← immutable, append-only
  │  raw bytes, exactly as received              │     ALSO the replay corpus
  └───────────────┬─────────────────────────────┘
                  │  normalizer reads
                  ▼
  ┌─────────────────────────────────────────────┐
  │  ANALYTICAL STORE      DuckDB + Parquet      │  ← everything you query
  │  normalized events, metrics, time series     │     bitemporal, append-only
  └─────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────┐
  │  OPERATIONAL STORE     SQLite                │  ← small, mutable,
  │  checkpoints · gap register · alert dedup    │     RECONSTRUCTIBLE
  │  REST budget ledger                          │
  └─────────────────────────────────────────────┘
```

**Land first, normalize second — never normalize in flight.** If the normalizer has a bug, the raw bytes are still on disk and you reprocess. If you normalize on the way in and discard the original, a parsing bug means the data is gone, and given the rate limit you may not be able to re-fetch it at any price. This is the single most important structural decision in the storage design.

---

## 2. Recording format

**Decision: newline-delimited JSON, zstd-compressed, partitioned by date and hour.**

```
data/landing/stream/
  dt=2026-09-09/
    hour=02/
      events-{run_id}-000001.jsonl.zst
      events-{run_id}-000002.jsonl.zst
    hour=03/
      ...
  _manifest/
    2026-09-09.json          # per-file: byte count, event count, sha256,
                             # first/last event_timestamp, gap markers
```

One JSON object per line, exactly as received from the socket, plus a thin envelope recording `received_at`, `run_id`, and a monotonic sequence number. Files roll on size (default 64 MB uncompressed) or on the hour, whichever comes first.

### 2.1 Why JSONL and not Parquet for this layer

Parquet is the right format for the analytical store and the wrong one for the landing zone. Three reasons:

**Append-friendliness.** Parquet writes in batches with a footer at the end of the file; a process killed mid-write leaves a file that cannot be read at all. JSONL is a stream of independent lines, so a truncated file still yields every complete line before the cut.

**This property is only preserved if the compressor cooperates**, and plain zstd does not by default: a `.zst` stream killed mid-frame loses the entire unflushed block, which at a 64 MB roll size could be most of the file. So the writer **must close a zstd frame on an explicit cadence** — every 5 seconds or 1,000 events, whichever comes first (REQ-D-26a). The real worst case is `flush_seconds + flusher_interval` — the interval being `flush_seconds/2` clamped to [0.25, 5.0], so **~7.5s at the shipped setting**, not 5s. That gap is stated because a documented bound that is tighter than the code's actual bound is precisely the shape of BUG-001, BUG-003 and BUG-006. A frame boundary is a recovery point: everything up to the last completed frame survives a kill, and the loss window is bounded in seconds rather than by the inter-event interval, which is unbounded. This is the difference between the crash-safety argument being true and being decorative, and it is the kind of detail that only shows up the first time a laptop sleeps mid-write.

**Schema tolerance.** Parquet requires a declared schema. When OpenSea adds a field — and they will — a Parquet writer either drops it or needs a migration. JSONL stores whatever arrived. Since the entire point of the landing zone is *preserving exactly what was received so a future bug can be repaired by reprocessing*, a format that silently drops unrecognized fields defeats it.

**Byte fidelity.** The landing zone's job is to be the record of truth about what the API actually sent. JSONL keeps it verbatim.

Parquet earns its place one layer up, where the schema is ours, writes are batched, and columnar scans are the whole point.

### 2.2 Why zstd and not gzip

Measured on 20,000 synthetic events with realistic entropy — varying addresses, token IDs, and order hashes, which is what actually dominates these payloads:

| | Ratio |
|---|---|
| gzip-6 | 5.6× |
| zstd-9 | ~6× |

The ratio difference is modest, and at these settings so is the compression speed — zstd-9 and gzip-6 have broadly comparable throughput. The real arguments are **decompression speed**, where zstd is several times faster regardless of level and which is paid every single time a test suite replays a session, and **tunability**: the writer can drop to zstd-3 for cheap real-time writes and a governed archive pass can use zstd-19, from one format with one library. The frame cadence above also gives replay a coarse seek: a reader can skip whole frames without decoding them. (Note this is ordinary frame-boundary skipping — the zstd *seekable format* is a separate contrib extension not exposed by the mainline Python binding, and nothing here depends on it.)

**A caution about compression estimates.** A naive benchmark on repeated identical events reports ~185×, which is meaningless — it is measuring the compressor finding duplicate lines. Real event streams are full of high-entropy hex (addresses, order hashes, image URLs), and **5.6× is the honest planning number.** Budget against 5.6×, not 185×. The frame-flush cadence in §2.1 costs a little ratio by resetting the compression window; measure the real figure in week one and update this table.

### 2.3 Sizing

> **MEASURED 2026-09-09 (BUG-20260909-036).** The first two minutes of live
> ingestion falsified every number that was in this table. What follows is the
> measurement, its provenance, and its limits. The previous table -- 2,000
> events/day for Argonauts, 1,150 bytes/event, 5.6× compression -- was an
> estimate that had never been sourced, and it was wrong by three orders of
> magnitude in the direction that matters.

**What was measured** (`data/landing/stream/dt=2026-09-09/hour=10/`,
run `20260909T102050Z-bc378c2b`, 122 s of steady state after excluding the
30 s connect burst):

| Quantity | Measured | Was assumed |
|---|---|---|
| Event rate, Argonauts, steady state | **47.6/s mean, 49/s median, 58/s p90, 68/s max** (7,240 events / 152 s) | 2,000/day ≈ 0.02/s |
| Raw bytes per event (envelope + verbatim frame) | **2,205** | 1,150 |
| Compression ratio, zstd-9, 5 s frames | **13.8×** (14.1 MB → 1.02 MB) | 5.6× |
| Stored bytes per event | **~160** | ~205 |
| Event mix | `item_received_bid` 51%, `item_cancelled` 46%, `order_invalidate` 2%, `collection_offer` <1% (of 7,240) | not estimated |
| Source concentration | 10 distinct makers; the top 3 placed **88%** of events (644 of 728 in the first file) | not estimated |

**What it implies, if the rate held** (it will not hold uniformly -- see limits):

| Scenario | Events/day | Raw/day | Stored/day | Stored/year |
|---|---|---|---|---|
| Argonauts only, at the measured rate | ~4,100,000 | 9.1 GB | **660 MB** | **~240 GB** |
| Argonauts only, at one-tenth of it | ~410,000 | 0.9 GB | 66 MB | ~24 GB |
| 25 collections, if each were like Argonauts | ~100,000,000 | 230 GB | 16 GB | ~6 TB |

**Limits of the measurement, stated so nobody plans off it blindly:**

- **One sample, two minutes, one hour of one day.** Bot bid churn is likely to
  vary by hour and by day; the daily figure is an extrapolation, not a count.
  Replace it with a 24-hour measurement before any procurement decision.
- **One collection.** Argonauts is evidently bot-market-made; there is no
  evidence yet that other collections churn at anything like this rate. The
  25-collection row is the assumption the old table made, applied to the new
  rate, and it is labelled as such.
- **46% of the flow is `item_cancelled`** -- REQ-D-09a IRRECOVERABLE. This is
  the strongest evidence yet for the stream-first architecture: nearly half of
  this market's activity cannot be fetched by any REST call at any budget.
- Compression at 13.8× is *better* than planned because bot bids are
  repetitive. A less bot-dominated collection will compress worse.

**What this changes operationally:**

1. Disk is a real constraint on a laptop within months, not years. The
   `roll_bytes` of 64 MiB raw now rolls a file roughly every **10 minutes**
   (~144 files/day), which the manifest handles but which makes an external
   volume for `data/landing/` worth considering early.
2. Nothing here argues for dropping event classes. The cancellations *are* the
   signal for the price of immediacy, and they are the ones that cannot be
   re-fetched.
3. The first thing Phase 1 should build is the 24-hour rate measurement, per
   collection, so that adding a collection to the watchlist comes with a
   storage cost that was measured rather than guessed.

**The operational consequence is the last row: subscribe per-collection, not to the wildcard firehose.** The old 15 GB/year figure for a 200-collection watchlist is withdrawn -- see the measurement above; the honest per-collection cost is unknown until a 24-hour measurement exists, and for a bot-market-made collection it is two orders of magnitude higher. The firehose figure of 373 GB a year was built on the same falsified estimate and buys you events about collections you are not analyzing. Subscribe to what is on the watchlist; add a collection's subscription when it joins.

### 2.4 Cost

**Zero.** Local disk, open-source formats, no service dependency. If you later want off-machine backup, the compressed landing zone at watchlist scale fits in the free tier of most object storage.

**A re-compression pass is *not* a free action and is out of scope for v1.** Rewriting a landing-zone file at `zstd-19` changes its SHA-256, which the weekly integrity audit (§2.6, REQ-D-28) would correctly flag as an S0a, and modifying landing-zone records is forbidden to every agent at every level (`04_ENVIRONMENTS.md` §3.2). If archive compression is ever wanted, it requires a governed procedure that re-issues the manifest with both the old and new checksums and a signed record of the operation — which does not exist and should not be improvised. At the measured Argonauts rate (~240 GB/year for one collection, §2.3) the pressure to do this will arrive within months, and the governed procedure should be designed before it does rather than improvised when it does.

### 2.5 The landing zone *is* the replay corpus

`04_ENVIRONMENTS.md` §3.1 asks for recorded sessions for integration testing. Those are not a separate artifact — **they are these same files.** The REPLAY data environment is a pointer at a date range of the landing zone.

This is worth stating explicitly because the obvious alternative — a separate "test recording" path — would be both duplicated work and worse: test data that was captured by a different code path is not guaranteed to look like production data. Here they are the same bytes by construction.

### 2.6 Integrity

**A partitioning caveat that matters for replay.** Files are partitioned by *arrival* hour, but REQ-D-07 orders events by `event_timestamp`, and the stream delivers out of order. A REPLAY range expressed purely as a landing-zone date range will therefore clip late-arriving events at its boundaries. The manifest records each file's first and last `event_timestamp` precisely so a replay can widen its file selection to cover the requested event-time window; readers **must** use the manifest rather than filename partitions to resolve a range.

Every file's SHA-256 goes in the daily manifest when it is closed. The weekly historical integrity audit (`03_VALIDATION_AND_TESTING.md` §3.4) re-verifies them. A checksum mismatch means the landing zone was modified, which is an S0a — the append-only guarantee is what everything else rests on, and a silent modification invalidates the reprocessing path that is the whole reason the landing zone exists.

---

## 3. When REST actually binds

You asked in what circumstances the rate limit is a problem. It is not a constant tax — steady-state monitoring of a subscribed watchlist is nearly free, because the stream carries it. REST binds in six specific situations, and it is worth knowing them because they are all *plannable*.

### 3.1 Cold start on a new collection — the worst one

The stream tells you what happens *from now on*. It tells you nothing about what already exists. Adding a collection means fetching its token set, traits, and current listings from REST.

Argonauts has 9,210 items. The NFTs-by-collection endpoint accepts `limit` 1–200 with cursor pagination (verified against the API reference on 2026-09-09), so that is **47 requests for the token list alone**, before traits or listings. Realistically 60–100 requests to onboard one collection properly.

At 120/hour (measured), onboarding a 25-collection watchlist is **2,000+ requests — three to four hours of doing nothing else.** Onboarding 200 collections is a multi-day operation.

*Mitigation:* onboard collections deliberately, a few at a time, at `BACKFILL` priority overnight. Treat adding a collection as a scheduled operation, not something that happens instantly when you click a button. The UI should tell you a new collection is "backfilling, 40% complete, ~2 hours remaining" rather than appearing broken.

### 3.2 Historical backfill

Everything before you started streaming has to come through the events endpoint, 200 per page. A collection with 30,000 lifetime events is 150 requests. This competes directly with §3.1 for the same budget.

*Mitigation:* backfill is inherently a background, low-priority, multi-day process. Prioritize recent history — the last 90 days matter far more for a trait model than the collection's first month.

### 3.3 Reconciliation at scale

Checking every watchlist collection hourly means 200 reads/hour forever — a third of the budget, permanently, just for self-checking.

*Mitigation:* rotate. Check a subset each hour so every collection is reconciled daily rather than hourly. `03_VALIDATION_AND_TESTING.md` §3.3 already specifies this; the arithmetic is why.

### 3.4 Gap recovery after a long outage

Your laptop sleeps for twelve hours. The gap detector wants to backfill twelve hours across every watchlist collection, and that may exceed a full hour's budget — possibly several.

*Mitigation:* prioritize gap recovery by collection importance, accept that some gaps stay unfilled, and **record them honestly as gaps**. Note that some event classes (cancellations, order invalidate/revalidate, received bids, metadata updates) have no REST backfill at all and are permanently unrecoverable — REQ-D-09a.

### 3.5 Interactive use competing with background jobs

You are clicking around the dashboard wanting fresh data while the backfiller is running. Without arbitration, whichever asks first wins, and it will usually be the background job because it never stops asking.

*Mitigation:* this is exactly what the governor's `INTERACTIVE` priority class is for. When you are waiting, you go first, and background work is starved until you stop.

### 3.6 Things the stream simply does not carry

Token metadata and traits, collection-level aggregate stats, holder lists, and anything about a collection you are not subscribed to. These are REST-only by nature.

*Mitigation:* traits change rarely — fetch once at onboarding, refresh on the `item_metadata_update` stream event rather than on a timer. This turns a recurring cost into a one-time one.

### 3.7 And two structural drains

**Testing against the live API** (§2.5 solves this — replay costs nothing) and **multiple environments sharing one bucket** (`04_ENVIRONMENTS.md` §2.2 — a separate OpenSea account for STAGE is the clean fix).

### 3.8 When it does not bind

Worth stating plainly, because the constraint is easy to over-fear: **once a collection is onboarded and subscribed, receiving its market data costs zero REST.** Every listing, sale, offer, and transfer arrives on the unmetered stream, and a hundred collections generating a hundred thousand events a day cost nothing to ingest.

Steady state is not *literally* zero, though — §3.3's reconciliation is mandatory and permanent (REQ-D-10), so watching N collections costs N reads per full rotation cycle. At a daily rotation for 200 collections that is roughly 8 reads/hour against a 120/hour (measured) budget: about 1.4%, versus the 33% an hourly rotation would cost. **Cheap and bounded, not free.**

The rate limit is therefore overwhelmingly a **startup and catch-up** constraint rather than a running one. That is why the architecture front-loads the pain: onboard deliberately, then run at a small, predictable, permanent cost.

### 3.9 The escape hatch

OpenSea operates a rate-limit increase request form for eligible users, and paid tiers exist. If §3.1 proves genuinely blocking at the universe size you want, that is the moment to ask — not before, because a request backed by "here is our measured usage and why we need more" is a better request than a speculative one.

### 3.x Redundant stream (PR-10, default off)

*It sits in §3 because it is the one thing in this document that changes the shape of the landing zone, and because §3.8's "steady state costs zero REST" is exactly as true with two connections as with one — the second socket is unmetered too.*

**The flag.** `config/base.yaml` → `stream.redundant.enabled`, **false by default**. Off means off: one process, one landing root, one `.ingest.lock`, a byte-identical landing envelope and a byte-identical manifest. `tests/selftest.py::test_pr10_flag_off_changes_nothing` holds it there, and it checks the number of connections the process actually opened rather than the configuration that was supposed to produce it.

**The shape when it is on.** Two *processes* — `navanax ingest` and `navanax ingest --redundant` — with two landing roots (`data/landing/`, `data/landing-b/`), two `.ingest.lock` files, two run ids, and **two API keys**. Two writers on one landing root silently lose manifest records: 295 of 600 gap records, with the integrity audit reporting clean. Two roots with one writer each keeps §1's single-writer rule intact twice over, and the normalizer — still the only read-write process on the analytical store — folds both.

**Two keys, created on different days, is not a nicety.** Free instant keys expire after seven days (REQ-D-06). Two processes sharing one key therefore fail *simultaneously*, on a known schedule, at precisely the moment redundancy was bought to cover something. `ingest --redundant` **refuses to start** if `OPENSEA_API_KEY_2` is missing or equal to `OPENSEA_API_KEY`, and says why.

**What it buys, exactly:** the reconnect window of one connection, covered by the other — 1–3 s per clean drop, 21–45 s per silent half-open one, which at the measured Argonauts rate is ~1,900 events, about half of them cancellations that no REST call can ever fetch back (REQ-D-09a). **What it does not buy:** anything at all against a server-side outage, a sleeping laptop, a power cut, or a key expiry. On one laptop the correlated failures dominate, and this is a process- and connection-level mitigation only.

**What it costs, which is the half to read first.** Disk **doubles** — ~660 MB/day becomes ~1.3 GB/day for one collection at the measured rate, ~480 GB/year. And **every raw count over `events` doubles with it**, because `events` is a faithful fold of two append-only stores and a row is never deleted from it. `order_criteria` doubles too: its primary key is `(run, seq, idx)`, which is per-connection by construction (E-W5). The COVERS join still resolves correctly — nothing is *wrong* — but the first "trait offers by criteria" figure read raw off that table would be 2×.

**Where the duplicate is resolved: inside `order_lives`, never in a view.** `metrics.py` carries `INDEXED BY ix_events_lifecycle` on three lifecycle queries — the hint that turned 29 s of bid lifetimes into milliseconds (BUG-20260909-040) — and SQLite will not accept `INDEXED BY` against a view, so the obvious `events_dedup` view would have silently reintroduced a fixed S3 (E-W4/C8). `order_lives` is materialised and has its own indexes, so that is where the collapse happens.

**The key, and the row it refuses to merge.** `dedup_key` = SHA-256 over `event_type ‖ order_hash ‖ event_timestamp ‖ tx_hash ‖ contract ‖ token_id ‖ maker ‖ price_wei ‖ quantity`, separated by `0x1F` so no value can forge a field boundary. It is keyed on **`event_timestamp` explicitly and never on the coalesced `valid_at`** (E-W3): `valid_at` falls back to the Phoenix envelope's `sent_at`, which is a per-message push timestamp with no guarantee of agreeing across two independent sockets, and keying on it would leave the duplicate in place and double every count. A row with no `event_timestamp` is therefore **un-dedupable**: it is kept, it is counted (sync stats, `/api/health.dedup`, `order_lives.terminations_undedupable`), and it is never merged with anything. `tx_hash` is in the key because `order_hash` is NULL for `item_transferred`, for some `item_sold` shapes and for `item_metadata_updated` (E-U3), and those are the two types the widening exists to protect.

One further rule the key alone does not express: **one row per dedup key, full stop.** Two rows sharing a key are one event, whichever connections sent them and however many copies each sent.

The rule that shipped first was "a duplicate is one copy per connection" — the maximum over connections — and it was wrong in the commonest redundant shape there is (BUG-20260911-073). B drops, rejoins, and is **replayed** an event A already had: `max(1, 2) = 2`, and one real cancellation is recorded as two. What one-per-key costs instead is stated rather than hidden: a genuine repeat delivery down *one* socket is now recorded as one event. That is an **undercount** — it understates activity, which is the safe direction — and it is unavoidable, because two rows agreeing on `event_type`, `order_hash`, `event_timestamp`, `tx_hash`, contract, token, maker, price and quantity are indistinguishable from one event delivered twice. The ambiguity is not resolved by guessing; it is **counted**: `order_lives` carries `deliveries_a`, `deliveries_b` and `multiplicity_disagreements` (keys two connections each saw, a *different* number of times), the same figures appear in the sync stats and on `/api/health.dedup`, and the monitor warns above `stream.redundant.monitor.multiplicity_disagreement_max`.

**The monitor, and the direction it watches.** Under a healthy pair *most* events are seen twice, so the dangerous failure is the flattering one: a key that is too wide leaves every duplicate in place, every count and every rate doubles, and the backtest improves. `/api/health.dedup.monitor` reports the duplicate fraction **with its counts** over a rolling window and raises `status: warn`, logged once per transition, when duplicates collapse toward zero **while both connections are reporting healthy**. It deliberately does *not* alarm when B is simply down — zero duplicates with a dead B is the correct observation, and an alarm that fires through every restart is one the Operator learns to ignore.

**Counts everywhere else.** Resolving the duplicate in `order_lives` fixes the lifecycle; it does nothing for the dozens of raw `COUNT`s over `events` — the series aggregates, the maker and wallet tables, the event mix, the ledger's total and its chart (BUG-20260911-075). `MetricEngine.multi_connection()` answers, in three index seeks cached for the life of a fold, whether the store holds more than one connection's rows. `dedup_where()` returns the **empty string** when it does not — so on a single-connection store every SQL statement in `metrics.py` is character-for-character what it was, and no query plan, no `INDEXED BY` and no ledger cursor fingerprint moves — and a one-row-per-key filter when it does. Every response that prints an `n` carries `dedup_applied` and `n_undedupable`. A caveat on the Health tab is not a substitute for the number on the tab being read.

**Gaps.** A gap in A that B covered **stays a gap in A**. It keeps its start, its end, its class lists and its open/closed state, it is still returned by `open_gaps()` and `unbackfilled_gaps()`, and it gains exactly one additive field: `covered_by: "b"`, on `gap_register` and on the manifest's `GapRecord`. "A was blind and B was not" is a different fact from "no gap occurred", and only the first one is true. Nothing in this feature closes, shortens, or suppresses a gap.

**What counts as coverage, and why the manifest alone does not** (BUG-20260911-074). A landing file's `opened_at`/`closed_at` is an *upper bound*: `close()` runs on a clean stop and does not run when the machine sleeps, when the process is killed, or when launchd force-restarts it — which are the events that produce gaps in the first place. So the manifest's "still open" interval is widest exactly when the connection was least able to record, and on one laptop the outage that blinds A blinds B: each one's unclosed file would vouch for the other. Coverage is therefore the manifest intervals **minus that connection's own rows in the gap register**, and only a **closed** gap contained **end to end** by one covering interval is annotated. Partial coverage gets no annotation: every consumer of the register treats a gap as a unit, and "60% of it" would be rounded up in somebody's head to "covered".

**Upgrading an existing store.** `order_lives` rows are stamped with `method_version`, the fold rules that produced them — and for one review cycle that stamp was written and read by nothing (BUG-20260911-076). The only full re-fold fired when the table was *empty*, which answers "has this ever been built", not "was it built by the rules this code implements". Since `sync()` refreshes only the orders a pass **touched**, an order whose last event was yesterday would have kept the old rules' counts — a doubled `terminations_seen` — for as long as the store lived. The folding **writer** now re-folds every row on open when the stamp is stale, inside the writer lock, logging the count and the elapsed time, and writes nothing when it is current. A **reader** must not (a second writer on one SQLite store is BUG-20260910-067) and instead reports: `/api/health.lives_method` carries `mixed` with the min and max versions, the top-level `status` goes to `warn`, and the payload says *"Run rebuild-store.command or restart the dashboard."* **Measured: 6.6–6.7 s for 200,000 lives**, against a 30 s budget, so the open path is where it belongs.

**Coverage annotations are revocable, and that is deliberate** (BUG-20260911-077). `record_downtime_gap` runs at the *start* of a run, not at its death, so a connection killed without warning has not yet recorded the gap it is in: for the seconds or hours until it restarts, the register shows no gap for it and its last landing file is still "open". A fold in that window annotates honestly on the evidence it has, and that evidence later turns out to be wrong. So every fold recomputes coverage for every closed gap in **both** directions — setting where the evidence now supports it, **clearing** where it no longer does — and logs each transition once, naming the gap and the reason, with the revocation line saying the gap is UNCHANGED. `covered_by_n` and `covered_by_revoked_since_start_n` (scoped to the running dashboard process, not persisted) are on Health. This is allowed because `gap_register` lives in the **operational** store, which §1 calls disposable and reconstructible and which `close_gap` already updates in place; it is not the landing zone and not the bitemporal record. The invariant that does not move: **no coverage logic ever closes, shortens or removes a gap.**

**Migration.** `events` gains two nullable columns (`conn`, `dedup_key`) and `order_lives` gains two integer counters, all additive. A row folded before PR-10 existed has no `dedup_key` and cannot get one — `valid_at` was already coalesced, so it cannot say whether it carried an `event_timestamp` — and stays un-dedupable, which is the safe direction. `/api/health.dedup.pre_migration_rows_n` counts those separately so a migration is never reported as a property of the market. A re-fold (`reset_for_refold()` then `sync()`) fills them from the landing zone, which is read-only throughout.

---

## 4. Retention

| Data | Retention | Why |
|---|---|---|
| Landing zone | **Forever** | Irreplaceable. Cannot be re-fetched. The reprocessing path depends on it |
| Analytical store | **Forever** | Derived, but recomputing costs hours; and the bitemporal history is required for honest backtests |
| FIXTURE datasets (curated) | **Forever**, in the repo | Small, hand-picked synthetic and excerpted cases covering known edge cases. Distinct from REPLAY, which is a date range of the landing zone and is not copied into the repo |
| SHADOW store | Discard after cutover | Disposable by design |
| Operational store | Rolling, with backup | Small; losing it costs a resync, not data |

**Nothing in the landing zone is ever deleted.** If disk becomes a real constraint — which at the measured rate it will, within months — the answer is colder storage and heavier compression, never deletion.
