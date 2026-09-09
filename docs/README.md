# OpenSea Dashboard & Analysis Tool — Foundational Documents

**Version:** 1.0 · **Date:** 2026-09-09 · **Status:** Draft, awaiting Operator sign-off

These documents are the project's contract. No implementation work should begin until §12 of the Requirements document is agreed.

## Read in this order

| # | Document | Answers |
|---|---|---|
| 0 | [`00_REQUIREMENTS.md`](00_REQUIREMENTS.md) | What is being built, what is explicitly out of scope, what "done" means |
| 1 | [`01_METHODOLOGY.md`](01_METHODOLOGY.md) | How the analysis works, what we measure, what standard of evidence a signal must meet |
| 2 | [`02_AGENT_HIERARCHY.md`](02_AGENT_HIERARCHY.md) | Who does what, with what authority, through which workflows |
| 3 | [`03_VALIDATION_AND_TESTING.md`](03_VALIDATION_AND_TESTING.md) | How we find out we are wrong, before it costs money |
| 4 | [`04_ENVIRONMENTS.md`](04_ENVIRONMENTS.md) | Code environments, data environments, branching, promotion gates |
| 5 | [`05_BUG_TAXONOMY.md`](05_BUG_TAXONOMY.md) | Severity hierarchy, error classes, bug log schema, triage |
| 6 | [`06_TIME_UNITS_AND_LAYERING.md`](06_TIME_UNITS_AND_LAYERING.md) | Chart intervals, denominations and transforms, the assumption/structure boundary |
| 7 | [`07_STORAGE_AND_RECORDING.md`](07_STORAGE_AND_RECORDING.md) | The three stores, stream recording format and sizing, when REST actually binds |

Agent definitions live in `.claude/agents/`. Any new Claude session reads 0–3, then states its role, authority level, and permitted data partitions before beginning work (`02_AGENT_HIERARCHY.md` §10).

## The seven decisions everything else follows from

1. **Stream-first, REST-rationed.** The OpenSea free tier allows 120 REST reads per hour (measured); the WebSocket Stream API is unmetered. The stream is the primary ingestion path and REST is a budgeted, priority-queued resource. Every architectural choice downstream follows from this.

2. **The historical record is the asset.** OpenSea provides no historical floor time series. The platform builds its own, and it can only be built forward from the day ingestion starts. **Phase 0 is the only phase that must not be deferred** — every day of delay is a permanently missing day.

3. **Spencer approves every pull request.** No auto-merge, ever, at any authority level. A Validator sign-off is permission to ask, not permission to merge.

4. **Liquidity is time-to-clear, and it is a curve.** *(Formalization deferred — working program and valid database first. `immediacy_cost` is tracked from Phase 1 regardless, since it needs no model.)* The Operator's definition — how long the market takes to clear — is canonical. Because you can always clear instantly at a low enough price, liquidity is `time_to_clear(price)`, and the bid-ask spread is simply the price of immediacy. Position size is bounded by this curve, and it is almost always the binding constraint.

5. **Floor price is not a price.** It is one seller's opinion about one token. The canonical collection price series is `qa_index`, the quality-adjusted index from the hedonic model. Treating raw floor as a price series is the foundational error of NFT analytics and the methodology is built to avoid it.

6. **The proposer never validates.** The agent that builds a signal hands off at `SPECIFIED` and its involvement ends. Self-validation is how overfitting becomes invisible, and this separation is the single most important structural rule in the document set.

7. **Killing signals cheaply is the goal.** Most hypotheses will fail. A validation process that produces no negative results is not working. The economic-significance screen runs first, before any statistical work, precisely because it disqualifies most candidates in one step.

## Committed stack

Python 3.12+ · DuckDB + Parquet (analytical) · SQLite (operational) · statsmodels/scipy/PyMC · Plotly · local web dashboard · local-first, single operator.

Primary test collection: **Argonauts** (see `06_TIME_UNITS_AND_LAYERING.md` §5).

Deferred to an ADR after Phase 1: Dash vs. React+FastAPI for the dashboard; whether the background engine moves to a VPS for 24/7 uptime.

## Open questions blocking Phase 0

See Requirements §11. The two that need an answer soonest:

- **Q1** — which collections form the initial watchlist, and how many?
- **Q3** — snapshot cadence, which is a direct tradeoff against the REST budget.

## Sources

API constraints in these documents were verified against OpenSea's documentation on 2026-09-09:

- [API Overview](https://docs.opensea.io/reference/api-overview)
- [API Keys and Rate Limits](https://docs.opensea.io/reference/api-keys)
- [Stream API Overview](https://docs.opensea.io/reference/stream-api-overview)
- [Events by Collection](https://docs.opensea.io/reference/list_events_by_collection)
