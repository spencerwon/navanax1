# Methodology Document
## Analytical Methods for Illiquid NFT Market Edge Discovery

**Version:** 1.0
**Date:** 2026-09-09
**Companion to:** `00_REQUIREMENTS.md`

---

## 1. Purpose

The requirements document says *what* the platform must do. This document says *how the analysis works* — what we measure, why those measurements are the right ones for this market's structure, which statistical techniques apply at which stage, and what standard of evidence a signal must meet before capital is committed.

It is written to be adversarial toward its own conclusions. In a market this noisy, the default outcome of any research process is a false positive, and a methodology that does not actively fight that will manufacture confident nonsense.

---

## 2. Market Microstructure: Why This Market Is Different

Every method here follows from four structural facts. Applying equity-market technique without accounting for these is the single most common way NFT quantitative work fails.

### 2.1 There is no continuous two-sided market

An NFT collection does not have a price. It has:

- an **ask side**: a discrete, sparse set of listings, each for a *specific, non-fungible token*, most of them stale;
- a **bid side**: collection-wide offers (bid on any token), trait offers (bid on any token with trait T), and token-specific offers;
- a **last trade**: possibly days or weeks old, on a token that may not be comparable to the one you care about.

The "floor price" is the minimum ask. It is **not a market price** — it is one seller's opinion, possibly set months ago, possibly on the worst token in the collection. Treating floor as a price series and running standard time-series methods on it is the foundational error of NFT analytics.

**Methodological consequence:** we model the *listing distribution*, not the floor. Floor is retained as one order statistic among many (§3.2).

### 2.2 The assets within a collection are not fungible

Two tokens in one collection can differ in fair value by 100× because of traits. A "collection price" is an aggregate over a heterogeneous population whose composition changes as tokens are listed and delisted.

This creates a **composition bias** that mimics price movement: if the three cheapest tokens sell, the floor rises even though nothing about the collection's value changed. A naive floor-momentum signal will fire on this constantly.

**Methodological consequence:** all collection-level price measures must be quality-adjusted. We use hedonic regression (§5.2) to separate "the collection re-rated" from "the composition of what is listed changed."

### 2.3 Liquidity is the binding constraint, not price

> **STATUS: PROVISIONAL, formalization deferred (Spencer, 2026-09-09).**
> A working program and a valid database come first; the economics come after.
> Nothing in Phase 0 or Phase 1 depends on this section being settled. It is
> recorded now so the eventual work starts from the right definition rather than
> from a volume proxy, and `immediacy_cost` (§3.2) is tracked and graphed for
> every collection from day one because it is directly observable and needs no
> model. The survival machinery in §5.5 is Phase 3 work.

**Canonical definition, adopted from the Operator:** *liquidity is the time it takes the market to clear.* Every market clears eventually; the question is how long. In S&P 500 equities, depth of capital makes clearing effectively instantaneous, and that near-instant clearing **is** what liquidity means. NFT collections sit at the far opposite end: clearing can take weeks or months.

This is a better definition than the volume-based proxies usually used in NFT analytics, and the platform adopts it as primary. It has one refinement that the non-fungible case forces:

**Time-to-clear is not a scalar. It is a function of price.** You can always clear an NFT instantly by pricing it at the standing collection offer, and you can always fail to clear it forever by pricing it at ten times the floor. So the honest measure of a collection's liquidity is a **curve**:

```
time_to_clear( price relative to fair value , quantity )
```

**Quantity is not optional.** A standing collection offer clears one token at its stated size; the second token you want to sell faces a different, worse curve, and the tenth may face no bid at all. Position sizing (§9.2) and the liquidity ceiling in REQ-R-01 are statements about this surface, and a one-argument curve cannot bound a position.

Two points on that curve have names you already use:

- At the **best collection offer**, time-to-clear ≈ 0. That is the bid — someone is standing there ready.
- At the **floor**, time-to-clear is the *expected remaining* time for a listing at that price to sell. Note this is **not** the elapsed age of the cheapest listing — an unsold listing's sitting time is a right-censored lower bound, and the surviving listings are exactly the slow ones (the inspection paradox), so using elapsed age would bias the estimate. The quantity is expected residual duration conditional on current age, which is what the survival model in §5.5 produces and a raw average would not.

Which means the **bid-ask spread is the price of immediacy** — literally what you pay to compress time-to-clear to zero. Spread and time-to-clear are two projections of the same surface, not two separate metrics, and a 30% spread is a market telling you that clearing quickly costs 30%.

**Two boundary cases must be handled explicitly, because they are common in the target universe.** Where no collection offer exists, `bid_ask_spread` is undefined and the curve has no left anchor — the system reports both as unavailable rather than substituting the floor or a stale offer, and a collection with no standing bid is flagged as such, since "no bid at any price" is itself the most important liquidity fact about it. And "every market clears eventually" is a modelling convenience, not an economic law: a dead collection may not clear at any positive price, so the curve is reported over a bounded horizon with an explicit *may not clear* mass rather than extrapolated to a finite time.

**Methodological consequences.**

1. Every valuation is paired with a point on this curve, not a scalar liquidity score.
2. Every expected return is computed **per unit of holding time**, because a return that takes six months to realize is not comparable to one that takes six days — and neither is comparable to simply holding ETH over the same window.
3. The curve is estimated by survival analysis (§5.5), which is the correct statistical tool because most listings are censored — they have not sold *yet* — and discarding them biases every estimate toward the fast sales.
4. Position size is bounded by where the curve becomes unacceptable (§9.2). This is almost always the binding constraint, and it is the most common reason an NFT strategy that backtests well loses money in practice.

Strict liquidity tiers are deliberately deferred until the curve has been measured on real collections. Defining tiers before measuring them would be an assumption dressed as a definition.

### 2.4 The data is adversarial

Wash trading, self-offers to fake demand, listing spam to manipulate floor, and coordinated hype are all present and all cheap to execute. Some of it is designed specifically to trigger the kind of automated screens this platform will run.

**Methodological consequence:** wash detection (§4) is not a data-cleaning step, it is a core analytical component, and its output is itself a signal.

---

## 3. Metric Definitions

Definitions are normative. Every metric below is the single canonical definition used across the platform; ambiguity here propagates into every downstream result.

### 3.1 Price and value

| Metric | Definition | Notes |
|---|---|---|
| `floor_price` | Minimum active listing price, excluding listings flagged as spam or non-transferable | Reported with the count of listings within 5% of it — a floor backed by one listing is not the same object as a floor backed by twenty |
| `floor_depth_k` | Total cost to buy the `k` cheapest listed tokens | The real cost of acquiring size. `floor_depth_5 / (5 × floor)` is a direct spread measure |
| `listing_price_quantiles` | 10th/25th/50th/75th/90th percentile of active listings | The shape of the ask side |
| `best_collection_offer` | Highest active collection-wide bid | The realistic immediate-exit price |
| `bid_ask_spread` | `(floor − best_collection_offer) / floor` | **The primary cost hurdle.** Commonly 15–40% in this market; the round-trip hurdle in §8.1 is this plus fees, royalty and gas |
| `trait_floor(t)` | Minimum active listing among tokens with trait value `t` | Sparse; requires shrinkage (§5.3) |
| `fair_value(token)` | Model estimate from hedonic regression, with credible interval | §5.2 |
| `mispricing(token)` | `(fair_value − list_price) / fair_value`, with propagated uncertainty | **The core signal.** Never reported without its interval |

**All prices are stored in both ETH and USD with the primary denomination recorded.** Analysis defaults to ETH for within-market relative value and USD for absolute P&L, because those answer different questions.

### 3.2 Activity and liquidity

| Metric | Definition |
|---|---|
| `sales_count_w` | Wash-filtered sale count over window `w` |
| `volume_w` | Wash-filtered volume over window `w` |
| `wash_ratio_w` | `1 − (filtered_volume / raw_volume)`. A signal in its own right |
| `unique_buyers_w`, `unique_sellers_w` | Distinct wallets, post-clustering (§4.3) |
| `circulating_supply` | Total minted − burned − provably-locked. The canonical denominator for every ratio below; never the raw `total_supply` field |
| `turnover_w` | `sales_count_w / circulating_supply` |
| `listed_ratio` | `active_listings / circulating_supply` |
| `time_to_clear(p, q)` | **The canonical liquidity measure** (§2.3). Competing-risks estimate of time to clear quantity `q` at price `p` relative to fair value, reported as restricted mean survival time over a stated horizon with confidence bands and an explicit probability of not clearing. A surface, not a number |
| `time_to_clear_at_floor` | The surface at `(floor, 1)`. The headline "how illiquid is this?" figure. Reported as unavailable — never as a number — when the curve does not clear within the stated horizon |
| `immediacy_cost` | `bid_ask_spread` — what you pay to force time-to-clear to zero by hitting the standing collection offer. **Tracked and charted for every collection from Phase 1** (REQ-F-13a): it is directly observable, needs no model, and is the single most decision-relevant number in a thin market. **Undefined when no collection offer exists**, and reported as such rather than substituted |
| `time_to_sale` | Realized days from listing to sale, for listings that sold. **Right-censored** — never average this over sold listings only (§5.5) |
| `liquidity_score` | Scalar summary of the curve for ranking and screening. **A convenience, not the definition** — always drill to the curve before sizing a position |
| `max_position_size` | Largest position exitable within the configured time horizon at acceptable slippage |

### 3.3 Holder and flow

| Metric | Definition |
|---|---|
| `holder_count` | Distinct beneficial owners after wallet clustering |
| `gini_concentration` | Gini coefficient of token holdings |
| `top10_share` | Share held by the ten largest holders |
| `mean_holding_period` | Average time current holders have held |
| `diamond_hand_ratio` | Share of supply held > 180 days |
| `smart_money_flow_w` | Net accumulation by wallets ranked in the top decile of historical realized PnL |
| `new_holder_rate_w` | Rate of first-time holders entering |
| `listing_pressure` | New listings minus delistings, normalized by supply |

### 3.4 Derived and relative

| Metric | Definition |
|---|---|
| `qa_index` | The quality-adjusted price index — `α_c(t)` from the hedonic model (§5.2). **This is the canonical collection price series** and is what every method here means by "quality-adjusted". Raw `floor_price` is an order statistic, not a price |
| `floor_return_w` | Log return of `qa_index` over `w` |
| `realized_vol_w` | Volatility of quality-adjusted returns |
| `relative_strength` | Return versus the collection's peer group (§5.6) |
| `eth_beta` | Sensitivity of collection value to ETH — **critical**, since an ETH-denominated gain in a falling ETH market may be a USD loss |
| `regime_state` | Discrete state from the state-space model (§6.2): dormant / accumulating / active / distributing |

---

## 4. Wash Trading and Data Integrity

### 4.1 Why this comes before the analysis, not after

Wash trading in NFT markets has at times accounted for the majority of reported volume in specific collections. Any volume-ranked screen run on raw data is, in effect, a wash-trade detector with the sign flipped — it will surface exactly the collections you should avoid. This section is therefore load-bearing.

### 4.2 Detection heuristics

Each sale receives a `wash_score` from an ensemble. The field is deliberately **not** named `wash_probability`, because it is not one (§4.4):

1. **Round-trip detection** — token returns to a previous owner within a short window.
2. **Wallet-pair repetition** — the same buyer/seller pair transacts repeatedly.
3. **Funding-graph proximity** — buyer and seller share a common funding source within N hops.
4. **Price implausibility** — sale price far from the trait-adjusted fair value with no corroborating book movement.
5. **Timing regularity** — machine-like inter-arrival times.
6. **Economic irrationality** — the trade loses money on fees and gas for both parties, with no other explanation.
7. **Royalty avoidance patterns** — routing consistent with fee minimization rather than genuine transfer of ownership.

### 4.3 Wallet clustering

Wallets are clustered into probable beneficial owners using shared funding sources, temporal co-movement, and gas-payer patterns. **All holder counts and unique-buyer/seller metrics are computed on clusters, not raw addresses.** A collection with 500 "holders" that clusters to 40 entities is a different asset than one with 500 genuine holders, and only the clustered view distinguishes them.

### 4.4 Calibration and honesty about it

These heuristics are unsupervised and there is no ground-truth labelled dataset. Therefore:

- `wash_score` is treated as **ordinal, not calibrated**. It ranks; it does not give a true probability. We do not report it as a percentage.
- A manually-labelled validation set is maintained (target: 200+ hand-reviewed sales) and detector performance is reported on it, with the acknowledgment that the labels themselves are judgment calls.
- All analytics are dual-mode (raw / filtered) per REQ-D-23 so the effect of the filter on any conclusion is always visible.
- **Sensitivity requirement:** if a signal's profitability flips sign between raw and filtered data, the signal is rejected. It is measuring the filter, not the market.

---

## 5. Tier 1 and 2 Statistical Methods

### 5.1 Tier 1 — Descriptive foundations

Standard but with market-specific care:

- **Returns** computed on `qa_index` (§5.2), not raw floor, to avoid composition bias.
- **Volatility** — realized volatility on irregularly-spaced observations, computed on `qa_index`. Close-to-close estimators are unusable on sparse irregular data. Range-based estimators (Parkinson and similar) are **not** valid substitutes here either: they assume a driftless continuous process observed over a complete high/low range, and a range built from a handful of sparse sales and stale asks violates that in a way that biases the estimate in an unknown direction. We therefore use a time-scaled estimator over the `qa_index` posterior (§6.2), which propagates the sparsity into the uncertainty rather than hiding it, and we always report the observation count and the interval. Where the count is below the §8.2 minimum, volatility is reported as unavailable rather than estimated.
- **Correlation** — pairwise correlations require a common time grid. Because collections trade at different frequencies, correlations are computed on a coarse grid (daily or weekly) and **the effective sample size is reported alongside**. Two collections with 8 overlapping observations have no meaningful correlation, however impressive the coefficient.
- **Percentile ranks** — cross-sectional ranking within the universe and within peer group, which is more robust than levels in a market with no stable scale.
- **Drawdown** — peak-to-trough decline of `qa_index`, with the underwater curve and time-to-recovery. Reported in both ETH and USD, because these routinely disagree in sign.
- **Time-series decomposition** — trend / periodic / residual separation via STL on the `qa_index` series, used to test whether apparent momentum is a weekly listing-and-selling rhythm rather than a re-rating. Requires a regular grid, so it runs on the interpolated snapshot series and its output is flagged accordingly; it is diagnostic only and may not feed a signal directly.

### 5.2 Hedonic trait pricing — the core valuation model

The central model. It decomposes an observed price into contributions from the collection level and from each trait.

For a sale or listing `i` of token `j` in collection `c` at time `t`:

```
log(price_i) = α_c(t) + Σ_k β_ck · trait_jk + γ · controls_i + ε_i
```

Where `α_c(t)` is the time-varying collection level (this is `qa_index`, the **quality-adjusted price index** — the thing that should be used everywhere "floor" is naively used), `β_ck` are trait premia, and controls include sale venue, currency, and market conditions.

**Why this matters more than any other method here:** it separates the two things a naive floor conflates — "the collection re-rated" versus "different tokens are being listed." It produces `fair_value` per token, and the residual `ε_i` is the mispricing estimate that drives the primary signal class.

**Estimation notes:**

- Fit in log-price space; NFT prices are strongly right-skewed.
- Use **robust regression** (Huber) or **quantile regression** at the median. OLS is unusable here — a single outlier sale in a 30-sale collection will dominate the fit.
- Traits are high-dimensional and sparse. Regularize (ridge/elastic net) and pool rare trait values into an "other" bucket by frequency threshold.
- **Diagnostics are mandatory:** residual normality, heteroskedasticity, influence measures (leverage, Cook's distance). A model driven by three influential points must be flagged as such in its output.
- Report `R²`, but treat it with suspicion — high `R²` on 25 observations with 40 trait dummies is memorization.

### 5.3 Hierarchical shrinkage for sparse collections

The problem this solves: a collection with 12 sales cannot support its own trait pricing model, but it is not independent of the market either.

**Partial pooling.** Trait premia for a thin collection are shrunk toward the mean premia of its peer group, with the shrinkage weight determined by that collection's sample size:

```
β_ck ~ Normal(μ_peer_group_k, τ_k²)
```

A collection with 200 sales is estimated mostly from its own data. A collection with 8 sales is estimated mostly from its peers. This is the statistically correct answer to sparse data, and it is why hierarchical modelling (Tier 3, §6.1) is not an optional luxury for this market but the natural home of the core model. The Tier 2 implementation is empirical-Bayes shrinkage; Tier 3 is the full Bayesian version.

### 5.4 Changepoint and regime detection

Thin collections do not drift; they jump between states. Continuous-time models fit to them badly.

- **Changepoint detection** (PELT or Bayesian online changepoint detection) on the quality-adjusted index, sale-rate series, and unique-buyer series.
- Independent changepoints in *activity* preceding changepoints in *price* are a candidate lead indicator — a specific, testable hypothesis and a good first entry in the signal library.
- Multiple-testing caution: scanning 200 collections × 4 series for changepoints generates false positives by construction. Apply the correction discipline in §8.3.

### 5.5 Survival analysis of time-to-sale

**The most underused method for this market, and one of the most valuable.** The question "if I list at price P, when does it sell?" is a survival problem, not a regression problem, because most listings are censored — they haven't sold yet, and dropping them biases everything.

- **This is a competing-risks problem, not plain right-censoring, and treating it as the latter is the main way this analysis goes wrong.** A listing ends by sale, cancellation, expiry, or reprice. Kaplan–Meier assumes censoring is independent of the event process, but a seller cancels or reprices *precisely because* the item is not selling — the censoring is informative, and naive KM will overstate clearing speed, possibly by a lot. Model sale and withdrawal as competing risks (cumulative incidence / Fine–Gray) rather than censoring withdrawals away.
- **Repricing changes the stratum mid-life.** A listing that starts at +40% over fair value and is cut to −5% is not one observation in one price bucket. Price relative to fair value enters as a **time-varying covariate**, not a fixed stratum assignment.
- Cox proportional-hazards with covariates: price relative to fair value (time-varying), trait rarity, collection activity level, market regime. Check the proportional-hazards assumption via Schoenfeld residuals; it will frequently fail across regimes, which is informative rather than fatal.
- **The median is often undefined, and the headline metric must survive that.** In a thin collection the survival curve may never reach 0.5 within the observation window, so "median days to sale" does not exist. Report **restricted mean survival time** over a stated horizon plus the estimated probability of not clearing within it. A `time_to_clear_at_floor` that silently returns a number when the curve never crosses 0.5 would be exactly the kind of confident-but-meaningless output the platform exists to prevent.
- **Direct outputs:** `time_to_clear(p)` — the price-versus-speed curve that §2.3 defines as liquidity itself — plus the expected holding period for exit modelling (REQ-F-31) and a principled bound for position sizing.

Right-censoring must be handled explicitly. A naive average of "days to sale" over sold listings only is one of the most misleading numbers it is possible to compute here.

### 5.6 Peer group construction

Many methods above require a peer group. Peers are constructed by clustering on structural features — supply, price tier, trait-structure similarity, holder-overlap, category, mint age — **not on price correlation**, which would induce circularity when the peer group is then used to compute relative strength.

### 5.7 Cointegration and relative value

For pairs of collections with sufficient history and economic linkage (shared holder base, same category, same creator), test for cointegration and model the spread. Genuine mean-reverting spreads are a candidate strategy class.

**Caveats that must be stated in any output:** cointegration tests over short samples have poor power; spurious relationships are the norm at this sample size; and a mean-reversion trade in an illiquid market requires the ability to exit both legs, which frequently does not exist. Treat this as a research direction with a high prior probability of failure.

---

## 6. Tier 3 — Advanced Methods

### 6.1 Full Bayesian hierarchical valuation

The Tier 2 hedonic model with proper uncertainty propagation. Multi-level: market → category → peer group → collection → trait.

**Why it earns its complexity here:** sparse data is the defining problem of this market, and Bayesian hierarchical models are the principled solution to sparse data. More importantly, they produce **credible intervals on `fair_value`**, which is what makes the mispricing signal usable — "this token is 30% underpriced ± 40%" is correctly a non-signal, and only this class of model tells you that. A point estimate would have you trade it.

Implementation: PyMC or NumPyro. Convergence diagnostics (R̂, ESS, divergences) are mandatory and a non-converged model's output must be rejected, not caveated. Prior predictive and posterior predictive checks required.

### 6.2 State-space models for latent value and regime

Model true collection value as an unobserved state, with floor, sales, and offers as noisy, biased observations of it:

```
value_t = value_{t−1} + ω_t                       (state evolution)
floor_t = value_t + composition_bias_t + η_t      (biased observation)
sale_t  = value_t + trait_adjustment + ν_t        (noisy observation)
```

This is the natural formulation for a market where every observable is a corrupted view of the thing you care about. Estimated via Kalman filter (linear) or particle filter (non-linear). A **regime-switching** variant (Markov-switching state space) produces the `regime_state` metric.

### 6.3 Network analysis of wallet flow

Construct the transfer graph and analyze:

- Centrality measures identifying influential wallets.
- Community detection revealing coordinated groups.
- **Lead-lag analysis:** do certain wallet clusters accumulate before price appreciation? This is the direct operationalization of the "flow edge" thesis (Requirements §1.2, item 3).
- Contagion patterns — activity spreading between related collections.

Methodological caution: identifying "smart money" retrospectively is trivially easy and almost entirely survivorship bias. Wallets are selected on past performance, then evaluated on the same data that selected them. **The only valid test is out-of-sample**: identify the smart-money set on data up to time T, and evaluate its predictive power strictly after T. Any other framing is circular.

### 6.4 Machine learning — gated

ML is permitted only under these conditions, because the failure mode is severe and the temptation is high:

1. A specified, interpretable baseline (the hedonic model) must exist and be beaten by a meaningful margin.
2. Sample size must support it. With 50 sales, ML is memorization. **Minimum 500 observations per model, and a features-to-observations ratio no greater than 1:20.**
3. Feature importance and partial-dependence must be inspected and be economically sensible. A model whose top feature is `token_id` has found an artifact.
4. Purged, embargoed cross-validation only — standard k-fold leaks across time and will produce spectacular, entirely fictional results.
5. The model must survive the full validation protocol in `03_VALIDATION_AND_TESTING.md`, unchanged.

Preferred: gradient boosting with monotonicity constraints where economically justified. Deep learning is not justified at this data scale and is out of scope.

---

## 7. Signal Development Protocol

### 7.1 The protocol is hypothesis-first, and this is non-negotiable

Every signal begins as a **written economic hypothesis**, registered before the data is examined for it. The registration states:

1. **Mechanism** — *why* would this be true? What structural feature of the market creates the inefficiency? Who is on the other side of this trade, and why are they making a mistake?
2. **Prediction** — a specific, falsifiable, quantitative claim.
3. **Test design** — universe, period, measurement, and the decision rule, all fixed in advance.
4. **Kill criteria** — the result that would cause abandonment, stated before seeing results.
5. **Prior** — an honest estimate of the probability this works, recorded so that calibration can be assessed later.

**A signal discovered by scanning data for patterns, without a mechanism, is rejected regardless of statistical significance.** There is no exception to this. With a few hundred collections and dozens of metrics, patterns that clear any significance threshold can be found on pure noise, and they will not persist.

The "who is on the other side" question in (1) deserves emphasis. If a signal is real, someone is systematically losing money to it. A plausible answer exists for genuine inefficiencies — sellers who set a listing and stopped watching, buyers anchored to the floor who don't price traits, participants who cannot process on-chain flow. If no such answer exists, the signal is probably an artifact.

### 7.2 Signal taxonomy

| Class | Mechanism | Example hypothesis |
|---|---|---|
| **Relative value** | Trait mispricing versus hedonic fair value | Tokens listed >25% below fair value with tight credible intervals outperform |
| **Stale listing** | Listings not repriced after the collection re-rated | Listings older than 30 days below current fair value are systematically underpriced |
| **Flow** | Informed accumulation precedes price | Net accumulation by top-decile-PnL clusters leads floor by 5–15 days |
| **Regime transition** | Dormant→active transitions are persistent and detectable | Sale-rate changepoints lead price changepoints |
| **Liquidity provision** | Compensation for bearing illiquidity | Buying at collection-offer level and reselling at floor earns the spread net of costs and time |
| **Structural** | Supply/demand imbalances | Sharp declines in listed ratio without price movement precede appreciation |
| **Cross-collection** | Related collections mean-revert | Cointegrated pairs revert within N days |

### 7.3 Lifecycle

```
HYPOTHESIS → registered, unexamined
     ↓  prior plausible, mechanism articulated
EXPLORATORY → tested on training partition ONLY
     ↓  effect present, correct sign, economically meaningful
SPECIFIED → parameters frozen, code frozen, documented
     ↓  passes full backtest with realistic costs
VALIDATED → survives out-of-sample + walk-forward
     ↓  30+ days paper trading meeting criteria
LIVE → generating actionable ideas, outcomes tracked
     ↓  degradation detected
RETIRED → archived with post-mortem
```

Movement between stages is one-directional. A signal that fails at any stage returns to `RETIRED` with a documented post-mortem, **not** to `EXPLORATORY` for another attempt with tweaked parameters. Repeated re-testing of a variant is the mechanism by which overfitting happens, and this rule is the primary defence against it.

### 7.4 Data partitioning — set once, permanently

| Partition | Use | Rule |
|---|---|---|
| **Training** (oldest ~50%) | Exploration, model fitting, parameter selection | Unlimited use |
| **Validation** (~25%) | Confirming a specified signal | Limited use, each use logged and counted |
| **Test** (most recent ~25%) | Final out-of-sample evaluation | **Used once per signal. Ever.** |
| **Live-forward** | Real time after specification | The only fully honest evaluation |

**Walk-forward analysis runs over the training and validation partitions only.** Its rolling origin never crosses into the test partition, so walk-forward and the single test-partition evaluation are separate, non-conflicting steps.

The test partition is a consumable resource. Every look at it costs statistical validity that cannot be recovered. The number of times each partition has been accessed, per signal, is logged and reported.

---

## 8. Standards of Evidence

### 8.1 Economic significance before statistical significance

A signal must clear the full round-trip cost:

```
required_edge > marketplace_fee + royalty + gas_in + gas_out + spread_cost + slippage + opportunity_cost
```

In practice the all-in round-trip hurdle is commonly **20–40%** for an illiquid collection, of which the spread (§3.1) is the largest component. A statistically bulletproof 5% effect is worthless here, and this test is applied *first* — before any statistical testing — because it disqualifies most candidates immediately and cheaply.

### 8.2 Minimum sample requirements

| Analysis | Minimum |
|---|---|
| Collection-level hedonic fit | 30 sales, else mandatory shrinkage to peers |
| Trait premium estimate | 10 observations of that trait value |
| Signal validation | 50 independent signal events across ≥10 distinct collections |
| Correlation estimate | 30 overlapping observations |
| ML model | 500 observations, ≤1 feature per 20 observations |

"Independent" is doing real work in the signal-validation row. Fifty firings within one collection during one month are not fifty independent observations — they are approximately one. Clustered standard errors and effective-sample-size reporting are required.

### 8.3 Multiple testing

Every hypothesis tested is logged in a permanent register. Reported significance is corrected for the total number tested (Benjamini–Hochberg FDR at q=0.10 for exploration; Bonferroni for confirmatory claims). The register is append-only; hypotheses cannot be quietly removed after failing.

**Deflated Sharpe ratio** is reported for any backtested strategy, accounting for the number of configurations tried. An undeflated Sharpe from a search over 40 parameter settings is not a real number.

### 8.4 Robustness requirements

A signal must be stable across:

- Parameter perturbation (±20% on every threshold — a signal that only works at exactly 25% is fitted to noise)
- Sub-periods
- Sub-universes (collection categories, price tiers)
- Wash-filter on/off (sign must not flip — §4.4)
- Reasonable cost-assumption variation

### 8.5 The negative-result standard

**A signal that does not work is a valid and valuable finding, and is documented with the same rigor as one that does.** The register of dead hypotheses is one of the most useful artifacts this project will produce: it prevents re-testing the same idea, it calibrates priors for future hypotheses, and it is the honest record of what was tried. A research process that produces no negative results is not doing research.

---

## 9. Valuation and Position Sizing

### 9.1 Fair value with uncertainty

Fair value is always a distribution, never a number. Every valuation output carries a credible interval, and the interval width determines whether the signal is actionable at all:

```
actionable ⟺ (fair_value_lower_bound − list_price − total_costs) > 0
```

Note this uses the **lower bound**, not the point estimate. A 30% mispricing with a ±40% interval is not a trade.

### 9.2 Sizing

Position size is the minimum of three ceilings:

1. **Liquidity ceiling** — the largest position exitable within the horizon at acceptable slippage, from the survival model (§5.5) and depth (§3.1).
2. **Conviction ceiling** — scaled by the signal's validated hit rate and the width of the fair-value interval. Fractional Kelly (≤¼ Kelly) with the estimate itself shrunk for parameter uncertainty.
3. **Concentration ceiling** — portfolio-level limits per collection and per category.

**The liquidity ceiling binds almost always.** This is the defining feature of the market and the most common way NFT strategies that backtest well lose money in practice.

### 9.3 Expected value

```
EV = P(favorable) × E[gain | favorable] − P(unfavorable) × E[loss | unfavorable] − costs
EV_per_unit_time = EV / expected_holding_period
```

The time normalization is what makes an illiquid opportunity comparable to a liquid one, and to simply holding ETH.

---

## 10. Known Failure Modes

Recorded explicitly so they can be checked against, and so that a future reader understands what the validation process is defending against.

| Failure | How it happens here | Defence |
|---|---|---|
| **Look-ahead bias** | Using a metric revised after the fact; using a collection's later prominence to select it | Bitemporal store, structural enforcement (REQ-F-28) |
| **Survivorship bias** | Dead collections leave the API; screening today's universe over history | Historical universe reconstruction from own records |
| **Composition bias** | Floor moves because listing mix changed | Quality-adjusted index (§5.2) |
| **Wash contamination** | Volume/momentum signals fire on fake activity | Dual-mode analytics, sign-flip rejection (§4.4) |
| **Overfitting** | Many parameters, few observations, repeated tweaking | One-directional lifecycle, partition discipline, deflated Sharpe |
| **Multiple testing** | Scanning hundreds of collections × dozens of metrics | Permanent hypothesis register, FDR correction |
| **Cost underestimation** | Backtest at floor, ignore spread and gas | Full cost model, economic-significance-first |
| **Illiquidity denial** | Backtest assumes instant exit at mark | Survival-based exit modelling (REQ-F-31) |
| **Regime dependence** | Signal discovered in a bull market | Sub-period robustness, regime-conditional reporting |
| **Selection on the dependent variable** | "Smart money" identified from the same data used to test it | Strict temporal separation (§6.3) |
| **Denominator blindness** | ETH gains during an ETH crash | Mandatory dual-denomination reporting |
| **Small-sample confidence** | 7 sales, tight-looking estimate | Mandatory uncertainty reporting (REQ-F-19), minimum-N gates |

---

## 11. Research Cadence

| Activity | Cadence | Output |
|---|---|---|
| Data quality review | Daily, automated | Quality dashboard, alerts on breach |
| Signal performance review | Weekly | Live signal hit rates versus expectation |
| Hypothesis generation | Weekly (WF-D-05, Wednesday) | New registered hypotheses with mechanisms |
| Model revalidation | Monthly | Refit, drift check, recalibration |
| Strategy review | Quarterly | Promote / retain / retire decisions |
| Methodology review | Semi-annually | This document, updated against what was learned |

---

## 12. Interpretation Discipline

Rules governing how results are communicated, including by the platform to the Operator:

1. Never report a point estimate without its uncertainty.
2. Never report a percentage without the underlying count.
3. Always report the effective sample size, not the nominal one.
4. Always state the assumptions a result depends on and whether diagnostics flagged violations.
5. Correlation claims must be accompanied by an explicit mechanism or labelled as unexplained.
6. Backtest results must be presented net of costs by default; gross figures are supplementary.
7. When a result is ambiguous, say so. "The data does not distinguish these hypotheses" is a legitimate and frequently correct conclusion.
8. Absence of evidence is reported as absence of evidence, not as evidence of absence.

These apply to every agent in `02_AGENT_HIERARCHY.md` and are enforced in output schemas rather than left to good intentions.
