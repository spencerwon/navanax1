# DATAENG — Can gap backfill and the trait finder become streams instead of RESTs?

*Proposal. 2026-09-09, data-engineer. Answers the Operator's question verbatim: "Can we turn this and the trait finder into streams instead of RESTs? Really need more on both of those."*
*Status: **PROPOSED**. Nothing here is built. Companion to `FACTS_2026-09-09_stream_payloads.md` (measurements), `QUANT_2026-09-09_metrics_of_value.md` §5 (REST-read ranking), `docs/07_STORAGE_AND_RECORDING.md`.*

**Basis labels used throughout:** **[OBS]** measured in our own landing zone · **[DOC]** OpenSea's published API surface · **[ASM]** assumption, stated so it can be falsified · **[CODE]** read out of this repository.

---

## 0. The short answer

**Trait finding: yes, to a genuine zero-OpenSea-REST floor — but not via the stream.** The stream can never carry traits. The zero-read path is *on-chain plus the metadata host*, both of which are outside the OpenSea budget, plus a one-time import of your friend's existing cache. The stream's contribution is not discovery; it is a free completeness monitor.

**Gap backfill: partly, and the part that works is prevention, not cure.** A stream cannot replay the past — this is not a limitation we can engineer around, it is what a stream *is*. What a second live stream connection buys is that fewer gaps ever open. That matters enormously here, because of one number:

> **Backfilling one hour of Argonauts through the events endpoint costs ~322 REST reads. Our whole budget is 120/hour.** One hour of blindness costs 2.7 hours of the entire account's REST budget to repair. A twelve-hour laptop sleep costs 3,854 reads — 32 hours of budget, during which you fall further behind than you catch up. [OBS rates × DOC pagination]

For this collection, REST gap repair is arithmetically unavailable at any gap longer than a few minutes. So the honest answer to "can we stream instead of REST" is: **we must, because the REST option does not actually exist at this event rate.**

---

## 1. What the Stream API can and cannot deliver

### 1.1 Cannot — and these are structural, not bugs

| # | Claim | Basis |
|---|---|---|
| 1 | **No history replay.** Subscribing at 10:20 gets you events from 10:20. There is no `since` parameter, no cursor, no replay buffer, no way to ask for what you missed. Phoenix Channels has no such concept and OpenSea exposes none. | [DOC] + [CODE] `stream.py` join is `{"topic":..., "event":"phx_join", "payload":{}}` — the payload has nowhere to put a start point |
| 2 | **No token enumeration.** There is no "list the collection" message. The stream only tells you about tokens something *happened to*. It cannot tell you a token exists, and it can never tell you a token you have never seen does *not* exist. | [DOC] + [OBS] |
| 3 | **No traits on item payloads.** `item.metadata` = `{name, image_url, metadata_url: null, traits: null}`. `metadata_url` is **null**, not merely unused — so the stream does not even hand us the pointer. | [OBS] 85,775 frames, every one |
| 4 | **No collection-level aggregates.** No floor, no listed count, no supply, no holder list. | [DOC] |
| 5 | **The standing book before you connected is invisible.** ~801 listings existed at 10:20 that we will never see placed. Every stream-only book is left-truncated. | [OBS], already documented in QUANT §0 |

### 1.2 Can — and two of these are underused

| # | Capability | Basis |
|---|---|---|
| 6 | **Every item event names its token.** `item.nft_id` = `chain/contract/token_id`; where that is absent (`item_received_bid`) the id is in the Seaport `consideration` entry. **3,170 distinct token ids in 40 minutes, range 4..9999.** | [OBS] + [CODE] `normalize.py::_token_id_from` |
| 7 | **`trait_offer` carries its criteria.** `trait_criteria: {trait_type, trait_name}`, `trait_criteria_list: [...]` (an AND across traits), `numeric_trait_criteria_list`. 16 of 39 had only the list form. | [OBS] — §4 acts on this |
| 8 | **~50% of this market's activity is stream-only at any price.** `item_cancelled` 41,361 + `order_invalidate` 1,529 + `order_revalidate` 1 = 42,891 of 85,775 frames, none of which the events endpoint exposes. | [OBS] + [DOC] REQ-D-09a |
| 9 | **Unmetered.** The WebSocket does not touch the 120/hour bucket. | [DOC] + [OBS] — the governor ledger shows zero reads during ingestion |
| 10 | **Prices in three forms per event**, including a USD figure at the event's own timestamp. | [OBS] + [CODE] `normalize.py::_resolve_price` |

**Assumed, not verified — flagged because a design leans on it:** that OpenSea permits **two simultaneous WebSocket connections on one account**, and that a second connection is not metered against anything. [ASM]. Nothing in the published surface forbids it and nothing promises it. §3 depends on this and it must be tested before the design is built — a ten-minute test, not a research project.

---

## 2. Trait discovery with fewer or zero metered reads

Four options. The table first, the honesty after it.

| | Option | OpenSea reads | Other cost | Coverage | Time to coverage |
|---|---|---|---|---|---|
| **a** | Passive token discovery from the stream | **0** | none | **34.4% at 40 min; 40%–100% at 24 h, and we cannot tell which** | hours to never |
| **b** | On-chain enumeration + `tokenURI` pattern → direct metadata fetch | **0** | RPC provider key; ~1 day dev | **100% of minted ids** | ~1–2 h of wall clock |
| **c** | The existing 47-read list walk (`traits.command`) | **47**, +≤50 fallback | already built | 100% of what OpenSea has indexed | ~24 min of exclusive budget |
| **d** | Import argonauts-explorer's cache | **0** | licensing conversation; ~2 h dev | **8,798 of ~9,212 = 95.5%**, traits *and* rarity | minutes |

### 2.a Passive discovery — real, but it discovers ids, not traits

**What it gives:** 3,170 of ~9,212 token ids (34.4%) in 40 minutes, free, from data we already recorded. [OBS]

**Honest 24-hour projection.** One 40-minute window at one hour of one day cannot distinguish two models, and they disagree wildly:

- *Uniform-draw model* [ASM, and known false]: fit the observed 34.4% to `1 − e^(−λ)` → λ = 0.42 → an effective **3,885 independent token draws** in 40 minutes. Note what that implies: 85,775 events produced only ~3,885 draws' worth of novelty — **each event is worth 0.045 of an independent draw.** That number *is* the bot clustering, quantified. Extrapolated: 1 h → 47%, 2 h → 72%, 6 h → 98%, 24 h → essentially complete.
- *Fixed-active-subset model* [ASM]: the three makers placing 88% of events quote a **fixed** set of tokens. Coverage asymptotes near what we already saw. The only tokens added after that arrive via listings (12/h), sales (4.5/h) and transfers (4.5/h) — ~500 events in 24 h, mostly repeats. 24 h → **35–45%**.

**The truth is between them and the measurement is free.** Count distinct token ids at five-minute marks over one day. If the curve is still climbing near-linearly at minute 40, the first model is closer; if it flattened by minute 15, the second is. **Do this before anyone plans off a coverage number.** I will not put a single figure in a plan when the two defensible models differ by 2.5×.

**Two limits that no amount of waiting fixes:**
1. **It yields ids, not traits.** Passive discovery can replace step 1 of `traits.py` (the 47-read list walk). It cannot replace step 2. [OBS: `metadata_url` is null on every stream payload]
2. **It cannot certify completeness.** You never learn whether token 8,000 is missing from your set or missing from the collection. A discovery method with no denominator cannot tell you when it is done.

**Correct role: a completeness monitor, not a discovery mechanism.** A stream event naming a `token_id` absent from `tokens` is a free, continuous, zero-read alarm that the token table is stale — the trigger to re-run (b). That is genuinely valuable and costs nothing.

### 2.b On-chain — the one that actually reaches zero

Three sub-steps, all off the OpenSea budget:

1. **Enumerate.** `totalSupply()`, then `tokenByIndex(i)` if the contract is ERC721Enumerable; if not, `eth_getLogs` for `Transfer(from=0x0)` (mints) over the contract's block range. The mint-log path gives the **exact** minted id set with a block number attached — that is a stronger provenance than OpenSea's index, which is itself demonstrably incomplete (8,798 indexed vs 9,180 reported supply vs 9,212 — the discrepancy already flagged in `08_ARGONAUTS_EXPLORER_INTEGRATION.md` §5.4).
2. **Infer the URI pattern.** `tokenURI(id)` on ~12 ids spread across the range. If all twelve agree on a template (`https://host/api/token/{id}` or `ipfs://CID/{id}`), use it for all. **Verify, do not assume** — sample twelve, not three, and if any one disagrees, fall back to a per-token `tokenURI` call for every id. Still zero OpenSea reads, just 9,212 `eth_call`s.
3. **Fetch metadata direct from the host.** This path **already exists and is already unmetered** — `traits.py::_fetch_json`, with `is_opensea_host()` refusing to fetch anything on an OpenSea domain so it can never become a hidden metered call. [CODE]

**Is this acceptable under stream-first?** Yes, and I want to be precise about why rather than hand-wave it. REQ-D-05 rations *OpenSea REST*, not all HTTP. The rule exists because of a 120/hour shared token bucket, and an Ethereum RPC does not draw on it. The precedent is already shipped and reviewed: `traits.py` fetches every token's metadata directly off-OpenSea today.

**What it costs, and who runs it.** An Alchemy/Infura free tier key, or a public endpoint. `eth_call` is ~26 compute units; a free tier at ~300 CU/s absorbs 9,212 calls in roughly ten minutes. Public endpoints (`cloudflare-eth.com`, `llamarpc`) work but rate-limit unpredictably and give no support — acceptable for a one-time job, unacceptable for the recurring holder ledger of REQ-D-15..17. **The Operator runs it**, in the sense that it needs a provider account under his name; no agent holds keys of any kind. It is "free on the OpenSea budget, not free overall" — the same honest label QUANT §5.1 puts on the holder ledger, which needs exactly the same RPC key. **Build them together.**

**Hard requirement — provenance.** The id set is `source='chain'`, with `valid_at` = the mint block's timestamp (an id set is true as of a block, not as of when we asked). The traits are `traits_source='tokenuri'` — *not* `source='scrape'`, because metadata fetched from the contract's own declared URI is the token's canonical record, not a page we read off a website; REQ-D-19's "scraped data never drives the engine" rule is aimed at layout-fragile HTML and does not apply here. Both carry `observed_at` and an `ingestion_run_id` like anything else.

### 2.c The 47-read list walk — keep it, demote it

Already built, already governed, already resumable. 47 reads for 9,212 ids at 200/page [DOC pagination, verified 2026-09-09], plus a capped ≤50 OpenSea per-token fallback. 47 reads at 30-second spacing is ~24 minutes of exclusive budget — real, but not ruinous.

Its remaining virtue: it is the only path that needs **no new dependency and no new key**. Keep it as the fallback when the RPC is unavailable, and as an independent cross-check on (b)'s id set — a disagreement between the on-chain mint set and OpenSea's index is a data-quality finding worth having, not a nuisance.

### 2.d The friend's cache — the best value on the table, with three conditions

8,798 tokens with traits, OpenRarity information content, and per-trait floors, dated 2026-09-09. Zero reads. **Coverage 95.5%; 414 tokens short.**

This is a one-time import, not an integration. Conditions, all non-negotiable:

1. **It imports as an observation, not as truth.** `traits_source='argonauts_explorer_import'`, `observed_at` = the file's own mtime (**not** import time — recording a stale snapshot as fresh is how a chart lies), and the import is one append, never a live sync.
2. **The 8,798 / 9,180 / 9,212 discrepancy is recorded as a discrepancy**, not reconciled by picking a favourite. It becomes the first thing (b)'s on-chain enumeration adjudicates.
3. **Licensing settled with the friend before any bytes land in the repo** (`08_...` §5.1).

### 2.e Recommendation — layered, and what the floor really is

| Layer | Action | OpenSea reads | Gets us to |
|---|---|---|---|
| **L0** | Import the argonauts-explorer cache | 0 | 95.5% |
| **L1** | On-chain mint logs → the authoritative id set; diff against L0 | 0 | the true denominator, and the ~414 gap named |
| **L2** | `tokenURI` pattern → direct metadata fetch for the diff | 0 | ~100% |
| **L3** | OpenSea per-token fallback for whatever L2 could not resolve, capped at 50 as `traits.py` already caps it | **≤50, one-time** | the residue |
| **L4** | Stream-driven completeness monitor: any `token_id` not in `tokens` re-triggers L1–L2 | **0, forever** | stays correct as the collection mints |

**The honest floor.** For Argonauts specifically, **zero metered reads is genuinely achievable** — but only because of two contingent facts: your friend's cache exists, and an RPC key is obtainable. Neither is free of effort, and neither generalises automatically.

For a *new* collection with no friend's cache: the floor is **still zero OpenSea reads** via L1+L2 alone, at the cost of one RPC key and ~1–2 hours of wall clock. If the RPC is unavailable, the floor is **47 reads**, not zero, because **the stream cannot enumerate a collection and never will.** That sentence is the whole of what the stream can and cannot do for trait finding.

---

## 3. Gap backfill without REST

### 3.0 The thing that cannot be engineered around

**A stream cannot replay the past.** There is no configuration, no reconnect strategy, and no second connection that recovers an event which occurred while nothing of ours was listening. Every proposal below is about *shrinking the windows in which nothing of ours is listening*. None of them is a backfill.

### 3.a Redundant subscribers — two connections, two landing roots

**The design.** Two independent processes, A and B. Separate WebSocket connections, separate `run_id`s, **separate landing-zone roots** (`data/landing/a/`, `data/landing/b/`), each with its own `_manifest/` and its own `.ingest.lock`. The normalizer reads both roots into the one analytical store.

**Does it violate one-writer-per-store (docs/07 §1)? No — and the reason is precise.** Two writers into *one* landing root would violate it twice over: `cli.py::_single_instance` takes an exclusive `flock` on `<root>/.ingest.lock` and would simply refuse the second process [CODE], and both writers' daemon flusher threads would contend on the shared `_manifest/<date>.json` read-modify-write — the exact collision that lost 295 of 600 gap records in BUG-006/007 [CODE comment, measured]. With separate roots there is **one writer per store and two stores**, both append-only, neither ever edited. The single-writer rule for the *analytical* store is also untouched: the normalizer remains the sole read-write process.

**The dedup key, precisely.** The requested triple `(event_type, order_hash, event_timestamp)` is *nearly* right and fails on one class: `order_hash` is NULL for `item_transferred`, for some `item_sold` shapes, and for `item_metadata_updated` [CODE `normalize.py` reads `p.get("order_hash")` with no guarantee]. A key that collapses to `('item_transferred', '', '2026-09-09T10:31:07Z')` merges two genuinely distinct transfers in the same second. So widen it with exactly the fields that disambiguate those rows:

```
dedup_key = sha256(
    event_type          ‖ US ‖
    coalesce(order_hash,'')      ‖ US ‖
    coalesce(valid_at,'')        ‖ US ‖   -- event_timestamp, server-assigned, identical on both connections
    coalesce(contract,'')        ‖ US ‖
    coalesce(token_id,'')        ‖ US ‖
    coalesce(maker,'')           ‖ US ‖
    coalesce(price_wei,'')       ‖ US ‖
    coalesce(quantity,'')
)                                          -- US = 0x1F, so no field value can forge a boundary
```

Deliberately **excluded**: `observed_at`, `_recv`, `_seq`, `run`, `sent_at`. Those differ between the two connections by construction; including any of them makes the key useless and every count double.

**Do not enforce it with a UNIQUE index.** `events` is a faithful fold of two append-only landing zones; deleting or rejecting a row there discards evidence. Instead:

- add a `dedup_key` column to `events` (additive migration, computed at parse time);
- add a **derived view** `events_dedup` selecting one row per `dedup_key` — lowest `observed_ts`, tie-broken by `(run, seq)`. Lowest-first is not arbitrary: it is the honest "when did we first learn this", and it is the figure `stream_lag` should use;
- **every metric query reads `events_dedup`, never `events`.** That is a one-line change in `metrics.py`'s FROM clauses and a test that fails if anyone writes `FROM events`.

**The bonus nobody asked for, and it is the best part.** The disagreement between A and B is a *measurement we currently cannot make at all*: the fraction of events seen by exactly one connection is a direct estimate of single-connection loss rate. Today we have no way to know what one socket drops. Expose it as `redundancy_disagreement` on the Health view, with its count.

**Failure modes, named:**

1. **Both drop at once — redundancy buys nothing.** Server-side outage; laptop sleep; power; ISP; disk full; **and the one people forget: a shared API key expiring.** Free-tier instant keys die after 7 days (REQ-D-06) and both processes would use the same one — a *guaranteed* simultaneous failure on a known schedule. **Mitigation: two separate API keys with staggered creation dates.** On a single laptop the correlated failures dominate anyway, so state the honest scope: this covers process-level and connection-level failures, and nothing else.
2. **Dedup key too narrow** → distinct events collapse → **undercount**. Direction: understates activity. Detectable by the disagreement metric behaving oddly.
3. **Dedup key too wide** → duplicates survive → **every count doubles, every rate doubles, and a backtest looks wonderful.** This is the S0-class risk and it is exactly the shape the fifth project rule warns about: a surprisingly good result is evidence of a bug. **Mandatory monitor:** the expected duplicate fraction is *high* (most events should be seen twice). If observed duplicates collapse toward zero while both connections are healthy, the key is broken — alarm, do not celebrate.
4. **Disk doubles.** 660 MB/day stored for one connection at the measured rate → **1.3 GB/day, ~482 GB/year, for one collection.** [OBS 160 stored bytes/event × 47.6 ev/s] Not free. This is the real price of the proposal.
5. **A gap in A that B covered is still a gap in A.** It must be recorded in A's register with `notes='covered by run <B>'`. The honest record is "A was blind and B was not", never "no gap occurred". Never silently suppress a gap because the other process happened to be up.

**What it buys, exactly:** the reconnect windows of one process, covered by the other, provided they do not overlap. Per §3.c that is **1–3 s per clean drop and 21–45 s per silent drop** — at 47.6 events/s, a single silent drop is ~1,900 events, half of them irrecoverable. Over a month of ordinary flapping this is the difference between a mostly-complete record and a Swiss cheese one. **What it does not buy:** anything about server outages, key expiry, or history before either connection opened.

### 3.b What REST backfill remains necessary — and the number that decides the design

The events endpoint pages at 200 [DOC, REQ-D-13]. Applying the measured hourly rates to the backfillable classes:

| Class | Events/h [OBS] | Backfillable? |
|---|---|---|
| `item_received_bid` | 63,902 | yes (as `offer`) |
| `collection_offer` | 252 | yes |
| `trait_offer` | 58.5 | yes |
| `item_listed` | 12 | yes |
| `item_transferred` | 4.5 | yes |
| `item_sold` | 4.5 | yes |
| `item_cancelled` | 62,042 | **no** |
| `order_invalidate` / `order_revalidate` | 2,295 | **no** |

**Full backfill: 64,233 events/h ÷ 200 = ~322 reads per hour of gap.** Against 120/hour that is **2.7 hours of the entire account's budget per hour of gap.** A 12-hour sleep: 3,854 reads = **32 hours of budget** — you never catch up, because 12 more hours of gap accrue while you try.

**Now drop `item_received_bid` from backfill: 331.5 events/h ÷ 200 = 2 reads per hour of gap.** A 12-hour sleep becomes **20 reads — ten minutes of budget.**

**That 160× is the whole recommendation.** Backfill `sale`, `listing`, `transfer`, `trait_offer`, `collection_offer`. **Do not backfill item bids.** Record the bid-side window as a recorded gap and leave it a gap. The justification is not budget alone, it is the data-generating process: QUANT §0 establishes that the bid side is a machine-quoted curve whose information is in level changes, with an effective sample size plausibly ~10² against 64k nominal events — a 20-minute hole in it costs a little of an already-tiny effective n. Three missed *sales* is 40% of an hour's valuation-relevant evidence. **Spend the reads where the effective sample size is.**

The bid-side gap must be marked in the register as an explicit class-level exclusion, and every window carrying one must be excluded from any analysis needing order-state fidelity (REQ-D-09a). It is a gap, permanently, recorded as such. **We never forward-fill it.**

**Also true and worth stating:** none of this exists yet. There is no events-endpoint client in the repo [CODE — `grep '/events' src/` returns nothing]. Whoever builds it inherits this arithmetic as its design constraint, not as a footnote.

### 3.c Making the reconnect itself faster

**Provenance note, stated because the request assumed otherwise:** there is **no `data/landing/` in this working copy and no e2e mirror manifest** — `data/` holds only `analytics.sqlite` (0 rows) and `ops.db` (empty `gap_register`, empty `rest_ledger`). The only manifests on this machine are synthetic test fixtures under `/tmp` (one of them, `probeA`, holds 305 gaps that are the BUG-006 race fixture, not observations). The container also lacks `zstandard`, so real `.zst` files would not have been readable anyway. **So the reconnect budget below is derived from code constants and the measured event rate, not from an observed gap distribution.** The observed distribution should replace it as soon as a real manifest exists — and it is the single measurement that would most improve this section.

Reconnect budget [CODE]:

| Phase | Cost | Source |
|---|---|---|
| Detect a **clean** close | ~0 s | the `async for` loop ends |
| Detect a **silent half-open** connection | **up to 40 s** | `websockets.connect(ping_interval=20, ping_timeout=20)` |
| Backoff sleep, first attempt | 0.5–1.5 s | `min(60, backoff) × (0.5 + random())`, backoff starts at 1.0 |
| Backoff sleep, at cap | 30–90 s | backoff doubles to 60; resets only after `stable_seconds=60` of uptime |
| TCP + TLS + WS handshake | ~0.2–1.0 s | — |
| `phx_join` → first event | <1 s typical; 15 s before rejection is declared | `join_timeout=15.0` |

**Common case (clean close): ~1–3 s. Worst common case (silent drop, first retry): ~21–45 s. Flapping at the cap: 30–90 s per attempt.** At 47.6 events/s, that worst case is **~1,900 events, ~950 of them irrecoverable.**

Changes, ranked by payoff per unit of risk:

1. **`ping_interval=10, ping_timeout=10`.** Halves worst-case detection from 40 s to 20 s. Costs two extra frames a minute. One line. **Do this first.**
2. **An application-level idle watchdog — the fix that actually beats the ping timer.** At 47.6 events/s, three seconds of silence on a subscribed topic is a p≈0 event. A watchdog that force-closes the socket after `idle_seconds` of no market frame detects a half-open connection in seconds regardless of what the ping layer is doing. **Must be config-driven and armed per collection**, because a quiet collection is legitimately silent for minutes and a watchdog that reconnects a healthy quiet socket is a self-inflicted gap generator. Arm it only where a measured baseline rate exceeds a configured floor; default disarmed. (REQ-N-09: the threshold lives in `config/`, not in code.)
3. **First retry immediate.** Start doubling from the *second* attempt, not the first. Saves 0.5–1.5 s per drop and cannot hot-loop, because `stable_seconds=60` already gates the backoff reset. Free.
4. **Per-topic rejoin instead of a full reconnect.** Irrelevant at one collection; at 200 it is 200 join frames and a 15 s join window on every blip. Note it, do not build it yet.

**And the point that subsumes all four: §3.a makes reconnect latency almost irrelevant to coverage.** A warm second connection is a reconnect that already finished. Do 1 and 3 anyway — they are one-line changes that reduce the correlated-outage window too.

---

## 4. `trait_offer` criteria — schema, migration, and matching

Currently these fields are parsed by nothing and stored nowhere [CODE], and `metrics.py` compensates by **excluding every `trait_offer` under any trait filter** (BUG-044/045). That is a defensible placeholder and a bad permanent answer: 58.5 trait offers/hour are pure revealed demand for a specific trait, which is precisely the signal QUANT metric 4 is built on.

### 4.1 Schema — a side table, not columns

Columns are wrong here: an offer carries 1..n criteria in an AND, so a column form needs a JSON blob, and a blob cannot be joined against `traits`. A side table joins directly against `traits(collection, trait_type, value, token_id)`, which is already indexed for exactly this shape.

```sql
CREATE TABLE IF NOT EXISTS order_criteria (
    run         TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    idx         INTEGER NOT NULL,   -- position in the AND list; 0 for the single form
    kind        TEXT NOT NULL,      -- 'string' | 'numeric'
    trait_type  TEXT NOT NULL,
    value       TEXT,               -- string criteria: trait_name, VERBATIM
    num_min     REAL,               -- numeric criteria
    num_max     REAL,
    PRIMARY KEY (run, seq, idx)
);
CREATE INDEX IF NOT EXISTS ix_criteria_lookup ON order_criteria(trait_type, value, run, seq);
```

Plus two columns on `events`, and they are load-bearing, not conveniences:

```sql
ALTER TABLE events ADD COLUMN criteria_n         INTEGER;  -- count of STRING criteria
ALTER TABLE events ADD COLUMN criteria_numeric_n INTEGER;  -- count of NUMERIC criteria
```

`criteria_n IS NULL` → not a criteria-bearing order. `criteria_n = 0` on a `trait_offer` → **we saw a trait offer and could not parse its criteria.** That value is the loud-failure marker, and §4.3 explains why the design collapses without it.

### 4.2 Parsing and migration

**Parsing rules** (in `normalize.py::parse_event`, structure only, no judgement):
- `trait_criteria_list` non-empty → one row per element, `idx` 0..n−1, `kind='string'`.
- else `trait_criteria` non-null → one row, `idx=0`, `kind='string'`.
- `numeric_trait_criteria_list` → `kind='numeric'` rows with min/max as reported.
- **`trait_name` → `value`, verbatim and stripped, no case folding.** Same rule `traits.py::parse_attributes` already applies: "Blue" and "blue" are two values until a human says otherwise.

**A validation that must ship with it.** If OpenSea's `trait_name` casing or spelling differs from the metadata's `value`, every match silently returns nothing — a wrong answer that looks like a quiet market. So: for each distinct `(trait_type, value)` appearing in `order_criteria`, assert it exists in `traits`; count the misses and alert. Cheap, and it is the difference between this feature working and appearing to work.

**Migration — additive, and it touches nothing irreplaceable.** In `normalize.py::_migrate`: `CREATE TABLE IF NOT EXISTS order_criteria`, then `ALTER TABLE events ADD COLUMN` for each of the two if `PRAGMA table_info` says they are absent. Existing rows then need populating, and the criteria only exist in the landing zone — so **reset the watermarks and re-fold**. `events` has `PRIMARY KEY (run, seq)` and inserts are `INSERT OR IGNORE` [CODE], so a re-read cannot duplicate events; make the criteria insert `INSERT OR REPLACE` on `(run, seq, idx)` and the re-fold is idempotent.

Re-folding a day is ~4M frames — minutes, not hours. **The landing zone is read, never written.** The analytical store is derived (docs/07 §1), and re-folding it from the immutable record is explicitly not an edit of the record — the same reasoning `_migrate` already documents for `expiration_ts`.

### 4.3 Matching a trait offer to a filter — the definition

Let `traits(j)` be token *j*'s set of `(trait_type, value)` pairs.
Let **C** = the offer's criteria set, an **AND**: the offer bids on any token holding *all* of them. A `collection_offer` is the case **C = ∅**.
Let **F** = `parse_trait_filter`'s output: `{T₁ → V₁, T₂ → V₂, ...}`, **OR within a type, AND across types**.

Define the two token sets:

```
S(C) = { j : ∀ (t,v) ∈ C,  (t,v) ∈ traits(j) }        the offer's actual reach
S(F) = { j : ∀ Tᵢ,  ∃ v ∈ Vᵢ with (Tᵢ,v) ∈ traits(j) } the filter's tokens
```

**Primitive — per token (this is what the metrics actually need).**

> An offer applies to token *j* ⟺ **C ⊆ traits(j)**, i.e. *j* ∈ S(C).

Exact, indexed, no set enumeration. This is verbatim what QUANT §1 metric 1 requires ("best standing `trait_offer` at τ whose criteria ⊆ traits(j)"), and it makes the collection-offer case fall out as C = ∅ ⊆ anything rather than needing a special branch in the code.

**Derived — offer vs. filter as a class.**

> The offer is a bid on **every** token in the filter ⟺ **S(F) ⊆ S(C)**.

Note the direction, because the intuitive one is backwards. What makes an offer count as bid depth for a filter is not that the offer is *confined* to the filter — it is that the offer will buy *any* token the filter selects. Under this definition a collection offer (C = ∅, S(C) = everything) trivially satisfies it for every F, which is exactly the behaviour `metrics.py` already has for collection offers — now derived from one rule instead of special-cased.

Three mutually exclusive verdicts, and they must never be summed into one number:

| Verdict | Condition | Treatment |
|---|---|---|
| **COVERS** | S(F) ⊆ S(C) | counts as bid depth for the filter |
| **PARTIAL** | S(F) ⊄ S(C) but S(F) ∩ S(C) ≠ ∅ | reported separately, **always with `|S(F) ∩ S(C)|` and `|S(F)|`** — never a bare number (project rule 4) |
| **DISJOINT** | S(F) ∩ S(C) = ∅ | excluded, on evidence |

Allocating a fraction of a PARTIAL offer's quantity as depth (e.g. `qty × |S(F)∩S(C)| / |S(C)|`) is a **judgement** — it models the offerer as indifferent among the tokens in reach. It belongs in ANALYSIS with its assumption declared (docs/06 §4), never in the derivation layer. The structural outputs are the verdict and the two counts.

**SQL for COVERS** (`o` is the candidate offer row):

```sql
NOT EXISTS (
  SELECT 1 FROM (/* tokens matching F */) f
  WHERE EXISTS (
    SELECT 1 FROM order_criteria c
    WHERE c.run = o.run AND c.seq = o.seq AND c.kind = 'string'
      AND NOT EXISTS (
        SELECT 1 FROM traits tr
        WHERE tr.collection = :slug AND tr.token_id = f.token_id
          AND tr.trait_type = c.trait_type AND tr.value = c.value))
)
AND (o.event_type = 'collection_offer'
     OR (o.criteria_n > 0 AND COALESCE(o.criteria_numeric_n, 0) = 0))   -- the guard
```

**The guard is the whole safety of this design, and it is one line from being a disaster.** A `trait_offer` whose criteria we failed to parse has **no** `order_criteria` rows, so the `NOT EXISTS` is *vacuously true* and the offer would match **every filter and every token** — silently, plausibly, and in the direction that makes an edge look real. The explicit `criteria_n > 0` clause is what stops it. **This needs a named property test:** *a `trait_offer` with `criteria_n = 0` matches nothing.* Same treatment for numeric criteria: they cannot be evaluated against the string `traits` table, so the verdict is **UNKNOWN**, and UNKNOWN is not TRUE — exclude and count, exactly as `unparsed` works today.

**Net effect on BUG-044/045:** the blanket "exclude every trait offer under a filter" rule is replaced by an evidence-based verdict, with unparseable and numeric-criteria offers still excluded *and now counted* rather than silently lumped in with the rest.

---

## 5. Cost table

| # | Proposal | Metered reads/h, steady state | One-time reads | Stream connections | Disk |
|---|---|---|---|---|---|
| 2.a | Passive token discovery + completeness monitor | **0** | 0 | 0 (rides the existing one) | 0 |
| 2.b | On-chain enumeration + `tokenURI` + direct metadata | **0** | 0 (≈9.2k RPC calls, ~10 min on a free tier) | 0 | ~3 MB traits |
| 2.c | Existing 47-read list walk | 0 | **47**, +≤50 fallback | 0 | ~3 MB |
| 2.d | argonauts-explorer cache import | 0 | **0** | 0 | ~5 MB |
| **2.e** | **L0+L1+L2+L4 layered** | **0** | **0** (L3 caps at ≤50 if needed) | 0 | ~8 MB |
| 3.a | Second redundant subscriber | **0** | 0 | **+1** (2nd API key required) | **+660 MB/day, +241 GB/yr** |
| 3.b | REST backfill, **full** classes | — | **~322 per hour of gap** | 0 | negligible |
| 3.b | REST backfill, **recommended** (no item bids) | — | **~2 per hour of gap** | 0 | negligible |
| 3.c | Reconnect tuning (ping 10/10, idle watchdog, immediate first retry) | 0 | 0 | 0 | 0 |
| 4 | `trait_offer` criteria + `order_criteria` | **0** | 0 | 0 | ~4 MB/day of criteria rows; one full re-fold, minutes of CPU |
| — | **Everything above, together** | **0 steady-state metered reads** | **0–50** | **2** | **~1.3 GB/day, ~482 GB/yr** |

Two figures deserve to be read next to each other: the recommended package costs **zero steady-state metered reads**, and it costs **half a terabyte a year of disk for one collection.** The constraint moves from OpenSea's rate limit to your hard drive, and `07 §2.3` already flags that an external volume for `data/landing/` is worth considering early. That is a better problem to have — disk is purchasable and REST budget is not — but it is not "free".

Recurring reads are unchanged and remain worth buying: QUANT §5.4/§5.5's listings + collection-offers snapshot at **~11 reads/hour (9% of budget)** for a complete two-sided book, and REQ-D-10 reconciliation. Nothing here displaces those; §2 and §3 free up budget *for* them.

---

## 6. Risks, and what I will not do

**Refusals — these are not negotiable and not subject to a deadline.**

1. **No second writer into one landing-zone root.** Redundant subscribers get separate roots, separate manifests, separate ingest locks. The measured cost of getting this wrong is 295 of 600 gap records lost with the verifier reporting clean (BUG-006/007) — an undetectable loss of exactly the records that distinguish "we were not watching" from "nothing happened".
2. **No edit to the landing zone.** Not for dedup, not for re-compression, not to remove a duplicate. Both connections' bytes are kept. Dedup is a *query-layer* decision (`events_dedup`) precisely so it can be revised when the key turns out to be wrong.
3. **No forward-fill of a bid-side gap**, including the ones §3.b recommends not backfilling. A recorded gap, with its class-level exclusion, is the deliverable.
4. **No suppression of a gap in A because B happened to cover it.** Both registers record the truth about their own process.
5. **No transaction, no key custody, no capital.** The RPC provider account in §2.b is the Operator's; no agent holds it.
6. **No shipping of §4's matching without the `criteria_n > 0` guard and its property test.** Without the guard, an unparsed trait offer matches every filter — a wrong answer in the flattering direction.

**Risks I am carrying, stated plainly:**

- **[ASM] Two WebSocket connections on one account may not be permitted.** §3.a rests on it. Ten-minute test, before design.
- **[ASM] The 24-hour passive-coverage figure is unknown by a factor of 2.5.** Do not plan against a single number until the 5-minute-mark curve is measured.
- **A shared API key guarantees correlated failure** on a 7-day schedule (REQ-D-06). Two keys, staggered, or the redundancy is partly theatre.
- **Disk becomes the binding constraint within months** at 2× the measured rate. Foreseen in `07 §2.3`; doubling it brings the date forward.
- **The `trait_name` ↔ `value` casing mismatch** would make §4 return nothing while looking healthy. Validated at import or not shipped.
- **This proposal has been validated by nobody.** Per the third project rule, the agent that builds a thing does not validate it. This is SPECIFIED, not done.

**Escalations for the Operator:**

1. **The 322-reads-per-hour-of-gap figure is a structural finding, not a tuning parameter.** REST gap repair is arithmetically unavailable for this collection at any meaningful gap length. If a complete bid-side record over gaps is genuinely required, that is the moment for `07 §3.9`'s rate-limit increase request — backed by this measurement.
2. **§4 changes data semantics** (trait offers stop being excluded under filters). That requires a shadow-run comparison with every difference explained, before merge.
3. **The friend's cache needs a licensing answer** before it can be imported.
