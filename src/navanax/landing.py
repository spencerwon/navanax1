"""Landing zone writer -- the first and most important component in the system.

docs/07_STORAGE_AND_RECORDING.md §1.2: "Land first, normalize second."

Every stream frame is written here, verbatim, before anything parses it. If the
normalizer has a bug, the raw bytes are still on disk and can be reprocessed.
If we normalized in flight and discarded the original, a parsing bug would mean
the data is gone -- and at 120 REST reads/hour (measured) it may not be re-fetchable at any
price.

Guarantees this module provides:

  REQ-D-26   raw JSONL, envelope with received_at / run_id / sequence,
             compressed, partitioned by UTC date and hour
  REQ-D-26a  a frame is closed every `flush_seconds` or `flush_events`,
             whichever comes first, bounding crash loss to seconds
  REQ-D-28   sha256, event count and first/last event_timestamp recorded in a
             daily manifest when a file closes

Byte fidelity note: the envelope stores the raw frame text as a JSON string
rather than as a parsed object. Re-serialising a parsed object would reorder
keys and normalise numbers, which would break the "exactly what the API sent"
guarantee that the whole reprocessing path depends on. The cost is ~2% in
escaping overhead. That is the right trade.
"""

from __future__ import annotations

import hashlib
import json
import os
import re as _re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .codec import Codec, get_codec

_UTC = timezone.utc
_DATE_RE = _re.compile(r"\d{4}-\d{2}-\d{2}")


def _utcnow() -> datetime:
    return datetime.now(_UTC)


def _dates_spanned(start_iso: str, end_iso: str | None,
                   now: Callable[[], datetime] = _utcnow) -> list[str]:
    """Every UTC date from `start_iso` to `end_iso` inclusive.

    An open-ended gap (`end_iso is None`) spans to today: it is still running,
    and today's manifest is where a reader will look for it.
    """
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        # Tech-lead finding: this used to return `start_iso[:10]` verbatim, and
        # that string becomes a FILENAME. `_dates_spanned("../../../etc/passwd")`
        # produced `_manifest/../../../e.json` -- a write outside the landing
        # zone. The input is our own SQLite today, so this is shape rather than
        # exploit, but a value that becomes a path gets validated regardless.
        head = str(start_iso)[:10]
        return [head] if _DATE_RE.fullmatch(head) else [_iso(now())[:10]]
    if end_iso:
        try:
            end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        except ValueError:
            end = now()
    else:
        end = now()
    if end < start:
        end = start
    first = start.astimezone(_UTC).date()
    last = end.astimezone(_UTC).date()
    # Cap the walk: a gap longer than a year is a data problem, not a reason to
    # write 400 manifest files synchronously at startup. The cap used to keep
    # the OLDEST 366 days and drop the most recent -- so a gap starting in 2019
    # produced a list that did not include today, which is the wrong direction
    # to lose information in. Keep both ENDS and mark the elision, so a reader
    # sees a gap that begins and ends where it really does.
    span = (last - first).days + 1
    if span > 366:
        head = [(first + timedelta(days=i)).isoformat() for i in range(183)]
        tail = [(last - timedelta(days=i)).isoformat() for i in range(182, -1, -1)]
        return head + tail
    return [(first + timedelta(days=i)).isoformat() for i in range(span)]


def _iso(dt: datetime) -> str:
    return dt.astimezone(_UTC).isoformat().replace("+00:00", "Z")


@dataclass
class FileRecord:
    """One entry in the daily manifest."""

    filename: str
    dt: str
    hour: str
    run_id: str
    codec: str
    opened_at: str
    closed_at: str | None = None
    event_count: int = 0
    raw_bytes: int = 0
    stored_bytes: int = 0
    sha256: str | None = None
    first_event_timestamp: str | None = None
    last_event_timestamp: str | None = None
    first_seq: int | None = None
    last_seq: int | None = None
    frames: int = 0
    truncated: bool = False
    # BUG-20260909-007. A file used to enter the manifest only when it CLOSED,
    # so a file being written right now -- or one whose process was killed --
    # was invisible to `verify_manifest`, which then reported "clean". The
    # record is now written when the file OPENS and updated on every frame
    # flush, so the manifest always describes what is on disk.
    status: str = "open"          # open | closed
    last_flush_at: str | None = None
    # V2 (validator, second review). BUG-005's fix started landing control
    # frames -- join replies, heartbeats, channel closes -- through the same
    # writer as market events, which made `event_count` count them. At the
    # target volume that is not a rounding error: Argonauts at ~2,000
    # events/day against 2,880 heartbeat frames/day means control frames would
    # be the MAJORITY of `event_count`, and the ING monitor keyed on event rate
    # could never fire on a dead subscription -- which is BUG-005's own failure
    # mode re-entering through the door BUG-005's fix opened.
    control_count: int = 0


@dataclass
class GapRecord:
    """A recorded ingestion gap. Gaps are represented as gaps -- never filled.

    `backfillable` is False for the event classes REQ-D-09a marks permanently
    unrecoverable: cancellations, order invalidate/revalidate, metadata updates.
    """

    started_at: str
    ended_at: str | None
    reason: str
    run_id: str
    topics: list[str] = field(default_factory=list)
    # BUG-20260909-013. A single boolean cannot describe this window honestly.
    # Any gap wide enough to matter spans BOTH recoverable and unrecoverable
    # event classes: sales, listings and offers can be re-fetched from the
    # events endpoint; cancellations and order invalidate/revalidate cannot
    # (REQ-D-09a) and are gone for good. Every gap used to be written
    # `backfillable: True`, which contradicted this record's own docstring and
    # told a downstream reader that a hole it can never fill was fillable.
    #
    # `backfillable` now means "can this gap be fully repaired" -- which for a
    # subscription covering any IRRECOVERABLE class is False. The two lists say
    # exactly what is and is not recoverable, so nothing has to be inferred
    # from a flag that cannot carry the distinction.
    backfillable: bool = False
    backfillable_classes: list[str] = field(default_factory=list)
    irrecoverable_classes: list[str] = field(default_factory=list)
    backfilled_at: str | None = None
    # BUG-20260909-023. A gap opened live was written with ended_at=None and
    # NOTHING ever wrote its end into the manifest -- _close_gap only touched
    # the SQLite operational store, which docs/07 §1 classifies as disposable.
    # So every live gap in the durable, append-only record said "still open"
    # forever, and a reader could not tell a three-second reconnect from a
    # weekend outage. `gap_id` is what lets the closure find the record it
    # completes. Completing a gap is not editing history: the fact being
    # recorded is one interval, and its end is simply not knowable when it
    # starts.
    gap_id: int | None = None
    # PR-10, both additive and both omitted from the manifest when None, so a
    # single-connection run's manifest is byte-identical to what it was before
    # this field existed (see `_OPTIONAL_GAP_FIELDS`).
    #
    # `conn_label` is which redundant connection recorded this gap: None for the
    # primary connection A -- which is every gap ever recorded until the
    # redundant stream is switched on -- and e.g. "b" for the second one.
    #
    # `covered_by` is the label of a DIFFERENT connection that was demonstrably
    # recording through this window. It NEVER shortens, closes, or removes the
    # gap: "A was blind and B was not" is a different fact from "no gap
    # occurred", and only the first is true (dataeng §3.a, failure mode 5).
    conn_label: str | None = None
    covered_by: str | None = None


#: Gap fields written to the manifest only when they carry a value. Their absence
#: is their default, so adding one cannot change a single byte of a manifest
#: written by a run that does not use it.
_OPTIONAL_GAP_FIELDS = frozenset({"conn_label", "covered_by"})


class ManifestWriter:
    """Daily manifest of landing-zone files.

    Readers MUST resolve a time range through the manifest rather than through
    directory names. Files partition by ARRIVAL hour, but events are ordered by
    `event_timestamp` (REQ-D-07) and the stream delivers out of order, so a
    range expressed as a directory glob silently clips late-arriving events at
    its boundaries. The manifest carries each file's true first/last event
    timestamps precisely so a reader can widen its file selection correctly.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / "_manifest"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._lockfile = self.dir / ".manifest.lock"

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialise read-modify-write across THREADS AND PROCESSES.

        V3 (validator, second review). `threading.Lock` is process-local, and
        each LandingZoneWriter builds its own ManifestWriter. record_file and
        record_gap are load -> mutate -> os.replace. os.replace makes the file
        never CORRUPT; it does not make the update not LOST. Two ingest
        processes on one root -- a double start, or a restart overlapping a
        process that has not exited -- silently dropped records: measured at
        295 of 600 gap records lost, with verify_manifest reporting clean.

        A lost FILE record is detectable as an ORPHAN. A lost GAP record is
        undetectable by anything, forever, and a missing gap is exactly how
        "we were not watching" comes to read as "nothing happened".

        BUG-006 and BUG-007 together raised the collision rate from once per
        file close to once every few seconds from an independent daemon thread
        per writer, which is what turned a theoretical race into a measured one.
        """
        with self._lock:
            fh = None
            try:
                fh = self._lockfile.open("a+b")
                try:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except (ImportError, OSError):
                    # No flock (Windows, or a filesystem that refuses it). The
                    # in-process lock still holds; cross-process safety is then
                    # provided by the ingest lockfile in cli.py, which is the
                    # real single-instance guard.
                    pass
                yield
            finally:
                if fh is not None:
                    try:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        pass
                    fh.close()

    def path_for(self, dt: str) -> Path:
        return self.dir / f"{dt}.json"

    def _load(self, dt: str) -> dict[str, Any]:
        p = self.path_for(dt)
        if not p.exists():
            return {"dt": dt, "files": [], "gaps": []}
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _atomic_write(self, p: Path, payload: dict[str, Any]) -> None:
        # Atomic replace: a manifest half-written during a crash would be worse
        # than no manifest, because it would look valid.
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def record_file(self, rec: FileRecord) -> None:
        with self._exclusive():
            data = self._load(rec.dt)
            files = [f for f in data["files"] if f["filename"] != rec.filename]
            files.append(asdict(rec))
            files.sort(key=lambda f: f["filename"])
            data["files"] = files
            self._atomic_write(self.path_for(rec.dt), data)

    def record_gap(self, dt: str, gap: GapRecord) -> None:
        with self._exclusive():
            data = self._load(dt)
            rec = {k: v for k, v in asdict(gap).items()
                   if v is not None or k not in _OPTIONAL_GAP_FIELDS}
            if gap.gap_id is not None:
                # Replace the open record for this gap rather than appending a
                # second one. Without this, closing a gap would leave both an
                # "open forever" and a "closed" record for the same interval.
                data["gaps"] = [g for g in data["gaps"]
                                if g.get("gap_id") != gap.gap_id]
            data["gaps"].append(rec)
            data["gaps"].sort(key=lambda g: (g.get("started_at") or "",
                                             g.get("gap_id") or 0))
            self._atomic_write(self.path_for(dt), data)

    def open_gaps(self) -> list[dict[str, Any]]:
        """Every gap record in every daily manifest that has no end. One entry
        per gap_id (a multi-day gap is filed under each day it spans)."""
        seen: set[Any] = set()
        out: list[dict[str, Any]] = []
        for mp in sorted(self.dir.glob("*.json")):
            try:
                with mp.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            for g in data.get("gaps", []):
                if g.get("ended_at") is None:
                    key = (g.get("gap_id"), g.get("run_id"), g.get("started_at"))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(g)
        return out

    def files_for_event_range(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        """Resolve an EVENT-TIME range to the files that may contain it.

        Deliberately inclusive: a file is selected if its event-time span
        overlaps the request at all. Under-selecting here silently drops
        late-arriving events, which is a correctness bug that would be very
        hard to notice downstream.
        """
        out: list[dict[str, Any]] = []
        for mp in sorted(self.dir.glob("*.json")):
            with mp.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            for f in data.get("files", []):
                lo = f.get("first_event_timestamp")
                hi = f.get("last_event_timestamp")
                if lo is None or hi is None:
                    # V2 second-order. "Unknown span -> include" is the right
                    # instinct for a file of market events whose timestamps we
                    # failed to parse. It is wrong for a file that contains NO
                    # market events at all -- a control-only file, which the
                    # flusher now guarantees for every idle hour. Those used to
                    # be returned by a range query for the year 2020.
                    if f.get("event_count", 0) == 0:
                        continue
                    out.append(f)  # genuinely unknown span -- include, do not risk a miss
                    continue
                if hi >= start_iso and lo <= end_iso:
                    out.append(f)
        return out


class LandingZoneWriter:
    """Append-only writer for raw stream frames.

    Thread-safe. Not async -- the stream consumer calls `write` from its loop
    and the work is a memcpy plus an occasional compress, which is cheap enough
    that adding an executor would cost more than it saves.
    """

    def __init__(
        self,
        root: str | Path,
        run_id: str,
        *,
        codec: Codec | str = "zstd",
        codec_level: int | None = None,
        roll_bytes: int = 64 * 1024 * 1024,
        flush_seconds: float = 5.0,
        flush_events: int = 1000,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        subdir: str = "stream",
        auto_flush: bool = True,
    ) -> None:
        self.root = Path(root)
        self.base = self.root / subdir
        self.base.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.codec: Codec = get_codec(codec, codec_level) if isinstance(codec, str) else codec
        self.roll_bytes = roll_bytes
        self.flush_seconds = flush_seconds
        self.flush_events = flush_events
        self._clock = clock
        self._monotonic = monotonic
        self.manifest = ManifestWriter(self.root)

        self._lock = threading.Lock()
        self._seq = 0
        self._file_no = 0
        self._fh = None
        self._w = None
        self._rec: FileRecord | None = None
        self._path: Path | None = None
        self._hasher: hashlib._Hash | None = None
        self._raw_since_frame = 0
        self._events_since_frame = 0
        self._last_frame_at = monotonic()
        self._current_key: tuple[str, str] | None = None

        # BUG-20260909-006. REQ-D-26a promises a frame every `flush_seconds`.
        # It used to be checked ONLY inside write(), which means the timer only
        # fired when the NEXT event arrived. For a thin collection -- the target
        # market, where preflight saw zero events in 60 seconds -- that is the
        # normal regime: one event lands, the laptop sleeps, and the event is
        # gone with nothing on disk recording that it existed.
        #
        # A daemon thread ticks the clock so an idle writer still closes frames.
        # Tests pass auto_flush=False and drive tick() from a fake clock, so the
        # cadence is asserted deterministically rather than by sleeping.
        self._closed = threading.Event()
        self._flusher: threading.Thread | None = None
        if auto_flush and flush_seconds > 0 and flush_seconds < 1e6:
            interval = max(0.25, min(flush_seconds / 2.0, 5.0))
            self._flusher = threading.Thread(
                target=self._flush_loop, args=(interval,),
                name=f"navanax-flusher-{run_id}", daemon=True,
            )
            self._flusher.start()

    # -- public ------------------------------------------------------------
    @property
    def sequence(self) -> int:
        return self._seq

    def tick(self) -> None:
        """Advance the time-based flush cadence without writing anything.

        This is the half of REQ-D-26a that write() cannot provide. Safe to call
        as often as you like: it is a no-op unless a frame is both non-empty and
        older than `flush_seconds`.
        """
        with self._lock:
            self._maybe_flush_frame()

    def _flush_loop(self, interval: float) -> None:
        while not self._closed.wait(interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a flusher that dies silently is
                # worse than one that logs: the writer would keep accepting
                # events and buffering them forever with no frame ever closed.
                import logging
                logging.getLogger("navanax.landing").exception(
                    "landing-zone flush thread failed; frames are NOT being closed "
                    "on the time cadence (REQ-D-26a). Crash loss is now unbounded."
                )

    def write(
        self,
        raw: str,
        *,
        topic: str | None = None,
        event_timestamp: str | None = None,
        received_at: datetime | None = None,
        control: bool = False,
    ) -> int:
        """Append one raw frame. Returns its sequence number.

        `raw` is the frame exactly as it came off the socket. It is stored
        verbatim; this method never parses it beyond what the caller supplies.

        `control=True` marks a protocol frame (join reply, heartbeat, channel
        close). It is landed identically -- it is the only on-disk evidence of
        what this process was subscribed to -- but counted separately, so
        `event_count` keeps meaning "market events" (V2).
        """
        with self._lock:
            now = received_at or self._clock()
            key = (now.strftime("%Y-%m-%d"), now.strftime("%H"))
            if self._fh is None or key != self._current_key:
                self._roll(key, now)

            self._seq += 1
            envelope = {
                "_seq": self._seq,
                "_run": self.run_id,
                "_recv": _iso(now),
                "_topic": topic,
                "_ets": event_timestamp,
                "raw": raw,  # verbatim; see module docstring
            }
            line = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False) + "\n"
            data = line.encode("utf-8")

            self._w.write(data)
            assert self._rec is not None
            if control:
                self._rec.control_count += 1
            else:
                self._rec.event_count += 1
            self._rec.raw_bytes += len(data)
            self._raw_since_frame += len(data)
            self._events_since_frame += 1
            if self._rec.first_seq is None:
                self._rec.first_seq = self._seq
            self._rec.last_seq = self._seq

            if event_timestamp:
                if (
                    self._rec.first_event_timestamp is None
                    or event_timestamp < self._rec.first_event_timestamp
                ):
                    self._rec.first_event_timestamp = event_timestamp
                if (
                    self._rec.last_event_timestamp is None
                    or event_timestamp > self._rec.last_event_timestamp
                ):
                    self._rec.last_event_timestamp = event_timestamp

            self._maybe_flush_frame()
            if self._rec.raw_bytes >= self.roll_bytes:
                self._close_current()
            return self._seq

    def flush(self) -> None:
        with self._lock:
            self._flush_frame()

    def close(self) -> None:
        self._closed.set()
        f = self._flusher
        if f is not None and f.is_alive() and f is not threading.current_thread():
            f.join(timeout=5.0)
        with self._lock:
            self._close_current()

    def record_gap(self, gap: GapRecord) -> None:
        """Register an ingestion gap in EVERY daily manifest it spans.

        V4 (validator, second review). This used to file a gap only under
        `started_at[:10]`. A 57-hour weekend outage -- the headline
        BUG-20260909-009 scenario -- was therefore recorded only under the
        Friday, and a reader asking "were there gaps on Sunday?" got none, from
        the artifact docs/07 tells it it MUST resolve through. The gap register
        in SQLite is not a substitute: docs/07 1 classifies that store as
        disposable and reconstructible, and a gap is reconstructible from
        nothing.
        """
        for dt in _dates_spanned(gap.started_at, gap.ended_at, self._clock):
            self.manifest.record_gap(dt, gap)

    def close_orphan_gaps(self, this_run_id: str, ended_at: str) -> list[GapRecord]:
        """Close every manifest gap that a PREVIOUS run left open (tech-lead,
        PR-1 review, S1). A gap with no end masks every bucket from its start
        to infinity -- one stale record from a run that died mid-disconnect
        blanks every chart forever. The predecessor cannot close it (it is
        dead); its successor can, at the moment the predecessor was last known
        alive, which is what `ended_at` should be. Records are re-filed with
        an end, never deleted."""
        closed: list[GapRecord] = []
        fields = {f.name for f in GapRecord.__dataclass_fields__.values()}
        for g in self.manifest.open_gaps():
            if g.get("run_id") == this_run_id:
                continue
            rec = GapRecord(**{k: v for k, v in g.items() if k in fields})
            end = max(ended_at, rec.started_at or ended_at)
            rec.reason = (rec.reason or "") + f" | left open by run {rec.run_id}; closed by successor {this_run_id} at its startup"
            self.close_gap_record(rec, end)
            closed.append(rec)
        return closed

    def close_gap_record(self, gap: GapRecord, ended_at: str) -> None:
        """Write a gap's END into the durable record.

        BUG-20260909-023. `_close_gap` used to update only the SQLite gap
        register. The manifest -- the artifact docs/07 says a reader MUST
        resolve through, and the one that is durable -- kept `ended_at: null`
        forever. Re-filing the completed record covers every day the gap turned
        out to span, which is only knowable now that it has an end.
        """
        gap.ended_at = ended_at
        self.record_gap(gap)

    def __enter__(self) -> LandingZoneWriter:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- internals ---------------------------------------------------------
    def _maybe_flush_frame(self) -> None:
        """REQ-D-26a: close a frame every flush_seconds or flush_events.

        The true worst-case latency is `flush_seconds + flusher_interval`,
        where the interval is flush_seconds/2 clamped to [0.25, 5.0] -- so ~7.5s
        at the shipped flush_seconds=5, not 5s. Stated here because the
        difference between a documented bound and the real one is the exact
        shape of the last four bugs in this project's log.
        """
        if self._events_since_frame >= self.flush_events:
            self._flush_frame()
            return
        if (self._monotonic() - self._last_frame_at) >= self.flush_seconds:
            self._flush_frame()

    def _flush_frame(self) -> None:
        if self._w is None or self._events_since_frame == 0:
            self._last_frame_at = self._monotonic()
            return
        self._w.flush_frame()
        if self._rec is not None:
            self._rec.frames += 1
            # BUG-20260909-007: publish progress to the manifest as the file
            # grows. A crash now leaves a manifest that says how many events
            # SHOULD be recoverable from the file, which is what turns "the
            # file is short" into a detectable fact rather than a silent one.
            self._rec.last_flush_at = _iso(self._clock())
            if self._path is not None and self._path.exists():
                self._rec.stored_bytes = self._path.stat().st_size
            self.manifest.record_file(self._rec)
        self._events_since_frame = 0
        self._raw_since_frame = 0
        self._last_frame_at = self._monotonic()

    def _roll(self, key: tuple[str, str], now: datetime) -> None:
        self._close_current()
        dt, hour = key
        d = self.base / f"dt={dt}" / f"hour={hour}"
        d.mkdir(parents=True, exist_ok=True)
        self._file_no += 1
        name = f"events-{self.run_id}-{self._file_no:06d}{self.codec.ext}"
        self._path = d / name
        self._fh = self._path.open("wb")
        self._w = self.codec.writer(self._fh)
        self._current_key = key
        self._events_since_frame = 0
        self._raw_since_frame = 0
        self._last_frame_at = self._monotonic()
        self._rec = FileRecord(
            filename=str(self._path.relative_to(self.root)),
            dt=dt,
            hour=hour,
            run_id=self.run_id,
            codec=self.codec.name,
            opened_at=_iso(now),
            status="open",
        )
        # Register the file the moment it exists. Previously the manifest only
        # learned about a file when it closed, so any file the process was
        # killed while writing was an orphan that `verify_manifest` could not
        # see and therefore certified clean (BUG-20260909-007).
        self.manifest.record_file(self._rec)

    def _close_current(self) -> None:
        if self._fh is None:
            return
        try:
            self._flush_frame()
            self._w.close()
        finally:
            self._fh.close()
        assert self._rec is not None and self._path is not None
        raw_bytes = self._path.read_bytes()
        self._rec.stored_bytes = len(raw_bytes)
        self._rec.sha256 = hashlib.sha256(raw_bytes).hexdigest()
        self._rec.closed_at = _iso(self._clock())
        self._rec.status = "closed"
        self.manifest.record_file(self._rec)
        self._fh = None
        self._w = None
        self._rec = None
        self._path = None
        self._current_key = None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def read_file(
    path: str | Path,
    codec: Codec | str | None = None,
    *,
    tolerate_truncation: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield envelopes from a landing-zone file.

    `tolerate_truncation` is the crash-recovery path: a file whose last frame
    was never closed still yields every complete frame before it. That is the
    property REQ-D-26a exists to provide, and it is exercised directly by the
    test suite rather than assumed.

    It DEFAULTS TO FALSE (V1, validator second review). It used to default to
    True, which meant every ordinary read silently downgraded any decompression
    failure to best-effort recovery and returned a prefix -- turning "this file
    is damaged" into "this file is short", with no exception anywhere. Recovery
    is a decision; it should be made deliberately by the caller that wants it,
    not applied by default to every read in the system.
    """
    p = Path(path)
    if codec is None:
        codec = "zstd" if p.name.endswith(".zst") else "gzip" if p.name.endswith(".gz") else "raw"
    c: Codec = get_codec(codec) if isinstance(codec, str) else codec
    blob = p.read_bytes()
    try:
        data = c.decompress(blob)
    except Exception:  # noqa: BLE001 - deliberate: ANY decompression failure
        # means the file is damaged or truncated, and every codec raises a
        # different type. Falling back to frame-by-frame recovery is the whole
        # point of REQ-D-26a; narrowing this would silently lose recoverable data.
        if not tolerate_truncation:
            raise
        data = c.decompress_truncated(blob)
    for line in data.decode("utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        yield json.loads(line)


#: Problem prefixes that mean the historical record itself is in question.
#: Everything else verify_manifest returns is a NOTE the operator should read
#: but which does not, on its own, mean stop.
INTEGRITY_PREFIXES = ("MISSING", "CHECKSUM MISMATCH", "COUNT MISMATCH", "ORPHAN",
                      "UNREADABLE", "no _manifest")


def is_integrity_failure(problem: str) -> bool:
    return problem.startswith(INTEGRITY_PREFIXES)


def verify_manifest(root: str | Path, *, deep: bool = True) -> list[str]:
    """Reconcile the manifest against the disk, in BOTH directions.

    BUG-20260909-007. This used to walk manifest -> disk only. That direction
    alone cannot see a file that exists on disk and is absent from the manifest,
    which was exactly what an ungracefully-killed process left behind, because
    files were only recorded when they closed. The audit whose entire job is to
    notice that something is wrong with the landing zone returned "verified
    clean" on a landing zone with unrecorded data files in it.

    Now:
      manifest -> disk   every recorded file exists, its sha256 still matches,
                         AND it reads back the number of records the manifest
                         says it holds (`deep=True`, the default)
      disk -> manifest   every file on disk is recorded (ORPHAN otherwise)

    Returns a list of problem strings. Entries matching INTEGRITY_PREFIXES are
    S0a -- the append-only guarantee is what the whole reprocessing path rests
    on. OPEN entries are notes: a file recorded but not yet closed is normal
    while ingestion is running, and a sign of an unclean shutdown when it is
    not. Called by the weekly integrity audit
    (docs/03_VALIDATION_AND_TESTING.md §3.4).
    """
    root = Path(root)
    problems: list[str] = []
    mdir = root / "_manifest"
    if not mdir.exists():
        return ["no _manifest directory"]

    recorded: set[str] = set()
    for mp in sorted(mdir.glob("*.json")):
        with mp.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        for f in data.get("files", []):
            recorded.add(f["filename"])
            fp = root / f["filename"]
            if not fp.exists():
                problems.append(f"MISSING {f['filename']}")
                continue
            if f.get("status") == "closed" and f.get("sha256"):
                actual = hashlib.sha256(fp.read_bytes()).hexdigest()
                if actual != f["sha256"]:
                    problems.append(
                        f"CHECKSUM MISMATCH {f['filename']} "
                        f"manifest={f['sha256'][:12]} actual={actual[:12]}"
                    )
                    continue
                # V1. The checksum is over the COMPRESSED bytes, so it proves the
                # file was not modified -- and proves NOTHING about whether the
                # decoder can still read all of it. BUG-20260909-010's entire
                # shape was "byte-perfect file, decoder returns a prefix,
                # checksum still matches, audit says clean". The manifest has
                # held the number that catches this since BUG-007; nothing was
                # comparing against it. Now it does.
                if deep:
                    n = _recoverable_event_count(fp)
                    expected = f.get("event_count", 0) + f.get("control_count", 0)
                    if isinstance(n, str):
                        problems.append(f"UNREADABLE {f['filename']} -- {n}")
                    elif n != expected:
                        problems.append(
                            f"COUNT MISMATCH {f['filename']} -- manifest says "
                            f"{expected} records, the file reads back {n}. The "
                            f"checksum matches, so the FILE is intact and the "
                            f"DECODER is returning a prefix (see BUG-20260909-010)."
                        )
                continue
            if f.get("sha256") is None:
                # Open (or abandoned) file: no final checksum exists yet, by
                # design. Report what is actually recoverable from it, so the
                # operator sees a number rather than an absence.
                n = _recoverable_event_count(fp)
                problems.append(
                    f"OPEN {f['filename']} -- not closed cleanly; "
                    f"{n} events recoverable, manifest last recorded "
                    f"{f.get('event_count', 0)} at {f.get('last_flush_at') or f.get('opened_at')}"
                )
                continue
            actual = hashlib.sha256(fp.read_bytes()).hexdigest()
            if actual != f["sha256"]:
                problems.append(
                    f"CHECKSUM MISMATCH {f['filename']} "
                    f"manifest={f['sha256'][:12]} actual={actual[:12]}"
                )

    # disk -> manifest. Anything here is data nothing knows about.
    for fp in sorted(root.rglob("*")):
        if not fp.is_file():
            continue
        rel = str(fp.relative_to(root))
        if rel.startswith("_manifest" + os.sep) or rel.startswith("."):
            continue
        if fp.name.startswith(".tmp-"):
            continue
        if rel not in recorded:
            n = _recoverable_event_count(fp)
            problems.append(
                f"ORPHAN {rel} -- on disk, absent from the manifest "
                f"({n} events recoverable, {fp.stat().st_size} bytes)"
            )
    return problems


def _recoverable_event_count(fp: Path) -> int | str:
    try:
        return sum(1 for _ in read_file(fp, tolerate_truncation=True))  # noqa: E501
    except Exception as exc:  # noqa: BLE001 - reporting, never raising: the
        # audit must finish and list every problem, not stop at the first
        # unreadable file.
        return f"unreadable: {type(exc).__name__}"
