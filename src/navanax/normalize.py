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

import errno
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from .landing import read_file

log = logging.getLogger("navanax.normalize")

# How long a connection waits for another one to release a write lock before it
# gives up with "database is locked". The fold holds the write lock for the
# length of one batch insert; a job that only writes `tokens`/`traits` (the
# traits importer) must wait that out rather than fail spuriously.
BUSY_TIMEOUT_MS = 30_000

# errno values that mean "THIS FILESYSTEM does not implement flock", as opposed
# to "another process holds the lock". Defined once and shared with
# `cli._single_instance`, which learned the distinction the expensive way
# (tech-lead finding #4): an SMB/AFP share and some NFS mounts answer EOPNOTSUPP,
# and treating that as contention stops the job entirely and tells the operator
# to kill a process that does not exist.
LOCK_UNSUPPORTED_ERRNOS = frozenset({
    errno.EOPNOTSUPP, errno.ENOLCK, errno.EINVAL, errno.ENOSYS,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
})


class StoreWriterBusyError(RuntimeError):
    """Another process is already folding into this analytical store.

    A distinct type, like `cli.SingleInstanceError`, so a caller can tell
    "someone else owns the store" from any other RuntimeError raised out of a
    fold. It carries the holder's pid so the message can name it: BUG-20260910-067
    happened because two dashboard processes folded into `data/analytics.sqlite`
    alternately and one was killed mid-write, and the operator had no way to see
    which pid to stop.
    """

    def __init__(self, message: str, *, pid: int | None = None,
                 lock_path: Path | str | None = None) -> None:
        super().__init__(message)
        self.pid = pid
        self.lock_path = str(lock_path) if lock_path is not None else None


def writer_lock_path(db_path: str | Path) -> Path:
    """`<store>.lock`, next to the sqlite file -- one lock per store, not per root."""
    p = Path(db_path)
    return p.with_name(p.name + ".lock")


def _parse_lock_text(text: str) -> tuple[int | None, str | None]:
    """`pid=123 started=2026-09-10T...` -> (123, '2026-09-10T...'). Never raises."""
    pid: int | None = None
    since: str | None = None
    for part in (text or "").split():
        if part.startswith("pid="):
            try:
                pid = int(part[4:])
            except ValueError:
                pid = None
        elif part.startswith("started="):
            since = part[8:] or None
    return pid, since


def store_writer_info(db_path: str | Path, *, lock_fh: Any = None) -> dict[str, Any]:
    """Who holds the fold-writer lock on this store: {pid, since, alive, ...}.

    A FREE function, not only a `Normalizer` method, because the caller who most
    needs the answer may have no Normalizer at all: a dashboard running degraded
    on a malformed store still has to say which process owns the store's write
    side (BUG-20260910-067). Read from the lock FILE, so a writer, a reader and a
    degraded process all answer the same question the same way, and a stale pid
    is visible as `alive: false` rather than absent.
    """
    lock = writer_lock_path(db_path)
    info: dict[str, Any] = {"pid": None, "since": None, "alive": None,
                            "this_process": False, "lock": str(lock)}
    try:
        pid, since = _parse_lock_text(lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return info
    info["pid"], info["since"] = pid, since
    info["alive"] = _pid_alive(pid)
    info["this_process"] = pid == os.getpid() and lock_fh is not None
    return info


def writer_lock_held(db_path: str | Path) -> bool | None:
    """Is the fold-writer lock ACTUALLY held right now? True / False / None (cannot tell).

    Asks the lock rather than the lock file. "Is the pid in the file still alive?"
    is the wrong question and gives false refusals in both directions: a process
    that closed its Normalizer but is still running leaves its own pid in the
    file (the writer that just released is the commonest case of all), and a pid
    can be reused by something unrelated. The only authority on whether a lock is
    held is the lock.

    Tested by taking it non-blocking and giving it straight back. Returns None
    where flock is unavailable or the filesystem does not implement it -- the
    caller must then fall back to the pid heuristic and SAY that is what it did,
    because "cannot tell" and "not held" are different answers.
    """
    lock = writer_lock_path(db_path)
    if not lock.exists():
        return False
    try:
        import fcntl
    except ImportError:
        return None
    try:
        fh = lock.open("a+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in LOCK_UNSUPPORTED_ERRNOS:
                return None
            return True                  # somebody else holds it
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


def rebuild_recipe(db_path: str | Path) -> str:
    """What to do about a corrupt analytical store. One sentence, in one place.

    The store is DERIVED: every row in it is a fold of the landing zone, which is
    append-only and untouched by any of this. So the answer to corruption is
    never a repair and never an edit -- it is to move the file aside under a
    dated name and let the next start re-fold it. Naming the recipe here rather
    than in the message that happens to notice the fault means the dashboard,
    the CLI and `rebuild-store.command` all say the same thing.
    """
    p = Path(db_path)
    stamp = datetime.now(timezone.utc).date().isoformat()
    return (f"The analytical store is DERIVED -- every row in it is a fold of the landing "
            f"zone, which is append-only and was NOT touched. Rebuild it: move it aside "
            f"under a dated name (`mv {p} {p}.corrupt-{stamp}`, which "
            f"rebuild-store.command does for you), then restart the dashboard -- it re-folds "
            f"from the landing zone on the next start. Never delete it, never edit it, and "
            f"never touch data/landing.")


def _pid_alive(pid: int | None) -> bool | None:
    """True / False / None when this process may not ask (permission)."""
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # it exists; it just is not ours
    except OSError:
        return None

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
    -- PR-10, redundant stream. `conn` is the connection label the frame was
    -- folded out of ('a' = the primary landing root); it is derived from the
    -- ROOT, so not one byte of the landing envelope changed to carry it.
    -- `dedup_key` is NULL for a row we cannot prove a duplicate of anything --
    -- see `dedup_key()` below for why that is keyed on `event_timestamp` and
    -- never on the coalesced `valid_at` (E-W3).
    conn           TEXT,
    dedup_key      TEXT,
    -- token_id as a NUMBER, so #10 sorts after #9 instead of before it (design §8.1.1,
    -- the Operator asked for this by name). GENERATED, never written: a stored copy
    -- can drift from token_id, an expression cannot. It is NULL unless token_id is
    -- EXACTLY the decimal rendering of an integer -- '007' and '0x1f' are NULL rather
    -- than 7 and 0, because a token whose id is not a plain number has no number and
    -- guessing one would put it somewhere in the sort order that is not true.
    token_num INTEGER GENERATED ALWAYS AS (
        CASE WHEN token_id IS NOT NULL AND CAST(CAST(token_id AS INTEGER) AS TEXT) = token_id
             THEN CAST(token_id AS INTEGER) END) VIRTUAL,
    PRIMARY KEY (run, seq)
);
CREATE INDEX IF NOT EXISTS ix_events_coll_valid ON events(collection, valid_ts);
CREATE INDEX IF NOT EXISTS ix_events_type_valid ON events(event_type, valid_ts);
-- The ledger's indexes are NOT here: they are created by `ensure_ledger_indexes()`
-- below, which checks the columns exist first. See its docstring for why.
-- Order lifecycle joins (live book, bid lifetimes) look up BY order_hash then
-- filter by type and time. Without this composite the planner chose the
-- (event_type, valid_ts) index for the join side and scanned every
-- cancellation for every bid: 2,000 rows in 29 s on the first hour of real
-- data. With it: milliseconds.
DROP INDEX IF EXISTS ix_events_order;   -- pre-composite name; harmless if absent
CREATE INDEX IF NOT EXISTS ix_events_lifecycle  ON events(order_hash, event_type, valid_ts);
CREATE INDEX IF NOT EXISTS ix_events_observed   ON events(observed_ts);
-- The duplicate-fraction monitor groups a rolling arrival window by dedup_key.
-- Without this it is a full scan of `events` every time Health is opened.
CREATE INDEX IF NOT EXISTS ix_events_dedup      ON events(observed_ts, dedup_key);
-- `metrics` resolves duplicates with `rowid = (SELECT MIN(rowid) ... WHERE dedup_key=?)`,
-- which is a seek per row with this index and a table scan per row without it.
CREATE INDEX IF NOT EXISTS ix_events_dedupkey   ON events(dedup_key);
-- `MetricEngine.multi_connection()` asks MIN(conn), MAX(conn) and "is there a NULL"
-- once per fold. With this index those are three seeks; without it they are three
-- scans of a table that will be millions of rows deep.
CREATE INDEX IF NOT EXISTS ix_events_conn       ON events(conn);

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
    -- PR-10. `duplicates_merged` is how many rows of this order's history were
    -- the SAME event arriving on the second connection and were collapsed here
    -- (E-W4/C8: dedup is resolved in this materialised derivation, never in a
    -- view, because the lifecycle queries carry INDEXED BY hints that SQLite
    -- will not accept against a view).
    -- `terminations_undedupable` is how many of `terminations_seen` arrived with
    -- NO `event_timestamp` and therefore could not be proved a duplicate of
    -- anything. Those rows are counted and never merged -- but they are also the
    -- ONLY part of `terminations_seen` that a redundant stream can inflate, so
    -- they are named separately rather than hidden inside the total.
    duplicates_merged        INTEGER NOT NULL DEFAULT 0,
    terminations_undedupable INTEGER NOT NULL DEFAULT 0,
    -- How many rows about this order each connection actually delivered.
    -- `deliveries_a` is the PRIMARY connection (a row with `conn` NULL is the
    -- primary, which is every row folded before PR-10); `deliveries_b` is every
    -- other connection summed. They are recorded because one-per-key dedup
    -- DISCARDS multiplicity, and a number that was thrown away and not written
    -- down cannot be audited afterwards.
    deliveries_a             INTEGER NOT NULL DEFAULT 0,
    deliveries_b             INTEGER NOT NULL DEFAULT 0,
    -- Dedup keys in this order's history that two connections each saw, a
    -- DIFFERENT number of times (the A x 1 / B x 2 shape a rejoin-replay makes).
    -- Every one of these is a place where one-per-key may have undercounted, or
    -- where one socket replayed; we cannot tell which, so we count it rather
    -- than guess. Always 0 under a single connection.
    multiplicity_disagreements INTEGER NOT NULL DEFAULT 0,
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
#: The connection label a row with no `conn` belongs to: the primary connection,
#: which is every row ever folded before the redundant stream existed. Defined
#: here rather than imported from `redundancy` so the fold has no dependency on
#: the feature that made it necessary.
PRIMARY_CONN = "a"

# Bump when the fold below changes, so a stored row says which rules made it.
#   1  the original fold (PR-2)
#   2  PR-10: rows the two redundant connections both delivered are collapsed
#      by `dedup_key` before the life is folded (max-over-connections; WRONG,
#      it doubled on a B replay -- BUG-20260911-073)
#   3  PR-10 as corrected: ONE row per dedup_key, with the discarded
#      multiplicity counted in `deliveries_a`/`deliveries_b` and
#      `multiplicity_disagreements` rather than folded into a total
ORDER_LIVES_METHOD = 3

#: Unit separator. No field VALUE can contain it, so no combination of values can
#: forge a field boundary and collide with a different event's key.
_US = "\x1f"

#: The dedup key's fields, in order. Deliberately EXCLUDED: `observed_at`,
#: `_recv`, `_seq`, `run`, `sent_at`. Every one of those differs between two
#: independent sockets by construction, and including any of them would make the
#: key useless and every count double.
DEDUP_KEY_FIELDS = ("event_type", "order_hash", "event_timestamp", "tx_hash",
                    "contract", "token_id", "maker", "price_wei", "quantity")


def dedup_key(row: dict[str, Any]) -> str | None:
    """A key identical on both connections for the same event, or None.

    KEYED ON `event_timestamp`, NEVER ON `valid_at` (E-W3). `valid_at` is
    `event_timestamp or sent_at`, and `sent_at` is the Phoenix envelope's
    per-message push timestamp: there is no guarantee whatsoever that two
    independent sockets are pushed at the same instant. Keying on the coalesced
    value would leave every timestamp-less duplicate in place, which is dataeng
    failure mode 3 -- counts double, rates double, and the backtest improves.

    So a row with no `event_timestamp` returns **None**: un-dedupable. It is
    counted (sync stats, `/api/health.dedup`, `order_lives.terminations_undedupable`)
    and it is never merged with anything. Guessing that two such rows are the same
    event would be imputation, and imputation in the flattering direction.

    WIDENED past the requested `(event_type, order_hash, event_timestamp)` for two
    reasons the tech-lead's E-U3 names: `order_hash` is NULL for
    `item_transferred`, for some `item_sold` shapes and for
    `item_metadata_updated`, so the bare triple would merge two genuinely distinct
    transfers in the same second; and `tx_hash` is the natural disambiguator for
    exactly those two types and is already parsed and stored.
    """
    ets = row.get("event_timestamp")
    if not isinstance(ets, str) or not ets:
        return None
    import hashlib
    payload = _US.join("" if row.get(f) is None else str(row.get(f))
                       for f in DEDUP_KEY_FIELDS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def iso_to_ts(s: str | None) -> float | None:
    """ISO-8601 (as OpenSea and our envelope write it) -> epoch seconds, or None."""
    if not s or not isinstance(s, str):
        return None
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

    # `event_ts` is the server-assigned field and the ONLY one that is identical
    # on two independent sockets. `valid_at` keeps its historical coalescing so
    # nothing downstream changes -- but the dedup key is built from `event_ts`
    # alone, because that is the whole of E-W3.
    event_ts = p.get("event_timestamp")
    event_ts = event_ts if isinstance(event_ts, str) else None
    valid_at = p.get("event_timestamp") or outer.get("sent_at")
    tx = p.get("transaction") or {}
    criteria = parse_criteria(p)
    # NULL = not a criteria-bearing order. 0 on a trait_offer = we saw one and
    # could not read its criteria -- the marker the matching rule needs.
    carries = event == "trait_offer" or any(
        p.get(k) is not None for k in
        ("trait_criteria", "trait_criteria_list", "numeric_trait_criteria_list"))
    row = {
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
    row["dedup_key"] = dedup_key({**row, "event_timestamp": event_ts})
    return row


COLS = ["run", "seq", "file", "observed_at", "valid_at", "observed_ts", "valid_ts",
        "event_type", "collection",
        "chain", "contract", "token_id", "order_hash", "maker", "taker", "price_wei",
        "price_eth", "price_usd", "implied_ethusd", "price_basis", "payment_symbol", "quantity",
        "expiration_at", "expiration_ts", "tx_hash", "criteria_n", "criteria_numeric_n",
        # PR-10. `conn` is set by sync() from the landing root the frame came out
        # of, not by parse_event -- the parser sees one frame and has no idea
        # which socket delivered it.
        "conn", "dedup_key"]

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
    # PR-10: `conn` and `dedup_key`. Additive, and like criteria_n there is no
    # existing column to fill them from -- `valid_at` is ALREADY coalesced with
    # `sent_at`, so a row folded by an earlier version cannot say whether it
    # carried an `event_timestamp`. They stay NULL, which reads as "un-dedupable"
    # and is the safe direction: nothing is ever merged on a guess. A re-fold
    # (`reset_for_refold()` then `sync()`) fills them from the landing zone.
    # `/api/health.dedup` carries `pre_migration_rows` so that count is never
    # mistaken for a real rate of timestamp-less events.
    for col in ("conn", "dedup_key"):
        if cols and col not in cols:
            with conn:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
    lcols = {r[1] for r in conn.execute("PRAGMA table_info(order_lives)")}
    for col in ("duplicates_merged", "terminations_undedupable",
                "deliveries_a", "deliveries_b", "multiplicity_disagreements"):
        if lcols and col not in lcols:
            with conn:
                conn.execute(
                    f"ALTER TABLE order_lives ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    # token_num: the numeric sort key the ledger needs (design §8.1.1). Additive and
    # GENERATED, so the migration writes no data at all -- the expression is evaluated
    # from `token_id`, which is already there, and can never disagree with it.
    # Guarded on `token_id` existing: a store so old (or so abbreviated) that it has no
    # token_id column has nothing to derive a token number FROM, and adding a generated
    # column over a missing one is an error at ALTER time, not at query time.
    #
    # `PRAGMA table_info` does NOT list a VIRTUAL generated column -- only `table_xinfo`
    # does. Asking the wrong pragma here would re-ALTER an existing token_num on every
    # open and raise "duplicate column"; asking it in `ensure_ledger_indexes` would skip
    # the token_num index on a store that has the column. Both use xinfo.
    xcols = {r[1] for r in conn.execute("PRAGMA table_xinfo(events)")}
    if cols and "token_num" not in xcols and "token_id" in cols:
        with conn:
            conn.execute(
                "ALTER TABLE events ADD COLUMN token_num INTEGER GENERATED ALWAYS AS ("
                " CASE WHEN token_id IS NOT NULL AND CAST(CAST(token_id AS INTEGER) AS TEXT) = token_id"
                " THEN CAST(token_id AS INTEGER) END) VIRTUAL")


#: The ledger's sortable columns and the index each one needs (docs/08 §4e).
#: `metrics.LEDGER_SORTS` names these by name and `test_ledger_refuses_a_sort_it_has_no
#: _index_for` asserts each one exists in a real store, so the whitelist and the schema
#: cannot drift apart.
LEDGER_INDEXES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("ix_events_coll_tokennum", ("collection", "token_num", "valid_ts"),
     "CREATE INDEX IF NOT EXISTS ix_events_coll_tokennum ON events(collection, token_num, valid_ts)"),
    ("ix_events_coll_maker", ("collection", "maker", "valid_ts"),
     "CREATE INDEX IF NOT EXISTS ix_events_coll_maker ON events(collection, maker, valid_ts)"),
    ("ix_events_coll_type", ("collection", "event_type", "valid_ts"),
     "CREATE INDEX IF NOT EXISTS ix_events_coll_type ON events(collection, event_type, valid_ts)"),
    ("ix_events_coll_price", ("collection", "price_eth", "valid_ts"),
     "CREATE INDEX IF NOT EXISTS ix_events_coll_price ON events(collection, price_eth, valid_ts)"),
    ("ix_events_coll_observed", ("collection", "observed_ts"),
     "CREATE INDEX IF NOT EXISTS ix_events_coll_observed ON events(collection, observed_ts)"),
)


def ensure_ledger_indexes(conn: sqlite3.Connection) -> list[str]:
    """Create the ledger's five indexes, skipping (and NAMING) any whose columns are absent.

    They are not in `SCHEMA` because `executescript` cannot be conditional, and an
    index over a column an older store never had would make the whole schema step
    fail -- i.e. the dashboard would refuse to open rather than opening without one
    sort. The ledger's own refusal is the right place for that failure: a sort whose
    index is missing is refused by name, with the reason, when it is asked for.

    Returns the names of the indexes that were skipped, so a caller can say so.
    """
    # xinfo, not info: `token_num` is a VIRTUAL generated column and table_info hides it.
    cols = {r[1] for r in conn.execute("PRAGMA table_xinfo(events)")}
    if not cols:
        return [name for name, _, _ in LEDGER_INDEXES]
    skipped: list[str] = []
    for name, needs, sql in LEDGER_INDEXES:
        missing = [c for c in needs if c not in cols]
        if missing:
            skipped.append(name)
            log.warning("ledger index %s not created: events has no column %s -- the ledger will "
                        "refuse that sort by name rather than scanning the table", name, ", ".join(missing))
            continue
        with conn:
            conn.execute(sql)
    return skipped


_LIFE_COLS = ["order_hash", "collection", "event_type", "scope_kind", "token_id", "maker",
              "quantity", "price_eth", "price_usd", "t_place", "t_place_observed", "t_term",
              "exit_reason", "exit_source", "exit_event_type", "expiration_ts",
              "placement_seen", "revalidated", "terminations_seen",
              "criteria_n", "criteria_numeric_n",
              "duplicates_merged", "terminations_undedupable",
              "deliveries_a", "deliveries_b", "multiplicity_disagreements",
              "method_version"]

_LIFE_SELECT = """
    SELECT order_hash, run, seq, event_type, valid_ts, observed_ts, collection, token_id,
           maker, quantity, price_eth, price_usd, expiration_ts, criteria_n, criteria_numeric_n,
           conn, dedup_key
    FROM events
    WHERE order_hash IS NOT NULL AND event_type IN
          ('item_listed','item_received_bid','item_received_offer','collection_offer',
           'trait_offer','item_cancelled','order_invalidate','item_sold','order_revalidate')
"""


#: Column positions inside a `_LIFE_SELECT` row, named so the fold below reads.
_R_RUN, _R_SEQ, _R_OBSERVED, _R_CONN, _R_DEDUP = 1, 2, 5, 15, 16


class DedupOutcome(NamedTuple):
    """What `_dedupe_life_rows` decided, with every number it threw away named."""

    kept: list[tuple]
    merged: int                      # rows collapsed into a surviving copy
    undedupable: int                 # rows with no dedup_key: kept, never merged
    deliveries: dict[str, int]       # connection label -> rows that connection gave us
    disagreements: int               # dedup keys the connections delivered a DIFFERENT number of times


def _dedupe_life_rows(rows: list[tuple]) -> DedupOutcome:
    """ONE row per `dedup_key`. Everything discarded is counted, never silent.

    THE WHOLE OF PR-10'S DEDUP LIVES HERE, and it lives here rather than in a
    view on purpose (E-W4, C8). `metrics.py` carries `INDEXED BY ix_events_lifecycle`
    on three lifecycle queries -- the hint that turned 29 s of bid lifetimes into
    milliseconds (BUG-20260909-040) -- and SQLite does not accept `INDEXED BY`
    against a view. Resolving dedup inside `order_lives`, which is materialised
    and has its own indexes, is the only form that does not silently reintroduce
    a fixed S3.

    THE RULE: one row per dedup key, full stop. Two rows that share a key are one
    event, whichever connections they came from and however many copies each one
    sent.

    THIS REPLACES A MAX-OVER-CONNECTIONS RULE THAT WAS WRONG, and the way it was
    wrong is worth keeping written down (BUG-20260911-073). "A duplicate is one
    copy per connection" sounds careful and doubles in the commonest redundant
    shape there is: B drops, rejoins, and REPLAYS -- A delivers a cancellation
    once, B delivers it twice, max(1, 2) = 2, and one real cancellation is
    recorded as two. Two events doubling is not a rounding error in a system whose
    fifth rule is that a surprisingly good result is evidence of a bug.

    WHAT ONE-PER-KEY COSTS, stated rather than hidden. A genuine repeat delivery
    of an identical event down ONE socket is now recorded as one event. That
    direction is an UNDERCOUNT -- it understates activity, which is the safe
    direction -- and it is unavoidable: two rows agreeing on `event_type`,
    `order_hash`, `event_timestamp`, `tx_hash`, contract, token, maker, price and
    quantity are indistinguishable from one event delivered twice, because that
    is what a duplicate IS. The ambiguity is not resolved by guessing; it is
    COUNTED, as `deliveries` per connection and as `disagreements`, and it is
    surfaced on `/api/health.dedup` and in the sync stats so nobody has to infer
    it from a total.

    `disagreements` is the number of dedup keys that at least two connections saw
    and saw a DIFFERENT number of times -- the A x 1 / B x 2 shape above. It is
    the honest marker of "here is where one-per-key may have undercounted, or
    where one socket replayed". Under a single connection it is always 0, because
    no key has two connections.

    Which copy survives: the one with the LOWEST `observed_ts`, tie-broken by
    `(run, seq)`. Lowest-first is not arbitrary -- it is the honest "when did we
    first learn this", which is exactly what redundancy buys and exactly the
    figure any stream-lag number should use.

    A row with no `dedup_key` (no `event_timestamp` -- E-W3) is KEPT, always, and
    counted separately. It is never merged with anything, because there is no
    field on it that two independent sockets are guaranteed to agree on.
    """
    best: dict[str, int] = {}
    per_key_conn: dict[str, dict[str, int]] = {}
    deliveries: dict[str, int] = {}
    undedupable = 0
    for i, r in enumerate(rows):
        label = r[_R_CONN] or PRIMARY_CONN
        deliveries[label] = deliveries.get(label, 0) + 1
        k = r[_R_DEDUP]
        if k is None:
            undedupable += 1
            continue
        seen = per_key_conn.setdefault(k, {})
        seen[label] = seen.get(label, 0) + 1
        cur = best.get(k)
        if cur is None or ((r[_R_OBSERVED], r[_R_RUN], r[_R_SEQ])
                           < (rows[cur][_R_OBSERVED], rows[cur][_R_RUN], rows[cur][_R_SEQ])):
            best[k] = i
    kept = [r for i, r in enumerate(rows) if r[_R_DEDUP] is None or best[r[_R_DEDUP]] == i]
    disagreements = sum(1 for counts in per_key_conn.values()
                        if len(counts) > 1 and min(counts.values()) != max(counts.values()))
    return DedupOutcome(kept, len(rows) - len(kept), undedupable, deliveries, disagreements)


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
      * PR-10: rows sharing a `dedup_key` are ONE event here, however many
        connections sent them and however many copies each sent. So a KEYABLE
        row can no longer inflate `terminations_seen` at all -- not by a second
        connection, not by a replay after a rejoin. The only residual ambiguity
        is a termination with NO `event_timestamp`, which cannot be proved a
        duplicate of anything: those are counted per row AND named separately in
        `terminations_undedupable`, so the inflatable part of the total is always
        visible next to it. `multiplicity_disagreements` names the other half of
        the honesty: the keys where the connections disagreed about how many
        copies there were, i.e. where one-per-key may have undercounted.
    """
    outcome = _dedupe_life_rows(rows)
    rows = outcome.kept
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
        outcome.merged, sum(1 for r in terms if r[_R_DEDUP] is None),
        outcome.deliveries.get(PRIMARY_CONN, 0),
        sum(n for lbl, n in outcome.deliveries.items() if lbl != PRIMARY_CONN),
        outcome.disagreements,
        ORDER_LIVES_METHOD,
    )


def refresh_order_lives(conn: sqlite3.Connection, hashes: list[str] | None = None,
                        now_ts: float | None = None,
                        stats: dict[str, Any] | None = None) -> int:
    """(Re)compute order_lives for `hashes`, or for every order when None.

    Called at the end of every sync(), so the standing book is never more than
    one fold behind the events table. docs/07 §1: the normalizer is the one
    writer to this store; readers attach read-only and see whatever the last
    fold produced.

    `stats`, when given, has this pass's `duplicates_merged` and
    `multiplicity_disagreements` ADDED to it. They are accumulated during the
    fold rather than queried back out of the store afterwards: the per-pass
    number is the one that answers "did this fold throw anything away", and a
    store-wide re-query would answer a different question at a much higher cost.

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
            out += _refresh_chunk(conn, sql + f" AND order_hash IN ({','.join('?' * len(chunk))})",
                                  chunk, now_ts, stats)
        return out
    return _refresh_chunk(conn, sql, args, now_ts, stats)


def _refresh_chunk(conn: sqlite3.Connection, sql: str, args: list[Any], now_ts: float,
                   stats: dict[str, Any] | None = None) -> int:
    cur = conn.execute(sql + " ORDER BY order_hash", args)
    grouped: dict[str, list[tuple]] = {}
    for row in cur:
        grouped.setdefault(row[0], []).append(row)
    lives = [_fold_one_life(h, rows, now_ts) for h, rows in grouped.items()]
    if stats is not None:
        # Positions of `duplicates_merged` and `multiplicity_disagreements` in
        # the life tuple, taken from _LIFE_COLS so a column reorder cannot make
        # this quietly report the wrong field.
        di = _LIFE_COLS.index("duplicates_merged")
        mi = _LIFE_COLS.index("multiplicity_disagreements")
        stats["duplicates_merged"] = stats.get("duplicates_merged", 0) + sum(r[di] for r in lives)
        stats["multiplicity_disagreements"] = (stats.get("multiplicity_disagreements", 0)
                                               + sum(r[mi] for r in lives))
    if lives:
        with conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO order_lives ({','.join(_LIFE_COLS)}) "
                f"VALUES ({','.join('?' * len(_LIFE_COLS))})", lives)
    return len(lives)


def lives_method_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """Which fold rules produced the rows currently in `order_lives`.

    `{rows, min, max, null_rows, mixed, current}`. `mixed` is True when ANY row
    was produced by a rule older than the one this code implements -- including a
    row with a NULL `method_version`, which is a row from a schema so old it had
    no such column.

    This exists because `ORDER_LIVES_METHOD` was WRITTEN and never READ
    (BUG-20260911-076). The stamp was there to say which rules made a row, and
    nothing ever asked, so an upgraded store carried rows from two different folds
    with nothing anywhere -- no log line, no health field, no count -- saying so.
    """
    try:
        rows, lo, hi, nulls = conn.execute(
            "SELECT COUNT(*), MIN(method_version), MAX(method_version),"
            "       SUM(method_version IS NULL) FROM order_lives").fetchone()
    except sqlite3.OperationalError:
        return {"rows": 0, "min": None, "max": None, "null_rows": 0,
                "mixed": False, "current": ORDER_LIVES_METHOD}
    nulls = nulls or 0
    mixed = bool(rows) and (nulls > 0 or (lo is not None and lo < ORDER_LIVES_METHOD))
    return {"rows": rows or 0, "min": lo, "max": hi, "null_rows": nulls,
            "mixed": mixed, "current": ORDER_LIVES_METHOD}


#: How long a full `order_lives` re-fold may take before it is worth NOT doing at
#: open time. Measured on a 200,000-life synthetic fixture in
#: `test_order_lives_refold_cost_at_200k_lives`; the constant is here so the
#: measurement and the decision cannot drift apart.
REFOLD_BLOCKING_BUDGET_SECONDS = 30.0


def refold_lives_if_stale(conn: sqlite3.Connection, *, log_prefix: str = "") -> dict[str, Any]:
    """Re-fold EVERY order_lives row when any of them predates the current rules.

    `order_lives` is DERIVED -- every row in it is a fold of `events`, which is a
    fold of the landing zone (docs/07 §1). Rebuilding it is a recomputation, not
    an edit of the record, and nothing here reads or writes the landing zone.

    WHY A FULL RE-FOLD AND NOT AN INCREMENTAL ONE. `sync()` refreshes only the
    orders a pass TOUCHED. An order that received its last event yesterday is
    never touched again, so on an upgraded store it keeps whatever the OLD rules
    computed -- forever, with the flag off, with no signal. Under method 2 that
    means a doubled `terminations_seen` on every quiet order, which is exactly
    the number PR-10 was blocked for. The re-fold is keyed on the stamp rather
    than on "is the table empty", because an empty table is the one case that was
    already handled and the populated-but-stale case is the one that bites.

    Returns `{refolded, rows, seconds, before, after}`. `refolded` False means the
    table was already current and nothing was written.
    """
    import time as _time
    before = lives_method_state(conn)
    if not before["mixed"]:
        return {"refolded": False, "rows": before["rows"], "seconds": 0.0,
                "before": before, "after": before}
    t0 = _time.monotonic()
    log.warning("%sorder_lives holds rows folded by method_version %s-%s and this build "
                "writes %s: re-folding every row from `events`. The analytical store is "
                "DERIVED; the landing zone is not read and not touched.",
                log_prefix, before["min"] if before["null_rows"] == 0 else "NULL",
                before["max"], ORDER_LIVES_METHOD)
    try:
        n = refresh_order_lives(conn)
    except sqlite3.OperationalError as exc:
        # A store so old that `events` lacks a column the fold selects. Do NOT take
        # the open down for it: the dashboard coming up at all is worth more than a
        # repaired derivation, and `lives_method.mixed` stays true so Health says
        # `warn` and names rebuild-store.command. Raising here would turn a stale
        # derivation into an unopenable store.
        log.error("%sorder_lives re-fold could not run against this store (%s: %s). The rows "
                  "stay at their old method_version and Health will say so; "
                  "rebuild-store.command re-folds the whole store from the landing zone, "
                  "which is untouched.", log_prefix, type(exc).__name__, exc)
        return {"refolded": False, "rows": before["rows"],
                "seconds": _time.monotonic() - t0, "before": before, "after": before,
                "error": f"{type(exc).__name__}: {exc}"}
    took = _time.monotonic() - t0
    after = lives_method_state(conn)
    log.warning("%sorder_lives re-folded: %d orders in %.1fs, now all at method_version %s",
                log_prefix, n, took, after["max"])
    if took > REFOLD_BLOCKING_BUDGET_SECONDS:
        log.warning("%s...which took longer than the %.0fs budget. If this becomes routine the "
                    "re-fold belongs in the sync loop after the dashboard has bound its port, "
                    "not in the open path.", log_prefix, REFOLD_BLOCKING_BUDGET_SECONDS)
    return {"refolded": True, "rows": n, "seconds": took, "before": before, "after": after}


class Normalizer:
    """Incremental landing-zone -> store sync. Safe to call every few seconds.

    ONE FOLDING WRITER PER STORE (docs/07 §1). `writer=True` -- the default --
    takes an exclusive `flock` on `<store>.lock` for the life of the object and
    refuses to construct if another process already holds it. `writer=False`
    opens the same file read-only (`mode=ro` URI), takes no lock, runs no
    migration, and refuses to sync.

    BUG-20260910-067. Before this, nothing stopped two processes folding into one
    SQLite store. When a second dashboard was already listening on 8765, every
    launchd retry of the first opened the store, folded new frames, and then died
    on "Address already in use" -- 177 times, ten seconds apart -- and
    `data/analytics.sqlite` (2.8 GB) ended as "database disk image is malformed".
    The store is derived and was rebuilt from the landing zone, which is the only
    reason that was survivable.

    A job that writes `tokens`/`traits` but folds no events (the traits importer)
    is NOT a second folding writer and deliberately does not take this lock: it
    opens with a busy timeout and waits out a fold's batch instead (traits.open_store).
    """

    def __init__(self, landing_root: str | Path, db_path: str | Path, *,
                 writer: bool = True,
                 extra_roots: Any = (), label: str = "a") -> None:
        """`extra_roots` is [(label, path), ...] -- the redundant stream's landing
        roots (PR-10). Empty by default, which is the flag-off case and is
        byte-for-byte the behaviour this class has always had: ONE root, watermarks
        keyed by bare filename, `conn` set to 'a'.

        Each redundant root is a SEPARATE append-only store with its own manifest
        and its own `.ingest.lock` -- one writer per store, two stores (dataeng
        §3.a). The normalizer is a reader of both and stays the single folding
        writer of the analytical store, so docs/07 §1's rule is untouched.
        """
        self.root = Path(landing_root)
        self.label = label
        self.extra_roots = [(str(lbl), Path(p)) for lbl, p in (extra_roots or ())]
        self.db_path = Path(db_path)
        self.writer = bool(writer)
        self.lock_path = writer_lock_path(self.db_path)
        self._lock_fh: Any = None
        self._locked_at: str | None = None
        self._lock_enforced = False
        #: What the open found and did about `order_lives`' method_version.
        self.refold_stats: dict[str, Any] = {"refolded": False, "rows": 0, "seconds": 0.0,
                                             "before": None, "after": None}
        if not self.writer:
            self._open_readonly()
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._take_writer_lock()
        try:
            self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                                        timeout=BUSY_TIMEOUT_MS / 1000)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            _migrate(self.conn)
            self.conn.executescript(SCHEMA)
            ensure_ledger_indexes(self.conn)
            # A store folded by an earlier version has events but no lives. Build
            # them once, here, rather than letting every reader see an empty book.
            if (self.conn.execute("SELECT COUNT(*) FROM order_lives").fetchone()[0] == 0
                    and self.conn.execute(
                        "SELECT EXISTS(SELECT 1 FROM events WHERE order_hash IS NOT NULL)").fetchone()[0]):
                log.info("order_lives is empty on a populated store: building it once")
                log.info("order_lives built: %d orders", refresh_order_lives(self.conn))
            # ...and a store folded by an earlier version of the RULES has lives
            # that are not empty and not current, which is the case the emptiness
            # check above cannot see (BUG-20260911-076). Only the folding writer
            # may do this; a reader reports it on Health instead.
            self.refold_stats = refold_lives_if_stale(self.conn)
        except BaseException:
            # Never hold the lock for a writer that did not come up: the next
            # start would then refuse against a process that owns nothing.
            self._release_writer_lock()
            raise

    # -- the writer lock -----------------------------------------------------
    def _open_readonly(self) -> None:
        """A reader. `mode=ro` is enforced by SQLite, not by our good intentions."""
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"cannot open {self.db_path} read-only: it does not exist. A reader never "
                f"creates the store -- run the folding writer (the dashboard, or "
                f"`navanax normalize`) first.")
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False,
                                    timeout=BUSY_TIMEOUT_MS / 1000)
        self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # A reader NEVER re-folds: that would be a second writer on one store,
        # which is BUG-20260910-067. It records what it found so Health can say
        # the store needs the writer's attention.
        self.refold_stats = {"refolded": False, "rows": None, "seconds": 0.0,
                             "before": lives_method_state(self.conn),
                             "after": lives_method_state(self.conn),
                             "reader": True}

    def _take_writer_lock(self) -> None:
        """Exclusive flock on `<store>.lock`, with `cli._single_instance`'s errno rules."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.lock_path.open("a+")
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_enforced = True
        except ImportError:
            print(f"WARNING: this platform has no flock; a second process folding into "
                  f"{self.db_path} cannot be detected.", file=sys.stderr)
        except OSError as exc:
            if exc.errno in LOCK_UNSUPPORTED_ERRNOS:
                # The filesystem, not another process. Failing closed here would
                # stop the dashboard folding at all on a network share.
                print(f"WARNING: {self.lock_path.parent} does not support file locking "
                      f"({errno.errorcode.get(exc.errno, exc.errno)}). This is normal on a "
                      f"network share or an external volume.\n"
                      f"         Folding will proceed, but a SECOND folding writer against "
                      f"{self.db_path.name} cannot be detected, and two writers on one SQLite "
                      f"store can corrupt it (BUG-20260910-067).\n"
                      f"         Make sure only one dashboard is running.", file=sys.stderr)
            else:
                fh.seek(0)
                holder = fh.read().strip()
                fh.close()
                pid, since = _parse_lock_text(holder)
                who = f"pid={pid}" if pid is not None else "an unknown process"
                if since:
                    who += f", since {since}"
                stop = (f" -- `kill {pid}`, or quit the dashboard that owns it"
                        if pid is not None else "")
                raise StoreWriterBusyError(
                    f"another navanax process is already folding into "
                    f"{self.db_path} ({who}).\n"
                    "  Two writers on one SQLite store, with a process killed mid-write, is "
                    "how this store became 'database disk image is malformed' on 2026-09-10 "
                    "(BUG-20260910-067).\n"
                    f"  Fix: stop the other one{stop};\n"
                    "       or open this store read-only instead: Normalizer(..., writer=False).\n"
                    f"  Lock file: {self.lock_path}",
                    pid=pid, lock_path=self.lock_path) from None
        self._locked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} started={self._locked_at}\n")
        fh.flush()
        self._lock_fh = fh

    def _release_writer_lock(self) -> None:
        fh, self._lock_fh = self._lock_fh, None
        if fh is None:
            return
        try:
            fh.close()          # closing the fd releases the flock
        except OSError:
            pass

    def _require_writer(self, what: str) -> None:
        if not self.writer:
            raise RuntimeError(
                f"{what}() needs the folding writer, and this Normalizer was opened with "
                f"writer=False (read-only, {self.db_path}). A reader never folds: it would "
                f"be the second writer on one store, which is BUG-20260910-067.")

    def lives_method(self) -> dict[str, Any]:
        """`order_lives`' method_version state, for Health. Cheap; no re-fold."""
        return lives_method_state(self.conn)

    def writer_info(self) -> dict[str, Any]:
        """Who holds the fold-writer lock: {pid, since, alive, ...}.

        Read from the lock FILE rather than from this object, so a reader and the
        writer answer the same question the same way, and so a stale pid (a
        writer that was killed) is visible as `alive: false` instead of absent.
        """
        # `enforced` is a BOOLEAN only for a writer. On a reader `False` would be
        # indistinguishable from a writer whose filesystem has no flock -- and those
        # are opposite facts: "this process never asked for the lock" versus "this
        # process asked and cannot be protected, so a second writer could corrupt
        # the store". Health renders this field; the two must not print alike.
        return {**store_writer_info(self.db_path, lock_fh=self._lock_fh),
                "enforced": (self._lock_enforced if self.writer else
                             "not applicable (reader: this process never takes "
                             "the fold-writer lock)")}

    # -- manifest-driven file discovery ------------------------------------
    def _files(self) -> list[dict[str, Any]]:
        """Every landing file in every root, annotated with where it came from.

        `_key` is the watermark / `events.file` identity. For the PRIMARY root it
        is the bare filename, exactly as it has always been, so no existing
        watermark is orphaned and no existing store re-folds. For a redundant
        root it is prefixed with the connection label, because two roots are two
        namespaces and a collision would make one connection's watermark silently
        skip the other's frames.
        """
        out: list[dict[str, Any]] = []
        for label, root in [(self.label, self.root), *self.extra_roots]:
            mdir = root / "_manifest"
            if not mdir.exists():
                continue
            for mp in sorted(mdir.glob("*.json")):
                try:
                    data = json.loads(mp.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                for rec in data.get("files", []):
                    fname = rec.get("filename")
                    if not fname:
                        continue
                    out.append({**rec, "_root": root, "_label": label,
                                "_key": fname if root == self.root else f"{label}::{fname}"})
        return out

    def _watermark(self, filename: str) -> tuple[int, str | None]:
        row = self.conn.execute(
            "SELECT last_seq, status FROM watermarks WHERE file=?", (filename,)).fetchone()
        return (row[0], row[1]) if row else (0, None)

    # -- the sync ------------------------------------------------------------
    def sync(self) -> dict[str, int]:
        """Read every new complete frame from every landing file. Returns counts."""
        self._require_writer("sync")
        stats = {"files_checked": 0, "files_read": 0, "files_failed": 0, "files_short": 0, "last_error": None,
                 "rows_added": 0, "unparsed": 0,
                 "criteria_rows": 0, "lives_refreshed": 0,
                 # PR-10 / E-W3. Rows folded THIS PASS that carried no
                 # `event_timestamp` and therefore cannot be proved a duplicate of
                 # anything. Counted here as well as on Health because a rate that
                 # only ever appears as a store-wide total cannot be attributed to
                 # a window, and this is the number that says how much of the
                 # record a redundant stream cannot deduplicate.
                 "undedupable_rows": 0,
                 # PR-10. What the order_lives fold collapsed this pass, and where
                 # it could not tell a duplicate from a repeat. Both are counts of
                 # information DISCARDED, which is exactly the kind of number that
                 # has to be reported rather than inferred from a total.
                 "duplicates_merged": 0,
                 "multiplicity_disagreements": 0,
                 "rows_by_conn": {}}
        touched: list[str] = []
        for rec in self._files():
            stats["files_checked"] += 1
            fname = rec["filename"]
            key = rec.get("_key", fname)
            label = rec.get("_label", self.label)
            root = rec.get("_root", self.root)
            last_seq, last_status = self._watermark(key)
            # A closed file we have fully read never changes again (append-only,
            # and the checksum audit would catch it if it did). Skip it.
            if last_status == "closed" and rec.get("status") == "closed" \
                    and last_seq >= (rec.get("last_seq") or 0):
                continue
            path = root / fname
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
                            bad.append((env.get("_run"), seq, key, "unrecognised frame"))
                        continue
                    row["file"] = key
                    row["conn"] = label
                    if row.get("dedup_key") is None:
                        stats["undedupable_rows"] += 1
                    rows.append(tuple(row.get(c) for c in COLS))
                    for c in row.get("criteria") or []:
                        crit.append((row["run"], row["seq"], c["idx"], c["kind"],
                                     c["trait_type"], c["value"], c["num_min"], c["num_max"]))
                    if row.get("order_hash"):
                        touched.append(row["order_hash"])
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the sync
                # ...but it must not vanish either. A missing codec on the reading
                # machine used to yield "115 files read, 0 rows added" with no
                # error anywhere a caller looks (BUG-058). Counted and named now.
                log.warning("could not read %s: %s", key, exc)
                stats["files_failed"] += 1
                stats["last_error"] = f"{key}: {type(exc).__name__}: {exc}"
                continue
            # A closed file the manifest says runs to last_seq, from which we
            # read fewer frames, is SHORT: truncated, corrupt, or decoded by a
            # codec that tolerates truncation into silence (BUG-058). Counted
            # and named; deep `verify` is the authority on why.
            if rec.get("status") == "closed" and rec.get("last_seq") is not None and high < rec["last_seq"]:
                stats["files_short"] = stats.get("files_short", 0) + 1
                stats["last_error"] = f"{key}: read up to seq {high} of {rec['last_seq']} the manifest records"
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
                    (key, high, len(rows), rec.get("status"),
                     datetime.now(timezone.utc).isoformat()))
            stats["rows_added"] += len(rows)
            if rows:
                stats["rows_by_conn"][label] = stats["rows_by_conn"].get(label, 0) + len(rows)
            stats["unparsed"] += len(bad)
            stats["criteria_rows"] += len(crit)
        if touched:
            # Refresh only the orders this pass touched. The whole life is
            # recomputed from every event naming that hash, so an out-of-order
            # arrival (a cancel folded before its bid) is corrected, not patched.
            stats["lives_refreshed"] = refresh_order_lives(self.conn, touched, stats=stats)
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
        self._require_writer("refold_criteria")
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
        self._require_writer("reset_for_refold")
        counts: dict[str, int] = {}
        with self.conn:
            for t in ("events", "order_criteria", "order_lives", "unparsed", "watermarks"):
                counts[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                self.conn.execute(f"DELETE FROM {t}")
        log.warning("reset_for_refold: cleared %s from the analytical store; "
                    "the landing zone was not touched", counts)
        return counts

    def close(self) -> None:
        """Close the connection AND release the writer lock, in that order.

        The lock must outlive the connection: releasing it first would let a
        second writer in while this one still has a WAL checkpoint to do.
        """
        try:
            self.conn.close()
        finally:
            self._release_writer_lock()

    def __del__(self) -> None:  # pragma: no cover - best effort on an abandoned object
        try:
            self._release_writer_lock()
        except Exception as exc:  # noqa: BLE001 - a finaliser must never raise
            log.debug("releasing the writer lock in __del__ failed: %s", exc)
