#!/usr/bin/env python3
"""Print what THIS python-zstandard build actually does. No opinions, just facts.

Runs in CI before the codec gate so that a red X comes with answers. Every
question here is one the project has already got wrong by assuming
(BUG-20260909-010, -024, -032). Costs nothing to ask.
"""
from __future__ import annotations

import io
import sys

try:
    import zstandard as z
except ImportError:
    print("zstandard: NOT INSTALLED -- the production codec cannot be probed here")
    sys.exit(0)

print(f"python-zstandard {z.__version__}  (zstd lib {z.ZSTD_VERSION})")

# --- 1. does close() emit an empty epilogue frame after FLUSH_FRAME? --------
buf = io.BytesIO()
w = z.ZstdCompressor(level=3).stream_writer(buf, closefd=False)
w.write(b"frame-0\n")
w.flush(z.FLUSH_FRAME)
after_flush = len(buf.getvalue())
w.close()
after_close = len(buf.getvalue())
print(f"epilogue frame on close(): {'YES, ' + str(after_close - after_flush) + ' bytes' if after_close > after_flush else 'no'}")

# --- 2. decompressobj() contract the landing-zone walk depends on ---------
blob = buf.getvalue()[:after_flush]
two = blob + blob
obj = z.ZstdDecompressor().decompressobj()
out = obj.decompress(two)
print(f"decompressobj: has eof={hasattr(obj, 'eof')} unused_data={hasattr(obj, 'unused_data')}")
if hasattr(obj, "eof"):
    print(f"  one full frame + extra -> eof={obj.eof}, out={out!r}, "
          f"unused={len(getattr(obj, 'unused_data', b''))} bytes "
          f"(expect {len(blob)})")

# --- 3. truncated frame: does the binding raise, or hand back a partial? ---
cut = blob[:-3]
obj = z.ZstdDecompressor().decompressobj()
try:
    out = obj.decompress(cut)
    print(f"decompressobj on a truncated frame: NO EXCEPTION, out={out!r}, "
          f"eof={getattr(obj, 'eof', '?')}  <- so eof is what detects it")
except z.ZstdError as exc:
    print(f"decompressobj on a truncated frame: raises ZstdError ({exc})")
try:
    with z.ZstdDecompressor().stream_reader(io.BytesIO(cut)) as r:
        got = r.read(1 << 20)
    print(f"stream_reader on a truncated frame: NO EXCEPTION, returned {got!r}")
except z.ZstdError as exc:
    print(f"stream_reader on a truncated frame: raises ZstdError ({exc})")

# --- 4. the read_across_frames default that started all this ---------------
with z.ZstdDecompressor().stream_reader(io.BytesIO(two)) as r:
    got = r.read(1 << 20)
verdict = "True" if len(got) == 16 else "FALSE (BUG-010 territory)"
print(f"stream_reader default on 2 frames: read {len(got)} of 16 bytes "
      f"-> read_across_frames default is {verdict}")
