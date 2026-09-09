# Validation and Testing Process

**Version:** 1.0
**Date:** 2026-09-09
**Companion to:** `00_REQUIREMENTS.md`, `01_METHODOLOGY.md`, `02_AGENT_HIERARCHY.md`

---

## 1. Purpose and Philosophy

### 1.1 What this process is for

The purpose of validation here is **not** to demonstrate that the platform works. It is to find the ways in which it does not, as early and as cheaply as possible.

That distinction matters more in this project than in most software. A conventional application that is subtly wrong produces visible errors. A quantitative trading platform that is subtly wrong produces *confident, plausible, profitable-looking output* and loses money silently. There is no crash. The failure mode is a beautiful backtest.

### 1.2 The two failure classes

| Class | Question | Section |
|---|---|---|
| **Software correctness** | Does the code compute what it claims to compute? | §3–§4 |
| **Analytical validity** | Does the thing it computes mean what we think it means? | §5–§8 |

Standard software testing addresses only the first. The second requires the statistical protocol in §5–§7, and is where the money is actually at risk.

### 1.3 Governing principles

1. **Validation is independent.** The Validator agent never validates its own work (`02_AGENT_HIERARCHY.md` §3.5). Structural, not aspirational.
2. **Suspicion is proportional to good news.** A surprisingly strong result triggers *more* scrutiny, not celebration.
3. **Cheap tests first.** Order tests so the ones most likely to kill a bad idea run earliest.
4. **Negative results are outputs.** A killed signal is a successful validation.
5. **Tests are permanent.** Every bug found becomes a regression test. Every failure mode becomes a monitor.
6. **The data record is sacred.** Tests never write to the landing zone or historical store.

---

## 2. The Validation Pyramid

```
                    ┌───────────────────┐
                    │  LIVE PAPER       │  weeks–months, highest cost
                    │  TRADING GATE     │  the only honest evaluation
                    └─────────┬─────────┘
                ┌─────────────┴─────────────┐
                │  OUT-OF-SAMPLE +          │  hours, once per signal
                │  WALK-FORWARD             │
                └─────────────┬─────────────┘
            ┌─────────────────┴─────────────────┐
            │  BACKTEST INTEGRITY +             │  minutes
            │  ROBUSTNESS SUITE                 │
            └─────────────────┬─────────────────┘
        ┌─────────────────────┴─────────────────────┐
        │  STATISTICAL VALIDITY CHECKS              │  seconds
        │  (sample size, assumptions, multiplicity) │
        └─────────────────────┬─────────────────────┘
    ┌─────────────────────────┴─────────────────────────┐
    │  ECONOMIC SIGNIFICANCE SCREEN                     │  instant
    │  Does the edge clear round-trip costs at all?     │  kills most candidates
    └─────────────────────────┬─────────────────────────┘
┌───────────────────────────────┴───────────────────────────────┐
│  SOFTWARE CORRECTNESS: unit · property · integration · fixture │  continuous
└───────────────────────────────────────────────────────────────┘
```

Run bottom-up. The economic significance screen sits deliberately low because it is instant and disqualifies the majority of candidates before any expensive work is done.

---

## 3. Data Quality Gates

Data quality is validated continuously in production, not just in test. A platform whose analysis is correct but whose data is wrong is worthless in exactly the same way.

### 3.1 Ingestion-time checks (blocking)

Applied to every record before it enters the normalized store. Failures quarantine the record and alert — they never discard it.

| Check | Rule |
|---|---|
| Schema conformance | Matches expected shape and types |
| Required fields | Present and non-null |
| Timestamp sanity | Not in the future; not before the chain's genesis; within a plausible band |
| Price sanity | Non-negative; below a configured absurdity ceiling |
| Currency | Recognized token, with a known decimals value |
| Referential integrity | Referenced collection/token resolvable or resolvable-later-flagged |
| Duplicate detection | Idempotency key check |

### 3.2 Continuous monitoring checks (alerting)

| Check | Threshold | Rationale |
|---|---|---|
| Stream connection alive | Any disconnect | Gap begins accumulating |
| Event rate within band | Robust quantile band on the log event rate, seasonalized by hour-of-week, with a sustained-breach requirement (N consecutive intervals) | Stream events are bursty and heavy-tailed; a ±3σ Gaussian band would alarm on every normal activity spike and produce exactly the alert fatigue REQ-F-25 warns about. The rate falling to **zero** is a separate, immediate, non-suppressible alert |
| REST budget burn rate | On track for the hour | Prevents exhaustion mid-hour |
| Reconciliation drift | Configurable per metric | Stream-derived and REST-derived state diverging from **each other**. Neither is declared truth: the stream is the operational source of current state (REQ-D-05), the REST snapshot is an independent corroborator. Drift measures disagreement and localizes a bug; it does not identify which side is wrong |
| Snapshot series continuity | No unexplained gap | The historical asset must be complete |
| Metric distribution stability | Population stability index | Detects upstream semantic change |
| Wash ratio stability | Sudden spike | Either real manipulation or a broken detector |
| Null/zero rate per field | Above baseline | Upstream field deprecation |
| Cross-source agreement | Chain-derived vs. API-derived holder counts | Independent corroboration |

### 3.3 The reconciliation test

The single most valuable ongoing data check, because it independently corroborates the primary ingestion path.

```
For a rotating subset of collections (REST budget permitting):
  1. Compute collection state from stream-derived data
  2. Fetch a REST snapshot
  3. Compare: floor, listed count, best offer, supply
  4. Record drift per metric, per collection, over time
  5. Drift trending upward = systematic bug, not noise → escalate
```

Neither side is treated as ground truth; the value of the check is that two independent paths agreeing is strong evidence both are right. A drift metric that is small and stationary is evidence the stream pipeline is correct. A drift metric that grows is evidence of an accumulating error, which is precisely the bug class that is otherwise invisible.

### 3.4 Historical integrity audit (weekly)

- No records modified in the landing zone (checksum verification against a stored manifest).
- Bitemporal invariants hold: no record has `observed_at < valid_at` where that is impossible.
- Every derived metric traceable to source records.
- Provenance tags present on 100% of records.
- Gap register matches actual gaps in the snapshot series.

---

## 4. Software Testing

### 4.1 Unit tests

Standard coverage of pure functions, with emphasis on:

- **Metric calculations against hand-computed fixtures.** Every metric in methodology §3 has a small fixture dataset with a manually verified expected value. This is tedious and it is the only way to know a metric is right.
- **Edge cases specific to this domain:** empty collection, single listing, all listings identical, zero sales in window, token with no traits, collection with one holder, sale in an unusual currency, negative-implied values.
- **Precision:** ETH values must not lose precision. Test with wei-level integers; assert no floating-point drift in accumulations.

### 4.2 Property-based tests

More valuable than example-based tests for the invariant-heavy parts of this system:

| Property | Statement |
|---|---|
| Idempotency | Replaying any event sequence yields identical state |
| Order invariance | Events applied in any order, then sorted by `event_timestamp`, converge to the same state |
| Monotonicity | Cumulative volume never decreases as events are added |
| Bounds | `floor ≥ 0`; `listed_ratio ∈ [0,1]`; `gini ∈ [0,1]`; percentiles ordered |
| Conservation | Token supply = mints − burns; holder balances sum to supply |
| Denomination | `usd_value == eth_value × eth_usd_rate` to within tolerance, at the correct historical rate |
| Uncertainty | Every estimator returns an interval containing its point estimate |

### 4.3 Integration tests

- Full pipeline: synthetic stream event → landing zone → normalized → metric → signal evaluation.
- Gap-and-backfill: simulate disconnect, verify gap detection, backfill, and final-state correctness.
- Rate-limit governor: verify priority ordering, 429 backoff, budget exhaustion behavior, and that no component can bypass it.
- Reconciliation: inject known drift, verify detection and alerting.

### 4.4 Fixture datasets

Maintained permanently, version-controlled:

| Fixture | Purpose |
|---|---|
| `golden_collection` | Small hand-verified collection with known correct values for every metric |
| `known_wash` | Hand-labelled wash-trading examples for detector calibration |
| `sparse_collection` | 8 sales — exercises shrinkage and minimum-N gates |
| `gap_scenario` | Ingestion with known holes |
| `leakage_trap` | A strategy with deliberate look-ahead, which the backtester **must reject** |
| `regime_change` | Series with a known changepoint at a known index |
| `censored_listings` | Listings dataset with known censoring, for survival-model validation |

### 4.5 Analytical code review

Analytical code receives a distinct review focused on statistical correctness, not code style:

- Are the estimator's assumptions stated and checked?
- Is the sample size reported the *effective* size?
- Is uncertainty propagated through every transformation, or dropped somewhere?
- Are there any implicit forward-looking references?
- Is the denominator correct? (ETH vs USD, filtered vs raw, supply vs circulating)
- Does the function fail loudly on insufficient data, or return a number anyway?

---

## 5. Statistical Validation Protocol

Applied to every metric, model, and signal before it is trusted.

### 5.1 Gate 1 — Economic significance (instant, run first)

```
edge_estimate > marketplace_fee + royalty + gas_in + gas_out
              + spread_cost + slippage + opportunity_cost
```

Run in two passes:

- **Gate 1a (screen).** A conservative point estimate of the edge — the raw effect size, haircut by a fixed factor — against the round-trip cost. This requires no inference and is the instant screen.
- **Gate 1b (confirm).** Once Gate 3 has produced an interval, the same test is re-run at the **lower bound** of that interval. A candidate that clears 1a but fails 1b is rejected at that point.

**Gate 1a FAIL → reject immediately.** No statistical work is performed. This gate exists to avoid spending days validating a 4% edge in a market with a 20–40% round-trip cost (methodology §8.1), and it will reject the majority of candidates.

### 5.2 Gate 2 — Sample adequacy

| Requirement | Threshold |
|---|---|
| Effective sample size | ≥ methodology §8.2 minimum for the analysis type |
| Independent events | ≥50 signal firings across ≥10 distinct collections |
| Clustering | Standard errors clustered by collection and by time |
| Power | **Pre-specified** minimum detectable effect, with design power ≥ 0.80 at the economically meaningful effect size from Gate 1, computed **before** results are seen. Post-hoc/observed power is explicitly prohibited — computed from the realized effect it is a monotone transform of the p-value and adds no information, so a gate on it would pass exactly what the significance test already passed |

**Effective, not nominal.** Fifty firings inside one collection in one month are approximately one observation. The validator computes effective sample size accounting for cross-sectional and serial correlation, and rejects on the effective figure.

### 5.3 Gate 3 — Assumption diagnostics

Every model reports diagnostics; violations are surfaced, not buried:

- **Regression:** residual normality, heteroskedasticity (Breusch–Pagan), influence (Cook's distance, leverage), multicollinearity (VIF), specification (RESET).
- **Time series:** stationarity (ADF/KPSS), autocorrelation (Ljung–Box), structural stability (CUSUM).
- **Bayesian:** R̂ < 1.01, ESS adequate, zero divergences, prior and posterior predictive checks. **A non-converged model is rejected, not caveated.**
- **Survival:** proportional-hazards assumption (Schoenfeld residuals), censoring-mechanism plausibility.

### 5.4 Gate 4 — Multiplicity

- The hypothesis register is consulted: how many hypotheses have been tested in this family?
- Benjamini–Hochberg FDR at q=0.10 for exploratory work; Bonferroni for confirmatory claims.
- **Deflated Sharpe ratio** reported for any backtested strategy, adjusted for the number of configurations trialled.
- Validator independently audits the register for undisclosed testing — comparing the register against the actual experiment logs.

### 5.5 Gate 5 — Robustness

| Test | Pass criterion |
|---|---|
| Parameter perturbation | Sign and approximate magnitude stable under ±20% on every threshold |
| Sub-period stability | Effect present in ≥2 of 3 sub-periods, correct sign in all |
| Sub-universe stability | Holds across price tiers and categories, not driven by one segment |
| Wash-filter sensitivity | **Sign must not flip** between raw and filtered data |
| Cost sensitivity | Survives a 1.5× cost assumption |
| Leave-one-collection-out | No single collection accounts for >30% of the effect |
| Outlier sensitivity | Removing the top 5% of outcomes does not eliminate the effect |

The leave-one-collection-out and outlier tests are frequently decisive. A "signal" whose entire profit comes from one collection during one month is a story about that collection, not a signal.

---

## 6. Backtest Integrity

### 6.1 Structural enforcement of point-in-time correctness

Look-ahead bias is not prevented by careful coding. It is prevented by making the data physically unavailable.

- The backtest data-access layer accepts an `as_of` timestamp and **cannot return** any record whose `observed_at` exceeds it. This is enforced at the query layer, below the strategy code.
- A strategy has no access to a raw data connection. There is no path around the accessor.
- Every backtest data access is logged with its `as_of` and the returned record range, and the log is audited for violations.

### 6.2 The leakage trap test

Run before every backtest release: a strategy in the fixture set contains deliberate look-ahead (it references tomorrow's price). The backtester **must detect and reject it**.

If the leakage trap ever reports a profit, the backtester's integrity guarantee is broken and all backtest results produced since the last passing run are invalidated. This test is the backtester's own smoke alarm.

### 6.3 Cost realism audit

The Validator independently re-derives costs and compares them against what the backtest charged:

| Cost | Requirement |
|---|---|
| Marketplace fee | Historical rate at that date, not current |
| Creator royalty | Collection-specific, historical, honoring the enforcement regime at the time |
| Gas | **Historical gas price at that block**, both legs. Failed-transaction cost included |
| Spread | Actual observed bid-ask at that timestamp, not an assumption |
| Slippage | Modelled from observed book depth at that timestamp |
| Currency conversion | Historical ETH/USD at that timestamp |

**Total costs paid as a percentage of gross profit must be reported.** A strategy where costs consume 80% of gross profit is fragile in a way the net return alone does not convey.

### 6.4 Fill realism audit

- Fills occur only against listings that actually existed, at their actual prices.
- Fill probability accounts for competition — a listing far below fair value would likely have been taken by someone else. A backtest that assumes it always won the race is fiction.
- Partial fills where the desired size exceeds available depth.
- **Exit realism (the critical one):** exits are simulated against the survival model's time-to-sale distribution (methodology §5.5), not assumed instant. A position that would have taken 90 days to exit is held for 90 days in the simulation, with the market risk that implies.

### 6.5 Walk-forward analysis

Rolling origin: train on window W, test on the following period, roll forward, repeat. **Confined to the training and validation partitions** — the rolling origin never enters the test partition, which remains reserved for the single evaluation in methodology §7.4.

- Reported per-window, not only in aggregate. Consistency across windows matters more than the average.
- Parameter stability across windows is examined. Wildly varying optimal parameters indicate fitting to noise.
- Performance decay from in-sample to out-of-sample is quantified. Decay above a threshold (default 50%) fails.

### 6.6 Benchmark comparison (mandatory)

Every strategy is reported against:

1. Buy-and-hold the collection floor over the same period
2. Buy-and-hold ETH over the same period
3. A random-selection strategy with the same trade count and holding periods, over many draws

**A strategy that does not beat holding ETH, net of costs and risk-adjusted, is not a strategy.** This comparison is reported first in every backtest result, before the strategy's own figures, so it cannot be skipped past.

---

## 7. Paper Trading Gate

The final and only fully honest evaluation. No signal goes live without passing it.

### 7.1 Protocol

| Requirement | Value |
|---|---|
| Minimum duration | 30 days, or 30 independent **forward** firings, whichever is longer. This is not in tension with the ≥50 events required at Gate 2 (§5.2): that is the historical evidence base required to reach this gate, while these are new observations generated after specification. Both must be satisfied |
| Success criteria | Defined **before** the period begins, in writing, immutably |
| Position records | Immutable at creation. Entry, thesis, target, invalidation cannot be edited (REQ-F-36) |
| Tracking | All firings tracked, including ones not acted on |
| Benchmarks | Reported against §6.6 benchmarks |

### 7.2 Evaluation

Compare live paper results against backtest expectation:

- Hit rate within the backtest's confidence interval?
- Average return per trade consistent?
- Actual holding periods consistent with the survival model's predictions?
- Realized costs consistent with modelled costs?
- **Any systematic divergence is investigated before promotion, not averaged away.** Divergence between paper and backtest is the clearest available evidence of a backtest flaw.

### 7.2a Investment Strategy Test protocol (REQ-F-35)

Distinct from the per-signal gate above. An Investment Strategy Test evaluates a *strategic hypothesis* about the market ("thin gaming collections re-rate after a mint by the same creator") rather than a mechanical signal, and it is the vehicle for acceptance criterion §12.7.

| Requirement | Value |
|---|---|
| Registration | Written hypothesis, mechanism, universe, success criteria, review date, and prior — all recorded and immutable **before** the period begins |
| Success criteria | Quantitative and pre-committed. "It felt directionally right" is not a verdict |
| Review date | Fixed at registration. It may be extended **once**, with a written reason recorded before the original date passes — never after seeing the result |
| Positions | Immutable at creation (REQ-F-36) |
| Benchmarks | §6.6, including the ETH-hold comparison (REQ-F-37) |
| Verdict | One of: SUPPORTED / NOT SUPPORTED / INCONCLUSIVE (pre-specified sample not reached). Recorded with the realized outcome against the stated prior, feeding §8.3 calibration |

A NOT SUPPORTED verdict closes the hypothesis. Re-testing a variant requires a new registration with its own mechanism, and the register records it as a descendant of the failed one so that the multiplicity correction in §5.4 counts the family, not the individual test.

### 7.3 Promotion decision

Only the Operator promotes. The decision record states: the evidence, the residual concerns, the position-size limit, the monitoring plan, and the predefined conditions under which the signal will be retired.

### 7.4 Post-promotion monitoring

- Continuous comparison of live performance to expectation.
- Degradation alert when live hit rate falls below the lower bound of the validated interval for a sustained period.
- Scheduled revalidation (monthly refit, quarterly full review).
- Automatic retirement trigger on sustained underperformance — defined in advance so the decision is not made under the influence of sunk cost.

---

## 8. Verification of This Process

The validation process itself needs verification, or it becomes ritual.

### 8.1 Deliberate-failure injection

Periodically, known-bad inputs are introduced and the process must catch them:

| Injected fault | Must be caught by |
|---|---|
| Look-ahead strategy | §6.2 leakage trap |
| Signal profitable only via wash trades | §5.5 wash-filter sensitivity |
| Signal driven by one collection | §5.5 leave-one-out |
| Understated costs | §6.3 cost audit |
| Overfit parameter set | §5.5 perturbation, §6.5 walk-forward decay |
| Corrupted ingestion | §3.1/§3.2 quality gates |
| Silent stream failure | §3.2 event-rate monitoring |

A fault that passes through is a process defect, and the process is amended.

### 8.2 Validator calibration

The Validator's own record is tracked: how many signals it passed that subsequently failed in live paper trading, and how many it blocked that would have worked. A Validator that never blocks anything is not validating.

### 8.3 Calibration of predictions

Registered hypotheses carry a stated prior (methodology §7.1). Over time, the realized rate at which hypotheses with a stated prior of X actually validate is compared to X. Systematic overconfidence is measurable, and once measured, correctable.

---

## 9. Definition of Done

Work is complete only when all applicable rows pass.

### 9.1 Any code change
- [ ] Unit tests, including domain edge cases
- [ ] Property tests for stated invariants
- [ ] Integration test if it crosses a component boundary
- [ ] Independent review (Validator for anything analytical)
- [ ] Requirement ID referenced
- [ ] No new unexplained data-quality alerts

### 9.2 A new metric
- [ ] Definition added to methodology §3
- [ ] Hand-verified fixture test
- [ ] Sample size and provenance reported with every value
- [ ] Uncertainty reported where applicable
- [ ] Behavior on insufficient data is to fail loudly, not to return a number
- [ ] Surfaced with provenance in the UI

### 9.3 A new signal
- [ ] Hypothesis registered with mechanism, before data examination
- [ ] Gate 1 economic significance passed
- [ ] Gates 2–5 passed
- [ ] Independently validated (not by the author)
- [ ] Backtest passes integrity, cost, and fill realism audits
- [ ] Beats all three §6.6 benchmarks
- [ ] Walk-forward decay within threshold
- [ ] Out-of-sample test partition evaluation — one, logged
- [ ] 30+ days paper trading against predefined criteria
- [ ] Operator promotion decision recorded with retirement conditions

### 9.4 Non-functional and operational requirements

Checked at release and re-verified monthly, since these degrade silently:

- [ ] REQ-N-09/10 — every threshold lives in version-controlled config; config changes carry a timestamp and rationale in the change log
- [ ] REQ-N-11 — no credential present anywhere in the repository (automated secret scan in CI)
- [ ] REQ-N-12 — automated search confirms no private-key or seed-phrase handling code path exists
- [ ] REQ-N-13 — port scan confirms no listener bound beyond localhost
- [ ] REQ-N-14 — automated check confirms no transaction-signing or order-submission capability is reachable from any runtime agent. **This is a standing test, not a one-time review**
- [ ] REQ-N-15 — a randomly selected past analysis is reproduced from its recorded (code version, config version, data-as-of) triple and matches
- [ ] REQ-N-16 — dependencies pinned; a recorded seed reproduces a stochastic result exactly
- [ ] REQ-D-06 — API key expiry detection verified by simulating an expired key
- [ ] REQ-D-21 — if the Scraper is enabled, a current ToS review is on record; if no review is on record, the Scraper is disabled

### 9.5 A pipeline change
- [ ] Shadow-run comparison with every difference explained
- [ ] Historical reinterpretation impact assessed
- [ ] Backfill plan within REST budget
- [ ] Provenance log updated
- [ ] Rollback path available

---

## 10. Test Environments

| Environment | Data | Purpose |
|---|---|---|
| **Fixture** | Synthetic, hand-verified | Unit and property tests. Fast, deterministic |
| **Replay** | Recorded real stream events | Integration tests against real-world message shapes without consuming API budget |
| **Shadow** | Live stream, parallel write to a separate store | Pipeline changes, comparison against production path |
| **Production** | Live | The real thing |

**Replay is important for a non-obvious reason:** with 600 REST reads per hour, integration testing against the live API is not affordable. Recorded stream sessions provide realistic test data at zero API cost, and the recording should begin in Phase 0 alongside production ingestion.

---

## 11. Metrics on the Process Itself

Tracked monthly:

| Metric | Meaning |
|---|---|
| Hypotheses registered / validated / retired | Research throughput and kill rate |
| Median stage at which a signal dies | Cheap early kills are the goal; late kills mean early gates are too permissive |
| Validator block rate and subsequent accuracy | Is validation actually filtering? |
| Data quality alerts: count, false-positive rate | Alert fatigue risk |
| Backtest-to-live performance decay | The headline honesty metric |
| Ideas emitted vs. ideas acted on vs. ideas profitable | End-to-end platform value |
| Prior calibration error | Are we systematically overconfident? |

### 11.1 The one that matters most

**Backtest-to-live performance decay.** Everything in this document exists to make this number small. If validated signals routinely underperform their backtests in live paper trading, the validation process is not working, regardless of how thorough it looks on paper. That number is the process's own report card, and it should be reviewed every month with the same skepticism the process applies to everything else.
