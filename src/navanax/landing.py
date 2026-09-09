"""Landing zone writer -- the first and most important component in the system.

docs/07_STORAGE_AND_RECORDING.md §1.2: "Land first, normalize second."

Every stream frame is written here, verbatim, before anything parses it. If the
normalizer has a bug, the raw bytes are still on disk and can be reprocessed.
If we normalized in flight and discarded the original, a parsing bug would mean
the data is gone -- and at 600 REST reads/hour it may not be re-fetchable at any
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
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

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
        self._hasher: "hashlib._Hash | None" = None
        self._raw_since_frame = 0
        self._events_since_frame = 0
        self._last_frame_at = monotonic()
        self._current_key: tuple[str, str] | None = None

    # -- public ------------------------------------------------------------
    @property
    def sequence(self) -> int:
        return self._seq

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
        with self._lock:
            self._close_current()

    def record_gap(self, gap: GapRecord) -> None:
        """Register an ingestion gap. Gaps are recorded, never filled."""
        dt = gap.started_at[:10]
        self.manifest.record_gap(dt, gap)

    def __enter__(self) -> "LandingZoneWriter":
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
        )

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
        self.manifest.record_file(self._rec)
        self._fh = None
        self._w = None
        self._rec = None
        self._path = None
        self._current_key = None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def read_file(path: str | Path, codec: Codec | str | None = None, *, tolerate_truncation: bool = True) -> Iterator[dict[str, Any]]:
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
    except Exception:
        if not tolerate_truncation:
            raise
        data = c.decompress_truncated(blob)
    for line in data.decode("utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        yield json.loads(line)


def verify_manifest(root: str | Path) -> list[str]:
    """Re-verify every recorded sha256. Returns a list of problems.

    A mismatch means a landing-zone file was modified, which is an S0a: the
    append-only guarantee is what the reprocessing path rests on, and a silent
    modification invalidates it. Called by the weekly integrity audit
    (docs/03_VALIDATION_AND_TESTING.md §3.4).
    """
    root = Path(root)
    problems: list[str] = []
    mdir = root / "_manifest"
    if not mdir.exists():
        return ["no _manifest directory"]
    for mp in sorted(mdir.glob("*.json")):
        with mp.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        for f in data.get("files", []):
            fp = root / f["filename"]
            if not fp.exists():
                problems.append(f"MISSING {f['filename']}")
                continue
            if f.get("sha256") is None:
                problems.append(f"NO CHECKSUM {f['filename']} (file was never closed cleanly)")
                continue
            actual = hashlib.sha256(fp.read_bytes()).hexdigest()
            if actual != f["sha256"]:
                problems.append(
                    f"CHECKSUM MISMATCH {f['filename']} "
                    f"manifest={f['sha256'][:12]} actual={actual[:12]}"
                )
    return problems
