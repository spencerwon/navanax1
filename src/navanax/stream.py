"""OpenSea Stream API consumer.

The stream is the primary ingestion path: unmetered, and it does not count
against the REST budget (measured 120 reads/hour). This process is what starts the
historical record accumulating, and every day it is not running is a day of
history that cannot be bought back.

Protocol: the Stream API speaks Phoenix Channels over a WebSocket at
`wss://stream.openseabeta.com/socket/websocket?token=<API_KEY>`. Messages are
JSON of the shape:

    {"topic": "collection:argonauts", "event": "phx_join", "payload": {}, "ref": "1"}

and a heartbeat must be sent on the "phoenix" topic every ~30s or the server
closes the connection.

Implemented against the raw protocol rather than a Phoenix client library
because the protocol is small, and a thin unmaintained dependency in the most
critical component of the system is a poor trade.

THREE PROPERTIES THIS MODULE MUST GET RIGHT
-------------------------------------------
1. Land before parsing. Every frame goes to the landing zone verbatim first
   (REQ-D-26). Parsing happens afterwards and cannot lose data.
2. Order by `event_timestamp`, never arrival (REQ-D-07). The stream is
   explicitly best-effort and delivers out of order.
3. Gaps are recorded, never filled (REQ-D-14). On disconnect we open a gap in
   the register, and on reconnect we close it and enqueue backfill for the
   event classes REST can actually recover (REQ-D-09a).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import (
    LandingZoneWriteError,
    SubscriptionRejectedError,
    UpstreamUnavailableError,
)
from .landing import GapRecord, LandingZoneWriter
from .opstore import OperationalStore

#: One checkpoint row covers the whole consumer. It answers exactly one
#: question -- "when was this process last known to be alive?" -- which is what
#: turns the interval between two runs into a recorded gap (BUG-20260909-009).
STREAM_KEY = "opensea:stream"

log = logging.getLogger("navanax.stream")

MAINNET_WS = "wss://stream.openseabeta.com/socket/websocket"
TESTNET_WS = "wss://testnets-stream.openseabeta.com/socket/websocket"

# Event classes the events REST endpoint can backfill after a disconnect.
BACKFILLABLE = frozenset(
    {"item_sold", "item_transferred", "item_listed", "item_received_bid",
     "collection_offer", "trait_offer", "item_metadata_update_ignored"}
)
# REQ-D-09a: permanently unrecoverable after a disconnect. Note
# `item_received_bid` is NOT here -- it is an item-level offer, recoverable via
# the events endpoint's `offer` type.
IRRECOVERABLE = frozenset(
    {"item_cancelled", "order_invalidate", "order_revalidate", "item_metadata_updated"}
)

ALL_EVENTS = (
    "item_listed", "item_sold", "item_transferred", "item_cancelled",
    "item_received_bid", "item_received_offer", "item_metadata_updated",
    "collection_offer", "trait_offer", "order_invalidate", "order_revalidate",
)


def normalize_frame(parsed: Any) -> dict[str, Any] | None:
    """Return a uniform dict from either Phoenix wire format.

    BUG-20260909-002. Phoenix ships two serializers and OpenSea uses v2:

      v1 (map)   {"topic":..., "event":..., "payload":..., "ref":...}
      v2 (array) [join_ref, ref, topic, event, payload]

    v2 exists because arrays are smaller on the wire. Our code assumed v1 and
    called .get() on what turned out to be a list, so the consumer died on the
    very first frame the server sent -- the join reply.

    Returns None for anything that is neither shape. The caller still lands the
    raw bytes; an unrecognised frame is a parsing question, never a reason to
    drop data we may not be able to fetch again.
    """
    if isinstance(parsed, dict):
        return {
            "join_ref": parsed.get("join_ref"),
            "ref": parsed.get("ref"),
            "topic": parsed.get("topic"),
            "event": parsed.get("event"),
            "payload": parsed.get("payload"),
        }
    if isinstance(parsed, list) and len(parsed) >= 5:
        join_ref, ref, topic, event, payload = parsed[0], parsed[1], parsed[2], parsed[3], parsed[4]
        return {
            "join_ref": join_ref,
            "ref": ref,
            "topic": topic if isinstance(topic, str) else None,
            "event": event if isinstance(event, str) else None,
            "payload": payload,
        }
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class StreamStats:
    connected_at: str | None = None
    frames: int = 0
    events: int = 0
    heartbeats: int = 0
    reconnects: int = 0
    out_of_order: int = 0
    unknown_events: dict[str, int] = field(default_factory=dict)
    last_event_ts: str | None = None
    max_event_ts: str | None = None
    # BUG-20260909-017. REQ-D-07 orders on event_timestamp and never on
    # arrival, so an event landed without one cannot be ordered and drops out
    # of every manifest-driven range query. It used to land silently, with
    # nothing counting it -- so we could not say how often it happened, which
    # means we could not say whether it mattered.
    missing_event_ts: int = 0
    missing_event_ts_by_type: dict[str, int] = field(default_factory=dict)
    joined: list[str] = field(default_factory=list)
    join_rejected: dict[str, str] = field(default_factory=dict)
    gaps_opened: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected_at": self.connected_at,
            "frames": self.frames,
            "events": self.events,
            "heartbeats": self.heartbeats,
            "reconnects": self.reconnects,
            "out_of_order": self.out_of_order,
            "unknown_events": dict(self.unknown_events),
            "last_event_ts": self.last_event_ts,
            "max_event_ts": self.max_event_ts,
            "missing_event_ts": self.missing_event_ts,
            "missing_event_ts_by_type": dict(self.missing_event_ts_by_type),
            "missing_event_ts_pct": (
                round(100.0 * self.missing_event_ts / self.events, 2)
                if self.events else 0.0
            ),
            "joined": list(self.joined),
            "join_rejected": dict(self.join_rejected),
            "gaps_opened": self.gaps_opened,
        }


class StreamConsumer:
    """Subscribes to watchlist collections and lands every frame.

    REQ-D-29: subscribe per collection, never the wildcard firehose. The
    firehose is ~373 GB/year and carries events about collections we are not
    analysing; a 200-collection watchlist is ~15 GB/year.
    """

    def __init__(
        self,
        api_key: str,
        collections: Sequence[str],
        writer: LandingZoneWriter,
        opstore: OperationalStore,
        *,
        url: str = MAINNET_WS,
        events: Iterable[str] = ALL_EVENTS,
        heartbeat_seconds: float = 30.0,
        max_backoff: float = 60.0,
        run_id: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        connect_factory: Callable[[str], Any] | None = None,
        join_timeout: float = 15.0,
        stable_seconds: float = 60.0,
        checkpoint_every: int = 50,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError(
                "OPENSEA_API_KEY is required. The stream needs a key even though "
                "it is unmetered. Get one at https://docs.opensea.io/reference/api-keys "
                "and put it in .env -- never in the repo."
            )
        self.api_key = api_key
        self.collections = list(collections)
        self.writer = writer
        self.opstore = opstore
        self.url = url
        self.events = list(events)
        self.heartbeat_seconds = heartbeat_seconds
        self.max_backoff = max_backoff
        self.run_id = run_id or writer.run_id
        self.on_event = on_event
        self._connect_factory = connect_factory  # injectable for tests
        self.join_timeout = join_timeout
        self.stable_seconds = stable_seconds
        self.checkpoint_every = checkpoint_every
        self._monotonic = monotonic
        self.stats = StreamStats()
        self._ref = 0
        self._stop = asyncio.Event()
        self._open_gap_id: int | None = None
        self._open_gap_record: GapRecord | None = None
        # BUG-20260909-005: join replies are now tracked to a conclusion.
        self._pending_joins: dict[str, str] = {}   # ref -> topic
        self._joined: set[str] = set()
        # V6/V11 (validator, second review). Joins are re-issued on EVERY
        # reconnect, and a rejection gap was opened each time and never closed.
        # Measured: 200 collections x 20 reconnects = 4,000 permanently-open
        # gaps and 89.6s of synchronous manifest fsync inside the event loop,
        # which blocked the reader long enough to miss the heartbeat and force
        # another reconnect. Self-reinforcing, and triggered by the certainty
        # of a 7-day key expiring. One open gap per topic, retracted the moment
        # that topic joins successfully.
        self._rejection_gaps: dict[str, int] = {}   # topic -> gap id
        self._rejection_records: dict[str, GapRecord] = {}   # topic -> manifest record
        self._since_checkpoint = 0

    # -- protocol helpers --------------------------------------------------
    def _next_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    def _topics(self) -> list[str]:
        return [f"collection:{slug}" for slug in self.collections]

    def join_messages(self) -> list[str]:
        """Build the join frames AND register each ref as outstanding.

        The ref is how a reply is matched back to the topic it answers. Without
        that mapping a `phx_reply` is an anonymous frame and a rejection is
        indistinguishable from an acknowledgement (BUG-20260909-005).
        """
        self._pending_joins.clear()
        self._joined.clear()
        # NOTE: _rejection_gaps deliberately survives a reconnect. It is what
        # stops the same rejection opening a new gap on every retry (V6).
        out: list[str] = []
        for t in self._topics():
            ref = self._next_ref()
            self._pending_joins[ref] = t
            out.append(
                json.dumps({"topic": t, "event": "phx_join", "payload": {}, "ref": ref})
            )
        return out

    # -- join outcomes -----------------------------------------------------
    def _handle_join_reply(self, msg: dict[str, Any]) -> None:
        # V9 (validator, second review). The server answers a heartbeat with a
        # `phx_reply` on topic "phoenix" -- NOT with an `{"event":"heartbeat"}`
        # frame, which is what we send and what the old test fixture asserted.
        # So `stats.heartbeats` was 0 in production while the test said 1: the
        # same failure as BUG-002, where the fixture inherited its assumption
        # from the code it was testing.
        if msg.get("topic") == "phoenix":
            self.stats.heartbeats += 1
            return
        ref = msg.get("ref")
        ref = str(ref) if ref is not None else None
        topic = self._pending_joins.pop(ref, None) if ref else None
        if topic is None:
            topic = msg.get("topic") if isinstance(msg.get("topic"), str) else None
            if topic in self._pending_joins.values():
                self._pending_joins = {
                    r: t for r, t in self._pending_joins.items() if t != topic
                }
            elif topic is None or not topic.startswith("collection:"):
                return  # a reply to a heartbeat or some other ref -- not ours

        payload = msg.get("payload")
        status = payload.get("status") if isinstance(payload, dict) else None
        if status == "ok":
            self._joined.add(topic)
            if topic not in self.stats.joined:
                self.stats.joined.append(topic)
            # A late reply, or a reconnect that succeeded, retracts the gap the
            # timeout opened. Leaving it open would assert a hole that does not
            # exist -- as damaging in its own way as missing one that does.
            gid = self._rejection_gaps.pop(topic, None)
            if gid is not None:
                self.opstore.close_gap(gid)
                rec = self._rejection_records.pop(topic, None)
                if rec is not None:
                    # BUG-023: write the END into the durable record too, not
                    # just into the disposable one.
                    self.writer.close_gap_record(rec, _iso(_now()))
                self.stats.join_rejected.pop(topic, None)
                log.info("subscription for %s recovered; gap %d closed", topic, gid)
            log.info("subscribed: %s", topic)
            return

        reason = json.dumps(payload)[:300] if payload is not None else "no payload"
        self._reject_join(topic, f"status={status} {reason}")

    def _reject_join(self, topic: str, reason: str) -> None:
        """A topic we are NOT subscribed to. Loud, recorded, and gap-registered.

        The failure this prevents is the quiet one: an expired key or a bad slug
        leaves a healthy-looking process recording nothing, and the first sign
        is an empty chart weeks later.
        """
        if topic in self._rejection_gaps:
            # Already open and unresolved. Re-opening it every reconnect is what
            # produced the gap storm (V6).
            self.stats.join_rejected[topic] = reason
            return
        self.stats.join_rejected[topic] = reason
        err = SubscriptionRejectedError(
            f"phx_join refused for {topic}; this process is recording NOTHING for it",
            expected="status=ok",
            received=reason,
            ingestion_run_id=self.run_id,
            topic=topic,
        )
        log.error("%s", err)
        gid = self.opstore.open_gap(
            self.run_id,
            f"subscription rejected: {topic} ({reason})",
            topics=[topic],
            # Whatever happens in this window is not backfillable in general:
            # cancellations and order invalidations in it are gone (REQ-D-09a),
            # and we do not know how long it will last.
            backfillable=False,
        )
        self._rejection_gaps[topic] = gid
        self._rejection_records[topic] = GapRecord(
            started_at=_iso(_now()), ended_at=None,
            reason=f"subscription rejected: {reason}",
            run_id=self.run_id, topics=[topic], backfillable=False,
            gap_id=gid,
        )
        self.stats.gaps_opened += 1
        self.writer.record_gap(self._rejection_records[topic])
        log.error(
            "gap %d opened for %s. Check: (1) the API key has not expired -- free "
            "instant keys last 7 days (REQ-D-06); (2) the collection slug is the "
            "last path segment of its OpenSea URL.", gid, topic,
        )

    def _check_join_timeouts(self) -> None:
        """A join that is never answered is a rejection that forgot to reply."""
        for ref, topic in list(self._pending_joins.items()):
            self._pending_joins.pop(ref, None)
            self._reject_join(topic, f"no phx_reply within {self.join_timeout:.0f}s")

    def _checkpoint(self) -> None:
        """Record that this process was alive, and how far it got.

        BUG-20260909-009: `save_checkpoint` existed and was called by nothing
        except the test suite, so after a restart there was no "last alive"
        timestamp and the downtime between two runs was never recorded as a gap.
        """
        self.opstore.save_checkpoint(
            STREAM_KEY, self.run_id, self.writer.sequence, self.stats.max_event_ts
        )
        self._since_checkpoint = 0

    def heartbeat_message(self) -> str:
        return json.dumps(
            {"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": self._next_ref()}
        )

    # -- frame handling ----------------------------------------------------
    @staticmethod
    def extract_event_timestamp(msg: dict[str, Any]) -> str | None:
        """Dig out `event_timestamp`, which sits at varying depths by event type.

        REQ-D-07 orders on this field, never on arrival time, so failing to find
        it matters: an event landed without one cannot be ordered correctly.
        """
        p = msg.get("payload")
        if not isinstance(p, dict):
            return None
        for candidate in (p, p.get("payload")):
            if isinstance(candidate, dict):
                ts = candidate.get("event_timestamp")
                if isinstance(ts, str):
                    return ts
                inner = candidate.get("payload")
                if isinstance(inner, dict):
                    ts = inner.get("event_timestamp")
                    if isinstance(ts, str):
                        return ts
        return None

    def _land(self, raw: str, **kw: Any) -> None:
        """Write to the landing zone, distinguishing LOCAL failure from remote.

        BUG-20260909-018. A disk-full OSError raised here used to be caught by
        the run loop's `except Exception`, recorded as an ingestion gap, and
        retried forever -- so the gap register blamed OpenSea for a local
        failure, and the retry could not succeed because the thing that failed
        was writing. Reconnecting is the right response to a remote failure and
        exactly the wrong one to a local failure.
        """
        try:
            self.writer.write(raw, **kw)
        except OSError as exc:
            raise LandingZoneWriteError(
                f"could not write to the landing zone: {exc}",
                expected="a writable landing zone",
                received=f"{type(exc).__name__}: {exc}",
                ingestion_run_id=self.run_id,
            ) from exc

    def handle_frame(self, raw: str) -> dict[str, Any] | None:
        """Land the frame, then parse. Landing never depends on parsing.

        Returns the parsed message, or None for control frames and unparseable
        input. Unparseable frames are still landed -- if OpenSea sends something
        we do not understand, the bytes are on disk and can be reprocessed once
        we do.
        """
        self.stats.frames += 1
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Land it anyway. Losing an unparseable frame loses the evidence
            # needed to find out why it was unparseable.
            self._land(raw, topic=None, event_timestamp=None)
            log.warning("unparseable frame landed verbatim (%d bytes)", len(raw))
            return None

        # BUG-20260909-002: OpenSea speaks Phoenix v2, which sends ARRAYS.
        msg = normalize_frame(parsed)
        if msg is None:
            self._land(raw, topic=None, event_timestamp=None)
            log.warning("unrecognised frame shape landed verbatim: %.120s", raw)
            return None

        event = msg.get("event")
        topic = msg.get("topic")

        if event in ("phx_reply", "phx_close", "phx_error"):
            # Control frames are landed too. They are small and infrequent, and
            # they are the only on-disk evidence of what this process was
            # actually subscribed to at a given moment -- which is precisely
            # the question that could not be answered when BUG-20260909-005
            # made a refused join invisible.
            self._land(raw, topic="__control__", event_timestamp=None, control=True)
            if event == "phx_reply":
                self._handle_join_reply(msg)
            elif event in ("phx_close", "phx_error"):
                t = topic if isinstance(topic, str) else "?"
                if t in self._joined:
                    self._joined.discard(t)
                    self._reject_join(t, f"channel {event} after a successful join")
            return msg
        if topic == "phoenix" or event == "heartbeat":
            self.stats.heartbeats += 1
            self._land(raw, topic="__control__", event_timestamp=None, control=True)
            return msg

        ets = self.extract_event_timestamp(msg)
        self._land(raw, topic=topic, event_timestamp=ets)
        self.stats.events += 1
        if not ets:
            # Landed regardless -- the bytes are the record, and a reprocessor
            # may be able to find the timestamp we could not. But it is COUNTED,
            # because an unknown rate of unorderable events is an unknown-size
            # hole in every event-time query (BUG-20260909-017).
            self.stats.missing_event_ts += 1
            key = event if isinstance(event, str) else "?"
            self.stats.missing_event_ts_by_type[key] = (
                self.stats.missing_event_ts_by_type.get(key, 0) + 1)
            if self.stats.missing_event_ts in (1, 10, 100) or (
                    self.stats.missing_event_ts % 1000 == 0):
                log.warning(
                    "%d event(s) landed with NO parseable event_timestamp "
                    "(%.1f%% of %d). REQ-D-07 orders on that field, so these "
                    "are unorderable and drop out of manifest range queries. "
                    "By type: %s",
                    self.stats.missing_event_ts,
                    100.0 * self.stats.missing_event_ts / max(1, self.stats.events),
                    self.stats.events, dict(self.stats.missing_event_ts_by_type),
                )
        self._since_checkpoint += 1
        if self._since_checkpoint >= self.checkpoint_every:
            self._checkpoint()

        if ets:
            # Out-of-order arrival is EXPECTED, not an error -- the stream is
            # best-effort. We count it so the rate is visible, because a sudden
            # change in that rate is a signal about upstream behaviour.
            if self.stats.max_event_ts and ets < self.stats.max_event_ts:
                self.stats.out_of_order += 1
            else:
                self.stats.max_event_ts = ets
            self.stats.last_event_ts = ets

        if isinstance(event, str) and event not in ALL_EVENTS:
            self.stats.unknown_events[event] = self.stats.unknown_events.get(event, 0) + 1

        if self.on_event:
            self.on_event(msg)
        return msg

    # -- gap tracking ------------------------------------------------------
    def _gap_classes(self) -> tuple[list[str], list[str], bool]:
        """Split this subscription's event classes into recoverable and not.

        REQ-D-09a. Returns (recoverable, irrecoverable, fully_backfillable).
        `fully_backfillable` is True only when NOTHING in the subscription is
        permanently lost -- which, for the default ALL_EVENTS subscription, is
        never. Saying so on every gap is the point: an honest "no" is worth
        more than a hopeful "yes" that no backfill can ever satisfy.
        """
        subscribed = set(self.events)
        recoverable = sorted(subscribed & BACKFILLABLE)
        lost = sorted(subscribed & IRRECOVERABLE)
        return recoverable, lost, not lost

    def _open_gap(self, reason: str) -> None:
        if self._open_gap_id is not None:
            return
        recoverable, lost, full = self._gap_classes()
        self._open_gap_id = self.opstore.open_gap(
            self.run_id, reason, topics=self._topics(), backfillable=full
        )
        self._open_gap_record = GapRecord(
            started_at=_iso(_now()),
            ended_at=None,
            reason=reason,
            run_id=self.run_id,
            topics=self._topics(),
            gap_id=self._open_gap_id,
            backfillable=full,
            backfillable_classes=recoverable,
            irrecoverable_classes=lost,
        )
        self.stats.gaps_opened += 1
        self.writer.record_gap(self._open_gap_record)
        log.warning(
            "ingestion gap opened: %s -- %d event classes recoverable, "
            "%d PERMANENTLY LOST for this window (%s)",
            reason, len(recoverable), len(lost), ", ".join(lost) or "none",
        )

    def _close_gap(self) -> None:
        if self._open_gap_id is None:
            return
        self.opstore.close_gap(self._open_gap_id)
        _rec, _lost, _full = self._gap_classes()
        # BUG-20260909-023. This used to update ONLY the SQLite gap register,
        # which docs/07 §1 classifies as disposable, reconstructible
        # bookkeeping -- and a gap is reconstructible from nothing. The durable
        # manifest kept `ended_at: null` forever, so a reader could not tell a
        # three-second reconnect from a weekend outage. The end is written to
        # the manifest now, across every day the gap turned out to span, which
        # is only knowable once it has an end.
        if self._open_gap_record is not None:
            self.writer.close_gap_record(self._open_gap_record, _iso(_now()))
            self._open_gap_record = None
        # BUG-20260909-013: this used to say backfill "enqueued". Nothing was
        # enqueued -- there is no backfill queue yet -- and a log line claiming
        # a recovery that never happens is how a hole comes to look filled.
        log.info(
            "ingestion gap closed. Recoverable when a backfiller exists: %s. "
            "PERMANENTLY LOST for this window (REQ-D-09a): %s. "
            "NOTE: no backfill queue exists yet; nothing has been enqueued.",
            ", ".join(_rec) or "none", ", ".join(_lost) or "none",
        )
        self._open_gap_id = None

    # -- main loop ---------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def record_downtime_gap(self) -> int | None:
        """Record the interval since this consumer last checkpointed.

        BUG-20260909-009. A restart used to leave no trace at all: the previous
        run's last-known-alive time was never written, so the window between
        two runs -- an overnight laptop sleep, a crash, a deploy -- simply did
        not appear in the gap register. Downstream, an absence of events is
        indistinguishable from a quiet market, which is the single most
        dangerous confusion available in this system: it turns "we were not
        watching" into "nothing happened", and a backtest reads the second.

        Called once at the start of `run()`. Returns the gap id, or None if
        this is the first run this store has ever seen.
        """
        ck = self.opstore.get_checkpoint(STREAM_KEY)
        if not ck:
            log.info("no previous checkpoint: first run against this operational store")
            self._checkpoint()
            return None
        since = ck.get("updated_at") or ck.get("last_received_at")
        gid = self.opstore.open_gap(
            self.run_id,
            f"process not running (previous run {ck.get('run_id')} last CHECKPOINTED "
            f"{since}; it may have been alive up to one heartbeat interval longer, "
            f"so this gap is over-recorded rather than under-recorded)",
            topics=self._topics(),
            backfillable=self._gap_classes()[2],
            started_at=since,
        )
        self.opstore.close_gap(gid)
        self.stats.gaps_opened += 1
        self.writer.record_gap(
            GapRecord(
                started_at=since, ended_at=_iso(_now()),
                reason=f"process not running since {since} (run {ck.get('run_id')})",
                run_id=self.run_id, topics=self._topics(),
                backfillable=self._gap_classes()[2],
                backfillable_classes=self._gap_classes()[0],
                irrecoverable_classes=self._gap_classes()[1],
            )
        )
        log.warning(
            "recorded downtime gap %d: the previous run last checkpointed at %s. "
            "The gap is measured from there, so it is conservative -- it may "
            "cover up to one heartbeat interval during which events WERE "
            "recorded. Erring long is the safe direction. "
            "Sales, listings and offers in that window are backfillable; "
            "cancellations and order invalidate/revalidate are NOT (REQ-D-09a).",
            gid, since,
        )
        self._checkpoint()
        return gid

    async def run(self) -> None:
        backoff = 1.0
        self.record_downtime_gap()
        while not self._stop.is_set():
            t0 = self._monotonic()
            try:
                await self._run_once()
                if self._stop.is_set():
                    break
                # BUG-20260909-008. A clean close used to fall straight through
                # here: no gap recorded, backoff reset to 1.0, and the loop
                # reconnected with NO sleep at all. A server closing politely in
                # a loop therefore span the event loop hot while recording
                # nothing -- and because no exception was raised, it looked like
                # a series of successful runs.
                self._open_gap("connection closed by peer without a stop request")
                self.stats.reconnects += 1
                log.warning("stream closed cleanly but we did not ask it to")
            except asyncio.CancelledError:
                raise
            except LandingZoneWriteError:
                # BUG-20260909-018: local, not remote. Reconnecting cannot help
                # and the retry loop would spin forever recording gaps it also
                # cannot write. Stop, and let the operator see why.
                self.writer.flush()
                log.error(
                    "HALTING: the landing zone is not writable. This is a LOCAL "
                    "failure (disk, permissions, or an unmounted volume) -- not "
                    "an OpenSea outage, and not something reconnecting fixes."
                )
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self._open_gap(f"{type(exc).__name__}: {exc}")
                self.stats.reconnects += 1
                log.warning("stream error (%s)", exc)
            finally:
                self.writer.flush()
                self._checkpoint()

            if self._stop.is_set():
                break
            # Backoff resets only after a connection that actually STAYED UP.
            # Resetting it on every return let a flapping connection retry at
            # full speed forever.
            if (self._monotonic() - t0) >= self.stable_seconds:
                backoff = 1.0
            delay = min(self.max_backoff, backoff) * (0.5 + random.random())
            log.warning("reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)
            backoff = min(self.max_backoff, backoff * 2)

    async def _run_once(self) -> None:
        connect = self._connect_factory or self._default_connect
        url = f"{self.url}?token={self.api_key}"
        async with connect(url) as ws:
            self.stats.connected_at = _iso(_now())
            self._close_gap()
            for m in self.join_messages():
                await ws.send(m)
            log.info("sent %d join requests; awaiting replies", len(self.collections))

            hb = asyncio.create_task(self._keepalive(ws))
            try:
                async for raw in ws:
                    if self._stop.is_set():
                        break
                    self.handle_frame(raw if isinstance(raw, str) else raw.decode("utf-8"))
            finally:
                hb.cancel()
                try:
                    await hb
                except asyncio.CancelledError:
                    pass  # expected: we just cancelled it
                except Exception:  # noqa: BLE001 - logged, never swallowed
                    # A heartbeat that died for a real reason is a clue about
                    # why the connection dropped. Narrowing this would discard
                    # that clue; we log it and let the outer error stand.
                    log.warning("keepalive task failed", exc_info=True)

    async def _keepalive(self, ws: Any) -> None:
        """Heartbeat, join-reply deadline, and periodic checkpoint.

        One task rather than three: they all just need a clock, and a single
        task is one thing to cancel correctly rather than three.
        """
        deadline = self._monotonic() + self.join_timeout
        checked = False
        next_hb = self._monotonic() + self.heartbeat_seconds
        while True:
            await asyncio.sleep(min(1.0, self.heartbeat_seconds))
            now = self._monotonic()
            if not checked and now >= deadline:
                checked = True
                self._check_join_timeouts()
                if not self._joined:
                    log.error(
                        "NOT SUBSCRIBED TO ANYTHING after %.0fs. The socket is open "
                        "and the process looks healthy, but no market data is being "
                        "recorded.", self.join_timeout,
                    )
            if now >= next_hb:
                next_hb = now + self.heartbeat_seconds
                await ws.send(self.heartbeat_message())
                self._checkpoint()

    @staticmethod
    def _default_connect(url: str) -> Any:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            raise UpstreamUnavailableError(
                "the `websockets` package is required to run the stream consumer",
                expected="websockets installed",
                received="ImportError",
            ) from exc
        return websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024)


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
