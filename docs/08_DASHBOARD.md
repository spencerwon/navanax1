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

Frames that do not parse go to the `unparsed` table with the reason. Nothing is dropped; the raw bytes are still in the landing zone.

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

Non-series views: the **live book** (orders placed, not since cancelled/invalidated/filled, unexpired — lifecycle by `order_hash`), the **sales tape**, **makers** (who is generating the flow), **bid lifetimes** (placed → cancelled, same order; percentiles only above n = 30, REQ-F-19), and the **event mix**.

`wash_filter` is always `raw` and the response says so: no wash-trade filter exists yet, and the page must not imply one.

## 5. What it does not do yet — read this before trusting a number

- **SQLite, not DuckDB.** docs/07 specifies DuckDB + Parquet for the analytical store. The environment this was built in cannot install DuckDB, and shipping an untested store for irreplaceable data is how BUG-010 happened. Every query is plain SQL DuckDB accepts; the swap is the `analytical.path` line. Revisit when the store passes ~50 M rows or a query is slow.
- **No wash filter, no `qa_index`, no trait model.** Prices are raw observations. Methodology §4.4 and REQ-F-13 are Phase 1.
- **`immediacy_cost` uses the highest collection offer *seen in the interval*,** not the standing best offer at each instant. At 5-minute intervals on a bot-made book the difference is small; at 1-day intervals it is not. A resting-book reconstruction is the next step.
- **No screener, no cross-sectional views, no heatmap** (REQ-F-05..11) — one collection so far.
- **No alerts.** The page shows; it does not tell.
- **Gaps are shaded** on every time chart (REQ-F-15) from the manifest. A chart drawn over shaded time is drawn over time we were not listening.

## 6. Files

`src/navanax/normalize.py` · `src/navanax/metrics.py` · `src/navanax/dashboard.py` · `src/navanax/ui/index.html` · `dashboard.command` · tests in `tests/selftest.py` (`test_normalizer_*`, `test_metric_engine_contract`, `test_dashboard_serves_localhost_only`) — fixtures are **real frames** from the 2026-09-09 capture.
