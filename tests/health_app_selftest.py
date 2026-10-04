"""Standard-library-only self-test for the desktop app (module desktop-app, docs/health/07).

Runnable with a bare `python3 tests/health_app_selftest.py` -- no pytest, nothing to
install, under five seconds. It holds `python -m health.app`, the two launchers and the
icon to what ADR-0007 says they are:

  * the server binds 127.0.0.1 only (HREQ-N-05), serves the viewer's files unchanged with
    JavaScript, JSON and HTML media types and no caching, refuses a foreign Host header,
    and answers one request while another connection stalls (it is threaded);
  * the launcher refuses while the desktop-app or reference-v1 flag is off, or the registry
    cannot be read, saying why (HEALTH_MODULES_PATH plants the registry, as the kb suite
    does); when it runs, the disclaimer and the validation status are its last two lines,
    before serving and after stopping (HREQ-S-01); it stops cleanly on SIGINT and SIGTERM,
    when the native window closes (a stand-in `webview` module), and on the Stop dialog
    (a stand-in `osascript`); a failing native window falls back to the browser;
  * Info.plist parses and names an executable and an icon that exist; both launchers are
    executable in git, run `-m health.app` from the repository root with src on the path,
    and say so when no Python is found or the bundle was moved out of the checkout;
  * the .icns holds the eleven PNG renditions at their sizes (IHDR read here with struct),
    is what tools/health_icon.py writes from them, and the 16 and 32 px renders still show
    a head apart from the body; desktop/icon.svg is a black rounded square and a white
    stick figure, and nothing else.

Conventions are those of tests/selftest.py: module-level `test_*` functions in definition
order, `check()` records each assertion, `--no-skips` refuses to exit 0 if anything was
skipped (nothing here can be: there is no third-party import).
"""

from __future__ import annotations

import ast
import contextlib
import http.client
import importlib.util
import inspect
import io
import os
import plistlib
import queue
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import health.app as app  # noqa: E402
from health.registry import ENV_PATH, load_modules, parse_yaml  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIPPED: list[tuple[str, tuple[str, ...]]] = []

BUNDLE = ROOT / "desktop" / "MetabolicMap.app"
BUNDLE_EXE = BUNDLE / "Contents" / "MacOS" / "MetabolicMap"
COMMAND = ROOT / "MetabolicMap.command"
ICNS = BUNDLE / "Contents" / "Resources" / "MetabolicMap.icns"
SVG = ROOT / "desktop" / "icon.svg"
PNG_1024 = ROOT / "desktop" / "icon-1024.png"
FOOTER = [app.DISCLAIMER, app.VALIDATION_STATUS]
EXPECTED_ELEMENTS = [("icp4", 16), ("icp5", 32), ("icp6", 64), ("ic07", 128), ("ic08", 256),
                     ("ic09", 512), ("ic10", 1024), ("ic11", 32), ("ic12", 64), ("ic13", 256),
                     ("ic14", 512)]
TIMEOUT = 10.0


def check(name: str, cond: bool, detail: str = "") -> None:
    suffix = f" -- {detail}" if detail and not cond else ""
    PASS.append(name) if cond else FAIL.append(f"{name}{suffix}")
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"\n        {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _registry(tmp: Path, name: str, **flags: bool | None) -> Path:
    """A copy of the shipped registry with each named module's flag set (None: the entry
    removed), written to tmp/name."""
    text = (ROOT / "config" / "health" / "modules.yaml").read_text(encoding="utf-8")
    for mid, on in flags.items():
        mid = mid.replace("_", "-")
        if on is None:
            text, n = re.subn(rf"  - id: {re.escape(mid)}\n(?:(?!  - id: ).*\n)*", "", text)
        else:
            text, n = re.subn(rf"(  - id: {re.escape(mid)}\n    enabled: )(?:true|false)",
                              rf"\g<1>{'true' if on else 'false'}", text)
        if n != 1:
            raise AssertionError(f"registry plant: {mid} not found exactly once")
    out = tmp / name
    out.write_text(text, encoding="utf-8")
    return out


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in (ENV_PATH, "BROWSER")}
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def _fakes(tmp: Path) -> tuple[Path, Path]:
    """(bin dir, log dir): stand-ins for osascript, a browser and a Python interpreter, each
    recording what it was given in the log dir."""
    fake_bin, log = tmp / "fakebin", tmp / "fakelog"
    if fake_bin.is_dir():
        return fake_bin, log
    fake_bin.mkdir()
    log.mkdir()
    scripts = {
        "osascript": '#!/bin/bash\nprintf \'%s\\0\' "$@" > "$FAKE_LOG/osascript.args"\n'
                     'sleep "${FAKE_OSASCRIPT_DELAY:-0}"\n'
                     'for last; do :; done\necho "button returned:$last"\n',
        "fake-browser": '#!/bin/bash\nprintf \'%s\' "$1" > "$FAKE_LOG/browser.url"\n',
        "fake-python": '#!/bin/bash\n[ "$1" = "-c" ] && exit 0\n'
                       'printf \'%s\\n%s\\n%s\\n\' "$PWD" "$PYTHONPATH" "$*" '
                       '> "$FAKE_LOG/python.call"\n',
    }
    for name, body in scripts.items():
        p = fake_bin / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    return fake_bin, log


def _webview_stub(tmp: Path) -> Path:
    """A stand-in for pywebview's `webview` module: create_window records its arguments;
    start() loads the page, as the window would, and returns, as when it is closed -- or
    raises when FAKE_WEBVIEW=fail."""
    stub = tmp / "webview-stub"
    (stub / "webview").mkdir(parents=True, exist_ok=True)
    (stub / "webview" / "__init__.py").write_text(
        "import http.client, os, urllib.parse\n"
        "_win = {}\n"
        "def create_window(title, url, **kw):\n"
        "    _win.update(title=title, url=url)\n"
        "def start(**kw):\n"
        "    if os.environ.get('FAKE_WEBVIEW') == 'fail':\n"
        "        raise RuntimeError('no GUI backend here')\n"
        "    u = urllib.parse.urlsplit(_win['url'])\n"
        "    c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)\n"
        "    c.request('GET', u.path or '/')\n"
        "    r = c.getresponse(); body = r.read(); c.close()\n"
        "    with open(os.path.join(os.environ['FAKE_LOG'], 'webview.txt'), 'w') as f:\n"
        "        f.write('\\n'.join([_win['title'], _win['url'], str(r.status),\n"
        "                str(b'<title>Metabolic Map</title>' in body)]))\n",
        encoding="utf-8")
    return stub


class _Run:
    """`python -m health.app ARGS` with its stdout read line by line on a thread."""

    def __init__(self, args: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen([sys.executable, "-m", "health.app", *args], cwd=ROOT,
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, text=True, encoding="utf-8")
        self.lines: list[str] = []
        self._q: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._q.put(line.rstrip("\n"))
        self._q.put(None)

    def until(self, predicate: Any, timeout: float = TIMEOUT) -> bool:
        """Read lines until predicate(lines) holds; False on timeout or end of output."""
        end = time.monotonic() + timeout
        while not predicate(self.lines):
            try:
                line = self._q.get(timeout=max(0.0, end - time.monotonic()))
            except queue.Empty:
                return False
            if line is None:
                return predicate(self.lines)
            self.lines.append(line)
        return True

    def finish(self, timeout: float = TIMEOUT) -> tuple[int, str]:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = -999
        self.until(lambda _ls: False, timeout=1.0)
        err = self.proc.stderr.read() if self.proc.stderr else ""
        return code, err

    def url(self) -> str:
        m = next((re.search(r"http://127\.0\.0\.1:\d+/", ln) for ln in self.lines
                  if ln.startswith("serving ")), None)
        return m.group(0) if m else ""


def _serving(lines: list[str]) -> bool:
    return any(ln.startswith("serving ") for ln in lines) and lines[-2:] == FOOTER


def _get(port: int, path: str, host: str | None = None, method: str = "GET"
         ) -> tuple[int, dict[str, str], bytes]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.putrequest(method, path, skip_host=host is not None)
        if host is not None:
            c.putheader("Host", host)
        c.endheaders()
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def _png_ihdr(data: bytes) -> tuple[int, int, int, int]:
    """(width, height, bit depth, colour type), read from the IHDR chunk with struct."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("no PNG signature")
    length, kind = struct.unpack(">I4s", data[8:16])
    if kind != b"IHDR" or length != 13:
        raise ValueError("IHDR is not the first chunk")
    w, h, depth, colour = struct.unpack(">IIBB", data[16:26])
    return w, h, depth, colour


def _png_rgba(data: bytes) -> list[list[tuple[int, int, int, int]]]:
    """Pixels of an 8-bit RGBA PNG (small ones only: pure Python)."""
    w, h, depth, colour = _png_ihdr(data)
    if (depth, colour) != (8, 6):
        raise ValueError(f"not 8-bit RGBA (depth {depth}, colour type {colour})")
    at, idat = 8, b""
    while at < len(data):
        n, kind = struct.unpack(">I4s", data[at:at + 8])
        if kind == b"IDAT":
            idat += data[at + 8:at + 8 + n]
        at += 12 + n
    raw, stride, rows, prev, k = zlib.decompress(idat), w * 4, [], bytearray(w * 4), 0
    for _ in range(h):
        f, line = raw[k], bytearray(raw[k + 1:k + 1 + stride])
        k += 1 + stride
        for x in range(stride):
            a = line[x - 4] if x >= 4 else 0
            b = prev[x]
            c = prev[x - 4] if x >= 4 else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append([tuple(line[i:i + 4]) for i in range(0, stride, 4)])
        prev = line
    return rows


def _icon_tool() -> Any:
    spec = importlib.util.spec_from_file_location("health_icon_probe",
                                                  ROOT / "tools" / "health_icon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_mode(rel: str) -> str:
    try:
        p = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "--", rel],
                           capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git unavailable ({exc})"
    return p.stdout.split(" ", 1)[0] if p.returncode == 0 and p.stdout else \
        f"not tracked by git ({p.stderr.strip()})"


# ---------------------------------------------------------------------------
# Tests: the server
# ---------------------------------------------------------------------------
def test_disclaimer_constants_match_the_config() -> None:
    """HREQ-S-01: the two lines the app prints are config/health/base.yaml's, word for word."""
    cfg = parse_yaml((ROOT / "config" / "health" / "base.yaml").read_text(encoding="utf-8"))
    check("constants: DISCLAIMER == config model.disclaimer",
          app.DISCLAIMER == cfg["model"]["disclaimer"], repr(cfg["model"]["disclaimer"]))
    check("constants: VALIDATION_STATUS == config model.validation_status",
          app.VALIDATION_STATUS == cfg["model"]["validation_status"])
    check("constants: the viewer served is reference/metabolic-map-v1/ (HREQ-P-09)",
          app.VIEWER == ROOT / "reference" / "metabolic-map-v1"
          and (app.VIEWER / "index.html").is_file())


def test_server_binds_loopback_only_and_serves_the_viewer_unchanged() -> None:
    """HREQ-N-05 and HREQ-P-09: 127.0.0.1 only, the viewer's own bytes, the right media
    types, no caching, a foreign Host refused, nothing outside the viewer served, and a
    client that hangs up mid-request leaves no traceback on the terminal."""
    errors = io.StringIO()
    with contextlib.redirect_stderr(errors):
        _serve_and_probe()
    check("server: a client that hangs up leaves no traceback (the footer stays last)",
          "Traceback" not in errors.getvalue(), errors.getvalue()[-600:])
    for host in ("0.0.0.0", "", "192.168.1.10", "::"):
        try:
            app.make_server(0, host=host).server_close()
            ok = False
        except ValueError:
            ok = True
        check(f"server: refuses to listen on {host or '(all interfaces)'!s} (HREQ-N-05)", ok)


def _serve_and_probe() -> None:
    server = app.make_server(0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True)
    t.start()
    try:
        check("server: bound to 127.0.0.1 (IPv4 loopback) on a port the OS chose",
              server.server_address[0] == "127.0.0.1" and port > 0
              and server.socket.family == socket.AF_INET, str(server.server_address))
        wants = [("/", "text/html", "index.html"), ("/index.html", "text/html", "index.html"),
                 ("/app/viewer.js", "text/javascript", "app/viewer.js"),
                 ("/engine/index.js", "text/javascript", "engine/index.js"),
                 ("/app/worker.js", "text/javascript", "app/worker.js"),
                 ("/kb/entities.json", "application/json", "kb/entities.json")]
        for path, mime, rel in wants:
            status, headers, body = _get(port, path)
            check(f"server: GET {path} -> 200 {mime}, the file's own bytes, Cache-Control "
                  "no-store", status == 200 and headers.get("content-type", "").split(";")[0]
                  == mime and body == (app.VIEWER / rel).read_bytes()
                  and headers.get("cache-control") == "no-store",
                  f"{status} {headers.get('content-type')} {headers.get('cache-control')}")
        status, _, _ = _get(port, "/", host=f"localhost:{port}")
        check("server: Host localhost:<port> is answered", status == 200, str(status))
        status, _, _ = _get(port, "/", host=f"rebound.example:{port}")
        check("server: a foreign Host header is refused (403)", status == 403, str(status))
        status, _, _ = _get(port, "/../../config/health/modules.yaml")
        check("server: a path above the viewer is not served", status == 404, str(status))
        status, _, _ = _get(port, "/", method="POST")
        check("server: GET and HEAD only (POST -> 501)", status == 501, str(status))
        stall = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            stall.sendall(b"GET /index.html HTTP/1.1\r\n")      # never finished
            t0 = time.perf_counter()
            status, _, _ = _get(port, "/app/viewer.js")
            took = time.perf_counter() - t0
        finally:
            stall.close()
        check(f"server: threaded -- a request is answered while another connection stalls "
              f"({took * 1000:.0f} ms)", status == 200 and took < 2.0, str(status))
        others = _non_loopback_address()
        refused = []
        for addr in others:
            try:
                socket.create_connection((addr, port), timeout=1).close()
            except OSError:
                refused.append(addr)
        check(f"server: not reachable on this host's non-loopback addresses "
              f"({len(others)} probed)", refused == others, f"answered on {set(others) - set(refused)}")
        abort = socket.create_connection(("127.0.0.1", port), timeout=5)
        abort.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        abort.sendall(f"GET /kb/entities.json HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n"
                      .encode())
        abort.close()                                   # a reset, before the response
        time.sleep(0.1)
    finally:
        server.shutdown()
        server.server_close()


def _non_loopback_address() -> list[str]:
    """This host's outbound IPv4 address, found by routing (a UDP connect sends nothing),
    not by name: resolving a Mac's `.local` host name can stall for seconds. [] offline."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))                 # TEST-NET-1: never answered, never sent
        addr = s.getsockname()[0]
    except OSError:
        return []
    finally:
        s.close()
    return [] if addr.startswith("127.") or addr == "0.0.0.0" else [addr]


def test_media_types_are_explicit() -> None:
    """A module script served as anything but JavaScript is refused by the browser, and the
    system MIME table differs between machines: the handler names them itself."""
    handler = app.ViewerHandler.__new__(app.ViewerHandler)
    for name, want in (("a.js", "text/javascript"), ("a.mjs", "text/javascript"),
                       ("a.json", "application/json"), ("a.svg", "image/svg+xml"),
                       ("a.html", "text/html"), ("A.JS", "text/javascript")):
        got = handler.guess_type(name)
        check(f"media type: {name} -> {want}", got.split(";")[0] == want, got)


# ---------------------------------------------------------------------------
# Tests: the launcher program
# ---------------------------------------------------------------------------
def test_launcher_refuses_while_a_flag_is_off(tmp: Path) -> None:
    """HREQ-X-01: the flag is the surface switch. Off -> no server, the reason, the enable
    recipe, and the footer last; a registry that cannot be read fails closed."""
    cases = [
        ("desktop-app off", _registry(tmp, "off.yaml", desktop_app=False),
         ["desktop-app (this app) is disabled in", "enabled: true", "--write-07",
          "tools/gates.py"]),
        ("reference-v1 off", _registry(tmp, "ref-off.yaml", desktop_app=True,
                                       reference_v1=False),
         ["reference-v1 (the viewer this app serves) is disabled in", "enabled: true"]),
        ("desktop-app not registered", _registry(tmp, "gone.yaml", desktop_app=None),
         ["desktop-app (this app) is not registered in"]),
        ("the registry missing", tmp / "no-such-registry.yaml",
         ["the module registry cannot be read (FileNotFoundError", "fails closed"]),
    ]
    for label, path, wants in cases:
        r = subprocess.run([sys.executable, "-m", "health.app", "--no-open"], cwd=ROOT,
                           env=_env(**{ENV_PATH: str(path)}), capture_output=True, text=True,
                           encoding="utf-8", timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        lines = r.stdout.splitlines()
        check(f"refusal ({label}): exit 1, nothing served, the reason, and the footer last",
              r.returncode == 1 and not any(ln.startswith("serving ") for ln in lines)
              and all(w in r.stdout for w in wants) and lines[-2:] == FOOTER,
              f"exit {r.returncode}\n{r.stdout}{r.stderr[-300:]}")


def test_launcher_serves_and_stops_cleanly_on_a_signal(tmp: Path) -> None:
    """HREQ-S-01: the address, then the disclaimer and the validation status as the last two
    lines before serving; Ctrl-C (SIGINT) and SIGTERM stop it, exit 0, footer last again."""
    reg = _registry(tmp, "on.yaml", desktop_app=True, reference_v1=True)
    for sig in (signal.SIGINT, signal.SIGTERM):
        run = _Run(["--no-open"], _env(**{ENV_PATH: str(reg)}))
        up = run.until(_serving)
        url = run.url()
        before = list(run.lines)
        status = _get(int(url.rsplit(":", 1)[1].strip("/")), "/")[0] if url else 0
        check(f"run ({sig.name}): prints the loopback address, the mode, then the disclaimer "
              "and the validation status as the last two lines; the page answers",
              up and bool(url) and before[-2:] == FOOTER and status == 200
              and any("--no-open" in ln for ln in before), "\n".join(before))
        run.proc.send_signal(sig)
        code, err = run.finish()
        after = run.lines[len(before):]
        name = "Ctrl-C" if sig == signal.SIGINT else "SIGTERM"
        check(f"run ({sig.name}): stops cleanly -- exit 0, `stopped ({name})`, footer last",
              code == 0 and after[:1] == [f"{app.TITLE} stopped ({name})."]
              and after[-2:] == FOOTER and "Traceback" not in err,
              f"exit {code}\n" + "\n".join(after) + err[-400:])


def test_native_window_when_pywebview_is_importable(tmp: Path) -> None:
    """The optional window: titled "Metabolic Map", on the loopback page, and closing it
    stops the server. A window that cannot open falls back to the browser."""
    reg = _registry(tmp, "on-window.yaml", desktop_app=True, reference_v1=True)
    fake_bin, log = _fakes(tmp)
    stub = _webview_stub(tmp)
    env = _env(**{ENV_PATH: str(reg), "FAKE_LOG": str(log),
                  "PYTHONPATH": f"{stub}{os.pathsep}{ROOT / 'src'}",
                  "BROWSER": str(fake_bin / "fake-browser")})
    run = _Run([], env)
    code, err = run.finish()
    rec = (log / "webview.txt").read_text().splitlines() if (log / "webview.txt").exists() else []
    check("window: create_window got the title \"Metabolic Map\" and the loopback address, "
          "and the page loaded in it",
          rec[:1] == ["Metabolic Map"] and len(rec) == 4
          and re.fullmatch(r"http://127\.0\.0\.1:\d+/", rec[1]) is not None
          and rec[2:] == ["200", "True"], f"{rec}\n{err[-300:]}")
    check("window: closing it stops the server -- exit 0, `stopped (window closed)`, footer "
          "last, no browser opened",
          code == 0 and f"{app.TITLE} stopped (window closed)." in run.lines
          and run.lines[-2:] == FOOTER and not (log / "browser.url").exists(),
          f"exit {code}\n" + "\n".join(run.lines) + err[-300:])

    run = _Run([], {**env, "FAKE_WEBVIEW": "fail"})
    fell_back = run.until(lambda ls: any("opening your default browser instead" in ln
                                         for ln in ls) and ls[-2:] == FOOTER)
    for _ in range(50):
        if (log / "browser.url").exists():
            break
        time.sleep(0.02)
    opened = (log / "browser.url").read_text() if (log / "browser.url").exists() else ""
    run.proc.send_signal(signal.SIGINT)
    code, err = run.finish()
    check("window: one that cannot open falls back to the browser, which gets the address; "
          "Ctrl-C still stops it cleanly",
          fell_back and opened == run.url() and code == 0 and run.lines[-2:] == FOOTER,
          f"exit {code} opened {opened!r}\n" + "\n".join(run.lines) + err[-300:])


def test_browser_with_the_stop_dialog(tmp: Path) -> None:
    """--dialogs (the .app has no terminal): the browser gets the address, a dialog shows
    it with the disclaimer and the validation status last and a Stop button, and the click
    stops the server."""
    reg = _registry(tmp, "on-dialog.yaml", desktop_app=True, reference_v1=True)
    fake_bin, log = _fakes(tmp)
    for f in ("browser.url", "osascript.args"):
        (log / f).unlink(missing_ok=True)
    env = _env(**{ENV_PATH: str(reg), "FAKE_LOG": str(log), "FAKE_OSASCRIPT_DELAY": "0.3",
                  "BROWSER": str(fake_bin / "fake-browser"),
                  "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"})
    run = _Run(["--browser", "--dialogs"], env)
    code, err = run.finish()
    args = (log / "osascript.args").read_bytes().decode().split("\0")[:-1] \
        if (log / "osascript.args").exists() else []
    text = args[-3] if len(args) >= 3 else ""
    check("dialog: the browser was given the loopback address",
          (log / "browser.url").exists() and (log / "browser.url").read_text() == run.url()
          and bool(run.url()), run.url())
    check("dialog: osascript shows the address, then the disclaimer and the validation status "
          "as its last two lines, titled Metabolic Map, with one Stop button",
          run.url() in text and text.splitlines()[-2:] == FOOTER
          and args[-2:] == [app.TITLE, app.STOP_BUTTON], str(args))
    check("dialog: the Stop click stops the server -- exit 0, `stopped (Stop clicked)`, footer "
          "last", code == 0 and f"{app.TITLE} stopped (Stop clicked)." in run.lines
          and run.lines[-2:] == FOOTER, f"exit {code}\n" + "\n".join(run.lines) + err[-300:])

    (log / "osascript.args").unlink(missing_ok=True)
    off = _registry(tmp, "off-dialog.yaml", desktop_app=False)
    r = subprocess.run([sys.executable, "-m", "health.app", "--dialogs"], cwd=ROOT,
                       env={**env, ENV_PATH: str(off)}, capture_output=True, text=True,
                       encoding="utf-8", timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    args = (log / "osascript.args").read_bytes().decode().split("\0")[:-1] \
        if (log / "osascript.args").exists() else []
    text = args[-3] if len(args) >= 3 else ""
    check("dialog: a refusal is shown as a dialog too, the reason first and the footer last",
          r.returncode == 1 and text.startswith(f"{app.TITLE}: not started -- desktop-app")
          and text.splitlines()[-2:] == FOOTER and args[-1:] == ["OK"], str(args))


# ---------------------------------------------------------------------------
# Tests: the bundle and the launchers
# ---------------------------------------------------------------------------
def test_bundle_plist_names_an_executable_and_an_icon_that_exist() -> None:
    with (BUNDLE / "Contents" / "Info.plist").open("rb") as f:
        plist = plistlib.load(f)
    want = {"CFBundleName": "Metabolic Map", "CFBundleIdentifier": "org.navanax.metabolic-map",
            "CFBundleExecutable": "MetabolicMap", "CFBundleIconFile": "MetabolicMap",
            "CFBundlePackageType": "APPL", "NSHighResolutionCapable": True}
    check("plist: parses, with the bundle's name, identifier, executable, icon, type and "
          "high-resolution flag", all(plist.get(k) == v for k, v in want.items()),
          str({k: plist.get(k) for k in want}))
    check("plist: LSMinimumSystemVersion is a version number",
          re.fullmatch(r"\d+\.\d+(\.\d+)?", str(plist.get("LSMinimumSystemVersion", "")))
          is not None, str(plist.get("LSMinimumSystemVersion")))
    exe = BUNDLE / "Contents" / "MacOS" / str(plist.get("CFBundleExecutable"))
    icon = BUNDLE / "Contents" / "Resources" / f"{plist.get('CFBundleIconFile')}.icns"
    check("plist: the executable it names exists and is executable",
          exe.is_file() and os.access(exe, os.X_OK), str(exe))
    check("plist: the icon it names exists", icon.is_file(), str(icon))
    check("plist: Get Info carries the disclaimer and the validation status (HREQ-S-01)",
          plist.get("NSHumanReadableCopyright") == " ".join(FOOTER))


def test_launchers_are_executable_in_git_and_run_the_app(tmp: Path) -> None:
    """Both launchers keep mode 755 in git (a checkout must double-click), parse as bash,
    run `-m health.app` from the repository root with src on the path, and say so when no
    Python is found; the bundle refuses to run once moved out of the checkout."""
    fake_bin, log = _fakes(tmp)
    path = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    for script in (BUNDLE_EXE, COMMAND):
        rel = script.relative_to(ROOT).as_posix()
        check(f"launcher {rel}: mode 100755 in git", _git_mode(rel) == "100755", _git_mode(rel))
        text = script.read_text(encoding="utf-8")
        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True,
                                check=False)
        check(f"launcher {rel}: #!/bin/bash, parses (bash -n), runs -m health.app, which "
              "exists", text.startswith("#!/bin/bash\n") and syntax.returncode == 0
              and "-m health.app" in text and (ROOT / "src" / "health" / "app.py").is_file(),
              syntax.stderr)

    home = tmp / "home"
    home.mkdir(exist_ok=True)
    (log / "python.call").unlink(missing_ok=True)
    env = {**os.environ, "HOME": str(home), "PATH": path, "FAKE_LOG": str(log),
           "METABOLIC_MAP_PYTHON": str(fake_bin / "fake-python")}
    env.pop("PYTHONPATH", None)
    r = subprocess.run([str(BUNDLE_EXE)], env=env, capture_output=True, text=True,
                       timeout=TIMEOUT, stdin=subprocess.DEVNULL, check=False)
    call = (log / "python.call").read_text().splitlines() if (log / "python.call").exists() else []
    check("bundle: runs `-m health.app --dialogs` from the repository root (four levels up "
          "from Contents/MacOS) with src first on PYTHONPATH, logging to "
          "~/Library/Logs/MetabolicMap.log",
          r.returncode == 0 and call == [str(ROOT), str(ROOT / "src"), "-m health.app --dialogs"]
          and (home / "Library" / "Logs" / "MetabolicMap.log").is_file(),
          f"exit {r.returncode} {call} {r.stderr[-300:]}")

    (log / "python.call").unlink(missing_ok=True)
    r = subprocess.run([str(COMMAND)], env=env, capture_output=True, text=True,
                       timeout=TIMEOUT, stdin=subprocess.DEVNULL, check=False)
    call = (log / "python.call").read_text().splitlines() if (log / "python.call").exists() else []
    check("command: runs `-m health.app` from the repository root with PYTHONPATH=src",
          r.returncode == 0 and call == [str(ROOT), "src", "-m health.app"],
          f"exit {r.returncode} {call} {r.stderr[-300:]}")

    (log / "osascript.args").unlink(missing_ok=True)
    missing = {**env, "METABOLIC_MAP_PYTHON": str(tmp / "no-such-python")}
    r = subprocess.run([str(BUNDLE_EXE)], env=missing, capture_output=True, text=True,
                       timeout=TIMEOUT, stdin=subprocess.DEVNULL, check=False)
    said = (log / "osascript.args").read_bytes().decode() if (log / "osascript.args").exists() else ""
    check("bundle: no usable Python -> a macOS dialog (osascript) says Python 3.10 or newer "
          "is needed, exit 1", r.returncode == 1 and "needs Python 3.10 or newer" in said,
          f"exit {r.returncode} {said[-200:]!r}")
    r = subprocess.run([str(COMMAND)], env=missing, capture_output=True, text=True,
                       timeout=TIMEOUT, stdin=subprocess.DEVNULL, check=False)
    check("command: no usable Python -> says so in the terminal, exit 1",
          r.returncode == 1 and "No Python 3.10+ found" in r.stdout, r.stdout[-200:])

    (log / "osascript.args").unlink(missing_ok=True)
    moved = tmp / "Applications" / "MetabolicMap.app" / "Contents" / "MacOS"
    moved.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUNDLE_EXE, moved / "MetabolicMap")
    r = subprocess.run([str(moved / "MetabolicMap")], env=env, capture_output=True, text=True,
                       timeout=TIMEOUT, stdin=subprocess.DEVNULL, check=False)
    said = (log / "osascript.args").read_bytes().decode() if (log / "osascript.args").exists() else ""
    check("bundle: moved out of the checkout -> a dialog says it has to stay in "
          "desktop/, exit 1, Python never run",
          r.returncode == 1 and "has to stay at desktop/MetabolicMap.app" in said,
          f"exit {r.returncode} {said[-200:]!r}")


# ---------------------------------------------------------------------------
# Tests: the icon
# ---------------------------------------------------------------------------
def test_icns_holds_every_size_as_a_png() -> None:
    tool = _icon_tool()
    data = ICNS.read_bytes()
    elements = tool.read_icns(data)
    kinds = [k for k, _ in elements]
    check("icns: header + TOC first, then the eleven PNG types in order",
          data[:4] == b"icns" and kinds == ["TOC "] + [k for k, _ in EXPECTED_ELEMENTS],
          str(kinds))
    sizes = {k: s for k, s in EXPECTED_ELEMENTS}
    bad = []
    for kind, payload in elements[1:]:
        try:
            w, h, depth, colour = _png_ihdr(payload)
        except ValueError as exc:
            bad.append(f"{kind}: {exc}")
            continue
        if (w, h, depth, colour) != (sizes.get(kind), sizes.get(kind), 8, 6):
            bad.append(f"{kind}: {w}x{h} depth {depth} colour {colour}")
    check("icns: every element is an 8-bit RGBA PNG of its type's size (IHDR)", not bad,
          "; ".join(bad))
    png = PNG_1024.read_bytes()
    check("icns: desktop/icon-1024.png is 1024x1024 and is the ic10 element, byte for byte",
          _png_ihdr(png)[:2] == (1024, 1024) and dict(elements).get("ic10") == png)
    check("icns: tools/health_icon.py writes this file from these PNGs, byte for byte",
          tool.write_icns(tool.pngs_of(elements)) == data)
    try:
        tool.read_icns(data[:-1])
        truncated = False
    except tool.IcnsError:
        truncated = True
    lied = bytearray(data)
    lied[16:20] = b"ic99"                      # the TOC's first entry renamed
    try:
        tool.read_icns(bytes(lied))
        toc_checked = False
    except tool.IcnsError:
        toc_checked = True
    check("icns: the reader refuses a truncated file and a TOC that does not match",
          truncated and toc_checked)
    for size in (16, 32):
        px = _png_rgba(tool.pngs_of(elements)[size])
        mid = [(px[y][size // 2 - 1][0] + px[y][size // 2][0]) / 2 for y in range(size)]
        corners = [px[0][0][3], px[0][-1][3], px[-1][0][3], px[-1][-1][3]]
        lit = [y for y, v in enumerate(mid) if v > 128]
        head_gap = any(mid[y] < min(max(mid[:y]), max(mid[y + 1:])) - 40
                       for y in range(lit[0] + 1, lit[-1])) if lit else False
        check(f"icon at {size} px: transparent corners, a figure down the middle, its head "
              "apart from its body", corners == [0, 0, 0, 0] and len(lit) >= size // 3
              and head_gap, f"corners {corners} column {[round(v) for v in mid]}")


def test_svg_is_a_black_rounded_square_and_a_white_stick_figure() -> None:
    ns = "{http://www.w3.org/2000/svg}"
    root = ET.parse(SVG).getroot()
    tags = [el.tag.replace(ns, "") for el in root.iter()]
    check("svg: 1024 x 1024, and only svg, rect, g, line and circle elements",
          root.get("viewBox") == "0 0 1024 1024" and root.get("width") == "1024"
          and root.get("height") == "1024"
          and set(tags) == {"svg", "rect", "g", "line", "circle"}, str(tags))
    rects = root.findall(f"{ns}rect")
    rect = rects[0] if len(rects) == 1 else None

    def f(el: ET.Element, k: str) -> float:
        return float(el.get(k, "nan"))

    if rect is not None:
        side, rx = f(rect, "width"), f(rect, "rx")
        check("svg: one #000000 rounded square, corner radius about 22 % (macOS), centred",
              rect.get("fill", "").lower() == "#000000" and side == f(rect, "height")
              and 0.20 <= rx / side <= 0.25 and f(rect, "x") + side / 2 == 512
              and f(rect, "y") + side / 2 == 512, f"side {side} rx {rx}")
    else:
        check("svg: exactly one rect (the background)", False, str(len(rects)))
        return
    groups = root.findall(f"{ns}g")
    lines = groups[0].findall(f"{ns}line") if len(groups) == 1 else []
    circles = root.findall(f".//{ns}circle")
    g = groups[0] if groups else ET.Element("g")
    check("svg: the figure is five white round-capped strokes and one white circle",
          len(groups) == 1 and len(lines) == 5 and len(list(g)) == 5
          and g.get("stroke", "").lower() == "#ffffff" and g.get("stroke-linecap") == "round"
          and len(circles) == 1 and circles[0].get("fill", "").lower() == "#ffffff",
          f"{len(groups)} g, {len(lines)} lines, {len(circles)} circles")
    if len(lines) != 5 or len(circles) != 1:
        return
    seg = [(f(ln, "x1"), f(ln, "y1"), f(ln, "x2"), f(ln, "y2")) for ln in lines]
    torso = next((s for s in seg if s[0] == s[2]), None)
    head = circles[0]
    cx, cy, r = f(head, "cx"), f(head, "cy"), f(head, "r")
    w = float(g.get("stroke-width", "0"))
    if torso is None:
        check("svg: a vertical torso", False, str(seg))
        return
    top, hip = min(torso[1], torso[3]), max(torso[1], torso[3])
    limbs = [s for s in seg if s is not torso]
    arms = [s for s in limbs if top <= s[1] < top + 0.25 * (hip - top)]
    legs = [s for s in limbs if s[1] == hip]
    feet = max(max(s[1], s[3]) for s in seg) + w / 2
    height = feet - (cy - r)
    check("svg: head on the torso's axis and above it; arms from the shoulders, legs from "
          "the hip, one of each to either side, mirror images",
          cx == torso[0] == 512 and cy + r < top - w / 2 and len(arms) == 2 and len(legs) == 2
          and sorted(s[2] - 512 for s in arms) == sorted(512 - s[2] for s in arms)
          and sorted(s[2] - 512 for s in legs) == sorted(512 - s[2] for s in legs)
          and all(s[3] > s[1] for s in limbs), str(seg))
    check(f"svg: proportioned as a figure -- head {2 * r / height:.3f} of the height (about "
          "one seventh), the hip near half-way", 1 / 8 <= 2 * r / height <= 1 / 5.5
          and 0.4 <= (hip - (cy - r)) / height <= 0.6, f"height {height}")
    x0, y0 = f(rect, "x"), f(rect, "y")
    side = f(rect, "width")
    left = min(min(s[0], s[2]) for s in seg) - w / 2
    right = max(max(s[0], s[2]) for s in seg) + w / 2
    margin = min(left - x0, x0 + side - right, cy - r - y0, y0 + side - feet)
    check(f"svg: the figure is centred in the square with a margin ({margin:.0f} px)",
          margin >= 0.08 * side and abs((cy - r + feet) / 2 - 512) <= 0.05 * side
          and abs((left + right) / 2 - 512) < 1)


# ---------------------------------------------------------------------------
# Tests: the module
# ---------------------------------------------------------------------------
def test_the_app_imports_only_the_standard_library_and_the_registry() -> None:
    """HREQ-N-02: nothing to install for the default path. pywebview is imported only
    inside the function that opens the optional window."""
    tree = ast.parse((ROOT / "src" / "health" / "app.py").read_text(encoding="utf-8"))
    std = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    top, nested = [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) \
                else [node.module or ""]
            # health.* by its full name; anything else by its top-level package
            names = [n if n.startswith("health") else n.split(".")[0] for n in names]
            (top if node.col_offset == 0 else nested).extend(names)
    check("imports: module level is the standard library only",
          bool(std) and all(n in std for n in top), str(top))
    check("imports: inside functions, only health.registry and the optional webview",
          set(nested) <= {"health.registry", "webview"} and "webview" in nested, str(nested))


def test_the_registry_entry_names_every_piece() -> None:
    """The entry's paths, tests and recipe match what is on disk, so the recipe removes all
    of it (HREQ-X-02); the gate and CI steps it deletes exist under those names."""
    mod = next((m for m in load_modules() if m.get("id") == app.MODULE_ID), {})
    me = Path(__file__).resolve().relative_to(ROOT).as_posix()
    paths = set(mod.get("paths", []))
    check("registry: desktop-app's paths are the app, the bundle and icon folder, the "
          ".command, the icon tool and this suite",
          paths == {"src/health/app.py", "desktop/", "MetabolicMap.command",
                    "tools/health_icon.py", me}, str(sorted(paths)))
    check("registry: depends on reference-v1 (the viewer it serves) and process (the "
          "registry it reads), tested by this suite, ADR-0007 exists",
          mod.get("depends_on") == ["reference-v1", "process"] and mod.get("tests") == [me]
          and any((ROOT / "docs" / "health" / "decisions").glob(f"{mod.get('adr')}-*.md")),
          str({k: mod.get(k) for k in ("depends_on", "tests", "adr")}))
    removal = str(mod.get("removal", ""))
    gates = (ROOT / "tools" / "gates.py").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    row = '("health app selftest", "tests/health_app_selftest.py")'
    steps = ["Health desktop app self-test (stdlib only)", "Health desktop app self-test (strict)"]
    flat = " ".join(removal.split())
    check("registry: the gate row and the two CI steps the recipe deletes exist by those names",
          row in gates and row in flat and all(f"- name: {s}" in ci for s in steps)
          and all(s in flat for s in steps),
          f"gates row {row in gates}, CI steps {[f'- name: {s}' in ci for s in steps]}")


# ---------------------------------------------------------------------------
# Runner (same conventions as tests/selftest.py)
# ---------------------------------------------------------------------------
def discover() -> list[tuple[str, object]]:
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    tests.sort(key=lambda nf: inspect.getsourcelines(nf[1])[1])
    return tests


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-skips", action="store_true",
                    help="fail if ANY test was skipped (none can be: stdlib only)")
    a = ap.parse_args(argv)
    print("=" * 72)
    print("HEALTH DESKTOP APP SELF-TEST  (stdlib only)" + ("  [--no-skips]" if a.no_skips else ""))
    print("=" * 72)
    t0 = time.perf_counter()
    tests = discover()
    tmp = Path(tempfile.mkdtemp(prefix="health-app-selftest-"))
    try:
        for name, fn in tests:
            print(f"\n--- {name} ---")
            try:
                if inspect.signature(fn).parameters:
                    fn(tmp)
                else:
                    fn()
            except Exception as exc:  # noqa: BLE001 - a crashing test is a FAIL, not an abort
                check(f"{name} ran to completion", False, repr(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    wall = time.perf_counter() - t0
    check(f"the suite runs in under 5 s ({wall:.2f} s)", wall < 5.0)
    print("\n" + "=" * 72)
    print(f"{len(tests)} test functions, {len(PASS)} passed, {len(FAIL)} failed, "
          f"{len(SKIPPED)} skipped")
    print(f"wall time {wall:.2f} s")
    if FAIL:
        print("\nFAILURES:")
        for f in FAIL:
            print("  " + f)
    print("=" * 72)
    if FAIL:
        return 1
    return 2 if a.no_skips and SKIPPED else 0


if __name__ == "__main__":
    raise SystemExit(main())
