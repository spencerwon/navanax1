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

`immediacy_cost` (REQ-F-13a, methodology §3.2): lowest ask minus highest
COLLECTION offer in the interval. Undefined -- returned as null, never
substituted -- when either side is absent. "No bid at any price" is the most
important liquidity fact about a collection, and a chart must show it as a
hole, not a guess.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

TRANSFORMS = ("ABS", "PCT", "LOG", "DIFF", "BPS")
DENOMS = ("ETH", "USD")

# metric id -> (event_type filter, aggregate, price column?, description)
METRICS: dict[str, dict[str, Any]] = {
    "collection_bid":  {"types": ("collection_offer",), "agg": "MAX", "price": True,
                        "label": "Highest collection offer seen in interval"},
    "top_item_bid":    {"types": ("item_received_bid",), "agg": "MAX", "price": True,
                        "label": "Highest item bid seen in interval"},
    "floor_ask":       {"types": ("item_listed",), "agg": "MIN", "price": True,
                        "label": "Lowest listing seen in interval"},
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
                        "label": "Lowest ask minus highest collection offer (REQ-F-13a)"},
}


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
        #   collection_offer (no token_id) -> a bid on EVERY token, so it passes;
        #   trait_offer (no token_id) -> a bid on SOME trait we do not yet store
        #     the criteria for, so it is EXCLUDED rather than counted under a
        #     filter it may not match (BUG-044).
        if tf and not (spec["types"] and set(spec["types"]) <= {"collection_offer"}):
            clauses = tf[len(" AND "):]                     # "A AND B AND C"
            where.append(f"((e.token_id IS NULL AND e.event_type = 'collection_offer') OR ({clauses}))")
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

    def series(self, *, metric: str, collection: str, denomination: str = "ETH",
               transform: str = "ABS", interval: str = "1h", range_: str = "24h",
               now: datetime | None = None, traits: dict[str, list[str]] | None = None,
               gaps: list[tuple[float, float | None]] | None = None) -> dict[str, Any]:
        """One metric on the full bucket grid of the range.

        A price bucket with no observation is None (undefined). A COUNT/SUM
        bucket with no event is 0.0 -- zero sales in an hour we were listening
        is a fact, not a hole -- EXCEPT inside an ingestion gap, where every
        metric is None: we were not listening, so we do not know (REQ-F-15).
        `gaps` is [(start_ts, end_ts_or_None)] from the landing-zone manifest.
        """
        if metric not in METRICS:
            raise ValueError(f"unknown metric {metric!r}; one of {sorted(METRICS)}")
        if denomination not in DENOMS:
            raise ValueError(f"unknown denomination {denomination!r}; one of {DENOMS}")
        if interval not in self.intervals["intervals"]:
            raise ValueError(f"unknown interval {interval!r}; one of {sorted(self.intervals['intervals'])}")
        now = now or datetime.now(timezone.utc)
        start_dt, end_dt = parse_range(range_, self.intervals, now, self.tz)
        start, end = start_dt.timestamp(), end_dt.timestamp()
        ispec = self.intervals["intervals"][interval]

        spec = METRICS[metric]
        keys = bucket_grid(start, end, ispec, self.tz)
        legs: dict[str, str] = {}
        if "derived" in spec:
            a, b = spec["derived"]
            ga = self._bucketed(a, collection, denomination, start, end, ispec, traits)
            gb = self._bucketed(b, collection, denomination, start, end, ispec, traits)
            raw = [(ga[k] - gb[k]) if (k in ga and k in gb) else None for k in keys]
            parts = {"floor_ask": [ga.get(k) for k in keys],
                     "collection_bid": [gb.get(k) for k in keys]}
            pct = [((ga[k] - gb[k]) / ga[k]) if (k in ga and k in gb and ga[k]) else None for k in keys]
            if traits:
                # The ask leg is trait-filtered (listings carry a token). The bid
                # leg is the collection-wide offer, because a collection offer is
                # the only standing bid those tokens have. Said out loud, every time.
                legs = {"floor_ask": "trait-filtered: lowest ask on tokens matching the filter",
                        "collection_bid": "collection-wide: collection offers carry no token and apply to every token; "
                                          "trait offers are excluded (criteria not stored)"}
        else:
            g = self._bucketed(metric, collection, denomination, start, end, ispec, traits)
            empty = 0.0 if spec["agg"] in ("COUNT", "SUM") else None
            raw = [g.get(k, empty) for k in keys]
            parts, pct = {}, None
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

        values, basis = apply_transform(raw, transform)
        basis.update({
            "metric": metric, "label": spec["label"], "collection": collection,
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
        if legs:
            basis["legs"] = legs
        out = {"t": [datetime.fromtimestamp(k, tz=timezone.utc).isoformat() for k in keys],
               "v": values, "raw": raw, "basis": basis}
        if parts:
            out["parts"] = parts
            out["pct_of_ask"] = pct
        return out

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
        # INDEXED BY: the planner otherwise picks the (event_type, valid_ts)
        # index for the correlated side and scans every cancellation per order.
        dead = """NOT EXISTS (SELECT 1 FROM events d INDEXED BY ix_events_lifecycle
                      WHERE d.order_hash = e.order_hash
                      AND d.event_type IN ('item_cancelled','order_invalidate','item_sold')
                      AND d.valid_ts >= e.valid_ts)"""
        base = f"""SELECT e.valid_at, e.event_type, e.token_id, e.price_eth, e.price_usd,
                          e.maker, e.expiration_at, e.order_hash
                   FROM events e
                   WHERE e.collection = ? AND e.event_type = ? AND e.order_hash IS NOT NULL
                     AND (e.expiration_ts IS NULL OR e.expiration_ts > ?) AND {dead}"""
        tf, targs = token_filter_sql(collection, traits or {})

        def rows(etype: str, order: str) -> list[dict[str, Any]]:
            extra = tf if etype != "collection_offer" else ""
            cur = self.conn.execute(base + extra + f" ORDER BY e.price_eth {order} LIMIT ?",
                                    (collection, etype, now_ts, *(targs if extra else []), limit))
            keys = ("valid_at", "event_type", "token_id", "price_eth", "price_usd",
                    "maker", "expiration_at", "order_hash")
            return [dict(zip(keys, r, strict=True)) for r in cur]
        return {"as_of": now_iso,
                "asks": rows("item_listed", "ASC"),
                "item_bids": rows("item_received_bid", "DESC"),
                "collection_offers": rows("collection_offer", "DESC")}

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

    def bid_lifetimes(self, collection: str, start: float, end: float) -> dict[str, Any]:
        """Seconds from bid placed to the same order being cancelled.

        A direct observation of how long liquidity actually stands. Reported
        with its count (REQ-F-19); percentiles only above a minimum n.
        """
        cur = self.conn.execute(
            """SELECT c.valid_ts - b.valid_ts
               FROM events b JOIN events c INDEXED BY ix_events_lifecycle ON c.order_hash = b.order_hash
               WHERE b.collection = ? AND b.event_type = 'item_received_bid'
                 AND c.event_type = 'item_cancelled' AND c.valid_ts >= b.valid_ts
                 AND b.valid_ts >= ? AND b.valid_ts < ?""", (collection, start, end))
        d = sorted(x for (x,) in cur if x is not None)
        n = len(d)
        def pct(p: float) -> float | None:
            return d[min(n - 1, int(p * n))] if n else None
        return {"n": n, "p10_s": pct(0.10), "median_s": pct(0.5), "p90_s": pct(0.9),
                "min_n_for_percentiles": 30, "percentiles_reliable": n >= 30}

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
        dead = """NOT EXISTS (SELECT 1 FROM events d INDEXED BY ix_events_lifecycle
                    WHERE d.order_hash = e.order_hash AND d.event_type IN ('item_cancelled','order_invalidate','item_sold')
                      AND d.valid_ts >= e.valid_ts)"""
        live = f"""SELECT e.token_id, MIN(e.{col}), MAX(e.{col}) FROM events e
                   WHERE e.collection = ? AND e.event_type = ? AND e.order_hash IS NOT NULL AND e.token_id IS NOT NULL
                     AND (e.expiration_ts IS NULL OR e.expiration_ts > ?) AND {dead} GROUP BY e.token_id"""
        ask = {t: lo for t, lo, _ in self.conn.execute(live, (collection, "item_listed", now_ts))}
        bid = {t: hi for t, _, hi in self.conn.execute(live, (collection, "item_received_bid", now_ts))}
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
