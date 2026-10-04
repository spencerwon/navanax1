#!/usr/bin/env python3
"""The desktop app's icon: render desktop/icon.svg, pack the macOS .icns, read one back.

    python3 tools/health_icon.py build [--pngs DIR] [--chromium PATH] [--node PATH]
    python3 tools/health_icon.py read [FILE.icns]
    python3 tools/health_icon.py iconset [--out DIR] [--iconutil]

build    renders desktop/icon.svg at 16, 32, 64, 128, 256, 512 and 1024 px with headless
         Chromium driven by Playwright for Node (needed for this step only: the results are
         committed), writes desktop/icon-1024.png, and packs every size into
         desktop/MetabolicMap.app/Contents/Resources/MetabolicMap.icns with the
         standard-library writer below. `--pngs DIR` packs icon-<size>.png files rendered
         some other way instead, with nothing to install. The Chromium is --chromium, else
         $HEALTH_ICON_CHROMIUM, else /opt/pw-browsers/chromium when it exists, else the one
         Playwright installed for itself.
read     prints an .icns file's elements: type, the size the type stands for, the PNG's own
         width and height, and bytes. Default: the committed bundle icon.
iconset  writes the folder Apple's `iconutil` reads (icon_16x16.png ... icon_512x512@2x.png)
         from the PNGs inside the committed .icns. On a Mac, `iconutil -c icns DIR -o FILE`
         then rebuilds the .icns with Apple's own tool; `--iconutil` runs it.

The ICNS format: an 8-byte header -- the type `icns` and the file's total length, a
big-endian 32-bit integer -- then elements, each a 4-byte type, a 4-byte length that counts
its own 8-byte header, and the data. The first element, `TOC `, lists every other
element's type and length. Since macOS 10.7 each of the types below holds a PNG; the @2x
types are the Retina renditions, so one PNG can appear under two types (32 px is both 32x32
and 16x16@2x).
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
SVG = DESKTOP / "icon.svg"
PNG_1024 = DESKTOP / "icon-1024.png"
ICNS = DESKTOP / "MetabolicMap.app" / "Contents" / "Resources" / "MetabolicMap.icns"
CHROMIUM_DEFAULT = Path("/opt/pw-browsers/chromium")
SIZES = (16, 32, 64, 128, 256, 512, 1024)
#: (type, pixel size, what it is) in the order they are written; the TOC lists them so.
ELEMENTS: tuple[tuple[str, int, str], ...] = (
    ("icp4", 16, "16x16"),
    ("icp5", 32, "32x32"),
    ("icp6", 64, "64x64"),
    ("ic07", 128, "128x128"),
    ("ic08", 256, "256x256"),
    ("ic09", 512, "512x512"),
    ("ic10", 1024, "512x512@2x"),
    ("ic11", 32, "16x16@2x"),
    ("ic12", 64, "32x32@2x"),
    ("ic13", 256, "128x128@2x"),
    ("ic14", 512, "256x256@2x"),
)
#: iconutil's file names, each with the pixel size of its PNG.
ICONSET: tuple[tuple[str, int], ...] = (
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class IcnsError(ValueError):
    """Not an .icns file this reader understands, or one whose table of contents lies."""


# ---------------------------------------------------------------------------
# PNG and ICNS, standard library only
# ---------------------------------------------------------------------------
def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a PNG's IHDR chunk; ValueError when `data` is not a PNG."""
    if data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError("not a PNG (no signature, or IHDR is not the first chunk)")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def write_icns(pngs: dict[int, bytes]) -> bytes:
    """An .icns holding every type of ELEMENTS, each with the PNG of its pixel size."""
    elements = []
    for kind, size, _ in ELEMENTS:
        data = pngs[size]
        if png_size(data) != (size, size):
            raise ValueError(f"{kind} needs a {size}x{size} PNG, got {png_size(data)}")
        elements.append((kind.encode("ascii"), data))
    toc = b"".join(kind + struct.pack(">I", 8 + len(data)) for kind, data in elements)
    body = b"TOC " + struct.pack(">I", 8 + len(toc)) + toc
    body += b"".join(kind + struct.pack(">I", 8 + len(data)) + data for kind, data in elements)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def read_icns(data: bytes) -> list[tuple[str, bytes]]:
    """Every element of an .icns as (type, data), in file order, the TOC included. Raises
    IcnsError on a bad header, a length that runs past the end, or a TOC that does not
    list exactly the elements that follow it."""
    if data[:4] != b"icns" or len(data) < 8:
        raise IcnsError("not an .icns file (no 'icns' header)")
    total = struct.unpack(">I", data[4:8])[0]
    if total != len(data):
        raise IcnsError(f"header says {total} bytes, the file has {len(data)}")
    out: list[tuple[str, bytes]] = []
    at = 8
    while at < total:
        if at + 8 > total:
            raise IcnsError(f"truncated element header at byte {at}")
        kind = data[at:at + 4].decode("latin-1")
        length = struct.unpack(">I", data[at + 4:at + 8])[0]
        if length < 8 or at + length > total:
            raise IcnsError(f"element {kind!r} at byte {at}: length {length} is impossible")
        out.append((kind, data[at + 8:at + length]))
        at += length
    tocs = [d for k, d in out if k == "TOC "]
    if tocs:
        toc = tocs[0]
        listed = [(toc[i:i + 4].decode("latin-1"), struct.unpack(">I", toc[i + 4:i + 8])[0])
                  for i in range(0, len(toc) - len(toc) % 8, 8)]
        actual = [(k, len(d) + 8) for k, d in out if k != "TOC "]
        if len(toc) % 8 or listed != actual:
            raise IcnsError(f"the TOC lists {listed}, the file holds {actual}")
    return out


def pngs_of(elements: list[tuple[str, bytes]]) -> dict[int, bytes]:
    """{pixel size: PNG} from read_icns' elements (one PNG per size)."""
    sizes = {kind: size for kind, size, _ in ELEMENTS}
    return {sizes[k]: d for k, d in elements if k in sizes}


# ---------------------------------------------------------------------------
# Rendering (headless Chromium through Playwright for Node; optional)
# ---------------------------------------------------------------------------
RENDER_JS = r"""
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const [svgPath, outDir, executablePath, ...sizes] = process.argv.slice(1);
  const uri = 'data:image/svg+xml;base64,' + fs.readFileSync(svgPath).toString('base64');
  const browser = await chromium.launch(executablePath ? { executablePath } : {});
  const page = await browser.newPage({ viewport: { width: 1024, height: 1024 }, deviceScaleFactor: 1 });
  for (const s of sizes.map(Number)) {
    await page.setContent(`<html><body style="margin:0;background:transparent"><img id="i" src="${uri}" width="${s}" height="${s}" style="display:block"></body></html>`);
    await page.waitForFunction(() => document.getElementById('i').complete);
    await page.screenshot({ path: `${outDir}/icon-${s}.png`, omitBackground: true,
                            clip: { x: 0, y: 0, width: s, height: s } });
  }
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _node_path(node: str) -> str:
    """Where a globally installed `playwright` package lives: $NODE_PATH, else `npm root
    -g`, else <node's prefix>/lib/node_modules."""
    if os.environ.get("NODE_PATH"):
        return os.environ["NODE_PATH"]
    npm = shutil.which("npm") or str(Path(node).with_name("npm"))
    try:
        p = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=60,
                           check=False)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path(node).resolve().parents[1] / "lib" / "node_modules")


def render(svg: Path, out_dir: Path, chromium: str | None = None,
           node: str | None = None) -> dict[int, bytes]:
    """Render `svg` at every size in SIZES into out_dir/icon-<size>.png; returns them."""
    node = node or shutil.which("node") or "/opt/node22/bin/node"
    if chromium is None:
        chromium = os.environ.get("HEALTH_ICON_CHROMIUM") or (
            str(CHROMIUM_DEFAULT) if CHROMIUM_DEFAULT.exists() else "")
    env = {**os.environ, "NODE_PATH": _node_path(node), "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}
    cmd = [node, "-e", RENDER_JS, str(svg), str(out_dir), chromium, *map(str, SIZES)]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"rendering failed (exit {p.returncode}): {p.stderr.strip()[-800:]}")
    return load_pngs(out_dir)


def load_pngs(directory: Path) -> dict[int, bytes]:
    """icon-<size>.png for every size in SIZES, each checked to be size x size."""
    out = {}
    for size in SIZES:
        data = (directory / f"icon-{size}.png").read_bytes()
        if png_size(data) != (size, size):
            raise ValueError(f"icon-{size}.png is {png_size(data)}, not {size}x{size}")
        out[size] = data
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_build(a: argparse.Namespace) -> int:
    if a.pngs:
        pngs = load_pngs(Path(a.pngs))
    else:
        with tempfile.TemporaryDirectory(prefix="health-icon-") as tmp:
            pngs = render(SVG, Path(tmp), a.chromium, a.node)
    PNG_1024.write_bytes(pngs[1024])
    ICNS.parent.mkdir(parents=True, exist_ok=True)
    ICNS.write_bytes(write_icns(pngs))
    print(f"wrote {PNG_1024.relative_to(ROOT)} and {ICNS.relative_to(ROOT)} "
          f"({ICNS.stat().st_size} bytes)")
    return cmd_read(argparse.Namespace(file=str(ICNS)))


def cmd_read(a: argparse.Namespace) -> int:
    path = Path(a.file)
    elements = read_icns(path.read_bytes())
    what = {kind: (size, label) for kind, size, label in ELEMENTS}
    print(f"{path}: {len(elements)} elements")
    for kind, data in elements:
        if kind == "TOC ":
            print(f"  {kind}  table of contents, {len(data) // 8} entries")
            continue
        size, label = what.get(kind, (None, "unknown type"))
        try:
            w, h = png_size(data)
            png = f"PNG {w}x{h}"
        except ValueError:
            png = "not a PNG"
        print(f"  {kind}  {label:<11} {png:<14} {len(data):>7} bytes")
    return 0


def cmd_iconset(a: argparse.Namespace) -> int:
    out = Path(a.out)
    pngs = pngs_of(read_icns(ICNS.read_bytes()))
    out.mkdir(parents=True, exist_ok=True)
    for name, size in ICONSET:
        (out / name).write_bytes(pngs[size])
    print(f"wrote {out} ({len(ICONSET)} PNGs)")
    if a.iconutil:
        exe = shutil.which("iconutil")
        if exe is None:
            print("iconutil is not here (it ships with macOS); run on a Mac:\n"
                  f"  iconutil -c icns {out} -o {ICNS}")
            return 1
        subprocess.run([exe, "-c", "icns", str(out), "-o", str(ICNS)], check=True)
        print(f"iconutil rebuilt {ICNS}")
    else:
        print(f"on a Mac: iconutil -c icns {out} -o {ICNS}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="render the SVG, write the 1024 px PNG and the .icns")
    b.add_argument("--pngs", help="pack DIR/icon-<size>.png instead of rendering")
    b.add_argument("--chromium", help="the Chromium executable Playwright drives")
    b.add_argument("--node", help="the node binary (default: node on PATH)")
    r = sub.add_parser("read", help="list an .icns file's elements")
    r.add_argument("file", nargs="?", default=str(ICNS))
    i = sub.add_parser("iconset", help="write an iconutil .iconset from the committed .icns")
    i.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "MetabolicMap.iconset"))
    i.add_argument("--iconutil", action="store_true",
                   help="then rebuild the .icns with iconutil (macOS)")
    a = ap.parse_args(argv)
    return {"build": cmd_build, "read": cmd_read, "iconset": cmd_iconset}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
