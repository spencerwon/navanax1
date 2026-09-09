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
- **Every response carries its basis** — metric, label, denomination, transform, baseline value and time, window, interval, timezone, `wash_filter`, `as_of`, bucket count, undefined-bucket count. The page prints it under each chart.

Metrics available now (all *observed* quantities — no fair value, no smoothing):

| metric | definition |
|---|---|
| `floor_ask` | lowest `item_listed` price seen in the interval |
| `collection_bid` | highest `collection_offer` seen in the interval |
| `top_item_bid` | highest `item_received_bid` seen in the interval |
| `immediacy_cost` | `floor_ask − collection_bid` (REQ-F-13a), also as % of ask. **Undefined when either side is absent** — drawn as a hole, never filled |
| `sale_price`, `volume`, `sales_count` | median / sum / count of `item_sold` |
| `listing_count`, `bid_count`, `cancel_count`, `event_count` | counts |

Non-series views, all reading the one standing-book relation (§3.3): the **live book** (`standing_sql` at `now`, with **depth in units** — a quantity-5 offer is five), the **sales tape**, **makers** (who is generating the flow), **bid lifetimes** (one life per order, ended durations plus the censored, `unknown`-terminator and **orphan** counts; percentiles only above n = 30, REQ-F-19), and the **event mix**.

Bid lifetimes still *counts* censoring rather than modelling it, so the percentiles are biased short; Kaplan–Meier with competing risks reads these same rows and is a later PR. The median may move in **either** direction from the old number (9 s), because the old estimator was biased short by dropped censoring and long by the cross-product join — a large change is the expected consequence of two known defects, not a discovery.

`wash_filter` is always `raw` and the response says so: no wash-trade filter exists yet, and the page must not imply one.

## 4a. Traits and the screener (`rest.py`, `traits.py`, `metrics.screener`)

**Loading traits costs REST reads; the design spends as few as possible.** OpenSea's per-token endpoint would cost one read per token — 9,212 reads for Argonauts, three days of the measured 120/hour budget. Instead:

1. **Token list** — `GET /collection/{slug}/nfts?limit=200`, about 47 governed reads, resumable from the saved cursor if interrupted (`ops.db` → `onboarding.last_cursor`). Records each token's `metadata_url`.
2. **Traits** — fetched **directly from each token's `metadata_url`** (IPFS through the configured gateway, Arweave, or HTTP). These are not OpenSea calls and are not metered. Six at a time (`traits.concurrency`).
3. **Fallback** — for tokens whose metadata cannot be read, the OpenSea per-token endpoint, capped at `traits.opensea_fallback_budget` (50) per run so a dead metadata host cannot spend the hour.

**What it costs.** 47 governed reads for the list plus up to 50 fallback reads — up to 97 of the 120/hour, and more if a call is retried after a 429 (every attempt, retries included, is what `requests_spent` reports; BUG-047). It shares the budget with the recorder's backfill: run it when `status.command` shows no gaps awaiting backfill. A `metadata_url` on an OpenSea domain (`opensea.io`, `seadn.io`, `openseauserdata.com`) is **never fetched directly** — that would be a metered call outside the governor (BUG-046); such tokens take the fallback. Bodies are capped at 2 MB; the list loop stops on a repeated cursor.

Double-click **`traits.command`** once per collection; it reports pages, tokens, reads spent and the trait-type summary, and is safe to re-run: the list step is skipped once complete, tokens that succeeded are not refetched, and tokens that **failed are retried** (`traits_at` stays NULL on failure). A token may carry two values of one trait type; both are kept. Progress is written to `onboarding` so the page can show *"onboarding: 63% of tokens have traits — metrics are provisional"* (REQ-F-07a) while it runs.

Values are stored **verbatim**: `"Blue"` and `"blue"` are two values until a human says otherwise. That is structure, not judgement.

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

**A filtered spread has two different legs.** `immediacy_cost` under a trait filter is a *trait-filtered* lowest ask minus the *collection-wide* highest collection offer. Trait offers do **not** enter that leg even now that their criteria are stored: `collection_bid` is collection offers by definition, and a bid leg that maxes over item bids, COVERing trait offers and collection offers is a **different metric** and a later PR. The COVER rule changes the metrics whose event set already includes `trait_offer` — `bid_count` and `event_count`. The response carries a `legs` field naming all of this and the page prints it as a warning line under the chart. Whether that leg should instead refuse to compute is an open product decision for the Operator (see §5).

**Holes, zeros and gaps.** Every series is returned on the **full bucket grid** of its range (BUG-044). A price bucket with no observation is `null` — a hole on the chart, never bridged. A count or volume bucket with no event while the recorder was listening is `0`: zero sales in an hour we watched is a fact. Any bucket overlapping an **ingestion gap** from the landing-zone manifest is `null` for every metric, counts included — we were not listening, so we do not know (REQ-F-15); the basis reports `gap_masked_buckets`. Grids over 20,000 buckets are refused with a message rather than thinned.

**Screener.** Every token matching the filter with its traits, the **lowest standing ask**, the **highest standing item bid** (`standing_sql` over `order_lives` — the same one predicate as the live book, §3.3) and its **last sale**. Sortable on every column — token, name, every trait type, and the three prices — with "no value" always at the bottom in either direction. Paged at 50. An unknown sort column falls back to `token_id`; sort is applied in Python, never interpolated into SQL.

## 4b. The page — the design rules

Set by the Operator on 2026-09-09; pinned by `test_ui_contract` so they cannot regress silently.

- **Layout language:** OpenSea/Coinbase — near-black ground, cards with 14 px radii, quiet grid, strong marks. Accent is **Austin FC Verde `#00B140`**. Colour roles: asks orange, item bids blue, collection offers green, spread yellow, gaps shaded red.
- **Contrast:** every text/background pair ≥ 4.5:1. `color-scheme: dark` is declared so macOS cannot paint native controls white (BUG-041); selects and buttons are custom-drawn.
- **Time:** everything on screen is in `display.timezone` (America/Chicago) and says so — header, footer, every basis line. Stored data stays UTC (BUG-042). Sub-day buckets are UTC-aligned; day-and-longer buckets align to local midnight (docs/06).
- **Numbers:** USD to the cent, always. ETH to 3–4 decimals with Ξ. Counts with thousands separators. Hover cards are dark with light monospace text and carry the unit (BUG-043).
- **Honesty over smoothness:** lines are straight between observations; undefined intervals are holes, and every series is on the full bucket grid so a hole is a real null, not a missing point (§4a). Third-party strings — trait names and values, token names, image URLs — are escaped before they reach the page (BUG-048). A moving-average overlay, labelled with its window, is planned once there is ≥ 24 h of data — it will be an overlay, never a replacement.
- **KPI cards** at the top: lowest ask now, collection offer now, 24 h volume, 24 h sales — each with a 24-hour sparkline and, for the prices, change versus the first hour of the window with the count of hours that had an observation.

## 5. What it does not do yet — read this before trusting a number

- **SQLite, not DuckDB.** docs/07 specifies DuckDB + Parquet for the analytical store. The environment this was built in cannot install DuckDB, and shipping an untested store for irreplaceable data is how BUG-010 happened. Every query is plain SQL DuckDB accepts; the swap is the `analytical.path` line. Revisit when the store passes ~50 M rows or a query is slow.
- **No wash filter, no `qa_index`, no trait model.** Prices are raw observations. Methodology §4.4 and REQ-F-13 are Phase 1.
- **`immediacy_cost` uses the highest collection offer *seen in the interval*,** not the standing best offer at each instant. At 5-minute intervals on a bot-made book the difference is small; at 1-day intervals it is not. A resting-book reconstruction is the next step.
- **No cross-sectional views, no heatmap, no rarity or trait pricing model** (REQ-F-05..11) — one collection so far. The screener shows observed prices per token; it does not yet estimate what a trait is worth.
- **No moving average yet** — waiting for 24 h of data so the window is a choice, not a guess.
- **Open decision for the Operator:** under a trait filter, should `immediacy_cost` use the collection-wide offer as its bid leg (current behaviour, labelled) or refuse to compute? Both are defensible; the page labels the current choice until he decides.
- **`scope`** in the MetricRequest tuple (docs/06 §3) is implemented as `collection` only, with `traits` as an orthogonal filter. Token, peer-group and universe scopes are not implemented.
- **No alerts.** The page shows; it does not tell.
- **Gaps are shaded** on every time chart (REQ-F-15) from the manifest. A chart drawn over shaded time is drawn over time we were not listening.

## 6. Files

`src/navanax/normalize.py` · `src/navanax/metrics.py` · `src/navanax/dashboard.py` · `src/navanax/rest.py` · `src/navanax/traits.py` · `src/navanax/ui/index.html` · `dashboard.command` · `traits.command` · tests in `tests/selftest.py` (`test_normalizer_*`, `test_metric_engine_contract`, `test_dashboard_serves_localhost_only`, `test_traits_pipeline`, `test_screener_sort_and_filter`, `test_trait_filtered_metrics`, `test_series_gap_masking`, `test_rest_client_accounting`, `test_ui_contract`, `test_order_lives_primitive`, `test_bid_lifetimes_censoring_and_orphans`, `test_order_criteria_parsing_and_migration`, `test_trait_offer_matching_rule`) — fixtures are **real frames** from the 2026-09-09 capture.
