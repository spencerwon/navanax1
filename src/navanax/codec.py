"""Pluggable frame-based compression for the landing zone.

Why this abstraction exists rather than calling zstandard directly:

docs/07_STORAGE_AND_RECORDING.md §2.1 makes a crash-safety claim -- "kill the
process at any byte and everything up to that point is still valid" -- which is
true of raw JSONL and FALSE of a naive `.zst` stream. A zstd stream killed
mid-frame loses the entire unflushed block, which at a 64 MB roll size could be
most of the file, in exactly the "laptop slept" case the design invokes.

REQ-D-26a therefore requires the writer to close a frame on an explicit cadence.
A frame boundary is a recovery point. That requirement is what this module
implements, and every codec here must honour it.

The abstraction also lets the test suite exercise the real rolling / manifest /
recovery logic using only the standard library, which matters because the
tricky code is the writer, not the compressor binding.
"""

from __future__ import annotations

import gzip
import io
import zlib
from typing import Any, BinaryIO, Protocol


class FrameWriter(Protocol):
    """A writer that can close a frame on demand, creating a recovery point."""

    def write(self, data: bytes) -> None: ...

    def flush_frame(self) -> None:
        """Close the current frame. Everything written before this survives a kill."""
        ...

    def close(self) -> None: ...


class Codec(Protocol):
    name: str
    ext: str

    def writer(self, fh: BinaryIO) -> FrameWriter: ...

    def decompress(self, data: bytes) -> bytes: ...

    def decompress_truncated(self, data: bytes) -> bytes:
        """Best-effort recovery from a truncated file.

        Returns every byte from every COMPLETE frame, discarding a partial
        trailing frame. This is the operation that makes the crash-safety
        property real, so every codec must implement it and the test suite
        asserts on it directly.
        """
        ...


# ---------------------------------------------------------------------------
# gzip -- standard library. Multi-member gzip is a genuine framed format:
# each member is independently decompressible and `gzip.decompress` reads a
# concatenation of them. Used for tests and as the fallback when zstandard is
# not installed.
# ---------------------------------------------------------------------------
class _GzipFrameWriter:
    def __init__(self, fh: BinaryIO, level: int = 6) -> None:
        self._fh = fh
        self._level = level
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._buf.extend(data)

    def flush_frame(self) -> None:
        if not self._buf:
            return
        # A complete, self-contained gzip member.
        member = io.BytesIO()
        with gzip.GzipFile(fileobj=member, mode="wb", compresslevel=self._level, mtime=0) as gz:
            gz.write(bytes(self._buf))
        self._fh.write(member.getvalue())
        self._fh.flush()
        self._buf.clear()

    def close(self) -> None:
        self.flush_frame()


class GzipCodec:
    name = "gzip"
    ext = ".jsonl.gz"

    def __init__(self, level: int = 6) -> None:
        self.level = level

    def writer(self, fh: BinaryIO) -> FrameWriter:
        return _GzipFrameWriter(fh, self.level)

    def decompress(self, data: bytes) -> bytes:
        return gzip.decompress(data)

    def decompress_truncated(self, data: bytes) -> bytes:
        """Walk gzip members, stopping at the first incomplete one."""
        out = bytearray()
        pos = 0
        n = len(data)
        while pos < n:
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                chunk = d.decompress(data[pos:])
                if not d.eof:
                    break  # trailing member is incomplete -- discard it
                out.extend(chunk)
                consumed = len(data) - pos - len(d.unused_data)
                if consumed <= 0:
                    break
                pos += consumed
            except zlib.error:
                break
        return bytes(out)


# ---------------------------------------------------------------------------
# zstd -- production. Frames are flushed explicitly per REQ-D-26a.
# ---------------------------------------------------------------------------
class _ZstdFrameWriter:
    def __init__(self, fh: BinaryIO, level: int) -> None:
        import zstandard  # imported lazily so the module loads without it

        self._zstd = zstandard
        self._fh = fh
        self._level = level
        self._cctx = zstandard.ZstdCompressor(level=level)
        self._w = self._cctx.stream_writer(fh, closefd=False)

    def write(self, data: bytes) -> None:
        self._w.write(data)

    def flush_frame(self) -> None:
        # FLUSH_FRAME ends the current frame; the next write starts a new one.
        # This is the recovery point REQ-D-26a requires.
        self._w.flush(self._zstd.FLUSH_FRAME)
        self._fh.flush()

    def close(self) -> None:
        self._w.close()
        self._fh.flush()


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _zstd_one_frame(dctx: Any, blob: bytes) -> bytes:
    """Decompress exactly one zstd frame from the front of `blob`.

    Uses stream_reader with its DEFAULT frame behaviour, which is the one thing
    that is stable across every python-zstandard version. Whether that default
    is "stop at the first frame" (<0.23) or "read across frames" (>=0.23) does
    not matter here, because `blob` is sliced to a single frame by the caller.
    """
    out = bytearray()
    with dctx.stream_reader(io.BytesIO(blob)) as r:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.extend(chunk)
    return bytes(out)


def _zstd_walk(data: bytes, *, strict: bool) -> bytes:
    """Decompress a concatenation of zstd frames, one frame at a time.

    BUG-20260909-010. `ZstdDecompressor.stream_reader` gained a
    `read_across_frames` parameter whose DEFAULT CHANGED in python-zstandard
    0.23.0: before that release a multi-frame file silently decompressed to its
    first frame only. Every landing-zone file we write is multi-frame by
    design (REQ-D-26a), so on an older binding `decompress` would have returned
    the first ~5 seconds of a 64 MB file and reported success. Nothing in the
    system would have noticed: the manifest checksum is over the COMPRESSED
    bytes and would still have matched.

    Rather than depend on a version whose behaviour we cannot check at import
    time, this walks the frames explicitly. Frame boundaries are found by the
    4-byte magic; a magic sequence occurring by chance inside compressed data
    is handled by validation, not by trust -- a slice that does not decompress
    cleanly is extended to the next candidate boundary before being believed.

    `strict=True` raises on a damaged or truncated trailing frame (the normal
    read path). `strict=False` stops there and returns everything recovered so
    far, which is the crash-recovery path REQ-D-26a exists to provide.
    """
    import zstandard

    dctx = zstandard.ZstdDecompressor()
    out = bytearray()
    pos = 0
    n = len(data)

    while pos < n:
        # Candidate ends: every later frame magic, then end-of-file.
        ends: list[int] = []
        probe = pos + 4
        while len(ends) < 64:
            nxt = data.find(ZSTD_MAGIC, probe)
            if nxt == -1:
                break
            ends.append(nxt)
            probe = nxt + 4
        ends.append(n)

        frame: bytes | None = None
        consumed = 0
        last_exc: Exception | None = None
        for end in ends:
            try:
                frame = _zstd_one_frame(dctx, data[pos:end])
            except Exception as exc:  # noqa: BLE001 - any binding raises its own type
                last_exc = exc
                continue
            consumed = end - pos
            break

        if frame is None:
            if strict:
                raise ValueError(
                    f"zstd frame at byte {pos} does not decompress "
                    f"({type(last_exc).__name__}: {last_exc})"
                )
            break  # truncated trailing frame -- recovery path, keep what we have
        out.extend(frame)
        if consumed <= 0:
            break
        pos += consumed
    return bytes(out)


class ZstdCodec:
    name = "zstd"
    ext = ".jsonl.zst"

    def __init__(self, level: int = 9) -> None:
        self.level = level

    def writer(self, fh: BinaryIO) -> FrameWriter:
        return _ZstdFrameWriter(fh, self.level)

    def decompress(self, data: bytes) -> bytes:
        return _zstd_walk(data, strict=True)

    def decompress_truncated(self, data: bytes) -> bytes:
        return _zstd_walk(data, strict=False)


class RawCodec:
    """No compression. For debugging and for measuring true raw sizes."""

    name = "raw"
    ext = ".jsonl"

    def writer(self, fh: BinaryIO) -> FrameWriter:
        return _RawFrameWriter(fh)

    def decompress(self, data: bytes) -> bytes:
        return data

    def decompress_truncated(self, data: bytes) -> bytes:
        # Drop a trailing partial line.
        idx = data.rfind(b"\n")
        return data[: idx + 1] if idx != -1 else b""


class _RawFrameWriter:
    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh

    def write(self, data: bytes) -> None:
        self._fh.write(data)

    def flush_frame(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.flush()


def verify_codec_roundtrip(codec: Codec, *, frames: int = 3) -> None:
    """Prove, on THIS machine with THIS library version, that the crash-safety
    property actually holds. Raises RuntimeError if it does not.

    Written because the alternative was to reason about which version of
    `zstandard` pip happened to install and what its defaults were that year.
    A 200-microsecond check at startup answers the question for the machine
    that is about to record data it cannot re-fetch.

    Asserts two things:
      1. A file of `frames` closed frames reads back COMPLETE. (A binding that
         stops at frame one returns 1/frames of the file and raises nothing.)
      2. That same file with its trailing frame chopped still yields every
         COMPLETE frame before the cut. That is REQ-D-26a's whole purpose.
    """
    buf = io.BytesIO()
    w = codec.writer(buf)
    lines = [f"frame-{i}\n".encode() for i in range(frames)]
    for line in lines:
        w.write(line)
        w.flush_frame()
    w.close()
    blob = buf.getvalue()
    expected = b"".join(lines)

    got = codec.decompress(blob)
    if got != expected:
        raise RuntimeError(
            f"codec {codec.name!r} FAILED the multi-frame round-trip on this machine: "
            f"wrote {len(expected)} bytes across {frames} frames, read back {len(got)}. "
            f"Landing-zone files are multi-frame by design (REQ-D-26a), so this codec "
            f"would silently return a PREFIX of every file while reporting success, and "
            f"the manifest checksum -- taken over compressed bytes -- would still match. "
            f"Do not ingest with this codec. For zstd, upgrade: pip install -U zstandard"
        )

    # Chop mid-final-frame. Everything before the cut must survive.
    cut = blob.rfind(ZSTD_MAGIC) if codec.name == "zstd" else int(len(blob) * 0.9)
    if cut <= 0 or cut >= len(blob):
        cut = int(len(blob) * 0.9)
    damaged = blob[:cut] + blob[cut : cut + max(1, (len(blob) - cut) // 2)]
    recovered = codec.decompress_truncated(damaged)
    if not recovered.startswith(b"frame-0\n"):
        raise RuntimeError(
            f"codec {codec.name!r} recovered NOTHING from a truncated file. "
            f"REQ-D-26a's crash-safety guarantee does not hold with this codec on "
            f"this machine; a process killed mid-write would lose the whole file."
        )


def get_codec(name: str, level: int | None = None) -> Codec:
    """Resolve a codec by name, falling back loudly rather than silently.

    A silent fallback from zstd to gzip would change the file extension and the
    manifest without anyone noticing, so this raises instead.
    """
    name = name.lower()
    if name == "zstd":
        try:
            import zstandard  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "codec 'zstd' requested but the `zstandard` package is not installed. "
                "Install it, or set landing.codec: gzip in config -- but do so "
                "deliberately: the file extension and manifest record the codec, "
                "and a silent swap would make old and new files inconsistent."
            ) from exc
        return ZstdCodec(level if level is not None else 9)
    if name == "gzip":
        return GzipCodec(level if level is not None else 6)
    if name == "raw":
        return RawCodec()
    raise ValueError(f"unknown codec {name!r}; expected one of: zstd, gzip, raw")
