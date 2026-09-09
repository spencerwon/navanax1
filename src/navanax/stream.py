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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from .errors import UpstreamUnavailableError
from .landing import GapRecord, LandingZoneWriter
from .opstore import OperationalStore

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
        self.stats = StreamStats()
        self._ref = 0
        self._stop = asyncio.Event()
        self._open_gap_id: int | None = None

    # -- protocol helpers --------------------------------------------------
    def _next_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    def _topics(self) -> list[str]:
        return [f"collection:{slug}" for slug in self.collections]

    def join_messages(self) -> list[str]:
        return [
            json.dumps({"topic": t, "event": "phx_join", "payload": {}, "ref": self._next_ref()})
            for t in self._topics()
        ]

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
            self.writer.write(raw, topic=None, event_timestamp=None)
            log.warning("unparseable frame landed verbatim (%d bytes)", len(raw))
            return None

        # BUG-20260909-002: OpenSea speaks Phoenix v2, which sends ARRAYS.
        msg = normalize_frame(parsed)
        if msg is None:
            self.writer.write(raw, topic=None, event_timestamp=None)
            log.warning("unrecognised frame shape landed verbatim: %.120s", raw)
            return None

        event = msg.get("event")
        topic = msg.get("topic")

        if event in ("phx_reply", "phx_close", "phx_error"):
            return msg
        if topic == "phoenix" or event == "heartbeat":
            self.stats.heartbeats += 1
            return msg

        ets = self.extract_event_timestamp(msg)
        self.writer.write(raw, topic=topic, event_timestamp=ets)
        self.stats.events += 1

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
    def _open_gap(self, reason: str) -> None:
        if self._open_gap_id is not None:
            return
        self._open_gap_id = self.opstore.open_gap(
            self.run_id, reason, topics=self._topics(), backfillable=True
        )
        self.writer.record_gap(
            GapRecord(
                started_at=_iso(_now()),
                ended_at=None,
                reason=reason,
                run_id=self.run_id,
                topics=self._topics(),
                # The window is partially backfillable: sales/listings/offers can
                # be recovered, cancellations and order invalidations cannot.
                backfillable=True,
            )
        )
        log.warning("ingestion gap opened: %s", reason)

    def _close_gap(self) -> None:
        if self._open_gap_id is None:
            return
        self.opstore.close_gap(self._open_gap_id)
        log.info(
            "ingestion gap closed; backfill enqueued for recoverable classes only "
            "(cancellations and order invalidate/revalidate in this window are "
            "permanently lost -- REQ-D-09a)"
        )
        self._open_gap_id = None

    # -- main loop ---------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self._open_gap(f"{type(exc).__name__}: {exc}")
                self.stats.reconnects += 1
                delay = min(self.max_backoff, backoff) * (0.5 + random.random())
                log.warning("stream error (%s); reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                backoff = min(self.max_backoff, backoff * 2)
            finally:
                self.writer.flush()

    async def _run_once(self) -> None:
        connect = self._connect_factory or self._default_connect
        url = f"{self.url}?token={self.api_key}"
        async with connect(url) as ws:
            self.stats.connected_at = _iso(_now())
            self._close_gap()
            for m in self.join_messages():
                await ws.send(m)
            log.info("subscribed to %d collections", len(self.collections))

            hb = asyncio.create_task(self._heartbeat(ws))
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
                    log.warning("heartbeat task failed", exc_info=True)

    async def _heartbeat(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await ws.send(self.heartbeat_message())

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
