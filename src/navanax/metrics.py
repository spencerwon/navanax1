"""The metric request contract (docs/06 §3), implemented once.

    MetricRequest = {metric, scope, denomination, transform, interval, range}

Any chart is that tuple. This module serves the tuple; the UI never contains
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
# the engine
# ---------------------------------------------------------------------------
class MetricEngine:
    def __init__(self, conn: sqlite3.Connection, intervals: dict[str, Any], tz_name: str) -> None:
        self.conn = conn
        self.intervals = intervals
        self.tz = tz_name

    def _bucketed(self, metric: str, collection: str, denom: str, start: float, end: float,
                  interval: dict[str, Any]) -> dict[float, float]:
        spec = METRICS[metric]
        col = "price_usd" if denom == "USD" else "price_eth"
        where = ["collection = ?", "valid_ts >= ?", "valid_ts < ?"]
        args: list[Any] = [collection, start, end]
        if spec["types"]:
            where.append(f"event_type IN ({','.join('?' * len(spec['types']))})")
            args.extend(spec["types"])
        if spec["price"]:
            where.append(f"{col} IS NOT NULL")
        sql = f"SELECT valid_ts, {col} FROM events WHERE {' AND '.join(where)} ORDER BY valid_ts"
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
               now: datetime | None = None) -> dict[str, Any]:
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
        if "derived" in spec:
            a, b = spec["derived"]
            ga = self._bucketed(a, collection, denomination, start, end, ispec)
            gb = self._bucketed(b, collection, denomination, start, end, ispec)
            keys = sorted(set(ga) | set(gb))
            raw = [(ga[k] - gb[k]) if (k in ga and k in gb) else None for k in keys]
            parts = {"floor_ask": [ga.get(k) for k in keys],
                     "collection_bid": [gb.get(k) for k in keys]}
            pct = [((ga[k] - gb[k]) / ga[k]) if (k in ga and k in gb and ga[k]) else None for k in keys]
        else:
            g = self._bucketed(metric, collection, denomination, start, end, ispec)
            keys = sorted(g)
            raw = [g[k] for k in keys]
            parts, pct = {}, None

        values, basis = apply_transform(raw, transform)
        basis.update({
            "metric": metric, "label": spec["label"], "collection": collection,
            "denomination": denomination, "interval": interval,
            "range": {"spec": range_, "start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "display_timezone": self.tz,
            "wash_filter": "raw",           # no filter exists yet; say so rather than imply one
            "as_of": now.isoformat(),
            "baseline_at": (datetime.fromtimestamp(keys[basis["baseline_index"]], tz=timezone.utc).isoformat()
                            if basis.get("baseline_index") is not None and keys else None),
            "buckets": len(keys),
            "undefined_buckets": sum(1 for v in raw if v is None),
        })
        out = {"t": [datetime.fromtimestamp(k, tz=timezone.utc).isoformat() for k in keys],
               "v": values, "raw": raw, "basis": basis}
        if parts:
            out["parts"] = parts
            out["pct_of_ask"] = pct
        return out

    # -- non-series views ----------------------------------------------------
    def live_book(self, collection: str, now: datetime | None = None, limit: int = 25) -> dict[str, Any]:
        """Orders placed and not since cancelled/invalidated/filled, and unexpired.

        Lifecycle by order_hash. This is the closest thing to "the book right
        now" that the stream supports without a REST snapshot.
        """
        now_iso = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
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
                     AND (e.expiration_at IS NULL OR e.expiration_at > ?) AND {dead}"""
        def rows(etype: str, order: str) -> list[dict[str, Any]]:
            cur = self.conn.execute(base + f" ORDER BY e.price_eth {order} LIMIT ?",
                                    (collection, etype, now_iso, limit))
            keys = ("valid_at", "event_type", "token_id", "price_eth", "price_usd",
                    "maker", "expiration_at", "order_hash")
            return [dict(zip(keys, r, strict=True)) for r in cur]
        return {"as_of": now_iso,
                "asks": rows("item_listed", "ASC"),
                "item_bids": rows("item_received_bid", "DESC"),
                "collection_offers": rows("collection_offer", "DESC")}

    def tape(self, collection: str, limit: int = 50) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            """SELECT valid_at, token_id, price_eth, price_usd, maker, taker, tx_hash
               FROM events WHERE collection = ? AND event_type = 'item_sold'
               ORDER BY valid_ts DESC LIMIT ?""", (collection, limit))
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

    def event_mix(self, collection: str | None, start: float, end: float) -> list[dict[str, Any]]:
        where = "valid_ts>=? AND valid_ts<?" + (" AND collection=?" if collection else "")
        args: list[Any] = [start, end] + ([collection] if collection else [])
        cur = self.conn.execute(
            f"SELECT event_type, COUNT(*) FROM events WHERE {where} GROUP BY event_type ORDER BY 2 DESC", args)
        return [{"event_type": t, "n": n} for t, n in cur]
