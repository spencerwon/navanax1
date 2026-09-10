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
| **@data** | `--ask` `#FF8A65` · `--bid` `#7CC4FF` · `--coll` `#33E7C6` · `--trait-offer` `#C792EA` · `--sale` `#FFFFFF` · `--spread` `#FFD166` · `--cancel` `#E24E9B` · `--gap-fill` `rgba(255,107,107,.10)` | hue = event role. Every mark on every chart reads one of these. |

Role assignments: ask/listing orange · item bid blue · collection offer green-cyan · trait offer purple · sale/fill **pure white** · cancel magenta · spread yellow · ingestion gap red at 10 %. Event-mix bars are neutral `--text-2`, because that panel's bars encode a *quantity*, not a role.

Two hexes were chosen by measurement rather than by eye, using the OKLab ΔE (×100) and Machado CVD simulation in the dataviz validator, against the `--surface` `#141B17` ground:

- **`--coll` `#33E7C6`.** Worst-case ΔE across normal/protan/deutan vision is 11.4 against `--bid` and 11.4 against `--ask`; 15.1 against `--bid` under normal vision; 12.3 from `--verde-2`, which is what actually severs the brand/data collision. Contrast 11.2 : 1. *DESIGN §5.4's suggested `#2ED573` was measured and rejected: it is ΔE 4.0 from `--verde-2` (so it does not fix D-V3 at all) and ΔE 2.5 from `--ask` under deuteranopia.*
- **`--cancel` `#E24E9B`.** DESIGN §5.4 assigns cancel to `--bad` `#FF6B6B`, but the cancels and listings bars sit adjacent in one stack on the Activity chart at ΔE 4.6 (deutan) / 6.9 (normal) — below the readability floor. `#E24E9B` sits at 16.1 / 17.9 from `--ask`, contrast 4.8 : 1, and keeps `--bad` reserved for status. **This is a deliberate deviation from §5.4 and is the design-lead's to confirm.**

- **`--trait-offer` `#B266FF` (re-picked in PR-6).** The old `#C792EA` was ΔE **5.0** from `--bid` under deuteranopia *and* **14.6** under normal vision — below the validator's 15.0 hard floor, so it was a pair a full-colour reader could not reliably separate either. It was recorded rather than fixed because no chart drew it; PR-6 is the chart that draws it, twice (the bid leg's legend swatch, and slot 3 of the §3.2 ramp), so it is re-picked. Worst of protan/deutan: **15.8** from `--bid`, **15.4** from `--cancel`, **27.7** from `--ask`, **25.7** from `--coll`; contrast **5.20 : 1**. At OKLCH L 0.665 / C 0.221 it is the first mark on the page **inside** the validator's dark-mode lightness band.

One measured problem is still recorded rather than fixed: every other mark on the page sits **above** that lightness band, because the Operator chose bright marks on a near-black ground. Two checks in `validate_palette.js` therefore still FAIL on the full data palette by design — the lightness band, and the chroma floor, which `--sale` `#FFFFFF` fails because pure white has zero chroma and is deliberately the loudest mark on the page. What PR-6 *did* fix is the two checks that were failing for a real reason: CVD separation and the normal-vision floor both passed after the re-pick and failed before it.

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

## 5. What it does not do yet — read this before trusting a number

- **SQLite, not DuckDB.** docs/07 specifies DuckDB + Parquet for the analytical store. The environment this was built in cannot install DuckDB, and shipping an untested store for irreplaceable data is how BUG-010 happened. Every query is plain SQL DuckDB accepts; the swap is the `analytical.path` line. Revisit when the store passes ~50 M rows or a query is slow.
- **No wash filter, no `qa_index`, no trait model.** Prices are raw observations. Methodology §4.4 and REQ-F-13 are Phase 1.
- **The trait chart cannot draw a line until `traits` has rows, and it will say so rather than draw something else.** `traits` has 0 rows in this working copy. Every trait filter therefore selects no token, so `trait_ask`, `trait_bid` and every single-clause floor are null, `basis.empty_token_set` is `true`, and the panel prints the reason. Only the baseline collection floor draws. Nothing here is verifiable against real trait data until the Explorer cache is imported (§4a) or `traits.command` is run.
- **Every metric under a filter that selects no token is `null`, and says so** (BUG-20260910-059/060 — both fixed). See §4a.2. Because `traits` is empty today, that is *every* filter: the page will draw the unfiltered baselines and print the reason, rather than draw the collection offer under a trait's name.
- **The standing book only contains orders whose placement we witnessed.** `immediacy_cost`, `floor_ask` and `collection_bid` are now resting-book quantities (§4c), but the resting book is reconstructed from the stream and is left-truncated: ~801 Argonauts listings were already resting when recording started and none of them are in it. The reconstructed floor is an **upper bound**. Until the ask side is seeded from a REST listings snapshot, read every standing spread as an upper bound on the true spread, which is what `basis.left_truncated` says.
- **No cross-sectional views, no heatmap, no rarity or trait pricing model** (REQ-F-05..11) — one collection so far. The screener shows observed prices per token; it does not yet estimate what a trait is worth.
- **No moving average yet** — waiting for 24 h of data so the window is a choice, not a guess.
- **Open decision for the Operator:** under a trait filter, should `immediacy_cost` use the collection-wide offer as its bid leg (current behaviour, labelled) or refuse to compute? Both are defensible; the page labels the current choice until he decides.
- **`scope`** in the MetricRequest tuple (docs/06 §3) is implemented as `collection` only, with `traits` as an orthogonal filter. Token, peer-group and universe scopes are not implemented.
- **No alerts.** The page shows; it does not tell.
- **Gaps are shaded** on every time chart (REQ-F-15) from the manifest. A chart drawn over shaded time is drawn over time we were not listening.

## 6. Files

`src/navanax/normalize.py` · `src/navanax/metrics.py` · `src/navanax/dashboard.py` · `src/navanax/rest.py` · `src/navanax/traits.py` · `src/navanax/ui/index.html` · `dashboard.command` · `traits.command` · tests in `tests/selftest.py` (`test_normalizer_*`, `test_metric_engine_contract`, `test_dashboard_serves_localhost_only`, `test_traits_pipeline`, `test_screener_sort_and_filter`, `test_trait_filtered_metrics`, `test_series_gap_masking`, `test_rest_client_accounting`, `test_ui_contract`, `test_order_lives_primitive`, `test_bid_lifetimes_censoring_and_orphans`, `test_order_criteria_parsing_and_migration`, `test_trait_offer_matching_rule`, `test_standing_series_is_time_weighted_over_the_bucket`, `test_immediacy_cost_is_a_standing_book_spread`, `test_percentiles_are_withheld_below_min_n`, `test_ui_trait_chart_is_the_shape_the_operator_decided`, `test_trait_set_series_bid_leg_is_a_union_that_names_its_winner`, `test_trait_set_series_collection_offer_covers_every_filter`, `test_trait_set_series_partial_offers_are_counted_never_summed`, `test_trait_set_series_empty_and_set_is_null_never_zero`, `test_trait_set_series_combined_floor_bounds_every_single_clause_floor`, `test_trait_set_series_baseline_is_present_and_unfiltered`, `test_trait_series_endpoint_passes_the_filter_through`) — fixtures are **real frames** from the 2026-09-09 capture.
