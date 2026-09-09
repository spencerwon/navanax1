# Requirements Document
## OpenSea Consolidation, Analysis & Trade Discovery Platform

**Version:** 1.0 (Draft for approval)
**Date:** 2026-09-09
**Owner:** Spencer
**Status:** Awaiting sign-off — no code should be written against this until Section 12 acceptance gates are agreed.

---

## 1. Purpose and Thesis

### 1.1 Problem statement

NFT markets on OpenSea are structurally inefficient in ways equity markets are not. Order books are thin, listings are stale, price discovery is driven by a small number of participants, and the data needed to value an asset (trait rarity, recent comparable sales, holder concentration, listing depth) is scattered across screens and never presented in a form that supports comparison. A trader who wants to answer "is this token mispriced relative to its collection and its own history?" cannot do so from the OpenSea UI in a reasonable amount of time.

### 1.2 Thesis

The exploitable edge in illiquid NFT markets is **information latency and information assembly**, not speed. In liquid markets edge comes from microseconds; here it comes from being the only participant who has assembled the full picture of a thin collection before other participants react to the same public facts. Specifically:

1. **Stale-listing edge** — listings priced against a floor that has since moved, or against a trait tier the seller mispriced.
2. **Comparable-sales edge** — trait-adjusted fair value derived from recent sales, versus the naive collection floor most participants anchor to.
3. **Flow edge** — accumulation or distribution by wallets whose past behavior predicted moves, visible on-chain before it shows up in floor price.
4. **Regime edge** — recognizing when a thin collection has transitioned from a dead state to an active state, early in the transition.

### 1.3 Non-goals (explicit)

The following are **out of scope** and any proposal to add them requires a written amendment:

- High-frequency or MEV-style execution, sniping bots, or gas-war participation.
- Automated order submission without human confirmation (see §7.4).
- Wash trading, market manipulation, or any activity that fabricates volume or price signals.
- Portfolio management for third parties, or any activity constituting investment advice to others.
- Tax accounting or regulatory reporting.

### 1.4 Honest statement of difficulty

This document assumes an adversarial reality: most statistically discovered NFT signals are artifacts of wash trading, survivorship bias, or overfitting, and do not survive out-of-sample testing. Transaction costs in this market are severe — marketplace fees, creator royalties, gas, and above all the **bid-ask spread on an illiquid asset, which is commonly 15–40%**. A signal must clear that hurdle before it is worth acting on. The platform's validation process (`03_VALIDATION_AND_TESTING.md`) exists primarily to kill bad signals cheaply, and it should be expected to kill the large majority of them.

---

## 2. Scope

### 2.1 Market scope (v1)

| In scope | Out of scope (v1) |
|---|---|
| Ethereum mainnet NFT collections | Fungible token / swap surface |
| A curated cross-chain watchlist (Base, Solana, and other OpenSea-supported chains) added explicitly by the operator | Automatic ingestion of all chains |
| Collections meeting the illiquidity profile in §2.2 | Blue-chip high-liquidity collections (tracked for context only, not traded) |
| ERC-721 | ERC-1155 semi-fungibles deferred to Phase 5 |
| Drops / mints as a *data source* for new collection detection | Mint participation as a strategy |

### 2.2 Target asset profile

The primary universe is collections exhibiting:

- 30-day sale count between roughly 5 and 300 (thin enough to be inefficient, active enough to price).
- Listed ratio typically 2–15% of supply.
- A measurable trait structure (≥3 trait categories) that supports within-collection relative valuation.
- Floor price above a minimum threshold (default 0.05 ETH). At 0.01 ETH two legs of gas alone can exceed the entire round-trip hurdle in methodology §8.1, so the threshold must be set high enough that gas is a minor cost component, and re-derived whenever gas prices shift materially.

These thresholds are **configuration, not code** (§6.3), and are expected to be tuned.

### 2.3 Deployment scope

Local-first, single-operator. Runs on the operator's machine. No multi-tenancy, no authentication surface, no public network exposure. This is deliberate: it removes an entire class of security and operational requirements from v1.

---

## 3. Users and Operating Modes

There is one human user (the Operator) and several software agents (defined in `02_AGENT_HIERARCHY.md`). The Operator works in three distinct modes, and the interface must serve all three:

| Mode | Frequency | Need | Primary surface |
|---|---|---|---|
| **Monitor** | Passive, continuous | Be told when something material happens; otherwise be left alone | Alert feed, notifications |
| **Explore** | Weekly, 30–120 min | Slice, sort, chart, and compare freely without a predetermined question | Dashboard, charting, screener |
| **Decide** | Ad hoc, minutes | Full context on one asset or collection, fast, to make a buy/sell call | Asset detail view, decision panel |

The reference UI shown in the project screenshots (thinkorswim) reflects the **Explore** and **Decide** modes: a dense, multi-pane, dark-themed workspace with linked charts, a watchlist grid, a heatmap, and a bubble chart for cross-sectional comparison. That density is a requirement, not an aesthetic preference — the point is to see many variables at once without navigation.

---

## 4. Data Requirements

### 4.1 Source inventory and constraints

| Source | Access | Rate limit | Role |
|---|---|---|---|
| **OpenSea Stream API** (WebSocket, `wss://stream.openseabeta.com/socket`) | API key | **None — events do not count against REST limits** | **Primary ingestion path.** Real-time listings, sales, transfers, cancellations, bids, collection/trait offers, order invalidate/revalidate, metadata updates |
| **OpenSea REST API v2** | `x-api-key` header | **Free tier: 120 reads/hr (measured), 30 writes/hr, 5 fulfillments/min.** Token bucket, shared across all keys on an account. 429 on exhaustion | Backfill, snapshots, reconciliation. **Scarce, must be budgeted** |
| **On-chain RPC / indexer** (Alchemy, QuickNode, or Dune/Flipside SQL) | Provider key | Provider-dependent | Wallet flow, transfer graphs, mint tracking, holder concentration — data the marketplace API does not expose |
| **ETH/USD reference price** (an external market-data source; provider TBD) | Provider-dependent | Provider-dependent | Historical and current ETH/USD, required for the dual-denomination rule in REQ-F-02. **Not available from OpenSea** |
| **Browser scraping** | — | — | **Last resort only.** See §4.6 |

### 4.2 The rate limit is the central architectural constraint

120 reads/hour (measured) is roughly **one request every 30 seconds**. A single naive "refresh all collections" loop over a 200-collection watchlist costs 200 reads for one field each — a third of the hourly budget — and a realistic refresh touching stats, listings and offers costs three or more reads per collection, exhausting the budget in one pass. Therefore:

- **REQ-D-01** The system SHALL treat REST calls as a rationed resource with an explicit, enforced budget allocator. No component may call REST directly; all calls pass through a rate-limit governor.
- **REQ-D-02** The governor SHALL derive remaining budget from the rate-limit headers returned on responses where present, rather than assuming a fixed number, and SHALL adapt when limits change. Where headers are absent or unreliable, it SHALL fall back to a locally-maintained token-bucket model calibrated from observed 429 responses. It SHALL NOT depend on header presence for correctness.
- **REQ-D-03** The governor SHALL implement priority classes: `INTERACTIVE` (Operator is waiting) > `SIGNAL` (a candidate needs confirmation) > `BACKFILL` (historical fill) > `MAINTENANCE` (routine refresh). Lower classes are starved before higher ones.
- **REQ-D-04** The governor SHALL implement exponential backoff with jitter on 429, and SHALL surface budget exhaustion to the Operator rather than failing silently.
- **REQ-D-05** The system SHALL maintain the WebSocket stream as the default source of truth for anything the stream emits, and SHALL use REST only for (a) data the stream does not carry, (b) backfill of history, and (c) periodic reconciliation against stream-derived state.
- **REQ-D-06** Free-tier instant keys expire after 7 days. The system SHALL detect key expiry and alert rather than degrading silently.

### 4.3 Stream reliability requirements

The Stream API is explicitly **best-effort**: events may arrive out of order, and events lost to a connection error are never resent. This is a correctness hazard, not a nuisance.

- **REQ-D-07** Every ingested event SHALL be ordered by its `event_timestamp` payload field, never by arrival time.
- **REQ-D-08** The ingestion layer SHALL be idempotent — replaying an event SHALL NOT change derived state.
- **REQ-D-09** The system SHALL detect connection loss, record the gap window, reconnect with backoff, and enqueue a REST backfill for the gap at `BACKFILL` priority.
- **REQ-D-09a** Backfill is only possible for event classes the events endpoint exposes (`sale`, `transfer`, `mint`, `listing`, `offer`, `trait_offer`, `collection_offer`). Cancellations, order invalidate/revalidate and metadata updates are **permanently unrecoverable** after a disconnect. (An `item_received_bid` stream event is an item-level offer and *is* recoverable via the events endpoint's `offer` type — it is not in the unrecoverable set.) The system SHALL record these as irrecoverable gaps, SHALL reconstruct affected order state from a REST snapshot rather than from event replay, and SHALL exclude irrecoverable windows from any backtest requiring order-state fidelity. The Phase 0 "<1% gap" exit criterion (§10) applies to backfillable classes only.
- **REQ-D-10** The system SHALL run a scheduled reconciliation comparing stream-derived collection state against a REST snapshot, and SHALL record a drift metric. Drift above a configured threshold raises a data-quality alert.

### 4.4 Historical data

OpenSea does **not** provide a historical floor-price time series. Floor history must be constructed by the platform.

- **REQ-D-11** The system SHALL persist a time series of derived collection state (floor, listed count, best offer, depth quantiles) at a configurable cadence, forming the historical record the platform itself owns.
- **REQ-D-12** This series SHALL record its own provenance — for each point, whether it was stream-derived, REST-snapshot-derived, or interpolated — because a backtest run over interpolated data is not a valid backtest.
- **REQ-D-13** Historical sales SHALL be backfilled via the events endpoint (`sale`, `transfer`, `mint`, `listing`, `offer`, `trait_offer`, `collection_offer`; cursor-paginated, up to 200 per page, `before`/`after` Unix-second bounds) subject to the REST budget.
- **REQ-D-14** The system SHALL NOT silently forward-fill missing data. Gaps SHALL be represented as gaps.

### 4.5 On-chain data

- **REQ-D-15** The system SHALL ingest ERC-721 `Transfer` events for watchlist collections to build a holder ledger independent of marketplace data.
- **REQ-D-16** The system SHALL classify transfers as: marketplace sale, private/OTC transfer, mint, burn, or wallet-internal move (same beneficial owner heuristic).
- **REQ-D-17** The system SHALL compute holder concentration, holding-period distribution, and wallet-level accumulation/distribution for watchlist collections.

### 4.6 Scraping

- **REQ-D-18** Scraping is permitted only for fields with no API equivalent, only against pages the Operator could view manually, and only at human-scale request rates.
- **REQ-D-19** Every scraped field SHALL be tagged `source=scrape` and SHALL be excluded from any backtest or automated signal by default. Scraped data informs the Operator; it does not drive the engine.
- **REQ-D-20** Scrapers SHALL fail loudly and disable themselves on layout change rather than silently returning wrong values. A scraper that returns a plausible-but-wrong number is worse than one that returns nothing.
- **REQ-D-21** Any scraping component SHALL be reviewed against OpenSea's Terms of Service before activation, and SHALL be disabled if in conflict. This is a hard gate.

### 4.7 Data integrity — wash trading

Wash trading is endemic in NFT markets and will corrupt every volume-based and price-based statistic if not addressed.

- **REQ-D-22** The system SHALL implement wash-trade detection (see methodology §4) and SHALL tag every sale with a `wash_score` (an ordinal rank, deliberately **not** named or reported as a probability — see methodology §4.4).
- **REQ-D-23** All analytics SHALL be computable in two modes: raw, and wash-filtered. The default view SHALL be wash-filtered, with the raw figure available for comparison.
- **REQ-D-24** The divergence between raw and filtered volume SHALL itself be exposed as a metric, since a large divergence is a red flag about the collection.

### 4.8 Landing zone and stream recording

- **REQ-D-26** Every stream event SHALL be written to the immutable landing zone **before** normalization, as newline-delimited JSON with an envelope recording `received_at`, `run_id`, and a monotonic sequence number, compressed with zstd and partitioned by UTC date and hour. Rationale and format analysis: `07_STORAGE_AND_RECORDING.md` §2.
- **REQ-D-26a** The landing-zone writer SHALL close a zstd frame at least every 5 seconds of *elapsed frame age* — worst-case latency is `flush_seconds + flusher_interval` (the interval being `flush_seconds/2` clamped to [0.25, 5.0]), so **~7.5s at the shipped `flush_seconds: 5`**. The bound is stated as the code's real one rather than the aspirational one; see `landing.py::_maybe_flush_frame` or 1,000 events, whichever comes first, so that a killed process loses at most the current frame. Without this the crash-safety property claimed for JSONL does not hold through the compressor.
- **REQ-D-27** The landing zone SHALL serve as the REPLAY corpus for integration testing. No separate test-recording path SHALL be built — test data captured by a different code path is not guaranteed to resemble production data.
- **REQ-D-28** Each landing-zone file SHALL have its SHA-256, event count, and first/last `event_timestamp` recorded in a daily manifest on close, and the weekly integrity audit SHALL re-verify them. A mismatch is an S0a.
- **REQ-D-29** The system SHALL subscribe to the stream **per watchlist collection**, not to the wildcard firehose, and SHALL manage subscriptions as collections enter and leave the watchlist.
- **REQ-D-29a** Trade ideas, idea outcomes, paper positions, paper fills, and the config change log SHALL be stored in the analytical store with append-only bitemporal treatment, **not** in the operational store. They are primary records that cannot be reconstructed, and REQ-F-26 and REQ-F-36 require durability and immutability that a rolling operational store does not provide.
- **REQ-D-30** Onboarding a new collection SHALL be an explicitly scheduled background operation with visible progress, not a synchronous action. A single collection costs 60–100 REST reads (`07_STORAGE_AND_RECORDING.md` §3.1) and the UI SHALL show backfill state rather than appearing broken.

### 4.9 Reference price series

- **REQ-D-25** The system SHALL ingest and maintain a historical ETH/USD price series at a resolution sufficient to value any recorded event at its own timestamp. REQ-F-02's dual-denomination rule is unsatisfiable without it, and OpenSea does not supply it. The series SHALL be stored with the same provenance and bitemporal treatment as any other ingested data, SHALL record which provider supplied each point, and SHALL be reproducible — a backtest re-run must convert at the same historical rates it used before. Where a rate is unavailable for a timestamp, the USD value is reported as unavailable rather than converted at a nearby rate.

---

## 5. Functional Requirements

### 5.1 Consolidation and storage

- **REQ-F-01** Ingest and normalize collections, tokens, traits, listings, offers, sales, transfers, and mints into a single local store with a stable schema.
- **REQ-F-02** Normalize all prices to a common numeraire. Both ETH-denominated and USD-denominated views SHALL be available, and the system SHALL record which was primary. *(A collection whose ETH floor is flat during a 30% ETH drawdown has fallen 30% in USD. Conflating these produces false signals.)*
- **REQ-F-03** Preserve raw API payloads in an immutable landing zone separate from the normalized tables, so that a schema change or parsing bug can be repaired by reprocessing rather than re-fetching. Given the REST budget, re-fetching may be impossible.
- **REQ-F-04** Maintain full bitemporal history: both *when a fact was true on-chain* and *when the system learned it*. Backtests must be able to reconstruct exactly what was knowable at a past moment (see §5.6).

### 5.2 Screening, sorting, filtering

- **REQ-F-05** Provide a cross-sectional screener over all tracked collections supporting arbitrary boolean filters on any stored or derived metric.
- **REQ-F-06** Support multi-key sort with saved, named screens.
- **REQ-F-07** Support within-collection token screening (e.g. "tokens in this collection listed below trait-adjusted fair value by >15%").
- **REQ-F-07a** The system SHALL expose onboarding and backfill progress per collection — percent complete, requests remaining, estimated completion — wherever a collection appears before its backfill is finished, and SHALL mark such a collection's derived metrics as provisional. Without this a collection mid-onboarding is indistinguishable from a broken one. *(Phase 0 delivers the progress data; Phase 1 delivers its display.)*
- **REQ-F-08** Screens SHALL be expressible as saved definitions that the alerting engine can subscribe to, so an exploratory screen becomes a monitor without rewriting it.

### 5.3 Visualization

- **REQ-F-09** Time series charting of any metric, with multi-series overlay and independent axis scaling.
- **REQ-F-10** Cross-sectional scatter/bubble charts with configurable X, Y, size, and color encodings — the direct analogue of the reference bubble chart, mapping e.g. volume vs. floor change, sized by market cap, colored by category.
- **REQ-F-11** Heatmap view over the watchlist, configurable metric and time window.
- **REQ-F-12** Distribution views: histogram and empirical CDF of listing prices and sale prices, which is how listing depth is actually read in a thin book.
- **REQ-F-13** Trait-space views: price-per-trait-tier, rarity vs. realized price scatter with fitted relationship and residuals. **The residual is the signal** — it is the estimate of mispricing.
- **REQ-F-13a** `immediacy_cost` (the bid-ask spread) SHALL be tracked and charted for every watchlist collection as a first-class time series from Phase 1, in both percentage and absolute terms, alongside a cross-sectional view ranking the watchlist by it. It is directly observable, requires no model, and is the clearest available signal of what clearing quickly actually costs. Where no collection offer exists it SHALL be shown as unavailable, never substituted or interpolated — "no bid at any price" is itself the most important liquidity fact about a collection.
- **REQ-F-14** Linked selection: selecting a collection anywhere updates linked panes, per the reference workspace.
- **REQ-F-15** Every chart SHALL indicate data provenance and completeness — a chart drawn over a period with a known ingestion gap SHALL show the gap visibly.

### 5.4 Statistical analysis

Three tiers, all specified in `01_METHODOLOGY.md`:

- **REQ-F-16 (Basic)** Descriptive statistics, returns, realized volatility, drawdown, correlation matrices, rolling windows, percentile ranks.
- **REQ-F-17 (Intermediate)** Robust and quantile regression (OLS permitted for diagnostics and comparison only — see methodology §5.2), hedonic trait pricing models, empirical-Bayes shrinkage for sparse collections, peer-group clustering, cointegration between collections, time-series decomposition (trend/seasonal/residual), drawdown and underwater-curve analysis, changepoint detection, survival analysis of time-to-sale.
- **REQ-F-18 (Advanced)** Full Bayesian hierarchical models with posterior uncertainty, state-space and regime-switching models for latent fair value, network analysis of wallet flow, and — strictly gated by `01_METHODOLOGY.md` §6.4 — machine-learning models.
- **REQ-F-19** Every statistical routine SHALL report uncertainty (confidence or credible intervals) and effective sample size alongside any point estimate. A point estimate from 7 sales presented without its interval is a lie by omission, and the system SHALL make that impossible.
- **REQ-F-20** Every routine SHALL declare its assumptions and SHALL run automatic diagnostics for violations, surfacing warnings in the output.

### 5.5 Trade analysis engine

- **REQ-F-21** Run continuously in the background, evaluating registered signals against live and derived data.
- **REQ-F-22** Emit **Trade Ideas** as structured objects containing: thesis in plain language, the signal(s) that fired, entry reference price, target and invalidation levels, estimated round-trip cost, estimated time-to-exit given the collection's liquidity, position-size ceiling, confidence with interval, and the full input snapshot that produced it.
- **REQ-F-23** Every Trade Idea SHALL be reproducible — replaying the recorded input snapshot SHALL regenerate an identical idea. Non-reproducible ideas are rejected.
- **REQ-F-24** Emit **Market Development** alerts for material state changes independent of any trade thesis (regime change, unusual flow, holder concentration shift, sudden listing-depth change).
- **REQ-F-25** Alerts SHALL be deduplicated, rate-limited per collection, and prioritized. An engine that cries wolf will be ignored, at which point it is worse than no engine.
- **REQ-F-26** Every idea and alert SHALL be logged permanently with its outcome tracked automatically, whether or not it was acted upon. This log is the primary evidence base for whether the platform works.

### 5.6 Backtesting

- **REQ-F-27** Provide a backtesting engine over the historical store supporting arbitrary strategy definitions.
- **REQ-F-28** The engine SHALL enforce point-in-time correctness using the bitemporal store (REQ-F-04). It SHALL be **structurally impossible** for a strategy to read a fact the system had not yet learned at simulation time. This is enforced at the data-access layer, not by strategy-author discipline.
- **REQ-F-29** The engine SHALL model costs explicitly and by default: marketplace fee, creator royalty, gas (using historical gas prices, not current), and slippage. Slippage for an illiquid asset SHALL be modeled from actual observed book depth at that timestamp, not as a fixed percentage.
- **REQ-F-30** The engine SHALL model **fill realism**: a backtest may only fill against listings that actually existed, at their actual prices, and SHALL account for the probability that a listing would have been taken by another buyer first.
- **REQ-F-31** The engine SHALL model **exit realism**: for illiquid assets, the constraint is not price but time. Exits SHALL be simulated against realistic time-to-sale distributions, and a strategy that cannot exit is a losing strategy regardless of mark-to-market.
- **REQ-F-32** Report a standard result set: total and annualized return, volatility, Sharpe and Sortino, max drawdown, win rate, profit factor, average holding period, capital utilization, turnover, and total costs paid as a fraction of gross profit.
- **REQ-F-33** Support walk-forward analysis and out-of-sample partitioning as first-class operations, not manual procedure.

### 5.7 Paper trading

- **REQ-F-34** **Watchlist testing** — track specific assets or collections with hypothetical entries, marked to live data, with full P&L attribution.
- **REQ-F-35** **Investment Strategy Test** — register a strategic hypothesis in writing, with success criteria and a review date defined *before* results accrue, and track it to a verdict. This structure exists specifically to prevent retrospective rationalization of results.
- **REQ-F-36** Paper trades SHALL be recorded with an immutable timestamp at creation. Editing entry price or thesis after the fact SHALL be prohibited, not merely discouraged.
- **REQ-F-37** The system SHALL report paper-trading results honestly, including a comparison against a naive benchmark (e.g. buy-and-hold the collection floor, or hold ETH). **A strategy that underperforms holding ETH is not a strategy.**

### 5.8 Alerting

- **REQ-F-38** Deliver alerts through at least one channel that reaches the Operator when not at the workstation.
- **REQ-F-39** Alerts SHALL be tiered by urgency, with configurable per-tier quiet hours and delivery channels.
- **REQ-F-40** Every alert SHALL carry a direct link into the platform view containing the full evidence for it.

---

## 6. Non-Functional Requirements

### 6.1 Reliability and correctness

- **REQ-N-01** Correctness over completeness: given a choice between showing a possibly-wrong number and showing "unknown", the system shows "unknown".
- **REQ-N-02** Every displayed number SHALL be traceable to its source records via a drill-down path.
- **REQ-N-03** Component failure SHALL degrade gracefully and visibly. A dead ingestion worker SHALL produce a visible staleness indicator on every affected view, not a silently frozen chart.
- **REQ-N-04** All persistent state SHALL survive process restart. Ingestion SHALL resume from a durable checkpoint.

### 6.2 Performance

- **REQ-N-05** Screener queries over the full universe: < 2s.
- **REQ-N-06** Chart render on a loaded view: < 1s.
- **REQ-N-07** Stream event to persisted, queryable state: < 5s at p95.
- **REQ-N-08** Full backtest over 2 years × 100 collections: < 10 minutes. *Until the platform's own history reaches that depth (see §7.1), this budget is verified against synthetic data of equivalent size; it is a performance target, not a claim that two years of real history exists.*

### 6.3 Configurability

- **REQ-N-09** All thresholds, weights, universe criteria, cost assumptions, chart interval and anchored-range definitions, and every registered assumption SHALL live in version-controlled configuration, never in code.
- **REQ-N-10** Configuration changes SHALL be logged with timestamp and rationale. When results change, it must be possible to determine whether the world changed or the configuration did.

### 6.4 Security and operational safety

- **REQ-N-11** API keys and RPC credentials SHALL be stored outside the repository, loaded from environment or a local secret store.
- **REQ-N-12** The system SHALL NOT require, request, or store private keys or wallet seed phrases in v1. Execution, when it arrives, will be manual or via an explicitly-scoped session.
- **REQ-N-13** The system SHALL NOT expose a network listener beyond localhost.
- **REQ-N-14** No component SHALL be capable of submitting a transaction. Order submission is out of scope for v1 and requires an explicit amendment plus the safety controls in §7.4.

### 6.5 Reproducibility

- **REQ-N-15** Any analysis, chart, backtest, or trade idea SHALL be reproducible from a recorded (code version, config version, data-as-of timestamp) triple.
- **REQ-N-16** Dependencies SHALL be pinned. Random seeds SHALL be recorded.

---

## 7. Constraints and Risks

### 7.1 Hard constraints

| Constraint | Implication |
|---|---|
| 120 REST reads/hour (measured) free tier | Universe size is bounded by refresh budget. Stream-first architecture is mandatory, not optional |
| Stream is lossy and unordered | Reconciliation and gap-backfill are core features, not polish |
| No historical floor series from OpenSea | The platform's own history is an asset that accrues value only with uptime — **start collecting on day one, before the analysis layer exists** |
| Free instant keys expire in 7 days | Key lifecycle management required |
| Single local machine | 24/7 background engine is best-effort; cloud migration is a known future decision |

### 7.2 Analytical risks

| Risk | Mitigation |
|---|---|
| Wash trading corrupts all volume/price statistics | Detection + dual-mode analytics (REQ-D-22..24) |
| Tiny sample sizes make everything look significant | Mandatory uncertainty reporting (REQ-F-19); minimum-N gates in validation |
| Overfitting to a handful of collections | Walk-forward, out-of-sample holdout, multiple-testing correction (validation doc §5) |
| Survivorship bias — dead collections vanish from screens | Universe must include delisted/dead collections in historical tests |
| Backtest looks great, live results don't | Cost and fill realism (REQ-F-29..31); mandatory paper-trading gate |
| Regime change invalidates the model | Changepoint monitoring; scheduled model revalidation |

### 7.3 Market and personal risk

The platform can lose money. Requirements that exist specifically to bound that:

- **REQ-R-01** A per-collection and per-idea position-size ceiling SHALL be computed as the minimum of a liquidity ceiling, a conviction ceiling, and a concentration ceiling (methodology §9.2), and SHALL be displayed on every Trade Idea with the binding constraint named. The binding constraint in an illiquid market is *how much you can get out of*, not how much you can put in.
- **REQ-R-02** The system SHALL track and display total capital at risk and concentration by collection.
- **REQ-R-03** The system SHALL surface an explicit illiquidity warning when estimated time-to-exit exceeds a configured threshold.

### 7.4 Automation safety (forward-looking)

Should automated execution ever be added, these are preconditions, recorded now so they are not negotiated away later:

- Hard per-transaction and per-day spend caps enforced below the signing layer.
- A kill switch that halts all activity, reachable without the UI.
- Mandatory human confirmation above a value threshold.
- A minimum of 90 days of paper-trading evidence for the specific strategy.
- Dry-run mode as the default; live mode requires explicit, expiring activation.

---

## 8. Data Model (logical)

Core entities and the relationships that matter:

```
Chain ──< Collection ──< Token ──< TokenTrait
                │            │
                │            ├──< Listing   (order state over time)
                │            ├──< Offer
                │            ├──< Sale      (+ wash_score)
                │            └──< Transfer  (+ transfer_class)
                │
                ├──< CollectionStateSnapshot   (the floor/depth time series WE build)
                ├──< TraitFloor                (per trait-value floor over time)
                └──< PeerGroupMembership

Wallet ──< WalletPosition ──> Token
   └──< WalletFlowMetric

Signal ──< SignalEvaluation ──< TradeIdea ──< IdeaOutcome
Strategy ──< BacktestRun ──< BacktestTrade
Strategy ──< PaperPosition ──< PaperFill
DataQualityCheck ──< DataQualityResult
```

**Cross-cutting requirements on every fact table:**

- `observed_at` (when the system learned it) and `valid_at` / `valid_from`–`valid_to` (when it was true) — bitemporality per REQ-F-04.
- `source` enum: `stream` | `rest` | `chain` | `scrape` | `derived`.
- `ingestion_run_id` for lineage.
- Immutable landing-zone payload reference (REQ-F-03).

---

## 9. Architecture (logical, stack-committed)

**Committed stack:** Python 3.12+, local-first.

```
┌─────────────────────────────────────────────────────────┐
│  PRESENTATION                                           │
│  Dashboard (multi-pane, linked) · Screener · Charts      │
│  Asset detail · Backtest UI · Paper trading · Alert feed │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────┴─────────────────────────────────┐
│  ANALYSIS                                               │
│  Metrics · Statistics (3 tiers) · Signal library         │
│  Backtest engine · Paper trading · Trade analysis engine │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────┴─────────────────────────────────┐
│  STORAGE                                                │
│  Landing zone (immutable raw) · Normalized store         │
│  Derived/feature store · Bitemporal time series          │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────┴─────────────────────────────────┐
│  INGESTION                                              │
│  Stream consumer · REST governor (budget+priority)       │
│  Chain indexer · Reconciler · Gap detector · Scraper     │
└─────────────────────────────────────────────────────────┘
```

**Technology decisions:**

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12+ | Statistical and backtesting ecosystem is decisive here |
| Landing zone | zstd-compressed JSONL files | Immutable raw record. Append-friendly and crash-safe (a killed process leaves valid data), schema-tolerant when OpenSea adds fields, byte-faithful. Also the replay corpus. See `07_STORAGE_AND_RECORDING.md` §2 |
| Analytical store | DuckDB + Parquet | Columnar, fast for the scan-heavy cross-sectional work, zero-ops, file-based so it is trivially backed up and versioned. **This is where all market data is queried from** |
| Operational store | SQLite | Bookkeeping only — ingestion checkpoints, gap register, alert state, paper positions, REST budget ledger, config change log. **Holds no market data** |
| Stream client | `websockets` / OpenSea SDK | — |
| Stats | `statsmodels`, `scipy`, `arviz`/`pymc` for hierarchical models | — |
| Charts | Plotly | Interactive, linked selection, dense multi-pane layouts |
| Dashboard | Local web app (Dash or a React front end over a FastAPI backend) | Reference UI density requires real layout control; Streamlit is likely insufficient for §5.3 |
| Orchestration | Prefect or plain asyncio + APScheduler | Start simple |

Deferred to an ADR after Phase 1: whether the dashboard is Dash or React+FastAPI, and whether to migrate the background engine to a VPS for 24/7 uptime.

---

## 10. Phased Delivery

**Phase 0 — Foundation (start immediately, in parallel with everything else)**
Stream consumer, landing zone, REST governor, watchlist config, snapshot writer. **Deliverable: the historical record begins accumulating.** This has the highest time-value of any work in the project — every day of delay is a permanently missing day of history that cannot be bought back.
*Exit:* 7 consecutive days of ingestion with <1% gap, reconciliation drift within threshold.

**Phase 1 — Consolidation & visibility**
Normalized store, core metrics, screener, watchlist grid, time-series and cross-sectional charts, asset detail view.
*Exit:* Operator can answer "what is interesting today?" in under 5 minutes without opening OpenSea.

**Phase 2 — Analysis**
Basic + intermediate statistics, trait pricing model, wash-trade detection, distribution and trait-space visualizations, peer grouping.
*Exit:* A trait-adjusted fair-value estimate exists for every watchlist token, with calibrated uncertainty.

**Phase 3 — Signals & backtesting**
Signal framework, bitemporal point-in-time enforcement, backtest engine with full cost/fill/exit realism, walk-forward tooling.
*Exit:* At least one signal has passed the full validation protocol, including out-of-sample.

**Phase 4 — Engine & paper trading**
Background trade analysis engine, alerting, watchlist paper testing, Investment Strategy Test framework, outcome tracking.
*Exit:* 30 days of continuous operation with tracked outcomes on every emitted idea.

**Phase 5 — Advanced**
Hierarchical/Bayesian and state-space models, wallet-flow network analysis, ML models (gated), cross-chain expansion, 24/7 hosting decision.

**Dependency note:** Phase 0 is the only phase that must not be deferred. Phases 1–2 can overlap. Phase 3 cannot begin until Phase 0 has produced enough history to test against — realistically 60–90 days. Plan for that gap by using it for Phase 1–2 work.

---

## 11. Open Questions

| # | Question | Needed by | Default if unanswered |
|---|---|---|---|
| Q1 | Initial watchlist — which collections, how many? | Phase 0 | 25 collections meeting §2.2, operator-selected |
| Q2 | Capital scale? (Determines whether position-size ceilings ever bind) | Phase 4 | Assume ceilings bind; design for it |
| Q3 | Snapshot cadence vs. REST budget tradeoff | Phase 0 | Hourly for watchlist, daily for wider universe |
| Q4 | Is a paid data provider acceptable later if free-tier limits prove fatal? | Phase 2 | Assume no; design to survive on free tier |
| Q5 | Dash vs. React+FastAPI | Phase 1 | Dash first, migrate if §5.3 density is unachievable |
| Q6 | Acceptable time-to-exit ceiling for a tradeable idea | Phase 3 | 30 days |
| Q7 | 24/7 uptime — accept laptop-only, or move to VPS? | Phase 4 | Laptop-only with gap backfill |

---

## 12. Acceptance Criteria

The platform is considered successful when all of the following hold:

1. **Data** — 30 consecutive days of ingestion at <1% event gap, with reconciliation drift within threshold, and the Operator's own floor-price history is longer and more complete than any available third-party source for the watchlist.
2. **Consolidation** — the Operator can answer each of the ten benchmark questions in §12.1a without leaving the platform, in under 60 seconds each.
3. **Visualization** — any two stored variables can be plotted against each other, cross-sectionally or over time, in under 30 seconds of interaction.
4. **Statistics** — every tier-1 and tier-2 technique in the methodology document is implemented, tested against known-answer fixtures, and reports uncertainty.
5. **Backtesting** — the engine passes the point-in-time leakage test suite, and a deliberately-planted look-ahead strategy is *detected and rejected* rather than reported as profitable.
6. **Engine** — 30 days of continuous operation with every emitted idea outcome-tracked; alert precision above an agreed threshold.
7. **Paper trading** — at least one Investment Strategy Test has run to its predefined review date and reached a verdict, with results reported against the ETH-hold benchmark.
8. **The honest test** — the platform has either (a) produced at least one validated, out-of-sample-positive signal net of realistic costs, or (b) produced clear evidence that a hypothesized edge does not exist. **Both are successful outcomes.** Outcome (b) delivered cheaply and early is more valuable than outcome (a) delivered on the strength of an overfit backtest.

### 12.1a The ten benchmark questions

Fixed now so that acceptance cannot be redefined later to match whatever was built:

1. Which watchlist collections moved most on quality-adjusted value in the last 7 days, and on what volume?
2. Which collections show a rising sale rate but a flat price?
3. For collection C, what is the listing depth curve, and what would it cost to buy five tokens?
4. For collection C, which listed tokens are furthest below trait-adjusted fair value, and how wide are those intervals?
5. For token T, what are the comparable sales, and what is fair value with its interval?
6. Which collections had a changepoint in sale rate or unique buyers in the last 30 days?
7. Where has the holder base concentrated or dispersed most in the last 30 days?
8. Which collections have the widest and narrowest bid-ask spreads right now?
9. For collection C, what is the expected time-to-sale at each price percentile?
10. Which collections' wash ratio changed materially in the last 30 days?

---

## 13. Change Control

This document is the contract. Amendments require: a written statement of what changes, why, what it invalidates, and re-approval. Requirement IDs are permanent — superseded requirements are marked `DEPRECATED` with a pointer, never deleted, so that historical decisions remain legible.

---

## Sources

- [OpenSea API Overview](https://docs.opensea.io/reference/api-overview)
- [OpenSea API Keys and Rate Limits](https://docs.opensea.io/reference/api-keys)
- [OpenSea Stream API Overview](https://docs.opensea.io/reference/stream-api-overview)
- [OpenSea Events by Collection endpoint](https://docs.opensea.io/reference/list_events_by_collection)
