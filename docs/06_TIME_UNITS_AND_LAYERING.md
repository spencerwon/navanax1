# Time, Units, and the Assumption/Structure Boundary

**Version:** 1.0 · **Date:** 2026-09-09
**Companion to:** `00_REQUIREMENTS.md`, `01_METHODOLOGY.md`

---

## 1. Chart intervals — you asked for seventeen, the right answer is "all of them, free"

You listed: minute, 3-hour, 6-hour, hourly, daily, 2-day, weekly, monthly, 2-month, 3-month, 6-month, yearly, 2-year, YTD, MTD, DTD, HTD.

Do **not** build seventeen aggregation pipelines. Build one thing correctly and every interval — including ones you have not thought of yet — comes free:

> **Store at event grain with exact timestamps. Aggregate on demand at query time.**

Event grain means every sale, listing, delisting, offer, and transfer is stored with its own precise `event_timestamp`, never pre-bucketed into hours or days. A 6-hour bar is then a query, not a stored table. So is a 37-minute bar, if you ever want one.

The cost of the alternative is severe and permanent: if you store hourly buckets and later want 15-minute resolution, **the information is gone** and cannot be recovered — you would have to re-download history you cannot re-download. Pre-aggregation is one of the few genuinely irreversible mistakes available here.

### 1.1 The honest caveat about short intervals

For a collection doing 20 sales a month, at most 20 of roughly 43,200 one-minute buckets contain anything — the chart is empty 99.95% of the time. That is not a reason to exclude minute resolution — it is a reason to be clear about **which series you are looking at**, because the platform has two kinds and they behave completely differently.

| Series type | Grain | Behavior | Right for |
|---|---|---|---|
| **Event series** | Exact timestamps, irregular | Sparse. A point exists only where something happened | Sales, listings, offers, transfers. Any short interval |
| **Regular-grid series** | Fixed cadence, resampled | Every bucket has a value or an explicit gap marker | `qa_index`, floor, listing depth, holder metrics. Any statistical method needing a regular grid |

**Never silently resample an event series onto a regular grid.** That is where interpolation sneaks in and quietly fabricates data. When a regular grid is required — and several statistical methods do require one — the resampling is explicit, its gaps are marked as gaps, and any chart or analysis built on it carries that provenance (REQ-D-12, REQ-D-14).

A practical consequence: on a thin collection, a 1-minute chart of sales is mostly whitespace with occasional marks. **That is the correct rendering.** It honestly shows you that the collection barely trades, which is information. A chart that smooths it into a continuous line is lying to you about liquidity, which is the single most important variable in this market.

### 1.2 Interval configuration

Intervals live in `config/intervals.yaml`, never hard-coded (REQ-N-09):

```yaml
intervals:
  # fixed-duration buckets
  - {id: 1m,   label: "1 min",   duration: 60}
  - {id: 5m,   label: "5 min",   duration: 300}
  - {id: 15m,  label: "15 min",  duration: 900}
  - {id: 1h,   label: "1 hour",  duration: 3600}
  - {id: 3h,   label: "3 hour",  duration: 10800}
  - {id: 6h,   label: "6 hour",  duration: 21600}
  - {id: 1d,   label: "1 day",   duration: 86400}
  - {id: 2d,   label: "2 day",   duration: 172800}
  - {id: 1w,   label: "1 week",  duration: 604800}
  - {id: 1mo,  label: "1 month", calendar: month,  n: 1}
  - {id: 2mo,  label: "2 month", calendar: month,  n: 2}
  - {id: 3mo,  label: "3 month", calendar: month,  n: 3}
  - {id: 6mo,  label: "6 month", calendar: month,  n: 6}
  - {id: 1y,   label: "1 year",  calendar: year,   n: 1}
  - {id: 2y,   label: "2 year",  calendar: year,   n: 2}

anchored_ranges:            # "to date" — a range, not a bucket size
  - {id: HTD, label: "Hour to date",  anchor: hour}
  - {id: DTD, label: "Day to date",   anchor: day}
  - {id: MTD, label: "Month to date", anchor: month}
  - {id: YTD, label: "Year to date",  anchor: year}
```

Note the distinction in that config: `intervals` are **bucket sizes** (how wide is each bar), `anchored_ranges` are **window bounds** (where does the chart start). YTD is not a bar width; it is "from January 1 until now, at whatever bar width you chose." Conflating the two is a common source of off-by-one chart bugs.

### 1.3 Calendar arithmetic is not duration arithmetic

Months and years are not fixed durations. "One month ago" from March 31 is ambiguous, and every date library resolves it differently. Fix the convention once, in config, and test it:

- Month arithmetic clamps to the last valid day (Mar 31 − 1 month = Feb 28/29).
- All internal timestamps are **UTC**. Display converts to America/Chicago.
- Day boundaries for DTD/MTD/YTD use **the display timezone**, not UTC, because "today" means your today. This will produce an off-by-one somewhere; the fixture tests exist to find it.
- DST transitions mean some local days are 23 or 25 hours. Bucket boundaries follow local calendar days, not 86400-second increments, for calendar-anchored ranges.

---

## 2. Denominations and transforms

You asked for percentage, dollar, differential, and log terms. Formalizing that as a matrix: **any metric × any denomination × any transform** is a valid request, and the platform should serve it without a bespoke implementation per combination.

### 2.1 Denominations

| Code | Meaning | Use for |
|---|---|---|
| `ETH` | Native ETH (stored as integer wei) | Within-market relative value. Comparing tokens and collections to each other |
| `USD` | Converted at the **historical rate at that timestamp**, from the ETH/USD reference series (REQ-D-25) | Absolute P&L. What you actually made or lost |
| `NATIVE` | The chain's own token for non-ETH chains | Cross-chain watchlist entries |

**Both ETH and USD are computed and stored for every price, with the primary denomination recorded** (REQ-F-02). This requires a historical ETH/USD series the marketplace API does not provide; ingesting and maintaining it is REQ-D-25, owned by `data-engineer`. This is not redundancy — they disagree constantly and the disagreement is itself information. A collection whose ETH floor was flat through a 30% ETH drawdown lost 30% of its dollar value. A trader looking only at the ETH chart sees a calm sideways market and a trader looking only at the USD chart sees a crash. Both are real, and you need both visible.

`eth_beta` (methodology §3.4) quantifies this per collection, and it is one of the more decision-relevant metrics in the set.

### 2.2 Transforms

| Code | Formula | When it is the right choice |
|---|---|---|
| `ABS` | raw value | Levels. What something costs |
| `PCT` | `(v_t − v_0) / v_0` | Returns over a stated window. Intuitive, but **not additive across periods** |
| `LOG` | `ln(v_t / v_0)` | **The correct default for all statistical work.** Additive across time — log returns sum over consecutive periods, percentage returns do not. Symmetric under reversal: a move and the move that exactly undoes it have equal magnitude in log space (+50% is `ln 1.5` = +0.405; the −33.3% that returns you to the start is `ln 0.667` = −0.405), whereas in PCT those are +50% and −33.3%. And NFT prices are strongly right-skewed, so log space is where the distributions behave |
| `DIFF` | `v_a − v_b` | Spreads. Two series in the same units |
| `RATIO` | `v_a / v_b` | Relative value between series |
| `ZSCORE` | `(v − μ) / σ` over a stated window | Cross-sectional comparison across collections with different scales |
| `PCTILE` | rank within a stated population | **More robust than z-score** in a market with fat tails and no stable scale |
| `BPS` | `PCT × 10,000` | Small differences — spreads, fee comparisons |

**Every transform declares its window and its baseline in the output.** "Up 12%" is meaningless without "over what period, from what starting point." The API returns these as fields, and the UI renders them, so a chart or a number can never be quoted without its basis.

### 2.3 The default that prevents the most errors

Statistical work uses `LOG` on `qa_index`. Display defaults to `PCT` because it is what a human reads naturally. **These must never diverge silently** — a chart labelled "% change" whose underlying model fitted log returns is a small but real source of misread magnitudes, and the axis label must say which it is.

---

## 3. The metric request contract

The consequence of §1 and §2: any chart, screen, or analysis is fully specified by a tuple, and the platform implements the tuple once rather than implementing views one at a time.

```
MetricRequest = {
  metric:       "qa_index" | "floor_price" | "sales_count" | ... ,
  scope:        collection | token | trait | peer_group | universe,
  denomination: ETH | USD | NATIVE,
  transform:    ABS | PCT | LOG | DIFF | RATIO | ZSCORE | PCTILE | BPS,
  interval:     1m | 1h | 1d | ... ,
  range:        {start, end} | anchored: HTD|DTD|MTD|YTD,
  wash_filter:  filtered | raw | both,
  as_of:        timestamp        # bitemporal — what did we KNOW at this moment
}
```

Two fields deserve attention because they are easy to omit and expensive to omit:

**`wash_filter`** defaults to `filtered`, with `raw` always available and `both` showing the divergence. If a conclusion changes depending on this flag, the conclusion is about the filter, not the market (methodology §4.4).

**`as_of`** is what makes point-in-time correctness structural. Every request carries it. For a live view it is `now`. For a backtest it is the simulation timestamp, and the data-access layer **cannot return** records observed after it. A strategy has no path around the accessor — which is why look-ahead bias is prevented by architecture here rather than by careful coding (REQ-F-28).

---

## 4. The assumption/structure boundary

You said: *"Let's be very clear to separate all assumptions and analysis from our structural code."* That instinct is right, and it is worth making precise, because "keep them separate" is easy to say and easy to violate one small convenience at a time.

### 4.1 The definition

> **Structural code** answers: *what happened?*
> **Analytical code** answers: *what does it mean?*
>
> A fact is structural. A judgment is analytical. **If two reasonable people could disagree about it, it is a judgment.**

That test does the work. "This sale occurred at 14:22 UTC for 0.53 ETH" — nobody disagrees, structural. "This collection is illiquid" — depends entirely on where you set the threshold, analytical.

### 4.2 The layers

```
┌──────────────────────────────────────────────────────┐
│  STRATEGY          Judgment, freely revised          │
│  Signals, hypotheses, thresholds, trade rules        │  ← assumptions live here
├──────────────────────────────────────────────────────┤
│  ANALYSIS          Models with stated assumptions    │
│  Hedonic pricing, survival, regime, wash scoring     │  ← assumptions DECLARED here
├──────────────────────────────────────────────────────┤
│  DERIVATION        Deterministic computation         │
│  Metrics, aggregations, transforms                   │  ← NO assumptions
├──────────────────────────────────────────────────────┤
│  STRUCTURE         What happened, as recorded        │
│  Ingestion, normalization, storage, provenance       │  ← NO assumptions
└──────────────────────────────────────────────────────┘
```

**Dependencies point downward only.** Structure knows nothing about analysis. Derivation knows nothing about strategy. A change to a trading hypothesis must be able to touch nothing below the top layer — and if it does, the layering has been violated somewhere and that is a defect worth fixing before continuing.

### 4.3 Rules per layer

**STRUCTURE and DERIVATION — no assumptions at all.**

- No thresholds. No "if volume is low then..." Low is a judgment.
- No filtering by quality, importance, or plausibility. Store the weird record; flag it, never drop it.
- No imputation, no interpolation, no forward-fill. A gap is a gap.
- No opinion about what matters. Store the fields; let the layers above decide which are interesting.
- Deterministic: identical inputs produce identical outputs, forever.

**The test:** if the code contains a number that could reasonably be a different number, it does not belong in these layers.

**ANALYSIS — assumptions permitted, declaration mandatory.**

Every model declares, in a machine-readable form attached to its output:

- What it assumes (distributional form, independence, stationarity, whatever applies).
- What diagnostics it ran and whether they passed.
- What sample size backs it, effective not nominal.
- What uncertainty attaches to its output.
- What it does when its assumptions fail — which must be *refuse to produce a number*, never *produce one quietly*.

**STRATEGY — assumptions are the whole point.**

This layer exists to hold judgment. It is where thresholds, weights, and hypotheses live, and it is expected to change often. It is also where the hypothesis protocol applies: registration before examination, one-directional lifecycle, no retesting variants (methodology §7).

### 4.4 The assumptions registry

Every assumption in the system is registered in `config/assumptions.yaml` with an ID, a value, a rationale, and an owner. Nothing in ANALYSIS or STRATEGY may use a magic number that is not in this file.

```yaml
assumptions:
  - id: ASM-001
    name: illiquid_sale_count_range
    value: {min: 5, max: 300, window_days: 30}
    layer: strategy
    rationale: >
      Thin enough that mispricing persists, active enough that a trait
      model has data to fit. Both bounds are guesses to be tuned.
    owner: quant-research      # proposing owner; see note below
    review: 2026-12-01

  - id: ASM-014
    name: min_sales_for_collection_hedonic
    value: 30
    layer: analysis
    rationale: >
      Below this, trait premia are estimated mostly from peer-group
      shrinkage rather than the collection's own data (methodology §5.3).
    owner: quant-research
    review: 2026-12-01
```

**On `owner`.** The owner is the agent responsible for the value being *right* and for proposing changes to it — not an agent with unilateral authority to change it. `assumptions.yaml` is configuration, so a change is proposed on a branch and merged through the normal gate (`docs/04_ENVIRONMENTS.md` §5). `quant-research` holds a narrow carve-out to edit this one file in a branch (`docs/02_AGENT_HIERARCHY.md` §3.8, note ³); it cannot merge its own change.

Two properties make this worth the overhead:

1. **You can audit every judgment in the system by reading one file.** Which is what you asked for — you want to interrogate the assumptions, and this is the surface for doing that without reading code.
2. **When a result changes, you can tell whether the world changed or you did.** Config changes are logged with timestamp and rationale (REQ-N-10). Without this, a shifted result is unattributable, and unattributable results are how a project loses confidence in its own history.

### 4.5 What this buys, concretely

When you eventually decide the illiquidity band should be 10–500 sales instead of 5–300:

- You edit `ASM-001` in one file.
- Nothing in STRUCTURE, DERIVATION, or ANALYSIS changes.
- Every stored record is still valid — nothing needs re-ingesting.
- The change is logged, so a screen that returned different results last month is explainable.
- The old value is recoverable, so a past analysis can be reproduced exactly (REQ-N-15).

Compare the alternative, where `5` and `300` are typed into a screener query, a signal, and a chart filter. Now the change is three edits, you find two of them, and the third quietly disagrees with the others for months.

---

## 5. Argonauts as the primary test collection

Good choice, for a reason worth stating: **you can tell when a number is wrong.** That is the single most valuable property in a test fixture, and it is exactly what an unfamiliar collection cannot give you. Automated tests catch the errors you predicted; a knowledgeable human catches the ones you did not.

From the project screenshot, as of 2026-09-08: floor $1,324.15, 9,210 items, $9.1M total volume, 801 listed (8.7%).

**Two honest caveats.**

**It may be more liquid than the target profile.** At 8.7% listed it sits inside the §2.2 band, but $9.1M cumulative volume on 9,210 items suggests a collection that trades more actively than the thin market where the thesis says the edge lives. That makes it an excellent **correctness** fixture — enough sales that models actually fit, enough trait structure to exercise the hedonic pricing — but it will not stress-test the sparse-data paths. Those are exercised by the `sparse_collection` fixture (8 sales) and, eventually, by a genuinely thin second collection.

**One collection cannot validate cross-collection logic.** Peer grouping, relative strength, cointegration, and the leave-one-collection-out robustness test all need several collections by construction. Argonauts is the primary fixture, not the only one.

**Suggested progression:** Argonauts alone through Phase 0–1 (correctness, where your domain knowledge is the test oracle) → add 4–5 collections spanning liquidity tiers for Phase 2 → the full watchlist for Phase 3.

**A specific request.** When the trait-adjusted fair value model first produces numbers for Argonauts, look at its estimates for tokens you know well and tell us where it is wrong. A model that prices a trait you know to be desirable as worthless has a bug or a data problem, and you will spot that in seconds where a test suite would not spot it at all. That kind of review is not a nice-to-have here — it is the highest-value validation available in Phase 2, and it is available only because you picked a collection you actually know.
