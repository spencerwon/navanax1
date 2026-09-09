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


class ZstdCodec:
    name = "zstd"
    ext = ".jsonl.zst"

    def __init__(self, level: int = 9) -> None:
        self.level = level

    def writer(self, fh: BinaryIO) -> FrameWriter:
        return _ZstdFrameWriter(fh, self.level)

    def decompress(self, data: bytes) -> bytes:
        import zstandard

        dctx = zstandard.ZstdDecompressor()
        out = bytearray()
        with dctx.stream_reader(io.BytesIO(data)) as r:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                out.extend(chunk)
        return bytes(out)

    def decompress_truncated(self, data: bytes) -> bytes:
        import zstandard

        out = bytearray()
        pos = 0
        n = len(data)
        while pos < n:
            try:
                size = zstandard.frame_content_size(data[pos:])
            except Exception:
                break
            try:
                frame = zstandard.ZstdDecompressor().decompress(
                    data[pos:], max_output_size=max(size, 1 << 24) if size > 0 else (1 << 24)
                )
            except Exception:
                break  # partial trailing frame
            out.extend(frame)
            try:
                consumed = zstandard.frame_header_size(data[pos:])
            except Exception:
                break
            # Advance by locating the next frame magic.
            nxt = data.find(b"\x28\xb5\x2f\xfd", pos + consumed)
            if nxt == -1:
                break
            pos = nxt
        return bytes(out)


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
