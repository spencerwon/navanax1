"""Landing zone -> queryable store. Incremental, restartable, structure only.

docs/07 §1.2: "Land first, normalize second." This is the second step. It
reads the raw frames the stream consumer landed, parses each into one row,
and appends it to the analytical store -- keeping a watermark per landing
file so the next pass picks up only what is new. Because landing files close
frames on a cadence (REQ-D-26a), an OPEN file that is still being written can
be read up to its last complete frame; the next pass reads the frames that
arrived since.

What this module deliberately does NOT do (docs/06 §4, the assumption /
structure boundary): no thresholds, no wash-trade judgement, no "fair value",
no smoothing. A row is what OpenSea said, with its two timestamps:

    observed_at   when WE learned it            (envelope `_recv`)
    valid_at      when it was TRUE on the market (payload `event_timestamp`)

That pair is what makes point-in-time queries possible later (REQ-F-04).

Store: SQLite for now, at `config.analytical.path`. docs/07 specifies DuckDB;
the substitution is deliberate and recorded in docs/08 §5 -- the container
this was written in cannot install DuckDB, and shipping an untested
analytical store to record irreplaceable data would repeat BUG-010. The
schema and every query here are plain SQL that DuckDB also accepts, so the
swap is a connection string, not a rewrite.

Prices: the stream reports every order in three forms at its own timestamp --
`base_price` (integer wei of the payment token), `payment_token.eth_price`
(the ORDER's value in ETH) and `payment_token.usd_price` (the ORDER's value in
USD). The USD figure is therefore a historical rate at the event's own
timestamp with a named provider (OpenSea), which is what REQ-D-25 asks for;
the implied ETH/USD rate is stored alongside so it can be audited against a
second provider later.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .landing import read_file

log = logging.getLogger("navanax.normalize")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    run            TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    file           TEXT NOT NULL,
    observed_at    TEXT NOT NULL,        -- when we learned it (envelope _recv)
    valid_at       TEXT,                 -- when it was true (event_timestamp); NULL if absent
    observed_ts    REAL NOT NULL,        -- the same two, as epoch seconds, for bucketing in SQL
    valid_ts       REAL,
    event_type     TEXT NOT NULL,
    collection     TEXT,
    chain          TEXT,
    contract       TEXT,
    token_id       TEXT,
    order_hash     TEXT,
    maker          TEXT,
    taker          TEXT,
    price_wei      TEXT,                 -- integer as text: never lose precision
    price_eth      REAL,
    price_usd      REAL,
    implied_ethusd REAL,                 -- usd/eth at this event, provider: opensea
    price_basis    TEXT,                 -- order_value | units_x_rate | wei_only | reported_value(_unverified)
    payment_symbol TEXT,
    quantity       INTEGER,
    expiration_at  TEXT,
    expiration_ts  REAL,                 -- expiration_at as epoch seconds: compared numerically, never as text
    tx_hash        TEXT,
    PRIMARY KEY (run, seq)
);
CREATE INDEX IF NOT EXISTS ix_events_coll_valid ON events(collection, valid_ts);
CREATE INDEX IF NOT EXISTS ix_events_type_valid ON events(event_type, valid_ts);
-- Order lifecycle joins (live book, bid lifetimes) look up BY order_hash then
-- filter by type and time. Without this composite the planner chose the
-- (event_type, valid_ts) index for the join side and scanned every
-- cancellation for every bid: 2,000 rows in 29 s on the first hour of real
-- data. With it: milliseconds.
DROP INDEX IF EXISTS ix_events_order;   -- pre-composite name; harmless if absent
CREATE INDEX IF NOT EXISTS ix_events_lifecycle  ON events(order_hash, event_type, valid_ts);
CREATE INDEX IF NOT EXISTS ix_events_observed   ON events(observed_ts);

-- One row per landing file: how far we have read it. Re-runnable.
CREATE TABLE IF NOT EXISTS watermarks (
    file        TEXT PRIMARY KEY,
    last_seq    INTEGER NOT NULL,
    rows        INTEGER NOT NULL DEFAULT 0,
    status      TEXT,                    -- manifest status when last read
    updated_at  TEXT NOT NULL
);

-- Frames we could not parse into a row. Never dropped: the raw bytes are in
-- the landing zone, and this table says which ones need a smarter parser.
CREATE TABLE IF NOT EXISTS unparsed (
    run         TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    file        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (run, seq)
);
"""

MARKET_EVENTS = {
    "item_listed", "item_sold", "item_transferred", "item_cancelled",
    "item_received_bid", "item_received_offer", "item_metadata_updated",
    "collection_offer", "trait_offer", "order_invalidate", "order_revalidate",
}


def iso_to_ts(s: str | None) -> float | None:
    """ISO-8601 (as OpenSea and our envelope write it) -> epoch seconds, or None."""
    if not s or not isinstance(s, str):
        return None
    from datetime import datetime, timezone
    t = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def _token_id_from(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """(contract, token_id). The stream puts these in more than one place."""
    item = payload.get("item") or {}
    nft_id = item.get("nft_id") or ""
    parts = nft_id.split("/")
    contract = parts[1] if len(parts) >= 2 else None
    token = parts[2] if len(parts) >= 3 else None
    if token is None:
        # item_received_bid puts the id in the Seaport consideration, not nft_id
        params = (payload.get("protocol_data") or {}).get("parameters") or {}
        for side in ("consideration", "offer"):
            for entry in params.get(side) or []:
                if entry.get("itemType") in (2, 3, 4, 5) and entry.get("identifierOrCriteria"):
                    token = str(entry["identifierOrCriteria"])
                    contract = contract or entry.get("token")
                    break
            if token:
                break
    if contract is None:
        acc = payload.get("asset_contract_criteria") or {}
        contract = acc.get("address")
    return contract, token


def _resolve_price(event: str, wei: Any, pt: dict[str, Any], quantity: Any = 1) -> tuple[float | None, float | None, str | None]:
    """(price_eth, price_usd, basis) PER ITEM. Self-checking, because the stream is not consistent.

    Found in the first hour of real data, not in any document: on a bid or a
    listing, `payment_token.eth_price` / `usd_price` are the ORDER'S VALUE
    (0.88 WETH bid -> eth_price "0.88"). On a SALE they are the TOKEN'S RATE
    (1.43 WETH sale -> eth_price "1.000863", the WETH/ETH rate). A parser that
    trusted the field as a value would have recorded that sale at 1.00 ETH --
    a 30% error, silent, in the one event type every metric downstream cares
    about most.

    So the field is not trusted; it is checked. `units` (wei / 10^decimals)
    is always the PER-ITEM amount of the payment token. If eth_price matches
    units x quantity, it is the order's total value (a 2-item collection offer
    at 0.453 each reports eth_price "0.906" -- found in real data, 29 rows in
    the first hour) and the per-item price is eth_price / quantity. If it does
    not match but looks like a per-unit rate, the value is units x rate. The
    basis used is recorded on the row for audit.
    """
    ep, up = _f(pt.get("eth_price")), _f(pt.get("usd_price"))
    units: float | None = None
    try:
        if wei is not None and pt.get("decimals") is not None:
            units = int(wei) / (10 ** int(pt["decimals"]))
    except (TypeError, ValueError):
        units = None
    symbol = str(pt.get("symbol") or "").upper()
    eth_like = symbol in ("ETH", "WETH")
    try:
        qty = max(1, int(quantity or 1))
    except (TypeError, ValueError):
        qty = 1

    if ep is None and up is None:
        if units is not None and eth_like:
            return units, None, "wei_only"
        return None, None, None
    if units is None or ep is None:
        return ep, up, "reported_value"
    if abs(ep - units * qty) <= 0.01 * max(units * qty, 1e-12):
        return ep / qty, (up / qty) if up is not None else None, "order_value"   # bids, listings, offers
    if event == "item_sold" or (eth_like and abs(ep - 1.0) < 0.05):
        return units * ep, (units * up) if up is not None else None, "units_x_rate"   # sales
    # Neither form fits. Keep the reported figures, but say so: an audit can
    # find every row where the two disagreed.
    return ep, up, "reported_value_unverified"


def parse_event(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """One landing envelope -> one row dict, or None for control/unparseable.

    Structure only. Nothing here decides what a price MEANS.
    """
    if envelope.get("_topic") == "__control__":
        return None
    try:
        raw = json.loads(envelope["raw"])
    except (KeyError, TypeError, ValueError):
        return None
    # Phoenix v2 array or v1 map (BUG-20260909-002)
    if isinstance(raw, list) and len(raw) >= 5:
        event, outer = raw[3], raw[4]
    elif isinstance(raw, dict):
        event, outer = raw.get("event"), raw.get("payload")
    else:
        return None
    if not isinstance(event, str) or event not in MARKET_EVENTS:
        return None
    if not isinstance(outer, dict):
        return None
    p = outer.get("payload") if isinstance(outer.get("payload"), dict) else outer

    contract, token_id = _token_id_from(p)
    pt = p.get("payment_token") or {}
    price_wei = p.get("base_price") or p.get("sale_price")
    price_eth, price_usd, price_basis = _resolve_price(event, price_wei, pt, p.get("quantity"))
    implied = (price_usd / price_eth) if (price_usd and price_eth) else None

    valid_at = p.get("event_timestamp") or outer.get("sent_at")
    tx = p.get("transaction") or {}
    return {
        "run": envelope.get("_run"),
        "seq": envelope.get("_seq"),
        "observed_at": envelope.get("_recv"),
        "valid_at": valid_at if isinstance(valid_at, str) else None,
        "observed_ts": iso_to_ts(envelope.get("_recv")) or 0.0,
        "valid_ts": iso_to_ts(valid_at if isinstance(valid_at, str) else None),
        "event_type": event,
        "collection": (p.get("collection") or {}).get("slug"),
        "chain": p.get("chain") or ((p.get("item") or {}).get("chain") or {}).get("name"),
        "contract": contract,
        "token_id": token_id,
        "order_hash": p.get("order_hash"),
        "maker": (p.get("maker") or {}).get("address") or (p.get("from_account") or {}).get("address"),
        "taker": (p.get("taker") or {}).get("address") or (p.get("to_account") or {}).get("address"),
        "price_wei": str(price_wei) if price_wei is not None else None,
        "price_eth": price_eth,
        "price_usd": price_usd,
        "implied_ethusd": implied,
        "price_basis": price_basis,
        "payment_symbol": pt.get("symbol"),
        "quantity": p.get("quantity"),
        "expiration_at": p.get("expiration_date"),
        "expiration_ts": iso_to_ts(p.get("expiration_date")),
        "tx_hash": tx.get("hash") if isinstance(tx, dict) else None,
    }


COLS = ["run", "seq", "file", "observed_at", "valid_at", "observed_ts", "valid_ts",
        "event_type", "collection",
        "chain", "contract", "token_id", "order_hash", "maker", "taker", "price_wei",
        "price_eth", "price_usd", "implied_ethusd", "price_basis", "payment_symbol", "quantity",
        "expiration_at", "expiration_ts", "tx_hash"]


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for stores built by earlier versions. The analytical
    store is DERIVED from the landing zone (docs/07 §1); adding a column and
    filling it from a column already present is a re-fold, not an edit of the
    record. Nothing here touches the landing zone."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    if cols and "expiration_ts" not in cols:
        conn.create_function("nvx_iso_ts", 1, iso_to_ts)
        with conn:
            conn.execute("ALTER TABLE events ADD COLUMN expiration_ts REAL")
            conn.execute("UPDATE events SET expiration_ts = nvx_iso_ts(expiration_at) WHERE expiration_at IS NOT NULL")


class Normalizer:
    """Incremental landing-zone -> store sync. Safe to call every few seconds."""

    def __init__(self, landing_root: str | Path, db_path: str | Path) -> None:
        self.root = Path(landing_root)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        _migrate(self.conn)
        self.conn.executescript(SCHEMA)

    # -- manifest-driven file discovery ------------------------------------
    def _files(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        mdir = self.root / "_manifest"
        if not mdir.exists():
            return out
        for mp in sorted(mdir.glob("*.json")):
            try:
                data = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.extend(data.get("files", []))
        return out

    def _watermark(self, filename: str) -> tuple[int, str | None]:
        row = self.conn.execute(
            "SELECT last_seq, status FROM watermarks WHERE file=?", (filename,)).fetchone()
        return (row[0], row[1]) if row else (0, None)

    # -- the sync ------------------------------------------------------------
    def sync(self) -> dict[str, int]:
        """Read every new complete frame from every landing file. Returns counts."""
        from datetime import datetime, timezone
        stats = {"files_checked": 0, "files_read": 0, "rows_added": 0, "unparsed": 0}
        for rec in self._files():
            stats["files_checked"] += 1
            fname = rec["filename"]
            last_seq, last_status = self._watermark(fname)
            # A closed file we have fully read never changes again (append-only,
            # and the checksum audit would catch it if it did). Skip it.
            if last_status == "closed" and rec.get("status") == "closed" \
                    and last_seq >= (rec.get("last_seq") or 0):
                continue
            path = self.root / fname
            if not path.exists():
                continue
            stats["files_read"] += 1
            rows: list[tuple] = []
            bad: list[tuple] = []
            high = last_seq
            try:
                for env in read_file(path, tolerate_truncation=True):
                    seq = env.get("_seq", 0)
                    if seq <= last_seq:
                        continue
                    high = max(high, seq)
                    row = parse_event(env)
                    if row is None:
                        if env.get("_topic") != "__control__":
                            bad.append((env.get("_run"), seq, fname, "unrecognised frame"))
                        continue
                    row["file"] = fname
                    rows.append(tuple(row.get(c) for c in COLS))
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the sync
                log.warning("could not read %s: %s", fname, exc)
                continue
            with self.conn:
                if rows:
                    self.conn.executemany(
                        f"INSERT OR IGNORE INTO events ({','.join(COLS)}) "
                        f"VALUES ({','.join('?' * len(COLS))})", rows)
                if bad:
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO unparsed (run, seq, file, reason) VALUES (?,?,?,?)", bad)
                self.conn.execute(
                    """INSERT INTO watermarks (file, last_seq, rows, status, updated_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(file) DO UPDATE SET
                         last_seq=excluded.last_seq, rows=rows+excluded.rows,
                         status=excluded.status, updated_at=excluded.updated_at""",
                    (fname, high, len(rows), rec.get("status"),
                     datetime.now(timezone.utc).isoformat()))
            stats["rows_added"] += len(rows)
            stats["unparsed"] += len(bad)
        if stats["rows_added"]:
            # Give the planner real statistics: a fresh store with no ANALYZE is
            # where the O(n^2) join plan came from.
            self.conn.execute("ANALYZE")
        return stats

    def close(self) -> None:
        self.conn.close()
