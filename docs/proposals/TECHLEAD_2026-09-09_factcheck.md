# TECH LEAD — Fact-check of the three 2026-09-09 proposals

*tech-lead (L3), 2026-09-09. Reviews `DESIGN_2026-09-09_views.md`, `QUANT_2026-09-09_metrics_of_value.md`, `DATAENG_2026-09-09_streams_not_rest.md` against `FACTS_2026-09-09_stream_payloads.md`, `docs/01`, `docs/06`, `docs/07`, `docs/00`, `docs/logs/BUGS.md`, and the code.*

**Verdict: none of the three is blocked. All three contain at least one claim that is wrong or unsourced in a way that would change a build decision.** Corrections below. Nothing here relitigates the Operator's decisions of today; §2 resolves conflicts *toward* those decisions.

**Standing caveat that colours everything below.** In this working copy `data/analytics.sqlite` has **0 rows** in `events`, `tokens`, `traits`, `watermarks`, `unparsed`; `data/ops.db` has an empty `gap_register` and `rest_ledger`; there is **no `data/landing/`**. Every "VERIFIED" in this document means *verified by reading code or a repo document*, never by running a query against real data. The data lives on the Operator's machine. Where a claim can only be settled by a query, I say so and give the query.

---

## 1. Proposal-by-proposal

### 1A. QUANT — `QUANT_2026-09-09_metrics_of_value.md`

#### VERIFIED

**Q-V1 — Every line number the quant cites in §6 is correct.** I checked all eight. `bid_lifetimes` 446–457, `cancel_count` 61–62, `pct()` 454–455, `immediacy_cost` aggregation 278–293 and derivation 321–328, `dead` at `metrics.py:394–397` and `metrics.py:478–480`, `apply_transform` 186, the BUG-044 comment at 268, `valid_ts` nullable at `normalize.py:56`. This is the first proposal in this project whose citations all survive checking; it is worth saying so once.

**Q-V2 — T1 is real. `bid_lifetimes` has an unbounded join.** `metrics.py:447–451`:

```sql
FROM events b JOIN events c INDEXED BY ix_events_lifecycle ON c.order_hash = b.order_hash
WHERE ... c.event_type = 'item_cancelled' AND c.valid_ts >= b.valid_ts
```

There is no `MIN(c.valid_ts)`, no `LIMIT 1`, and no first-termination restriction. Any `order_hash` with two matching cancel rows produces a cross product. `n = 38,286` (`docs/logs/BUGS.md:397`) is therefore a count of *bid×cancel pairs*, not of bids. The quant's verification query is the right one and I endorse it verbatim.

**Q-V3 — T1's secondary point is worse than stated, and I rate it blocking for any PR that touches this panel.** `metrics.py:454–457` returns percentiles at any `n` and hands the caller a `percentiles_reliable` flag. `ui/index.html:215` renders `p10 / median / p90` unconditionally and appends the warning as a string. `docs/00:199` (REQ-F-19) says the system "SHALL make that impossible". It does not. Return `None` below `min_n_for_percentiles`.

**Q-V4 — T1's "two panels disagree about what ends an order" is correct, and there is a third disagreement.** `cancel_count` counts `('item_cancelled','order_invalidate')` (`metrics.py:61`); `bid_lifetimes` terminates on `item_cancelled` only (`metrics.py:450`); the `dead` predicate terminates on `('item_cancelled','order_invalidate','item_sold')` (`metrics.py:396`, `479`). **Three definitions of "ended" in one module.** Additionally `bid_count` counts `item_received_bid`, `collection_offer` and `trait_offer` (`metrics.py:59`) while `bid_lifetimes` samples only `item_received_bid` (`metrics.py:449`) — so the page's "bid count" and "bid lifetime" panels are not about the same population either.

**Q-V5 — T3(a): `order_revalidate` is ignored. VERIFIED.** `metrics.py:396` and `:479` list three terminators; `order_revalidate` is parsed and stored (`normalize.py:111`, in `MARKET_EVENTS`) and appears nowhere in `metrics.py`. An invalidated-then-revalidated order is dead forever in the live book and the screener.

**Q-V6 — T3(b): `quantity` is ignored. VERIFIED.** The column exists (`normalize.py:71` schema, `:262` parse, `:272` COLS) and is read by nothing in `metrics.py`. A collection offer good for 5 counts as one unit of depth.

**Q-V7 — T3(c): a NULL `valid_ts` terminator leaves an order standing forever. VERIFIED by construction.** `valid_ts` is nullable (`normalize.py:56`) and is `NULL` whenever `event_timestamp` and `sent_at` are both absent or unparseable (`normalize.py:239, 247`). `d.valid_ts >= e.valid_ts` is `NULL` — not true — so the terminator is not seen and the order never dies.

  **The same NULL has the opposite effect one function away.** In `bid_lifetimes` (`metrics.py:450`) the same comparison silently *drops* the pair. And in `_bucketed` (`metrics.py:255`) `e.valid_ts >= ?` drops NULL-`valid_ts` rows from every charted series entirely. One nullable column, three different silent behaviours. Whatever the frequency turns out to be, this needs one rule, applied once, and a count on the Health view.

**Q-V8 — T2's direction of bias. VERIFIED, and it is worse than a bias — it is a requirement violation.** `metrics.py:278–293` aggregates `MIN` over the ask leg and `MAX` over the bid leg *within a bucket*, then `metrics.py:325` subtracts them. The two legs need not have coexisted. `docs/01 §3.2` defines `immediacy_cost` as "what you pay to force time-to-clear to zero by **hitting the standing collection offer**" — a *standing-book* quantity, not an interval extremum. So this is not only "biased narrow, worse at wider intervals" (true); it is **not the metric REQ-F-13a cites**. It is charted on the front page (`ui/index.html:196`), and it can render negative, which the KPI would display as a free arbitrage. Elevate T2 from a statistical note to a requirement-traceability defect.

**Q-V9 — the left-truncation argument in §0 is sound and the sign is right.** Our observed standing-ask set is a subset of the true one, so `min` over it is ≥ the true floor (**over**estimate); `max` over the observed bid subset is ≤ the true best bid (**under**estimate); the spread is biased **wide**. There is no counter-effect: a termination for an order we never saw placed is an orphan and never entered the set. The "801 standing listings" figure is sourced — `docs/06:249`, from an OpenSea screenshot of 2026-09-08 ("9,210 items … 801 listed (8.7%)"), one day before recording started.

**Q-V10 — §5.3's "already built" claim. VERIFIED.** `traits.py:9` (47 governed reads at `limit=200`), `:119` (`opensea_fallback_budget: int = 50`), `:145` (`params = {"limit": 200}`), `:191–194` (`_fetch_json` refuses any OpenSea host). 9,212 ÷ 200 = 46.1 → 47. ✓

**Q-V11 — §5.4/§5.5's ~11 reads/hour ≈ 9% of budget.** 11/120 = 9.2%. ✓ Budget is 120/h, not 600 — `config/base.yaml:16–29`, `governor.py:5–6, 95`.

**Q-V12 — §3's estimator algebra.** Kaplan–Meier, Greenwood, the log-log band, Aalen–Johansen (`Σ_c F̂_c = 1 − Ŝ`), and the RMST rectangle sum are all stated correctly. The `1 − KM per cause overstates incidence` point is right and matches `docs/01 §5.5`. The competing-risks-not-KM rule for *listings* (informative withdrawal) is `docs/01 §5.5` almost verbatim.

**Q-V13 — the min-of-n argument in metric 5 is correct.** `E[min of n] = ∫(1−F)ⁿ` is strictly decreasing in `n`; comparing trait floors across differently-sized trait groups measures `n`. This is the most important single point in the quant document and I want it on the record separately from the rest.

#### WRONG

**Q-W1 — §0's "88% of events from 3 makers" is not measured over the window the sentence implies.** The measured figure is `docs/07 §2.3`: "the top 3 placed **88%** of events (**644 of 728** in the first file)" — 728 events in one landing file, not 85,775 over 40 minutes. **Correction:** state it as "88% of 728 events in the first file (docs/07 §2.3)". The 3-maker count likewise comes from "10 distinct makers" in that same 152-second sample. This is exactly the error class this project keeps shipping (a real measurement quoted at a scope it does not have), and it appears in the sentence the quant builds `n_eff` on.

**Q-W2 — §0's symmetric framing of the truncation bias is wrong on the bid side.** The quant's own metric 12 has it right: "`orphan_rate` decays … fast for 9-second bot bids, slow for listings that live for weeks." With a measured median bid life of 9 s (`docs/logs/BUGS.md:397`), the item-bid book turns over completely within minutes of connecting; the bid-side left-truncation is essentially gone by 10:25. The ask side, with 801 pre-existing listings, persists indefinitely. **Correction: the spread bias is almost entirely an ask-side bias.** This matters for §5's ranking — it strengthens §5.4 (listings snapshot, 9 reads) and weakens §5.5 (collection offers, 2 reads) as a *bias correction*, though §5.5 is still worth its 2 reads for depth completeness.

**Q-W3 — §5.2's "~7,000 sales ⇒ ~35 pages" substitutes the floor for the mean sale price.** `$9.1M lifetime volume` and `$1,324.15` both come from `docs/06:249`, where `$1,324.15` is the **floor**, not the average sale. Mean sale price ≥ floor in any collection with trait dispersion, so 7,000 is an upper bound on the sale count and 35 pages is an upper bound on the cost. Direction is favourable, but it is an undeclared assumption in a document that declares everything else. Label it.

**Q-W4 — §1 metric 1's "the current `immediacy_cost` is not it" understates the problem.** See Q-V8: it is not a bias, it is the wrong quantity relative to `docs/01 §3.2`. The remediation is not "report the time-weighted median of the current thing" — it is to compute the spread from a standing book (`order_lives`), which the current schema cannot do until T3 is fixed.

#### UNVERIFIABLE FROM THE REPO

| Claim | What would settle it |
|---|---|
| T1's actual inflation factor | `SELECT COUNT(*) FROM (SELECT order_hash FROM events WHERE event_type IN ('item_cancelled','order_invalidate') GROUP BY 1 HAVING COUNT(*)>1)` and `n` vs `COUNT(DISTINCT b.order_hash)`, on the Operator's store. Both queries as the quant wrote them. |
| T3(c)'s frequency | `SELECT event_type, COUNT(*) FROM events WHERE valid_ts IS NULL GROUP BY 1;` and `SELECT COUNT(*) FROM events WHERE event_type='order_revalidate';` |
| T2's magnitude | Count buckets with `immediacy_cost < 0` at `1h` and `1d`; compare `5m` aggregated up to `1d` against `1d` computed directly. |
| "median bid life 9 s" as a market property | It is a measured number (`docs/logs/BUGS.md:397`: median 9 s, p10 3.2 s, p90 234 s, n = 38,286) from the **defective** estimator, so it is biased short by censoring *and* long by the T1 join, net sign unknown — which is precisely what the quant says in §3.4. Correct as stated; not correct if anyone quotes 9 s as a fact. |
| `n_eff ≈ 190` price episodes (§2.3) | An illustration, not a measurement. Presented in a code block that reads like output. Recompute or mark it. |
| Extrapolated hourly rates (§1) | Explicitly declared an assumption from one 40-minute window. Accepted as declared. |
| Every §5 endpoint cost model | The quant explicitly asks the tech-lead to verify page limits against the API reference. I could not: see D-W1. |

---

### 1B. DESIGN — `DESIGN_2026-09-09_views.md`

#### VERIFIED

**D-V1 — §4.0: `bid_lifetimes` drops right-censoring. VERIFIED.** `metrics.py:447–451` is an inner join to `item_cancelled`; a bid still standing at window end has no matching row and never enters the sample. The percentiles are biased short. `docs/01 §5.5` ("Right-censoring must be handled explicitly. A naive average of 'days to sale' over sold listings only is one of the most misleading numbers it is possible to compute here") applies directly.

**D-V2 — §4: "today it is a four-cell table". VERIFIED.** `ui/index.html:214–215` renders exactly `n · p10 · median · p90` from `/api/lifetimes` (`dashboard.py:172–175, 279`).

**D-V3 — the palette collision is real, and it is broader than the design lead reports.** `ui/index.html:11` defines `--verde-2:#19C95A`; `:12` defines `--coll:#19C95A`. Same hex. But `#19C95A` is hardcoded as a *data* colour in four more places: the KPI sparkline (`:180`), the collection-offer price line (`:194`), the **sales** bar series (`:202`), and the **event-mix** bars (`:205`) — while simultaneously carrying UI chrome duty for links (`:18`), the up-delta (`:63`), sorted headers (`:69`), `.ok` (`:72`) and filter pills (`:73–74`). **One hex, one brand accent, and four unrelated data roles.** The fix the design lead proposes is right and must extend to the four hardcoded literals, not just the two tokens.

**D-V4 — the interaction complaint is legitimate in substance.** Charts are drawn with the default `dragmode` (box zoom, which does rescale the y-axis on drag) and default `doubleClick:'reset+autosize'`, with no `fixedrange`. The Operator's decision (toolbar goes) is directly implementable as `displayModeBar:false`.

**D-V5 — §7's dependency table is accurate.** Every dependency it names is genuinely absent from the code: no hash routing, no `observed_at − valid_at` endpoint (`dashboard.py:272–284`), no criteria storage (`normalize.py` never reads `trait_criteria`), no censoring-aware estimator, no drill endpoint, no baseline timestamp on the KPI response.

#### WRONG

**D-W1 — §0/§2.3: "the page ships Plotly's default interaction model … scroll-wheel zoom that hijacks page scroll, a modebar of eleven buttons".** Both specifics are wrong. `ui/index.html:161`:

```js
const CFG={displaylogo:false,responsive:true,modeBarButtonsToRemove:['lasso2d','select2d']};
```

The modebar is already customised (logo hidden, two buttons removed) — **seven** buttons on a cartesian plot, not eleven. And `scrollZoom` is unset, which for cartesian plots means Plotly's default of **false**: wheel-zoom is not enabled and does not hijack page scroll. What *is* default and *is* annoying: drag-to-box-zoom with y-rescale, double-click reset, and the seven-button bar. **The conclusion (fix the config, keep Plotly) survives; two of the four named symptoms do not exist.** This matters because a config change justified by a symptom that isn't there is how a "fix" gets shipped that changes nothing the Operator complained about.

**D-W2 — §2.3's own config block contradicts §2.3's items 2 and 3.** `dragmode:false` with `xaxis:{fixedrange:true}` makes shift-drag time selection (item 3) and the mini-map brush (§4.1c) impossible. These need `dragmode:'select'` with `fixedrange` on **y only**. Ship the y-lock and toolbar removal; do not ship `xaxis.fixedrange:true` if brushing is wanted.

**D-W3 — §5.4 introduces a second palette collision while fixing the first.** It proposes `--sale: #F1F5F2`. `ui/index.html:10` already defines `--text:#F1F5F2` — the body text colour. §3.2 then also assigns `#F1F5F2` to the combined-trait floor line. So the proposal gives one hex to body text, the sale/fill role, *and* the combined-trait series. Pick three.

**D-W4 — §4.0 diagnoses only half the defect.** Right-censoring is dropped (D-V1, correct) **and** the join is unbounded (Q-V2), which biases the opposite way. The design lead's stated expectation — "the fix … changes the median [longer]" — is not safe: with both defects live the net sign is unknown (quant §3.4 says exactly this). If the panel is rebuilt on a "we expect it to get longer" prior and it gets shorter, that will be read as a bug in the new code when it is the old one.

**D-W5 — §4.1 conflicts with the quant on the confidence band, and the quant is right.** Design specifies "95% confidence band (Greenwood)". Quant §3.2 shows Greenwood assumes independent observations and that 42,601 quotes from 3 makers are not; it prescribes a maker-episode cluster bootstrap and estimates the order-level band would be ~√(n/n_eff) ≈ 15× too narrow. **A Greenwood band on this data is a false precision shipped to the front page.** Resolve toward the quant (§2 below).

**D-W6 — §4.1's header example prints numbers that were never measured.** `n = 41,361 ended · 2,904 still standing (censored)` — 41,361 is the *cancel* count from FACTS (not the count of bids whose placement we witnessed, which excludes orphans), and 2,904 is invented. In a document whose own §5.1 mandates "every number carries its basis", a fabricated example number in a spec is how a placeholder ships as a value.

#### UNVERIFIABLE FROM THE REPO

**D-U1 — the Lightweight Charts disqualification ("the x-axis is a time scale, full stop"; "no scatter series"; "no horizontal bars").** These are claims about a third-party library that is not in this repo and cannot be reached from this container. As far as I know them they are substantively correct for LWC 4.x — the horizontal scale is a time/logical scale and there is no value-versus-value series type — but I did not verify them and **the entire library recommendation turns on this one row**. **What settles it: a 20-line spike drawing `y = survival, x = seconds` in LWC 4.x.** Half a day, and it decides a decision the design lead frames as irreversible-ish. Given the Operator has already asked for more views (not for a chart-library migration), I endorse "keep Plotly" *on effort grounds alone* — zero migration versus rewriting every chart plus carrying a second library — which does not depend on the disputed row at all. **Recommend: adopt the recommendation, drop the load-bearing unverified justification, keep the effort argument.**

**D-U2 — bundle sizes (3.6 MB / 1.1 MB gz; 46 KB gz), and "first paint is slow on the Mac".** Not measurable from here. The design lead already says "measure before optimising"; agreed, and nothing should be built on the numbers until then.

**D-U3 — §1.1's "at 5 m buckets the real data is ~14,400 bids against 2 listings".** Plausible from the FACTS rates (63.9k/h ÷ 12 = 5,325 per 5 min, not 14,400 — 14,400 would be a 13.5-minute bucket). The *argument* (four orders of magnitude, split the panel) is unaffected and correct. Fix the number.

---

### 1C. DATA ENGINEER — `DATAENG_2026-09-09_streams_not_rest.md`

#### VERIFIED

**E-V1 — "no history replay; the `phx_join` payload has nowhere to put a start point". VERIFIED.** `stream.py:12` and `:265` both send `{"topic": t, "event": "phx_join", "payload": {}, "ref": ref}`. Empty payload.

**E-V2 — the reconnect budget table. VERIFIED line by line.** `stream.py:773` `ping_interval=20, ping_timeout=20` (→ up to 40 s to detect a half-open socket); `:194` `join_timeout=15.0`; `:195` `stable_seconds=60.0`; `:190` `max_backoff=60.0`; `:652` `backoff = 1.0`; `:702` `delay = min(max_backoff, backoff) * (0.5 + random())` → 0.5–1.5 s first attempt, 30–90 s at cap; `:705` doubling; `:700–701` reset only after `stable_seconds` of uptime. Every figure in the table traces to a constant.

**E-V3 — "no `data/landing/` in this working copy". VERIFIED.** `find` returns no `_manifest`, no `.zst`, no `landing/`. `data/` holds `analytics.sqlite` (0 rows) and `ops.db` (empty `gap_register`, empty `rest_ledger`). The data engineer volunteered this provenance limit unprompted; it is the single most useful sentence in the three documents and it is correct.

**E-V4 — the one-writer argument. VERIFIED.** `cli.py:57–77, 205` takes `fcntl.flock(LOCK_EX|LOCK_NB)` on `<landing root>/.ingest.lock`, so a second process on the same root is refused. `landing.py:207` records the measured consequence of the manifest race: "295 of 600 gap records lost, with `verify_manifest` reporting clean" (BUG-006/007). Separate roots give one writer per store. `docs/07 §1.1`'s single-writer rule is about the *analytical* store's read-write connection and is untouched by a second landing root. Correct on all three points.

**E-V5 — "`traits.py` fetches metadata off-OpenSea today, unmetered". VERIFIED.** `traits.py:51` `is_opensea_host`, `:191–194` `_fetch_json` refuses any OpenSea host and routes to the governed fallback instead. The precedent for the on-chain path is genuinely already shipped and reviewed.

**E-V6 — "50% of this market is stream-only at any price". VERIFIED against REQ-D-09a** (`docs/00:115`): cancellations and order invalidate/revalidate are permanently unrecoverable. 41,361 + 1,529 + 1 = 42,891 of 85,775 = 50.0%. ✓ Note REQ-D-09a explicitly rules that `item_received_bid` **is** recoverable as an `offer` — the data engineer's table gets this right where it is easy to get wrong.

**E-V7 — passive-coverage arithmetic. VERIFIED.** 3,170/9,212 = 34.4%. `1 − e^(−λ) = 0.344` → λ = 0.4216; λ × 9,212 = 3,884 effective draws; 3,884/85,775 = 0.045 draws per event; 1 h → 46.8%, 2 h → 71.7%, 6 h → 97.7%. Every figure checks. The refusal to publish a single 24-hour coverage number when two defensible models differ by 2.5× is the correct call and I am endorsing it explicitly so nobody "simplifies" it later.

**E-V8 — the `criteria_n > 0` guard and its failure mode. VERIFIED as a genuine trap.** A `trait_offer` with no `order_criteria` rows makes the `NOT EXISTS` in the COVERS predicate **vacuously true**, so it would match every filter and every token, silently, in the direction that manufactures an edge. This is a correct reading of SQL semantics and it is the single highest-value sentence in the document. **I am making the named property test a blocking condition on that PR: a `trait_offer` with `criteria_n = 0` matches nothing.**

**E-V9 — the COVERS direction (`S(F) ⊆ S(C)`, not the reverse) is correct**, and the observation that it makes `collection_offer` fall out as `C = ∅` rather than needing the special branch currently at `metrics.py:269–272` is correct and is a real simplification.

**E-V10 — "`trait_offer` criteria are parsed by nothing and stored nowhere". VERIFIED.** `normalize.py:241–266` builds the row dict; `trait_criteria`, `trait_criteria_list` and `numeric_trait_criteria_list` appear nowhere in `normalize.py`, and there is no criteria table in `SCHEMA` (`normalize.py:48–106`).

**E-V11 — the additive-migration precedent. VERIFIED.** `normalize.py:276–286` (`_migrate`) already does `PRAGMA table_info` → `ALTER TABLE ADD COLUMN` → backfill from an existing column, with the docstring reasoning the proposal cites. And `normalize.py:361` inserts events `INSERT OR IGNORE` on `PRIMARY KEY (run, seq)`, so a re-fold genuinely cannot duplicate events. Both supporting claims hold.

**E-V12 — the backfill arithmetic, given its inputs.** Backfillable/h = 63,902 + 252 + 58.5 + 12 + 4.5 + 4.5 = **64,233.5**; ÷200 = 321.2 → **322**. ✓ 322/120 = **2.68 h of budget per hour of gap**. ✓ 12 h: 64,233.5 × 12 ÷ 200 = **3,854**; ÷120 = **32.1 h**. ✓ Without item bids: 331.5 ÷ 200 = 1.66 → **2**; ratio 322/2 = 161 ≈ the claimed "160×". ✓ The internal arithmetic is clean. Two input caveats follow (E-W1, E-W2).

**E-V13 — the key-expiry correlated-failure point. VERIFIED against REQ-D-06** (`docs/00:106`, free-tier instant keys expire after 7 days). Two processes on one key fail simultaneously on a known schedule. The two-keys-staggered mitigation is right and is the kind of failure mode that only shows up if someone thinks about it in advance.

**E-V14 — disk figures. VERIFIED against `docs/07 §2.3`**, which measures 160 stored bytes/event, 47.6 events/s, and 660 MB/day for one connection. 660 × 2 = 1.32 GB/day; × 365 = 482 GB/yr. ✓ Arithmetic and source both check.

#### WRONG

**E-W1 — the document uses two different measured event rates in different sections without saying so, and the mismatch runs 33%.** `docs/07 §2.3` measures **47.6 events/s** (7,240 events over 152 s of steady state, connect burst excluded). `FACTS` measures **85,775 frames over 40 minutes = 35.7 events/s**. Both are legitimate measurements of different windows; `docs/07 §2.3` itself warns "it will not hold uniformly".

The proposal uses the **40-minute** rate for §3.b's backfill arithmetic and the **152-second** rate for §3.a's disk figure and §3.c's event-loss figure. That is not an error of arithmetic, but the two headline numbers are **not mutually consistent**, and the mismatch runs in the unhelpful direction on both:

- **Backfill cost is quoted at the lower rate.** At 47.6/s with the same ~50% backfillable share, one hour of gap is ~**429** reads, not 322 — 3.6 h of budget, not 2.7.
- **Disk cost is quoted at the higher rate.** At 35.7/s, one connection is ~493 MB/day and two are ~987 MB/day ≈ **360 GB/yr**, not 482.
- **Event-loss per silent drop is quoted at the higher rate.** 21–45 s × 35.7/s = **750–1,607** events, not "~1,900".

**Correction: state the range (322–429 reads/gap-hour; 360–482 GB/yr) with both windows named, or pick one window and use it everywhere.** No conclusion changes; the honesty of the numbers does, and this document is otherwise scrupulous about exactly this.

**E-W2 — the "2 reads per hour of gap" figure is a best case that assumes one request can span multiple event types.** If the events endpoint requires a separate cursor walk per `event_type`, the floor for the five recommended classes is `ceil(252/200) + 1 + 1 + 1 + 1 = 6` reads per gap-hour, not 2. Immaterial to the recommendation (6 is still trivial); material to anyone sizing a backfill worker off the table.

**E-W3 — the dedup key is broken by `normalize.py:239`, and it fails in the direction the document itself calls S0.**

```python
valid_at = p.get("event_timestamp") or outer.get("sent_at")
```

The key's comment asserts `valid_at` is "event_timestamp, server-assigned, **identical on both connections**". That is true only when `event_timestamp` is present. When it is absent, `valid_at` falls back to the Phoenix envelope's `sent_at`, which is a per-message push timestamp and has no guarantee of being identical across two independent sockets. **If it differs, the key differs, the duplicate survives, and every count doubles** — failure mode 3 in the proposal's own list, the one it flags as S0 and "exactly the shape the fifth project rule warns about".

**Correction:** key on `event_timestamp` explicitly, never on the coalesced `valid_at`; rows lacking `event_timestamp` are marked **un-dedupable** and counted, not merged. That count belongs on the Health view next to `redundancy_disagreement`.

**E-W4 — `events_dedup` as a view collides with the index hints that fixed BUG-20260909-040.** The proposal says "every metric query reads `events_dedup`, never `events`" and calls it "a one-line change in `metrics.py`'s FROM clauses". It is not. `metrics.py:394`, `:448` and `:478` all carry `INDEXED BY ix_events_lifecycle`. SQLite does not accept `INDEXED BY` against a view. `normalize.py:79–85` records why the hint is there: without it the planner chose the wrong index and 2,000 bid lifetimes took **29 s** and hung the dashboard (BUG-20260909-040, `docs/logs/BUGS.md:391`). Swapping the FROM clauses as described would silently reintroduce a fixed S3. **This is a real design gap, not a nit: dedup must be resolved inside `order_lives` (a materialised derivation with its own indexes), not by a view swapped under existing hinted queries.**

**E-W5 — the criteria side table's primary key is per-connection.** `PRIMARY KEY (run, seq, idx)` is correct against today's `events` PK, but under §3.a the *same* offer arrives on both connections with different `(run, seq)` and therefore gets two sets of criteria rows. The COVERS join (`c.run = o.run AND c.seq = o.seq`) still resolves correctly, so nothing is wrong — but any count over `order_criteria` doubles. Say so, or the first "trait offers by criteria" count will be 2×.

**E-W6 — the "95.5% coverage" claim is not in this repository, and its denominator is one of four disputed values.** See below; I am filing it as UNVERIFIABLE-with-a-correction rather than simply wrong, because the numerator is sourced.

#### UNVERIFIABLE FROM THE REPO

**E-U1 — the events-endpoint page size of 200. This is the load-bearing assumption of §3.b and I could not verify it.**

What exists in-repo: `docs/00:124` (REQ-D-13) states "cursor-paginated, **up to 200 per page**"; `docs/07 §3.2` (line 204) repeats "200 per page". Both are internal documents. Neither carries a verification note.

**Why that is not good enough here.** Twelve lines earlier, `docs/07 §3.1` (line 196) says the *NFTs* endpoint's 1–200 was "**verified against the API reference on 2026-09-09**". The events endpoint gets no such note — the repo's own convention marks the difference. And this project has already been burned by exactly this artifact class: `config/base.yaml:16–21` and `governor.py:5–6` record that the 600 reads/hour figure "was an assumption that survived into every document" until it was measured at 120. Note also that `traits.py:145`'s `limit: 200` is the **NFTs** endpoint and is not evidence about events.

**Sensitivity, and why the recommendation survives anyway:**

| page size | full backfill, per gap-hour | as % of 120/h budget | recommended (no item bids) |
|---|---|---|---|
| 200 (assumed) | 322 | 268% | 2 |
| 100 | 643 | 536% | 4 |
| 50 | 1,285 | 1,071% | 7 |

Every branch says the same thing: full backfill is arithmetically unavailable, no-bid backfill is trivially cheap. **The decision does not turn on the assumption; the headline number does.** *What settles it: one read against the events endpoint with `limit=200`, counting the array length in the response.* One token. Do it before any backfill client is sized, and record the result next to REQ-D-13.

**E-U2 — the argonauts-explorer 95.5% coverage claim.**

- **Not in this repository at all.** `8,798`, `95.5%` and `08_ARGONAUTS_EXPLORER_INTEGRATION.md` appear nowhere in the working copy except inside this proposal. The cited document lives in the claude.ai project knowledge base, not on disk.
- **The numerator is sourced.** That project document reads: "Current cache: **8,798 tokens**, 768 listed, floor Ξ0.537778." So 8,798 is real.
- **The denominator is one of four disputed values, and the proposal picked the one that maximises the percentage.** The same project document, §5.4 — which the proposal cites — reports "`supply: 9180` from the collection stats endpoint but 8,798 indexed tokens, against a **9,999 max supply**". `docs/06:249` says **9,210**. `FACTS` says **9,212**. So:

  | denominator | source | coverage |
  |---|---|---|
  | 9,180 | stats endpoint (project doc §5.4) | 95.8% |
  | 9,210 | OpenSea screenshot (`docs/06:249`) | 95.5% |
  | 9,212 | FACTS, "from earlier session" | 95.5% |
  | 9,999 | max supply (project doc §5.4) | **88.0%** |

  **Correction: "88.0%–95.8%, denominator disputed four ways", not "95.5%".** The proposal's own §2.d condition 2 says the discrepancy must be "recorded as a discrepancy, not reconciled by picking a favourite" — and then §2.d, §2.e and the cost table all quote a single number computed from one favourite. Fix the number to match the condition.
- **Two further caveats the proposal does not carry.** The project document's §5.3 is an open question: "**Where did his 8,798-token cache come from, and when?**" — provenance unconfirmed. And the document says the cache carries rarity; it does not assert that all 8,798 rows carry complete trait sets. Neither is fatal to L0; both need answering before L0 is treated as 95%-of-anything.
- Licensing (§5.1 of that document) is confirmed open, as the proposal says.

**E-U3 — `order_hash` is NULL for `item_transferred` / some `item_sold` / `item_metadata_updated`.** The **[CODE]** half is exactly right: `normalize.py:253` is a bare `p.get("order_hash")` with no guarantee, so the column is nullable for any type. The **[OBS]** half — that it is *actually* NULL for those types — cannot be checked here (`events` has 0 rows). *What settles it:* `SELECT event_type, COUNT(*), SUM(order_hash IS NULL) FROM events GROUP BY 1;` on the Operator's store.

  **Answering the question directly: is `(event_type, order_hash, event_timestamp)` unique?** No, for three separate reasons, and the proposal catches only the first.
  1. **NULL `order_hash` collapses distinct rows.** The proposal's fix (widen with contract/token/maker/price/quantity) is correct in shape.
  2. **The widened key still omits `tx_hash`**, which is parsed and stored (`normalize.py:265`) and is the natural disambiguator for `item_sold` and `item_transferred` — the exact two types the widening exists to protect. **Add it.** (For transfers the widened key is probably already safe: after a transfer the maker no longer holds the token, so two same-second transfers of one token cannot share a maker. `tx_hash` makes that reasoning unnecessary, which is better than relying on it.)
  3. **`event_timestamp` may be absent** — E-W3.

**E-U4 — "two simultaneous WebSocket connections on one account are permitted".** Correctly flagged `[ASM]` by the author, correctly identified as a ten-minute test, correctly named as the thing §3.a rests on. Nothing to add except: **run the test before the design review, not before the build.** It is the cheapest gate in the entire plan.

**E-U5 — RPC compute-unit costs (~26 CU per `eth_call`, ~300 CU/s free tier, ~10 minutes for 9,212 calls).** Provider-specific and unverifiable here. Not load-bearing — the conclusion is "zero OpenSea reads", which holds at any RPC throughput.

---

## 2. Conflicts between the three proposals, and how each resolves

**C1 — Episode collapse (quant §2.1) vs the ladder chart (design §1.3) vs the 20,000-point scatter.**
Design's "Bids vs floor" is a `scattergl` scatter with a 20k point cap and systematic sampling above it. Quant §2.3 says draw one step line per maker from `bid_levels` and keep the scatter as a drill-down.
**Resolve toward the quant, with one amendment.** A sampled scatter is a chart that requires a caveat to be honest; a level ladder needs none, has no point budget, and shows the structure the scatter only implies. **But `bid_levels` does not exist and is two derivations away** (it needs `order_lives`, which needs T1/T3 fixed). So: **ship the capped scatter first as the drill-down it will eventually be, and build the ladder as the hero when `bid_levels` lands.** Do not ship the sampled scatter *as the hero* — the caveat line becomes permanent furniture.

**C2 — Greenwood (design §4.1) vs cluster bootstrap (quant §3.2).**
**Resolve toward the quant, without qualification.** Greenwood assumes independent observations; 42,601 quotes from 3 makers are not independent, and the quant's own estimate is that an order-level band would be ~15× too narrow. A too-narrow band on the front page is REQ-F-19 satisfied in letter and violated in substance. **Ship the cluster bootstrap (resample by maker-episode, B = 1000) or ship no band and say why.** Greenwood may be computed alongside as a diagnostic — where the two disagree, the disagreement is itself the measurement of clustering.

**C3 — Quant §5's REST ranking vs data-engineer §2's zero-read plan.**
These read as competitors and are not. **They do not overlap.** Quant §5.3 (traits, 97 reads) is the *same capability* the data engineer's L0–L3 reaches for 0–50 reads; quant §5.1 (holder ledger) and data-engineer §2.b need the *same RPC key* and the data engineer says so ("build them together"). What genuinely remains metered after §2.e is quant §5.2 (historical sales, ~35 reads one-time) and §5.4/§5.5 (standing book, ~11 reads/h) — and the data engineer explicitly says §2 and §3 "free up budget *for* those".
**Resolution: not a conflict. Merge into one ledger.**

| item | reads | when |
|---|---|---|
| Traits / token set (dataeng L0–L3 supersedes quant §5.3) | 0–50 one-time | first |
| Holder ledger + on-chain enumeration (quant §5.1 = dataeng §2.b, one key) | 0 | with the above |
| Historical sales (quant §5.2) | ~35 one-time (upper bound — see Q-W3) | after traits |
| Standing listings + collection offers (quant §5.4/§5.5) | ~11/h recurring | after the store can hold a standing book (`order_lives`) |
| Events-endpoint gap backfill, no item bids (dataeng §3.b) | 2–7 per gap-hour | when a client exists |
| Per-token offer queries (quant §5.8) | — | **never** |

The one thing the merged ledger changes: **quant §5.4 should be sequenced after `order_lives`, not before.** A listings snapshot loaded into a store whose `dead` predicate keeps orders alive forever (T3) produces a book that looks complete and is wrong.

**C4 — Trait-offer schema: `trait_offer_criteria(order_hash, trait_type, trait_name, seq)` (design §3.1) vs `order_criteria(run, seq, idx, kind, trait_type, value, num_min, num_max)` (dataeng §4.1).**
**Resolve toward the data engineer.** `order_hash` is nullable (`normalize.py:253`); the `events` PK is `(run, seq)` (`normalize.py:75`); the design's version has no `kind` discriminator and therefore cannot represent numeric criteria at all, which is exactly the class the data engineer's UNKNOWN verdict exists to exclude safely. One amendment: see E-W5 on double-counting under a redundant stream.

**C5 — What "the trait's lowest ask" means. Unresolved, and it gates the Operator's own decision.**
The Operator has decided the chart shows "the trait's lowest ask". Design §6-Q3 asks whether a floor is (a) standing asks only, (b) lowest ask *seen in the interval*, or (c) both. Today `floor_ask` is **(b)** — `metrics.py:49–50`, "Lowest listing seen in interval". Quant metric 1 and metric 5 assume **(a)**. Dataeng's COVERS matching is agnostic.
**This is not a presentation question and it cannot be defaulted.** Under (b) the chart shows a price that existed and may have been cancelled seconds later; under (a) it shows a price you can hit, with many holes at 8 listings/40 min. **Escalate to the Operator as a blocking question on the trait-chart PR.** My recommendation, and it is only that: **(a) standing, with (b) available as the dotted comparison** — because the Operator's stated purpose is trade decisions, and a floor you cannot hit is not a floor. But (a) is not computable correctly until T3 is fixed, which is another reason `order_lives` comes first.

**C6 — `immediacy_cost` under a trait filter (design §6-Q1) vs quant metric 1's leg discipline.**
Quant §1 metric 1 is explicit: the bid leg is the bid on the token *at the floor*, not the global best bid — "mixing those populations is how a spread goes negative." Design Q1 offers (a) pair the trait ask against the collection-wide offer, (b) refuse, (c) both.
**These are the same question and the quant has already answered it on the merits.** Q1(a) is precisely the population mixing the quant forbids. **Resolve toward Q1(c)** — it is the only option that both always produces a number and never presents a mixed-population number as the spread — but note that (c) reduces to (b) whenever no trait-specific bid exists, which on today's data is most buckets. Still the Operator's call; the recommendation now has a stated reason rather than three equal options.

**C7 — Design §4.0 and quant §6-T1 describe the same defect and prescribe different fixes.**
Design: "the fix is Kaplan–Meier with censored observations." Quant: KM *plus* competing risks, *plus* first-termination, *plus* `order_invalidate` as a terminator, *plus* orphan exclusion, *plus* expiry inference.
**Resolve toward the quant. Neither should be built as a chart fix.** Both are consequences of there being no `order_lives` relation. Fix the relation once; the panel then reads it.

**C8 — Dedup as a view (dataeng §3.a) vs the index hints (E-W4).**
**Resolve toward `order_lives` as the dedup boundary.** Materialise the derivation with its own indexes; do not swap a view under `INDEXED BY` queries. This also removes the "one-line change" framing, which was the most likely thing in that proposal to be taken at face value.

---

## 3. Build order

Each PR is small, independently reviewable, and lists the regression test that must **fail against today's code** before it is accepted (`docs/03 §9`). None of these merges without Spencer's approval.

### PR-0 — Two measurements, no code. *Do these first; both gate designs below.*
1. **Events-endpoint page size.** One REST read at `limit=200`; count the returned array. Record next to REQ-D-13 with the date and the response. Settles E-U1.
2. **Two simultaneous WebSocket connections on one account.** Ten minutes, two throwaway clients, one key. Settles E-U4 and gates PR-9.
*No test. These are facts, not code.*

### PR-1 — Autostart and supervised restart
Why first among code: **every hour not recording is irreplaceable** (`docs/07 §4`, landing zone retention "Forever … cannot be re-fetched"), and every other PR on this list is recoverable work that can be done later. The stream already halts safely on a local write failure rather than spinning (`stream.py:671–681`, `LandingZoneWriteError`), and records its own downtime gap on start (`stream.py:596, 653`, `record_downtime_gap`), so unattended running is not reckless.
**Test:** kill the process; assert a gap is opened in `gap_register` with a reason, that the supervisor restarts it, and that `_close_gap` fires on reconnect. Must fail today (nothing restarts it).
**Unblocks:** the `n` in every other item on this list. Nothing else depends on it, which is why it can go first.

### PR-2 — `order_lives`: the standing-book primitive
The root fix for quant T1 and T3 and design §4.0. A derivation (`docs/06 §4.2` DERIVATION layer, no assumptions) producing one row per `order_hash`: `t_place`, `t_term`, `exit_reason ∈ {cancelled, invalidated, filled, expired, censored}`, `expiration_ts`, `quantity`, `placement_seen`, plus a `standing(h, τ)` predicate. Replaces the three divergent `dead`/terminator definitions (Q-V4) with one. Keeps its own indexes (E-W4).
**Tests, each failing today:**
- an `order_hash` with two cancel rows yields exactly **one** life (T1);
- `order_invalidate` then `order_revalidate` ⇒ standing (T3a);
- a terminator with `valid_ts IS NULL` does **not** leave the order standing forever, and is counted as unknown rather than dropped (T3c);
- `quantity` is carried through; a `quantity=5` collection offer is 5 units of depth (T3b);
- a termination with no placement is excluded from durations **and counted** (`orphan_rate`, quant metric 12);
- `expiry` is written as a derived row with `source='derived'`, never as a market event.
**Unblocks:** PR-3, PR-5, PR-7, PR-8, and quant §5.4's snapshot (C3).

### PR-3 — `immediacy_cost` becomes the metric REQ-F-13a names
Recompute from `order_lives` as a standing-book spread with the quant's leg discipline (bid on the token at the floor). Report the time-weighted median with [p10, p90] per bucket and `both_legs_seconds / bucket_seconds` as coverage. **A negative value is an alarm, not a data point** — return `null`, log, and surface it on Health. Also in this PR: `pct()` returns `None` below `min_n_for_percentiles` (Q-V3, REQ-F-19).
**Tests:** a constructed book where the min ask and max bid never coexist yields the true spread, not the narrow one (fails today); a negative spread raises rather than rendering; `bid_lifetimes` percentiles are `None` at n = 7 and the UI shows the small-n message instead of numbers.
**Unblocks:** the Market KPI row; removes a front-page number that can currently display a free arbitrage.

### PR-4 — `order_criteria` side table, parsing, migration, re-fold
Dataeng §4.1 schema (C4), `parse_event` extraction of `trait_criteria` / `trait_criteria_list` / `numeric_trait_criteria_list`, `criteria_n` / `criteria_numeric_n` on `events`, additive `_migrate` following the `expiration_ts` precedent (`normalize.py:276–286`), watermark reset and re-fold. Values stored **verbatim, no case folding**.
**Tests, each failing today:**
- **the named property test: a `trait_offer` with `criteria_n = 0` matches nothing** (E-V8 — I am treating this as blocking);
- single form, list form and numeric form each parse to the right rows;
- a `trait_offer` we could not parse sets `criteria_n = 0` rather than `NULL`;
- numeric criteria produce verdict UNKNOWN, which is excluded **and counted**, never TRUE;
- the re-fold is idempotent (`INSERT OR REPLACE` on `(run, seq, idx)`, `INSERT OR IGNORE` on events);
- the casing validation: every distinct `(trait_type, value)` in `order_criteria` exists in `traits`, with misses counted and alerted.
**Note:** the re-fold can only be exercised against the Operator's landing zone. In CI it runs against fixtures. Say so in the PR description.
**Unblocks:** PR-5.

### PR-5 — Trait-group bid / trait floor / multi-floor metrics
The metric layer behind the Operator's decision. Over an arbitrary token set `S`, on the full bucket grid: `lowest_ask(S)` from `order_lives`; `highest_bid(S)` = max over (item bids on `S`, trait offers COVERing `S` per dataeng §4.3, collection offers), **each leg reported with its own n and kind**. Plus the multi-clause variants: combined AND-set, each single clause, and the unfiltered baseline. Matching-token counts on every series.
**Depends on:** PR-2 (standing), PR-4 (criteria).
**Blocked on:** the Operator's answer to C5 (what a floor is built from). Do not guess.
**Tests:** a collection offer COVERS every filter (`C = ∅`); a PARTIAL offer is reported as PARTIAL with `|S(F) ∩ S(C)|` and `|S(F)|`, never summed into the depth number; a bucket where the AND-set has no standing ask is `null`, never `0`; the BUG-045 blanket exclusion is gone and the replacement is evidence-based.
**Unblocks:** PR-6.

### PR-6 — The two decided trait charts
Single clause: trait highest bid (blue, solid) · trait lowest ask (orange, solid) · collection floor (pale dotted, drawn first). Multi-clause: combined-trait floor (weight 3.0) · each single-trait floor · baseline (pale dotted). `connectgaps:false`, markers on below ~120 points, custom HTML legend carrying `n` tokens and observed/total buckets.
**Depends on:** PR-5.
**Tests:** every series arrives on the full bucket grid with holes preserved; a series null at hover renders `— no observation` rather than being dropped; with 8 listings in the window the chart draws 8 visible markers, not an empty pane.

### PR-7 — Chart interaction and palette (independent; can run in parallel from PR-1 onward)
`displayModeBar:false`, y-axis locked, our own range chips. **Do not set `xaxis.fixedrange:true` if brushing is wanted** (D-W2). Split the palette: `--coll` off `#19C95A`, and replace the four hardcoded `#19C95A` data literals at `ui/index.html:180, 194, 202, 205` (D-V3). Pick a `--sale` that is not `--text` `#F1F5F2` (D-W3).
**Test:** an assertion over the token table that no two *data-role* tokens share a hex and no data-role token equals a chrome token — and that no chart literal bypasses the tokens. Fails today on `--verde-2` = `--coll` and on all four literals.
**Note for the PR description:** the Operator asked for the toolbar to go and it goes. But two of the four symptoms the design proposal named do not exist in the code (D-W1) — the description must not claim to have fixed wheel-zoom hijacking, which was never enabled.

### PR-8 — Survival estimator + bid-lifetime panel rebuild
KM with right-censoring, Aalen–Johansen cumulative incidence per cause, **maker-episode cluster bootstrap bands** (C2), RMST with `P(not ended by τ*)`, residual survival. Below n_eff = 30 clusters: strip plot, no curve, no percentiles. Panel: survival curve + exit-reason histogram + placement-time mini-map, with the filters and drill-down the Operator asked for.
**Depends on:** PR-2 (`order_lives` supplies censoring, competing risks and orphan exclusion).
**Tests:** a fixture where 40% of orders are still standing gives a median longer than the current estimator's (fails today by construction); 1−KM per cause and the CIF disagree on a fixture with two competing causes, and the code reports the CIF; the band is a cluster bootstrap and is materially wider than Greenwood on a 3-maker fixture; below the cluster minimum, percentiles are refused.
**Expectation to state in the PR, per D-W4:** the median may move in **either** direction, because the old estimator was biased short by censoring and long by the unbounded join. A large change is the expected consequence of two known defects, not a new bug — and not a discovery.

### PR-9 — UI view split (Market / Traits / Flow / Health)
Hash routing, persisted global filter state, trait sidebar on Market and Traits only. Deliberately **after** PR-6 and PR-8 so it moves working panels rather than broken ones.
**Test:** a deep link `#/traits?traits=...&range=6h` restores the exact view; global state survives reload; per-view overrides do not leak across tabs.

### PR-10 — Redundant stream, behind a config flag, default off
**Gated on PR-0.2 passing.** Two processes, two landing roots, two `.ingest.lock` files, **two API keys with staggered creation dates** (E-V13). Dedup resolved inside `order_lives`, not by a view (E-W4, C8). Key on `event_timestamp` explicitly, with `tx_hash` added, and rows lacking `event_timestamp` marked un-dedupable and counted (E-W3, E-U3). A gap in A that B covered stays a gap in A, annotated.
**Tests:** two synthetic connections delivering the same event yield one row in `order_lives` and two in `events`; an event lacking `event_timestamp` is counted as un-dedupable rather than merged or double-counted; **the duplicate-fraction monitor alarms when observed duplicates collapse toward zero while both connections are healthy** (dataeng failure mode 3 — the flattering-direction failure); a gap in A is recorded in A's register even when B was up.
**Config:** the flag must be read by running code. A `config/` block nothing parses is worse than no config.

---

## 4. My three biggest risks in this plan

**R1 — Everything above is verified by reading, not by running, and the gap between those two has already cost this project five silent data-loss bugs.**
`events` has 0 rows. `tokens` has 0 rows. `traits` has 0 rows. There is no landing zone in this working copy. Every defect I confirmed I confirmed from source text; every frequency question I had to defer to a query on the Operator's machine. `docs/logs/BUGS.md:394–397` records that BUG-039 (a 30% price error on every sale) and BUG-040 (a 29-second query) were caught **only** by running the new code over every real frame on the Operator's machine before shipping. PR-2 and PR-4 are the two largest rewrites on this list and both will meet real payload shapes for the first time in exactly that step. **Mitigation, and it is not optional: no PR on this list merges without a run over the full real corpus on the Operator's machine, with before/after counts in the PR description.** If that run is skipped for schedule, the plan's risk profile changes completely and I would rather stop the plan than skip the run.

**R2 — The critical path runs through the one thing that is not free.** PR-5 and PR-6 — the two charts the Operator has actually decided on — cannot produce a single line without a populated `traits` table, and that table is empty. Filling it costs either ~97 metered reads through `traits.py` (48 minutes of exclusive budget at 120/h) or an import whose licensing is unsettled (project doc §5.1), whose provenance is unconfirmed (§5.3), and whose coverage is 88.0%–95.8% depending on which of four disputed denominators you use (E-U2). Meanwhile PR-1 through PR-4 are all pure code and will look like fast progress. **The risk is that we ship four green PRs and discover at PR-5 that the deliverable is blocked on a conversation with a friend and a budget spend nobody sequenced.** Mitigation: run the traits onboarding at BACKFILL priority now, in parallel with PR-1, and treat the import as an optimisation rather than a dependency.

**R3 — Three of the changes on this list move numbers in the flattering direction, and they land close together.** A dedup key that is too wide doubles every count and makes a backtest look wonderful (dataeng failure mode 3). An unparsed trait offer with no criteria guard matches every filter and every token (E-V8). The ask-side left truncation makes the reconstructed floor too **high**, which makes every "underpriced relative to floor" screen look better than it is (Q-V9, Q-W2) — and that bias does not decay, because listings live for weeks. **All three produce plausible, confident, wrong numbers that look like edge.** Layered on top: `docs/01 §8.2` requires 50 independent signal events across ≥10 distinct collections for signal validation, and we have one collection, 3 makers and 3 sales — so nothing built here can be validated on this data no matter how clean the code is. The fifth project rule is the whole mitigation: **if any of this starts to look like an easy edge, the first hypothesis is a bug in the book reconstruction, and the escalation is to me before it is to a trade.**

---

## 5. What I am escalating to the Operator

1. **C5 — what a "floor" is allowed to be built from** (standing asks only / lowest seen in interval / both). This gates the trait charts you decided on and cannot be defaulted. My recommendation is standing-only with the observed floor as a dotted comparison, but it is your call and it has money attached.
2. **C6 / design Q1 — immediacy cost under a trait filter.** Recommendation is now (c), two lines, with a stated reason rather than three equal options.
3. **Design Q2 — what a KPI "% change" compares to.** Unchanged; still yours.
4. **The traits table is empty and the trait charts cannot draw without it** (R2). Either authorise ~97 metered reads now, or settle the licensing conversation for the friend's cache. Doing neither blocks PR-5.
5. **Two measurements before design work** (PR-0): one REST read to settle the events-endpoint page size, ten minutes to settle whether two WebSocket connections are permitted.
