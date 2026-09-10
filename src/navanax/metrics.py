"""The metric request contract (docs/06 §3), implemented once.

    MetricRequest = {metric, scope, denomination, transform, interval, range}

Any chart is that tuple. Phase 0 implements `scope = collection`, with an
orthogonal `traits` filter narrowing the token set (docs/08 §4a); token,
peer-group and universe scopes are not implemented yet. This module serves the tuple; the UI never contains
a bespoke calculation. Intervals come from `config/intervals.yaml` (REQ-N-09),
anchored ranges (HTD/DTD/MTD/YTD) use the DISPLAY timezone because "today"
means your today (docs/06 §1.3), and every response carries its basis --
baseline value and time, window, interval, denomination -- so a number can
never be quoted without it (docs/06 §2.2).

STRUCTURE ONLY. Every metric here is an observed quantity: the highest
collection offer seen in an interval, the lowest listing seen, how many sales
happened. No fair value, no smoothing, no wash filtering (wash_filter is
always `raw` and the response says so). Those are assumptions and live in the
layer above (docs/06 §4).

"Is this order still live?" is asked in three places here -- the live book, the
screener and the bid-lifetime panel -- and it is answered in exactly one:
`standing_sql()`, over the `order_lives` relation the normalizer folds
(docs/08 §3.3). Before that relation existed each of the three carried its own
terminator list and none of them handled a revalidate, an untimed terminator or
a duplicate cancel (BUG-049, BUG-050). A trait offer's criteria are matched by
`criteria_cover_sql()` and the guard in its docstring is load-bearing
(BUG-051).

`immediacy_cost` (REQ-F-13a, methodology §3.2): "what you pay to force
time-to-clear to zero by **hitting the standing collection offer**". Note the
word *standing*. Until BUG-20260910-057 this was computed as an interval
extremum -- `MIN(ask)` over a bucket minus `MAX(collection offer)` over the same
bucket -- and the two legs need not ever have coexisted. That is a different
quantity from the one docs/01 §3.2 defines: biased narrow, worse at wider
intervals, and capable of rendering NEGATIVE on the front page, where a KPI
would read as free arbitrage. It is now built from the STANDING book
(`order_lives`), both legs sampled on the same tau, and a negative value is an
alarm rather than a data point. The old behaviour is still reachable as
`book='observed'` and is labelled as such wherever it is used.

**The Operator's decision of 2026-09-10:** floors and lowest-ask lines are built
from STANDING asks only. A bucket with no live ask is a hole, not a zero and not
the last price seen. `book='observed'` exists for comparison, never as the
default.

Undefined -- returned as null, never substituted -- when either leg is absent.
"No bid at any price" is the most important liquidity fact about a collection,
and a chart must show it as a hole, not a guess.

**The standing book is LEFT-TRUNCATED and every standing series says so.** We
only see orders whose placement we witnessed; Argonauts had ~801 standing
listings before recording started (docs/06 §249, quant §0). So the
reconstructed floor is an OVER-estimate of the true floor and the reconstructed
best bid an UNDER-estimate: a stream-only spread is biased WIDE, and it is an
upper bound, not a measurement. `basis.left_truncated` carries this on every
response.
"""

from __future__ import annotations

import bisect
import heapq
import logging
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("navanax.metrics")

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

TRANSFORMS = ("ABS", "PCT", "LOG", "DIFF", "BPS")
DENOMS = ("ETH", "USD")
BOOKS = ("standing", "observed")

# REQ-F-19 / Q-V3: below this many observations a percentile is not reported at
# all. A flag next to a number is not a refusal -- docs/00:199 says the system
# SHALL make quoting an unreliable percentile impossible, and a rendered number
# with a warning beside it is exactly what people quote.
#
# REQ-N-09 tension, stated rather than hidden: this is a threshold and thresholds
# belong in config. It lived as a bare `30` inside `bid_lifetimes` before; one
# named constant used by every caller is strictly better than that, and moving it
# to `config/base.yaml` needs a MetricEngine constructor change that this PR was
# told not to make in dashboard.py. Raised for the tech-lead.
MIN_N_FOR_PERCENTILES = 30

# How many PARTIAL offers travel with a verdict report as full detail. The
# report is JSON on a localhost response and the page prints the first few; an
# uncapped list reached 4.1 MB on the tech-lead's fixture, which is a page that
# stalls rather than a page that tells the truth harder. The COUNT is never
# capped -- `partial_n` is computed separately from the detail list, because a
# cap that silently becomes the answer is worse than no detail at all.
PARTIAL_DETAIL_CAP = 50

# metric id -> (event_type filter, aggregate, price column?, description)
#
# `book`      : the standing-book kind this metric has, if any (STANDING_KINDS).
# `book_default`: which book the metric uses when the caller does not say.
METRICS: dict[str, dict[str, Any]] = {
    "collection_bid":  {"types": ("collection_offer",), "agg": "MAX", "price": True,
                        "book": "collection_bid", "book_default": "standing",
                        "label": "Highest collection offer seen in interval",
                        "standing_label": "Highest STANDING collection offer "
                                          "(time-weighted median over the bucket)"},
    # `top_item_bid` HAS a standing-book kind -- `item_bid` in STANDING_KINDS --
    # and its default is nevertheless `observed`, which is the one metric where
    # those two differ. Tech-lead re-review, 2026-09-10: the `book` key was
    # missing entirely, and three wrong behaviours followed from that one
    # omission. (1) `series(book='standing')` was refused with the reason "it is
    # a count or flow metric", which is false -- it is a price metric with a
    # resting book, and the real reason is leg discipline. (2) The basis printed
    # `book: "n/a (top_item_bid is a count/flow metric...)"`, so the page told the
    # Operator the wrong thing about a line it was drawing. (3) The
    # `observed_book_warning` is gated on `"book" in spec`, so the ONE price line
    # on the Prices panel that is always an interval extremum was the one line
    # carrying no warning that it is one. The default stays `observed`; what
    # changes is that the refusal, the basis and the warning now tell the truth.
    "top_item_bid":    {"types": ("item_received_bid",), "agg": "MAX", "price": True,
                        "book": "item_bid", "book_default": "observed",
                        "label": "Highest item bid seen in interval",
                        "standing_label": "Highest STANDING item bid "
                                          "(time-weighted median over the bucket)"},
    "floor_ask":       {"types": ("item_listed",), "agg": "MIN", "price": True,
                        "book": "ask", "book_default": "standing",
                        "label": "Lowest listing seen in interval",
                        "standing_label": "Lowest STANDING ask "
                                          "(time-weighted median over the bucket)"},
    "sale_price":      {"types": ("item_sold",), "agg": "MEDIAN", "price": True,
                        "label": "Median sale price in interval"},
    "volume":          {"types": ("item_sold",), "agg": "SUM", "price": True,
                        "label": "Sum of sale prices in interval"},
    "sales_count":     {"types": ("item_sold",), "agg": "COUNT", "price": False,
                        "label": "Sales in interval"},
    "listing_count":   {"types": ("item_listed",), "agg": "COUNT", "price": False,
                        "label": "New listings in interval"},
    "bid_count":       {"types": ("item_received_bid", "collection_offer", "trait_offer"),
                        "agg": "COUNT", "price": False, "label": "Bids placed in interval"},
    "cancel_count":    {"types": ("item_cancelled", "order_invalidate"), "agg": "COUNT",
                        "price": False, "label": "Cancellations + invalidations in interval"},
    "event_count":     {"types": None, "agg": "COUNT", "price": False,
                        "label": "All market events in interval"},
    "immediacy_cost":  {"derived": ("floor_ask", "collection_bid"),
                        "book": "spread", "book_default": "standing",
                        "label": "Lowest ask minus highest collection offer (REQ-F-13a)",
                        "standing_label": "STANDING lowest ask minus STANDING highest collection "
                                          "offer, both legs on the same tau (REQ-F-13a, docs/01 §3.2)"},
}

# The standing-book legs `standing_series` can build. Each is one event type,
# one extremum, and whether a trait filter narrows it.
#
# `collection_offer` is deliberately NOT token-scoped: a collection offer carries
# no token and bids on every one of them, so it is the bid that is available on
# whichever token is at the floor. That is the whole of the quant's leg
# discipline for metric 1 -- the bid leg must be a bid on the token at the floor,
# not the global best bid over all tokens, "mixing those populations is how a
# spread goes negative" (quant §1 metric 1). Item bids and trait offers are
# per-token or per-criteria and so are NOT interchangeable with this leg; a bid
# leg that maxes over all three is a different metric (PR-5).
STANDING_KINDS: dict[str, dict[str, Any]] = {
    "ask":            {"event_type": "item_listed", "want": "min", "token_scoped": True,
                       "label": "lowest standing ask"},
    "collection_bid": {"event_type": "collection_offer", "want": "max", "token_scoped": False,
                       "label": "highest standing collection offer"},
    "item_bid":       {"event_type": "item_received_bid", "want": "max", "token_scoped": True,
                       "label": "highest standing item bid"},
    # PR-5. The third bid leg of the trait chart. It is NOT token-scoped -- a
    # trait offer carries no token_id -- and it is not narrowed by the token
    # filter either: it is narrowed by the COVERS rule (dataeng §4.3), which
    # asks whether the offer will buy ANY token the filter selects. Under an
    # EMPTY filter no trait offer covers (S(F) is every token, and only C = {}
    # reaches all of them), so this leg is legitimately empty with no filter --
    # that is the rule, not a missing join.
    "trait_offer_cover": {"event_type": "trait_offer", "want": "max", "token_scoped": False,
                          "cover_scoped": True,
                          "label": "highest standing trait offer whose criteria COVER the filter"},
}

# The three legs of the trait group's highest standing bid (PR-5, Operator's
# decision of 2026-09-10). Reported with its own n and kind, never summed:
# they are three populations, and the number on the chart is the MAX over their
# union at each tau, not a total.
TRAIT_BID_LEGS: dict[str, str] = {
    "item": "item bids on tokens in S(F) -- token-scoped, narrowed by the filter",
    "trait_offer": "trait offers whose stored criteria COVER F, i.e. S(F) subset of S(C) "
                   "(dataeng §4.3); unparsed (criteria_n = 0) and numeric-criteria offers "
                   "are excluded AND counted, never assumed to match",
    "collection": "collection offers -- C = {}, a bid on every token, so they reach S(F) "
                  "whatever F is",
}

# Metrics that HAVE a standing-book kind but whose STANDING variant `series()`
# deliberately does not offer yet, with the reason. The reason is returned to the
# caller verbatim, so it has to be true: a refusal that misstates why is worse
# than no refusal, because it is the sentence the next person reasons from.
STANDING_NOT_OFFERED: dict[str, str] = {
    "top_item_bid": (
        "an item bid is PER-TOKEN. A standing 'highest item bid' maxes over bids on "
        "different tokens, which is not interchangeable with a collection-wide leg -- "
        "mixing those populations is how a spread goes negative (quant §1 metric 1 leg "
        "discipline, Operator's decision of 2026-09-10). The union bid leg that would "
        "make it comparable is PR-5. The primitive already exists and is reachable "
        "directly -- standing_series('item_bid', ...) -- it is only the metric DEFAULT "
        "that is deliberately not moved. Ask for it without `book`, or with "
        "book='observed', and read the observed_book_warning in the basis."),
}

# Negative-spread alarms are logged once per (collection, interval, denomination,
# bucket_start) per process. The dashboard re-renders every 10 s and an alarm that
# floods the log is an alarm nobody reads -- but the key used to omit the bucket,
# so once ANY bucket for a collection had crossed, every LATER crossing on that
# collection was swallowed for the life of the process. A dashboard left open
# overnight would log the 09:00 crossing and never mention the 14:00 one. The
# bucket start is in the key so a NEW crossing always logs, while re-rendering the
# SAME crossed bucket stays deduped, which is the flooding the cap was for.
_NEGATIVE_LOGGED: set[tuple[str, str, str, float]] = set()


# ---------------------------------------------------------------------------
# intervals and ranges
# ---------------------------------------------------------------------------
def load_intervals(cfg_path: str | Path) -> dict[str, Any]:
    import yaml
    with Path(cfg_path).open() as fh:
        data = yaml.safe_load(fh) or {}
    out = {"intervals": {}, "anchored": {}}
    for it in data.get("intervals", []):
        out["intervals"][it["id"]] = it
    for ar in data.get("anchored_ranges", []):
        out["anchored"][ar["id"]] = ar
    return out


def _tz(name: str):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone name: fall back, do not crash a chart
        return timezone.utc


def anchored_start(anchor: str, now: datetime, tz_name: str) -> datetime:
    """HTD/DTD/MTD/YTD start, in the display timezone, returned as UTC."""
    local = now.astimezone(_tz(tz_name))
    if anchor == "hour":
        s = local.replace(minute=0, second=0, microsecond=0)
    elif anchor == "day":
        s = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif anchor == "month":
        s = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif anchor == "year":
        s = local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown anchor {anchor!r}")
    return s.astimezone(timezone.utc)


def parse_range(spec: str, intervals: dict[str, Any], now: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """'6h' / '24h' / '7d' / '30d' trailing windows, or an anchored id (DTD...)."""
    if spec in intervals["anchored"]:
        return anchored_start(intervals["anchored"][spec]["anchor"], now, tz_name), now
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        n, u = int(spec[:-1]), spec[-1]
        return now - timedelta(seconds=n * units[u]), now
    except (ValueError, KeyError, IndexError) as exc:
        raise ValueError(f"unknown range {spec!r}; use e.g. 1h, 24h, 7d or HTD/DTD/MTD/YTD") from exc


def bucket_of(ts: float, interval: dict[str, Any], tz_name: str) -> float:
    """Start (epoch seconds) of the bucket containing `ts`.

    Fixed-duration intervals shorter than a day bucket in UTC. Days and
    longer bucket on DISPLAY-timezone boundaries, and calendar intervals
    (months, years) use calendar arithmetic -- docs/06 §1.3: a month is not
    30 days.
    """
    if "duration" in interval:
        d = int(interval["duration"])
        if d < 86400:
            return math.floor(ts / d) * d
        # whole days: align to local midnight
        local = datetime.fromtimestamp(ts, tz=_tz(tz_name))
        day0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
        days_per = d // 86400
        epoch_day = (day0 - datetime(1970, 1, 5, tzinfo=day0.tzinfo)).days  # Monday-aligned for weeks
        start_day = day0 - timedelta(days=epoch_day % days_per)
        return start_day.timestamp()
    cal, n = interval.get("calendar"), int(interval.get("n", 1))
    local = datetime.fromtimestamp(ts, tz=_tz(tz_name))
    if cal == "month":
        m0 = ((local.month - 1) // n) * n + 1
        return local.replace(month=m0, day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    if cal == "year":
        y0 = (local.year // n) * n
        return local.replace(year=y0, month=1, day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    raise ValueError(f"unsupported interval {interval}")


MAX_BUCKETS = 20_000   # 1-minute bars over two weeks; beyond this the chart is noise and the JSON is megabytes


def bucket_grid(start: float, end: float, interval: dict[str, Any], tz_name: str) -> list[float]:
    """Every bucket start in [start, end), observed or not.

    A series is reported on this grid so that an interval with no observation
    is an explicit null -- a hole on the chart -- rather than a missing point
    that the line silently bridges (docs/06 §1.1: a regular-grid series must
    mark its gaps). Steps by the interval for sub-day buckets and by one day
    otherwise, so calendar buckets (months, years) come out of bucket_of.
    """
    step = int(interval["duration"]) if "duration" in interval and int(interval["duration"]) < 86400 else 86400
    if end <= start:
        return []
    if (end - start) / step > MAX_BUCKETS * 4:
        raise ValueError(f"{interval.get('id', '?')} bars over this range is more than {MAX_BUCKETS:,} buckets; choose a larger interval")
    grid: list[float] = []
    t = start
    last = None
    while t < end:
        b = bucket_of(t, interval, tz_name)
        if b != last:
            grid.append(b)
            last = b
        t += step
    if len(grid) > MAX_BUCKETS:
        raise ValueError(f"{len(grid):,} buckets is more than {MAX_BUCKETS:,}; choose a larger interval")
    return grid


# ---------------------------------------------------------------------------
# transforms
# ---------------------------------------------------------------------------
def apply_transform(values: list[float | None], transform: str) -> tuple[list[float | None], dict[str, Any]]:
    """docs/06 §2.2. Baseline = first non-null value in the window."""
    if transform not in TRANSFORMS:
        raise ValueError(f"unknown transform {transform!r}; one of {TRANSFORMS}")
    base_idx = next((i for i, v in enumerate(values) if v is not None), None)
    basis: dict[str, Any] = {"transform": transform, "baseline_index": base_idx,
                             "baseline_value": values[base_idx] if base_idx is not None else None}
    if transform == "ABS" or base_idx is None:
        return list(values), basis
    v0 = values[base_idx]
    out: list[float | None] = []
    for v in values:
        if v is None:
            out.append(None)
        elif transform == "DIFF":
            out.append(v - v0)
        elif transform == "PCT":
            out.append((v - v0) / v0 if v0 else None)
        elif transform == "BPS":
            out.append((v - v0) / v0 * 10_000 if v0 else None)
        elif transform == "LOG":
            out.append(math.log(v / v0) if v0 > 0 and v > 0 else None)
    return out, basis


# ---------------------------------------------------------------------------
# trait filters -- AND across trait types, OR within one (OpenSea's semantics)
# ---------------------------------------------------------------------------
def parse_trait_filter(spec: str | None) -> dict[str, list[str]]:
    """'Background:Blue|Red;Eyes:Laser' -> {'Background': ['Blue','Red'], 'Eyes': ['Laser']}"""
    out: dict[str, list[str]] = {}
    if not spec:
        return out
    for part in spec.split(";"):
        if ":" not in part:
            continue
        t, vals = part.split(":", 1)
        vs = [v.strip() for v in vals.split("|") if v.strip() != ""]
        if t.strip() and vs:
            out[t.strip()] = vs
    return out


def token_filter_sql(collection: str, traits: dict[str, list[str]], alias: str = "e") -> tuple[str, list[Any]]:
    """SQL fragment restricting `alias`.token_id to tokens matching every trait clause."""
    if not traits:
        return "", []
    clauses, args = [], []
    for t, vals in traits.items():
        clauses.append(
            f"{alias}.token_id IN (SELECT token_id FROM traits WHERE collection = ? AND trait_type = ? "
            f"AND value IN ({','.join('?' * len(vals))}))")
        args += [collection, t, *vals]
    return " AND " + " AND ".join(clauses), args


# ---------------------------------------------------------------------------
# trait offers under a filter -- the COVERS rule (dataeng §4.3)
# ---------------------------------------------------------------------------
def criteria_cover_sql(alias: str, traits: dict[str, list[str]]) -> tuple[str, list[Any]]:
    """True iff the criteria-bearing order at `alias` COVERS the filter F.

    C is the offer's criteria set, an AND: it bids on any token holding all of
    them. F is the filter, OR within a trait type and AND across types.

        COVERS  <=>  S(F) subset of S(C)  -- the offer will buy ANY token the
                     filter selects, so it is bid depth for that filter.

    Note the direction: what makes an offer count is not that it is confined to
    the filter, it is that it will buy anything the filter picks. Structurally
    that means every criterion (t, v) in C must be GUARANTEED by F -- F must
    have a clause on `t` whose only permitted value is `v`. A filter of
    `Background: Blue|Red` guarantees nothing about Background, so an offer
    requiring Blue is PARTIAL, not COVERS.

    **The guard is the whole safety of this rule.** An unparsed trait offer has
    no `order_criteria` rows, which makes the NOT EXISTS vacuously true and the
    offer would match every filter and every token -- silently, plausibly, and
    in the direction that manufactures an edge (BUG-051). `criteria_n > 0` is
    what stops it. Numeric criteria cannot be evaluated against the string
    `traits` table, so their verdict is UNKNOWN, and UNKNOWN is not TRUE:
    `criteria_numeric_n = 0` excludes them here and
    `MetricEngine.trait_offer_verdicts` counts them.
    """
    pairs = [(t, list(dict.fromkeys(vs))[0]) for t, vs in traits.items() if len(set(vs)) == 1]
    args: list[Any] = []
    inner = ""
    if pairs:
        guard = " OR ".join("(c.trait_type = ? AND c.value = ?)" for _ in pairs)
        for t, v in pairs:
            args += [t, v]
        inner = f" AND COALESCE({guard}, 0) = 0"
    sql = (f"({alias}.criteria_n > 0 AND COALESCE({alias}.criteria_numeric_n, 0) = 0"
           f" AND NOT EXISTS (SELECT 1 FROM order_criteria c"
           f" WHERE c.run = {alias}.run AND c.seq = {alias}.seq AND c.kind = 'string'{inner}))")
    return sql, args


def filter_narrows(spec: dict[str, Any]) -> bool:
    """Does a trait filter actually narrow this metric's event set?

    **No** for a metric whose events are all collection offers. A collection
    offer carries no token and bids on every one of them, so `collection_bid` is
    deliberately collection-wide even under a filter -- that is the leg
    discipline quant §1 metric 1 requires, and the `legs` line on every filtered
    spread says so out loud.

    **Yes** for everything else, including a metric with no `types` at all
    (`event_count`) and a derived metric whose legs include a token-scoped one
    (`immediacy_cost`).

    One predicate, because two places need the same answer and they were about
    to disagree: `_bucketed` decides whether to append the filter clause, and
    `series()` decides whether an empty token set makes the metric undefined.
    """
    types = spec.get("types")
    return not (types and set(types) <= {"collection_offer"})


def criteria_covers(criteria: list[tuple[str, str]], traits: dict[str, list[str]]) -> bool:
    """The same rule in Python, for the verdict report. `criteria` is [(type, value)]."""
    if not criteria:
        return False           # C = {} on a criteria-bearing order is the loud-failure marker
    single = {t: list(set(vs))[0] for t, vs in traits.items() if len(set(vs)) == 1}
    return all(single.get(t) == v for t, v in criteria)


def standing_sql(alias: str, now_ts: float) -> tuple[str, list[Any]]:
    """SQL predicate: the order named by `alias`.order_hash was STANDING at `now_ts`.

    One definition, used by `live_book`, `screener` and `bid_lifetimes`. Before
    `order_lives` there were three (metrics.py had a `dead` subquery in two
    places and a third, different, terminator list in `cancel_count`), and none
    of them handled a revalidate, a NULL-`valid_ts` terminator or a duplicate
    cancel (BUG-050).

        standing(h, t)  <=>  t_place <= t
                             AND exit_reason <> 'unknown'
                             AND (t_term IS NULL OR t_term > t)
                             AND (expiration_ts IS NULL OR expiration_ts > t)

    `unknown` is a life whose terminator we saw but cannot place in time. It is
    excluded from the book AND counted, never silently standing forever.
    """
    return (f"""EXISTS (SELECT 1 FROM order_lives ol
                        WHERE ol.order_hash = {alias}.order_hash
                          AND ol.placement_seen = 1
                          AND ol.t_place IS NOT NULL AND ol.t_place <= ?
                          AND ol.exit_reason <> 'unknown'
                          AND (ol.t_term IS NULL OR ol.t_term > ?)
                          AND (ol.expiration_ts IS NULL OR ol.expiration_ts > ?))""",
            [now_ts, now_ts, now_ts])


# ---------------------------------------------------------------------------
# percentiles -- refused, not flagged, below the minimum n (REQ-F-19, Q-V3)
# ---------------------------------------------------------------------------
def pct(sorted_values: list[float], p: float, *, min_n: int = MIN_N_FOR_PERCENTILES) -> float | None:
    """The p-quantile of an ALREADY SORTED sample, or None below `min_n`.

    `docs/00:199` (REQ-F-19) says the system SHALL make it impossible to quote a
    percentile computed from too few observations. Returning the number with a
    `percentiles_reliable: false` beside it does not make it impossible -- the
    page rendered `p10 / median / p90` unconditionally and appended the warning
    as a string, and a number on screen is a number that gets quoted. None is
    the refusal; the count and the minimum travel with it so the caller can say
    WHY it is missing.
    """
    n = len(sorted_values)
    if n == 0 or n < min_n:
        return None
    return sorted_values[min(n - 1, int(p * n))]


# ---------------------------------------------------------------------------
# the standing book over time: a sweep line, not a per-bucket scan
# ---------------------------------------------------------------------------
def _extremum_segments(live: list[tuple[float, float, float]],
                       want_min: bool) -> list[tuple[float, float, float]]:
    """The running min (or max) of a set of intervals, as constant segments.

    `live` is [(t_from, t_to, price)] already clipped to the window. The result
    is [(t0, t1, value)] -- the value of the extremum over [t0, t1), one entry
    per change, in time order, with no entry for time when nothing stood.

    A sweep line over placements and terminations with a lazily-cleaned heap:
    O(E log E) in the number of order ENDPOINTS, never O(buckets x table). The
    naive alternative -- re-querying the book at every bucket boundary -- is
    O(buckets) full scans, which is how the 29-second query of BUG-040 happened
    on a table two orders of magnitude smaller than this one will be.
    """
    sign = 1.0 if want_min else -1.0
    by_t: dict[float, list[tuple[int, int]]] = {}
    for i, (a, b, _price) in enumerate(live):
        by_t.setdefault(a, []).append((1, i))
        by_t.setdefault(b, []).append((-1, i))
    times = sorted(by_t)
    alive: set[int] = set()
    heap: list[tuple[float, int]] = []
    segs: list[tuple[float, float, float]] = []
    for idx, t in enumerate(times):
        # Apply EVERY event at t before reading the state: standing(h, tau) is
        # `t_place <= tau AND t_term > tau`, so an order placed at t is standing
        # at t and one terminated at t is not, and the two orderings must not
        # race each other when they share a timestamp.
        for typ, i in by_t[t]:
            if typ == 1:
                alive.add(i)
                heapq.heappush(heap, (sign * live[i][2], i))
            else:
                alive.discard(i)
        if idx + 1 >= len(times):
            break
        nxt = times[idx + 1]
        while heap and heap[0][1] not in alive:
            heapq.heappop(heap)
        if heap and nxt > t:
            segs.append((t, nxt, sign * heap[0][0]))
    return segs


def _join_segments(a: list[tuple[float, float, float]], b: list[tuple[float, float, float]],
                   ) -> list[tuple[float, float, float, float]]:
    """[(t0, t1, a_value, b_value)] over the time BOTH legs stood.

    Both inputs are sorted and non-overlapping, so this is a linear merge. This
    is what "both legs on the same tau samples" means mechanically: a bucket's
    spread is only ever computed from an ask and a bid that were simultaneously
    live, which is the property the interval-extremum version did not have.
    """
    out: list[tuple[float, float, float, float]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        a0, a1, av = a[i]
        b0, b1, bv = b[j]
        lo, hi = max(a0, b0), min(a1, b1)
        if hi > lo:
            out.append((lo, hi, av, bv))
        if a1 <= b1:
            i += 1
        else:
            j += 1
    return out


def _bucket_windows(grid: list[float], start: float, horizon: float,
                    interval: dict[str, Any], tz_name: str) -> list[tuple[float, float]]:
    """The observable [lo, hi) of every bucket on the grid.

    A bucket at the edge of the range is only observable over its intersection
    with the query window, and NOTHING is observable past `horizon` (= min(end,
    now)) -- a censored order stands "until further notice", and treating that as
    standing into the future would be imputing the future (docs/06 §4.3). So the
    coverage denominator is the observable width, and the basis reports it.
    """
    out: list[tuple[float, float]] = []
    for i, b in enumerate(grid):
        nxt = grid[i + 1] if i + 1 < len(grid) else _next_bucket_start(b, interval, tz_name)
        out.append((max(b, start), min(nxt, horizon)))
    return out


def _next_bucket_start(b: float, interval: dict[str, Any], tz_name: str) -> float:
    step = int(interval["duration"]) if "duration" in interval else 86400
    t = b + step
    for _ in range(400):                     # a calendar month is at most 31 steps of a day
        nb = bucket_of(t, interval, tz_name)
        if nb > b:
            return nb
        t += step
    return b + step


def _accumulate(segs: list[tuple[float, float, float]], grid: list[float],
                windows: list[tuple[float, float]]) -> dict[int, list[tuple[float, float]]]:
    """Split constant segments at bucket boundaries -> {bucket index: [(seconds, value)]}.

    Total work is O(segments + buckets): a segment spanning k buckets emits k
    pairs, and sum over segments of (1 + duration/step) is bounded by
    (segments + buckets). No segment is ever visited per-bucket-of-the-range.
    """
    out: dict[int, list[tuple[float, float]]] = {}
    for t0, t1, v in segs:
        i = max(0, bisect.bisect_right(grid, t0) - 1)
        while i < len(grid) and grid[i] < t1:
            lo, hi = windows[i]
            a, b = max(t0, lo), min(t1, hi)
            if b > a:
                out.setdefault(i, []).append((b - a, v))
            i += 1
    return out


def _distinct_per_bucket(live: list[tuple[float, float, float]], grid: list[float]) -> list[int]:
    """How many DISTINCT orders stood at any point in each bucket.

    A difference array, so an order standing across ten thousand buckets is one
    increment and one decrement rather than ten thousand of each. This is the
    `n` that travels with every median: project rule 4 -- never a point estimate
    without its count.
    """
    diff = [0] * (len(grid) + 1)
    for a, b, _p in live:
        i = max(0, bisect.bisect_right(grid, a) - 1)
        j = min(len(grid) - 1, bisect.bisect_left(grid, b) - 1)
        if j < i:
            continue
        diff[i] += 1
        diff[j + 1] -= 1
    out, run = [], 0
    for i in range(len(grid)):
        run += diff[i]
        out.append(run)
    return out


def merge_leg_maxima(legs: dict[str, list[tuple[float, float, float]]],
                     ) -> list[tuple[float, float, tuple[float, tuple[str, ...]]]]:
    """The running MAX across several legs, carrying WHICH leg(s) set it.

    Each value in `legs` is that leg's own extremum step function from
    `_extremum_segments` -- sorted, non-overlapping, with no entry for time when
    that leg had nothing standing. The result is
    `[(t0, t1, (value, legs_at_that_value))]` over the time ANY leg stood.

    Why not one sweep over the concatenation: the value would be identical, but
    the answer to "which leg is this number?" would be lost, and that answer is
    the point. The Operator's trait chart reports the group's highest standing
    bid as ONE line whose three legs are three different populations (item bids,
    COVERing trait offers, collection offers). A line that is sometimes one
    population and sometimes another, with nothing on screen saying which, is a
    chart that invites exactly the population-mixing quant §1 metric 1 forbids.

    Ties are reported as a tuple of every leg holding the maximum, not as a
    winner picked by precedence: two legs quoting the same best price is a fact
    about the book, and naming one of them would be an invention.
    """
    bounds = sorted({t for segs in legs.values() for s in segs for t in (s[0], s[1])})
    ptr = dict.fromkeys(legs, 0)
    out: list[tuple[float, float, tuple[float, tuple[str, ...]]]] = []
    for a, b in zip(bounds, bounds[1:], strict=False):   # pairwise over the boundary list
        best: float | None = None
        who: list[str] = []
        for k, segs in legs.items():
            p = ptr[k]
            while p < len(segs) and segs[p][1] <= a:
                p += 1
            ptr[k] = p
            if p < len(segs) and segs[p][0] <= a < segs[p][1]:
                v = segs[p][2]
                if best is None or v > best:
                    best, who = v, [k]
                elif v == best:
                    who.append(k)
        if best is not None and b > a:
            out.append((a, b, (best, tuple(who))))
    return out


def _blank_standing(s: dict[str, Any]) -> None:
    """Blank every value array of a `standing_series` response in place.

    Used where a series is DECLARED undefined -- the filter selects no token, so
    there is no group for the number to be about -- and the arrays the sweep
    produced must not survive the declaration. `n` goes to 0 rather than None:
    "zero orders were in a book about nothing" is true and countable, where a
    null n would read as "we did not look".
    """
    n = len(s.get("keys", []))
    for key in ("median", "p10", "p90", "coverage"):
        if key in s:
            s[key] = [None] * n
    if "n" in s:
        s["n"] = [0] * n
    if "standing_seconds" in s:
        s["standing_seconds"] = [0.0] * n


def time_weighted_quantile(pairs: list[tuple[float, float]], p: float) -> float | None:
    """The p-quantile of a step function, weighted by how long each value held.

    `pairs` is [(seconds, value)]. On a bot-quoted book the instantaneous
    extremum changes many times inside one bucket and a single extreme is not
    the level (quant §1 metric 1): the value that held for most of the bucket is.
    Convention: the smallest value whose cumulative time reaches `p` of the
    total, which for p = 0.5 is the ordinary weighted median.
    """
    if not pairs:
        return None
    agg: dict[float, float] = {}
    for w, v in pairs:
        agg[v] = agg.get(v, 0.0) + w
    total = sum(agg.values())
    if total <= 0:
        return None
    acc, target = 0.0, p * total
    last = None
    for v in sorted(agg):
        acc += agg[v]
        last = v
        if acc >= target - 1e-9:
            return v
    return last


# ---------------------------------------------------------------------------
# survival: Kaplan-Meier with right-censoring, Aalen-Johansen competing risks,
# and a maker-episode cluster bootstrap (quant §3.2, tech-lead PR-8)
#
# Library-free on purpose. The whole estimator is four recurrences and a
# resampling loop; a dependency on lifelines/scipy would put the one number the
# Operator is going to quote behind a version pin nobody in this project reads.
# 1.96 is the only constant, exactly as the quant's §3.2 says.
#
# The four registry entries these read live in `config/assumptions.yaml` as
# ASM-022, and `test_survival_constants_cannot_drift_from_assumptions_yaml`
# pins the two copies together (the same device ASM-021 uses for
# `min_n_for_percentiles`: the register is what a reviewer trusts, so it must
# not be able to disagree with the code).
# ---------------------------------------------------------------------------

# The four ways an order leaves the book. `censored` and `unknown` are NOT
# causes: the first is "still standing as far as we know", the second is a
# terminator we could not place in time (docs/08 §3.3).
SURVIVAL_CAUSES = ("cancelled", "invalidated", "filled", "expired")

# epsilon: the gap that ends a quoting EPISODE (ASM-022). Two quotes from one
# maker on one scope less than this apart are the same episode.
EPISODE_GAP_SECONDS = 60.0

# Cluster-bootstrap replicates, and the seed that makes the band reproducible.
SURVIVAL_BOOTSTRAP_B = 1000
SURVIVAL_BOOTSTRAP_SEED = 20260910

# The binding minimum (quant §3.3): 30 maker-episode CLUSTERS, not 30 orders.
MIN_CLUSTERS_FOR_SURVIVAL = 30

# B x observations before the bootstrap is cut down. 42,601 quotes x 1000
# replicates is ~4e7 resampled observations inside a 10-second dashboard
# refresh; the cut is reported, never silent.
SURVIVAL_BOOTSTRAP_WORK_CAP = 2_000_000

# How many points the band's fixed time grid may have. A band evaluated at
# every distinct event time of a 38k-life sample is 38k x B work and a
# multi-megabyte response.
SURVIVAL_GRID_MAX = 200

# The only constant in the estimator (quant §3.2).
Z95 = 1.96


def episode_ids(lives: list[dict[str, Any]], eps: float = EPISODE_GAP_SECONDS) -> list[str]:
    """One maker-episode id per life: the cluster the bootstrap resamples.

    An EPISODE is *the same maker, the same scope, consecutive quotes less than
    `eps` apart* (quant §2.1). Scope is `(scope_kind, token_id)`: a bot
    requoting one token is one episode, the same bot working a different token
    is another, and a collection offer has no token so every collection offer
    from that maker in one run is one episode.

    Why this matters more than any other number here: 42,601 quotes from 3
    makers are not 42,601 independent observations, and an order-level bootstrap
    would produce a band about sqrt(n / n_eff) -- roughly 15x -- too narrow
    (quant §3.2, factcheck D-W5). `n_eff` is the count of these ids.

    **A life with no maker joins ONE shared cluster, not its own.** That is the
    conservative direction: more clusters means a narrower band, and inventing
    independence we cannot see is exactly the flattering failure the project's
    fifth rule is about. The count is reported as `unclustered_no_maker_n`.
    """
    order = sorted(
        range(len(lives)),
        key=lambda i: (str(lives[i].get("maker") or ""), str(lives[i].get("scope_kind") or ""),
                       str(lives[i].get("token_id") or ""), float(lives[i].get("t_place") or 0.0)))
    out = [""] * len(lives)
    prev_key: tuple[Any, ...] | None = None
    prev_t: float | None = None
    run = 0
    for i in order:
        r = lives[i]
        if not r.get("maker"):
            out[i] = "no-maker|shared"
            continue
        key = (r.get("maker"), r.get("scope_kind"), r.get("token_id"))
        t = float(r.get("t_place") or 0.0)
        if key != prev_key or prev_t is None or (t - prev_t) >= eps:
            run += 1
        out[i] = f"{key[0]}|{key[1]}|{key[2]}|{run}"
        prev_key, prev_t = key, t
    return out


def km_curve(obs: list[tuple[float, bool, str | None]],
             causes: tuple[str, ...] = SURVIVAL_CAUSES) -> dict[str, Any]:
    """Kaplan-Meier + Greenwood + Aalen-Johansen from `[(duration, ended, cause)]`.

    Distinct event times t_1 < ... < t_k. At t_i: `n_i` = at risk just before
    (every observation with duration >= t_i, so an observation censored at
    exactly t_i is still at risk there), `d_i` = all-cause exits, `d_i^c` =
    exits from cause c. Censoring contributes to `n_i` and never to `d_i`.

        S(t)   = PROD_{t_i <= t} (1 - d_i / n_i)
        v(t)   = SUM_{t_i <= t} d_i / (n_i (n_i - d_i))          Greenwood
        Var[S] = S(t)^2 v(t)
        sigma  = sqrt(v(t)) / |ln S(t)|
        CI95   = [ S^exp(+1.96 sigma), S^exp(-1.96 sigma) ]      log-log
        F_c(t) = SUM_{t_i <= t} S(t_{i-1}) d_i^c / n_i           Aalen-Johansen

    The band is **log-log, not S +/- 1.96 SE**: a linear band leaves [0, 1] in
    the tails, and on a curve that reaches 0.02 the tail is the whole story
    (quant §3.2). It is returned as `null` -- never clamped -- where it is
    undefined: at S = 1 (ln S = 0), at S = 0, and at any t_i where n_i = d_i
    (Greenwood's denominator vanishes and the variance is infinite from there
    on). A clamped band would draw a confident line where there is no estimate.

    **The Greenwood band assumes independent observations, which quotes from
    three bots are not.** It travels here as a diagnostic; the band the panel
    draws is the cluster bootstrap. Where they disagree, Greenwood is the one
    that is wrong on this data (factcheck D-W5).

    SUM_c F_c(t) = 1 - S(t) exactly, at every t, and that is a test, not a
    hope: S(t_{i-1}) - S(t_i) = S(t_{i-1}) d_i / n_i telescopes to 1 - S(t).
    1 - KM per cause does NOT have this property and overstates each cause.

    The curve starts at (0, 1) so a step plot has somewhere to start.
    """
    durs = sorted(d for d, _e, _c in obs)
    n = len(durs)
    t_out: list[float] = [0.0]
    s_out: list[float] = [1.0]
    lo_out: list[float | None] = [1.0]
    hi_out: list[float | None] = [1.0]
    risk_out: list[int] = [n]
    d_out: list[int] = [0]
    cif: dict[str, list[float]] = {c: [0.0] for c in causes}
    if n == 0:
        return {"t": t_out, "s": s_out, "greenwood_lower": lo_out, "greenwood_upper": hi_out,
                "n_at_risk": risk_out, "d": d_out, "cif": cif, "n": 0, "events": 0}
    ended: dict[float, int] = {}
    by_cause: dict[float, dict[str, int]] = {}
    for d, e, c in obs:
        if not e:
            continue
        ended[d] = ended.get(d, 0) + 1
        if c in causes:
            by_cause.setdefault(d, {})[c] = by_cause.setdefault(d, {}).get(c, 0) + 1
    s = 1.0
    v = 0.0
    v_infinite = False
    run = {c: 0.0 for c in causes}
    for ti in sorted(ended):
        n_i = n - bisect.bisect_left(durs, ti)
        if n_i <= 0:                       # unreachable: an event time has at least itself at risk
            continue
        d_i = ended[ti]
        s_prev = s
        for c, dc in (by_cause.get(ti) or {}).items():
            run[c] += s_prev * dc / n_i
        s = s_prev * (1.0 - d_i / n_i)
        if n_i > d_i and not v_infinite:
            v += d_i / (n_i * (n_i - d_i))
        else:
            v_infinite = True
        if v_infinite or s <= 0.0 or s >= 1.0:
            lo: float | None = None
            hi: float | None = None
        else:
            sigma = math.sqrt(v) / abs(math.log(s))
            lo = s ** math.exp(Z95 * sigma)
            hi = s ** math.exp(-Z95 * sigma)
        t_out.append(float(ti))
        s_out.append(s)
        lo_out.append(lo)
        hi_out.append(hi)
        risk_out.append(n_i)
        d_out.append(d_i)
        for c in causes:
            cif[c].append(run[c])
    return {"t": t_out, "s": s_out, "greenwood_lower": lo_out, "greenwood_upper": hi_out,
            "n_at_risk": risk_out, "d": d_out, "cif": cif,
            "n": n, "events": sum(ended.values())}


def step_at(t_grid: list[float], values: list[float | None], t: float) -> float | None:
    """The value of a right-continuous step function at `t`: the last point at or before it."""
    if not t_grid:
        return None
    i = bisect.bisect_right(t_grid, t) - 1
    return values[i] if i >= 0 else None


def rmst(t_grid: list[float], s: list[float], tau: float) -> float:
    """RMST(tau) = INT_0^tau S(u) du = SUM_i S(t_{i-1}) (min(t_i, tau) - t_{i-1}).

    The area under a step function, which is a sum of rectangles and nothing
    more. The caller is responsible for not asking past the data: `survival()`
    refuses a tau beyond the largest observed duration rather than extending the
    last step flat, because that is imputing (docs/06 §4.3).
    """
    total = 0.0
    prev_t, prev_s = 0.0, 1.0
    for ti, si in zip(t_grid, s, strict=True):
        if ti <= 0.0:
            prev_s = si
            continue
        hi = min(ti, tau)
        if hi > prev_t:
            total += prev_s * (hi - prev_t)
        prev_t, prev_s = hi, si
        if ti >= tau:
            break
    if prev_t < tau:
        total += prev_s * (tau - prev_t)
    return total


def _quantile(sorted_values: list[float], p: float) -> float | None:
    """The p-quantile of a sorted list, with NO minimum-n refusal.

    Deliberately not `pct()`. `pct()` refuses below `MIN_N_FOR_PERCENTILES`
    because its input is a sample of OBSERVATIONS and REQ-F-19 is about
    quoting an unreliable percentile of the market. This one's input is a list
    of B bootstrap REPLICATES; B is a compute budget we chose, not evidence,
    and letting a large B satisfy an observation-count guard would launder the
    small-n problem into a tight band. The guard that applies to a bootstrap
    band is `n_eff >= MIN_CLUSTERS_FOR_SURVIVAL`, and it is applied by the
    caller, on clusters.
    """
    n = len(sorted_values)
    if n == 0:
        return None
    return sorted_values[min(n - 1, max(0, int(p * n)))]


def survival_grid(t_events: list[float], max_points: int = SURVIVAL_GRID_MAX) -> list[float]:
    """A fixed time grid for the bootstrap band: every event time, or a log-spaced
    subset of them when there are too many. Log-spaced because lifetimes span
    seconds to hours and the panel's x-axis is logarithmic (design §4.1a)."""
    pts = sorted({t for t in t_events if t > 0})
    if len(pts) <= max_points:
        return pts
    lo, hi = math.log(pts[0]), math.log(pts[-1])
    want = [math.exp(lo + (hi - lo) * i / (max_points - 1)) for i in range(max_points)]
    out: list[float] = []
    for w in want:
        i = min(len(pts) - 1, bisect.bisect_left(pts, w))
        if not out or pts[i] != out[-1]:
            out.append(pts[i])
    return out


def downsample_km(km: dict[str, Any], max_points: int = 2 * SURVIVAL_GRID_MAX,
                  causes: tuple[str, ...] = SURVIVAL_CAUSES) -> dict[str, Any]:
    """The curve thinned onto its own grid for TRANSPORT, never for computation.

    A 40,000-life sample has ~40,000 distinct event times, and the response
    carries seven arrays of that length (t, S, the two Greenwood bounds,
    n_at_risk, d, and one CIF per cause) -- about 6 MB of JSON for a panel
    1,100 px wide (BUG-20260910-064). The points are thinned log-spaced, which
    is the axis the panel draws on, so what is dropped is invisible.

    **Every retained point is an ACTUAL point of the estimate, not an
    interpolation**: this selects indices, it never averages or resamples. t = 0
    and the final step are always kept, so `S` still ends where it ends and
    `SUM_c F_c = 1 - S` still holds exactly at every point that survives.

    Percentiles, RMST, the residual survivals and the bootstrap are all computed
    from the FULL curve before this runs -- thinning the curve and then reading a
    median off it would snap the median to a grid point.
    """
    t = km["t"]
    if len(t) <= max_points:
        return {**km, "points": len(t), "event_times": len(t) - 1, "downsampled": False}
    keep = [0]
    for g in survival_grid(t, max_points):
        i = bisect.bisect_left(t, g)
        if i < len(t) and i != keep[-1]:
            keep.append(i)
    if keep[-1] != len(t) - 1:
        keep.append(len(t) - 1)
    pick = lambda arr: [arr[i] for i in keep]      # noqa: E731 - one expression, used seven times
    return {**km,
            "t": pick(t), "s": pick(km["s"]),
            "greenwood_lower": pick(km["greenwood_lower"]), "greenwood_upper": pick(km["greenwood_upper"]),
            "n_at_risk": pick(km["n_at_risk"]), "d": pick(km["d"]),
            "cif": {c: pick(km["cif"][c]) for c in causes},
            "points": len(keep), "event_times": len(t) - 1, "downsampled": True}


def cluster_bootstrap(obs_by_cluster: dict[str, list[tuple[float, bool, str | None]]],
                      grid: list[float], *, b: int = SURVIVAL_BOOTSTRAP_B,
                      seed: int = SURVIVAL_BOOTSTRAP_SEED,
                      causes: tuple[str, ...] = SURVIVAL_CAUSES) -> dict[str, Any]:
    """2.5/97.5 percentile bands for S and every CIF, resampling MAKER-EPISODES.

    Resample the clusters with replacement (as many as there are), pool their
    observations, refit KM/AJ, and read the band off the replicate
    distribution on a fixed grid. Resampling individual ORDERS would be the
    wrong bootstrap: orders inside one bot episode are not independent, and the
    band would come out roughly sqrt(n / n_eff) too narrow (quant §3.2).

    Deterministic: `random.Random(seed)` and nothing else, so the band on the
    page is the band in the test and a re-run cannot quietly move it.
    """
    import random

    keys = list(obs_by_cluster)
    if not keys or not grid or b <= 0:
        return {"s_lower": [None] * len(grid), "s_upper": [None] * len(grid),
                "cif_lower": {c: [None] * len(grid) for c in causes},
                "cif_upper": {c: [None] * len(grid) for c in causes},
                "b_used": 0, "seed": seed}
    rng = random.Random(seed)
    k = len(keys)
    s_draws: list[list[float]] = [[] for _ in grid]
    c_draws: dict[str, list[list[float]]] = {c: [[] for _ in grid] for c in causes}
    for _ in range(b):
        sample: list[tuple[float, bool, str | None]] = []
        for _ in range(k):
            sample.extend(obs_by_cluster[keys[rng.randrange(k)]])
        cur = km_curve(sample, causes)
        for gi, g in enumerate(grid):
            i = bisect.bisect_right(cur["t"], g) - 1
            s_draws[gi].append(cur["s"][i] if i >= 0 else 1.0)
            for c in causes:
                c_draws[c][gi].append(cur["cif"][c][i] if i >= 0 else 0.0)
    def band(draws: list[list[float]]) -> tuple[list[float | None], list[float | None]]:
        lo: list[float | None] = []
        hi: list[float | None] = []
        for col in draws:
            col.sort()
            lo.append(_quantile(col, 0.025))
            hi.append(_quantile(col, 0.975))
        return lo, hi
    s_lo, s_hi = band(s_draws)
    cif_lo: dict[str, list[float | None]] = {}
    cif_hi: dict[str, list[float | None]] = {}
    for c in causes:
        cif_lo[c], cif_hi[c] = band(c_draws[c])
    return {"s_lower": s_lo, "s_upper": s_hi, "cif_lower": cif_lo, "cif_upper": cif_hi,
            "b_used": b, "seed": seed, "clusters_resampled": k}


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------
class MetricEngine:
    def __init__(self, conn: sqlite3.Connection, intervals: dict[str, Any], tz_name: str) -> None:
        self.conn = conn
        self.intervals = intervals
        self.tz = tz_name

    def _bucketed(self, metric: str, collection: str, denom: str, start: float, end: float,
                  interval: dict[str, Any], traits: dict[str, list[str]] | None = None) -> dict[float, float]:
        spec = METRICS[metric]
        col = "price_usd" if denom == "USD" else "price_eth"
        where = ["e.collection = ?", "e.valid_ts >= ?", "e.valid_ts < ?"]
        args: list[Any] = [collection, start, end]
        if spec["types"]:
            where.append(f"e.event_type IN ({','.join('?' * len(spec['types']))})")
            args.extend(spec["types"])
        if spec["price"]:
            where.append(f"e.{col} IS NOT NULL")
        tf, targs = token_filter_sql(collection, traits or {})
        # Under a trait filter, three kinds of event exist:
        #   token-level (listing, item bid, sale, cancel...) -> must match every clause;
        #   collection_offer (no token_id) -> a bid on EVERY token (C = {}), so it passes;
        #   trait_offer (no token_id) -> passes iff its STORED criteria COVER the
        #     filter (dataeng §4.3). Until 2026-09-09 the criteria were parsed by
        #     nothing and every trait offer was excluded under every filter
        #     (BUG-045); that blanket rule is now an evidence-based verdict, and
        #     the offers it still excludes -- unparsed (criteria_n = 0) and
        #     numeric-criteria -- are counted by trait_offer_verdicts (BUG-051).
        if tf and filter_narrows(spec):
            # BUG-20260910-060. Each disjunct below is right on its own and the
            # SET of them is wrong when the filter selects NO token: a bid on
            # every token is not a bid on any token of an empty set, but
            # `S(F) subset of S(C)` is vacuously true for `S(F) = {}`, so the
            # collection-offer and COVERing-trait-offer branches kept matching.
            # `bid_count` and `event_count` under an impossible filter therefore
            # counted bids on tokens the filter does not select -- and while the
            # `traits` table is empty EVERY filter is impossible, so that was
            # 100% of the filtered bid count. Return nothing; `series()` turns
            # that into a null on every bucket rather than a zero (a zero would
            # say "nothing happened to this trait group", and the truth is that
            # there is no trait group).
            if self._token_set_size(collection, traits or {}) == 0:
                return {}
            clauses = tf[len(" AND "):]                     # "A AND B AND C"
            cover, cargs = criteria_cover_sql("e", traits or {})
            where.append(f"((e.token_id IS NULL AND e.event_type = 'collection_offer')"
                         f" OR (e.event_type = 'trait_offer' AND {cover})"
                         f" OR ({clauses}))")
            args.extend(cargs)
            args.extend(targs)
        sql = f"SELECT e.valid_ts, e.{col} FROM events e WHERE {' AND '.join(where)} ORDER BY e.valid_ts"
        groups: dict[float, list[float]] = {}
        for ts, v in self.conn.execute(sql, args):
            b = bucket_of(ts, interval, self.tz)
            groups.setdefault(b, []).append(v if spec["price"] else 1.0)
        agg = spec["agg"]
        out: dict[float, float] = {}
        for b, vals in groups.items():
            if agg == "MAX":
                out[b] = max(vals)
            elif agg == "MIN":
                out[b] = min(vals)
            elif agg == "SUM":
                out[b] = sum(vals)
            elif agg == "COUNT":
                out[b] = float(len(vals))
            elif agg == "MEDIAN":
                s = sorted(vals)
                n = len(s)
                out[b] = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        return out

    # -- the standing book ---------------------------------------------------
    def _standing_live(self, kind: str, collection: str, denom: str, start: float,
                       horizon: float, traits: dict[str, list[str]] | None,
                       ) -> list[tuple[float, float, float]]:
        """Every order of `kind` that stood at any point in [start, horizon), as
        [(t_from, t_to, price)] clipped to the window.

        Read off `order_lives`, not `events`: one row per order_hash, with the
        duplicate cancel collapsed, the revalidate honoured and the untimed
        terminator marked `unknown` (BUG-049/050). The predicate here is
        `standing_sql`'s, expressed as an interval rather than a point:

            t_place <= tau  AND  exit_reason <> 'unknown'
            AND (t_term IS NULL OR t_term > tau)
            AND (expiration_ts IS NULL OR expiration_ts > tau)

        so the interval is [t_place, min(t_term, expiration_ts)). An order with
        neither stands to `horizon` and no further -- see `_bucket_windows`.

        A trait filter narrows the token-scoped kinds only. A collection offer
        carries no token and bids on every one of them, so it passes every
        filter; that is not a special case, it is what `C = {}` means.
        """
        spec = STANDING_KINDS[kind]
        col = "price_usd" if denom == "USD" else "price_eth"
        where = ["ol.collection = ?", "ol.event_type = ?", "ol.placement_seen = 1",
                 "ol.t_place IS NOT NULL", "ol.t_place < ?", "ol.exit_reason <> 'unknown'",
                 f"ol.{col} IS NOT NULL",
                 "(ol.t_term IS NULL OR ol.t_term > ?)",
                 "(ol.expiration_ts IS NULL OR ol.expiration_ts > ?)"]
        args: list[Any] = [collection, spec["event_type"], horizon, start, start]
        if traits and spec["token_scoped"]:
            tf, targs = token_filter_sql(collection, traits, alias="ol")
            where.append(tf[len(" AND "):])
            args.extend(targs)
        if spec.get("cover_scoped"):
            # `order_lives` is keyed on order_hash and carries no (run, seq), so the
            # criteria join goes through the placement row in `events`. The predicate
            # itself is `criteria_cover_sql` unchanged -- one rule, one guard, one
            # place it is written (BUG-051). Applied UNCONDITIONALLY, including under
            # an empty filter, where it correctly matches nothing.
            cover, cargs = criteria_cover_sql("e", traits or {})
            where.append(f"EXISTS (SELECT 1 FROM events e WHERE e.order_hash = ol.order_hash"
                         f" AND e.event_type = 'trait_offer' AND {cover})")
            args.extend(cargs)
        rows = self.conn.execute(
            f"SELECT ol.t_place, ol.t_term, ol.expiration_ts, ol.{col} FROM order_lives ol "
            f"WHERE {' AND '.join(where)}", args)
        live: list[tuple[float, float, float]] = []
        for t_place, t_term, exp_ts, price in rows:
            ends = [x for x in (t_term, exp_ts) if x is not None]
            t_end = min(ends) if ends else horizon
            a, b = max(t_place, start), min(t_end, horizon)
            if b > a:
                live.append((a, b, price))
        return live

    def standing_series(self, kind: str, collection: str, start: float, end: float,
                        interval: str | dict[str, Any], denom: str = "ETH",
                        traits: dict[str, list[str]] | None = None,
                        now: datetime | None = None) -> dict[str, Any]:
        """The standing book's extremum, per bucket, time-weighted.

        For every bucket on the FULL grid of [start, end): the time-weighted
        median of the lowest standing ask (or the highest standing bid) over the
        bucket, with [p10, p90], `coverage` = seconds the leg stood / observable
        bucket seconds, and `n` = distinct orders that were standing.

        The book is sampled at every event time inside the bucket -- a sweep line
        over placements and terminations -- which is both cheaper and strictly
        more informative than sampling at bucket boundaries. Boundary sampling
        would miss a listing that appeared and was cancelled inside one bucket,
        which on this collection is the majority of them (median bid life 9 s).

        **Median vs percentiles, and why they are treated differently.** The
        median here is the LEVEL of a continuously observed step function -- the
        price that was actually standing for most of the bucket. It is a fact
        with n = 1 (one listing stood, at that price, for that long) and is
        reported. [p10, p90] is a claim about the DISTRIBUTION of that level
        across the bucket, and quant §2.3 is explicit that machine quotes are not
        independent observations; below `MIN_N_FOR_PERCENTILES` distinct orders
        it is refused, not flagged (REQ-F-19). Suppressed buckets are counted in
        the basis.

        Nulls where nothing stood. Never zero: zero is a price, and "no standing
        ask" is not the price zero (Operator's decision, 2026-09-10).
        """
        if kind not in STANDING_KINDS:
            raise ValueError(f"unknown standing kind {kind!r}; one of {sorted(STANDING_KINDS)}")
        if denom not in DENOMS:
            raise ValueError(f"unknown denomination {denom!r}; one of {DENOMS}")
        ispec = self.intervals["intervals"][interval] if isinstance(interval, str) else interval
        now_ts = (now or datetime.now(timezone.utc)).timestamp()
        horizon = min(end, now_ts)
        grid = bucket_grid(start, end, ispec, self.tz)
        spec = STANDING_KINDS[kind]
        live = self._standing_live(kind, collection, denom, start, horizon, traits)
        windows = _bucket_windows(grid, start, horizon, ispec, self.tz)
        segs = _extremum_segments(live, spec["want"] == "min")
        per = _accumulate(segs, grid, windows)
        counts = _distinct_per_bucket(live, grid)

        median: list[float | None] = []
        p10: list[float | None] = []
        p90: list[float | None] = []
        coverage: list[float | None] = []
        stood: list[float] = []
        suppressed = 0
        for i in range(len(grid)):
            pairs = per.get(i, [])
            secs = sum(w for w, _ in pairs)
            width = max(0.0, windows[i][1] - windows[i][0])
            stood.append(secs)
            coverage.append((secs / width) if width > 0 else None)
            median.append(time_weighted_quantile(pairs, 0.5))
            if counts[i] >= MIN_N_FOR_PERCENTILES:
                p10.append(time_weighted_quantile(pairs, 0.10))
                p90.append(time_weighted_quantile(pairs, 0.90))
            else:
                if pairs:
                    suppressed += 1
                p10.append(None)
                p90.append(None)
        return {
            "keys": grid,
            "t": [datetime.fromtimestamp(k, tz=timezone.utc).isoformat() for k in grid],
            "median": median, "p10": p10, "p90": p90,
            "coverage": coverage, "n": counts,
            "standing_seconds": stood,
            "bucket_seconds": [max(0.0, hi - lo) for lo, hi in windows],
            "basis": {
                "book": "standing", "kind": kind, "leg": spec["label"],
                "event_type": spec["event_type"],
                "orders_in_window": len(live),
                "aggregation": "time-weighted median over the bucket, sampled at every "
                               "placement and termination inside it",
                "coverage_denominator": "observable bucket seconds = the bucket clipped to the "
                                        "query window and to now; nothing past now is observable",
                "percentiles_min_n": MIN_N_FOR_PERCENTILES,
                "percentiles_suppressed_buckets": suppressed,
                "left_truncated": True,
                "left_truncation_note": "the standing book contains only orders whose PLACEMENT we "
                                        "witnessed; orders resting before recording started are "
                                        "invisible, so a reconstructed floor is an upper bound and "
                                        "a reconstructed best bid a lower one (quant §0, Q-V9)",
                "as_of": datetime.fromtimestamp(horizon, tz=timezone.utc).isoformat(),
            },
        }

    def standing_spread(self, collection: str, start: float, end: float,
                        interval: str | dict[str, Any], denom: str = "ETH",
                        traits: dict[str, list[str]] | None = None,
                        now: datetime | None = None) -> dict[str, Any]:
        """`immediacy_cost` as docs/01 §3.2 defines it: a STANDING-book spread.

            spread(tau) = lowest standing ask(tau) - highest standing collection offer(tau)

        Both legs on the SAME tau. Reported per bucket as the time-weighted
        median with [p10, p90], with `coverage` = both-legs seconds / observable
        bucket seconds, and null wherever either leg was absent -- "no bid at any
        price" is the liquidity fact, not a missing pixel.

        **Leg discipline (quant §1 metric 1).** The bid leg is a COLLECTION offer
        and nothing else. A collection offer carries no token and applies to
        every one of them, so it is by construction a bid available on whichever
        token is at the floor. Item bids and trait offers are per-token and
        per-criteria: maxing over them would pair the ask on one token with the
        bid on another, and "mixing those populations is how a spread goes
        negative." A bid leg that unions all three is a different metric (PR-5).

        **A negative spread is an alarm, not a data point.** If any tau inside a
        bucket has ask < bid, the whole bucket is null and counted in
        `negative_buckets`: a crossed book means the reconstruction is wrong (a
        stale ask, a misparsed price, a left-truncated leg), and publishing the
        median of the remaining tau would hide it behind a plausible number.
        Project rule 5 -- a surprisingly good result is evidence of a bug.
        """
        ispec = self.intervals["intervals"][interval] if isinstance(interval, str) else interval
        now_ts = (now or datetime.now(timezone.utc)).timestamp()
        horizon = min(end, now_ts)
        grid = bucket_grid(start, end, ispec, self.tz)
        windows = _bucket_windows(grid, start, horizon, ispec, self.tz)
        ask_live = self._standing_live("ask", collection, denom, start, horizon, traits)
        bid_live = self._standing_live("collection_bid", collection, denom, start, horizon, traits)
        ask_segs = _extremum_segments(ask_live, True)
        bid_segs = _extremum_segments(bid_live, False)
        joint = _join_segments(ask_segs, bid_segs)

        spread_segs = [(t0, t1, a - b) for t0, t1, a, b in joint]
        ask_on_joint = [(t0, t1, a) for t0, t1, a, _b in joint]
        bid_on_joint = [(t0, t1, b) for t0, t1, _a, b in joint]
        pct_segs = [(t0, t1, (a - b) / a) for t0, t1, a, b in joint if a]
        neg_segs = [(t0, t1, 1.0) for t0, t1, a, b in joint if a - b < 0]

        acc_spread = _accumulate(spread_segs, grid, windows)
        acc_ask = _accumulate(ask_on_joint, grid, windows)
        acc_bid = _accumulate(bid_on_joint, grid, windows)
        acc_pct = _accumulate(pct_segs, grid, windows)
        acc_neg = _accumulate(neg_segs, grid, windows)
        n_ask = _distinct_per_bucket(ask_live, grid)
        n_bid = _distinct_per_bucket(bid_live, grid)

        raw: list[float | None] = []
        p10: list[float | None] = []
        p90: list[float | None] = []
        coverage: list[float | None] = []
        pctv: list[float | None] = []
        leg_ask: list[float | None] = []
        leg_bid: list[float | None] = []
        negative_buckets = 0
        negative_starts: list[float] = []
        suppressed = 0
        for i in range(len(grid)):
            pairs = acc_spread.get(i, [])
            width = max(0.0, windows[i][1] - windows[i][0])
            both = sum(w for w, _ in pairs)
            coverage.append((both / width) if width > 0 else None)
            if acc_neg.get(i):
                negative_buckets += 1
                negative_starts.append(grid[i])
                for arr in (raw, p10, p90, pctv, leg_ask, leg_bid):
                    arr.append(None)
                continue
            raw.append(time_weighted_quantile(pairs, 0.5))
            leg_ask.append(time_weighted_quantile(acc_ask.get(i, []), 0.5))
            leg_bid.append(time_weighted_quantile(acc_bid.get(i, []), 0.5))
            pctv.append(time_weighted_quantile(acc_pct.get(i, []), 0.5))
            n_eff = min(n_ask[i], n_bid[i])
            if n_eff >= MIN_N_FOR_PERCENTILES:
                p10.append(time_weighted_quantile(pairs, 0.10))
                p90.append(time_weighted_quantile(pairs, 0.90))
            else:
                if pairs:
                    suppressed += 1
                p10.append(None)
                p90.append(None)
        iv_id = interval if isinstance(interval, str) else str(ispec.get("id", "?"))
        # One log line per CROSSED BUCKET, not per (collection, interval, denom).
        # Re-rendering the same crossed bucket every 10 s stays silent; a bucket
        # that crosses for the first time hours later always speaks up.
        fresh = [b for b in negative_starts
                 if (collection, iv_id, denom, b) not in _NEGATIVE_LOGGED]
        if fresh:
            for b in fresh:
                _NEGATIVE_LOGGED.add((collection, iv_id, denom, b))
            log.warning(
                "immediacy_cost: %d newly-seen bucket(s) had a CROSSED standing book (ask < bid) "
                "for %s at %s/%s, starting %s -- returned as null, not charted. A crossed book is "
                "a reconstruction defect (stale ask, misparsed price, left-truncated leg), never "
                "an arbitrage. BUG-20260910-057.",
                len(fresh), collection, iv_id, denom,
                ", ".join(datetime.fromtimestamp(b, tz=timezone.utc).isoformat() for b in fresh[:8])
                + (" ..." if len(fresh) > 8 else ""))
        return {
            "keys": grid,
            "t": [datetime.fromtimestamp(k, tz=timezone.utc).isoformat() for k in grid],
            "median": raw, "p10": p10, "p90": p90, "coverage": coverage,
            "pct_of_ask": pctv, "leg_ask": leg_ask, "leg_bid": leg_bid,
            "n_ask": n_ask, "n_bid": n_bid,
            "basis": {
                "book": "standing", "interval_id": iv_id,
                "negative_buckets": negative_buckets,
                "percentiles_min_n": MIN_N_FOR_PERCENTILES,
                "percentiles_suppressed_buckets": suppressed,
                "coverage_denominator": "both-legs seconds / observable bucket seconds",
                "left_truncated": True,
                "as_of": datetime.fromtimestamp(horizon, tz=timezone.utc).isoformat(),
            },
        }

    # -- PR-5: the trait chart's metric layer --------------------------------
    def trait_set_series(self, collection: str, traits: dict[str, list[str]] | None,
                         start: float, end: float, interval: str | dict[str, Any],
                         denom: str = "ETH", now: datetime | None = None) -> dict[str, Any]:
        """Everything the Operator's trait chart draws, on one bucket grid.

        The Operator decided this shape on 2026-09-10 and it is not a menu:

        **Under a SINGLE-clause filter** the chart shows exactly three things --
        the trait group's HIGHEST standing bid, the trait group's LOWEST standing
        ask, and the collection floor pale and dotted underneath. The highest
        standing bid is the max, at each tau, over the union of three legs:

          * item bids on tokens in `S(F)`,
          * trait offers whose stored criteria **COVER** F (dataeng §4.3),
          * collection offers (`C = {}` reaches every token, so they reach `S(F)`).

        Each leg is reported with its own `n` and its own kind, and `winning_leg`
        names the leg that actually set the number in each bucket. They are three
        different populations; the union max is a legitimate "best bid available
        to a holder of this trait", but a *sum* of them would not be depth and a
        line that silently swaps population would be the leg-mixing quant §1
        metric 1 forbids. Hence: one line, three counts, and a name.

        **Under a MULTI-clause filter** it shows the combined (AND) trait floor,
        each single-clause floor, and the unfiltered baseline floor.

        Everything is a STANDING quantity (`order_lives`), because a floor you
        cannot hit is not a floor (tech-lead C5, Operator's decision). Every
        series is on the FULL bucket grid and a bucket where nothing stood is
        `None`. **Never `0`.** Zero is a price; "the AND-set had no standing ask
        in this hour" is not the price zero, and the difference is the whole
        reason the chart is worth drawing on a book this thin.

        No REST. Every number here comes off the stream-derived store.
        """
        if denom not in DENOMS:
            raise ValueError(f"unknown denomination {denom!r}; one of {DENOMS}")
        # Refuse an unknown interval the same way `series()` does, and name the
        # valid ids. A bare KeyError here reached the page as a 500 with no body
        # worth reading; the handler turns a ValueError into a 400 that says what
        # to ask for instead (REQ-N-09: an interval not in intervals.yaml is
        # refused, not improvised).
        if isinstance(interval, str) and interval not in self.intervals["intervals"]:
            raise ValueError(f"unknown interval {interval!r}; "
                             f"one of {sorted(self.intervals['intervals'])}")
        traits = traits or {}
        ispec = self.intervals["intervals"][interval] if isinstance(interval, str) else interval
        iv_id = interval if isinstance(interval, str) else str(ispec.get("id", "?"))
        now_dt = now or datetime.now(timezone.utc)
        now_ts = now_dt.timestamp()
        horizon = min(end, now_ts)
        grid = bucket_grid(start, end, ispec, self.tz)
        windows = _bucket_windows(grid, start, horizon, ispec, self.tz)

        # The three ask lines are `standing_series` verbatim with a different token
        # set: the AND-set, each single clause, and no filter at all. Nothing about
        # a "trait floor" differs from a floor except which tokens are in scope, so
        # nothing about it should differ in the code either.
        trait_ask = self.standing_series("ask", collection, start, end, ispec, denom,
                                         traits or None, now_dt)
        baseline_ask = self.standing_series("ask", collection, start, end, ispec, denom,
                                            None, now_dt)
        single_floors: list[dict[str, Any]] = []
        if len(traits) >= 2:
            for t, vals in traits.items():
                s = self.standing_series("ask", collection, start, end, ispec, denom,
                                         {t: vals}, now_dt)
                single_floors.append({"clause": t, "values": list(vals),
                                      "matching_tokens": self._token_set_size(collection, {t: vals}),
                                      **s})

        # -- the union bid leg ------------------------------------------------
        live = {
            "item": self._standing_live("item_bid", collection, denom, start, horizon, traits or None),
            "trait_offer": self._standing_live("trait_offer_cover", collection, denom, start,
                                               horizon, traits or None),
            "collection": self._standing_live("collection_bid", collection, denom, start,
                                              horizon, None),
        }
        per_leg = {k: _extremum_segments(v, False) for k, v in live.items()}
        union = merge_leg_maxima(per_leg)
        acc = _accumulate(union, grid, windows)
        n_leg = {k: _distinct_per_bucket(v, grid) for k, v in live.items()}

        bid: list[float | None] = []
        bid_cov: list[float | None] = []
        winning: list[str | None] = []
        for i in range(len(grid)):
            pairs = acc.get(i, [])
            width = max(0.0, windows[i][1] - windows[i][0])
            secs = sum(w for w, _ in pairs)
            bid_cov.append((secs / width) if width > 0 else None)
            v = time_weighted_quantile([(w, pv) for w, (pv, _who) in pairs], 0.5)
            bid.append(v)
            if v is None:
                winning.append(None)
                continue
            # Which leg is the number ON SCREEN? Not "which leg was highest most
            # often" -- the leg that held the reported level, for the longest.
            held: dict[str, float] = {}
            for w, (pv, who) in pairs:
                if pv == v:
                    for k in who:
                        held[k] = held.get(k, 0.0) + w
            top = max(held.values()) if held else 0.0
            winning.append("+".join(sorted(k for k, s in held.items() if s == top)) or None)

        verdicts = self.trait_offer_verdicts(collection, traits or None, start, end)
        matching = self._token_set_size(collection, traits)
        # S(F) = {} -- the filter selects no token at all. There is no trait group, so
        # there is no "highest bid available to a holder of this trait", and every
        # series about the group is undefined.
        #
        # The BID leg was the obvious one: a collection offer is not token-scoped and
        # covers the empty set vacuously, so without a guard the panel drew a
        # confident bid line for a trait group with zero members (BUG-20260910-059).
        #
        # The ASK legs are the subtle one and were left unguarded on the assumption
        # that "no tokens means no listings" (BUG-20260910-061). That assumption is
        # a claim that `tokens` and `traits` agree, and nothing enforces it:
        # `_standing_live`'s token filter reads `traits`, while |S(F)| is counted over
        # `tokens`. A populated `traits` table with an empty token list -- which is a
        # real intermediate state of the trait onboarding -- gave |S(F)| = 0 and a
        # drawn ask line at the same time. Every series is blanked explicitly now.
        #
        # Each series is guarded on ITS OWN token set, not on the combined one: a
        # single-clause floor whose own clause selects tokens is a real number and is
        # exactly what the multi-clause panel exists to show when the AND-set is
        # empty. Blanking it because the INTERSECTION is empty would delete the
        # answer to the question the panel is asking.
        empty_set = bool(traits) and matching == 0
        if empty_set:
            bid = [None] * len(grid)
            winning = [None] * len(grid)
            _blank_standing(trait_ask)
        for f in single_floors:
            if f["matching_tokens"] == 0:
                _blank_standing(f)
        observed = sum(1 for v in trait_ask["median"] if v is not None)
        legs_note = {k: TRAIT_BID_LEGS[k] for k in ("item", "trait_offer", "collection")}
        return {
            "keys": grid,
            "t": [datetime.fromtimestamp(k, tz=timezone.utc).isoformat() for k in grid],
            "trait_ask": trait_ask,
            "trait_bid": {
                "median": bid, "coverage": bid_cov, "winning_leg": winning,
                "n_item": n_leg["item"],
                "n_trait_offer_cover": n_leg["trait_offer"],
                "n_collection": n_leg["collection"],
                "legs": legs_note,
            },
            "baseline_ask": baseline_ask,
            "single_floors": single_floors,
            "matching_tokens": matching,
            "partial_offers": verdicts["partial_n"],
            "partial_detail": verdicts["partial"],
            "partial_detail_cap": verdicts.get("partial_detail_cap"),
            "partial_truncated": verdicts.get("partial_truncated"),
            "unknown_offers": verdicts["unknown_numeric"],
            "unparsed_offers": verdicts["unparsed"],
            "basis": {
                "book": "standing",
                "kind": "trait_set",
                "interval_id": iv_id,
                "denomination": denom,
                "trait_filter": traits,
                "clauses": len(traits),
                "mode": "multi" if len(traits) >= 2 else "single",
                "matching_tokens": matching,
                "empty_token_set": empty_set,
                "empty_token_set_note": (
                    "this filter selects NO token, so there is no trait group and every series "
                    "about it is undefined -- the ask, the bid, and every single-clause floor "
                    "whose own clause selects nothing. The bid would otherwise show the "
                    "collection offer, a bid on tokens this filter does not select. The per-leg "
                    "counts still report what was standing. |S(F)| is counted over the TOKENS "
                    "table, so the first thing to check is whether the token list is loaded at "
                    "all -- a populated `traits` table with an empty `tokens` table gives "
                    "|S(F)| = 0 too, and both are filled by the same onboarding run "
                    "(traits.command / import-traits.command)."
                    if empty_set else ""),
                "token_set_universe": "tokens",
                "buckets": len(grid),
                "observed_buckets": observed,
                "undefined_buckets": len(grid) - observed,
                "legs": legs_note,
                "bid_rule": ("highest standing bid = MAX at each tau over the union of the three "
                             "legs above. The three are never summed: they are three populations, "
                             "and a sum of them is not depth (dataeng §4.3, quant §1 metric 1)."),
                "ask_rule": ("lowest STANDING ask over S(F). A bucket with no standing ask is null, "
                             "never 0 and never the last price seen (Operator, 2026-09-10)."),
                "partial_offers": verdicts["partial_n"],
                "partial_note": ("PARTIAL offers overlap S(F) without covering it. They are counted "
                                 "and reported with |S(F) n S(C)| and |S(F)|, and are NEVER summed "
                                 "into the bid leg -- allocating a fraction of their quantity as "
                                 "depth is a judgement and belongs in ANALYSIS (docs/06 §4)."),
                "unknown_offers": verdicts["unknown_numeric"],
                "unparsed_offers": verdicts["unparsed"],
                "unknown_note": ("numeric criteria cannot be evaluated against the string `traits` "
                                 "table, so their verdict is UNKNOWN -- excluded AND counted, never "
                                 "TRUE. `unparsed` (criteria_n = 0) is the loud-failure marker and "
                                 "is counted separately: summing the two would blur a parser gap "
                                 "into a schema limit."),
                "gap_masked": False,
                "gap_note": ("ingestion-gap masking is applied by series() and is NOT applied here; "
                             "the page shades gap spans on the plot. A bucket inside a gap shows "
                             "what the reconstructed book held, which during a gap is stale."),
                "percentiles_min_n": MIN_N_FOR_PERCENTILES,
                "left_truncated": True,
                "left_truncation_note": trait_ask["basis"]["left_truncation_note"],
                "wash_filter": "raw",
                "timezone": self.tz,
                "as_of": datetime.fromtimestamp(horizon, tz=timezone.utc).isoformat(),
            },
        }

    def _token_set_size(self, collection: str, traits: dict[str, list[str]] | None) -> int:
        """|S(F)| -- how many tokens the filter selects. The `n` every trait number carries."""
        tf, targs = token_filter_sql(collection, traits or {}, alias="t")
        return self.conn.execute(
            f"SELECT COUNT(*) FROM tokens t WHERE t.collection = ?{tf}",
            (collection, *targs)).fetchone()[0]

    def series(self, *, metric: str, collection: str, denomination: str = "ETH",
               transform: str = "ABS", interval: str = "1h", range_: str = "24h",
               now: datetime | None = None, traits: dict[str, list[str]] | None = None,
               gaps: list[tuple[float, float | None]] | None = None,
               book: str | None = None) -> dict[str, Any]:
        """One metric on the full bucket grid of the range.

        A price bucket with no observation is None (undefined). A COUNT/SUM
        bucket with no event is 0.0 -- zero sales in an hour we were listening
        is a fact, not a hole -- EXCEPT inside an ingestion gap, where every
        metric is None: we were not listening, so we do not know (REQ-F-15).
        `gaps` is [(start_ts, end_ts_or_None)] from the landing-zone manifest.

        `book` selects which book a price metric is read off, and the basis
        always says which was used:

          * `'standing'` -- the resting book at each instant, from `order_lives`,
            reported as the time-weighted median over the bucket with [p10, p90],
            `coverage` and `n`. **The default** for `floor_ask`,
            `collection_bid` and `immediacy_cost` (Operator, 2026-09-10).
          * `'observed'` -- the old interval extremum: the lowest ask *seen*
            in the bucket, the highest offer *seen*. Kept reachable and
            labelled, because it answers a different question (what traded
            hands in this interval) and because a comparison line is how the
            size of the correction gets seen. It is NOT the quantity docs/01
            §3.2 defines (BUG-20260910-057).

        A count or flow metric has no resting book, so `book='standing'` on one
        is REFUSED with that reason rather than answered from a book that does
        not exist, and its basis reads `book: "n/a (...)"`.

        `top_item_bid` is refused too, but for a DIFFERENT reason, and the two
        must not be conflated: it is a price metric and it does have a resting
        book (`STANDING_KINDS['item_bid']`). What it does not have is a
        collection-wide leg -- an item bid applies to one token, so a standing
        "highest item bid" maxes over bids on different tokens. That belongs with
        the union bid leg of PR-5. Its default is therefore `observed`, its basis
        says `observed` rather than `n/a`, and it carries the
        `observed_book_warning` like every other observed-book price line. See
        `STANDING_NOT_OFFERED` for the reason the refusal actually returns.
        """
        if metric not in METRICS:
            raise ValueError(f"unknown metric {metric!r}; one of {sorted(METRICS)}")
        if denomination not in DENOMS:
            raise ValueError(f"unknown denomination {denomination!r}; one of {DENOMS}")
        if interval not in self.intervals["intervals"]:
            raise ValueError(f"unknown interval {interval!r}; one of {sorted(self.intervals['intervals'])}")
        if book is not None and book not in BOOKS:
            raise ValueError(f"unknown book {book!r}; one of {BOOKS}")
        now = now or datetime.now(timezone.utc)
        start_dt, end_dt = parse_range(range_, self.intervals, now, self.tz)
        start, end = start_dt.timestamp(), end_dt.timestamp()
        ispec = self.intervals["intervals"][interval]

        spec = METRICS[metric]
        if "book" not in spec:
            if book == "standing":
                raise ValueError(
                    f"{metric!r} has no standing-book variant: it is a count or flow metric, not a "
                    f"resting-book quantity. Ask for it without `book`, or with book='observed'.")
            book_used = f"n/a ({metric} is a count/flow metric, not a book quantity)"
        else:
            book_used = book or spec.get("book_default", "observed")
            if book_used == "standing" and metric in STANDING_NOT_OFFERED:
                raise ValueError(
                    f"{metric!r} has a standing book but series() does not offer the standing "
                    f"variant: {STANDING_NOT_OFFERED[metric]}")
        keys = bucket_grid(start, end, ispec, self.tz)
        # |S(F)| = 0 -- the filter selects no token, so there is no trait group and
        # nothing this metric reports is a statement about one (BUG-20260910-060).
        # Counted over `tokens`, the same universe `screener` and
        # `trait_offer_verdicts` use, so an unpopulated token list trips the guard
        # too -- which is the conservative direction: if we know of no tokens we can
        # say nothing about a subset of them.
        no_tokens = bool(traits) and self._token_set_size(collection, traits) == 0
        narrows = filter_narrows(spec)
        legs: dict[str, str] = {}
        extra: dict[str, Any] = {}
        book_basis: dict[str, Any] = {}
        if book_used == "standing" and spec.get("book") == "spread":
            st = self.standing_spread(collection, start, end, ispec, denomination, traits, now)
            raw = list(st["median"])
            parts = {"floor_ask": list(st["leg_ask"]), "collection_bid": list(st["leg_bid"])}
            pct = list(st["pct_of_ask"])
            extra = {"p10": st["p10"], "p90": st["p90"], "coverage": st["coverage"],
                     "n_ask": st["n_ask"], "n_bid": st["n_bid"]}
            book_basis = dict(st["basis"])
            legs = self._spread_legs(traits, standing=True)
        elif book_used == "standing":
            st = self.standing_series(spec["book"], collection, start, end, ispec,
                                      denomination, traits, now)
            raw = list(st["median"])
            parts, pct = {}, None
            extra = {"p10": st["p10"], "p90": st["p90"], "coverage": st["coverage"], "n": st["n"]}
            book_basis = dict(st["basis"])
        elif "derived" in spec:
            a, b = spec["derived"]
            ga = self._bucketed(a, collection, denomination, start, end, ispec, traits)
            gb = self._bucketed(b, collection, denomination, start, end, ispec, traits)
            raw = [(ga[k] - gb[k]) if (k in ga and k in gb) else None for k in keys]
            parts = {"floor_ask": [ga.get(k) for k in keys],
                     "collection_bid": [gb.get(k) for k in keys]}
            pct = [((ga[k] - gb[k]) / ga[k]) if (k in ga and k in gb and ga[k]) else None for k in keys]
            legs = self._spread_legs(traits, standing=False)
        else:
            g = self._bucketed(metric, collection, denomination, start, end, ispec, traits)
            # A COUNT/SUM bucket with no event is normally 0.0 -- zero sales in an
            # hour we were listening is a fact. It is NOT a fact when the filter
            # selects no token: "0 bids on this trait" says the trait group was
            # quiet, and the truth is that there is no trait group. Undefined
            # (BUG-20260910-060).
            empty = None if (no_tokens and narrows) else (
                0.0 if spec["agg"] in ("COUNT", "SUM") else None)
            raw = [g.get(k, empty) for k in keys]
            parts, pct = {}, None
        # |S(F)| = 0 blanks EVERY array this metric returns, on every branch
        # (BUG-20260910-061). The first version of this guard lived only on the
        # `_bucketed` branch, so `floor_ask` and `immediacy_cost` on the STANDING
        # book walked straight past it -- and those two are the ones that can
        # disagree with it, because `_standing_live`'s token filter reads the
        # `traits` table while the guard counts `tokens`. With traits populated
        # and the token list not, the basis said "every bucket of this metric is
        # undefined" while the chart drew a line. One place, after every branch,
        # so a branch added later cannot miss it.
        #
        # `parts` is blanked with the rest: `immediacy_cost` as a WHOLE is
        # undefined here, and leaving its collection-wide bid leg populated
        # invites someone to subtract two legs of a metric that has just been
        # declared meaningless. The metric-level carve-out is unaffected --
        # `collection_bid` asked for on its own has `narrows = False` and is
        # never blanked.
        if no_tokens and narrows:
            raw = [None] * len(keys)
            for leg in parts.values():
                leg[:] = [None] * len(keys)
            if pct is not None:
                pct = [None] * len(keys)
            for key in ("p10", "p90", "coverage"):
                if key in extra:
                    extra[key] = [None] * len(keys)
            for key in ("n", "n_ask", "n_bid"):
                if key in extra:
                    extra[key] = [0] * len(keys)
        gap_masked = 0
        if gaps and keys:
            widths = [keys[i + 1] - keys[i] for i in range(len(keys) - 1)] + [end - keys[-1]]
            for i, k in enumerate(keys):
                k_end = k + widths[i]
                if any(gs < k_end and (ge is None or ge > k) for gs, ge in gaps):
                    gap_masked += 1                # every bucket we were not listening in, valued or not
                    raw[i] = None
                    if parts:
                        for leg in parts.values():
                            leg[i] = None
                        if pct is not None:
                            pct[i] = None
                    # A standing series has its own arrays and every one of them
                    # is a claim about a window we were not listening in.
                    for key in ("p10", "p90", "coverage"):
                        if key in extra:
                            extra[key][i] = None
                    for key in ("n", "n_ask", "n_bid"):
                        if key in extra:
                            extra[key][i] = 0

        values, basis = apply_transform(raw, transform)
        basis.update(book_basis)
        basis.update({
            "metric": metric,
            "label": (spec.get("standing_label", spec["label"]) if book_used == "standing"
                      else spec["label"]),
            "book": book_used, "collection": collection,
            "denomination": denomination, "interval": interval,
            "range": {"spec": range_, "start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "display_timezone": self.tz,
            "wash_filter": "raw",           # no filter exists yet; say so rather than imply one
            "trait_filter": traits or {},
            "as_of": now.isoformat(),
            "baseline_at": (datetime.fromtimestamp(keys[basis["baseline_index"]], tz=timezone.utc).isoformat()
                            if basis.get("baseline_index") is not None and keys else None),
            "buckets": len(keys),
            "undefined_buckets": sum(1 for v in raw if v is None),
            "gap_masked_buckets": gap_masked,
            "bucket_alignment": ("UTC" if "duration" in ispec and int(ispec["duration"]) < 86400
                                 else f"local midnight ({self.tz})"),
        })
        basis["empty_token_set"] = no_tokens
        basis["token_set_universe"] = "tokens"
        basis["empty_token_set_note"] = ((
            "this trait filter selects NO token, so every array this metric returns is "
            "undefined -- null, not 0, on every book and every leg. A 0 would say 'nothing "
            "happened to this trait group in this hour'; the truth is that there is no trait "
            "group. Before BUG-20260910-060 the collection-offer and COVERing-trait-offer "
            "branches of the filter still matched here, and before BUG-20260910-061 the "
            "standing-book branches ignored this guard entirely."
            if narrows else
            "this trait filter selects NO token, so nothing on this panel is about a trait group. "
            "This metric is deliberately NOT narrowed by a trait filter (see `legs`): its events "
            "are collection offers, which carry no token and bid on every one of them, so the "
            "number below is collection-wide and is not a statement about the filter."
        ) + " |S(F)| is counted over the TOKENS table, so the first thing to check is whether "
            "the token list is loaded at all -- a populated `traits` table with an empty "
            "`tokens` table gives |S(F)| = 0 too, and both are filled by the same onboarding "
            "run (traits.command / import-traits.command).") if no_tokens else ""
        if legs:
            basis["legs"] = legs
        if book_used == "observed" and "book" in spec:
            tail = ("Kept for comparison; do not quote it as the spread."
                    if spec.get("book") == "spread" else
                    "Kept for comparison; do not quote it as the level of the book.")
            basis["observed_book_warning"] = (
                f"book='observed' on {metric!r} is the INTERVAL EXTREMUM -- the best price SEEN "
                "at any instant in the bucket, from orders that need never have coexisted and need "
                "not have been standing at the end of it. It is not the standing-book quantity "
                "docs/01 §3.2 defines and it is biased optimistic, more so at wider intervals "
                f"(BUG-20260910-057). {tail}")
        out = {"t": [datetime.fromtimestamp(k, tz=timezone.utc).isoformat() for k in keys],
               "v": values, "raw": raw, "basis": basis}
        if parts:
            out["parts"] = parts
            out["pct_of_ask"] = pct
        if extra:
            # The band, the coverage and the counts describe the RAW series and
            # are never transformed: a [p10, p90] in ETH under a PCT transform
            # would be two numbers on a different scale from the line they sit
            # under, which is the kind of chart that gets read wrong once.
            basis["bands_untransformed"] = True
            basis["bands_note"] = (f"p10/p90 are in {denomination} on the raw series; coverage is a "
                                   f"fraction of bucket seconds; n is a count of orders. The "
                                   f"transform applies to the plotted line only.")
        out.update(extra)
        return out

    @staticmethod
    def _spread_legs(traits: dict[str, list[str]] | None, *, standing: bool) -> dict[str, str]:
        """What each leg of the derived spread is, said out loud, every time.

        Only emitted under a trait filter, where the two legs are drawn from
        different populations and the reader has to know it.
        """
        if not traits:
            return {}
        book = "standing " if standing else "observed (interval-extremum) "
        return {
            "floor_ask": f"trait-filtered: {book}lowest ask on tokens matching the filter",
            "collection_bid": f"collection-wide: {book}collection offers carry no token and apply to every "
                              "token. Trait offers do NOT enter this leg -- `collection_bid` is "
                              "collection offers by definition, and a trait-offer bid leg is a "
                              "separate metric. Where a metric does include trait offers (bid_count, "
                              "event_count) they are now matched by the COVER rule on their stored "
                              "criteria; offers with no parsed criteria or with numeric criteria are "
                              "excluded and counted (MetricEngine.trait_offer_verdicts)",
        }

    # -- non-series views ----------------------------------------------------
    def live_book(self, collection: str, now: datetime | None = None, limit: int = 25,
                  traits: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """Orders placed and not since cancelled/invalidated/filled, and unexpired.

        Lifecycle by order_hash. This is the closest thing to "the book right
        now" that the stream supports without a REST snapshot.
        """
        now_dt = now or datetime.now(timezone.utc)
        now_iso = now_dt.isoformat().replace("+00:00", "Z")
        now_ts = now_dt.timestamp()
        standing, sargs = standing_sql("e", now_ts)
        base = f"""SELECT e.valid_at, e.event_type, e.token_id, e.price_eth, e.price_usd,
                          e.maker, e.expiration_at, e.order_hash,
                          (SELECT ol.quantity FROM order_lives ol WHERE ol.order_hash = e.order_hash)
                   FROM events e
                   WHERE e.collection = ? AND e.event_type = ? AND e.order_hash IS NOT NULL
                     AND {standing}"""
        tf, targs = token_filter_sql(collection, traits or {})

        def rows(etype: str, order: str) -> list[dict[str, Any]]:
            extra = tf if etype != "collection_offer" else ""
            cur = self.conn.execute(base + extra + f" ORDER BY e.price_eth {order} LIMIT ?",
                                    (collection, etype, *sargs, *(targs if extra else []), limit))
            keys = ("valid_at", "event_type", "token_id", "price_eth", "price_usd",
                    "maker", "expiration_at", "order_hash", "quantity")
            return [dict(zip(keys, r, strict=True)) for r in cur]
        book = {"as_of": now_iso,
                "asks": rows("item_listed", "ASC"),
                "item_bids": rows("item_received_bid", "DESC"),
                "collection_offers": rows("collection_offer", "DESC")}
        # Depth is units, not rows: a collection offer good for 5 is five units
        # (quant T3b). Carried from the placement by order_lives.
        book["depth"] = {k: sum(max(1, int(r["quantity"] or 1)) for r in v)
                         for k, v in book.items() if isinstance(v, list)}
        return book

    def tape(self, collection: str, limit: int = 50, traits: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
        tf, targs = token_filter_sql(collection, traits or {})
        cur = self.conn.execute(
            f"""SELECT e.valid_at, e.token_id, e.price_eth, e.price_usd, e.maker, e.taker, e.tx_hash
               FROM events e WHERE e.collection = ? AND e.event_type = 'item_sold'{tf}
               ORDER BY e.valid_ts DESC LIMIT ?""", (collection, *targs, limit))
        return [dict(zip(("valid_at", "token_id", "price_eth", "price_usd", "maker", "taker", "tx_hash"), r, strict=True))
                for r in cur]

    def makers(self, collection: str, start: float, end: float, limit: int = 10) -> dict[str, Any]:
        total = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE collection=? AND valid_ts>=? AND valid_ts<? AND maker IS NOT NULL",
            (collection, start, end)).fetchone()[0]
        cur = self.conn.execute(
            """SELECT maker, COUNT(*) n,
                      SUM(event_type='item_received_bid') bids,
                      SUM(event_type='item_cancelled') cancels,
                      SUM(event_type='item_listed') listings
               FROM events WHERE collection=? AND valid_ts>=? AND valid_ts<? AND maker IS NOT NULL
               GROUP BY maker ORDER BY n DESC LIMIT ?""", (collection, start, end, limit))
        rows = [dict(zip(("maker", "events", "bids", "cancels", "listings"), r, strict=True)) for r in cur]
        return {"total_events_with_maker": total, "top": rows}

    def bid_lifetimes(self, collection: str, start: float, end: float,
                      kind: str = "item_received_bid") -> dict[str, Any]:
        """How long a bid stands, read off `order_lives`: ONE life per order.

        The old version joined `events` to `events` with no first-termination
        restriction, so an order with two cancel rows produced a cross product
        and `n` was a count of bid x cancel PAIRS, not of bids (BUG-049). It
        also dropped every bid still standing at window end, which biases the
        percentiles short in the opposite direction -- so the net sign of the
        old error is unknown and the new median may move EITHER way. A large
        change here is the expected consequence of two known defects, not a
        discovery.

        Reported with its counts (project rule 4): ended, still-standing
        (censored), untimed terminators (unknown), and the orphan rate -- the
        share of terminations in the window whose placement we never saw, which
        is the direct estimator of how left-truncated this book still is
        (quant metric 12).

        **Percentiles are WITHHELD, not flagged, below `MIN_N_FOR_PERCENTILES`**
        (REQ-F-19, Q-V3). All three -- p10, median and p90 -- are quantiles of a
        duration sample, so all three are refused together; `n`,
        `min_n_for_percentiles` and `percentiles_withheld` travel with the None
        so the caller can say why the number is missing rather than showing a
        number with a warning beside it, which is what people quote.

        The estimator is still naive: censored lives are counted, not modelled.
        Kaplan-Meier with competing risks is PR-8 and reads these same rows.
        """
        ended: list[float] = []
        censored = unknown = at_risk = 0
        for t_place, t_term, reason in self.conn.execute(
                """SELECT t_place, t_term, exit_reason FROM order_lives
                   WHERE collection = ? AND event_type = ? AND placement_seen = 1
                     AND t_place IS NOT NULL AND t_place >= ? AND t_place < ?""",
                (collection, kind, start, end)):
            at_risk += 1
            if reason == "censored":
                censored += 1
            elif reason == "unknown":
                unknown += 1
            elif t_term is not None:
                ended.append(t_term - t_place)
        d = sorted(ended)
        n = len(d)
        terms, orphans = self.conn.execute(
            """SELECT COUNT(*), SUM(placement_seen = 0) FROM order_lives
               WHERE collection = ? AND exit_source = 'observed' AND t_term IS NOT NULL
                 AND t_term >= ? AND t_term < ?""", (collection, start, end)).fetchone()
        orphans = orphans or 0
        return {"n": n,
                "p10_s": pct(d, 0.10), "median_s": pct(d, 0.5), "p90_s": pct(d, 0.9),
                "min_n_for_percentiles": MIN_N_FOR_PERCENTILES,
                "percentiles_reliable": n >= MIN_N_FOR_PERCENTILES,
                "percentiles_withheld": n < MIN_N_FOR_PERCENTILES,
                "kind": kind, "orders_at_risk": at_risk,
                "censored_n": censored, "unknown_terminator_n": unknown,
                "terminations_in_window": terms, "orphan_terminations": orphans,
                "orphan_rate": (orphans / terms) if terms else None,
                "censoring": "counted, not modelled -- percentiles are biased short by "
                             "the censored lives (PR-8 replaces this with Kaplan-Meier). "
                             "AND THIS ESTIMATOR HAS NO as_of: it reads exit_reason and t_term "
                             "straight off order_lives, which stores them AS OF THE FOLD, so a "
                             "window that ends before the last fold is answered with terminations "
                             "that had not happened yet, and its censored lives are censored at "
                             "the fold rather than at the window end -- one sample, two horizons "
                             "(BUG-20260910-065). Not reachable from the page today, where every "
                             "range ends at now; use survival(), which takes an explicit as_of."}

    # -- PR-8: the survival estimator -------------------------------------------
    def _survival_rows(self, collection: str, start: float, end: float, as_of: float,
                       kind: str, traits: dict[str, list[str]] | None,
                       maker: str | list[str] | None,
                       price_band: tuple[float | None, float | None] | None,
                       ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """The lives in the window, with the **as_of censoring rule** applied.

        This is the one rule that makes the estimator correct and it is easy to
        get wrong: `order_lives.exit_reason` is stored **as of the fold**, not as
        of the question. A life folded at 14:00 says `cancelled` even when the
        question is "what did the book look like at 11:00", and reading it
        straight would mark an order as ended an hour before it was -- the
        estimator would learn the future. So:

            ended  <=>  exit_reason is one of the four CAUSES
                        AND t_term is not null AND t_term <= as_of
            everything else that was placed at or before as_of is CENSORED at
            as_of, with duration `as_of - t_place`.

        Two populations are excluded, and both are counted rather than dropped
        quietly: a life placed after `as_of` (it did not exist yet) and a life
        whose terminator could not be placed in time (`unknown` -- docs/08 §3.3).
        Calling `unknown` censored would say the order stood when we know it did
        not; calling it ended at `as_of` would invent a time. It is neither, and
        it is counted.
        """
        where = ["ol.collection = ?", "ol.event_type = ?", "ol.placement_seen = 1",
                 "ol.t_place IS NOT NULL", "ol.t_place >= ?", "ol.t_place < ?"]
        args: list[Any] = [collection, kind, start, end]
        if traits:
            tf, targs = token_filter_sql(collection, traits, alias="ol")
            where.append(tf[len(" AND "):])
            args.extend(targs)
        makers = [maker] if isinstance(maker, str) else list(maker or [])
        if makers:
            where.append(f"ol.maker IN ({','.join('?' * len(makers))})")
            args.extend(makers)
        if price_band:
            lo, hi = price_band
            if lo is not None:
                where.append("ol.price_eth >= ?")
                args.append(float(lo))
            if hi is not None:
                where.append("ol.price_eth <= ?")
                args.append(float(hi))
            where.append("ol.price_eth IS NOT NULL")
        cols = ("order_hash", "token_id", "maker", "scope_kind", "price_eth", "price_usd",
                "quantity", "t_place", "t_place_observed", "t_term", "exit_reason",
                "exit_source", "expiration_ts")
        cur = self.conn.execute(
            f"SELECT {','.join('ol.' + c for c in cols)} FROM order_lives ol "
            f"WHERE {' AND '.join(where)} ORDER BY ol.t_place", args)
        rows: list[dict[str, Any]] = []
        counts = {"selected": 0, "placed_after_as_of": 0, "unknown_terminator": 0,
                  "ended": 0, "censored": 0, "ended_after_as_of": 0}
        for r in cur:
            d = dict(zip(cols, r, strict=True))
            counts["selected"] += 1
            if d["t_place"] > as_of:
                counts["placed_after_as_of"] += 1
                continue
            if d["exit_reason"] == "unknown":
                counts["unknown_terminator"] += 1
                continue
            reason = d["exit_reason"]
            t_term = d["t_term"]
            if reason in SURVIVAL_CAUSES and t_term is not None and t_term <= as_of:
                d["ended"] = True
                d["cause"] = reason
                d["duration_s"] = float(t_term - d["t_place"])
                counts["ended"] += 1
            else:
                if reason in SURVIVAL_CAUSES and t_term is not None and t_term > as_of:
                    counts["ended_after_as_of"] += 1
                d["ended"] = False
                d["cause"] = None
                d["duration_s"] = float(as_of - d["t_place"])
                d["censored_at"] = as_of
                counts["censored"] += 1
            rows.append(d)
        # N2: the episode id is assigned HERE, not in survival(), so every caller
        # of this method gets it. `survival_drill` shipped a row with
        # `episode: None` because it never went through survival()'s assignment
        # -- a field on the drill list that was always null, beside a header that
        # reports n_eff in those same units.
        for r_, cid in zip(rows, episode_ids(rows, EPISODE_GAP_SECONDS), strict=True):
            r_["episode"] = cid
        return rows, counts

    def survival_prepare(self, collection: str, start: float, end: float, as_of: float,
                         kind: str = "item_received_bid", traits: dict[str, list[str]] | None = None,
                         maker: str | list[str] | None = None,
                         price_band: tuple[float | None, float | None] | None = None,
                         ) -> dict[str, Any]:
        """Every part of `survival()` that touches the sqlite connection, and nothing else.

        Split out so the dashboard can hold its writer lock across the QUERIES
        and drop it before the bootstrap (BUG-20260910-064). B x n resampled
        observations is arithmetic on a list that is already in memory; holding
        the connection lock for seconds of it blocks every other panel on the
        page and the background normalizer behind it.
        """
        if as_of <= 0:
            raise ValueError("survival() needs an explicit as_of (exit_reason is stored as of the fold)")
        rows, counts = self._survival_rows(collection, start, end, as_of, kind, traits, maker, price_band)
        terms, orphans = self.conn.execute(
            """SELECT COUNT(*), SUM(placement_seen = 0) FROM order_lives
               WHERE collection = ? AND exit_source = 'observed' AND t_term IS NOT NULL
                 AND t_term >= ? AND t_term < ?""", (collection, start, min(end, as_of))).fetchone()
        record_end = self.conn.execute(
            "SELECT MAX(valid_ts) FROM events WHERE collection = ?", (collection,)).fetchone()[0]
        return {"rows": rows, "counts": counts, "terminations_in_window": terms,
                "orphan_terminations": orphans or 0, "record_end": record_end}

    def survival(self, collection: str, start: float, end: float, as_of: float,
                 kind: str = "item_received_bid", traits: dict[str, list[str]] | None = None,
                 maker: str | list[str] | None = None,
                 price_band: tuple[float | None, float | None] | None = None,
                 *, prepared: dict[str, Any] | None = None, tau: float | None = None,
                 residual_ages: list[float] | None = None,
                 residual_horizons: list[float] | None = None,
                 bootstrap_b: int | None = None, seed: int = SURVIVAL_BOOTSTRAP_SEED,
                 bins: int = 12) -> dict[str, Any]:
        """How long an order stands, estimated properly: KM + competing risks + a cluster band.

        Everything the panel in design §4 draws, computed once here (the page
        contains no calculations of its own -- docs/06 §3). Replaces
        `bid_lifetimes`, which counted censored lives instead of modelling them.

        Returns, all of it with its counts (project rule 4):

        * `km`        -- the step curve: `t`, `s`, the Greenwood log-log band as
                         a diagnostic, `n_at_risk` and `d` per step.
        * `cif`       -- Aalen-Johansen cumulative incidence per cause. NOT
                         1 - KM per cause, which overstates every cause by
                         censoring the competitors (quant §3.2).
        * `band`      -- the maker-episode cluster bootstrap on a fixed grid.
                         This is the band the panel draws.
        * `rmst`      -- restricted mean standing time at `tau`, with
                         `p_alive_at_tau` = S(tau) beside it, because an RMST
                         without the probability of not ending is half a number.
        * `residual`  -- S(t + L | age = t) for a few (age, horizon) pairs.
        * `n`, `n_eff`, `censored_n`, `orphan_*`, and the excluded counts.
        * `percentiles` -- p10/median/p90 of the KM curve, or **None** below
                         `MIN_CLUSTERS_FOR_SURVIVAL` maker-episode clusters,
                         with the reason. Note the guard is on CLUSTERS, not
                         orders: percentiles from many observations of few
                         independent agents are precise about the agents and
                         silent about the market (quant §3.3).
        * `mode`      -- `'curve'` or `'strip'`. Below the cluster minimum the
                         panel draws every observation as a dot and no curve;
                         `strip` carries those observations.

        **Direction of the change from the old estimator is unknown, and that is
        written here before the number is computed** (factcheck D-W4). The old
        `bid_lifetimes` was biased short by dropping censoring and long by an
        unbounded join; the net sign of two defects pulling opposite ways is not
        predictable. A large move is the expected consequence of two known
        defects. It is not a discovery, and if the median comes out *shorter*
        that is not evidence of a bug in this code.
        """
        if as_of <= 0:
            raise ValueError("survival() needs an explicit as_of (exit_reason is stored as of the fold)")
        prep = prepared if prepared is not None else self.survival_prepare(
            collection, start, end, as_of, kind, traits, maker, price_band)
        rows, counts = prep["rows"], prep["counts"]
        obs = [(r["duration_s"], r["ended"], r["cause"]) for r in rows]
        by_cluster: dict[str, list[tuple[float, bool, str | None]]] = {}
        for r in rows:
            by_cluster.setdefault(r["episode"], []).append((r["duration_s"], r["ended"], r["cause"]))
        n = len(rows)
        n_eff = len(by_cluster)
        km = km_curve(obs)
        max_dur = max((r["duration_s"] for r in rows), default=0.0)

        # tau: never past the data. Extending the last step flat to a tau nobody
        # observed is imputation, and holes are never filled (docs/06 §4.3).
        tau_used = float(tau) if tau is not None else float(max_dur)
        tau_note = None
        if tau is not None and tau_used > max_dur:
            tau_note = (f"tau* = {tau_used:g}s is beyond the largest observed duration "
                        f"({max_dur:g}s); RMST is not extrapolated")
        rmst_v = None if (tau_note or n == 0) else rmst(km["t"], km["s"], tau_used)
        p_alive = None if (tau_note or n == 0) else step_at(km["t"], km["s"], tau_used)

        # percentiles of the KM curve: the first t at which S drops to or below q.
        def km_quantile(q: float) -> float | None:
            target = 1.0 - q
            for ti, si in zip(km["t"], km["s"], strict=True):
                if si <= target + 1e-12:
                    return float(ti)
            return None                       # the curve never got that low: not reached
        enough = n_eff >= MIN_CLUSTERS_FOR_SURVIVAL
        withheld_reason = None if enough else (
            f"n_eff = {n_eff} maker-episode cluster(s) < {MIN_CLUSTERS_FOR_SURVIVAL} "
            f"(REQ-F-19, ASM-022): percentiles from many observations of few independent "
            f"agents are precise about the agents and silent about the market")
        percentiles = ({"p10_s": km_quantile(0.10), "median_s": km_quantile(0.50),
                        "p90_s": km_quantile(0.90)} if enough else None)

        # residual survival S(t + L | age = t), only where BOTH ends are inside the data.
        ages = residual_ages if residual_ages is not None else [0.0, max_dur * 0.25, max_dur * 0.5]
        horizons = residual_horizons if residual_horizons is not None else [max_dur * 0.25, max_dur * 0.5]
        residual = []
        for a in ages:
            s_a = step_at(km["t"], km["s"], a)
            for h in horizons:
                s_ah = step_at(km["t"], km["s"], a + h)
                inside = (a + h) <= max_dur and s_a is not None and s_a > 0
                residual.append({
                    "age_s": float(a), "horizon_s": float(h),
                    "s_at_age": s_a, "s_at_age_plus_horizon": s_ah if inside else None,
                    "p_still_standing": (s_ah / s_a) if (inside and s_ah is not None) else None,
                    "beyond_data": not inside})

        # the cluster bootstrap band, on a fixed grid
        grid = survival_grid(km["t"])
        b_req = SURVIVAL_BOOTSTRAP_B if bootstrap_b is None else int(bootstrap_b)
        b_used, b_note = b_req, None
        if n and b_req * n > SURVIVAL_BOOTSTRAP_WORK_CAP:
            b_used = max(50, SURVIVAL_BOOTSTRAP_WORK_CAP // n)
            b_note = (f"B cut from {b_req} to {b_used}: B x n = {b_req * n} exceeds the "
                      f"{SURVIVAL_BOOTSTRAP_WORK_CAP} resampled-observation cap (ASM-022). "
                      f"The band is wider-tailed at low B, not narrower -- it is noisier, not tighter.")
        band = cluster_bootstrap(by_cluster, grid, b=b_used if n_eff > 1 else 0, seed=seed)
        band["b_requested"] = b_req
        band["note"] = b_note
        if n_eff <= 1:
            band["withheld_reason"] = (f"n_eff = {n_eff}: a bootstrap over one cluster resamples "
                                       f"the same cluster every time and produces a band of width zero, "
                                       f"which would read as certainty")

        # exit-reason histogram over log-spaced duration bins (design §4.1b)
        hist = self._duration_histogram(rows, bins)
        # placement-time mini-map (design §4.1c)
        mini = self._placement_minimap(rows, start, min(end, as_of))

        terms, orphans = prep["terminations_in_window"], prep["orphan_terminations"]
        record_end = prep["record_end"]
        # thinned for TRANSPORT only -- everything above was computed on the full curve
        thin = downsample_km(km)
        return {
            "kind": kind, "collection": collection,
            "mode": "curve" if enough else "strip",
            "km": {k: thin[k] for k in ("t", "s", "greenwood_lower", "greenwood_upper", "n_at_risk", "d")}
                  | {"points": thin["points"], "event_times": thin["event_times"],
                     "downsampled": thin["downsampled"]},
            "cif": thin["cif"], "causes": list(SURVIVAL_CAUSES),
            "band": {"grid": grid, **band},
            "rmst": {"tau_s": tau_used, "rmst_s": rmst_v, "p_alive_at_tau": p_alive, "note": tau_note},
            "residual": residual,
            "percentiles": percentiles, "percentiles_withheld": not enough,
            "percentiles_withheld_reason": withheld_reason,
            "n": n, "n_eff": n_eff, "min_clusters": MIN_CLUSTERS_FOR_SURVIVAL,
            "episode_gap_s": EPISODE_GAP_SECONDS,
            "ended_n": counts["ended"], "censored_n": counts["censored"],
            "unknown_terminator_n": counts["unknown_terminator"],
            "placed_after_as_of_n": counts["placed_after_as_of"],
            "ended_after_as_of_n": counts["ended_after_as_of"],
            "unclustered_no_maker_n": sum(1 for r in rows if not r.get("maker")),
            "terminations_in_window": terms, "orphan_terminations": orphans,
            "orphan_rate": (orphans / terms) if terms else None,
            "histogram": hist, "placements": mini,
            "strip": ([{"duration_s": r["duration_s"], "ended": r["ended"],
                        "exit_reason": r["exit_reason"] if r["ended"] else "censored",
                        "maker": r["maker"], "token_id": r["token_id"],
                        "price_eth": r["price_eth"], "price_usd": r["price_usd"],
                        "order_hash": r["order_hash"], "episode": r["episode"],
                        "t_place": datetime.fromtimestamp(r["t_place"], tz=timezone.utc).isoformat()}
                       for r in rows] if not enough else None),
            "basis": {
                "as_of": datetime.fromtimestamp(as_of, tz=timezone.utc).isoformat(),
                "window": {"start": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                           "end": datetime.fromtimestamp(end, tz=timezone.utc).isoformat()},
                "record_ends_at": (datetime.fromtimestamp(record_end, tz=timezone.utc).isoformat()
                                   if record_end else None),
                "as_of_beyond_record": bool(record_end and as_of > record_end),
                "traits": traits or {}, "maker": maker, "price_band": price_band,
                "estimator": "Kaplan-Meier, Greenwood log-log band (diagnostic), Aalen-Johansen "
                             "cumulative incidence, maker-episode cluster bootstrap band",
                "censoring": "administrative at as_of; exit_reason is stored as of the FOLD and is "
                             "re-read against as_of, so a life terminated after as_of is censored",
                "left_truncated": True,
                "left_truncation_note": "only orders whose PLACEMENT we witnessed enter the risk set; "
                                        "orphaned terminations are counted, never imputed",
                "direction_warning": "the median may move in EITHER direction from the old estimator: "
                                     "it was biased short by dropped censoring and long by an unbounded "
                                     "join. A large change is the expected consequence of two known "
                                     "defects, not a discovery (factcheck D-W4).",
                "wash_filter": "raw"},
        }

    @staticmethod
    def _duration_histogram(rows: list[dict[str, Any]], bins: int) -> dict[str, Any]:
        """Counts per LOG-spaced duration bin, stacked by exit reason (design §4.1b).

        Log-spaced because lifetimes run from a second to hours; linear bins put
        99% of a bot-quoted book in the first bar. A censored life is its own
        stack colour -- it has not exited, and calling it `cancelled` would be
        the exact error the estimator exists to fix.
        """
        durs = [r["duration_s"] for r in rows if r["duration_s"] > 0]
        if not durs:
            return {"edges": [], "bins": [], "reasons": [*SURVIVAL_CAUSES, "censored"],
                    "zero_duration_n": sum(1 for r in rows if r["duration_s"] <= 0)}
        lo, hi = min(durs), max(durs)
        if hi <= lo:
            hi = lo * 2 if lo > 0 else 1.0
        ll, lh = math.log(lo), math.log(hi)
        edges = [math.exp(ll + (lh - ll) * i / bins) for i in range(bins + 1)]
        edges[-1] = hi * (1 + 1e-9)
        reasons = [*SURVIVAL_CAUSES, "censored"]
        cells = [{"lo": edges[i], "hi": edges[i + 1], "total": 0,
                  "by_reason": dict.fromkeys(reasons, 0)} for i in range(bins)]
        for r in rows:
            d = r["duration_s"]
            if d <= 0:
                continue
            i = min(bins - 1, max(0, bisect.bisect_right(edges, d) - 1))
            key = r["exit_reason"] if r["ended"] else "censored"
            cells[i]["total"] += 1
            cells[i]["by_reason"][key] = cells[i]["by_reason"].get(key, 0) + 1
        return {"edges": edges, "bins": cells, "reasons": reasons,
                "zero_duration_n": sum(1 for r in rows if r["duration_s"] <= 0)}

    @staticmethod
    def _placement_minimap(rows: list[dict[str, Any]], start: float, end: float,
                           max_bars: int = 240) -> dict[str, Any]:
        """Orders PLACED per wall-clock bucket -- the brush strip (design §4.1c).

        A separate x-axis from the curve on purpose: one is a duration, the
        other is a time of day, and putting them on one axis is how a panel
        starts lying about which is which.
        """
        span = max(1.0, end - start)
        step = max(60.0, span / max_bars)
        nb = max(1, int(math.ceil(span / step)))
        counts = [0] * nb
        for r in rows:
            i = min(nb - 1, max(0, int((r["t_place"] - start) // step)))
            counts[i] += 1
        return {"step_s": step,
                "t": [datetime.fromtimestamp(start + i * step, tz=timezone.utc).isoformat()
                      for i in range(nb)],
                "n": counts, "total": sum(counts)}

    def survival_drill(self, collection: str, start: float, end: float, as_of: float,
                       bin_lo: float, bin_hi: float, kind: str = "item_received_bid",
                       traits: dict[str, list[str]] | None = None,
                       maker: str | list[str] | None = None,
                       price_band: tuple[float | None, float | None] | None = None,
                       page: int = 0, page_size: int = 50) -> dict[str, Any]:
        """Every life whose duration falls in `[bin_lo, bin_hi)`, as the drill list.

        Both timestamps ride along deliberately (design §4.3): `observed_at -
        valid_at` is our stream lag, and *if a bid's whole life is shorter than
        our lag we never had a chance at it*. That is a fact about strategy
        feasibility, not about the market, and it is invisible unless both
        clocks are on the row.

        `distance_to_floor_eth` is `price - lowest STANDING ask at the instant of
        placement`, read off the same sweep line the floor chart uses. It is
        `None` -- never estimated, never carried forward from the last known
        floor -- when no ask was standing at that instant. A hole is a hole
        (docs/06 §4.3).
        """
        rows, _counts = self._survival_rows(collection, start, end, as_of, kind, traits, maker, price_band)
        sel = [r for r in rows if bin_lo <= r["duration_s"] < bin_hi]
        sel.sort(key=lambda r: r["t_place"])
        total = len(sel)
        makers_n = len({r["maker"] for r in sel if r["maker"]})
        page = max(0, int(page))
        window = sel[page * page_size:(page + 1) * page_size]
        # the standing floor at placement, from the same sweep the floor chart uses
        floor_segs = _extremum_segments(
            self._standing_live("ask", collection, "ETH", start, max(as_of, start + 1e-9), None), True)
        seg_starts = [s[0] for s in floor_segs]

        def floor_at(t: float) -> float | None:
            i = bisect.bisect_right(seg_starts, t) - 1
            if i < 0:
                return None
            t0, t1, v = floor_segs[i]
            return v if t0 <= t < t1 else None
        tok_traits: dict[str, dict[str, str]] = {}
        ids = [r["token_id"] for r in window if r["token_id"]]
        if ids:
            for tid, tt, val in self.conn.execute(
                    f"SELECT token_id, trait_type, value FROM traits WHERE collection = ? "
                    f"AND token_id IN ({','.join('?' * len(ids))})", (collection, *ids)):
                tok_traits.setdefault(tid, {})[tt] = val
        out = []
        for r in window:
            fl = floor_at(r["t_place"])
            out.append({
                "order_hash": r["order_hash"], "token_id": r["token_id"],
                "traits": tok_traits.get(r["token_id"] or "", {}),
                "maker": r["maker"], "price_eth": r["price_eth"], "price_usd": r["price_usd"],
                "quantity": r["quantity"],
                "placed_at_valid": datetime.fromtimestamp(r["t_place"], tz=timezone.utc).isoformat(),
                "placed_at_observed": (datetime.fromtimestamp(r["t_place_observed"], tz=timezone.utc).isoformat()
                                       if r["t_place_observed"] else None),
                "stream_lag_s": ((r["t_place_observed"] - r["t_place"])
                                 if r["t_place_observed"] else None),
                "ended_at": (datetime.fromtimestamp(r["t_term"], tz=timezone.utc).isoformat()
                             if r["ended"] else None),
                "still_standing": not r["ended"],
                "lifetime_s": r["duration_s"],
                "exit_reason": r["exit_reason"] if r["ended"] else "censored",
                "exit_source": r["exit_source"] if r["ended"] else None,
                "floor_ask_at_placement_eth": fl,
                "distance_to_floor_eth": (r["price_eth"] - fl) if (fl is not None and r["price_eth"] is not None) else None,
                "episode": r.get("episode"),
            })
        return {"bin": {"lo": bin_lo, "hi": bin_hi}, "total": total, "makers": makers_n,
                "page": page, "page_size": page_size, "rows": out,
                "floor_basis": "lowest STANDING ask at the instant of placement; null where no ask "
                               "was standing -- never the last floor seen"}

    def criteria_coverage(self, collection: str) -> dict[str, Any]:
        """Every distinct (trait_type, value) in `order_criteria` should exist in `traits`.

        If OpenSea's `trait_name` casing or spelling differs from the metadata's
        `value`, every match silently returns nothing -- a wrong answer that
        looks like a quiet market. Values are stored verbatim on both sides
        precisely so this check can see the difference; the check is what makes
        the difference visible instead of invisible (dataeng §4.2).
        """
        pairs = self.conn.execute(
            """SELECT DISTINCT c.trait_type, c.value FROM order_criteria c
               JOIN events e ON e.run = c.run AND e.seq = c.seq
               WHERE e.collection = ? AND c.kind = 'string'""", (collection,)).fetchall()
        missing = [(t, v) for t, v in pairs if not self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM traits WHERE collection=? AND trait_type=? AND value=?)",
            (collection, t, v)).fetchone()[0]]
        return {"collection": collection, "distinct_criteria": len(pairs),
                "matched": len(pairs) - len(missing), "missing": len(missing),
                "missing_pairs": [{"trait_type": t, "value": v} for t, v in missing[:50]],
                "alert": bool(missing),
                "note": ("a criterion with no matching trait value can never match a token; "
                         "if this is non-zero the trait table is incomplete or the casing differs")}

    def trait_offer_verdicts(self, collection: str, traits: dict[str, list[str]] | None,
                             start: float, end: float) -> dict[str, Any]:
        """COVERS / PARTIAL / DISJOINT / UNKNOWN / UNPARSED for a filter. Never summed.

        Three verdicts are mutually exclusive and must never be added into one
        depth number (dataeng §4.3): a PARTIAL offer is reported with
        |S(F) n S(C)| and |S(F)|, never as a bare count. Allocating a fraction
        of a PARTIAL offer's quantity as depth models the offerer as
        indifferent among the tokens in reach -- a judgement, and so it belongs
        in ANALYSIS with its assumption declared, never here (docs/06 §4).

        **Two shapes here are load-bearing for cost, not for correctness
        (BUG-20260910-062).** The reach of a criterion `(trait_type, value)` is a
        property of the TRAIT TABLE, not of the offer that names it, so it is
        looked up once per distinct pair and memoised for the call -- 48 distinct
        pairs, not 27,694 lookups, on the fixture the tech-lead measured. And the
        criteria rows are fetched in ONE grouped query rather than one per offer.
        Both are O(distinct work) instead of O(offers x criteria); the verdicts
        they produce are identical, which is what the property tests assert.

        `partial` is capped at `PARTIAL_DETAIL_CAP` entries. `partial_n` is the
        TRUE count and is computed separately -- capping the detail must never
        cap the number, or the cap silently becomes the answer (BUG-20260910-063).
        """
        traits = traits or {}
        tf, targs = token_filter_sql(collection, traits, alias="t")
        s_f = {r[0] for r in self.conn.execute(
            f"SELECT t.token_id FROM tokens t WHERE t.collection = ?{tf}", (collection, *targs))}
        covers = disjoint = unknown = unparsed = 0
        partial: list[dict[str, Any]] = []
        partial_n = 0

        reach: dict[tuple[str, str], set[str]] = {}

        def tokens_with(t: str, v: str) -> set[str]:
            """S({(t, v)}) -- memoised per call. One query per DISTINCT pair."""
            key = (t, v)
            if key not in reach:
                reach[key] = {r[0] for r in self.conn.execute(
                    "SELECT token_id FROM traits WHERE collection=? AND trait_type=? AND value=?",
                    (collection, t, v))}
            return reach[key]

        crit_by_order: dict[tuple[str, int], list[tuple[str, str]]] = {}
        for run, seq, t, v in self.conn.execute(
                """SELECT c.run, c.seq, c.trait_type, c.value
                   FROM order_criteria c JOIN events e ON e.run = c.run AND e.seq = c.seq
                   WHERE e.collection = ? AND e.event_type = 'trait_offer'
                     AND e.valid_ts >= ? AND e.valid_ts < ? AND c.kind = 'string'
                   ORDER BY c.run, c.seq, c.idx""", (collection, start, end)):
            crit_by_order.setdefault((run, seq), []).append((t, v))

        for run, seq, oh, price, qty, cn, cnn in self.conn.execute(
                """SELECT run, seq, order_hash, price_eth, quantity, criteria_n, criteria_numeric_n
                   FROM events WHERE collection = ? AND event_type = 'trait_offer'
                     AND valid_ts >= ? AND valid_ts < ?""", (collection, start, end)):
            if cn is None or cn == 0:
                unparsed += 1                       # the loud-failure marker: matches nothing
                continue
            if (cnn or 0) > 0:
                unknown += 1                        # numeric criteria: UNKNOWN is not TRUE
                continue
            crit = crit_by_order.get((run, seq), [])
            if criteria_covers(list(crit), traits):
                covers += 1
                continue
            s_c: set[str] | None = None
            for t, v in crit:
                got = tokens_with(t, v)
                s_c = set(got) if s_c is None else (s_c & got)
            overlap = len(s_f & (s_c or set()))
            if overlap:
                partial_n += 1
                if len(partial) < PARTIAL_DETAIL_CAP:
                    partial.append({"order_hash": oh, "price_eth": price, "quantity": qty,
                                    "criteria": [{"trait_type": t, "value": v} for t, v in crit],
                                    "overlap_tokens": overlap, "filter_tokens": len(s_f)})
            else:
                disjoint += 1
        return {"collection": collection, "trait_filter": traits, "filter_tokens": len(s_f),
                "covers": covers, "partial_n": partial_n, "partial": partial,
                "partial_detail_cap": PARTIAL_DETAIL_CAP,
                "partial_truncated": partial_n > len(partial),
                "disjoint": disjoint, "unknown_numeric": unknown, "unparsed": unparsed,
                "distinct_criteria_pairs": len(reach),
                "note": "COVERS is the only verdict counted as bid depth; the four others are "
                        "reported separately and are never summed into it. `partial` is a SAMPLE "
                        f"of at most {PARTIAL_DETAIL_CAP}; `partial_n` is the true count."}

    def screener(self, collection: str, *, traits: dict[str, list[str]] | None = None,
                 sort: str = "token_id", direction: str = "asc", page: int = 0, page_size: int = 50,
                 denom: str = "ETH", now: datetime | None = None) -> dict[str, Any]:
        """Every token matching the filter, with its traits and its live market state,
        sortable by any column -- token, name, any trait type, lowest ask, highest bid,
        last sale (REQ-F-07). Computed from the store; no REST."""
        col = "price_usd" if denom == "USD" else "price_eth"
        now_dt = now or datetime.now(timezone.utc)
        now_iso = now_dt.isoformat().replace("+00:00", "Z")
        now_ts = now_dt.timestamp()
        tf, targs = token_filter_sql(collection, traits or {}, alias="t")
        toks = self.conn.execute(
            f"SELECT t.token_id, t.name, t.image_url FROM tokens t WHERE t.collection = ?{tf}",
            (collection, *targs)).fetchall()
        ids = {r[0] for r in toks}
        tr: dict[str, dict[str, str]] = {}
        for tid, tt, v in self.conn.execute("SELECT token_id, trait_type, value FROM traits WHERE collection = ?", (collection,)):
            if tid in ids:
                tr.setdefault(tid, {})[tt] = v
        standing, sargs = standing_sql("e", now_ts)      # one definition, shared with live_book
        live = f"""SELECT e.token_id, MIN(e.{col}), MAX(e.{col}) FROM events e
                   WHERE e.collection = ? AND e.event_type = ? AND e.order_hash IS NOT NULL AND e.token_id IS NOT NULL
                     AND {standing} GROUP BY e.token_id"""
        ask = {t: lo for t, lo, _ in self.conn.execute(live, (collection, "item_listed", *sargs))}
        bid = {t: hi for t, _, hi in self.conn.execute(live, (collection, "item_received_bid", *sargs))}
        last_sale: dict[str, tuple[float, str]] = {}
        for t, p, at in self.conn.execute(
                f"""SELECT token_id, {col}, valid_at FROM events WHERE collection = ? AND event_type = 'item_sold'
                    AND token_id IS NOT NULL ORDER BY valid_ts ASC""", (collection,)):
            last_sale[t] = (p, at)
        rows = []
        for tid, name, img in toks:
            ls = last_sale.get(tid)
            rows.append({"token_id": tid, "name": name, "image_url": img, "traits": tr.get(tid, {}),
                         "lowest_ask": ask.get(tid), "highest_bid": bid.get(tid),
                         "last_sale": ls[0] if ls else None, "last_sale_at": ls[1] if ls else None})
        trait_types = sorted({tt for d in tr.values() for tt in d})
        if sort not in ("token_id", "name", "lowest_ask", "highest_bid", "last_sale") and sort not in trait_types:
            sort = "token_id"   # an unknown column is a caller mistake, not a crash and never SQL
        direction = "desc" if direction == "desc" else "asc"

        def key(r: dict[str, Any]):
            if sort == "token_id":
                try:
                    return (0, 0, int(r["token_id"]))
                except ValueError:
                    return (0, 1, r["token_id"])
            if sort in ("lowest_ask", "highest_bid", "last_sale"):
                v = r[sort]
                return (1, 0, 0.0) if v is None else (0, 0, v)
            if sort == "name":
                return (1, 0, "") if not r["name"] else (0, 1, r["name"].lower())
            v = r["traits"].get(sort)
            if v is None:
                return (1, 0, "")
            try:
                return (0, 0, float(v))          # numbers before words, each kind compared with its own
            except ValueError:
                return (0, 1, v.lower())
        rows.sort(key=key, reverse=(direction == "desc"))
        if direction == "desc":
            # keep "no value" rows at the bottom in either direction
            rows.sort(key=lambda r: key(r)[0] == 1)
        total = len(rows)
        page_rows = rows[page * page_size:(page + 1) * page_size]
        return {"total": total, "page": page, "page_size": page_size, "sort": sort, "direction": direction,
                "denomination": denom, "trait_types": trait_types, "as_of": now_iso,
                "trait_filter": traits or {}, "rows": page_rows}

    def event_mix(self, collection: str | None, start: float, end: float) -> list[dict[str, Any]]:
        where = "valid_ts>=? AND valid_ts<?" + (" AND collection=?" if collection else "")
        args: list[Any] = [start, end] + ([collection] if collection else [])
        cur = self.conn.execute(
            f"SELECT event_type, COUNT(*) FROM events WHERE {where} GROUP BY event_type ORDER BY 2 DESC", args)
        return [{"event_type": t, "n": n} for t, n in cur]
