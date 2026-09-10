# 08 — Dashboard

*What the live view shows, where every number comes from, and what it deliberately does not do yet.*

## 1. Running it

Double-click **`dashboard.command`**. It opens `http://127.0.0.1:8765/` in your browser and keeps a Terminal window open; close that window to stop. It runs alongside `start.command` and never touches the landing zone the recorder is writing.

The page refreshes itself every 10 seconds. The store behind it is refreshed from the landing zone every 5 seconds (`config/base.yaml` → `dashboard.refresh_seconds`).

## 2. Architecture

```
navanax ingest  ──writes──▶  data/landing/            (append-only, verbatim frames)
                                   │
                                   │ every 5 s: read complete frames past each file's watermark
                                   ▼
navanax dashboard ──writes──▶ data/analytics.sqlite    (one row per market event, both timestamps)
        │
        └──serves──▶ http://127.0.0.1:8765   (loopback only; refuses any other bind)
```

One writer per store (docs/07 §1). The dashboard process is the only writer to the analytical store; its HTTP handlers query the same connection under a lock, so the page always sees the latest fold.

**Bind first, open the store second — and hold the writer lock while you do (BUG-20260910-067).** `serve()` binds the listening socket *before* it constructs `Dashboard`, because constructing one opens `data/analytics.sqlite` and starts the thread that writes to it. It used to be the other way round: with a second dashboard already on 8765, every launchd retry of the first opened the store, folded new frames into it, and only then died on "Address already in use" — 177 times in half an hour, two writers alternating on one SQLite store with processes killed mid-write, and a 2.8 GB store that ended `database disk image is malformed`. Binding first makes a doomed retry free: the `OSError` is raised before a byte of the store is opened, `navanax dashboard` exits **2**, and `ThrottleInterval: 30` on the dashboard plist keeps the retry slow enough to read in `dashboard.log`. Behind that, `Normalizer(..., writer=True)` holds an exclusive `flock` on `<store>.lock` for the process's lifetime, so a second folding writer that gets past the port is refused by pid rather than admitted; anything that only queries opens `writer=False` (`mode=ro`, no lock, cannot fold). `/api/health` reports who holds the lock and a `PRAGMA quick_check` result cached for ten minutes, so the next such fault is visible on Health before it is fatal.

**And if the store is already malformed, the dashboard comes up DEGRADED rather than dying.** This is the incident's actual end state, and the first fix did nothing about it: `sqlite3.connect()` succeeds on a corrupt file (it is lazy) and the first `PRAGMA journal_mode=WAL` raises `DatabaseError: database disk image is malformed` straight out of `Dashboard.__init__`, past every `except` in `cmd_dashboard`, as a raw traceback and an **undocumented exit 1** — which `KeepAlive` then repeats forever. A corrupt store is precisely when the Operator needs the page, because Health is where the rebuild recipe lives. So `Dashboard._open_store()` catches `sqlite3.DatabaseError` and refuses to fail:

| | while degraded |
|---|---|
| `/api/health` | **200**, with a `degraded` block naming the fault, the store and the recipe; `quick_check.ok` false with the sqlite error; every store-derived count `null`, never `0` — `0` is a claim about a store nobody could read |
| `/api/meta`, `/api/gaps` | **200** — neither reads the analytical store (`gaps` comes from the landing zone's manifests) |
| every other API route | **503** `{"error": "analytical store is malformed", "rebuild": …}`. Not a 500 with a sqlite traceback, and never a plausible-looking empty series |
| `/` and the page | **200** — there has to be somewhere to read all of the above |
| the recorder | untouched. It is a separate process, still landing frames; a rebuild loses no history |

`_loop` retries the open every `STORE_REOPEN_SECONDS` (60 s), so **`rebuild-store.command` is one click**: it renames the store to `analytics.sqlite.corrupt-<date>` — a move, never a delete; binning the old file is the Operator's call — refusing while the fold-writer lock is genuinely held (it asks the lock, not the lock file, and names the pid) and the running dashboard folds a fresh store within a minute, with no restart to remember. `StoreWriterBusyError` is deliberately *not* degraded around — another process owning the store is the wrong process, not a broken store, and that exits 4. `cmd_dashboard` still catches `sqlite3.DatabaseError` as a belt to those braces and maps it to the documented `DASH_EXIT_STORE_MALFORMED = 5`.

**Bound to 127.0.0.1 and nothing else.** `serve()` raises on any other host (REQ-N-13). There is no login because there is no exposure.

## 3. The store (`normalize.py`)

One row per market event, structure only — what OpenSea said, when it said it, and when we heard it:

| column | meaning |
|---|---|
| `observed_at` / `observed_ts` | when **we** learned it (landing envelope `_recv`) |
| `valid_at` / `valid_ts` | when it was **true on the market** (`event_timestamp`) |
| `event_type`, `collection`, `contract`, `token_id`, `order_hash`, `maker`, `taker` | identity |
| `price_wei` | integer as text — never a float |
| `price_eth`, `price_usd` | the order's value, both denominations, at its own timestamp (REQ-F-02) |
| `implied_ethusd` | `usd / eth` — the rate OpenSea used, kept so a second provider can be checked against it later (REQ-D-25) |
| `price_basis` | **how** `price_eth` was derived — see §3.1 |
| `expiration_at`, `quantity`, `tx_hash`, `payment_symbol` | lifecycle and settlement |
| `criteria_n`, `criteria_numeric_n` | how many criteria this order bids on — see §3.2. `NULL` = not a criteria-bearing order; `0` on a `trait_offer` = we saw one and could not read its criteria |

Frames that do not parse go to the `unparsed` table with the reason. Nothing is dropped; the raw bytes are still in the landing zone.

### 3.2 `order_criteria` — what a trait offer bids on

A `trait_offer` names the traits it will buy. Measured shape (FACTS 2026-09-09, 39 offers in 40 minutes): `trait_criteria: {trait_type, trait_name}` (the single form, often null), `trait_criteria_list: [{trait_type, trait_name}, …]` (an **AND** across entries — 16 of 39 offers had only this form), and `numeric_trait_criteria_list` (ranges).

One row per criterion, keyed `(run, seq, idx)` — the `events` primary key plus position, because `order_hash` is nullable. A side table, not a JSON column: a blob cannot be joined against `traits(collection, trait_type, value, token_id)`, and that join is what every match needs. `kind` is `string` or `numeric`. Values are stored **verbatim and stripped, never case-folded** — the same rule `traits.py` applies, so a casing mismatch between OpenSea's `trait_name` and the metadata's `value` stays visible instead of silently matching nothing. `MetricEngine.criteria_coverage()` reports every criterion with no matching trait value, with its count, and alerts.

Under a redundant stream (two connections, one event) the same offer arrives with two `(run, seq)` pairs and therefore **two sets of criteria rows**. The match still resolves correctly — it joins on `(run, seq)` — but any count *over* `order_criteria` doubles. Count over `events`, not over criteria rows.

### 3.3 `order_lives` — the standing book, one row per order

The relation that answers *"was this order live at time τ?"*, folded from `events` at the end of every `sync()` and rebuilt once, automatically, on a store an earlier version wrote. It is a **DERIVATION** (docs/06 §4.2): deterministic, no thresholds, no imputation.

| column | meaning |
|---|---|
| `t_place` / `t_place_observed` | when the order was placed — market time and our time |
| `t_term`, `exit_reason`, `exit_source` | when and how it ended; `exit_reason ∈ {cancelled, invalidated, filled, expired, censored, unknown}` |
| `expiration_ts` | carried from the placement; the standing predicate honours it separately |
| `quantity` | carried from the placement — a collection offer good for 5 is **five units of depth** |
| `placement_seen` | `0` = an orphan: a termination whose placement we never saw |
| `revalidated`, `terminations_seen` | a revalidate re-opened it; how many terminator rows named this hash |
| `event_type`, `scope_kind`, `token_id`, `maker`, `price_eth/usd` | identity, from the placement |
| `criteria_n`, `criteria_numeric_n`, `method_version` | criteria carried from the placement; which fold rules made this row |

The rules, none of them optional:

- **One life per `order_hash`.** Two cancel rows are one termination, terminated by the **first**. Before this, `bid_lifetimes` cross-produced them and its `n` was a count of bid × cancel *pairs* (BUG-049).
- **`order_invalidate` followed by a later `order_revalidate` is not a termination.** The order re-opened (BUG-050).
- **A terminator with `valid_ts IS NULL` is `unknown`** — excluded from the book *and counted*, never left standing forever (BUG-050).
- **Expiry is inferred, never observed.** With no terminator seen and an expiration on the order, the life ends at `expiration_ts` with `exit_source='derived'`. **No row is ever written to `events`** — the market record only holds what OpenSea sent.
- **A termination with no placement is an orphan.** Excluded from durations, counted as `orphan_rate`. `t_place` is never imputed.

`metrics.standing_sql(alias, now_ts)` is the single predicate built on it, and the live book, the screener and bid lifetimes all use it. Before `order_lives` there were **three** different definitions of "ended" in `metrics.py` and a fourth in `cancel_count`.

**A reader sees the last fold.** Only the normalizer writes here (docs/07 §1), and it refolds every touched order on every pass, so a read is at most one sync behind.

### 3.4 Re-folding

The criteria only exist in the raw frames, and the raw frames live in the **landing zone**, not here (docs/07 §1.2). So `_migrate` adds `criteria_n` / `criteria_numeric_n` to an existing store but cannot fill them — they stay `NULL`, which fails the `criteria_n > 0` guard, so a pre-migration trait offer matches nothing until a re-fold. That is the safe direction.

`Normalizer.refold_criteria()` re-parses in place *if* the store holds raw frames. It does not, so the method **refuses and names the recipe** rather than silently doing nothing. The recipe is `Normalizer.reset_for_refold()` followed by `sync()`: it deletes `events`, `order_criteria`, `order_lives`, `unparsed` and `watermarks` — every one reconstructible — and then re-reads the landing zone. It does **not** touch the landing zone (append-only, irreplaceable, docs/07 §4) and does **not** touch `tokens` / `traits`, which came from metered REST reads and would cost ~97 of the 120/hour budget to refetch. `events` is `INSERT OR IGNORE` on `(run, seq)` and `order_criteria` is `INSERT OR REPLACE` on `(run, seq, idx)`, so the re-fold is idempotent even if interrupted.

### 3.1 The price rule, and why it exists

Found in the first hour of real data, not in any document: on a **bid or listing**, `payment_token.eth_price` is the *order's value* (a 0.88 WETH bid reports `"0.88"`). On a **sale**, the same field is the *token's exchange rate* (a 1.43 WETH sale reports `"1.000863"`, the WETH/ETH rate). A parser that trusted the field would record that sale at 1.00 ETH — a 30% error, silent, in the event type every metric cares about most.

So the field is checked, not trusted: `units = wei / 10^decimals` is always the payment-token amount. If `eth_price ≈ units` it is a value (`price_basis = order_value`). If not, and it looks like a per-unit rate, the value is `units × rate` (`units_x_rate`). Rows where neither fits are kept with `reported_value_unverified`, so an audit can find every one. Regression test: `test_normalizer_parses_real_frames`.

## 4. The metrics (`metrics.py`) — the MetricRequest contract, implemented once

Every chart on the page is the tuple from docs/06 §3: `{metric, collection, denomination, transform, interval, range}`. The page contains no calculations of its own.

- **Intervals** come from `config/intervals.yaml` — every one you listed, and nothing hard-coded. An interval not in that file is refused, not improvised.
- **Ranges** are trailing (`1h`…`30d`) or anchored (`HTD`/`DTD`/`MTD`/`YTD`), and anchored ranges start on **display-timezone** boundaries (`config.display.timezone`), because "today" means your today. Buckets of a day or longer align the same way; sub-day buckets are UTC.
- **Denominations** `ETH` / `USD` select the event's own value at its own timestamp.
- **Transforms** `ABS`, `PCT`, `LOG`, `DIFF`, `BPS` per docs/06 §2.2. Baseline is the first non-null value in the window, and the response says what it was and when.
- **Every response carries its basis** — metric, label, denomination, transform, baseline value and time, window, interval, timezone, `wash_filter`, `as_of`, bucket count, undefined-bucket count, and **`book`** (§4c: which book a price metric was read off). The page prints it under each chart.

Metrics available now (all *observed* quantities — no fair value, no smoothing):

| metric | definition | book |
|---|---|---|
| `floor_ask` | lowest **standing** ask over the bucket, time-weighted | `standing` (default) |
| `collection_bid` | highest **standing** collection offer over the bucket, time-weighted | `standing` (default) |
| `top_item_bid` | highest `item_received_bid` seen in the interval | `observed` only — see §4c |
| `immediacy_cost` | standing lowest ask − standing highest collection offer, both legs on the same τ (REQ-F-13a, docs/01 §3.2), also as % of ask. **Undefined when either leg is absent** — drawn as a hole, never filled | `standing` (default) |
| `trait_set_series` | not a `series()` metric but its own call (§4a.1): the trait group's lowest standing ask, its highest standing bid over three named legs, the unfiltered baseline, and per-clause floors | `standing` only |
| `sale_price`, `volume`, `sales_count` | median / sum / count of `item_sold` | n/a — flow, not a book |
| `listing_count`, `bid_count`, `cancel_count`, `event_count` | counts | n/a |

### 4c. `book=standing` vs `book=observed` — the difference, and why the default moved

Set by the Operator on 2026-09-10: **floors and lowest-ask lines are built from standing asks only. A bucket with no live ask is a hole** — not a zero, not the last price seen.

Until BUG-20260910-057 every price metric was an **interval extremum**: the lowest ask *seen* in the bucket, the highest offer *seen* in the bucket. For `immediacy_cost` the two were then subtracted, and **the two legs need never have coexisted.** That is not the quantity docs/01 §3.2 defines — *"what you pay to force time-to-clear to zero by hitting the **standing** collection offer"* — it is biased **narrow**, the bias grows with the interval, and because the legs are unrelated in time it can come out **negative**, which the KPI card would show as a free arbitrage.

`book=standing` (the default for the three metrics above) reads the resting book off `order_lives` (§3.3) instead:

- The book is **sampled at every event time** inside the bucket — a sweep line over placements and terminations — not at bucket boundaries. Boundary sampling would miss a listing that appeared and was cancelled inside one bucket, which on this collection is most of them.
- The bucket's value is the **time-weighted median** of the extremum: the level that actually held for most of the bucket, not a single extreme that may have lived for one second of the hour.
- Every point carries **`coverage`** (seconds the leg stood ÷ observable bucket seconds — for `immediacy_cost`, seconds *both* legs stood) and **`n`** (distinct orders that were standing). A floor with coverage 0.05 and n = 1 is a different object from one with coverage 1.0 and n = 40, and the response says which you have.
- **`[p10, p90]`** accompanies the median, and is **withheld** below `min_n_for_percentiles` distinct orders (REQ-F-19). The median is not withheld: it is the level of a continuously observed step function and is a fact at n = 1. The band is a claim about a *distribution*, and machine quotes are not independent observations.
- **A crossed book is an alarm, not a data point.** If `ask < bid` at any τ inside a bucket, the whole bucket is `null`, counted in `basis.negative_buckets`, and logged once. A crossed book means the reconstruction is wrong — a stale ask, a misparsed price, a left-truncated leg — and publishing the median of the remaining τ would hide it behind a plausible number (project rule 5).
- **Leg discipline** (quant §1 metric 1). The bid leg of `immediacy_cost` is a **collection offer and nothing else**. A collection offer carries no token and applies to every one of them, so it is by construction a bid available on whichever token is at the floor. Item bids and trait offers are per-token and per-criteria; maxing over all three would pair the ask on one token with the bid on another, and *"mixing those populations is how a spread goes negative."* A union bid leg is a separate metric (PR-5).
- **The standing book is LEFT-TRUNCATED and every response says so.** We only see orders whose *placement* we witnessed. Argonauts had ~801 standing listings before recording started, so the reconstructed floor is an **upper bound** on the true floor and the reconstructed best bid a **lower bound** on the true best bid: a stream-only spread is biased **wide**. `basis.left_truncated` and `basis.left_truncation_note` carry this on every standing series. Seeding the ask side from a REST listings snapshot is the fix, and it is a later PR.

`book=observed` returns the **old numbers, unchanged**, and labels them: `basis.book = "observed"` plus an `observed_book_warning` saying what the number is not. It answers a different question — what was seen changing hands in this interval — and it is the comparison line that makes the size of the correction visible. Do not quote it as the spread.

`top_item_bid` deliberately has **no standing variant yet**: an item bid applies to one token, so a standing "highest item bid" is not interchangeable with a collection-wide leg, and it belongs with the `highest_bid(S)` work where the Operator's leg-discipline decision applies. Asking for `book=standing` on it is refused with that reason rather than silently answered.

API: `/api/series?...&book=standing|observed`. Omit it and each metric uses its own default.

Non-series views, all reading the one standing-book relation (§3.3): the **live book** (`standing_sql` at `now`, with **depth in units** — a quantity-5 offer is five), the **sales tape**, **makers** (who is generating the flow), **bid lifetimes** (one life per order, ended durations plus the censored, `unknown`-terminator and **orphan** counts), and the **event mix**.

**Percentiles are withheld, not flagged** (REQ-F-19, docs/00:199). Below `min_n_for_percentiles` = 30, `bid_lifetimes` returns `None` for **all three** of p10, median and p90 — a median is a percentile too — together with `n`, `min_n_for_percentiles` and `percentiles_withheld` so the caller can say *why* the number is missing. Returning the number with a `percentiles_reliable: false` beside it did not make quoting it impossible, which is what the requirement asks for; a number on screen is a number that gets quoted. One helper, `metrics.pct()`, enforces it for every caller.

Bid lifetimes still *counts* censoring rather than modelling it, so the percentiles are biased short; Kaplan–Meier with competing risks reads these same rows and is a later PR. The median may move in **either** direction from the old number (9 s), because the old estimator was biased short by dropped censoring and long by the cross-product join — a large change is the expected consequence of two known defects, not a discovery.

`wash_filter` is always `raw` and the response says so: no wash-trade filter exists yet, and the page must not imply one.

### 4d. `survival` — how long an order stands, estimated instead of counted (PR-8)

`MetricEngine.bid_lifetimes` **counts** censoring; `MetricEngine.survival` **models** it. The panel on the page reads the second one. Both still exist: `bid_lifetimes` is the naive comparison and is not on screen.

**The call.** `survival(collection, start, end, as_of, kind='item_received_bid', traits=None, maker=None, price_band=None)`, served at `/api/survival`; the drill list is `survival_drill(...)` at `/api/survival_drill?lo=&hi=`. Both take the same filters, so a drill list is always the same population as the bar that opened it.

**`as_of` is mandatory, and it is the rule that makes the estimator correct.** `order_lives.exit_reason` is stored **as of the fold** (§3.3), not as of the question. A life folded at 14:00 says `cancelled` even when the question is "what did the book look like at 11:00". Reading it straight would mark an order as ended an hour before it was — the estimator would learn the future. So:

- **ended** ⇔ `exit_reason` is one of the four causes **and** `t_term ≤ as_of`;
- everything else placed at or before `as_of` is **censored at `as_of`**, with duration `as_of − t_place`;
- a life placed *after* `as_of` did not exist yet — excluded, counted as `placed_after_as_of_n`;
- a life whose terminator could not be placed in time (`unknown`) is **neither**: calling it censored would say it stood when we know it did not, and calling it ended at `as_of` would invent a time. Excluded, counted as `unknown_terminator_n`.

**The estimator** (quant §3.2 — library-free, four recurrences, `1.96` the only constant):

| quantity | formula | note |
|---|---|---|
| survival | `Ŝ(t) = Π_{tᵢ≤t} (1 − dᵢ/nᵢ)` | Kaplan–Meier. Censored lives stay in `nᵢ` up to their censoring time and never enter `dᵢ` |
| Greenwood | `v(t) = Σ dᵢ/(nᵢ(nᵢ−dᵢ))`, `Var[Ŝ] = Ŝ²v` | **diagnostic only** — see the band below |
| band (log-log) | `σ = √v/|ln Ŝ|`, `[Ŝ^exp(+1.96σ), Ŝ^exp(−1.96σ)]` | not `Ŝ ± 1.96·SE`, which leaves [0,1] in the tails. Returned as `null`, never clamped, where it is undefined (`Ŝ = 1`, `Ŝ = 0`, `nᵢ = dᵢ`) |
| competing risks | `F̂_c(t) = Σ_{tᵢ≤t} Ŝ(t_{i−1})·dᵢᶜ/nᵢ` | Aalen–Johansen, causes `cancelled · invalidated · filled · expired`. **Not `1 − KM` per cause**, which censors the competitors and overstates every cause. `Σ_c F̂_c(t) = 1 − Ŝ(t)` exactly, at every `t`, and that identity is a test |
| RMST | `Σ Ŝ(t_{i−1})·(min(tᵢ,τ*) − t_{i−1})` | reported **with** `P(not ended by τ*) = Ŝ(τ*)`. A τ* past the largest observed duration is **refused**, not extrapolated by extending the last step flat |
| residual | `Ŝ(t+L)/Ŝ(t)` | reported only where both ends are inside the data |

**The band on screen is a maker-episode cluster bootstrap, not Greenwood** (factcheck D-W5). Greenwood assumes independent observations; 42,601 quotes from 3 makers are not, and an order-level band would be roughly `√(n/n_eff)` — about 15× — too narrow. The bootstrap resamples **episodes** (ASM-022): same maker, same scope (`scope_kind`, `token_id`), consecutive quotes less than ε apart. `B`, the seed and the number of clusters resampled travel with the band; the seed makes it reproducible, so the band on the page is the band in the test. Where Greenwood and the bootstrap disagree, **Greenwood is the one that is wrong on this data**.

**`n` is not one number and reporting one is the failure mode** (quant §3.3). The header prints all of them, always: `n = <ended> ended · <censored> still standing (censored) · n_eff = <clusters>`, plus ε and the minimum. **The binding minimum is 30 maker-episode CLUSTERS, not 30 orders.** Below it the response's `mode` is `strip`: `percentiles` is `None` with the reason, the page draws **every observation as one dot** and no curve, and the sub-header reads `strip, no curve (n_eff = N < 30)`. Percentiles from many observations of few independent agents are precise about the agents and silent about the market. On this collection today `n_eff` is 3 at the maker level and possibly 1 — so strip mode is the correct answer, not a failure.

**The panel** (design §4.1) is three coupled pieces plus a list: the KM step curve (`line.shape:'hv'` — survival *is* a step function) with the bootstrap band as a 12 %-opacity fill, on a **log** x-axis; a **stacked** exit-reason histogram over log-spaced duration bins, because *"cancelled after 4 s"* and *"filled after 4 s"* are opposite facts and one curve cannot carry both; and a placement-time mini-map on its **own wall-clock axis**, dragged to brush a window that both of the above are recomputed over. The brush snaps to whole placement buckets, so a brushed window is always a window the data was actually counted over. Clicking a histogram bar opens the drill list under the card.

**The drill list** carries **both clocks** deliberately: `observed_at − valid_at` is our stream lag, and *if a bid's whole life is shorter than our lag we never had a chance at it* — a fact about strategy feasibility, not about the market, and invisible unless both are on the row. `distance_to_floor_eth` is the bid minus the **lowest standing ask at the instant of placement**, read off the same sweep line the floor chart uses; it is `null` — never estimated, never carried forward from the last known floor — when no ask was standing then.

**What has to be said before the number is computed** (factcheck D-W4), and the response carries it as `basis.direction_warning`: the median may move in **either** direction from the old estimator's, because that one was biased short by dropped censoring and long by an unbounded join. A large change is the expected consequence of two known defects. It is not a discovery, and **a shorter median is not evidence of a bug in the new code**.

## 4a. Traits and the screener (`rest.py`, `traits.py`, `metrics.screener`)

**Loading traits costs REST reads; the design spends as few as possible.** OpenSea's per-token endpoint would cost one read per token — 9,212 reads for Argonauts, three days of the measured 120/hour budget. Instead:

1. **Token list** — `GET /collection/{slug}/nfts?limit=200`, about 47 governed reads, resumable from the saved cursor if interrupted (`ops.db` → `onboarding.last_cursor`). Records each token's `metadata_url` — **and its `traits`, which this response carries** (verified for Argonauts 2026-09-09 against a second tool's raw pull; BUG-054, when the field was being discarded and 9,161 per-token reads planned in its place). Traits found here are stored as they arrive, `traits_source = 'opensea_nft_list'`. For Argonauts this is where essentially all of them come from. **Other collections must be re-verified** — the field may be absent where OpenSea has not indexed metadata.
2. **Traits** — fetched **directly from each token's `metadata_url`** (IPFS through the configured gateway, Arweave, or HTTP). These are not OpenSea calls and are not metered. Six at a time (`traits.concurrency`). **For Argonauts this pass does nothing**: every Argonaut's `metadata_url` is NULL, so there is no URL to fetch. A run that reports `attempted: 0` here after a complete pass 1 is the pass working, not failing.
3. **Fallback** — for tokens neither pass got traits for, the OpenSea per-token endpoint, capped at `traits.opensea_fallback_budget` (50) per run so a dead metadata host cannot spend the hour. A token whose **contract is unknown** consumes no slot: the cap counts reads made, not tokens considered (BUG-055 — the decrement used to run before the contract check, so a contract-less token burned a budgeted read that was never issued, and a token that could have used it went without).
4. **Offline import** — `navanax import-traits <tokens.json>`, or double-click **`import-traits.command`**, loads a cache another tool already pulled. **Zero reads.** Covered in full below.

**What it costs.** 47 governed reads for the list plus up to 50 fallback reads — up to 97 of the 120/hour, and more if a call is retried after a 429 (every attempt, retries included, is what `requests_spent` reports; BUG-047). It shares the budget with the recorder's backfill: run it when `status.command` shows no gaps awaiting backfill. A `metadata_url` on an OpenSea domain (`opensea.io`, `seadn.io`, `openseauserdata.com`) is **never fetched directly** — that would be a metered call outside the governor (BUG-046); such tokens take the fallback. Bodies are capped at 2 MB; the list loop stops on a repeated cursor.

Double-click **`traits.command`** once per collection; it reports pages, tokens, reads spent, the trait-type summary and the coverage line (*"N of M tokens now have traits"*), and is safe to re-run: the list step is skipped once complete, tokens that succeeded are not refetched, and tokens that **failed are retried** (`traits_at` stays NULL on failure). `traits_from_list` counts **writes**, not list entries carrying traits — an entry the store already had is counted separately, and one whose value *disagrees* with what was recorded first is printed with both sides rather than dropped (BUG-055). A token may carry two values of one trait type; both are kept. Progress is written to `onboarding` so the page can show *"onboarding: 63% of tokens have traits — metrics are provisional"* (REQ-F-07a) while it runs.

Values are stored **verbatim**: `"Blue"` and `"blue"` are two values until a human says otherwise. That is structure, not judgement.

**Importing a cache (`import-traits.command`, `navanax import-traits`).** The Explorer tool writes a `tokens.json` of every token's traits and a `summary.json` beside it carrying the epoch it was pulled at. That epoch — not now — is what `traits_at` records: the traits were observed then, and stamping "now" would be a bitemporal lie the store cannot detect afterwards (docs/05 TMP). The command asks for nothing; it looks for the cache at `traits.explorer_cache_path` in config, then in a `cache/` folder beside it, and if it finds none it prints every path it looked in.

What the import refuses to do is most of what it is:

- **It never overwrites.** A token that already has traits from any source is diffed against the cache per trait type, and every disagreement is printed with both sides. Exit code 1. One of the two sources is wrong and the Operator decides which; neither is quietly preferred.
- **It never writes a token the cache contradicts itself about.** Two entries resolving to one token id with different traits are a disagreement *inside the cache*, reported the same way — no row, no traits, nothing (BUG-055; before the fix the second entry silently won and `imported` counted both).
- **It never coerces.** A cache entry that is not an object, or a trait whose value is a nested object or a list, is skipped and counted as `malformed`. `str({'a': 1})` in `traits.value` would be a plausible substitute for a value we could not read, which is the one thing this layer must never write.
- **It never believes an impossible observation time.** `--generated` and `summary.json`'s `generated` must fall in `[2020-01-01, now + 1 h]`. Outside it the run refuses **before writing anything**, and where the value looks like milliseconds — the JavaScript default, and the source tool is a JS tool — the message says so and gives the value in seconds.

**Provenance.** `traits_source` on an imported row is `explorer_cache:<sha256[:12]>` — the digest of the exact file — and a `trait_imports` row records the file, its size, its digest, the generated time and the run's counts. `explorer_cache` alone names no particular cache, and two caches that disagree are indistinguishable a week later without it.

**Reconciliation** compares the token ids the cache *claims* against the ids the table *holds*, both resolved from `entry["id"]` — not from the cache's keys, which need not be token ids at all and were the source of a total false-mismatch report before BUG-055. A stored `contract` that differs from the one being imported is **reported**: `COALESCE` keeps what is stored, which is right, but it also hides the disagreement, and a wrong contract sends every fallback read to the wrong collection.

**Every import ends with three checks printed.** How many of the collection's tokens now have traits (`N of M`); `criteria_trait_coverage` — every distinct string criterion in `order_criteria` matching no trait value, the same question `MetricEngine.criteria_coverage()` answers for the page, asked here because a casing mismatch is cheapest to see the moment the traits land; and a **case-folded near-miss report** over trait types and values, which is what makes the verbatim-storage rule safe. `Cloak` and `cloak` are two values by design — and a filter on one matching none of the other reads as an illiquid market rather than as a bug, unless something says so out loud. It reports; it never merges.

**Filters.** The sidebar lists every trait type with every value and its count. Selections are **AND across types, OR within a type**: `Background:Blue|Red;Eyes:Laser` means (Blue or Red) and Laser. The same filter (`traits=` on the API) applies to the price charts, the live book, the tape and the screener. Three kinds of event meet a filter: **token-level** events (listings, item bids, sales, cancels) must match every clause; **collection offers** carry no token and pass, because they are bids on every token (their criteria set is empty, so it is trivially satisfied); **trait offers** carry no token either, and are matched on their stored criteria by the rule below.

**The trait-offer matching rule (replaces the BUG-045 blanket exclusion; BUG-051).** Let **C** be the offer's criteria — an AND, so it will buy any token holding *all* of them — and **F** the filter. `S(C)` is the offer's reach, `S(F)` the filter's tokens.

> A trait offer counts as bid depth for F — verdict **COVERS** — iff `S(F) ⊆ S(C)`: the offer will buy **any** token the filter selects.

Note the direction; the intuitive one is backwards. What makes an offer count is not that it is *confined* to the filter. Structurally the rule is:

```
criteria_n > 0  AND  criteria_numeric_n = 0  AND  every criterion in C is guaranteed by F
```

and a criterion `(t, v)` is *guaranteed* only when F has a clause on `t` whose sole permitted value is `v`. A filter of `Background:Blue|Red` guarantees nothing about Background, so an offer requiring Blue is **PARTIAL**, not COVERS.

**`criteria_n > 0` is the whole safety of this rule and it is one line from being a disaster.** An unparsed trait offer has no `order_criteria` rows, which makes the `NOT EXISTS` in the match *vacuously true* — the offer would match **every filter and every token**, silently, plausibly, and in the direction that manufactures an edge. The guard is what stops it, and there is a named property test on it: *a trait offer with `criteria_n = 0` matches nothing*, checked against every shape of filter.

Numeric criteria cannot be evaluated against the string `traits` table, so their verdict is **UNKNOWN** — and UNKNOWN is not TRUE. They are excluded and counted, exactly as `unparsed` works.

Five verdicts, **never summed into one number**:

| verdict | condition | treatment |
|---|---|---|
| **COVERS** | `S(F) ⊆ S(C)` | counts as bid depth for the filter |
| **PARTIAL** | overlaps but does not cover | reported separately, **always with `\|S(F) ∩ S(C)\|` and `\|S(F)\|`** — never a bare count |
| **DISJOINT** | no overlap | excluded, on evidence |
| **UNKNOWN** | numeric criteria present | excluded and counted |
| **UNPARSED** | `criteria_n = 0` | excluded and counted — the loud-failure marker |

`MetricEngine.trait_offer_verdicts()` returns all five. Allocating a fraction of a PARTIAL offer's quantity as depth (say `qty × |S(F)∩S(C)| / |S(C)|`) models the offerer as indifferent among the tokens in reach — a **judgement**, so it belongs in ANALYSIS with its assumption declared, never in this layer (docs/06 §4).

The structural rule is deliberately **conservative**: it decides COVERS from F and C alone, without consulting `traits`. An offer whose reach happens to contain `S(F)` for reasons the filter does not state is reported PARTIAL rather than COVERS. That errs toward under-counting depth, which is the safe direction.

**A filtered spread has two different legs.** `immediacy_cost` under a trait filter is a *trait-filtered* lowest ask minus the *collection-wide* highest collection offer. Trait offers do **not** enter that leg even now that their criteria are stored: `collection_bid` is collection offers by definition, and a bid leg that maxes over item bids, COVERing trait offers and collection offers is a **different metric** — `trait_set_series`, below. The COVER rule changes the metrics whose event set already includes `trait_offer` — `bid_count` and `event_count`. The response carries a `legs` field naming all of this and the page prints it as a warning line under the chart. Whether that leg should instead refuse to compute is an open product decision for the Operator (see §5).

### 4a.1 `trait_set_series` — the trait chart's metric layer (PR-5)

`MetricEngine.trait_set_series(collection, traits, start, end, interval, denom, now)` returns, on **one** bucket grid, everything the trait chart draws. It is the Operator's decision of 2026-09-10 expressed as a function, and its shape is not a menu.

| output | definition |
|---|---|
| `trait_ask` | the **lowest STANDING ask** over `S(F)` — a full `standing_series` response (median, `[p10, p90]`, `coverage`, `n`, basis) |
| `trait_bid` | the trait group's **highest STANDING bid**: the MAX, at each τ, over the union of three legs, with `n_item`, `n_trait_offer_cover`, `n_collection` and `winning_leg` per bucket |
| `baseline_ask` | the **unfiltered** collection floor, on the same grid |
| `single_floors` | multi-clause only: one `standing_series` per clause, each with its own `matching_tokens` |
| `matching_tokens` | `\|S(F)\|` — the `n` every number on the panel carries |
| `partial_offers` · `partial_detail` | the **true** PARTIAL count, and a **sample** of at most 50 offers each carrying `\|S(F) ∩ S(C)\|` and `\|S(F)\|`. The count is computed separately from the list and is never capped — a cap that silently becomes the answer is worse than no detail (BUG-20260910-063); `partial_truncated` says when the sample is short of the count |
| `unknown_offers` · `unparsed_offers` | numeric-criteria UNKNOWN and `criteria_n = 0` UNPARSED, **counted separately** |

**The three bid legs, and why they are one line with three counts.**

1. **item bids on tokens in `S(F)`** — token-scoped, narrowed by the filter.
2. **trait offers whose criteria COVER F** — `S(F) ⊆ S(C)` (§4a). The COVERS predicate is `criteria_cover_sql()` unchanged; `order_lives` carries no `(run, seq)`, so the criteria join reaches it through the placement row in `events`. Under an **empty** filter no trait offer covers — `S(F)` is every token and only `C = {}` reaches all of them — so this leg is legitimately empty with no filter. That is the rule, not a missing join.
3. **collection offers** — `C = {}`, a bid on every token, so they reach `S(F)` whatever F is. Not a special branch: it falls out of the same rule.

The union max is a legitimate *"best bid available to a holder of this trait"*. A **sum** of the three would not be depth, and a line that silently swaps population between buckets is exactly the leg-mixing quant §1 metric 1 forbids — so the response reports each leg's own `n` and `winning_leg` names the leg that set the number in each bucket. Where two legs quote the same best price, `winning_leg` names both (`"item+collection"`): picking one would be an invention.

**Nulls, never zeros.** A bucket where the AND-set has no standing ask is `None`. Zero is a price; *"no standing ask in this hour"* is not the price zero, and on a book with eight listings in forty minutes the difference is most of the chart. Both emptinesses are null and both say which: an AND-set of real tokens none of which is listed (`matching_tokens > 0`), and an AND-set that selects no token at all (`matching_tokens = 0`).

**`|S(F)| = 0` withholds every trait series, and this matters today.** The bid line was the obvious one (BUG-20260910-059): a collection offer is not token-scoped and `S(F) ⊆ S(C)` is vacuously true when `S(F) = ∅`, so without a guard the panel draws a confident *"highest standing bid"* for a trait group with **zero members**.

The **ask** lines were left unguarded on the reasoning *"no tokens, so no listings"* — and that is an unstated assumption that `tokens` and `traits` agree (BUG-20260910-061). Nothing enforces it: `token_filter_sql`, and so `_standing_live`, and so every standing series, resolves membership against **`traits`**, while `|S(F)|` is counted over **`tokens`**. A populated `traits` table with an empty token list is a real intermediate state of trait onboarding, and in it the book matches the filter while `|S(F)| = 0`.

So every series is blanked explicitly:

| series | guarded on | when `|S(F)| = 0` |
|---|---|---|
| `trait_bid` + `winning_leg` | the combined set | null on every bucket |
| `trait_ask` (median, `p10`, `p90`, `coverage`; `n` → 0) | the combined set | null on every bucket |
| each `single_floors[i]` | **its own** clause's token set | null on every bucket |
| `baseline_ask` | — | **untouched**; it is not filtered at all |

Each single-clause floor is guarded on **its own** token set, not on the combined one: a clause that does select tokens is a real number, and it is exactly what the multi-clause panel exists to show when the AND-set is empty. Blanking it because the *intersection* is empty would delete the answer to the question the panel is asking.

`basis.empty_token_set` is `true`, `basis.token_set_universe` is `"tokens"`, and the panel and the basis both say so. The per-leg counts still report what **was** standing, so the withholding reads as a withholding rather than as an empty book. **Until the token list is loaded, every filter has `S(F) = ∅`**, so this is the first thing the panel would otherwise have drawn.

**The ordering property, and its direction.** `S(F_combined)` is a **subset** of every single-clause set, so the minimum over it is **≥** the minimum over each of them:

> at every bucket where both are defined, **combined floor ≥ each single-clause floor ≥ … and the unfiltered baseline is ≤ all of them.**

The intuitive direction is backwards, and it matters: a single-clause floor coming out *above* the combined floor would mean the AND-set is not a subset of that clause's set — a broken token filter, or a `traits` table where one token carries two values of one type and the join is duplicating rather than intersecting. It would render as a trait premium and it would be a bug (docs/05 rule 5). There is a property test on it.

**No REST, and two things it does not do.** Every number comes off the stream-derived store. Ingestion-gap masking is applied by `series()` and is **not** applied here (the page shades gap spans on the plot instead); the basis says so rather than leaving it to be discovered. And the response carries no `transform`: `/api/trait_series` returns levels, because a % change of a floor whose baseline bucket is a hole is a number with no basis. The panel prints a line saying the global transform control does not apply to it.

**`GET /api/trait_series`** — `collection`, `traits`, `interval`, `range`, `denom`. Minimal on purpose: no `transform`, no `book` (the Operator's decision fixes the book to STANDING). The handler adds no calculation.

**Holes, zeros and gaps.** Every series is returned on the **full bucket grid** of its range (BUG-044). A price bucket with no observation is `null` — a hole on the chart, never bridged. A count or volume bucket with no event while the recorder was listening is `0`: zero sales in an hour we watched is a fact. Any bucket overlapping an **ingestion gap** from the landing-zone manifest is `null` for every metric, counts included — we were not listening, so we do not know (REQ-F-15); the basis reports `gap_masked_buckets`. Grids over 20,000 buckets are refused with a message rather than thinned.

**Screener.** Every token matching the filter with its traits, the **lowest standing ask**, the **highest standing item bid** (`standing_sql` over `order_lives` — the same one predicate as the live book, §3.3) and its **last sale**. Sortable on every column — token, name, every trait type, and the three prices — with "no value" always at the bottom in either direction. Paged at 50. An unknown sort column falls back to `token_id`; sort is applied in Python, never interpolated into SQL.

### 4a.2 A filter that selects no token makes every metric under it undefined

`|S(F)| = 0` is not an edge case on this project — it is the **default** until the `traits` table is filled, and it was producing numbers.

Three of a filter's disjuncts are right on their own and wrong as a set. `_bucketed` matches `((token_id IS NULL AND event_type='collection_offer') OR (event_type='trait_offer' AND COVERS) OR (token matches every clause))`. A collection offer bids on every token, and `S(F) ⊆ S(C)` is **vacuously true** when `S(F) = ∅` — so with a filter that selects nothing, the two token-less branches kept matching. `bid_count` and `event_count` returned the collection-offer count; `sales_count`, `listing_count`, `cancel_count` and `volume` returned **0.0**, which reads as *"nothing happened to this trait group"* when the truth is that there is no trait group (BUG-20260910-060).

The rule now, in one place:

> When a trait filter is applied to a metric **and** `|S(F)| = 0`, that metric is **`null` on every bucket** — not 0, not a count of token-less events — and `basis.empty_token_set` is `true` with a note saying which case it is and that the first thing to check is whether `traits` is populated.

- `filter_narrows(spec)` is the single predicate for *"does a trait filter actually narrow this metric"*. It is **false** only for a metric whose events are all collection offers.
- `_bucketed` returns nothing under the guard, so no direct caller can get the leaked count; `series()` fills the grid with `None` instead of the usual `0.0` for COUNT/SUM.
- `trait_set_series` applies the same rule to its bid leg (§4a.1), which is where the defect was first seen.
- **`collection_bid` is the deliberate carve-out and is unchanged.** It is not narrowed by a trait filter — that is the leg discipline quant §1 metric 1 requires, and the `legs` line has always said so — but its basis now adds that the number is collection-wide and is **not** a statement about the filter.
- **The universe is `tokens`, and the note says so.** `|S(F)|` is counted over the `tokens` table — the same universe `screener` and `trait_offer_verdicts` use — so an unpopulated *token list* trips the guard even when `traits` is full. That is the conservative direction (knowing of no tokens, we can say nothing about a subset of them), and it is why the note names `tokens` rather than `traits`: on the fixture that exposed BUG-20260910-061, `traits` was the *populated* half, and a note telling the Operator to check `traits` would have named the one table that was fine. Both are filled by the same onboarding run (`traits.command` / `import-traits.command`). `basis.token_set_universe` carries the answer as data.
- **Every branch of `series()`, not just `_bucketed`** (BUG-20260910-061). The guard runs once after all four branches — standing spread, standing series, derived, `_bucketed` — and blanks `raw`, `parts`, `pct_of_ask`, `p10`/`p90`/`coverage` and the `n` arrays together. `parts` goes with the rest: `immediacy_cost` as a whole is undefined here, and leaving its collection-wide bid leg populated invites subtracting two legs of a metric that has just been declared meaningless.

The page prints the note under **every** chart's basis line, not only the trait panel.

## 4b. The page — the design rules

Set by the Operator on 2026-09-09; pinned by `test_ui_contract` so they cannot regress silently.

- **Layout language:** OpenSea/Coinbase — near-black ground, cards with 14 px radii, quiet grid, strong marks. Accent is **Austin FC Verde `#00B140`**. Colour roles: asks orange, item bids blue, collection offers green, spread yellow, gaps shaded red.
- **Contrast:** every text/background pair ≥ 4.5:1. `color-scheme: dark` is declared so macOS cannot paint native controls white (BUG-041); selects and buttons are custom-drawn.
- **Time:** everything on screen is in `display.timezone` (America/Chicago) and says so — header, footer, every basis line. Stored data stays UTC (BUG-042). Sub-day buckets are UTC-aligned; day-and-longer buckets align to local midnight (docs/06).
- **Numbers:** USD to the cent, always — **aggregates included**. `compact()` is gone from the page; a long total shrinks its own type (`.sm` → 21 px, `.xs` → 17 px) rather than being abbreviated or truncated. ETH to 3–4 decimals with Ξ. Counts with thousands separators. Hover cards are dark with light monospace text and carry the unit (BUG-043).
- **Honesty over smoothness:** lines are straight between observations; undefined intervals are holes, and every series is on the full bucket grid so a hole is a real null, not a missing point (§4a). Third-party strings — trait names and values, token names, image URLs — are escaped before they reach the page (BUG-048). A moving-average overlay, labelled with its window, is planned once there is ≥ 24 h of data — it will be an overlay, never a replacement.

### 4b.1 The palette — three classes, and why the boundary matters

The `:root` block in `src/navanax/ui/index.html` is the whole palette, and it is now split into three **classes** marked with `/* @chrome */`, `/* @status */`, `/* @data */`. The split exists because of a real defect (factcheck D-V3): `#19C95A` was at the same time the brand accent, the `--coll` token, the KPI sparkline stroke, the collection-offer price line, the *sales* bar series and the *event-mix* bars. A green mark on the page could be any of five different things.

| class | tokens | rule |
|---|---|---|
| **@chrome** | `--bg` `--bg-2` `--surface` `--surface-2` `--border` `--border-2` `--text` `--text-2` `--muted` `--faint` `--verde` `--verde-2` `--verde-dim` `--verde-glow` `--grid` `--hover-bg` `--hover-border` | brand, surfaces, ink, chart furniture. **Austin FC Verde is UI chrome only** — active state, focus ring, primary button, links, pills. Never a data series. |
| **@status** | `--bad` `#FF6B6B` · `--warn` `#FFB84D` | reserved state colours (failure, warning). Never a data series. |
| **@data** | all eleven, below | hue = event role. Every mark on every chart reads one of these. |

The eleven `@data` tokens, with the hexes that are in `:root` **now** — `test_docs_palette_table_matches_the_root_block` parses this table and the `:root` block and fails the build if they disagree, because a stale hex in the only table a reader consults is worse than no table (this row said `--trait-offer` `#C792EA` for a day after PR-6 re-picked it):

| token | hex | role |
|---|---|---|
| `--ask` | `#FF8A65` | ask / listing — orange |
| `--bid` | `#7CC4FF` | item bid — blue; also the survival curve |
| `--coll` | `#33E7C6` | collection offer — green-cyan |
| `--trait-offer` | `#B266FF` | trait offer — purple (re-picked in PR-6) |
| `--sale` | `#FFFFFF` | sale / fill — pure white, the loudest mark on the page |
| `--spread` | `#FFD166` | immediacy cost — yellow |
| `--cancel` | `#E24E9B` | cancel — magenta |
| `--invalidated` | `#007711` | exit: `order_invalidate` (PR-8) |
| `--expired` | `#5544FF` | exit: expiry, derived (PR-8) |
| `--censored` | `#EEAA00` | still standing at `as_of` — not an exit (PR-8) |
| `--gap-fill` | `rgba(255,107,107,.10)` | ingestion gap shading |

Event-mix bars are neutral `--text-2`, because that panel's bars encode a *quantity*, not a role.

Two hexes were chosen by measurement rather than by eye, using the OKLab ΔE (×100) and Machado CVD simulation in the dataviz validator, against the `--surface` `#141B17` ground:

- **`--coll` `#33E7C6`.** Worst-case ΔE across normal/protan/deutan vision is 11.4 against `--bid` and 11.4 against `--ask`; 15.1 against `--bid` under normal vision; 12.3 from `--verde-2`, which is what actually severs the brand/data collision. Contrast 11.2 : 1. *DESIGN §5.4's suggested `#2ED573` was measured and rejected: it is ΔE 4.0 from `--verde-2` (so it does not fix D-V3 at all) and ΔE 2.5 from `--ask` under deuteranopia.*
- **`--cancel` `#E24E9B`.** DESIGN §5.4 assigns cancel to `--bad` `#FF6B6B`, but the cancels and listings bars sit adjacent in one stack on the Activity chart at ΔE 4.6 (deutan) / 6.9 (normal) — below the readability floor. `#E24E9B` sits at 16.1 / 17.9 from `--ask`, contrast 4.8 : 1, and keeps `--bad` reserved for status. **This is a deliberate deviation from §5.4 and is the design-lead's to confirm.**

- **`--trait-offer` `#B266FF` (re-picked in PR-6).** The old `#C792EA` was ΔE **5.0** from `--bid` under deuteranopia *and* **14.6** under normal vision — below the validator's 15.0 hard floor, so it was a pair a full-colour reader could not reliably separate either. It was recorded rather than fixed because no chart drew it; PR-6 is the chart that draws it, twice (the bid leg's legend swatch, and slot 3 of the §3.2 ramp), so it is re-picked. Worst of protan/deutan: **15.8** from `--bid`, **15.4** from `--cancel`, **27.7** from `--ask`, **25.7** from `--coll`; contrast **5.20 : 1**. At OKLCH L 0.665 / C 0.221 it is the first mark on the page **inside** the validator's dark-mode lightness band.

- **`--invalidated` `#007711` · `--expired` `#5544FF` · `--censored` `#EEAA00` (PR-8).** DESIGN §4.1b assigns the exit stack to `--bad` / `--warn` / `--faint`; two of those are `@status` and one is `@chrome`, and the rule above forbids a reserved state colour as a data series, so the three missing exits get `@data` tokens. **PR-8's first draft of these three was chosen by eye and failed the validator hard** — `#F0A202` / `#7E8F87` / `#5C7C8A` measured worst-CVD **2.6** and worst-normal **7.7**, two greys no reader could separate. Re-picked and re-measured on the scope that actually governs, which is *co-occurrence*, not the token list:

| scope | worst CVD (protan/deutan) | worst normal | contrast |
|---|---|---|---|
| **A — the five-colour exit stack** (`--cancel`, `--sale`, `--invalidated`, `--expired`, `--censored`) | **16.8** `--expired`↔`--cancel` (tritan 9.1) | **27.1** `--censored`↔`--sale` | all ≥ 3:1 |
| **B — the whole survival panel** (A + `--bid`, the curve above the stack) | **16.2** `--bid`↔`--cancel` (tritan 9.1) | **23.3** `--bid`↔`--sale` | all ≥ 3:1 |
| C — all ten hex `@data` tokens at once | 6.4 `--censored`↔`--ask` | 10.3 `--censored`↔`--spread` | all ≥ 3:1 |

  Both governing scopes clear the ≥ 11 target, against a floor of 6. **Scope C is the wrong scope and is recorded so nobody re-derives it as a defect:** `--ask` and `--spread` are price lines on the Prices and Immediacy panels, `--censored` is a histogram stack on the survival panel, and no rendered surface puts them together. Scope C's ceiling is set by pre-existing tokens in any case — with *no* new amber at all it is 7.1 (`--expired`↔`--trait-offer`) / 15.1 (`--coll`↔`--bid`). The stack also carries a **2 px `--surface` separator between segments**, which is the secondary encoding the validator requires wherever a pair sits near a floor, and which the mark specs want on stacked fills regardless.

One measured problem is still recorded rather than fixed: most marks on the page sit **above** the dark-mode lightness band, because the Operator chose bright marks on a near-black ground. Two checks in `validate_palette.js` therefore still FAIL on the full data palette by design — the **lightness band** (`--ask` 0.755, `--bid` 0.796, `--coll` 0.834, `--spread` 0.88, `--sale` 1.0, `--censored` 0.782, all above the 0.67 ceiling) and the **chroma floor**, which only `--sale` `#FFFFFF` fails, because pure white has zero chroma and is deliberately the loudest mark on the page. `--invalidated` and `--expired` are *inside* the band; an in-band amber for `--censored` was searched for and found (`#BD8A00`, L 0.60) and **rejected**, because it costs 5.5 points of CVD separation (16.8 → 11.3) to buy one token's lightness harmony. What PR-6 and PR-8 *did* fix is the checks that were failing for a real reason: **CVD separation and the normal-vision floor pass on scopes A and B, and failed before each re-pick** (`--trait-offer` at 5.0/14.6 before PR-6; the exit trio at 2.6/7.7 before this table).

**The multi-trait categorical ramp (§4b.5) adds no new tokens.** Colour on that one panel encodes trait identity rather than event role — the single documented exception to §5.4 — and it reuses four hues the palette already carries: `--bid` · `--coll` · `--trait-offer` · `--cancel`, in that fixed order, never cycled. No event-role series but the combined ask is on the panel, the sub-title says *"colour = trait; asks only"*, and each swatch sits beside its trait's own name. Measured: all **ten** pairs on that panel (the combined `--ask` against each of the four, and the four against each other) are ≥ **11.4** worst-case ΔE across protan/deutan, against a target of 8; the tightest is `--bid` / `--coll` at 11.4, and the normal-vision worst is 15.1. *DESIGN §3.2's suggested ramp — violet `#C792EA` · cyan `#4DD0E1` · pink `#FF8FB1` · tan `#D6BA73` — was measured and rejected: `--ask` / tan is ΔE **3.8**, violet / cyan **6.1** and cyan / pink **7.6**, all below the floor, and it would have added four more hues to an eight-slot palette to get there.*

**No panel may reference a colour literal.** `test_ui_contract` asserts that the `<script>` block contains **no** six-digit hex at all: the chart code reads each token back out of `:root` with `getComputedStyle`, so the CSS and Plotly can never drift apart.

### 4b.2 Chart interaction

The Plotly toolbar is gone (`displayModeBar:false`) and `scrollZoom:false` is written explicitly — **not** because wheel-zoom was hijacking the page (it was never enabled; factcheck D-W1) but so a future Plotly default cannot enable it. What was genuinely annoying and is now fixed: a drag used to box-zoom and **rescale the y-axis**.

- **y is locked** (`yaxis.fixedrange:true`). Price can no longer be rescaled by dragging.
- **x is deliberately free** (`fixedrange:false`, D-W2). `dragmode:'select'` with `selectdirection:'h'` makes a drag select a time window on that chart; a `reset ×` chip appears in the panel header while a selection is live, which is the proof that something happened. The event-mix panel is the one chart that locks x — it is a horizontal bar chart, not a time axis.
- **Range chips** `1h · 6h · 24h · 7d · 30d` sit in the header of every time chart. They drive the same global `range` control, so the two can never disagree, and they replace nothing — the full range select (with HTD/DTD/MTD/YTD) stays. Chips are plain DOM and still render when the Plotly CDN is unreachable.
- **Crosshair:** `hovermode:'x unified'` with a 1 px `--border-2` spike across the plot, snapped to the cursor.
- **Hover card:** Plotly's `hoverlabel`, restyled to `--hover-bg` `#0E1512` on a `--hover-border` rule, monospace, `namelength:-1` so no series name is truncated. Each row carries the value **with its unit**, and — where `/api/series` supplies them — `cov NN%` (share of observable bucket seconds the leg actually stood), `n=` (orders in the book) and the `p10–p90` band. The header is the bucket time formatted `%b %d, %H:%M:%S` plus the live zone abbreviation (CDT/CST). *Known limit: in unified mode Plotly omits a null series from the card rather than printing `— no observation`; getting that row requires the hand-built hover card in DESIGN §5.2.*
- **Markers** are drawn when a series has fewer than 120 observed points and dropped above it, so eight listings in a window draw eight visible marks rather than an empty pane.

### 4b.3 KPI cards

Four cards: **lowest ask now · collection offer now · volume 24 h · sales 24 h**, on a 24 × 1 h grid, each with a hand-rolled SVG sparkline (no chart library — the row still renders offline) stroked in its own role colour.

- **The delta is `now` versus the value 24 hours ago** — the Operator's rule, decided 2026-09-10 against DESIGN §6-Q2. Not "last observed vs first observed", which silently compares two hours chosen by where the holes happened to fall.
- **Both endpoints are printed under the number**, with the zone abbreviation, in `--text-2`.
- **If either endpoint is a hole there is no percentage at all.** The card prints `no observation 24h ago` or `no observation in the current hour` in `--warn`. A hole is not a zero and it is not its nearest neighbour. If the current hour is a hole, the big number falls back to the last observed value and is labelled `· last observed`.
- The sub-line always carries the observed-bucket count out of the total, and a 24 h **sum** over a window containing holes says so: `⚠ N hour(s) not observed — this total covers the M we watched`.

### 4b.4 The basis line under a standing-book chart

Under the Prices and Immediacy-cost charts the basis line now also renders, from the PR-3 `series()` response: which `book` produced the number (`standing` or `observed`), the **median coverage** — share of observable bucket seconds both legs actually stood — with its bucket count and denominator, the median number of orders in the book per bucket (both legs, for the spread), `p10–p90 withheld (n<30) in N of M buckets` wherever the band arrays are null, a **crossed-book alarm** when `basis.negative_buckets > 0` (a crossed book is a reconstruction defect, never an arbitrage — BUG-20260910-057), the left-truncation warning, and the `observed_book_warning` when the interval-extremum variant is in use.

### 4b.5 The trait chart (PR-6) — what the Prices card becomes under a filter

Whenever a trait filter is on, the **Prices card becomes the trait chart**. It does not appear beside the collection prices panel: two price panels on one screen showing different token sets is how a trait floor gets read as the collection floor.

**Single clause — exactly three lines.**

| series | token | weight | dash | markers |
|---|---|---|---|---|
| trait group's **highest standing bid** | `--bid` | 2.4 | solid | below ~120 points |
| trait group's **lowest standing ask** | `--ask` | 2.4 | solid | below ~120 points |
| collection floor (baseline) | `--faint` | 1.4 | **dotted** | no |

The baseline is pushed **first**, so it sits under everything — z-order in Plotly is trace order, and the Operator asked for the collection floor pale and dotted *below*.

**Two or more clauses — asks only.** The combined (AND) floor in `--ask` at full strength, weight **3.0**, drawn on a 6 px `--bg` halo so it stays readable where a clause line crosses it; each single-clause floor on the categorical ramp at weight **2.0**; the unfiltered baseline dotted at **1.4**. The panel title says *"Trait floors — asks"* and the sub-title says *"colour = trait; asks only"*.

**Five or more clauses.** The ramp has four slots and a fifth clause does **not** get a generated hue — it gets a printed line: *"4 of 6 clauses drawn individually — the ramp has 4 slots and a 5th clause gets no generated colour, it gets this line. Deselect to see the others; the combined floor still counts every clause."*

**The legend is ours, not Plotly's** (`showlegend:false`), and sits under the plot. One row per series: a 24 px swatch drawn at the series' **actual** weight and dash, the series name with its clause, `n=NNN tokens`, `observed/total buckets`, and the last value. **The hole count is a first-class number on screen**, not something to be found in the basis. The bid row carries a second line naming its three legs with each leg's own median `n` per bucket, and says *"never summed"* on the same line.

**Hover** carries the value with its unit, `cov NN%` (share of observable bucket seconds the leg stood), the per-leg `n`, and **`leg:`** — which of the three legs set that bucket's bid. *Known limit, inherited from §4b.2: in `x unified` mode Plotly omits a null series from the card rather than printing `— no observation`; that row needs the hand-built hover card in DESIGN §5.2.*

**Holes, times, transforms.** `connectgaps:false` on every trace; no spline, no smoothing, ever; the x-axis is in `display.timezone` (Central) like every other chart. The global **transform** control does not apply to this panel and it says so in its basis instead of pretending: `/api/trait_series` returns levels, and a % change of a floor whose baseline bucket is a hole is a number with no basis.

**The basis under the chart** prints the book, the mode, the bucket and hole counts, `|S(F)|`, the ask and bid rules verbatim from the engine, all three leg definitions, the PARTIAL / UNKNOWN / UNPARSED counts with each PARTIAL offer's `|S(F) ∩ S(C)|` of `|S(F)|`, the left-truncation warning, and the note that ingestion-gap masking is not applied here.

## 4e. The views, the Event Ledger and the Wallets view (PR-9)

Until PR-9 the dashboard was **one page**: every panel on one scroll, every panel refetched every ten seconds whether or not it was on screen. DESIGN §1 and §8 split it into hash-routed views, and this section is what a developer needs in order not to undo that.

### 4e.1 The router

Five views, each linkable: `#/market` · `#/traits` · `#/flow` · `#/wallets` · `#/health`. Tabs are a segmented control in the header; the route is the URL hash so a view can be linked to, which REQ-F-40 (deep links into evidence) will need and which is far more expensive to retrofit later.

**Global state** — collection, interval, range, denomination, transform, trait filter — persists **across tabs** and survives a reload. It lives in the hash (`#/traits?traits=Palette:Seafoam&range=6h&denom=ETH`) and is mirrored to `localStorage`, which is the fallback when the URL carries no hash. Per-view controls (the survival sub-bar, the ledger filter row) live inside their view and are never global.

**Per-view defaults apply on first visit only.** `VIEW_DEFAULTS` gives Market 6 h/5 m, Traits 24 h/1 h, Flow 1 h/1 m, Wallets and Health 24 h/1 h. Once the Operator changes range or interval himself, `S.touched` is set and no view overrides it again — a tab that silently reset the range on every visit would be answering a different question from the one he asked.

**The trait sidebar is global and shows on Market and Traits only.** On Flow, Wallets and Health it collapses to a read-only chip row with a *clear all* button: the filter is still applied and must still be visible, but 272 px of checkboxes is not earning its place there.

**Only the active view is fetched.** `VIEW_LOADERS` maps a view to its panel loaders and `all()` awaits exactly those. Five views' worth of queries on every tab switch is how a loopback dashboard starts feeling remote.

**The Prices card is ONE card and the router moves it** between the Market grid and the Traits grid (`hostHero()`, the two `anchor-*-hero` elements). Two copies would be two price panels showing different token sets under one filter, which is precisely how a trait floor gets read as the collection floor (§4b.5). `test_ui_router_keeps_every_panel_and_adds_the_views` asserts every panel id that existed before the split still exists **exactly once** and is inside exactly one view — because the failure mode of a layout split is a panel that is still in the code, no longer in any view, and never drawn again while all of its own tests stay green.

| view | panels |
|---|---|
| **Market** | KPI row · Prices / trait chart · Live book · Immediacy cost · Activity · Makers · Sales tape · Event mix |
| **Traits** | trait sidebar · Prices / trait chart (c12 here) · Screener |
| **Flow** | Bid-lifetime survival panel · **Event ledger** · **Chart this selection** |
| **Wallets** | Wallet list · **Address card** · **Counterparty adjacency heat-map** |
| **Health** | trust summary · Gaps · Integrity audit · Crossed book · Criteria coverage · Normalizer · Store |

### 4e.2 `GET /api/ledger` — the raw record, keyset-paginated

At the measured ~48 events/s the events table gains **~4.1 M rows a day**, and the two things `screener()` does — sort in Python, page by `OFFSET` — are both unusable there. So the ledger sorts and filters **in SQL**, pages by **keyset**, and refuses what it cannot serve.

```
GET /api/ledger?collection=&traits=&token=&type=&maker=&taker=&price_band=min:max
               &order_hash=&has_price=1&range=&start=&end=
               &sort=valid_ts&dir=desc&cursor=<opaque>&limit=200
→ {rows:[…], next_cursor, has_more, sort, direction, sortable:[…], unsortable:{…},
   total_estimate, exact, count_cap, basis:{…}}
```

- **Keyset, never offset, and the tuple is the index's own column order.** The cursor is base64 JSON carrying the last row's ordering tuple plus a fingerprint of the sort, direction and filter. Each composite index is `(collection, key, valid_ts)`, so with `collection` fixed by an equality the rows arrive in `(key, valid_ts, rowid)` order, and the `ORDER BY` asks for exactly that — the scan **is** the sort, at any depth. `LEDGER_SORTS` carries the tuple per sort, so the ordering and the index cannot drift; the two sorts whose index leads with a timestamp (`valid_ts`, `observed_ts`) order on `(key, rowid)`. Anchored to a **row**, not an offset: rows the stream appends between two page requests cannot make page 2 skip or repeat a row page 1 already returned. `rowid` is last because it is the only column guaranteed unique — at 48 rows/s many rows share a timestamp, a maker and a price, and without it a page boundary inside a tie repeats or skips rows.
- **NULL keys are handled explicitly** (`metrics._keyset_clause`). SQLite orders NULLs first ascending and last descending, and `key < NULL` is NULL rather than true, so a naive keyset stops dead at the first null-keyed row and silently truncates. The comparison reproduces the `ORDER BY`'s own null placement, and `test_ledger_keyset_walks_nulls_and_ties_on_every_sort` removes each branch in turn to prove the walk catches it.
- **The index is named, and that alone was not enough** (BUG-20260910-068). `dashboard._window` is unconditional, so every request the page makes carries a `valid_ts` range. Given that range the planner preferred `ix_events_coll_valid` and sorted the whole window into a TEMP B-TREE — 864 ms for one 200-row page over 200,000 rows, at every depth. Adding `INDEXED BY` did **not** fix it: SQLite then *skip-scanned* the named index (`ANY(collection) AND ANY(maker) AND valid_ts>?`) to use the range term, and a skip-scan does not deliver rows in index order, so the sort survived. What fixes it is `INDEXED BY` **plus** the index-order `ORDER BY` **plus** writing the window as `+e.valid_ts >= ?` on the five sorts whose index does not lead with a timestamp — the unary plus is SQLite's documented way to keep a term out of the index constraint. **The trade is deliberate, scoped, and not free.** On those five sorts the window becomes a per-row filter, so the scan walks the collection's index until `limit` rows have matched — meaning the cost scales with the **collection**, not with the answer. On the default sort `valid_ts` the range stays an index range, because there the window *is* the order, and the `COUNT` is always built from the indexable form. Measured on the same 200,000-row store:

  | case | `sort=valid_ts` | `sort=maker` |
  |---|---|---|
  | full window, 200 rows | 6 ms | 6 ms (was 864 ms) |
  | 93-second window, 93 rows | 0.7 ms | **27 ms** |

  27 ms is **accepted for now** — two orders of magnitude better than the 864 ms it replaced, and a narrow window combined with an exotic sort is not what the page does by default. It is not free: at 4 M rows/day it grows linearly while the full-window case does not. **TODO, named in the code:** switch on window *width* rather than on sort alone. Both SQL shapes already exist and the flag is the only difference, so the change is a predicate rather than a rewrite — when the window covers a small fraction of the collection's recorded span, prefer the indexable form and accept the sort, because sorting a few hundred rows is cheaper than walking four million. The crossover must be measured on the real corpus, not guessed.

`basis.index_forced` and `basis.pagination` state all of this on every response, and `MetricEngine.ledger_query_plan()` returns `EXPLAIN QUERY PLAN` so the claim is checkable rather than asserted. `INDEXED BY` is **deliberately un-exercised insurance**: with the ORDER BY and the unary plus in place the planner already picks the right index, so no assertion can fail on its absence — it is there so a future schema or statistics change moves the query loudly rather than silently.
- **A cursor from a different query is refused**, not silently reused: continuing would interleave two result sets.
- **NULL keys are handled explicitly.** SQLite orders NULLs first ascending and last descending, and `key < NULL` is NULL rather than true, so a naive keyset stops dead at the first null row. The comparison reproduces the `ORDER BY`'s own null placement.
- **Sortable columns are a whitelist, and every one has an index** (`metrics.LEDGER_SORTS` ↔ `normalize.LEDGER_INDEXES`): `valid_ts`, `observed_ts`, `token_num`, `maker`, `event_type`, `price_eth`. A sort on anything else is a **400 naming the columns that work and why this one does not** — `metrics.LEDGER_UNSORTABLE` carries the reason per column, so the UI greys the header rather than offering a click that 400s. `price_usd` is the instructive case: the ETH/USD rate moves between rows, so a USD ordering is not the ETH ordering and cannot borrow that index.
- **`token_num` is a GENERATED column on `events`** (`normalize.SCHEMA`, added additively by `_migrate` on an existing store, following the `expiration_ts` precedent). `#10` sorts after `#9`; the Operator asked for that by name. It is `NULL` — never 0 — when `token_id` is not exactly the decimal rendering of an integer, because a token with no number has no place in a numeric order and 0 would put it in front of token #1. Generated rather than stored: a copy can drift from `token_id`, an expression cannot. **`PRAGMA table_info` does not list a VIRTUAL generated column — only `table_xinfo` does**, which both the migration and the index builder use.
- **The indexes are built by `normalize.ensure_ledger_indexes()`, not by `SCHEMA`**, because `executescript` cannot be conditional and an index over a column an old store never had would make the whole schema step fail — i.e. the dashboard would refuse to open rather than open without one sort. A skipped index is logged by name, and the sort that needed it is refused by name when it is asked for.
- **`total_estimate` is exact while it is cheap and says which.** It counts up to `LEDGER_COUNT_CAP` (50,000) and returns `exact:false` with a floor above that. An exact `COUNT(*)` over a filtered 4 M-row table on every keystroke is the other way to make this feel broken. The count runs against the **indexable** form of the window (a range scan on `ix_events_coll_valid`), never against the `+`-prefixed page form.
- **`exit_reason` comes from `order_lives`** via a `LEFT JOIN` on `order_hash`, and the basis says the left-truncation consequence out loud: a row with no exit reason may be an order that was already standing when recording started, not an order that never ended.

**Chart this selection** (`&mode=chart`) returns the same filter as **one series per event type**, oldest first, capped at **5,000** rows with both numbers printed. Never a silent sample. Two rules about that caption, both of them defects that shipped once (BUG-20260910-069):

- **`matched` counts what the chart can DRAW.** The count and the row query are built from one `drawable` predicate (`… AND price_eth IS NOT NULL AND valid_ts IS NOT NULL`), so the denominator in the caption is the chart's, not the table's. `basis.matched_predicate` says so on screen.
- **A capped count reads as a floor**, branching on `matched_exact` exactly as `count_note` does: `charting the 5,000 most recent of 41,208 chartable rows` when it is exact, `… of more than 50,000 (a floor, not a count) chartable rows` when it is not. A capped figure printed as exact, inside the one sentence whose job is to admit the picture is incomplete, is the flattering-direction failure the project's fifth rule is about. Markers are always on (a token has a handful of events; a markerless line draws nothing), segments join points **within one event type only** — a listing and a bid are not two readings of one quantity — and nothing is interpolated between two observations, so an ingestion gap stays a gap and is shaded like everywhere else. Rows with no price are excluded from the chart and said to be, because they cannot be drawn on a price axis.

### 4e.3 `GET /api/wallets` and `GET /api/wallet/<address>` — the Wallets view

The unit of analysis is an **address**. For this PR both endpoints are built from the **landing-zone-derived tables only** — `events` and `order_lives`. No REST, no chain read.

`/api/wallets` ranks addresses by event count in the window: events, **share with its count** (`{pct, n, of}`, never a bare percentage), bids, cancels, listings, sales, collection and trait offers, first and last seen — plus the **counterparty adjacency matrix**.

`/api/wallet/<address>` is the address card:

- **Behaviour** — every percentage as `{pct, n, of}` so the card prints `61.4% of 62,218` and `0.02% (3 of 19,180)`. `share(n, of)` returns `pct: null` when the denominator is zero: *0 of 0 is undefined, not 0%*.
- **Median bid life with `n` AND `n_eff`.** `n_eff` is maker-episode clusters (`metrics.episode_ids`, ε = 60 s) — one wallet's thousands of quotes are not thousands of independent observations, and the uncertainty belongs to the clusters. Percentiles are **withheld** below `min_n_for_percentiles` through the same `pct()` every other percentile on this page goes through, and the response says which happened.
- **Counterparties** from `item_sold` maker/taker pairs, each with its share of this address's trades and the count behind it.
- **Chain-sourced fields are `collected: false` with the reason** — holdings, acquisitions, disposals, first-funded-by, and any self-attached handle. They are `null`, never `0`: `0` would read as *"this address holds nothing"*, which is a claim this build has not earned. Wiring them needs a public RPC / block-explorer key, which is the Operator's to provide (market-analyst charter, data sources §2).
- **A cluster is a hypothesis with evidence attached**, so with no evidence derived there is **no cluster** — `id: null`, `evidence_count: 0`, and a note. The word never appears without its count.
- **A flag renders only if its rows can be listed.** No pattern detector has been built *and validated* (docs/01 §8.2, and the agent that builds a signal never validates it), so `flags` is empty and the basis says why. An unevidenced flag is an accusation, not a finding.

**The charter boundary is enforced by the schema, not by discipline.** `WALLET_PROFILE_KEYS` is the fixed top-level key set, and `_assert_address_only()` walks the whole object **to any depth** — dicts and lists — and raises on any key matching an identity marker. The markers are the free-text ones as much as the obvious ones: `note`/`notes`, `alias`, `handle`, `label`, `tag`, `bio`, `profile`, `owner`, `ens`, `twitter`, `discord`, `telegram`, alongside `name`, `company`, `employer`, `location`, `city`, `country`, `person`, `email`, `phone`, `identity`, `legal`, `real`. Nobody adds a field called `legal_name`; they add `cluster.members[].notes` and then type into it, which is why the walk is recursive and why free text counts.

**Matching is by name SEGMENT, not substring, and that is load-bearing:** `tokens` contains `ens` and `vintage` contains `tag`, so a substring rule would fire on `distinct_tokens_touched` and the guard would be deleted within a week for crying wolf. `_key_segments()` splits on separators and camelCase and tolerates a plural; the longer, unambiguous markers in `IDENTITY_SUBSTRING_MARKERS` are *additionally* checked as substrings, so `realname` is caught too.

Because the guard was widened, this build's own prose fields were **renamed rather than the guard narrowed to fit them**: `chain.note` → `chain.why_not_collected`, `cluster.note` → `cluster.evidence_rule`, `bid_life.note` → `bid_life.n_eff_rule`, `basis.left_truncation_note` → `basis.left_truncation`, and a flag's `label` → `flag`. **The field for a self-attached handle was removed entirely.** The charter permits one — it is self-declared and public — but the enforcement is the schema, and a field that is null today and populated later is precisely the hole the guard exists to close. It gets added when it is actually collected, in the same commit as the guard change, with a reviewer looking at both. A reviewer checks the constant, not the rendering code (`.claude/agents/market-analyst.md`; DESIGN §8.2.1).

**The counterparty graph is an adjacency heat-map, not a force graph** (DESIGN §8.2.2): a Plotly `heatmap` trace, so no second library and no physics simulation; deterministic, so two screenshots of the same data are the same picture; and a cell **states 88** where a force layout would only say "these two are near each other". Rows are sellers, columns buyers, cell = count of `item_sold` with that pair. **Sequential single hue** built from the `--coll` token — counts have no meaningful midpoint, so a diverging scale would invent one. Capped at 40 addresses by volume with the truncation printed.

### 4e.4 `GET /api/health` — can I trust the other tabs?

One request, because four would be a view that renders three of them and silently drops the fourth. It measures nothing of its own; it aggregates `api_status`, `api_gaps` and `api_audit` plus the two numbers nothing else surfaced. It carries:

| block | what it answers |
|---|---|
| `recorder` | is the stream running, and how old is the newest event |
| `gaps` | open · awaiting backfill · the ten most recent, with reason and irrecoverable classes |
| `integrity` | the checksum audit's failures and notes |
| `crossed_book` | `alarms` = total crossed buckets in the last 24 h **across every watched collection**, not just the one on screen |
| `criteria_coverage` | distinct trait-offer criteria and how many match no trait value |
| `normalizer` | last fold time, duration, **`files_failed` and `files_short` hoisted to the top level** |
| `store` | row counts per table, bytes on disk, landing-zone bytes, newest `valid_at` / `observed_at` |
| `store_writer` | who holds the fold-writer lock. `enforced` is a **boolean on a writer and the string `not applicable (reader: …)` on a reader** — a reader never takes the lock, and `False` there would read as "this writer is unprotected and a second one could corrupt the store", which is the opposite fact |

**The reopen probe runs on its own clock, not on the fold tick** (BUG-20260910-071). While the store is unreadable, `_loop` waits `min(refresh_seconds, STORE_REOPEN_SECONDS)`; once a store is open it waits `refresh_seconds`. The two settings answer different questions — `refresh_seconds` is how stale the *market data* may be, `STORE_REOPEN_SECONDS` is how long the Operator waits after fixing the *store* — and collapsing them meant that at the documented `refresh_seconds: 3600` a rebuilt store went unnoticed for up to an hour, which quietly removed the one-click part of `rebuild-store.command`.
| `store_writer` | which pid holds the fold-writer lock and since when, so "is a second dashboard folding into this?" is answerable from the page (BUG-20260910-067) |
| `quick_check` | `PRAGMA quick_check`, **cached for ten minutes** — it reads every page, and on a 2.8 GB store per request it would make Health the slowest tab and hold the writer lock each time. The response carries `at`, `age_seconds` and `cached` so the answer's age is never implied |

`files_failed` / `files_short` are hoisted deliberately: they are the **BUG-058** signature — *"115 files read, 0 rows added"* with no error anywhere an operator looks — and a count that lives three keys deep in a status blob is a count nobody reads.

### 4e.5 What PR-9 deliberately did not build

- **The wallet's `Ladder shape` panel** (DESIGN §8.2, this address's bid prices against the floor over time). The panel is one query away but it is a *chart of a wallet's quoting*, and with 3 makers and 3 sales in the corpus it would be a picture of nothing. It arrives with real volume.
- **Column picker, density toggle and row virtualisation** on the ledger (DESIGN §8.1.1/§8.1.2). The API is already the shape virtualisation needs — 100 rows a page, keyset — but rendering ~60 DOM rows regardless of result size is only worth writing against a store that has more than a few thousand rows in it.
- **Order-flow's `Bids vs floor` scatter and the Traits view's comparison table and ECDF** (DESIGN §1.2/§1.3). Out of PR-9's scope, which was the split itself.
- **Any wallet pattern flag.** Building one and shipping it in the same PR would violate docs/01: the agent that builds a signal never validates it.

## 5. What it does not do yet — read this before trusting a number

- **SQLite, not DuckDB.** docs/07 specifies DuckDB + Parquet for the analytical store. The environment this was built in cannot install DuckDB, and shipping an untested store for irreplaceable data is how BUG-010 happened. Every query is plain SQL DuckDB accepts; the swap is the `analytical.path` line. Revisit when the store passes ~50 M rows or a query is slow.
- **No wash filter, no `qa_index`, no trait model.** Prices are raw observations. Methodology §4.4 and REQ-F-13 are Phase 1.
- **The trait chart cannot draw a line until `traits` has rows, and it will say so rather than draw something else.** `traits` has 0 rows in this working copy. Every trait filter therefore selects no token, so `trait_ask`, `trait_bid` and every single-clause floor are null, `basis.empty_token_set` is `true`, and the panel prints the reason. Only the baseline collection floor draws. Nothing here is verifiable against real trait data until the Explorer cache is imported (§4a) or `traits.command` is run.
- **Every metric under a filter that selects no token is `null`, and says so** (BUG-20260910-059/060 — both fixed). See §4a.2. Because `traits` is empty today, that is *every* filter: the page will draw the unfiltered baselines and print the reason, rather than draw the collection offer under a trait's name.
- **The standing book only contains orders whose placement we witnessed.** `immediacy_cost`, `floor_ask` and `collection_bid` are now resting-book quantities (§4c), but the resting book is reconstructed from the stream and is left-truncated: ~801 Argonauts listings were already resting when recording started and none of them are in it. The reconstructed floor is an **upper bound**. Until the ask side is seeded from a REST listings snapshot, read every standing spread as an upper bound on the true spread, which is what `basis.left_truncated` says.
- **No cross-sectional views, no rarity or trait pricing model** (REQ-F-05..11) — one collection so far. The screener shows observed prices per token; it does not yet estimate what a trait is worth. (The one heat-map that exists is the wallet counterparty adjacency in §4e.3, which is a count of trades between two addresses, not the cross-collection heatmap REQ-F-11 asks for.)
- **The Wallets view is landing-zone-derived only** (§4e.3). Holdings, funding source, ENS/OpenSea handle, clusters and pattern flags are all absent and say so; they need a public RPC key and, for the flags, a validation pass by someone other than whoever wrote the detector. Nothing on that card is invented to fill a gap.
- **No moving average yet** — waiting for 24 h of data so the window is a choice, not a guess.
- **Open decision for the Operator:** under a trait filter, should `immediacy_cost` use the collection-wide offer as its bid leg (current behaviour, labelled) or refuse to compute? Both are defensible; the page labels the current choice until he decides.
- **`scope`** in the MetricRequest tuple (docs/06 §3) is implemented as `collection` only, with `traits` as an orthogonal filter. Token, peer-group and universe scopes are not implemented.
- **No alerts.** The page shows; it does not tell.
- **Gaps are shaded** on every time chart (REQ-F-15) from the manifest. A chart drawn over shaded time is drawn over time we were not listening.

## 6. Files

`src/navanax/normalize.py` · `src/navanax/metrics.py` · `src/navanax/dashboard.py` · `src/navanax/rest.py` · `src/navanax/traits.py` · `src/navanax/ui/index.html` · `dashboard.command` · `traits.command` · tests in `tests/selftest.py` (`test_normalizer_*`, `test_metric_engine_contract`, `test_dashboard_serves_localhost_only`, `test_traits_pipeline`, `test_screener_sort_and_filter`, `test_trait_filtered_metrics`, `test_series_gap_masking`, `test_rest_client_accounting`, `test_ui_contract`, `test_order_lives_primitive`, `test_bid_lifetimes_censoring_and_orphans`, `test_order_criteria_parsing_and_migration`, `test_trait_offer_matching_rule`, `test_standing_series_is_time_weighted_over_the_bucket`, `test_immediacy_cost_is_a_standing_book_spread`, `test_percentiles_are_withheld_below_min_n`, `test_ui_trait_chart_is_the_shape_the_operator_decided`, `test_trait_set_series_bid_leg_is_a_union_that_names_its_winner`, `test_trait_set_series_collection_offer_covers_every_filter`, `test_trait_set_series_partial_offers_are_counted_never_summed`, `test_trait_set_series_empty_and_set_is_null_never_zero`, `test_trait_set_series_combined_floor_bounds_every_single_clause_floor`, `test_trait_set_series_baseline_is_present_and_unfiltered`, `test_trait_series_endpoint_passes_the_filter_through`, and PR-9's `test_ledger_keyset_pages_are_stable_under_inserts`, `test_ledger_refuses_a_sort_it_has_no_index_for`, `test_ledger_token_number_sorts_numerically`, `test_ledger_chart_states_its_cap_and_never_samples_silently`, `test_wallet_card_percentages_carry_their_counts`, `test_wallet_profile_has_no_field_for_an_off_chain_identity`, `test_health_surfaces_the_alarms_nothing_else_does`, `test_ui_router_keeps_every_panel_and_adds_the_views`, `test_ui_ledger_and_wallets_panels_are_the_shape_the_design_specifies`) — fixtures are **real frames** from the 2026-09-09 capture.
