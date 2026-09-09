# DESIGN — Views, chart technology, and the two charts the Operator called out

*Proposal. 2026-09-09, design-lead. To be fact-checked by the tech-lead and built by developers.*
*Status: PROPOSED. Nothing here is built. **Four** questions — three in §6, one in §8.3 — are the Operator's and are **not** defaulted.*
*Revised 2026-09-09 after `TECHLEAD_2026-09-09_factcheck.md` §1B (six corrections applied in place, each marked) and after two Operator additions (§8).*

## 0. What this responds to

Operator, today, verbatim:

1. "the large charts are still clunky and the zoom, pan etc options are not helpful."
2. "The how long a bid stands is especially ugly but it provides very interesting info which itself should be filterable and connected to specific event times and other metadata."
3. "When a trait is filtered for we only want the highest [bid] line for that trait group and the lowest ask/list line for that trait. The floor price can be included dotted or pale below."
4. "When multiple traits are selected we want to see the combined trait floor, single trait floors, and baseline floor."
5. "Maybe it's time to start expanding out to more tabs and views."

My read of (1): **this is a configuration defect, not a library defect.** §2 recommends keeping the library and deciding the interaction.

> **Corrected 2026-09-09 after `TECHLEAD_2026-09-09_factcheck.md` §1B.** My first draft named four symptoms; two of them do not exist. `ui/index.html:161` already sets `displaylogo:false` and removes two buttons — the bar is **seven** buttons, not eleven — and `scrollZoom` is unset, which on a cartesian plot means Plotly's default of **false**, so wheel-zoom never hijacked page scroll. The symptoms that are real: **drag-to-box-zoom that rescales the y-axis**, **double-click-to-reset that nobody discovers**, and **the seven-button bar itself**. This matters more than a footnote: a config change justified by a symptom that is not there ships a "fix" that changes nothing the Operator complained about. Every correction the tech-lead filed against this document is applied in place below and marked the same way.

Binding constraints (unchanged, restated so a developer reading only this file cannot violate them):

- Undefined is a **hole**. Never bridged, never forward-filled, never zero. Every series arrives on the full bucket grid.
- Every number carries its basis; every percentage carries its count; no point estimate without uncertainty.
- No smoothing of a raw series in place. A moving average is a labelled overlay or it does not exist.
- One HTML file over loopback. One chart library. Container cannot reach CDNs; the Mac can.
- Real data today: ~48 events/s of bot bid/cancel churn; **8 listings and 3 sales in 40 minutes**. Every chart below must read with 3 points and with 40,000.

---

## 1. Information architecture — four views

> **§8 adds two more** (Ledger, Wallets) at the Operator's request, taking the final IA to six: **Market · Traits · Flow · Ledger · Wallets · Health**. §1 below is the original four; read §8.0 for what changes to pay for the other two.

Tabs live in the header as a segmented control (verde active), routed on the URL hash so a view is linkable (`#/market`, `#/traits`, `#/flow`, `#/ledger`, `#/wallets`, `#/health`) — REQ-F-40 will need deep links into evidence, and retrofitting routing later is more expensive than doing it now.

**Global vs local controls.** Collection, range, interval, denomination, transform, and the trait filter are *global state*: they persist across tabs and survive reload (hash + `localStorage`). Each view may override the **default** range/interval on first visit; after the Operator changes it, his choice sticks. Per-view controls (e.g. exit-reason toggle on Order flow) sit in a sub-bar inside the view, never in the global bar.

The trait sidebar shows on **Market** and **Traits**. On **Flow** and **Health** it collapses to a read-only chip row with a "clear" button — the filter is still applied and must be visible, but 272 px of checkboxes is not earning its place there.

Standard chart heights, three only: **420 px** (hero), **300 px** (standard), **200 px** (strip). Cards keep 14 px radius, 14 px gap, 12-column grid.

### 1.1 Market — "what is this collection doing, and what does immediacy cost?"

Default **6 h / 5 m**. Filterable: collection, traits, range, interval, denom, transform.

| panel | span | height | contents |
|---|---|---|---|
| KPI row | c12 | — | 5 cards: lowest ask now · collection offer now · immediacy cost now · volume 24 h · sales 24 h (§5.3) |
| **Price & liquidity** | c8 | 420 | lowest ask (orange), collection offer (green), top item bid (blue). Range chips + range strip (§2.3). MA overlay slot, disabled until ≥24 h of data |
| Live book | c4 | 420 | asks / collection offers / item bids, scrolling, each section with its own count |
| Immediacy cost | c8 | 300 | absolute on left axis (yellow solid), % of ask on right axis (grey dashed). Holes where either leg is absent — never bridged |
| **Order churn** | c4 | 200 | bids + cancels per interval, stacked bars |
| **Real activity** | c4 | 200 | listings + sales + transfers per interval, stacked bars |
| Sales tape | c6 | 300 | most recent 25 |
| Event mix | c6 | 300 | horizontal bars with count **and** share % of window total |

Why activity is split into two panels: at the measured rate (~63.9 k events/h), a 5 m bucket holds **~5,325 bids against 0–2 listings** — three to four orders of magnitude. *(Corrected per factcheck D-U3; my first draft said 14,400, which is a 13.5-minute bucket. The argument is unaffected.)* A stacked bar with a category four orders of magnitude larger than another is not a chart, it is a solid rectangle. Two panels on a shared x-axis, each with its own scale, shows both. A log axis would also work and lies less about "twice as tall"; splitting lies not at all.

### 1.2 Traits & screener — "what is this trait worth, and is anything mispriced right now?"

Default **24 h / 1 h**. Filterable: traits (sidebar, AND-across-types / OR-within-type), price band, `has standing ask`, `has standing bid`.

| panel | span | height | contents |
|---|---|---|---|
| **Trait price lines** | c12 | 420 | the §3 spec. Behaviour changes with 1 vs ≥2 selected clauses |
| Trait comparison | c5 | 300 | one row per selected clause, plus **combined** and **baseline**: n tokens · n standing asks · lowest ask · highest bid · last sale · median sale (with n) · premium vs collection floor (× and %, with n). "—" where undefined |
| **Ask depth (ECDF)** | c7 | 300 | x = price, y = cumulative count of *standing* asks. One step line per selected set + baseline. REQ-F-12. Reads fine at n=8 — eight visible steps |
| Screener | c12 | — | as today: thumbnails, every trait column, lowest ask / highest bid / last sale, sortable, paged 50 |

The ECDF is the panel that makes a thin book legible. "The floor is 0.42 Ξ" and "there are three asks between 0.42 and 0.44 and then nothing until 1.9" are different facts and only the second one is tradeable.

### 1.3 Order flow — "who is making this market, and how long does liquidity actually stand?"

Default **1 h / 1 m**. This is where 99.4% of the events live, so it gets its own view.

| panel | span | height | contents |
|---|---|---|---|
| **Bid lifetime** | c8 | 420 | the §4 spec: survival curve + exit-reason histogram + placement-time mini-map |
| Makers | c4 | 420 | wallet · events · share % (with n) · bids · cancels · listings · sales · median lifetime · median distance to floor. Click a maker → filters the lifetime panel |
| **Bids vs floor** | c12 | 300 | x = time, y = bid price; points coloured by maker (top 6 + "other"); lowest ask drawn as an orange step line over the top. A bot ladder is visible here and nowhere else |
| Drill list | overlay | — | 480 px slide-over, opened by clicking a histogram bin or a scatter point (§4.3) |

Point budget on "Bids vs floor": cap at 20,000 rendered points via `scattergl`. Above that, **say so and sample**: "showing 1 in 7 of 141,208 bids (systematic sample, k=7)" printed in the basis line. Never thin silently.

### 1.4 Data health — "can I trust anything on the other three tabs?"

Default **24 h / 1 h**. No trait filter (it is about ingestion, not about the market).

| panel | span | height | contents |
|---|---|---|---|
| **Coverage timeline** | c12 | 140 | one horizontal band across the range: green = listening, red = gap, grey = before recording started. Hover gives reason + irrecoverable classes |
| Gaps | c6 | 300 | as today |
| Integrity audit | c6 | 300 | as today |
| REST budget | c6 | 200 | reads spent this hour / 120 measured · backfill queue depth · onboarding % with progress bar |
| Store stats | c6 | 300 | events by type · **unparsed count with reasons** (click → raw frame) · landing bytes · oldest/newest `observed_at` · **stream lag** (`observed_at − valid_at`, p50/p95, with n) |

Stream lag is new and cheap: both timestamps are already stored. If it drifts, every "now" on the other three tabs is stale by that much, and nothing currently tells us.

**Deliberately deferred to phase 2:** cross-collection compare, heatmap (REQ-F-11), rarity/residual scatter (REQ-F-13), alerts, backtest, watchlist. One collection is not a cross-section.

---

## 2. Chart technology

### 2.1 What we actually need to draw

| capability | needed by |
|---|---|
| time series with **null holes** | every price chart |
| bar + line in one pane, **dual y-axis** | immediacy cost (abs + % of ask); volume + price |
| scatter, ~20 k points | bids vs floor |
| **non-time x-axis** | survival curve (x = seconds), ECDF (x = price) |
| step lines (`hv`) | survival, ECDF, standing-floor |
| horizontal bars | event mix |
| fully custom hover card | everywhere (Operator called this out on 2026-09-09) |
| brush / range select | lifetime mini-map, range strip |
| no modebar, no wheel-zoom | Operator, today |

### 2.2 The three candidates

| | Plotly 2.35 | Lightweight Charts 4.x | hand-rolled SVG |
|---|---|---|---|
*Rows marked † are my understanding of LWC 4.x and are **not verified** — the library is not in this repo and this container cannot reach a CDN (factcheck D-U1). The recommendation below is deliberately restructured so that it does not rest on them.*

| null holes | yes — `connectgaps:false` | yes — whitespace data † | yes |
| bar+line, dual axis | yes | yes (histogram series + 2nd price scale) | yes, by hand |
| scatter ~20 k | yes (`scattergl`) | no scatter series † | perf work |
| **non-time x-axis** | yes | believed no — horizontal scale is time/logical † | yes |
| step lines | yes | yes † | yes |
| horizontal bars | yes | no † | yes |
| custom hover card | via `plotly_hover` + our own div (~60 lines, once) | **best of the three** — you build it from crosshair events | total control |
| brush / range select | yes (`dragmode:'select'` → `plotly_selected`) | no built-in | by hand |
| CDN | cdn.plot.ly, ~3.6 MB min / ~1.1 MB gz | cdnjs, ~46 KB gz | none |
| effort | **zero migration**, ~1 day of config | rewrite every chart, **plus a second library** for survival/ECDF/mix/scatter | a quarter |

### 2.3 Recommendation: **keep Plotly. Fix the interaction model.**

Lightweight Charts is genuinely better at the one thing it does — a price chart that feels like a trading terminal — and it is far smaller.

**The reason to stay is effort, and that reason alone is sufficient.** Keeping Plotly costs zero migration and about a day of configuration. Switching costs a rewrite of every chart on the page. The Operator asked for more views and better-behaved charts; he did not ask for a chart-library migration, and spending the week on one would deliver neither.

*(Rewritten per factcheck D-U1. My first draft rested the whole recommendation on "its x-axis is a time scale and cannot be anything else" — a claim about a library I cannot reach from this container and did not verify. I still believe it is true for LWC 4.x, and if it is, LWC additionally cannot draw §4's survival curve (x = seconds), the ECDF (x = price), or the event mix (horizontal bars), which would mean carrying **two** chart libraries. But the effort argument decides it without that claim, so the claim is no longer load-bearing.)*

**What would settle it, if anyone wants it settled:** a 20-line spike drawing `y = survival, x = seconds` in LWC 4.x. Half a day. Not on the critical path — do it only if we later want the trading-terminal feel badly enough to reopen the question.

Hand-rolled SVG stays where it already is and nowhere else: **KPI sparklines**. Ten lines, no dependency, and it is why the KPI row still renders when the CDN is unreachable. Keep every table, KPI, and sparkline Plotly-free so the page degrades to "useful" rather than "blank" offline.

**What "fix the interaction model" means, concretely:**

```js
const CFG = {displayModeBar:false, responsive:true, doubleClick:false, scrollZoom:false};
// y is locked so a drag can never rescale price. x stays interactive so we can brush it.
yaxis: {fixedrange:true},          // lock the value axis — this is the real complaint
xaxis: {fixedrange:false},         // NOT locked: brushing needs it
dragmode: 'select'                 // drag = select a time window, never box-zoom
```

> **Corrected per factcheck D-W2.** My first draft wrote `dragmode:false` with `xaxis:{fixedrange:true}`, which would have made shift-drag time selection (item 3 below) and the lifetime mini-map brush (§4.1c) impossible — the config contradicted the features two paragraphs later. Lock **y only**. `scrollZoom:false` is written explicitly even though it is already the default, so a future Plotly default change cannot silently re-enable it.

Replacing what we remove:

1. **Range chips** on every time chart header: `1h · 6h · 24h · 7d · 30d · custom`. Our own buttons, our own styling — not Plotly's `rangeselector`, which cannot be themed to match.
2. **Range strip** under the hero chart only: a 60 px overview of the whole recorded history with a draggable, resizable window. Dragging it sets the global range. This is the pan/zoom the Operator actually wants — direct, visible, and it never fights page scroll.
3. **Drag to select a time window** on any time chart (`dragmode:'select'` → `plotly_selected`), with a `reset` chip appearing in the header while a selection is active. Discoverable because the chip is proof something happened. Drag no longer box-zooms and can no longer rescale the y-axis — that is the behaviour the Operator called clunky.
4. **Crosshair**: `hovermode:'x unified'`, `xaxis.showspikes:true`, spike thin (1 px), colour `--border-2`, `spikemode:'across'`, `spikesnap:'cursor'`.
5. **Hover card drawn by us**, not by Plotly (§5.2) — bound to `plotly_hover`/`plotly_unhover`, one shared DOM node for the whole page.
6. **Uniform margins**: `{l:56, r:56, t:8, b:36}`; the panel `<h2>` is the title, Plotly never draws one.

**What we lose by staying:** the inertial pan/scrub feel of a trading terminal, and ~1.1 MB gz on a cold cache (loopback + browser cache makes this a once-per-day cost, not a per-refresh one). If we ever build a genuine order-book replay terminal, Lightweight Charts is the right library for *that view alone*, lazily loaded. The door stays open; we do not walk through it for this.

**Measure before optimising the bundle:** if first paint is slow on the Mac, the `plotly-cartesian` partial bundle covers scatter/scattergl/bar/histogram and drops ~40%. Do not swap it until someone has timed the full bundle on the actual machine.

---

## 3. The trait-filter chart (Traits view, hero panel)

Two modes, chosen by how many **clauses** are selected. A clause is one trait type with one or more values (`Palette: Seafoam|Ivory` is one clause).

### 3.1 Single clause — 3 lines

| series | definition | colour | weight | dash | markers |
|---|---|---|---|---|---|
| **trait highest bid** | per bucket, max of: (a) `item_received_bid` on tokens matching the clause, (b) `trait_offer` whose stored criteria match the clause | `--bid` `#7CC4FF` | 2.4 | solid | yes |
| **trait lowest ask** | per bucket, min `item_listed` on tokens matching the clause | `--ask` `#FF8A65` | 2.4 | solid | yes |
| collection floor | unfiltered `floor_ask` | `rgba(255,138,101,.40)` | 1.4 | **dotted** | no |

Drawn in that z-order reversed — baseline added first so it sits under. The baseline is explicitly *pale and dotted below*, per the Operator.

Optional fourth line, **off by default**, toggle in the panel sub-bar: collection offer as a pale dotted green baseline `rgba(25,201,90,.40)`. He asked for the floor; the bid baseline is sometimes the more useful comparison, so it is available, not assumed.

**Trait offers are the new part.** Per `FACTS_2026-09-09_stream_payloads.md`, `trait_offer` payloads carry `trait_criteria` and `trait_criteria_list` (an AND across traits), and 16 of 39 observed had only the list form. Matching these to the filter replaces the BUG-045 rule that excludes trait offers entirely. **Dependency:** `normalize.py` must persist the criteria (proposed: `trait_offer_criteria(order_hash, trait_type, trait_name, seq)`, plus the numeric form). Until it does, the bid line is labelled in the legend as **"item bids only — trait offers not yet matched (39 seen in 40 min)"**. That is honest degradation; silently omitting them is not.

### 3.2 Two or more clauses — up to 6 ask lines

Everything in this mode is an **ask**. A sub-bar toggle switches the whole set to **bids** (highest bid per set) — never both at once, because 12 lines is not a chart. The panel title states which: *"Trait floors — asks"* / *"— bids"*.

| series | definition | colour | weight | dash | markers |
|---|---|---|---|---|---|
| **combined-trait floor** | lowest ask among tokens matching **all** clauses (the AND set) | `--ask` `#FF8A65` **at full strength**, with a 5 px `--bg` halo underneath | **3.0** | solid | 6 px |
| single-trait floor ×1..4 | lowest ask among tokens matching **that one clause alone** | violet `#C792EA` · cyan `#4DD0E1` · pink `#FF8FB1` · tan `#D6BA73` | 2.0 | solid | 4 px |
| baseline collection floor | unfiltered `floor_ask` | `rgba(255,138,101,.40)` — same hue, 40%, dotted | 1.4 | **dotted** | no |

> **Corrected per factcheck D-W3.** My first draft gave the combined line `#F1F5F2`, which is already `--text` (body copy) *and* which §5.4 simultaneously proposed for `--sale` — one hex, three meanings, while the section's whole point was fixing exactly that mistake elsewhere. Resolved by dropping the unique hue: the combined line **is an ask**, the panel is ask-only, so it takes the ask hue at full strength and is separated from its own baseline by weight (3.0 vs 1.4), opacity (100% vs 40%) and dash (solid vs dotted). Full-strength orange is the only saturated orange on the panel. The halo (draw the trace twice — a wider stroke in `--bg` beneath the coloured stroke) keeps it readable where a single-trait line crosses it.

Legibility rules that make 6 lines work:

- **Weight encodes role**, not identity: 3.0 = the combined set you asked for, 2.0 = a component, 1.4 = a reference.
- **Dash carries exactly one meaning globally: dotted = reference/baseline, never an observation.**
- **The combined set and its baseline share a hue on purpose** — "the same kind of thing, one is the reference" is read instantly from opacity and dash, and it costs no new colour.
- **Hue encodes trait identity here and only here.** This is a deliberate, single, documented exception to the global grammar (§5.4), justified because the legend swatch sits next to the trait's name and no event-role series is on the panel at the same time. The panel sub-title says *"colour = trait; asks only"*.
- **More than 4 clauses:** the 5th and beyond are collapsed into the combined line only, and the panel prints *"4 of 6 clauses drawn individually — deselect to see others"*. Six categorical hues on dark is past the legibility limit; five plus a white plus a baseline is already the ceiling.

**Holes.** Every series on the full bucket grid, `connectgaps:false`. Markers stay **on** below ~120 observed points per series so a single isolated observation is visible at all — with 8 listings in 40 minutes, a markerless line draws literally nothing. Ingestion gaps shade red at 10% across all panes. A bucket where the AND set has no standing ask is `null`, not `0`.

**Legend (custom HTML, above the plot, not Plotly's).** One row per series:

`▬▬  Cloak: Ivory + Print: Unclaimed        n=118 tokens · 14/72 buckets · last 0.8800 Ξ`

The swatch is a 24×3 px preview showing the actual weight and dash. `n` is the matching-token count. `14/72 buckets` is observed/total — the hole count is a first-class number on screen, not buried in the basis line. Clicking a row toggles that series; the toggle persists per view.

**Hover.** One shared dark card (§5.2). Rows sorted by value descending. A series that is null at that x shows **`— no observation`** rather than being dropped: a missing row reads as "the tooltip forgot"; an explicit hole reads as truth. Header = local time + zone + bucket width. Each row: swatch · name · value with unit · `(n=118)` in muted grey.

**Basis line under the chart**, per series: metric · denomination · transform · interval + alignment · window in Central · buckets and holes · trait clause · `wash_filter=raw` · matching token count. Plus the §6-Q1 warning if the immediacy-cost leg mismatch applies.

---

## 4. "How long a bid stands" — redesign

Today: a four-cell table (n, p10, median, p90). It is the richest dataset we have — 41,361 cancels in 40 minutes — rendered as four numbers.

### 4.0 A defect to fix first (tech-lead)

`MetricEngine.bid_lifetimes` (`metrics.py:440–457`) has **two** defects, and they bias in opposite directions.

1. **Right-censoring dropped.** The join keeps only bids that *were cancelled*; a bid still standing at window end never enters the sample, so the longest-lived liquidity is systematically invisible. Biases percentiles **short**. `docs/01 §5.5` names this exact mistake.
2. **The join is unbounded.** There is no `MIN(c.valid_ts)`, no `LIMIT 1`, no first-termination restriction — any `order_hash` with two matching cancel rows yields a cross product, so the reported `n` is a count of *bid×cancel pairs*, not of bids, and the duplicated pairs are the long ones. Biases **long**. (Tech-lead Q-V2; the recorded `n = 38,286` in `docs/logs/BUGS.md:397` is a pair count.)

> **Corrected per factcheck D-W4.** My first draft named only defect 1 and predicted the fixed median would come out **longer**. That prediction is not safe: with both defects live the **net sign is unknown**. Nobody should build this panel expecting a direction. If the corrected median comes out shorter, that is not evidence of a bug in the new code — write that sentence into the PR description before the number is computed, so it cannot be rationalised afterwards.

Also unresolved before this panel is built: **there are three definitions of "ended" in one module** (tech-lead Q-V4) — `cancel_count` terminates on cancel+invalidate, `bid_lifetimes` on cancel only, the live-book `dead` predicate on cancel+invalidate+sold — and `bid_count` counts item bids, collection offers and trait offers while `bid_lifetimes` samples item bids only. The "bid count" and "bid lifetime" panels on the page today are not about the same population. Pick one definition of "ended" and one of "bid", write them into docs/06, and make every panel use them. This is upstream of any visualisation work.

File all of the above as defects **before** this panel is built. A prettier chart over a biased estimator is worse than the ugly table, because it looks trustworthy.

While fixing: an order can leave the book four ways — `item_cancelled`, `order_invalidate`, `item_sold` (filled), or expiry (`expiration_at` passed with no event). These are **competing risks**, not one event. "Cancelled after 4 s" and "filled after 4 s" mean opposite things.

### 4.1 The panel — three coupled pieces on a shared x

**(a) Survival curve**, 240 px. `S(t) = P(bid still standing after t seconds)`, Kaplan–Meier, step line (`line_shape:'hv'` — survival *is* a step function; a smooth line here is a lie). X = seconds, **log-scale toggle, log default** (lifetimes span 1 s to hours). Y = 0–100% still standing.

- **95% band from a maker-episode cluster bootstrap**, 12% opacity fill of the line colour. REQ-F-19 is not optional, but *which* band matters. **Not Greenwood.**
  > **Corrected per factcheck D-W5, resolving toward the quant.** My first draft specified a Greenwood band. Greenwood assumes independent observations; the quant (§3.2) shows that ~42,601 quotes from **3 makers** are nowhere near independent, and estimates the order-level band would be roughly √(n/n_eff) ≈ **15× too narrow**. A band that tight on the front page is false precision — the exact failure the project's fourth rule exists to prevent. Resample **maker-episodes**, not orders.
- Markers with callouts at p10 / median / p90, labelled with the duration format from §5.1.
- Up to **3 curves** overlaid, chosen in the sub-bar: `item bids` / `collection offers` / `trait offers`. This comparison is the actual insight — if a collection offer stands 400× longer than a bot's item bid, that is a statement about who is real.
- Header prints `n = <ended> ended · <censored> still standing (censored) · n_eff = <effective>`. All three, always, computed at render time.
  > **Corrected per factcheck D-W6.** My first draft printed `n = 41,361 ended · 2,904 still standing` as the example. 41,361 was the *cancel* count lifted from FACTS (not the count of bids whose placement we witnessed, which excludes orphaned cancels), and **2,904 was invented**. In a document that mandates "every number carries its basis", a fabricated example number is how a placeholder ships as a value. No literal numbers in this spec — placeholders only.

**(b) Exit-reason histogram**, 140 px, shares the x-axis. Counts per log-spaced lifetime bin, **stacked by exit reason**: cancelled `--bad` red · invalidated `--warn` amber · **filled `#F1F5F2` white** · expired `--faint` grey. Fills are the rare, interesting bar and white makes them impossible to miss. Clicking a bin opens the drill list (4.3).

**(c) Placement-time mini-map**, 60 px, **separate x-axis (wall-clock)**. Bids placed per minute across the range. Drag to brush a placement-time window → (a) and (b) recompute over only bids placed in that window. This is "connected to specific event times". The brushed window is printed as text next to it and clearable.

### 4.2 Small n

Below **n = 30** the panel does not draw a survival curve. It draws a **strip plot** — every observation as one dot on a jittered lifetime axis, coloured by exit reason — and prints `n = 7 — showing every observation; no percentiles (REQ-F-19, min n = 30)`. Seven dots is an honest picture of seven events. A KM curve through seven points is a decoration.

### 4.3 Filters and the drill list

Filters, in the panel's own sub-bar (global trait filter is inherited and shown as chips):

| filter | control |
|---|---|
| trait | inherited from the sidebar; chips shown read-only with a clear button |
| maker | multi-select from the top 20 + "all others"; also set by clicking a row in the Makers panel |
| price band | dual-handle slider over the observed bid-price distribution, `any` default; shows n selected |
| placement time | mini-map brush (4.1c) |
| exit reason | toggles per reason, all on by default |
| order kind | item bid / collection offer / trait offer |

**Drill list** — 480 px slide-over on the right, opened by a histogram bin or a scatter point. Header: `bin 4–8 s · 3,208 bids · 12 makers`. One row per bid, paged 50, sortable:

token # (with thumbnail) · trait chips · price in both denominations (display one bold) with `price_basis` · maker (short, click copies full) · **placed at (valid) / (observed)** · **ended at / still standing** · lifetime · exit reason · **distance to floor at placement** (`bid − floor_ask` at that instant; `—` if no floor was known — never estimated) · `order_hash` short.

Both timestamps ride along deliberately. `observed_at − valid_at` is our stream lag; if a bid's whole life is shorter than our lag, we never had a chance at it, and that is a fact about strategy feasibility, not about the market.

`n` is visible on the header, on every curve, on every filter chip, and on the drill list. Everywhere.

---

## 5. Formats, hover, KPI cards, and the chart grammar

### 5.1 Number and time formats — one table, global

| quantity | format | example | rule |
|---|---|---|---|
| USD price | `$0,000.00` | `$1,284.06` | **always 2 dp. Never compact, never abbreviated, on any KPI or table cell.** If it overflows, reduce font size — do not truncate. (Current `compact()` on the volume KPI violates "cut off numbers at the cent"; remove it) |
| USD aggregate | `$0,000.00` | `$596,142.18` | same rule. Compact form (`$596.1K`) permitted **only** on an axis tick |
| ETH price | `0.0000 Ξ` | `0.8800 Ξ` | 4 dp for a single order's price |
| ETH aggregate | `0.000 Ξ` | `2,459.352 Ξ` | 3 dp for sums |
| count | `0,000` | `41,361` | integer, thousands separators, tabular-nums |
| percent | `+0.0%` | `+12.4% (n=18)` | 1 dp, **always signed**, **always with its count** |
| basis points | `+0 bps` | `+124 bps` | integer |
| multiple | `0.00×` | `3.41×` | for trait premium vs floor |
| duration | adaptive | `840 ms` · `4.2 s` · `3 m 12 s` · `2 h 07 m` · `1 d 04 h` | two significant units, never three |
| timestamp | `Mon DD, HH:MM:SS CT` | `Sep 09, 14:32:07 CT` | zone abbreviation printed **every time** |
| axis tick (time) | `HH:MM` | `14:32` | day-boundary tick promotes to `Sep 09` |
| address | `0x1f3a…9c2b` | mono | `title` = full address; click copies |
| **null / undefined** | `—` in `--faint` | `—` | `title="no observation in this bucket"`. **Never `0`, never blank, never a dash that could be a minus** |
| small-n warning | `⚠ n = 7 < 30` | `--warn` | shown wherever a percentile would otherwise appear |

### 5.2 Hover card — one implementation, every chart

| property | value |
|---|---|
| ownership | **ours**, one shared DOM node, bound to `plotly_hover` / `plotly_unhover`. Plotly's `hoverlabel` disabled |
| background | `#0E1512` @ 98% — darker than `--surface`, so it reads as *above* the card |
| border / radius / shadow | 1 px `#3B5044` · 10 px · `0 12px 32px rgba(0,0,0,.55)` |
| type | `ui-monospace` 12.5 px, `--text`, left-aligned, `font-variant-numeric: tabular-nums` |
| width | max 320 px; series names wrap, **never truncate** |
| header | `Sep 09, 14:32:00 CT · 5 min bucket` in `--text-2` |
| rows | swatch · series name · value with unit · `(n=118)` in `--muted`. Sorted by value descending |
| null rows | rendered as `— no observation`, **not omitted** |
| footer | warnings only (gap overlap, leg mismatch, provisional onboarding) in `--warn` |
| position | 14 px from cursor, flips side within 12 px of the viewport edge, never covers the hovered point |
| events | `pointer-events: none` |

### 5.3 KPI card spec

Five cards on Market: **lowest ask now · collection offer now · immediacy cost now · volume 24 h · sales 24 h.** Immediacy cost is added because REQ-F-13a makes it a first-class Phase-1 series and it is the clearest liquidity number we have.

```
LOWEST ASK NOW                       ← label, 11 px uppercase, --muted
0.8800 Ξ      +3.4% vs 14:00 CT      ← value 26 px; delta chip STATES ITS COMPARISON
▁▂▃▅▄▆█▇▅▃                            ← 44 px sparkline, 24 h hourly, holes broken
first observed 14:00 CT · 18 of 24 hours had a listing
```

Rules:

- **The delta always names what it compares to, in the chip itself.** `+3.4% vs 14:00 CT`, not `+3.4%`. Which baseline that is, is §6-Q2 and is the Operator's to decide.
- If the first bucket in the window is a hole, the delta compares to the **first non-null** bucket and the chip names that hour.
- Fewer than 2 observed buckets → **no delta**. Print `— insufficient observations (n=1)` in `--muted`. Never a `0.0%`.
- Sub-line always carries the observed-bucket count out of the total: `18 of 24 hours had a listing`.
- Sparkline: holes break the path (already correct); ingestion-gap spans get a faint red vertical band; the last observed point gets a 2 px dot.
- Sparklines are hand-rolled SVG and must render with **no chart library present**.

### 5.4 Chart grammar — global, one page

**Hue = event role.** Everywhere except the §3.2 multi-trait overlay, which is the single documented exception.

| role | token | hex |
|---|---|---|
| ask / listing | `--ask` | `#FF8A65` |
| item bid | `--bid` | `#7CC4FF` |
| collection offer | `--coll` | `#19C95A` |
| **trait offer** | `--trait-offer` *(new)* | `#C792EA` |
| sale / fill | `--sale` *(new)* | `#FFFFFF` pure white |
| cancel | `--bad` | `#FF6B6B` |
| invalidate / expire | `--warn` / `--faint` | `#FFB84D` / `#5E6B65` |
| spread / immediacy cost | `--spread` | `#FFD166` |
| ingestion gap | red @ 10% | `rgba(255,107,107,.10)` |

`--sale` is **pure white `#FFFFFF`**, not `#F1F5F2` — because `#F1F5F2` is `--text`, the body copy colour (factcheck D-W3). Three sales in forty minutes is the rarest and most important event on the page; pure white is the correct amount of loud, nothing else is white, and it is now distinct from both body text and the combined-trait line (§3.2).

**Defect to file (PRS) — larger than I first reported.** The tech-lead (D-V3) checked every use, and the collision is not two tokens, it is **one hex carrying a brand accent and four unrelated data roles at once**:

| `#19C95A` used as | where |
|---|---|
| token `--verde-2` (brand accent) | `ui/index.html:11` |
| token `--coll` (collection offer) | `:12` |
| **hardcoded** KPI sparkline stroke | `:180` |
| **hardcoded** collection-offer price line | `:194` |
| **hardcoded** *sales* bar series | `:202` |
| **hardcoded** *event-mix* bars | `:205` |
| UI chrome: links · up-delta · sorted header · `.ok` · filter pills | `:18, :63, :69, :72, :73–74` |

So today a green mark on the page may be a collection offer, a sale, an event-mix bar, a sparkline, or a piece of chrome. The fix stands and must extend to **all four hardcoded literals, not just the two tokens**: verde (`#00B140` / `#19C95A`) becomes **UI chrome only** — active tab, focus ring, primary button, selection, links — and is never a data series; collection offer moves to a distinguishable green (`#2ED573` or similar) as a token; sales become `--sale` white; event-mix bars become `--text-2` neutral, since that panel's bars encode *quantity*, not role. No panel may reference a colour literal: every mark reads from a token (design-lead charter, "the `:root` block is the palette").

**Dash** — exactly one meaning: **dotted = reference / baseline / comparison. Solid = an observation. Dashed = a derived-but-observed series on a secondary axis** (e.g. % of ask). No other dash patterns anywhere.

**Weight** — 2.2 px default · 3.0 px the focused or combined series · 1.4 px baselines.

**Markers** — on when a series has < 120 observed points, off above. Non-negotiable in a book this thin.

**Fill** — never under a price line. Fills exist only for uncertainty bands, at 12% opacity of the line colour.

**Bars** — counts only, never prices. Stacked = mutually exclusive parts of one total. Grouped = independent quantities. Never stack quantities that differ by more than ~50× (see §1.1).

**Axes** — y always titled with its unit; x always in `display.timezone` with the zone printed; zero line drawn only where zero is meaningful (counts, spread, diff), never on a price axis.

**Smoothing** — never in place, ever. A moving average is a `--text-2` grey 1.6 px **dashed** overlay, labelled with its window in the legend (`MA(6 × 5m = 30m)`), off by default, and the control is disabled with a tooltip until ≥ 24 h of data exists.

**One idea per chart.** If a panel needs two sentences to explain, it is two panels.

---

## 6. Three questions for the Operator

Not defaulted. Each changes a number, not a colour.

### Q1 — Under a trait filter, what should `immediacy cost` do?

A trait-filtered *ask* has no trait-filtered *bid* to pair with — the only standing bid those tokens have is the collection-wide offer. (docs/08 §5 records this as open.)

- **(a) Keep computing it** with the collection-wide offer as the bid leg, labelled on the chart. You always get a number; the two legs are different populations, so the number is an upper bound on the true trait spread.
- **(b) Refuse to compute it** under a trait filter. The panel says "undefined — no trait-specific bid observed", and shows instead a "trait bid coverage" line (what fraction of buckets had *any* trait-specific bid).
- **(c) Draw both.** Line 1: trait-ask − collection-offer (always available). Line 2: trait-ask − (trait offer or matching item bid), holed wherever no trait bid exists. Two lines, two legends, the gap between them is itself information.

### Q2 — What does a KPI "% change" compare to?

- **(a) The first observed bucket in the visible window** (today's behaviour). Moves when you change the range control — `+3.4%` means something different at 6 h than at 7 d.
- **(b) Fixed 24 h ago**, regardless of what the range control says. Stable and comparable across sessions; disconnected from what the chart beneath it is showing.
- **(c) Local midnight (DTD)** — the crypto equivalent of "change on the day". Matches how OpenSea and Coinbase quote it; resets at 00:00 Central.

### Q3 — What is a "floor" allowed to be built from?

This one has money attached.

- **(a) Standing asks only.** The lowest price you could *actually pay right now* (listed, not cancelled, not expired). Executable, and frequently absent — with 8 listings in 40 minutes, many buckets will be holes.
- **(b) Lowest ask observed in the interval** (today's `floor_ask`), even if since cancelled. Far fewer holes, but it is a price that *existed*, not one you can hit.
- **(c) Both, as two lines** — executable solid, observed dotted. Twice the lines everywhere floors appear (trait chart, KPI, screener, comparison table), and the divergence between them is a liquidity signal in its own right.

---

## 7. Dependencies this proposal creates (for the tech-lead)

| § | needs |
|---|---|
| 1 | hash routing + persisted global filter state |
| 1.4 | `observed_at − valid_at` percentiles endpoint; unparsed-rows endpoint with reasons |
| 3.1 | **store `trait_offer` criteria** (`trait_criteria`, `trait_criteria_list`, `numeric_trait_criteria_list`) — replaces the BUG-045 exclusion |
| 3.2 | metric variant: lowest ask over an arbitrary token set (AND of clauses, and each clause alone) on the full bucket grid, with matching-token counts |
| 4.0 | **One definition of "ended" and one of "bid"**, written into docs/06 — three and two exist today. Then: bounded first-termination join · Kaplan–Meier with right-censoring · competing risks · `None` (not a flagged number) below `min_n_for_percentiles`. File as defects first; net sign of the correction is unknown |
| 4.1 | maker-episode **cluster bootstrap** band and an `n_eff` in the response — not Greenwood |
| 4.3 | lifetime drill endpoint (bid-level rows with both timestamps, exit reason, distance to floor at placement); maker/price-band/time-window filters |
| 5.3 | KPI response must carry the baseline bucket's timestamp so the chip can name it |
| 5.4 | palette token split so verde ≠ collection-offer green; new `--trait-offer`, `--sale` tokens; **remove all four hardcoded `#19C95A` data literals** (`:180, :194, :202, :205`) — no colour literal in any panel |
| §6 | all three answers change metric semantics, not presentation — no build on the affected panels until answered |
| 8.1 | `/api/ledger` with **keyset (cursor) pagination**, **server-side sort/filter against a column whitelist**, estimated totals flagged `exact:false`; indexes on `(collection,valid_ts)`, `(collection,token_id,valid_ts)`, `(collection,maker,valid_ts)`, `(collection,event_type,valid_ts)`, `(order_hash)`; **generated `token_num INTEGER`** so token # sorts numerically; `order_lives` before the exit-reason column exists |
| 8.2 | wallet profile store (behaviour from the landing zone; holdings/flows from public RPC or block explorer, **never the OpenSea REST budget**); counterparty adjacency JSON in `data/market/`; cluster records with evidence row ids; flag records with evidence row ids. **Fixed key set — no field for a legal name, employer, location, or any off-chain identity, and unknown keys dropped at render** |
| 8.3 | Q4 decides whether any smoothed line exists at all — do not build the selection chart's line style until answered |

---

## 8. Addendum — two views the Operator added (2026-09-09, later)

> (a) *"a full event line item list sortable by the numbered argonaut and chartable in a smooth fashion"*
> (b) a Makers / wallets view for the new **market-analyst** role (`.claude/agents/market-analyst.md`).

**This takes the IA to six views, past the 3–5 I recommended in §1.** I am not quietly absorbing them into existing tabs to preserve my own number. Both earn a view: the ledger is the raw record every other panel is an aggregate of, and the wallet view is a new role's whole output surface. What changes to pay for it: **Flow's Makers panel (§1.3) stops being a panel and becomes a link** into Wallets — the same table in two places is how two definitions of "share of events" get shipped.

Final IA: **Market · Traits · Flow · Ledger · Wallets · Health**.

### 8.0 A reading I am flagging rather than defaulting

The Operator said the ledger should be **"chartable in a smooth fashion."** I read "smooth" as *the interaction is fluid* — select rows, the chart appears immediately, no reload — and **not** as *the line is splined between observations*. Splining raw observations is forbidden by this project's own rules and by my charter ("never draw a spline between observations"). The only smoothing that will exist is the labelled moving-average overlay, off by default, enabled at ≥ 24 h of data. If he meant a visually smoothed price curve, that is Q4 in §8.3 and I have not assumed the answer.

---

### 8.1 Ledger — "show me every event, and chart the ones I pick"

Answers: *what actually happened, to which Argonaut, at what price, by whom, and what did that token's price do?* This is the raw record; every other view is an aggregate of it.

Default **6 h**, newest first. No interval (rows, not buckets).

| panel | span | height | contents |
|---|---|---|---|
| Filter bar | c12 | — | token # (exact or range `4–500`) · event type · maker · taker · price band · time window · trait filter (inherited) · order hash · `has price` toggle |
| **Event table** | c12 | fill | virtualised rows, see below |
| **Selection chart** | c12 | 300 | appears only when a selection exists (§8.1.3) |

#### 8.1.1 Columns

| column | format | sortable | notes |
|---|---|---|---|
| time (valid) | `Sep 09, 14:32:07 CT` | **yes** (default, desc) | `valid_at` — when it was true on the market |
| time (observed) | same | yes | `observed_at`; **off by default**, toggled in the column picker |
| lag | duration | yes | `observed_at − valid_at`; our stream latency for that row |
| type | chip, colour = §5.4 role | yes | |
| **token #** | `#4207` + 26 px thumb | **yes — numeric, not lexical** | `#10` sorts after `#9`. The Operator asked for this by name |
| name | text, escaped | yes | |
| price ETH | `0.0000 Ξ` | yes | nulls last in both directions |
| price USD | `$0,000.00` **to the cent** | yes | never compact |
| basis | `order_value` / `units_x_rate` / `reported_value_unverified` | yes | the §3.1 price rule, visible per row — an unverified row must be findable |
| maker | `0x1f3a…9c2b` mono | yes | click → Wallets; ⌘-click → filter ledger to it |
| taker | same | yes | |
| exit reason | `cancelled` / `invalidated` / `filled` / `expired` / `standing` | yes | **only once `order_lives` exists**; until then the column is absent, not blank |
| order hash | `0x8f…21` mono | no | click copies full |
| traits | chips | no | from `tokens`; `—` where not yet onboarded |

Column picker (which columns, what order) persists per Operator. Density toggle: comfortable 32 px / compact 24 px.

#### 8.1.2 Scale — this is the part that decides the API

~48 events/s ⇒ **~4.1 M rows/day**. Two things in the current code break at that size and must be replaced, not tuned:

1. **`screener()` sorts in Python** (`metrics.py`, deliberate — it is safe against SQL injection). At 4 M rows, sorting in Python means loading 4 M rows into memory per page request. The ledger must sort and filter **server-side, in SQL, against a whitelist of column names** — a whitelist gives the same injection safety without the memory.
2. **Offset pagination is O(offset)** in SQLite. `LIMIT 50 OFFSET 2000000` scans two million rows to throw them away; page 40,000 of a day's events would take minutes.

API required:

```
GET /api/ledger?collection=&traits=&token=&type=&maker=&price_min=&price_max=
               &start=&end=&sort=valid_ts&dir=desc&cursor=<opaque>&limit=200
→ {rows:[…], next_cursor:"…", prev_cursor:"…", total_estimate:4103228, exact:false, basis:{…}}
```

- **Keyset (cursor) pagination**, never offset. The cursor encodes the last row's `(sort_key, rowid)` and the query becomes `WHERE (sort_key, rowid) < (?, ?) ORDER BY sort_key DESC, rowid DESC LIMIT ?` — constant time per page at any depth. `rowid` is the tiebreaker so rows with identical timestamps cannot be skipped or repeated.
- **Sortable columns must be indexed**, or a sort degrades to a full scan of a 4 M-row table. Needed: `(collection, valid_ts)`, `(collection, token_id, valid_ts)`, `(collection, maker, valid_ts)`, `(collection, event_type, valid_ts)`, `(order_hash)`. **A sort on a non-indexed column is refused with a message naming the indexed ones** — not silently slow. Index cost against an append-only writer at 48 rows/s is the data-engineer's call; if five indexes is too many, the sortable set shrinks and the UI shows only what it can actually do.
- **`token_id` must sort numerically.** It is TEXT in the store. Either add a generated `token_num INTEGER` column with its own index, or `CAST(token_id AS INTEGER)` — which cannot use a plain index. Prefer the generated column. If it is not added, the Operator's stated request is not met.
- **`total_estimate` is an estimate and says so** (`exact:false`). An exact `COUNT(*)` over a filtered 4 M-row table on every keystroke is the other way to make this feel broken. Exact counts only under a filter narrow enough to be cheap, and the flag says which you got.
- **Frontend virtualisation**: render ~60 DOM rows regardless of result size; fetch the next page at 80% scroll. 4 M `<tr>` elements is not a table, it is a hang.

#### 8.1.3 "Chart this selection"

Three ways in, one chart out:

| gesture | charts |
|---|---|
| click a **token #** anywhere | that token's full price history |
| shift-click a row range, or check rows | exactly those rows |
| **"chart all matching"** button in the filter bar | the whole current filter, server-aggregated, capped and stated |

The chart (300 px, appears below the table, dismissable):

- **One series per event type**, in role colours: listings orange, item bids blue, collection offers green, trait offers violet, sales white.
- **Markers always on.** A token has a handful of events; a markerless line draws nothing. Line segments connect points **within one event type only** — never across types. A listing and a bid are not two readings of one quantity.
- **Holes preserved.** No bridging across an ingestion gap; gap spans shaded red as everywhere else. With one event, one dot and the note `n = 1 — one observation, no line`.
- **Sales get a white marker at 9 px** with a leader line to the price axis. Three sales in forty minutes; when one appears it should be unmissable.
- **Moving-average overlay** in the header, disabled with a tooltip until ≥ 24 h exists, labelled with its window (`MA(6 × 5m = 30m)`), grey dashed, and **never replacing the raw series** (§5.4).
- Basis line: which rows, which filter, how many events of each type, how many holes, `wash_filter=raw`.
- Cap: 5,000 rows charted. Above that the button says `charting the 5,000 most recent of 41,208 — narrow the filter`. Never a silent sample.

---

### 8.2 Wallets — "who am I trading against?"

The market-analyst's output surface. **The unit of analysis is an address.** Per the charter, this view is address-level only: behaviour, holdings, flows, counterparties, clusters, and self-attached public handles. It is not about people.

Default **24 h**, ranked by event count. Filterable: min events · min share · has holdings · has flags · cluster · first-funded-by · time window.

| panel | span | height | contents |
|---|---|---|---|
| Wallet list | c4 | 620 | ranked, searchable; selection drives the two panels to its right |
| **Address card** | c8 | 620 | the selected address, §8.2.1 |
| **Counterparty adjacency** | c7 | 420 | §8.2.2 |
| Ladder shape | c5 | 420 | this address's bid prices vs the floor over time — the quote curve, one point per bid, floor as an orange step line |

#### 8.2.1 Address card

```
0x0d9e…4c11    ⧉                      [ ledger → ]  [ profile.md → ]
ENS  argobot.eth        OpenSea  @argobot        ← self-attached, public, or "none"

BEHAVIOUR (24 h)          HOLDINGS              FLOW
events    38,204          holds     41 tokens   first funded by  0x77b2…9e03
  share   61.4% of 62,218   of 9,212 (0.45%)      2026-04-11, 2.00 Ξ
bids      19,180          acquired  12 in 30 d  top counterparties
cancels   19,004          disposed   3 in 30 d    0x77b2…9e03   88 trades
listings       8                                  0x3ca9…1d40   31 trades
sales          3
median bid life  4.2 s  (n = 19,004; n_eff = 61)
fill ratio  0.016%  (3 of 19,180 bids)

CLUSTER   C-02 · 4 addresses · 3 pieces of evidence  [ show evidence ]
FLAGS     ⚑ quote-refresh episodes            1,412 rows  [ show ]
          ⚑ bids cancelled within 10 s of any approach  312 rows  [ show ]
```

Rules the card enforces:

- **Every percentage carries its count**, in the card, not in a tooltip: `61.4% of 62,218`, `0.016% (3 of 19,180)`.
- **Every median carries `n` and `n_eff`.** One address's 19,004 cancels are not 19,004 independent observations; the effective count is what the uncertainty is computed from (§4.1).
- **A cluster is a hypothesis with evidence attached** — the evidence count is on the card and `show evidence` lists the rows. The word "cluster" never appears without it. Never "these are the same person".
- **Every flag carries its rows.** `show` opens the Ledger filtered to exactly the rows that fired the flag. A flag whose rows cannot be listed does not render.
- **Holdings and flows are chain data** (public RPC / block explorer), never OpenSea REST — they do not touch the 120/hour budget. The card shows the source and the as-of time of the chain read.
- **The handle line is self-attached only** and labelled as such: an ENS name or OpenSea username the owner chose to put on the address. It is never a search result.

**The charter boundary, enforced by the schema rather than by discipline:** *the wallet profile object has no field for a legal name, employer, company, location, or any off-chain identity.* There is nowhere in the record to put one, so nothing can leak into the UI through a "notes" field. The card renders a fixed key set; any unknown key is dropped, not displayed. If a reviewer wants to check the boundary holds, they check the schema, not the rendering code.

#### 8.2.2 Counterparty graph: adjacency heat-map, not a force graph

**Choose the adjacency heat-map.** Three reasons, in order:

1. **No new dependency.** It is a Plotly `heatmap` trace. A force graph needs a physics simulation (d3-force or similar) — a second library, which §2.3 just spent a section declining to take on for better reasons than this.
2. **It is deterministic.** The same data draws the same picture every time. Force layouts are seeded and settle differently per run, so two screenshots of the same wallet pair look different — unusable in a document that has to be reproducible, and quietly misleading when someone reads meaning into a position that is an artifact of the seed.
3. **It reads counts exactly.** A wash-trade signature is "A→B→A, 88 times, at round prices". A heat-map cell states 88. A force graph states "these two are near each other", which is the thing that has to be converted back into a number anyway.

Spec: rows = source address, columns = destination, cell = count of trades `A→B`. Ordered by cluster, then by volume within cluster, so cluster membership appears as a visible block on the diagonal. **Sequential single-hue scale** (counts have no meaningful midpoint, so never diverging), `--faint` for zero. Hover: both addresses in full, trade count, total value in both denominations, first and last trade time. Click a cell → Ledger filtered to those two addresses. Cap at 40×40; above that, top-40 by volume with `showing 40 of 214 addresses` printed.

A force graph becomes worth reconsidering only if the analyst needs to read *topology* — chains of funding three or four hops deep — which the heat-map genuinely cannot show. That is a phase-2 question and it costs a dependency.

#### 8.2.3 Linking

- Any maker/taker address anywhere on the page (ledger, tape, book, makers) → **click opens Wallets on that address**; ⌘-click filters the current view to it instead.
- Address card `ledger →` → **Ledger filtered to that address**, time window preserved.
- Heat-map cell → Ledger filtered to that address **pair**.
- Flag `show` → Ledger filtered to the flag's evidence rows.
- Wallet selection is global state like the trait filter: it survives a tab switch and is shown as a dismissable chip.

Every link is one-way into the Ledger, and the Ledger always shows what filtered it. The evidence for any claim on the wallet card is two clicks from the claim — which is REQ-F-40's requirement applied early, and the reason the ledger is a view rather than a panel.

---

### 8.3 A fourth question for the Operator

Carrying §6's three, undefaulted:

**Q4 — "chartable in a smooth fashion": what does smooth mean?**

- **(a) Fluid interaction** — select rows or a token and the chart appears instantly, no reload, no spinner. The line stays straight between observations with markers on every point. *(This is my reading, and it is the only one that does not conflict with the project's rules.)*
- **(b) A smoothed curve you can switch on** — raw series stays as-is, plus a labelled moving-average overlay in grey dashed, off by default, available once ≥ 24 h of data exists. Both (a) and (b) can be true.
- **(c) A visually smoothed price line** — a spline through the observations, replacing the straight segments. **I will not build this without you saying so explicitly**, because a spline invents prices between two real observations and every rule in this project says the chart may not do that. If what you want is "it looks jagged and ugly with 8 points", the fix is markers, better spacing, and a longer interval — not a curve through invented values.
