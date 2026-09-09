"""Operational store -- SQLite. Disposable bookkeeping ONLY.

docs/07_STORAGE_AND_RECORDING.md §1. This holds no market data and no primary
records. The test for what belongs here:

    if losing it means losing EVIDENCE rather than losing a BOOKMARK,
    it is not operational state.

So: ingestion checkpoints, the gap register, alert dedup state, the REST budget
ledger, key expiry tracking. All reconstructible.

NOT here: trade ideas, idea outcomes, paper positions, paper fills, the config
change log. Those are primary records that cannot be reconstructed from
anything (REQ-D-29a) and live in the analytical store with append-only
bitemporal treatment.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- Where ingestion got to. Resume point after a restart.
CREATE TABLE IF NOT EXISTS ingest_checkpoint (
    stream_key       TEXT PRIMARY KEY,   -- e.g. 'opensea:collection:argonauts'
    run_id           TEXT NOT NULL,
    last_seq         INTEGER NOT NULL,
    last_event_ts    TEXT,
    last_received_at TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Gaps are RECORDED, never filled. backfillable=0 marks the event classes
-- REQ-D-09a says the events endpoint cannot recover.
CREATE TABLE IF NOT EXISTS gap_register (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    reason        TEXT NOT NULL,
    topics        TEXT NOT NULL DEFAULT '[]',
    backfillable  INTEGER NOT NULL DEFAULT 1,   -- "FULLY repairable" (BUG-013)
    backfillable_classes   TEXT NOT NULL DEFAULT '[]',   -- BUG-031
    irrecoverable_classes  TEXT NOT NULL DEFAULT '[]',
    backfilled_at TEXT,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_gap_open ON gap_register(ended_at) WHERE ended_at IS NULL;

-- Every REST call, for budget accounting and post-hoc "where did the budget go".
CREATE TABLE IF NOT EXISTS rest_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    priority     TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    status       INTEGER,
    cost         REAL NOT NULL DEFAULT 1.0,
    remaining    INTEGER,
    run_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_at ON rest_ledger(at);

-- Alert dedup / rate limiting. Rebuildable; losing it means some duplicate
-- alerts, not lost evidence.
CREATE TABLE IF NOT EXISTS alert_state (
    dedup_key   TEXT PRIMARY KEY,
    first_sent  TEXT NOT NULL,
    last_sent   TEXT NOT NULL,
    send_count  INTEGER NOT NULL DEFAULT 1,
    tier        TEXT NOT NULL
);

-- Free instant OpenSea keys expire after 7 days (REQ-D-06). A silently expired
-- key is indistinguishable from an API outage.
CREATE TABLE IF NOT EXISTS api_key_status (
    key_fingerprint TEXT PRIMARY KEY,
    label           TEXT,
    first_seen      TEXT NOT NULL,
    last_ok         TEXT,
    expires_at      TEXT,
    notes           TEXT
);

-- Collection onboarding progress (REQ-D-30 / REQ-F-07a). A collection
-- mid-backfill must be distinguishable from a broken one.
CREATE TABLE IF NOT EXISTS onboarding (
    collection_slug  TEXT PRIMARY KEY,
    state            TEXT NOT NULL,      -- queued|tokens|traits|listings|events|complete|failed
    started_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    requests_spent   INTEGER NOT NULL DEFAULT 0,
    requests_est     INTEGER,
    items_total      INTEGER,
    items_done       INTEGER NOT NULL DEFAULT 0,
    last_cursor      TEXT,
    error            TEXT
);
"""

_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OperationalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as c:
            c.executescript(_SCHEMA)
            # BUG-031: a store created before the class-list columns existed.
            have = {r[1] for r in c.execute("PRAGMA table_info(gap_register)")}
            for col in ("backfillable_classes", "irrecoverable_classes"):
                if col not in have:
                    c.execute(f"ALTER TABLE gap_register ADD COLUMN {col} TEXT NOT NULL DEFAULT '[]'")
            row = c.execute("SELECT MAX(version) FROM schema_version").fetchone()
            if row is None or row[0] is None:
                c.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?,?)",
                    (_VERSION, _now()),
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # -- checkpoints -------------------------------------------------------
    def save_checkpoint(
        self, stream_key: str, run_id: str, last_seq: int, last_event_ts: str | None
    ) -> None:
        with self.connect() as c:
            c.execute(
                """INSERT INTO ingest_checkpoint
                   (stream_key, run_id, last_seq, last_event_ts, last_received_at, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(stream_key) DO UPDATE SET
                     run_id=excluded.run_id, last_seq=excluded.last_seq,
                     last_event_ts=excluded.last_event_ts,
                     last_received_at=excluded.last_received_at,
                     updated_at=excluded.updated_at""",
                (stream_key, run_id, last_seq, last_event_ts, _now(), _now()),
            )

    def get_checkpoint(self, stream_key: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM ingest_checkpoint WHERE stream_key=?", (stream_key,)
            ).fetchone()
            return dict(row) if row else None

    # -- gaps --------------------------------------------------------------
    def open_gap(
        self,
        run_id: str,
        reason: str,
        topics: list[str] | None = None,
        backfillable: bool = True,
        started_at: str | None = None,
        backfillable_classes: list[str] | None = None,
        irrecoverable_classes: list[str] | None = None,
    ) -> int:
        """Open a gap. `started_at` defaults to now.

        It is passed explicitly for a downtime gap, whose start is the previous
        run's last checkpoint rather than the moment we noticed
        (BUG-20260909-009). A gap that claims to start when the new process
        booted would understate the hole by exactly its duration.
        """
        with self.connect() as c:
            cur = c.execute(
                """INSERT INTO gap_register(run_id, started_at, reason, topics, backfillable,
                                            backfillable_classes, irrecoverable_classes)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_id, started_at or _now(), reason,
                 json.dumps(topics or []), 1 if backfillable else 0,
                 json.dumps(backfillable_classes or []),
                 json.dumps(irrecoverable_classes or [])),
            )
            return int(cur.lastrowid)

    def close_gap(self, gap_id: int) -> None:
        with self.connect() as c:
            c.execute("UPDATE gap_register SET ended_at=? WHERE id=? AND ended_at IS NULL",
                      (_now(), gap_id))

    def open_gaps(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM gap_register WHERE ended_at IS NULL ORDER BY started_at")]

    def unbackfilled_gaps(self) -> list[dict[str, Any]]:
        """Closed gaps that still have recoverable work in them.

        BUG-20260909-031. BUG-013 redefined `backfillable` from "any of this is
        recoverable" to "ALL of it is" -- which under the default ALL_EVENTS
        subscription is never true, because it always includes cancellations.
        This query still filtered `backfillable=1`, so `navanax status` read
        "awaiting backfill 0" permanently while six re-fetchable event classes
        sat in every reconnect gap. No data was lost -- the manifest carried
        the class lists -- but the operator's only view of pending work went
        vacuous, and a future backfiller reading this would find nothing to do.

        The worklist is now "closed, not yet backfilled, and has at least one
        recoverable class" -- which is the question a backfiller actually asks.
        """
        with self.connect() as c:
            rows = [dict(r) for r in c.execute(
                """SELECT * FROM gap_register
                   WHERE ended_at IS NOT NULL AND backfilled_at IS NULL
                   ORDER BY started_at""")]
        out = []
        for r in rows:
            classes = json.loads(r.get("backfillable_classes") or "[]")
            if r.get("backfillable") == 1 or classes:
                out.append(r)
        return out

    # -- REST ledger -------------------------------------------------------
    def log_rest(
        self,
        priority: str,
        endpoint: str,
        status: int | None,
        cost: float = 1.0,
        remaining: int | None = None,
        run_id: str | None = None,
    ) -> None:
        with self.connect() as c:
            c.execute(
                """INSERT INTO rest_ledger(at, priority, endpoint, status, cost, remaining, run_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (_now(), priority, endpoint, status, cost, remaining, run_id),
            )

    def rest_spend_since(self, iso_ts: str) -> dict[str, float]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT priority, SUM(cost) AS spend FROM rest_ledger "
                "WHERE at >= ? GROUP BY priority",
                (iso_ts,),
            ).fetchall()
            return {r["priority"]: float(r["spend"]) for r in rows}

    # -- onboarding --------------------------------------------------------
    def upsert_onboarding(self, slug: str, **fields: Any) -> None:
        """Insert or partially update an onboarding row.

        `state` defaults to 'queued' ONLY on insert. Defaulting it on the update
        path would silently reset a collection mid-backfill back to 'queued' any
        time a caller updated progress without restating the state -- a bug that
        breaks nothing visibly and quietly corrupts the one signal that
        distinguishes "backfilling" from "broken" (REQ-F-07a).
        """
        with self.connect() as c:
            existing = c.execute(
                "SELECT collection_slug FROM onboarding WHERE collection_slug=?", (slug,)
            ).fetchone()
            if existing is None:
                fields.setdefault("state", "queued")
                c.execute(
                    """INSERT INTO onboarding(collection_slug, state, started_at, updated_at,
                       requests_spent, requests_est, items_total, items_done, last_cursor, error)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        slug, fields.get("state"), _now(), _now(),
                        fields.get("requests_spent", 0), fields.get("requests_est"),
                        fields.get("items_total"),
                        fields.get("items_done", 0),
                        fields.get("last_cursor"),
                        fields.get("error"),
                    ),
                )
            else:
                cols = [k for k in fields if k != "collection_slug"]
                if cols:
                    sets = ", ".join(f"{k}=?" for k in cols) + ", updated_at=?"
                    c.execute(
                        f"UPDATE onboarding SET {sets} WHERE collection_slug=?",
                        [fields[k] for k in cols] + [_now(), slug],
                    )

    def onboarding_status(self) -> list[dict[str, Any]]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM onboarding ORDER BY collection_slug")]
