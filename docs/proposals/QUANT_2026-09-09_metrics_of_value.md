# QUANT — Metrics of value, and the data we need to compute them

*Proposal. 2026-09-09, quant-research. Companion to `DESIGN_2026-09-09_views.md` (design-lead, today) and `FACTS_2026-09-09_stream_payloads.md`.*
*Status: **PROPOSED**. Nothing here is a signal. Nothing here has been tested. Every definition below is a **measurement**; the hypotheses they would feed are named but not registered, because registration precedes examination (docs/01 §7.1) and I have not examined data for any of them.*

---

## 0. The one fact that reorganises everything

In 40 minutes: **42,601 item bids, 41,361 cancels, 8 listings, 3 sales.** 88% of events from 3 makers, median bid life 9 s.

The naive reading is "this collection is busy." The correct reading is: **the bid side is a machine-quoted curve observed almost continuously, and the ask and trade sides are almost unobserved.** Those are two different data-generating processes and they need two different treatments:

- The bot layer is **not** 42,601 observations. It is a small number of *price levels* observed nearly continuously. Its information content is in **level changes**, and its effective sample size is the number of independent quoting episodes — plausibly ~10², possibly ~10⁰ if the three makers are one entity (unclustered — docs/01 §4.3).
- The listing/sale layer is 11 observations. Everything valuation-related is gated on it, and it accrues at roughly **288 listings/day and 108 sales/day** if the 40-minute rate holds — which is itself an assumption, since we have one window at one hour of one day and diurnality is unmeasured.

Second structural fact, and it is not in any document yet:

> **We only see orders whose placement we witnessed.** Recording started 10:20 UTC. Argonauts had ~801 standing listings before that. Every stream-only "standing book" is therefore **left-truncated**, and the bias has a known sign: the reconstructed floor is an **over**estimate of the true floor (cheaper older listings are invisible) and the reconstructed best bid is an **under**estimate. **Stream-only spread is biased wide, and any "no bid at any price" conclusion from it is unsafe.** This is why §5's first recurring purchase is a listings snapshot: it is the cheapest possible fix, and 9 reads buys it.

Everything below is written against those three facts.

---

## 1. Ranked metrics

Ranking criterion: *how much mispricing-finding power per unit of data we already have or can cheaply get.* **S** = structure (observed; DERIVATION layer, no assumptions). **J** = judgement (modelled; ANALYSIS layer, assumptions declared) — docs/06 §4.

Rates used for "minimum data" throughout, extrapolated from the 40-minute window (**an assumption — one window, one hour of one day**): bids 63.9k/h · cancels 62.0k/h · invalidates 2.3k/h · collection offers 252/h · trait offers 59/h · listings 12/h · sales 4.5/h.

### 1.0 The primitive everything depends on: `order_lives`

Not a metric — the derived relation the next twelve are defined on. Build once, in DERIVATION.

```sql
-- placements
P  = event_type IN ('item_listed','item_received_bid','item_received_offer',
                    'collection_offer','trait_offer')
-- terminations
X  = event_type IN ('item_cancelled','order_invalidate','item_sold')

t_place(h)   = MIN(valid_ts)  over P rows with order_hash = h
t_term(h)    = MIN(valid_ts >= t_place) over X rows for h,
               EXCLUDING an 'order_invalidate' that has a later 'order_revalidate' for h
exit_event(h)= event_type of that row      -- cancelled | invalidated | filled
t_expire(h)  = expiration_ts from the placement row
standing(h, τ) ⟺ t_place ≤ τ ∧ (t_term IS NULL ∨ t_term > τ) ∧ (t_expire IS NULL ∨ t_expire > τ)
```

Rules that are not optional:
- **Expiry is inferred, not observed.** If `t_expire < now` and no X row exists, the order ended by expiry at `t_expire`, written as a *derived* row with `source=derived`, never as a market event.
- **Placement unseen ⇒ excluded, and counted.** A termination whose `order_hash` has no placement in the store is left-truncated. It is dropped from duration analysis and its count is reported (metric 12). Never impute `t_place`.
- **`quantity` is carried through.** A collection offer with `quantity = 5` is five units of depth, not one row.

### The twelve

| # | metric | S/J | view |
|---|---|---|---|
| 1 | `spread_exec` — executable immediacy cost | S | Market (KPI + hero) |
| 2 | `bid_depth` / `executable_depth(p, L)` | S / J | Market + Flow |
| 3 | `ask_staleness` + `bid_drift_since_listing` | S | Traits (screener) |
| 4 | `trait_bid_premium` — revealed demand from trait offers | S | Traits |
| 5 | `trait_ask_qk` — trait ask quantiles at matched depth | S | Traits |
| 6 | `S_bid(t)` — bid persistence, KM with competing risks | J | Flow |
| 7 | `n_eff` — episodes, not events | S (ε=0) / J (ε>0) | everywhere |
| 8 | `trade_location` — where a sale printed in the book | S | Market / Flow |
| 9 | maker concentration on **three** denominators | S | Flow |
| 10 | `listing_survival` → `time_to_clear(p)` | J | Market (later) |
| 11 | `stream_lag` → `actionable_window` | S | Health |
| 12 | `book_completeness` — orphan terminations | S | Health |

---

**1. `spread_exec` — executable immediacy cost.** *The single most decision-relevant number we can compute, and the current `immediacy_cost` is not it (§6, T2).*

At instant τ, per token *j*:
```
ask_j(τ) = min{ price_eth : standing(h,τ) ∧ event_type='item_listed' ∧ token_id=j }
bid_j(τ) = max(  best standing collection_offer at τ,
                 best standing trait_offer at τ whose criteria ⊆ traits(j),
                 best standing item_received_bid at τ on token j )
spread_j(τ)     = ask_j(τ) − bid_j(τ)
spread_pct_j(τ) = spread_j(τ) / ask_j(τ)
```
Collection level: `j* = argmin_j ask_j(τ)`, then `spread_exec(τ) = spread_{j*}(τ)`. **Note the leg discipline: the bid leg is the bid available on the token that is at the floor, not the global best bid over all tokens.** Mixing those populations is how a spread goes negative.

Per bucket, report the **time-weighted median with [p10, p90] across the bucket**, not one number — on a bot book the instantaneous spread changes many times per bucket, and a single extreme is not the spread (§6, T2).

**Must accompany:** both legs' `order_hash`, `maker`, age at τ, `quantity`; count of standing asks within 5% of `ask_{j*}` (docs/01 §3.1 — a floor backed by one listing is a different object); `both_legs_seconds / bucket_seconds` as coverage; `null` when either leg is absent, never substituted.
**Minimum data:** instantaneous — *but* not trustworthy until the ask side is seeded from REST (§5.4). Until then label it **"stream-only: upper bound on the true spread"**.

**2. `bid_depth` and `executable_depth`.** The left anchor of the `time_to_clear` surface (docs/01 §2.3), directly observable — no model needed for the raw form.

```
bid_depth_j(p, τ) = Σ over standing bids b applicable to token j with price_b ≥ p  of  quantity_b
ask_depth(p, τ)   = #{ standing asks with price ≤ p } ;  floor_depth_k = Σ of k cheapest standing asks
```
Applicability: a collection offer applies to every token; a trait offer to tokens whose traits ⊇ its criteria; an item bid to its token only. **A depth curve for a specific holding is what the Operator actually needs** — he owns an Argonaut; `bid_depth_j` for that token is the answer to "what am I bid right now, and for how many."

The judgement layer on top (declare it as such):
```
executable_depth(p, L, τ) = Σ_b quantity_b · Ŝ_{class(b)}( age_b + L | age_b )
```
where `Ŝ(·|age)` is the residual survival from metric 6 and `L` = stream lag p95 + human decision time. **Raw depth and executable depth must be shown together**; the ratio is the honest statement of how much of the visible bid is reachable.
**Must accompany:** the raw depth, the count of orders composing it, the maker count (depth from one maker is one decision, not many), and the survival CI propagated into `executable_depth`.
**Minimum data:** raw depth is instantaneous; `executable_depth` needs metric 6's minimum (below).

**3. `ask_staleness` + `bid_drift_since_listing`.** *The stale-listing thesis (REQ §1.2 item 1) in a form that needs no fair-value model.*

For each standing ask *a* placed at `t_a`:
```
age_a(τ)       = τ − t_a
bid_drift_a(τ) = ln( CB(τ) / CB(t_a) )        CB = best standing collection offer
ask_over_bid_a = ask_a / CB(τ)
```
`bid_drift` is **defined only if the store observed CB continuously over [t_a, τ]** with no ingestion gap. Otherwise null. No forward-fill (docs/06 §4.3).

Mechanism, stated because §7.1 requires it before this becomes a hypothesis: *a seller who listed and stopped watching, while the machine bid ladder moved up around them.* That is a plausible loser. It is **not registered and not tested here.**
**Must accompany:** n standing asks meeting the screen out of n standing asks total; the number of CB observations over `[t_a, τ]`; the ingestion-gap seconds inside that interval.
**Minimum data:** needs continuous CB observation over the listing's life. CB arrives at 252/h so continuity is fine after the first hour; the constraint is that `t_a` must be *observed*, so this only covers listings placed after 10:20 UTC — 288/day. Useful at n ≈ 30 standing observed asks, ~2.5 h of listings, but it does not become the *screen* until the REST seed gives us the other ~801.

**4. `trait_bid_premium` — revealed demand.** *The rarest and most valuable thing in this dataset: a bid side that names a trait.* Requires the store to persist trait-offer criteria (design §3.1 dependency).

For a criteria set `C = {(type,value), …}`:
```
TB(C, τ) = max{ price_eth : standing trait_offer at τ with criteria(h) = C }
trait_bid_premium(C, τ) = TB(C, τ) / CB(τ) − 1
```
Also compute the **superset form** `TB⁺(C) = max over standing trait offers whose criteria ⊆ C`, i.e. every offer a token carrying C would satisfy — that is what a holder of such a token is actually bid.

**Must accompany:** `n_offers`, `n_distinct_makers`, `n_episodes` (metric 7), total quantity, and the fraction of the window in which any offer for C stood. **`n_eff` is distinct (maker, price level, episode) triples, never the offer count.** Ten `Print: Unclaimed` offers from one maker are **n_eff = 1**, and reporting a premium "from n=10" would be a lie by arithmetic.
**Minimum data:** 59 trait offers/h across an unknown number of criteria sets (observed: `Print: Unclaimed` ×10 of 39). For a single C: report the standing offer as a **fact with n=1** immediately; report a *distribution* only at `n_episodes ≥ 5` from `n_makers ≥ 2`. On observed rates that is days for the common criteria and possibly never for rare ones. Say "not enough" rather than estimating (docs/01 §8.2).

**5. `trait_ask_qk` — trait ask quantiles at matched depth.** *The correction to `trait_floor`, and the most important statistical point in this document.*

`trait_floor(C) = min` over a set of standing asks. **A minimum is a downward-biased estimator of location, and the bias grows with the size of the set.** For asks i.i.d. with CDF *F*:
```
E[min of n draws] = ∫₀^∞ (1 − F(x))ⁿ dx      strictly decreasing in n
```
So a trait carried by 2,000 tokens with 180 standing asks will show a **lower floor** than a trait carried by 40 tokens with 3 asks *even if the two value distributions are identical.* Every "rare traits are cheap / expensive" claim built on comparing trait floors across differently-sized groups is measuring `n`, not value.

Fix, in order of honesty:
```
trait_ask_q10(C), trait_ask_median(C)          -- quantiles, comparable across n
trait_ask_kth(C, k=5)                          -- the 5th-cheapest: matched depth
floor_vs_null_min(C) = trait_floor(C) − median{ min of n_asks(C) draws from the
                       baseline standing-ask empirical distribution }, by simulation
```
`floor_vs_null_min` is the cheap null-model annotation: *"this trait's floor is 0.09 Ξ below what 3 random asks from this collection would give."* It is descriptive, not a signal, and using it as one requires registration first.
**Must accompany:** `n_tokens(C)`, `n_standing_asks(C)`, and the rule **no ratio reported when `n_standing_asks < 3`** — below that, print the single ask as a labelled fact.
**Minimum data:** needs the standing ask book (§5.4, 9 reads) and traits (§5.3, ~97 reads). With those, immediate. Without the REST seed, most trait groups have zero observed asks.

**6. `S_bid(t)` — bid persistence.** Full specification in §3. Belongs on Flow. **J.**
It is the input to `executable_depth`, and on its own it answers the feasibility question: *is a bid that stands 9 s reachable by a human at all?*

**7. `n_eff` — episodes, not events.** Full specification in §2. Applies to every count on every view. **The ε = 0 form is pure structure; the ε > 0 tolerance form is judgement.**

**8. `trade_location` — where a sale printed.** *Answers "who is on the other side" for the liquidity-provision signal class, and it is cheap.*

For each `item_sold` at τ, price *P*, token *j*, using the book at τ⁻:
```
loc = (P − bid_j(τ⁻)) / (ask_j(τ⁻) − bid_j(τ⁻))     defined when ask > bid, both present
```
`loc ≈ 0`: the seller hit a standing bid — the bot bought and is being paid the spread. `loc ≈ 1`: a buyer lifted an ask. `loc > 1` or `< 0`: the trade happened outside the book we reconstructed, which is a **book-completeness alarm**, not an arbitrage.
**Must accompany:** n sales classified, n excluded for a missing leg (**report both** — excluding silently would select on the dependent variable), and the maker/taker addresses with whether they are in the dominant-maker set.
**Minimum data:** 4.5 sales/h → **n = 30 in ~7 h, n = 100 in ~22 h.** Report the raw list below n = 30; report the distribution above it.

**9. Maker concentration on three denominators.** The "88% of events" figure is true and nearly meaningless on its own.
```
share_m(w) on EVENTS      -- a statement about quoting machinery
share_m(w) on EPISODES    -- a statement about decisions          (metric 7)
share_m(w) on FILLS       -- a statement about the market
HHI = Σ_m share_m²        -- computed separately on each of the three
new_maker_rate(w) = #{ makers whose first observed event in this collection falls in w }
```
Every share printed with **both** numerator and denominator counts (docs/01 §12.2).
**Minimum data:** event-HHI is immediate. Fill-HHI needs metric 8's sales — ~7 h for n=30. **Until then fill-HHI is reported as unavailable, not as zero.**

**10. `listing_survival` → `time_to_clear(p)`.** The canonical liquidity measure (docs/01 §2.3, §5.5), and the slowest to mature. Same estimator as §3, run on `item_listed`, with one difference that matters:

> For **bids**, cancel/invalidate/fill/expire are all *events*; the only censoring is administrative (window end), which is independent by construction — so Kaplan–Meier is defensible. For **listings**, a seller cancels *because it is not selling*: the withdrawal is informative and KM will overstate clearing speed. Listings therefore need cumulative incidence (Fine–Gray / Aalen–Johansen), not KM, exactly as docs/01 §5.5 says.

And a cohort trap: the stream gives **incident** listings (all fresh — the clean cohort, no length bias). A REST snapshot gives **prevalent** listings (length-biased toward the slow ones). **Do not pool them.** If snapshot listings are used for duration at all, they enter with **delayed entry** at the snapshot time.
**Must accompany:** RMST over a stated horizon plus P(not cleared within horizon); never a median that the curve does not reach.
**Minimum data:** 12 listings/h incident. n = 30 *ended* listings requires listings to end, not merely to be placed — plausibly days. Honest expectation: **RMST over a 24 h horizon is estimable in ~2–3 days; median time-to-sale is likely never estimable from a two-week window and must be reported "not reached".**

**11. `stream_lag` → `actionable_window`.** Feasibility, and it costs nothing — both timestamps are already stored.
```
lag = observed_ts − valid_ts,  reported p50/p95 with n, PER event_type
actionable_window = quantile_q(bid lifetime) − lag_p95 − reaction_time
P(actionable | L) = Ŝ_bid(L),  L = lag_p95 + reaction_time
```
**Honest caveat that must be printed:** `lag` = true latency + (our clock − OpenSea's clock) + their timestamp granularity. It is a **bound contaminated by unmeasured skew**. Negative lags are not impossible — they are the measurement of skew, and they must be shown, not clipped.
**Minimum data:** immediate; n is every event.

**12. `book_completeness` — orphan terminations.** The direct estimator of how wrong metrics 1–3 currently are.
```
orphan_rate(w) = #{ terminations in w with no placement for that order_hash in store }
                 / #{ terminations in w }
standing_never_seen = (REST count of standing listings) − (our reconstructed count)   [REQ-D-10 drift]
censored_at_end(w)  = #{ orders standing at window end }
```
`orphan_rate` decays as recording lengthens, at a rate set by the order lifetime distribution — fast for 9-second bot bids, slow for listings that live for weeks. **Report it per order kind**, because the aggregate is dominated by bids and will look reassuring while the ask book is still 90% invisible.
**Minimum data:** immediate.

### Not yet computable — stated so nobody builds on them

| metric | gate |
|---|---|
| `qa_index` (docs/01 §3.4 — the canonical price series) | ≥30 trait-matched sales + a time-varying α. **Not before §5.2's sales backfill.** Until it exists, no "collection return" number is honest |
| `fair_value` / `mispricing` (**the core signal**, docs/01 §3.1) | the hedonic model, §4 below |
| `wash_ratio`, `wash_score` | needs sales *and* a labelled review set (docs/01 §4.4). **Every number on the page is `wash_filter=raw` and must keep saying so** |
| holder metrics, `smart_money_flow` | on-chain transfers (§5.1) |
| `eth_beta`, USD backfill | ETH/USD reference series, REQ-D-25 |
| `relative_strength`, peer groups, cointegration | ≥2 collections. One collection is not a cross-section |

---

## 2. The bot-churn problem

**Nothing is deleted. Nothing is filtered in DERIVATION.** The landing zone and the events table are append-only (docs/06 §4.3: *store the weird record; flag it, never drop it*). The whole solution is **classification and counting**, in layers above.

### 2.1 Episode collapse — assumption-free version first

Define scope: `token_id` for item bids, the collection for collection offers, the criteria set for trait offers.

**DERIVATION (ε = 0, no assumptions, deterministic):** consecutive placements by the same maker on the same scope at an **identical `price_wei`** with no intervening placement by that maker at a different price form one **episode**.
```
bid_levels(collection, scope_kind, scope_id, maker, price_wei,
           t_first, t_last, n_quotes, n_orders, ended_by)
```
One row per contiguous level. 42,601 bid events collapse to an expected O(10²) rows. **This table, not the events table, is what every price panel should read.**

**ANALYSIS (ε > 0, declared):** a placement is a *refresh* rather than a *revision* if
```
| ln(price / price_prev) | ≤ ε   AND   (t − t_prev) ≤ W
```
Proposed registry entries (docs/06 §4.4 — I own the values, I cannot merge them):

```yaml
- id: ASM-020  name: quote_refresh_log_tol   value: 0.002   # 20 bps
  layer: analysis  owner: quant-research
  rationale: below this, a re-quote is the same intent at a different wei amount.
- id: ASM-021  name: quote_refresh_window_s  value: 60
  layer: analysis  owner: quant-research
  rationale: a gap longer than this is a new decision, not a refresh.
- id: ASM-022  name: maker_class_min_events  value: 200
- id: ASM-023  name: quoter_max_median_life_s value: 60
- id: ASM-024  name: quoter_max_fill_rate    value: 0.01
```

### 2.2 Maker classification — a derived, versioned, append-only table

```sql
maker_class(collection, maker, class, method_version, computed_at, valid_from,
            n_events, n_episodes, n_fills, median_life_s, cancel_rate, evidence_json)
class ∈ {quoter, taker, resting, unknown}
```
- `quoter`: `n_events ≥ ASM-022` ∧ `median_life_s ≤ ASM-023` ∧ `fill_rate ≤ ASM-024`
- `taker`: appears in `events.taker` on `item_sold`
- `resting`: placements few, lifetimes long, `fill_rate` unconstrained
- `unknown`: below evidence threshold — **the default, and it must stay a large bucket**

Rules: this is an ANALYSIS artifact. It **never** becomes a column on `events` and **never** filters a DERIVATION query. It carries `method_version` so a chart drawn last month can be reproduced when the thresholds change (REQ-N-15). It is a *label*, not a fact — two reasonable people can disagree about it, which is exactly the docs/06 §4.1 test.

### 2.3 What to show the Operator

1. **Two panels, never one.** *Quote churn* (events/interval, bids + cancels) is a machinery panel. *Book state* (levels, spread, depth) is a market panel. They share an x-axis and nothing else.
2. **A ladder chart, not a 42,601-point scatter.** One step line per maker from `bid_levels`, plus the standing ask as an orange step over the top. This is the design-lead's "Bids vs floor" (§1.3) built on levels — it needs no point budget, no sampling caveat, and it shows the ladder structure the scatter only implies. Keep the scatter as a drill-down.
3. **Every count carries both numbers, everywhere:**
   `42,601 bid events · 190 price episodes · 3 makers (unclustered) — percentiles below use n_eff = 190`
4. **The maker table gets three share columns**, not one: share of events, share of episodes, share of fills — with numerator and denominator printed (metric 9).
5. **A standing "what this does not mean" line** under the churn panel. Not a footnote; a first-class line of the UI.

### 2.4 What NOT to conclude yet

- **Not** that the three makers are three entities. Wallet clustering (docs/01 §4.3) has not run. It could be one desk, or a relay.
- **Not** that this is wash trading. Wash detection is a sales-based method (docs/01 §4.2) and we have three sales. No `wash_score` exists. Saying "bots" is a description; saying "wash" is an accusation with no evidence behind it.
- **Not** that 88% of events is 88% of anything economic. Share of events ≠ share of liquidity ≠ share of volume. Two of those three are currently unknown.
- **Not** that "median bid lifetime = 9 s" is a property of this market. It is a *biased-short* estimate (§3), from a *cancelled-only* sample, over *40 minutes*, at *one hour of one day*, with *diurnality unmeasured*. Nothing should quote it as characteristic before ≥24 h of continuous recording.
- **Not** that the bid ladder is demand. A bid cancelled in 9 s is not an executable bid for a human; whether it is executable is metric 6's job to quantify, not an assumption to make either way.
- **Not** that the collection is liquid. 42,601 bids and 3 sales in 40 minutes is a statement about quoting, and the liquidity fact is the 3.
- **And the standing rule:** if any of this starts to look like a clean, easy edge, that is evidence of a bug (docs/01 §10, agent brief). The most likely bug is the book reconstruction (metric 12).

---

## 3. Bid lifetimes, done properly

The design-lead is right that `MetricEngine.bid_lifetimes` drops right-censoring. The fix is larger than that: it also drops competing risks, drops `order_invalidate`, and (§6, T1) probably double-counts.

### 3.1 Fields required per order — the `order_lives` row

```
order_hash · collection · scope_kind {item|collection|trait} · token_id · criteria_id
maker · price_wei · price_eth · price_usd · quantity
t_place (valid_ts) · t_place_observed (observed_ts)
t_end · exit_reason ∈ {cancelled, invalidated, filled, expired, censored}
expiration_ts · t_censor (window end / recording end)
placement_seen (bool) · revalidated (bool) · episode_id · maker_class · method_version
```
`placement_seen = false` ⇒ excluded from the risk set, **counted and reported** (metric 12). `exit_reason = expired` is inferred at `expiration_ts` when no X row exists, and marked `source=derived`.

### 3.2 Estimator

Distinct event times `t_1 < … < t_k`. At `t_i`: `n_i` = number at risk just before `t_i` (with delayed entry where it applies), `d_i` = all-cause exits at `t_i`, `d_i^c` = exits from cause `c`. Administrative censoring at window end contributes to `n_i` up to its censoring time and never to `d_i`.

**All-cause survival — Kaplan–Meier:**
```
Ŝ(t) = Π_{t_i ≤ t} ( 1 − d_i / n_i )
```

**Greenwood variance:**
```
v(t)      = Σ_{t_i ≤ t}  d_i / ( n_i (n_i − d_i) )
Var[Ŝ(t)] = Ŝ(t)² · v(t)
```

**Band — use log-log, not Ŝ ± 1.96·SE.** The linear band leaves [0,1] in the tails, which on a curve that reaches 0.02 is where the whole story is:
```
σ(t) = sqrt( v(t) ) / | ln Ŝ(t) |
CI95(t) = [ Ŝ(t)^exp(+1.96 σ(t)) ,  Ŝ(t)^exp(−1.96 σ(t)) ]
```
Fifteen lines of Python, no library, no scipy — 1.96 is the only constant.

**Competing risks — cumulative incidence, NOT 1 − KM per cause.** Computing 1 − KM for "cancelled" while censoring fills **overstates** cancellation incidence. Aalen–Johansen:
```
F̂_c(t) = Σ_{t_i ≤ t}  Ŝ(t_{i−1}) · d_i^c / n_i          Σ_c F̂_c(t) = 1 − Ŝ(t)
```
Causes: `cancelled` · `invalidated` · `filled` · `expired`. *"Cancelled after 4 s"* and *"filled after 4 s"* are opposite facts and one curve cannot carry both.

**Bands on the CIF:** skip the Aalen delta-method algebra and use a **cluster bootstrap** — resample **by maker-episode**, B = 1000, take the 2.5/97.5 percentiles of `F̂_c(t)` on a fixed time grid. Resampling individual orders would be wrong: orders inside one bot episode are not independent, and an order-level bootstrap would produce a band roughly `sqrt(n/n_eff)` too narrow — a factor of ~15 on this data. The cluster bootstrap is also the correct band for the KM curve, and where it disagrees with Greenwood, **Greenwood is the one that is wrong here** (Greenwood assumes independent observations, which 42,601 quotes from 3 makers are not).

**RMST**, for when the median is not reached (docs/01 §5.5):
```
RMST(τ*) = ∫₀^{τ*} Ŝ(u) du = Σ_i Ŝ(t_{i−1}) · ( min(t_i, τ*) − t_{i−1} )
```
Report RMST + its cluster-bootstrap CI + `P(not ended by τ*) = Ŝ(τ*)`.

**Residual survival**, which is what metric 2 needs:
```
Ŝ(t + L | age = t) = Ŝ(t + L) / Ŝ(t)
```

### 3.3 Minimum n — and there are four of them

`n` is not one number here and reporting one is the failure mode.

| quantity | proposed minimum | why |
|---|---|---|
| `n_events` | — | never the basis of a percentile |
| `n_orders` (ended) | **≥ 30** | docs/01 §8.2 spirit; below this show every observation as a strip plot (design §4.2) |
| per-cause CIF | **≥ 10 events of that cause** | docs/01 §8.2 trait-premium row, same logic |
| **`n_eff` = maker-episode clusters** | **≥ 30** | **the binding one** |

On today's data: `n_events` = 38,286, `n_orders` ≈ 38,286, `n_eff` ≈ the number of quoting episodes, and at the *maker* level **n_eff = 3, possibly 1 if the makers cluster.** A survival curve from 3 machines is a curve about 3 machines. It is still worth drawing — it is exactly what determines whether their bids are hittable — but it must be labelled *"3 makers, unclustered"* and it must never be presented as a property of the market. Proposed registry entry:

```yaml
- id: ASM-025  name: min_clusters_for_survival_percentiles  value: 30
  layer: analysis  owner: quant-research
  rationale: >
    Percentiles from many observations of few independent agents are
    precise about the agents and silent about the market.
```

### 3.4 Expected effect of the fix

Direction is predictable, magnitude is not: the current estimator excludes censored (still-standing) orders, so it drops exactly the longest-lived liquidity — **the true median is longer than 9 s, and the p90 is longer by more.** Do not guess by how much; recompute. And do not treat a large change as a surprise — it is the expected consequence of a known defect.

---

## 4. Trait pricing

### 4.1 What can honestly be said, given the data we will have

| statement | needs | honest today? |
|---|---|---|
| "n tokens carry this trait" | traits table (97 reads) | yes, once loaded |
| "the lowest standing ask among them is X, from 1 listing" | ask book (§5.4) | yes, **with n=1 printed** |
| "the 5th-cheapest ask among them is X" | ask book, n_asks ≥ 5 | yes |
| "someone is standing a trait offer at Y for this trait" | trait-offer criteria stored | yes, **as a fact, n=1** |
| "this trait's ask floor is below the null-min for its depth" | both | yes, **as description** |
| "this trait is worth +0.3 Ξ" | the hedonic model | **no, not for weeks** |
| "this token is 25% underpriced" | hedonic + interval | **no** |

### 4.2 The honest first model

docs/01 §5.2 + §5.3, on **sales**, with listings held out:

```
ln(price_i) = α + Σ_k β_k · x_ik + γ' controls_i + ε_i
```
- **Sales only** in the fit. A listing is an ask, not a transaction; pooling them without a stratum indicator conflates ask premium with value. Listings are the **out-of-sample** set the residuals are scored on — which is also the mispricing screen.
- **α constant, not time-varying, until there is enough history.** With one collection and <2 weeks, a time-varying `α_c(t)` is fitting the noise. That means **`qa_index` is unavailable and must be reported as unavailable**, not approximated by the floor.
- **Robust, not OLS** (docs/01 §5.2; agent brief). Huber M-estimation via IRLS, or median (L1) quantile regression. One outlier sale dominates an OLS fit at n=30.
- **Rare values pooled into `other`** by frequency threshold; **no β reported for any value with n < 10 observations** — reported *unavailable*, per docs/01 §8.2.

**Shrinkage without a peer group.** One collection means no peer group, so partial-pool *within trait type* instead — a value's premium is shrunk toward the mean premium of its own trait type. Empirical Bayes, method-of-moments, library-free:

```
b_k, s_k²         : robust estimate of value k's premium and its variance
μ̂_T   = mean of b_k over values k of type T
τ̂²_T  = max( 0, Var_k(b_k) − mean_k(s_k²) )              # method of moments
β̂_k   = μ̂_T + ( τ̂²_T / (τ̂²_T + s_k²) ) · ( b_k − μ̂_T )   # James–Stein / Efron–Morris
```
A value with 40 sales is mostly itself; a value with 6 is mostly its type's mean. This is the correct answer to sparse data and it needs no PyMC.

**Retransformation — the bias that is easiest to miss.** `exp(ln fv)` is the *median*, not the mean, and using it understates fair value by roughly `σ²/2`. Use Duan's smearing (no normality assumption):
```
fair_value_j = exp( α̂ + Σ_k β̂_k x_jk ) · ( (1/n) Σ_i exp(e_i) )
```

**Intervals — cluster bootstrap, again.** Robust-regression SEs at n≈30 with many dummies are not to be trusted. Resample **by token** (a token that sells twice is not two independent observations) and by day, B = 1000, and take percentiles of the linear predictor. Then, per docs/01 §9.1, the actionability test uses the **lower** bound:
```
actionable ⟺ fair_value_lower_90(j) − ask_j − total_round_trip_costs > 0
```
with `total_round_trip_costs` at the docs/01 §8.1 hurdle (commonly 20–40% here, spread dominating). **Apply the economic test first** — it disqualifies most candidates before any statistics run.

### 4.3 Sample size, plainly

| target | needed | on 4.5 sales/h |
|---|---|---|
| collection-level fit at all (docs/01 §8.2) | 30 sales | ~7 h of stream |
| any single trait value's β | 10 sales carrying it | depends on frequency; common values ~1 day, rare values **never from stream alone** |
| a full dummy model, ~150 trait values | ≥1,500 sales | **~14 days of stream, or 35 REST reads (§5.2)** |
| ML of any kind (docs/01 §6.4) | 500 obs, ≤1 feature per 20 | not in scope, and not soon |

That last row of the middle column is the whole argument for §5.2. **The difference between "a trait model in two weeks" and "a trait model this afternoon" is about 35 REST reads.**

### 4.4 What to show meanwhile, without pretending

Design's trait comparison table (§1.2), with these columns and no "value" column at all:

```
clause · n_tokens · n_standing_asks · lowest ask · 5th-cheapest ask · median standing ask
       · highest standing bid (+ kind: collection|trait|item) · n_sales_window · last sale
       · floor_vs_null_min (n)
```
Rules: every cell carries its n. `—` where undefined, never 0. **No premium ratio below n_asks = 3.** The ECDF panel (design §1.2) is the right hero here — *"the floor is 0.42"* and *"three asks between 0.42 and 0.44 then nothing until 1.9"* are different facts and only the second is tradeable. Annotate each ECDF line with its null-min marker so the eye is not fooled by the min-of-n effect (metric 5).

---

## 5. Data we do not have, ranked by value per REST read

Budget: **120 reads/hour, shared account-wide.** Ranked by unlock ÷ reads. Endpoint paths and page limits to be confirmed by the tech-lead against the API reference before anything is built — the numbers below are cost *models*, not verified calls.

**5.1 — On-chain transfer / holder ledger. 0 OpenSea reads.**
Via an RPC or indexer (Alchemy free tier / Dune), not OpenSea. ERC-721 `Transfer` logs for one contract, backfilled by block range.
*Unlocks:* `holder_count`, `gini_concentration`, `top10_share`, holding-period distribution, `diamond_hand_ratio`, the transfer graph, wallet-clustering inputs (docs/01 §4.3 — which every maker/holder count above is currently missing), private/OTC transfers invisible to the marketplace, and REQ-D-15..17 entirely. Also the only path to benchmark questions 7 and 10.
*Cost:* zero budget; the constraint it consumes is developer time and a provider key.
*Value per REST read: unbounded. Rank 1 on the stated criterion, and it should be honestly labelled as "free on this budget, not free overall".*

**5.2 — Historical sales, via the events endpoint. ~35 reads, one-time.**
`event_type=sale`, 200/page, `before`/`after` bounds (REQ-D-13). Argonauts: $9.1M lifetime volume at roughly $1.3k/sale ⇒ ~7,000 sales ⇒ **~35 pages.**
*Unlocks:* the hedonic model **today** instead of in two weeks; `qa_index`; per-trait β at n ≥ 10 for common values; comparable sales (benchmark Q5); realised-price history for `trade_location`; the training partition itself — right now the partition scheme (docs/01 §7.4) has nothing to partition.
*Does NOT unlock:* the bid/ask book history (offers and listings history is far more voluminous), or anything wash-filtered.
*Warning that must ride with it:* historical sales are the most wash-contaminated data we will hold and we have no filter. Fit robustly, and apply the two structural exclusions available without a model — `maker == taker`, and a token round-tripping between the same pair within 24 h — flagging, never deleting.
*Rank 2, and the highest value-per-read purchase available on the OpenSea budget.*

**5.3 — Token list + traits. ~97 reads, one-time. Already built (`traits.command`).**
47 for the list + up to 50 OpenSea fallbacks; traits themselves come from `metadata_url`, unmetered.
*Unlocks:* every trait metric, the screener, rarity (computed locally — rarity is never fetched), and it is a **hard prerequisite for 5.2's value**: sales without traits cannot fit a hedonic model.
*Rank 3 only because 5.2 is a bigger unlock per read; in execution order it comes first.*

**5.4 — Standing listings snapshot. ~9 reads, recurring.**
Best listing per token for the collection, ~801 listed at 100/page.
*Unlocks:* the true floor — fixing the upward bias in §0; the ask ECDF and `floor_depth_k`; `spread_exec` as a real number rather than an upper bound; trait floors with non-zero n; `ask_staleness` across the whole book instead of only listings placed after 10:20.
*Cadence:* hourly = 9 reads/h = **7.5% of budget**. Degrade to 4-hourly under contention with backfill. Each snapshot is a **prevalent** cohort — seed the price book with it, and if used for durations at all, enter with delayed entry (metric 10).
*Rank 4, and rank 1 among recurring costs.*

**5.5 — Collection offers snapshot. ~2 reads, recurring.**
*Unlocks:* the true best bid; corrects the downward bias in §0; makes `bid_depth` complete rather than "offers placed since 10:20".
*Cadence:* hourly = ~2 reads/h. Combined with 5.4 the standing book costs **~11 reads/h — under 10% of budget for a complete two-sided book.** That is the single best structural trade available.

**5.6 — ETH/USD reference series. 0 OpenSea reads.** REQ-D-25.
*Unlocks:* honest USD on backfilled history, `eth_beta`, dual-denomination P&L. Note we already get `implied_ethusd` per observed event, which is adequate for events we witnessed and useless for 5.2's backfill.

**5.7 — Reconciliation snapshot (collection stats). 1 read per rotation.** REQ-D-10, mandatory. Also the denominator for metric 12's drift.

**5.8 — Per-token offers / trait offers queried by criteria. 1 read each. Do not.**
9,212 tokens = three days of budget for a snapshot that is stale on arrival. The stream already delivers these continuously and unmetered. *Rank last, and the reason to name it is so nobody proposes it.*

**Summary:** ~132 one-time reads (5.3 + 5.2) ≈ **1.1 hours of budget buys the entire trait-pricing capability**, and ~11 reads/hour ≈ **9% of budget buys a complete standing book, permanently.** Everything else on the wishlist is either free (5.1, 5.6) or not worth it (5.8).

---

## 6. Three things for the tech-lead to verify in `src/navanax/metrics.py`

**T1 — `bid_lifetimes`, lines 446–452: the join is unbounded, so one bid can contribute several lifetimes.**
```sql
FROM events b JOIN events c ON c.order_hash = b.order_hash
WHERE ... c.event_type='item_cancelled' AND c.valid_ts >= b.valid_ts
```
There is no `MIN(c.valid_ts)` and no "first termination" restriction. Any `order_hash` with two matching cancel rows (a re-emitted frame, a re-used hash, or a cancel that follows an invalidate) yields the cross product, inflating `n` and **skewing the distribution long** — in the opposite direction to the censoring bias, so the two defects partly mask each other and the net sign of the error is currently unknown. Also `order_invalidate` is excluded as a terminator here while the `cancel_count` metric (line 61) counts it, so two panels on the same page disagree about what ends an order.
*Verify:* `SELECT COUNT(*) FROM (SELECT order_hash FROM events WHERE event_type IN ('item_cancelled','order_invalidate') GROUP BY 1 HAVING COUNT(*)>1)` — if that is non-zero, `n = 38,286` is not a count of bids. Then compare `n` against `COUNT(DISTINCT b.order_hash)`.
*Secondary:* `pct()` at line 454 returns percentiles even when `n < 30`, relying on the caller to honour `percentiles_reliable`. REQ-F-19 says the system should make that impossible — return `None`.

**T2 — `immediacy_cost` is biased narrow, and the bias grows with interval width.** Lines 279–292 (`MIN`/`MAX` aggregation) feeding lines 322–328 (`ga[k] − gb[k]`).
The metric is `min(ask seen in bucket) − max(collection bid seen in bucket)`. Taking the *smallest* ask and the *largest* bid over a window returns **the narrowest spread that occurred inside it, not the spread** — the two legs need not have coexisted for a single second. On a book with ~250 collection offers/hour the bias is material at 5 m and severe at 1 d, and the mechanism is stronger than the resting-book caveat already recorded in docs/08 §5. It can also go **negative**, which would render on the front-page KPI as a free arbitrage.
*Verify:* count buckets where the value is `< 0` at `1h` and `1d` on the real store; compare the `5m` series aggregated up to `1d` against the `1d` series directly — they should not agree, and the direction of disagreement is the size of the bias.
*Also add a test:* any negative `immediacy_cost` is an alarm, not a data point. A surprisingly good result is evidence of a bug.

**T3 — the `dead` predicate silently keeps orders alive.** Lines 394–397 (`live_book`) and 478–480 (`screener`), same subquery.
Three distinct failures: (a) it ignores `order_revalidate` — an order invalidated and then revalidated is dead forever (1 revalidate against 1,529 invalidates in 40 min, so rare and therefore hard to notice); (b) it ignores `quantity`, so a collection offer good for 5 tokens counts as one unit of depth, and a partially-filled offer is marked entirely dead; (c) `d.valid_ts >= e.valid_ts` never matches when the terminating row has `valid_ts IS NULL` — the schema permits it (normalize.py line 56) — so such an order stays standing forever.
*Verify:* `SELECT event_type, COUNT(*) FROM events WHERE valid_ts IS NULL GROUP BY 1;` and `SELECT COUNT(*) FROM events WHERE event_type='order_revalidate';` — both should be small, and both should be zero in the live book's notion of "dead".

*Also noticed, lower priority, not for this pass:* `apply_transform` (line 186) anchors `PCT`/`LOG` to the first non-null bucket, which on an 8-listings-per-40-minutes book is one arbitrary listing — this is design-lead's Q2 and it is a semantics question, not a bug; and the trait-filter comment at line 268 cites BUG-044 where docs/08 §4a says BUG-045.

---

## 7. What I am handing off, and what I am not

Everything above is a **measurement specification**. None of it is a signal, and I have registered no hypothesis, because registration precedes examination (docs/01 §7.1) and examining the data to decide what to register would be the exact failure the rule prevents.

Three mechanisms are *named* here and are the natural first registrations — stale-ask (metric 3), trait-offer revealed demand versus ask floor (metrics 4–5), and liquidity provision at the bot bid (metric 8). Each needs its own written registration with mechanism, prediction, test design, kill criteria and prior, **before** it is measured. When any reaches SPECIFIED it goes to `validator`, not back to me.

Two things I want on the record now:
1. **The bot layer is the most likely source of a spurious result in this project.** It has enormous nominal n and tiny effective n, which is precisely the shape that generates confident nonsense. Every n printed anywhere should be accompanied by its effective n from day one, before anyone gets attached to a number.
2. **The stream-only book is incomplete in a signed direction** (§0), and every metric in §1 inherits that until §5.4/5.5 land. Until then the honest label on `spread_exec` is *upper bound*, not *spread*.
