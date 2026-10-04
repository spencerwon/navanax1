"""The Metabolic Map viewer as a desktop app (stdlib only).

    python3 -m health.app [--port N] [--no-open] [--browser] [--dialogs] [--verbose]

Double-click `desktop/MetabolicMap.app` or `MetabolicMap.command` (both run this). It
serves the V1 viewer, `reference/metabolic-map-v1/`, unchanged (HREQ-P-09), from a
threaded `http.server` bound to 127.0.0.1 only (HREQ-N-05) on a port the operating system
chooses (or `--port`), prints the address, then the disclaimer and the validation status
as the last two lines -- as every message it prints ends (HREQ-S-01: the terminal is a
surface; request logging, off unless --verbose, goes to stderr) -- and opens the page:

  * in a native window titled "Metabolic Map" when the optional `pywebview` package is
    importable (never required; `--browser` skips it), stopping when the window closes;
  * otherwise in the default browser (`webbrowser`), stopping on Ctrl-C, SIGTERM or SIGHUP
    (the Terminal window closing) -- or, with `--dialogs` on a Mac, when the Stop button of
    the small dialog it shows is clicked (the .app has no terminal to press Ctrl-C in);
  * not at all with `--no-open`.

The flag is the surface switch (docs/health/07, "Flags and what reads them"): the app
refuses to start, printing why and the enable recipe, unless `desktop-app` (this surface)
and `reference-v1` (the viewer it serves) are both enabled in config/health/modules.yaml.
A registry that cannot be read fails closed. With `--dialogs` the refusal is also shown
as a macOS dialog (osascript), because a double-clicked .app has no terminal.

Every response carries `Cache-Control: no-store`, so a page updated on disk is never
hidden by a cached copy, and an explicit JavaScript, JSON, SVG and HTML media type (a
module script served as anything but JavaScript is refused by the browser, and the
system's MIME table is not to be trusted for that). A request whose Host header is not
127.0.0.1 or localhost on this port is refused (a page on another site cannot reach the
server by rebinding a name to 127.0.0.1). Request logging is off unless --verbose, so the
disclaimer stays the last thing on screen.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import importlib.util
import ipaddress
import os
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VIEWER = ROOT / "reference" / "metabolic-map-v1"
TITLE = "Metabolic Map"
MODULE_ID = "desktop-app"
"""The registry id whose flag governs this surface."""
VIEWER_MODULE = "reference-v1"
"""The registry id of the viewer it serves; its flag governs the page too."""
HOST = "127.0.0.1"
DISCLAIMER = "Educational model — not medical advice."
VALIDATION_STATUS = "Not clinically validated."
"""config/health/base.yaml model.disclaimer and model.validation_status, held equal by
tests/health_app_selftest.py. Constants, not a config read: the surface must print them
even when nothing else can be loaded (the rule health.cli follows)."""
STOP_BUTTON = "Stop Metabolic Map"
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".css": "text/css; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".wasm": "application/wasm",
}


def footer() -> None:
    """The last two lines of everything this program prints (HREQ-S-01)."""
    print(DISCLAIMER)
    print(VALIDATION_STATUS, flush=True)


def say(*lines: str) -> None:
    """Print `lines`, then the footer again, so the footer stays last on screen."""
    for line in lines:
        print(line)
    footer()


def _ensure_printable() -> None:
    """Never crash on the disclaimer's dash under a non-UTF-8 stdout (health.cli's rule)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            (DISCLAIMER + " · ").encode(stream.encoding or "ascii")
        except (UnicodeEncodeError, LookupError):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(errors="replace")


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------
def refusal() -> list[str] | None:
    """None when this surface may start; otherwise the lines that say why it may not and
    how to turn it on. HREQ-X-01: the flag governs the surface. Fails closed: a registry
    that cannot be read refuses, never starts."""
    try:
        from health.registry import display_path, load_modules, module_state, registry_path
        where = display_path(registry_path())
        modules = load_modules()
        states = {mid: module_state(mid, modules) for mid in (MODULE_ID, VIEWER_MODULE)}
    except Exception as exc:  # noqa: BLE001 - an unreadable registry fails closed, never open
        return [f"{TITLE}: not started -- the module registry cannot be read "
                f"({type(exc).__name__}: {exc}).",
                "The flag fails closed: nothing is served until the registry reads cleanly "
                "(PYTHONPATH=src python3 -m health.registry --check)."]
    for mid, what in ((MODULE_ID, "this app"), (VIEWER_MODULE, "the viewer this app serves")):
        state = states[mid]
        if state == "enabled":
            continue
        if state != "disabled":
            return [f"{TITLE}: not started -- {mid} ({what}) is not registered in {where}: "
                    "the module has been removed, or this registry predates it."]
        return [f"{TITLE}: not started -- {mid} ({what}) is disabled in {where}.",
                "A module lands switched off; its own one-line pull request turns it on, "
                "and the Operator approves it (docs/health/04 §6.5, the two-PR rule):",
                f"  1. on a branch enable/{mid}, set `enabled: true` on the {mid} entry of "
                "config/health/modules.yaml",
                "  2. PYTHONPATH=src python3 -m health.registry --write-07",
                "  3. python3 tools/gates.py  (ALL GATES GREEN), then the pull request"]
    if not (VIEWER / "index.html").is_file():
        return [f"{TITLE}: not started -- {VIEWER / 'index.html'} does not exist."]
    return None


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------
class ViewerHandler(http.server.SimpleHTTPRequestHandler):
    """Static files of the viewer, GET and HEAD only, with explicit media types and no
    caching; refuses a Host that is not this loopback address."""

    server_version = "MetabolicMap"
    sys_version = ""

    def guess_type(self, path: str | os.PathLike[str]) -> str:
        ext = os.path.splitext(os.fspath(path))[1].lower()
        return MIME_TYPES.get(ext) or super().guess_type(path)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self) -> Any:
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in getattr(self.server, "allowed_hosts", ()):
            self.send_error(403, "This server answers http://127.0.0.1 and http://localhost "
                                 "on its own port only")
            return None
        return super().send_head()

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)


class LoopbackServer(http.server.ThreadingHTTPServer):
    """A ThreadingHTTPServer that can only be bound to a loopback address (HREQ-N-05).
    One thread per request, so the viewer's module and worker fetches never wait on each
    other; daemon threads, so stopping never waits on a browser's open connection."""

    daemon_threads = True
    allow_reuse_port = False   # no second listener on the same port

    def __init__(self, directory: Path, port: int = 0, host: str = HOST,
                 verbose: bool = False) -> None:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError(f"{host} is not a loopback address: HREQ-N-05 allows no listener "
                             "beyond localhost")
        self.verbose = verbose
        handler = functools.partial(ViewerHandler, directory=str(directory))
        super().__init__((host, port), handler)
        bound = self.server_address[1]
        self.allowed_hosts = frozenset({f"{HOST}:{bound}", f"localhost:{bound}"})

    def server_bind(self) -> None:
        # HTTPServer.server_bind asks socket.getfqdn(), a DNS lookup that can stall for
        # seconds on a Mac; the name is never used here.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A browser that drops a connection mid-response (a reload, a stopped worker) is
        not an error worth a traceback under the disclaimer; anything else is one line,
        followed by the footer (the traceback with --verbose)."""
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            return
        if self.verbose:
            super().handle_error(request, client_address)
        else:
            say(f"{TITLE}: a request failed ({type(exc).__name__}: {exc}); --verbose shows "
                "the traceback")


def make_server(port: int = 0, *, host: str = HOST, directory: Path = VIEWER,
                verbose: bool = False) -> LoopbackServer:
    """The viewer's server, listening (not yet serving) on `host`:`port` (0: the OS picks)."""
    return LoopbackServer(directory, port, host, verbose)


def url_of(server: LoopbackServer) -> str:
    return f"http://{HOST}:{server.server_address[1]}/"


# ---------------------------------------------------------------------------
# Opening it
# ---------------------------------------------------------------------------
def native_window_available() -> bool:
    """The optional pywebview package (module `webview`) is importable."""
    try:
        return importlib.util.find_spec("webview") is not None
    except (ImportError, ValueError):
        return False


def open_native_window(url: str) -> None:
    """A native window titled "Metabolic Map" on `url`; returns when it is closed. Raises
    whatever pywebview raises when it cannot open one (the caller falls back)."""
    import webview  # optional: pywebview, never required

    webview.create_window(TITLE, url, width=1440, height=900, min_size=(960, 640))
    webview.start()


def _osascript(lines: list[str], *args: str) -> list[str] | None:
    """The osascript command for an AppleScript `on run argv` handler, or None when there
    is no osascript (not a Mac). Text travels as arguments, never spliced into the script."""
    exe = shutil.which("osascript")
    if exe is None:
        return None
    cmd = [exe, "-e", "on run argv"]
    for line in lines:
        cmd += ["-e", line]
    return [*cmd, "-e", "end run", *args]


def dialog_command(text: str, button: str, icon: str = "note") -> list[str] | None:
    """A macOS dialog with one button; None where osascript does not exist."""
    return _osascript(["display dialog (item 1 of argv) with title (item 2 of argv) "
                       f"buttons {{(item 3 of argv)}} default button 1 with icon {icon}"],
                      text, TITLE, button)


def show_refusal_dialog(lines: list[str]) -> None:
    cmd = dialog_command("\n".join([*lines, "", DISCLAIMER, VALIDATION_STATUS]), "OK", "caution")
    if cmd is None:
        return
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"{TITLE}: the dialog could not be shown ({type(exc).__name__}: {exc})")


def stop_dialog_text(url: str) -> str:
    return "\n".join([f"{TITLE} is running at {url} -- on this computer only.",
                      "",
                      "It has opened in your browser. Click the button below to stop it; the "
                      "page stays open but can no longer be reloaded.",
                      "",
                      DISCLAIMER,
                      VALIDATION_STATUS])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m health.app",
        description=f"Open the {TITLE} viewer (reference/metabolic-map-v1/) as a desktop app, "
                    f"served on {HOST} only. {DISCLAIMER} {VALIDATION_STATUS}")
    ap.add_argument("--port", type=int, default=0,
                    help="port on 127.0.0.1 (default: a free one the OS chooses)")
    ap.add_argument("--no-open", action="store_true",
                    help="serve only; print the address and open nothing")
    ap.add_argument("--browser", action="store_true",
                    help="open the default browser even when pywebview is installed")
    ap.add_argument("--dialogs", action="store_true",
                    help="on a Mac, also show refusals and a Stop button as dialogs "
                         "(the .app passes this: it has no terminal)")
    ap.add_argument("--verbose", action="store_true", help="log every request to stderr")
    return ap


def main(argv: list[str] | None = None) -> int:
    _ensure_printable()
    args = build_parser().parse_args(argv)
    why = refusal()
    if why is not None:
        say(*why)
        if args.dialogs:
            show_refusal_dialog(why)
        return 1
    try:
        server = make_server(args.port, verbose=args.verbose)
    except (OSError, ValueError, OverflowError) as exc:
        lines = [f"{TITLE}: not started -- cannot listen on {HOST}:{args.port} "
                 f"({type(exc).__name__}: {exc})."]
        say(*lines)
        if args.dialogs:
            show_refusal_dialog(lines)
        return 1
    url = url_of(server)
    if args.no_open:
        mode = "none"
    elif native_window_available() and not args.browser:
        mode = "window"
    else:
        mode = "browser"
    how = {"none": "not opening it (--no-open): open the address above yourself",
           "window": f'opening it in a native window, "{TITLE}" (pywebview); close the '
                     "window to stop",
           "browser": "opening it in your default browser"}[mode]
    say(f"{TITLE} -- the V1 viewer, reference/metabolic-map-v1/ (unchanged)",
        f"serving {url} on this computer only ({HOST}); Ctrl-C to stop",
        how)

    stop = threading.Event()
    reason = ["Ctrl-C"]

    def on_signal(signum: int, _frame: Any) -> None:
        reason[0] = signal.Signals(signum).name
        stop.set()

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                               name="metabolic-map-server", daemon=True)
    serving.start()
    dialog: list[subprocess.Popen[str]] = []
    try:
        if mode == "window":
            try:
                open_native_window(url)
                reason[0] = "window closed"
                stop.set()
            except Exception as exc:  # noqa: BLE001 - the window is optional; fall back
                mode = "browser"
                say(f"native window unavailable ({type(exc).__name__}: {exc}); "
                    "opening your default browser instead")
        if mode == "browser":
            if not webbrowser.open(url):
                say(f"no browser could be opened: open {url} yourself")
            cmd = dialog_command(stop_dialog_text(url), STOP_BUTTON) if args.dialogs else None
            if cmd is not None:
                dialog.append(subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, text=True))

                def watch(proc: subprocess.Popen[str]) -> None:
                    out, err = proc.communicate()
                    if f"button returned:{STOP_BUTTON}" in out:
                        reason[0] = "Stop clicked"
                        stop.set()
                    elif not stop.is_set():
                        say(f"{TITLE}: the Stop dialog closed without a click "
                            f"({err.strip() or out.strip() or proc.returncode}); still "
                            "serving -- stop it with Ctrl-C or from Activity Monitor")

                threading.Thread(target=watch, args=(dialog[0],), daemon=True).start()
        while not stop.wait(0.2):
            pass
    except KeyboardInterrupt:
        reason[0] = "Ctrl-C"
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
        for proc in dialog:
            if proc.poll() is None:
                proc.terminate()
    say(f"{TITLE} stopped ({reason[0]}).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:              # stdout closed early: exit quietly
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(1) from None
