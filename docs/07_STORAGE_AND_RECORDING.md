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

Using the **measured 5.6× ratio** and an average event of ~1,150 bytes. (Earlier drafts rounded this to 6×; the table below is computed at 5.6×, and the honest number is the measured one.)

| Scenario | Events/day | Raw/day | Stored/day | Stored/year |
|---|---|---|---|---|
| Argonauts only | 2,000 | 2.3 MB | 0.4 MB | **0.15 GB** |
| 25-collection watchlist | 25,000 | 29 MB | 5.1 MB | **1.9 GB** |
| 200-collection watchlist | 200,000 | 231 MB | 41 MB | **15 GB** |
| Firehose (all of OpenSea) | 5,000,000 | 5.8 GB | 1.03 GB | **376 GB** |

Event-rate figures are estimates and should be replaced with measurements in the first week of Phase 0 — this table is a planning tool, not a finding.

**The operational consequence is the last row: subscribe per-collection, not to the wildcard firehose.** A 200-collection watchlist costs about 15 GB a year, which is free on any laptop and trivially backed up. The firehose costs 373 GB a year and buys you events about collections you are not analyzing. Subscribe to what is on the watchlist; add a collection's subscription when it joins.

### 2.4 Cost

**Zero.** Local disk, open-source formats, no service dependency. If you later want off-machine backup, the compressed landing zone at watchlist scale fits in the free tier of most object storage.

**A re-compression pass is *not* a free action and is out of scope for v1.** Rewriting a landing-zone file at `zstd-19` changes its SHA-256, which the weekly integrity audit (§2.6, REQ-D-28) would correctly flag as an S0a, and modifying landing-zone records is forbidden to every agent at every level (`04_ENVIRONMENTS.md` §3.2). If archive compression is ever wanted, it requires a governed procedure that re-issues the manifest with both the old and new checksums and a signed record of the operation — which does not exist and should not be improvised. At 15 GB/year the pressure to do this will not arrive for a long time.

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

---

## 4. Retention

| Data | Retention | Why |
|---|---|---|
| Landing zone | **Forever** | Irreplaceable. Cannot be re-fetched. The reprocessing path depends on it |
| Analytical store | **Forever** | Derived, but recomputing costs hours; and the bitemporal history is required for honest backtests |
| FIXTURE datasets (curated) | **Forever**, in the repo | Small, hand-picked synthetic and excerpted cases covering known edge cases. Distinct from REPLAY, which is a date range of the landing zone and is not copied into the repo |
| SHADOW store | Discard after cutover | Disposable by design |
| Operational store | Rolling, with backup | Small; losing it costs a resync, not data |

**Nothing in the landing zone is ever deleted.** If disk becomes a real constraint — which at 15 GB/year it will not for many years — the answer is colder storage and heavier compression, never deletion.
