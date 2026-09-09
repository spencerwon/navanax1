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
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codec import Codec, get_codec

_UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(_UTC)


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
    backfillable: bool = True
    backfilled_at: str | None = None


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
        with self._lock:
            data = self._load(rec.dt)
            files = [f for f in data["files"] if f["filename"] != rec.filename]
            files.append(asdict(rec))
            files.sort(key=lambda f: f["filename"])
            data["files"] = files
            self._atomic_write(self.path_for(rec.dt), data)

    def record_gap(self, dt: str, gap: GapRecord) -> None:
        with self._lock:
            data = self._load(dt)
            data["gaps"].append(asdict(gap))
            self._atomic_write(self.path_for(dt), data)

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
                    out.append(f)  # unknown span -- include rather than risk a miss
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
    ) -> int:
        """Append one raw frame. Returns its sequence number.

        `raw` is the frame exactly as it came off the socket. It is stored
        verbatim; this method never parses it beyond what the caller supplies.
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
        """Register an ingestion gap. Gaps are recorded, never filled."""
        dt = gap.started_at[:10]
        self.manifest.record_gap(dt, gap)

    def __enter__(self) -> LandingZoneWriter:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- internals ---------------------------------------------------------
    def _maybe_flush_frame(self) -> None:
        """REQ-D-26a: close a frame every flush_seconds or flush_events."""
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
    tolerate_truncation: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield envelopes from a landing-zone file.

    `tolerate_truncation` is the crash-recovery path: a file whose last frame
    was never closed still yields every complete frame before it. That is the
    property REQ-D-26a exists to provide, and it is exercised directly by the
    test suite rather than assumed.
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
INTEGRITY_PREFIXES = ("MISSING", "CHECKSUM MISMATCH", "ORPHAN", "UNREADABLE", "no _manifest")


def is_integrity_failure(problem: str) -> bool:
    return problem.startswith(INTEGRITY_PREFIXES)


def verify_manifest(root: str | Path) -> list[str]:
    """Reconcile the manifest against the disk, in BOTH directions.

    BUG-20260909-007. This used to walk manifest -> disk only. That direction
    alone cannot see a file that exists on disk and is absent from the manifest,
    which was exactly what an ungracefully-killed process left behind, because
    files were only recorded when they closed. The audit whose entire job is to
    notice that something is wrong with the landing zone returned "verified
    clean" on a landing zone with unrecorded data files in it.

    Now:
      manifest -> disk   every recorded file exists and its sha256 still matches
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
        return sum(1 for _ in read_file(fp, tolerate_truncation=True))
    except Exception as exc:  # noqa: BLE001 - reporting, never raising: the
        # audit must finish and list every problem, not stop at the first
        # unreadable file.
        return f"unreadable: {type(exc).__name__}"
