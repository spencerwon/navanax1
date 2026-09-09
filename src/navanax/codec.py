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
from typing import BinaryIO, Protocol


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

        self._pending = 0

    def write(self, data: bytes) -> None:
        self._w.write(data)
        self._pending += len(data)

    def flush_frame(self) -> None:
        # FLUSH_FRAME ends the current frame; the next write starts a new one.
        # This is the recovery point REQ-D-26a requires.
        if self._pending:
            self._w.flush(self._zstd.FLUSH_FRAME)
            self._pending = 0
        self._fh.flush()

    def close(self) -> None:
        # BUG-20260909-032. `ZstdCompressionWriter.close()` ends the stream by
        # emitting a frame -- even when nothing has been written since the last
        # FLUSH_FRAME, which for this writer is always. The result was an EMPTY
        # epilogue frame at the end of every landing-zone file. Harmless to
        # read, but it is a frame that carries no data, and the startup gate's
        # "find the last frame" heuristic landed on it instead of on the last
        # frame that held anything. Only close the compressor if a frame is
        # actually open; otherwise there is nothing to end.
        if self._pending:
            self._w.close()
        self._pending = 0
        self._fh.flush()


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"   # kept for tooling; the walk no longer depends on it


def _zstd_walk(data: bytes, *, strict: bool) -> bytes:
    """Decompress a concatenation of zstd frames, one frame at a time.

    BUG-20260909-010: `stream_reader`'s `read_across_frames` default changed
    between python-zstandard releases, so a multi-frame file could silently
    read back as its first frame. This walk never relies on that flag.

    BUG-20260909-024 / -032: the first version of this walk found frame
    boundaries by SCANNING for the 4-byte magic and validating candidates,
    which left a residual hazard -- a chance magic inside compressed data plus
    a binding that returns partial output instead of raising -- that could
    only be excluded by a startup check. This version has no such hazard,
    because it does not guess where frames end: `decompressobj()` reports it.
    After a frame is fully decoded `obj.eof` is True and `obj.unused_data` is
    every byte after it. That is the same contract `zlib.decompressobj`
    provides, and the gzip walk in this module has used it from the start.

    Truncation detection is therefore OURS, not the binding's: a frame whose
    input ran out before `eof` is incomplete, whether or not the library
    chooses to raise about it.

    `strict=True` raises on a damaged or incomplete frame (the normal read
    path). `strict=False` stops there and returns everything recovered so far,
    which is the crash-recovery path REQ-D-26a exists to provide.
    """
    import zstandard

    dctx = zstandard.ZstdDecompressor()
    out = bytearray()
    pos = 0
    n = len(data)

    while pos < n:
        obj = dctx.decompressobj()
        if not (hasattr(obj, "eof") and hasattr(obj, "unused_data")):
            raise RuntimeError(
                "this python-zstandard build's decompressobj() does not expose "
                "`eof` and `unused_data`, which the landing-zone reader needs to "
                "find frame boundaries without guessing. Upgrade: "
                "pip install -U 'zstandard>=0.23.0'"
            )
        try:
            chunk = obj.decompress(data[pos:])
        except zstandard.ZstdError as exc:
            if strict:
                raise ValueError(
                    f"zstd frame at byte {pos} is damaged: {exc}"
                ) from exc
            break
        if not obj.eof:
            # Input ended mid-frame. This is exactly what a process killed
            # mid-write leaves behind, and exactly what strict mode must refuse.
            if strict:
                raise ValueError(
                    f"zstd frame at byte {pos} is incomplete: input ended before "
                    f"the frame did (file truncated)"
                )
            break
        out.extend(chunk)
        consumed = (n - pos) - len(obj.unused_data)
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
    """No compression. For debugging and for measuring true raw sizes.

    Its "frame" is a line, which is why docs/07 §2.1's crash-safety claim is
    trivially true of raw JSONL: every complete line is independently valid.
    """

    name = "raw"
    ext = ".jsonl"

    def writer(self, fh: BinaryIO) -> FrameWriter:
        return _RawFrameWriter(fh)

    def decompress(self, data: bytes) -> bytes:
        # Strict mode must RAISE on truncation rather than quietly returning a
        # prefix -- the property verify_codec_roundtrip check (3) enforces for
        # every codec. For a line-oriented format, truncation is exactly "the
        # last line has no newline". Returning it silently is how a partial
        # record becomes indistinguishable from a complete one.
        if data and not data.endswith(b"\n"):
            raise ValueError(
                "raw landing-zone file ends mid-line -- the process was killed "
                "while writing. Use decompress_truncated() to recover the "
                "complete lines deliberately."
            )
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

    Asserts, for every codec:
      1. A file of `frames` closed frames reads back COMPLETE. (A binding that
         stops at frame one returns 1/frames of the file and raises nothing.)
      2. That same file with its trailing frame chopped still yields every
         COMPLETE frame before the cut, and ONLY those -- not just the first.
         That is REQ-D-26a's whole purpose.
      3. A DAMAGED frame RAISES in strict mode rather than returning a prefix.
         (V5, validator second review.) The frame walk finds boundaries by the
         4-byte zstd magic, which occurs by chance inside compressed data with
         probability ~1.5% per 64 MB file -- three or four files a year at the
         documented volume. If the binding's stream_reader returns a partial
         result instead of raising on a truncated frame, the walk accepts that
         false boundary, `decompress` returns a silently truncated file, and
         nothing raises. That is BUG-20260909-010 all over again, one layer
         down. A binding that behaves that way cannot be used safely, and this
         check is what refuses it.
      4. `decompress` on a healthy file consumes the WHOLE file, so a prefix
         can never be mistaken for success.
    """
    buf = io.BytesIO()
    w = codec.writer(buf)
    lines = [f"frame-{i}\n".encode() for i in range(frames)]
    for i, line in enumerate(lines):
        if i == frames - 1:
            last_frame_start = len(buf.getvalue())
        w.write(line)
        w.flush_frame()
    # Recorded BEFORE close(): a binding may append an epilogue frame on close,
    # and a cut that lands in an epilogue damages nothing (BUG-20260909-032 --
    # the first version of this gate found "the last frame" by scanning for
    # the magic and hit exactly that).
    last_frame_end = len(buf.getvalue())
    w.close()
    blob = buf.getvalue()
    expected = b"".join(lines)
    complete = b"".join(lines[: frames - 1])

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

    # Two truncations of the last CONTENT frame, both of which a kill can
    # produce: (a) the frame's last three bytes missing -- inside its data or
    # trailer, the case where a lazy decoder might hand back a partial result
    # and call it done; (b) only the first six bytes present -- a kill during
    # the header. Every complete frame before the cut must be recovered, ALL of
    # them, and strict mode must refuse the file outright.
    cuts = {
        "tail": blob[: last_frame_end - 3],
        "head": blob[: last_frame_start + min(6, last_frame_end - last_frame_start - 1)],
    }
    for label, damaged in cuts.items():
        recovered = codec.decompress_truncated(damaged)
        if recovered != complete:
            raise RuntimeError(
                f"codec {codec.name!r} recovered {len(recovered)} bytes from a file "
                f"truncated at its {label}; the {frames - 1} complete frames before "
                f"the cut hold {len(complete)}. REQ-D-26a's crash-safety guarantee "
                f"does not hold with this codec on this machine. Recovering only the "
                f"first frame -- or a partial last one -- is the BUG-20260909-010 "
                f"failure mode, not a pass."
            )
        try:
            got = codec.decompress(damaged)
            raised = False
        except Exception:  # noqa: BLE001 - any binding raises its own type
            raised = True
            got = b""
        if not raised:
            raise RuntimeError(
                f"codec {codec.name!r} did NOT raise on a file truncated at its "
                f"{label} -- it returned {len(got)} of {len(expected)} bytes and "
                f"reported success.\n"
                f"  A reader that turns 'this file is damaged' into 'this file is "
                f"short' cannot be used to record data that cannot be re-fetched.\n"
                f"  For zstd: pip install -U 'zstandard>=0.23.0'"
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
