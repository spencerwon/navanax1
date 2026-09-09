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
    criteria_n         INTEGER,          -- count of STRING criteria; NULL = not a criteria-bearing order
    criteria_numeric_n INTEGER,          -- count of NUMERIC criteria; see order_criteria
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

-- The criteria a trait offer bids on: 1..n of them, ANDed. A side table, not
-- columns: a column form needs a JSON blob and a blob cannot be joined against
-- `traits(collection, trait_type, value, token_id)`, which is the join every
-- match needs. Keyed on (run, seq) because that is the events PRIMARY KEY --
-- `order_hash` is nullable. Values are stored VERBATIM: "Blue" and "blue" are
-- two values until a human says otherwise (the rule traits.py already applies).
CREATE TABLE IF NOT EXISTS order_criteria (
    run         TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    idx         INTEGER NOT NULL,   -- position in the AND list; 0 for the single form
    kind        TEXT NOT NULL,      -- 'string' | 'numeric'
    trait_type  TEXT NOT NULL,
    value       TEXT,               -- string criteria: trait_name, verbatim
    num_min     REAL,               -- numeric criteria, as reported
    num_max     REAL,
    PRIMARY KEY (run, seq, idx)
);
CREATE INDEX IF NOT EXISTS ix_criteria_lookup ON order_criteria(trait_type, value, run, seq);

-- One row per order_hash: when it was placed, when (and how) it ended. The
-- standing-book primitive (docs/06 §4.2, DERIVATION: no assumptions). It exists
-- because three places in metrics.py each had their own idea of what ends an
-- order, and none of them handled a revalidate, a NULL valid_ts terminator, a
-- duplicate cancel, or quantity. Materialised with its own key rather than a
-- view, because the lifecycle queries carry INDEXED BY hints (BUG-040) and
-- SQLite will not accept those against a view.
CREATE TABLE IF NOT EXISTS order_lives (
    order_hash       TEXT PRIMARY KEY,
    collection       TEXT,
    event_type       TEXT,           -- event_type of the PLACEMENT; NULL when unseen
    scope_kind       TEXT,           -- item | collection | trait
    token_id         TEXT,
    maker            TEXT,
    quantity         INTEGER,        -- carried from the placement: 5 is five units of depth
    price_eth        REAL,
    price_usd        REAL,
    t_place          REAL,           -- valid_ts of the first placement
    t_place_observed REAL,           -- observed_ts of that row (docs/06: both clocks)
    t_term           REAL,           -- valid_ts of the FIRST termination at or after t_place
    exit_reason      TEXT,           -- cancelled|invalidated|filled|expired|censored|unknown
    exit_source      TEXT,           -- observed | derived (expiry) | NULL (still standing)
    exit_event_type  TEXT,
    expiration_ts    REAL,
    placement_seen   INTEGER NOT NULL,   -- 0 = orphan termination: left-truncated, counted, never imputed
    revalidated      INTEGER NOT NULL,
    terminations_seen INTEGER NOT NULL,  -- how many X rows named this hash (duplicates are ONE life)
    criteria_n         INTEGER,
    criteria_numeric_n INTEGER,
    method_version   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lives_standing ON order_lives(collection, event_type, t_place, t_term);
CREATE INDEX IF NOT EXISTS ix_lives_term     ON order_lives(collection, t_term);
"""

MARKET_EVENTS = {
    "item_listed", "item_sold", "item_transferred", "item_cancelled",
    "item_received_bid", "item_received_offer", "item_metadata_updated",
    "collection_offer", "trait_offer", "order_invalidate", "order_revalidate",
}

# An order is PLACED by one of these and TERMINATED by one of those. Everything
# in order_lives is folded from these two sets plus order_revalidate.
PLACEMENT_EVENTS = ("item_listed", "item_received_bid", "item_received_offer",
                    "collection_offer", "trait_offer")
TERMINAL_EVENTS = ("item_cancelled", "order_invalidate", "item_sold")
EXIT_REASON_OF = {"item_cancelled": "cancelled", "order_invalidate": "invalidated",
                  "item_sold": "filled"}
SCOPE_KIND_OF = {"item_listed": "item", "item_received_bid": "item",
                 "item_received_offer": "item", "collection_offer": "collection",
                 "trait_offer": "trait"}
# Bump when the fold below changes, so a stored row says which rules made it.
ORDER_LIVES_METHOD = 1


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


def _crit_str(x: Any) -> str | None:
    """A criterion field as stored: verbatim, stripped, NO case folding."""
    if not isinstance(x, str):
        return None
    s = x.strip()
    return s or None


def parse_criteria(p: dict[str, Any]) -> list[dict[str, Any]]:
    """The criteria an order bids on, as rows. Structure only, no judgement.

    Measured payload shape (FACTS 2026-09-09, 39 trait offers):
        trait_criteria:              {trait_type, trait_name}    single; may be null
        trait_criteria_list:         [{trait_type, trait_name}]  an AND across entries
        numeric_trait_criteria_list: [...]                       numeric ranges

    Rules (dataeng §4.2):
      * `trait_criteria_list` non-empty  -> one row per element, idx 0..n-1
      * else `trait_criteria` non-null   -> one row, idx 0
      * numeric entries follow the string ones so `idx` stays unique in the PK
      * `trait_name` -> `value`, verbatim and stripped, never case-folded

    An element we cannot read (no trait_type, or a string criterion with no
    trait_name) produces NO row. That is deliberate and it is why `criteria_n`
    exists: 0 on a trait_offer is the loud-failure marker that the matching
    rule refuses to act on, rather than a silently empty AND that would match
    every token in the collection.
    """
    out: list[dict[str, Any]] = []
    idx = 0
    lst = p.get("trait_criteria_list")
    singles: list[Any] = []
    if isinstance(lst, list) and lst:
        singles = lst
    elif isinstance(p.get("trait_criteria"), dict):
        singles = [p["trait_criteria"]]
    for entry in singles:
        if not isinstance(entry, dict):
            continue
        tt, tn = _crit_str(entry.get("trait_type")), _crit_str(entry.get("trait_name"))
        if tt is None or tn is None:
            continue
        out.append({"idx": idx, "kind": "string", "trait_type": tt, "value": tn,
                    "num_min": None, "num_max": None})
        idx += 1
    num = p.get("numeric_trait_criteria_list")
    if isinstance(num, list):
        for entry in num:
            if not isinstance(entry, dict):
                continue
            tt = _crit_str(entry.get("trait_type"))
            if tt is None:
                continue
            # OpenSea's numeric field names are not pinned by any document we
            # hold; accept the plausible spellings and record what we found.
            lo = _f(next((entry[k] for k in ("min", "min_value", "numeric_min", "value_min")
                          if k in entry), None))
            hi = _f(next((entry[k] for k in ("max", "max_value", "numeric_max", "value_max")
                          if k in entry), None))
            out.append({"idx": idx, "kind": "numeric", "trait_type": tt, "value": None,
                        "num_min": lo, "num_max": hi})
            idx += 1
    return out


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
    criteria = parse_criteria(p)
    # NULL = not a criteria-bearing order. 0 on a trait_offer = we saw one and
    # could not read its criteria -- the marker the matching rule needs.
    carries = event == "trait_offer" or any(
        p.get(k) is not None for k in
        ("trait_criteria", "trait_criteria_list", "numeric_trait_criteria_list"))
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
        "criteria_n": sum(1 for c in criteria if c["kind"] == "string") if carries else None,
        "criteria_numeric_n": sum(1 for c in criteria if c["kind"] == "numeric") if carries else None,
        "criteria": criteria,   # NOT a column: written to order_criteria by sync()
    }


COLS = ["run", "seq", "file", "observed_at", "valid_at", "observed_ts", "valid_ts",
        "event_type", "collection",
        "chain", "contract", "token_id", "order_hash", "maker", "taker", "price_wei",
        "price_eth", "price_usd", "implied_ethusd", "price_basis", "payment_symbol", "quantity",
        "expiration_at", "expiration_ts", "tx_hash", "criteria_n", "criteria_numeric_n"]

CRITERIA_COLS = ["run", "seq", "idx", "kind", "trait_type", "value", "num_min", "num_max"]


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
    # criteria_n / criteria_numeric_n: additive, same precedent -- but UNLIKE
    # expiration_ts there is no existing column to fill them from. The criteria
    # only exist in the raw frames, which live in the landing zone and not here
    # (docs/07 §1.2, "land first, normalize second"). So they stay NULL on rows
    # folded by an earlier version, and NULL fails the `criteria_n > 0` guard --
    # a pre-migration trait offer matches nothing until a re-fold. That is the
    # safe direction. See Normalizer.refold_criteria() / reset_for_refold().
    for col in ("criteria_n", "criteria_numeric_n"):
        if cols and col not in cols:
            with conn:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} INTEGER")


_LIFE_COLS = ["order_hash", "collection", "event_type", "scope_kind", "token_id", "maker",
              "quantity", "price_eth", "price_usd", "t_place", "t_place_observed", "t_term",
              "exit_reason", "exit_source", "exit_event_type", "expiration_ts",
              "placement_seen", "revalidated", "terminations_seen",
              "criteria_n", "criteria_numeric_n", "method_version"]

_LIFE_SELECT = """
    SELECT order_hash, run, seq, event_type, valid_ts, observed_ts, collection, token_id,
           maker, quantity, price_eth, price_usd, expiration_ts, criteria_n, criteria_numeric_n
    FROM events
    WHERE order_hash IS NOT NULL AND event_type IN
          ('item_listed','item_received_bid','item_received_offer','collection_offer',
           'trait_offer','item_cancelled','order_invalidate','item_sold','order_revalidate')
"""


def _fold_one_life(hash_: str, rows: list[tuple], now_ts: float | None = None) -> tuple:
    """Every event naming one order_hash -> one order_lives row. DERIVATION: no
    thresholds, no imputation, no opinion about what matters.

    The rules that are not optional (quant §1.0, tech-lead PR-2):

      * ONE life per hash. Two cancels on one order are one termination, not
        two -- `bid_lifetimes` used to cross-product them (BUG-049).
      * An `order_invalidate` with a LATER `order_revalidate` is not a
        termination: the order re-opened (BUG-050a).
      * A terminator whose `valid_ts` is NULL cannot be placed in time. It does
        NOT leave the order standing forever, and it is not dropped: the life
        is `unknown` and counted (BUG-050c).
      * `quantity` is carried from the placement. A collection offer good for
        five is five units of depth (BUG-050b).
      * Expiry is INFERRED, never observed: when no termination is seen, the
        order carried an expiration, AND that expiration has ALREADY PASSED
        (`expiration_ts < now`), the life ends there with `exit_source='derived'`.
        An expiration still in the future is not a termination -- the order is
        `censored` (still standing as far as we know). Recording a future end
        would be imputing the future (docs/06 §4.3; tech-lead PR-2 review, S1:
        it fabricated 5% of lives and drained the censored bucket to 1).
        No row is written to `events`.
      * A termination with no placement is an orphan: `placement_seen=0`,
        excluded from durations, counted (orphan_rate, quant metric 12).
        `t_place` is never imputed.
    """
    places = [r for r in rows if r[3] in PLACEMENT_EVENTS]
    terms = [r for r in rows if r[3] in TERMINAL_EVENTS]
    revals = [r for r in rows if r[3] == "order_revalidate"]

    timed_places = [r for r in places if r[4] is not None]
    place = (min(timed_places, key=lambda r: (r[4], r[2])) if timed_places
             else (places[0] if places else None))
    t_place = place[4] if place is not None else None

    # A revalidate strictly after an invalidate re-opens the order, so that
    # invalidate is not a termination.
    reval_ts = [r[4] for r in revals if r[4] is not None]

    def _superseded(r: tuple) -> bool:
        return (r[3] == "order_invalidate" and r[4] is not None
                and any(rt > r[4] for rt in reval_ts))

    candidates = [r for r in terms
                  if r[4] is not None and (t_place is None or r[4] >= t_place)
                  and not _superseded(r)]
    term = min(candidates, key=lambda r: (r[4], r[2])) if candidates else None

    src = place if place is not None else (term if term is not None
                                           else (terms[0] if terms else rows[0]))
    if term is not None:
        t_term, reason, source, exit_type = term[4], EXIT_REASON_OF[term[3]], "observed", term[3]
    elif terms and not all(_superseded(r) for r in terms):
        # A terminator exists but cannot be placed in time (NULL valid_ts, or
        # earlier than the placement we saw). Unknown -- counted, never standing.
        t_term, reason, source, exit_type = None, "unknown", "observed", terms[0][3]
    elif place is not None and place[12] is not None and now_ts is not None and place[12] < now_ts:
        t_term, reason, source, exit_type = place[12], "expired", "derived", None
    else:
        t_term, reason, source, exit_type = None, "censored", None, None

    return (
        hash_, src[6], place[3] if place is not None else None,
        SCOPE_KIND_OF.get(place[3]) if place is not None else None,
        src[7], src[8], src[9], src[10], src[11],
        t_place, place[5] if place is not None else None, t_term,
        reason, source, exit_type,
        place[12] if place is not None else src[12],
        1 if places else 0, 1 if revals else 0, len(terms),
        place[13] if place is not None else None,
        place[14] if place is not None else None,
        ORDER_LIVES_METHOD,
    )


def refresh_order_lives(conn: sqlite3.Connection, hashes: list[str] | None = None,
                        now_ts: float | None = None) -> int:
    """(Re)compute order_lives for `hashes`, or for every order when None.

    Called at the end of every sync(), so the standing book is never more than
    one fold behind the events table. docs/07 §1: the normalizer is the one
    writer to this store; readers attach read-only and see whatever the last
    fold produced.

    `now_ts` is the fold time (default: wall clock). An incremental refresh
    ALSO re-folds every life still `censored` whose expiration has since
    passed -- otherwise an order nobody touched again would stay "standing"
    in this table forever, while `standing_sql` (which re-checks expiry at
    query time) already knew better.
    """
    import time as _time
    now_ts = _time.time() if now_ts is None else now_ts
    sql, args = _LIFE_SELECT, []
    if hashes is not None:
        hs = set(hashes)
        hs.update(h for (h,) in conn.execute(
            "SELECT order_hash FROM order_lives WHERE exit_reason='censored' "
            "AND expiration_ts IS NOT NULL AND expiration_ts < ?", (now_ts,)))
        hs = sorted(hs)
        if not hs:
            return 0
        out = 0
        for i in range(0, len(hs), 400):        # SQLite's parameter limit
            chunk = hs[i:i + 400]
            out += _refresh_chunk(conn, sql + f" AND order_hash IN ({','.join('?' * len(chunk))})", chunk, now_ts)
        return out
    return _refresh_chunk(conn, sql, args, now_ts)


def _refresh_chunk(conn: sqlite3.Connection, sql: str, args: list[Any], now_ts: float) -> int:
    cur = conn.execute(sql + " ORDER BY order_hash", args)
    grouped: dict[str, list[tuple]] = {}
    for row in cur:
        grouped.setdefault(row[0], []).append(row)
    lives = [_fold_one_life(h, rows, now_ts) for h, rows in grouped.items()]
    if lives:
        with conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO order_lives ({','.join(_LIFE_COLS)}) "
                f"VALUES ({','.join('?' * len(_LIFE_COLS))})", lives)
    return len(lives)


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
        # A store folded by an earlier version has events but no lives. Build
        # them once, here, rather than letting every reader see an empty book.
        if (self.conn.execute("SELECT COUNT(*) FROM order_lives").fetchone()[0] == 0
                and self.conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM events WHERE order_hash IS NOT NULL)").fetchone()[0]):
            log.info("order_lives is empty on a populated store: building it once")
            log.info("order_lives built: %d orders", refresh_order_lives(self.conn))

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
        stats = {"files_checked": 0, "files_read": 0, "rows_added": 0, "unparsed": 0,
                 "criteria_rows": 0, "lives_refreshed": 0}
        touched: list[str] = []
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
            crit: list[tuple] = []
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
                    for c in row.get("criteria") or []:
                        crit.append((row["run"], row["seq"], c["idx"], c["kind"],
                                     c["trait_type"], c["value"], c["num_min"], c["num_max"]))
                    if row.get("order_hash"):
                        touched.append(row["order_hash"])
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the sync
                log.warning("could not read %s: %s", fname, exc)
                continue
            with self.conn:
                if rows:
                    self.conn.executemany(
                        f"INSERT OR IGNORE INTO events ({','.join(COLS)}) "
                        f"VALUES ({','.join('?' * len(COLS))})", rows)
                if crit:
                    # INSERT OR REPLACE on (run, seq, idx): a re-fold of the same
                    # frame rewrites the same rows, so it is idempotent.
                    self.conn.executemany(
                        f"INSERT OR REPLACE INTO order_criteria ({','.join(CRITERIA_COLS)}) "
                        f"VALUES ({','.join('?' * len(CRITERIA_COLS))})", crit)
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
            stats["criteria_rows"] += len(crit)
        if touched:
            # Refresh only the orders this pass touched. The whole life is
            # recomputed from every event naming that hash, so an out-of-order
            # arrival (a cancel folded before its bid) is corrected, not patched.
            stats["lives_refreshed"] = refresh_order_lives(self.conn, touched)
        if stats["rows_added"]:
            # Give the planner real statistics: a fresh store with no ANALYZE is
            # where the O(n^2) join plan came from.
            self.conn.execute("ANALYZE")
        return stats

    # -- re-folding ----------------------------------------------------------
    def refold_criteria(self) -> dict[str, Any]:
        """Re-parse the criteria of events already in the store.

        This is possible only if the raw frame is stored HERE. It is not:
        docs/07 §1.2 puts the raw bytes in the landing zone and keeps the
        analytical store derived, one parsed row per event. So on this store
        the honest answer is a refusal with the recipe, not a silent no-op --
        the caller must `reset_for_refold()` and `sync()` again, which re-reads
        the landing zone (read-only, as always) and re-parses every frame.

        Nothing here writes to the landing zone under any branch.
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(events)")}
        raw_col = next((c for c in ("raw", "raw_frame") if c in cols), None)
        if raw_col is None:
            missing = self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='trait_offer' "
                "AND criteria_n IS NULL").fetchone()[0]
            return {"raw_frames_available": False, "refolded": 0, "criteria_rows": 0,
                    "trait_offers_needing_refold": missing,
                    "action_required": "reset_for_refold() then sync()",
                    "note": ("the analytical store keeps no raw frames (docs/07 §1.2); "
                             "a re-fold reads them from the landing zone, which is "
                             "read-only and untouched by either call")}
        refolded = crit_rows = 0
        for run, seq, raw in self.conn.execute(
                f"SELECT run, seq, {raw_col} FROM events WHERE {raw_col} IS NOT NULL"):
            row = parse_event({"_run": run, "_seq": seq, "_recv": None, "raw": raw})
            if row is None:
                continue
            refolded += 1
            with self.conn:
                self.conn.execute(
                    "UPDATE events SET criteria_n=?, criteria_numeric_n=? WHERE run=? AND seq=?",
                    (row["criteria_n"], row["criteria_numeric_n"], run, seq))
                for c in row.get("criteria") or []:
                    self.conn.execute(
                        f"INSERT OR REPLACE INTO order_criteria ({','.join(CRITERIA_COLS)}) "
                        f"VALUES ({','.join('?' * len(CRITERIA_COLS))})",
                        (run, seq, c["idx"], c["kind"], c["trait_type"], c["value"],
                         c["num_min"], c["num_max"]))
                    crit_rows += 1
        refresh_order_lives(self.conn)
        return {"raw_frames_available": True, "refolded": refolded,
                "criteria_rows": crit_rows, "action_required": None,
                "note": f"re-parsed in place from events.{raw_col}"}

    def reset_for_refold(self) -> dict[str, int]:
        """Drop every DERIVED row so the next sync() re-folds from the landing zone.

        Deletes, in the ANALYTICAL store only: `events`, `order_criteria`,
        `order_lives`, `unparsed`, `watermarks`. Every one of those is
        reconstructible from the landing zone by re-reading it.

        It does NOT touch:
          * the landing zone -- not one byte, in any code path. The record is
            append-only and irreplaceable (docs/07 §4).
          * `tokens` / `traits` -- those came from metered REST reads, not from
            the landing zone, and re-fetching them would cost ~97 of the
            120/hour budget.

        `events` is `INSERT OR IGNORE` on (run, seq) and `order_criteria` is
        `INSERT OR REPLACE` on (run, seq, idx), so the re-fold is idempotent
        even if it is interrupted and run again.
        """
        counts: dict[str, int] = {}
        with self.conn:
            for t in ("events", "order_criteria", "order_lives", "unparsed", "watermarks"):
                counts[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                self.conn.execute(f"DELETE FROM {t}")
        log.warning("reset_for_refold: cleared %s from the analytical store; "
                    "the landing zone was not touched", counts)
        return counts

    def close(self) -> None:
        self.conn.close()
