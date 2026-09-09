"""Local dashboard: one process, localhost only, reads what the recorder wrote.

    python -m navanax.cli dashboard      # then open http://127.0.0.1:8765

Architecture, and why (docs/07 §1, "one writer, readers attach read-only"):

  * The stream consumer (`navanax ingest`) is the ONLY writer to the landing
    zone. This process never touches it.
  * This process is the only writer to the analytical store. A background
    thread runs the Normalizer every few seconds -- landing zone in,
    queryable rows out -- and the HTTP handlers query the same connection
    under a lock. Readers that need fresh writes "query through the writer
    process" (docs/07 §1), which is exactly this.
  * Bound to 127.0.0.1 and nothing else (REQ-N-13). There is no auth because
    there is no exposure: if a request arrives it came from this machine.

Nothing here is a metric. Every number the page shows comes from
`metrics.MetricEngine` through the MetricRequest contract, and the response
carries its basis so the page can print it.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .landing import is_integrity_failure, verify_manifest
from .metrics import METRICS, MetricEngine, load_intervals, parse_range, parse_trait_filter
from .normalize import Normalizer, iso_to_ts
from .opstore import OperationalStore
from .traits import ensure_schema as ensure_traits_schema
from .traits import trait_values

log = logging.getLogger("navanax.dashboard")
UI_DIR = Path(__file__).resolve().parent / "ui"


class Dashboard:
    def __init__(self, root: Path, cfg: dict[str, Any], slugs: list[str]) -> None:
        self.root = root
        self.cfg = cfg
        self.slugs = slugs
        self.landing = root / cfg["landing"]["root"]
        self.tz = (cfg.get("display") or {}).get("timezone", "UTC")
        self.intervals = load_intervals(root / "config" / "intervals.yaml")
        self.norm = Normalizer(self.landing, root / cfg["analytical"]["path"])
        ensure_traits_schema(self.norm.conn)
        self.engine = MetricEngine(self.norm.conn, self.intervals, self.tz)
        self.store = OperationalStore(root / cfg["opstore"]["path"])
        self.lock = threading.Lock()
        self.refresh = float((cfg.get("dashboard") or {}).get("refresh_seconds", 5))
        self.last_sync: dict[str, Any] = {"at": None, "stats": None, "error": None, "took_ms": None}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="navanax-normalizer", daemon=True)

    # -- background normaliser ----------------------------------------------
    def start(self) -> None:
        self._sync_once()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.refresh):
            self._sync_once()

    def _sync_once(self) -> None:
        t0 = time.monotonic()
        try:
            with self.lock:
                stats = self.norm.sync()
            self.last_sync = {"at": _now_iso(), "stats": stats, "error": None,
                              "took_ms": round((time.monotonic() - t0) * 1000)}
        except Exception as exc:  # noqa: BLE001 - the page must keep serving
            log.exception("normalizer sync failed")
            self.last_sync = {"at": _now_iso(), "stats": None, "error": f"{type(exc).__name__}: {exc}",
                              "took_ms": round((time.monotonic() - t0) * 1000)}

    # -- API ------------------------------------------------------------------
    def api_status(self) -> dict[str, Any]:
        lock = self.landing / ".ingest.lock"
        recorder: dict[str, Any] = {"running": False}
        if lock.exists():
            try:
                txt = lock.read_text().strip()
                recorder = {"running": _pid_alive(txt), "lock": txt}
            except OSError:
                pass
        with self.lock:
            c = self.norm.conn
            n_events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            last = c.execute("SELECT MAX(observed_at), MAX(valid_at) FROM events").fetchone()
            per_min = c.execute(
                "SELECT COUNT(*) FROM events WHERE observed_ts >= ?", (time.time() - 60,)).fetchone()[0]
            unparsed = c.execute("SELECT COUNT(*) FROM unparsed").fetchone()[0]
            # REQ-F-02 / REQ-D-25: the rate the USD view is converting at, with
            # its provenance and age, so "in dollars" is never an unstated basis.
            rate = c.execute(
                """SELECT implied_ethusd, valid_at FROM events
                   WHERE implied_ethusd IS NOT NULL ORDER BY valid_ts DESC LIMIT 1""").fetchone()
            files = c.execute("SELECT COUNT(*), SUM(rows) FROM watermarks").fetchone()
        disk = sum(p.stat().st_size for p in self.landing.rglob("*") if p.is_file()) if self.landing.exists() else 0
        return {
            "now": _now_iso(), "display_timezone": self.tz, "watchlist": self.slugs,
            "recorder": recorder,
            "store": {"events": n_events, "events_last_60s": per_min, "unparsed_frames": unparsed,
                      "landing_files_seen": files[0] or 0, "last_observed_at": last[0], "last_valid_at": last[1]},
            "normalizer": self.last_sync,
            "ethusd": {"rate": rate[0] if rate else None, "at": rate[1] if rate else None,
                       "provider": "opensea payment_token (implied by the latest priced event)"},
            "landing_bytes": disk,
            "gaps": {"open": len(self.store.open_gaps()), "awaiting_backfill": len(self.store.unbackfilled_gaps())},
        }

    def _slug(self, q: dict[str, str]) -> str:
        slug = q.get("collection") or (self.slugs[0] if self.slugs else "")
        if not slug:
            raise ValueError("no collection: the watchlist in config/base.yaml is empty")
        return slug

    def api_series(self, q: dict[str, str]) -> dict[str, Any]:
        with self.lock:
            return self.engine.series(
                metric=q.get("metric", "immediacy_cost"),
                collection=self._slug(q),
                denomination=q.get("denom", "ETH"),
                transform=q.get("transform", "ABS"),
                interval=q.get("interval", "5m"),
                range_=q.get("range", "6h"),
                traits=parse_trait_filter(q.get("traits")),
                gaps=self._gap_spans())

    def api_multi(self, q: dict[str, str]) -> dict[str, Any]:
        """Several metrics on one time base, for the overlay chart (REQ-F-09)."""
        metrics = [m for m in q.get("metrics", "floor_ask,collection_bid,top_item_bid").split(",") if m]
        out = {}
        for m in metrics:
            qq = dict(q)
            qq["metric"] = m
            out[m] = self.api_series(qq)
        return out

    def _window(self, q: dict[str, str]) -> tuple[float, float]:
        now = datetime.now(timezone.utc)
        s, e = parse_range(q.get("range", "6h"), self.intervals, now, self.tz)
        return s.timestamp(), e.timestamp()

    def api_book(self, q: dict[str, str]) -> dict[str, Any]:
        with self.lock:
            return self.engine.live_book(self._slug(q), limit=int(q.get("limit", "25")),
                                         traits=parse_trait_filter(q.get("traits")))

    def api_tape(self, q: dict[str, str]) -> list[dict[str, Any]]:
        with self.lock:
            return self.engine.tape(self._slug(q), limit=int(q.get("limit", "50")),
                                    traits=parse_trait_filter(q.get("traits")))

    def api_makers(self, q: dict[str, str]) -> dict[str, Any]:
        s, e = self._window(q)
        with self.lock:
            return self.engine.makers(self._slug(q), s, e)

    def api_lifetimes(self, q: dict[str, str]) -> dict[str, Any]:
        s, e = self._window(q)
        with self.lock:
            return self.engine.bid_lifetimes(self._slug(q), s, e)

    def api_mix(self, q: dict[str, str]) -> list[dict[str, Any]]:
        s, e = self._window(q)
        with self.lock:
            return self.engine.event_mix(q.get("collection"), s, e)

    def api_traits(self, q: dict[str, str]) -> dict[str, Any]:
        slug = self._slug(q)
        with self.lock:
            vals = trait_values(self.norm.conn, slug)
            n_tokens = self.norm.conn.execute("SELECT COUNT(*) FROM tokens WHERE collection=?", (slug,)).fetchone()[0]
            n_traited = self.norm.conn.execute("SELECT COUNT(DISTINCT token_id) FROM traits WHERE collection=?", (slug,)).fetchone()[0]
        onboarding = next((r for r in self.store.onboarding_status() if r["collection_slug"] == slug), None)
        return {"collection": slug, "tokens": n_tokens, "with_traits": n_traited,
                "onboarding": onboarding, "traits": vals}

    def api_screener(self, q: dict[str, str]) -> dict[str, Any]:
        with self.lock:
            return self.engine.screener(
                self._slug(q), traits=parse_trait_filter(q.get("traits")),
                sort=q.get("sort", "token_id"), direction=q.get("dir", "asc"),
                page=int(q.get("page", "0")), page_size=min(200, int(q.get("size", "50"))),
                denom=q.get("denom", "ETH"))

    def _gap_spans(self) -> list[tuple[float, float | None]]:
        """Ingestion gaps as epoch spans, for masking buckets we were not listening in.
        Cached on the manifest directory's mtime: api_multi asks once per metric.
        The key is sound ONLY because ManifestWriter writes via mkstemp + os.replace,
        which moves the directory mtime. An in-place rewrite would leave this cache
        serving stale gaps silently -- keep the writer atomic (tech-lead, round 3)."""
        mdir = self.landing / "_manifest"
        try:
            stamp = mdir.stat().st_mtime_ns
        except OSError:
            stamp = None
        cached = getattr(self, "_gap_cache", None)
        if cached and cached[0] == stamp:
            return cached[1]
        out = []
        for g in self.api_gaps():
            gs = iso_to_ts(g.get("started_at"))
            if gs is None:
                continue
            out.append((gs, iso_to_ts(g.get("ended_at"))))
        self._gap_cache = (stamp, out)
        return out

    def api_gaps(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        mdir = self.landing / "_manifest"
        seen: set[Any] = set()
        if mdir.exists():
            for mp in sorted(mdir.glob("*.json")):
                try:
                    for g in json.loads(mp.read_text()).get("gaps", []):
                        key = (g.get("gap_id"), g.get("started_at"), g.get("reason"))
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(g)
                except (OSError, ValueError):
                    continue
        out.sort(key=lambda g: g.get("started_at") or "")
        return out

    def api_audit(self) -> dict[str, Any]:
        problems = verify_manifest(self.landing, deep=False)
        return {"at": _now_iso(), "mode": "shallow (checksums; run status.command for the deep audit)",
                "failures": [p for p in problems if is_integrity_failure(p)],
                "notes": [p for p in problems if not is_integrity_failure(p)]}

    def api_meta(self) -> dict[str, Any]:
        return {"metrics": {k: v["label"] for k, v in METRICS.items()},
                "intervals": list(self.intervals["intervals"]),
                "anchored": list(self.intervals["anchored"]),
                "transforms": ["ABS", "PCT", "LOG", "DIFF", "BPS"],
                "denominations": ["ETH", "USD"], "watchlist": self.slugs}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pid_alive(lock_text: str) -> bool:
    import os
    try:
        pid = int(lock_text.split("pid=")[1].split()[0])
        os.kill(pid, 0)
        return True
    except (IndexError, ValueError, ProcessLookupError, PermissionError, OSError):
        return False


# ---------------------------------------------------------------------------
def make_handler(dash: Dashboard):
    routes = {
        "/api/status": lambda q: dash.api_status(),
        "/api/meta": lambda q: dash.api_meta(),
        "/api/series": dash.api_series,
        "/api/multi": dash.api_multi,
        "/api/book": dash.api_book,
        "/api/tape": dash.api_tape,
        "/api/makers": dash.api_makers,
        "/api/lifetimes": dash.api_lifetimes,
        "/api/mix": dash.api_mix,
        "/api/gaps": lambda q: dash.api_gaps(),
        "/api/traits": dash.api_traits,
        "/api/screener": dash.api_screener,
        "/api/audit": lambda q: dash.api_audit(),
    }

    class H(BaseHTTPRequestHandler):
        server_version = "navanax-dashboard/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in routes:
                try:
                    payload = routes[u.path](q)
                    self._send(200, json.dumps(payload, default=str).encode(), "application/json")
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                except Exception as exc:  # noqa: BLE001 - report, never hang the page
                    log.exception("api error")
                    self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                               "application/json")
                return
            rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            f = (UI_DIR / rel).resolve()
            if UI_DIR.resolve() in f.parents and f.is_file():
                ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
            else:
                self._send(404, b"not found", "text/plain")

    return H


def serve(root: Path, cfg: dict[str, Any], slugs: list[str], *, host: str | None = None,
          port: int | None = None, open_browser: bool = True) -> None:
    dcfg = cfg.get("dashboard") or {}
    host = host or dcfg.get("host", "127.0.0.1")
    port = port if port is not None else int(dcfg.get("port", 8765))
    if host not in ("127.0.0.1", "localhost", "::1"):
        # REQ-N-13. Refuse rather than warn: a dashboard over the whole
        # landing zone bound to 0.0.0.0 is the exposure the requirement exists to prevent.
        raise ValueError(f"dashboard host must be loopback (REQ-N-13); got {host!r}")
    dash = Dashboard(root, cfg, slugs)
    dash.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(dash))
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"dashboard    {url}   (localhost only; ctrl-c to stop)")
    print(f"store        {root / cfg['analytical']['path']}")
    print(f"normalizer   every {dash.refresh:.0f}s from {dash.landing}")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - cosmetic; the URL is printed above
            log.debug("could not open a browser: %s", exc)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dash.stop()
        httpd.server_close()
        dash.norm.close()
