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
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import redundancy
from .landing import is_integrity_failure, verify_manifest
from .metrics import METRICS, MetricEngine, load_intervals, parse_range, parse_trait_filter
from .normalize import (
    DEDUP_KEY_FIELDS,
    Normalizer,
    iso_to_ts,
    rebuild_recipe,
    store_writer_info,
)
from .opstore import OperationalStore
from .traits import ensure_schema as ensure_traits_schema
from .traits import trait_values

log = logging.getLogger("navanax.dashboard")
UI_DIR = Path(__file__).resolve().parent / "ui"

# How long a `PRAGMA quick_check` result stands before Health runs another one.
# quick_check reads every page: on the Operator's 2.8 GB store that is seconds,
# and running it per request would make the Health tab the slowest page in the
# app AND hold the writer lock while it ran. Ten minutes is the trade
# (BUG-20260910-067): corruption that has just appeared is visible within one
# refresh cycle of the tab, and the cost is paid once.
QUICK_CHECK_TTL_SECONDS = 600

# How often a dashboard running DEGRADED (the store could not be opened) tries
# the store again. 60 s, so `rebuild-store.command` is one click: move the corrupt
# file aside and the running dashboard folds a fresh store within a minute, with
# no restart to remember. Not tighter, because retrying a large store is not free.
STORE_REOPEN_SECONDS = 60

# The API routes that still answer while degraded. Everything else 503s, because
# everything else reads the analytical store. These three are exactly the ones a
# person needs when the store is gone: what is wrong (`/api/health`, which carries
# the rebuild recipe), what the page can offer (`/api/meta`), and whether we were
# even listening (`/api/gaps`, which is read from the landing zone's manifests).
DEGRADED_ROUTES = frozenset({"/api/health", "/api/meta", "/api/gaps"})


class Dashboard:
    def __init__(self, root: Path, cfg: dict[str, Any], slugs: list[str]) -> None:
        self.root = root
        self.cfg = cfg
        self.slugs = slugs
        self.landing = root / cfg["landing"]["root"]
        # PR-10. Every landing root the normalizer folds, as (label, path). With
        # `stream.redundant.enabled` false -- the default -- this is exactly one
        # entry and everything below behaves as it always has. `self.landing`
        # stays the PRIMARY root: the recorder-liveness check, the disk figure and
        # the manifest-driven gap list are about connection A.
        self.redundant = redundancy.settings(cfg)
        self.landing_roots = redundancy.landing_roots(root, cfg)
        self.dup_monitor = redundancy.DuplicateFractionMonitor(
            min_events=self.redundant.min_events,
            collapse_fraction=self.redundant.collapse_fraction,
            label_b=self.redundant.label)
        self.tz = (cfg.get("display") or {}).get("timezone", "UTC")
        self.intervals = load_intervals(root / "config" / "intervals.yaml")
        self.db_path = root / cfg["analytical"]["path"]
        self.store = OperationalStore(root / cfg["opstore"]["path"])
        self.lock = threading.Lock()
        self.refresh = float((cfg.get("dashboard") or {}).get("refresh_seconds", 5))
        self.last_sync: dict[str, Any] = {"at": None, "stats": None, "error": None, "took_ms": None}
        #: `covered_by` annotations this process has had to RETRACT (BUG-20260911-077).
        self.covered_by_revoked = 0
        self._quick_check: dict[str, Any] | None = None
        self._quick_check_mono: float = 0.0
        # DEGRADED MODE (BUG-20260910-067, tech-lead B1). `norm` is None and
        # `store_error` is set when the store could not be opened at all. See
        # `_open_store`.
        self.norm: Normalizer | None = None
        self.engine: MetricEngine | None = None
        self.store_error: dict[str, Any] | None = None
        self._reopen_mono: float = 0.0
        self._open_store()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="navanax-normalizer", daemon=True)

    # -- opening the store, and surviving not being able to -------------------
    def _open_store(self) -> bool:
        """Open the analytical store as its folding writer. Never raises on a corrupt one.

        BUG-20260910-067, second round. The first fix stopped the dashboard
        CREATING a malformed store. This one stops a malformed store taking the
        dashboard down with it -- which was the incident's actual end state:
        `sqlite3.connect(...)` succeeds on a corrupt file (it is lazy) and the
        first `PRAGMA journal_mode=WAL` raises `DatabaseError: database disk image
        is malformed`, straight out of `__init__`, past every `except` in
        `cmd_dashboard`, as a raw traceback and an undocumented exit 1 -- which
        KeepAlive then repeats forever.

        A corrupt store is the moment the Operator most needs the page: Health is
        where the rebuild recipe lives. So this REFUSES TO FAIL. It returns False,
        leaves `norm`/`engine` None, and records `store_error`; `serve()` binds and
        serves anyway, data endpoints answer 503 with the reason, Health and the
        page still render, and `_loop` retries the open every
        STORE_REOPEN_SECONDS so a rebuilt store is picked up without a restart.

        `StoreWriterBusyError` is deliberately NOT caught: another process holding
        the lock is not a degraded store, it is the wrong process, and the caller
        must exit 4 rather than serve a page with no data behind it.
        """
        try:
            norm = Normalizer(self.landing_roots[0][1], self.db_path, writer=True,
                              label=self.landing_roots[0][0],
                              extra_roots=self.landing_roots[1:])
            ensure_traits_schema(norm.conn)
        except sqlite3.DatabaseError as exc:
            self.norm = self.engine = None
            self.store_error = {
                "at": _now_iso(),
                "error": f"{type(exc).__name__}: {exc}",
                "store": str(self.db_path),
                "rebuild": rebuild_recipe(self.db_path),
            }
            log.error("the analytical store at %s could not be opened (%s). Serving DEGRADED: "
                      "Health and the page still load, every data endpoint answers 503, and "
                      "the open is retried every %.0fs. %s",
                      self.db_path, exc, STORE_REOPEN_SECONDS, self.store_error["rebuild"])
            return False
        self.norm = norm
        self.engine = MetricEngine(norm.conn, self.intervals, self.tz)
        if self.store_error is not None:
            log.warning("the analytical store opened again after being unreadable since %s; "
                        "the page is live", self.store_error["at"])
        self.store_error = None
        self._quick_check = None        # the cached verdict was about the OLD file
        return True

    def degraded(self) -> bool:
        return self.norm is None

    def store_unavailable(self) -> dict[str, Any]:
        """The 503 body. One shape, so every refused endpoint says the same thing."""
        err = self.store_error or {}
        return {"error": "analytical store is malformed",
                "detail": err.get("error"), "store": err.get("store", str(self.db_path)),
                "since": err.get("at"), "rebuild": err.get("rebuild") or rebuild_recipe(self.db_path),
                "note": "the recorder is a separate process and is still landing frames; "
                        "nothing in data/landing is affected, and a rebuild loses no history."}

    # -- background normaliser ----------------------------------------------
    def start(self) -> None:
        if self.norm is not None:
            self._sync_once()
        self._thread.start()

    def stop(self, *, timeout: float = 30.0) -> None:
        """Signal the fold thread and WAIT for it, before anyone closes the store.

        The join matters for the writer lock (BUG-20260910-067): `serve()`'s
        `finally` calls `stop()` and then `norm.close()`, which releases the
        exclusive lock. Releasing it while a fold was still mid-batch would hand
        the store to the next process with a write in flight -- which is a
        smaller version of exactly what corrupted it. The timeout is generous
        because a fold on a large store is seconds, not minutes; if it is
        exceeded we go on anyway rather than hang the shutdown, and the OS
        releases the flock when the process ends regardless.
        """
        self._stop.set()
        if not self._thread.is_alive():
            return
        try:
            self._thread.join(timeout)
        except KeyboardInterrupt:
            # A second Ctrl-C while we are waiting out the last fold. Do not let
            # it escape: it would skip `norm.close()` and leave the store's WAL
            # un-checkpointed. The flock is released by the OS at exit either way.
            log.warning("interrupted while waiting for the last fold; closing anyway")
            return
        if self._thread.is_alive():
            log.warning("the normalizer thread did not finish within %.0fs; "
                        "closing the store anyway", timeout)

    def _loop(self) -> None:
        """One tick. The tick LENGTH depends on whether we have a store.

        While degraded the tick is bounded by `STORE_REOPEN_SECONDS`, not by
        `refresh`. `refresh` is "how often to fold new frames", and an operator who
        sets it to an hour is saying the market data can be an hour stale -- he is
        not saying he wants to wait an hour after rebuilding the store for the page
        to notice. Before this, `_retry_open`'s own STORE_REOPEN_SECONDS rate limit
        could only make the probe LESS frequent than `refresh`, never more, so at
        `refresh_seconds: 3600` the rebuilt store sat unnoticed for up to an hour
        and `rebuild-store.command` stopped being a one-click fix.
        """
        while True:
            wait = min(self.refresh, STORE_REOPEN_SECONDS) if self.norm is None else self.refresh
            if self._stop.wait(wait):
                return
            if self.norm is None:
                self._retry_open()
            else:
                self._sync_once()

    def _retry_open(self) -> None:
        """While degraded, try the store again -- at most once every STORE_REOPEN_SECONDS.

        This is what makes `rebuild-store.command` a one-click fix rather than a
        two-step one: the Operator moves the corrupt file aside and the running
        dashboard picks the rebuilt store up on the next tick, with no restart and
        no second thing to remember. The rate limit is separate from `refresh`
        because opening a large store is not free and a tight retry on a corrupt
        one is its own hot loop.
        """
        now = time.monotonic()
        if now - self._reopen_mono < STORE_REOPEN_SECONDS:
            return
        self._reopen_mono = now
        try:
            with self.lock:
                opened = self._open_store()
        except Exception as exc:  # noqa: BLE001 - incl. StoreWriterBusyError: stay degraded
            log.warning("retrying the analytical store failed: %s: %s", type(exc).__name__, exc)
            if self.store_error is not None:
                self.store_error["last_retry_error"] = f"{type(exc).__name__}: {exc}"
            return
        if opened:
            self._sync_once()

    def _sync_once(self) -> None:
        if self.norm is None:
            return
        t0 = time.monotonic()
        try:
            with self.lock:
                stats = self.norm.sync()
            stats = {**stats, "gaps_annotated_covered": self.annotate_covered_gaps()}
            # PR-10. A fold can be the one that first brings a second connection's
            # rows into the store, and every count in `metrics` branches on that
            # answer. Cached per fold, invalidated here -- the alternative is a
            # page that keeps printing doubled counts until the next restart.
            if self.engine is not None:
                self.engine.invalidate_connection_cache()
            self.last_sync = {"at": _now_iso(), "stats": stats, "error": None,
                              "took_ms": round((time.monotonic() - t0) * 1000)}
        except Exception as exc:  # noqa: BLE001 - the page must keep serving
            log.exception("normalizer sync failed")
            self.last_sync = {"at": _now_iso(), "stats": None, "error": f"{type(exc).__name__}: {exc}",
                              "took_ms": round((time.monotonic() - t0) * 1000)}

    def annotate_covered_gaps(self) -> int:
        """Mark A's gaps that B was demonstrably recording through. Returns the count.

        NOTHING IS SUPPRESSED. The gap keeps its start, its end, its class lists
        and its open/closed state, and it is still counted by `open_gaps()` and by
        `unbackfilled_gaps()`. All that is added is `covered_by='b'`, on a column
        that was NULL. "A was blind and B was not" is a different fact from
        "no gap occurred", and silently promoting the first into the second is
        exactly the confusion this project cannot afford (dataeng §3.a, failure
        mode 5).

        Both directions: it also CLEARS an annotation the current evidence no
        longer supports (BUG-20260911-077), because coverage is derived from
        evidence that arrives late and a claim made on incomplete evidence must be
        retractable. The gap itself is never closed, shortened or removed.

        A no-op with the flag off: there is no second connection to have covered
        anything, and this returns 0 without touching the register.

        Coverage is the other connection's open-file windows MINUS its own
        recorded gaps (BUG-20260911-074), and only a gap covered END TO END is
        annotated.
        """
        if not self.redundant.enabled:
            return 0
        marked = 0
        for label, lroot in self.landing_roots[1:]:
            upd = redundancy.annotate_gaps_covered_by(self.store, label, lroot)
            marked += len(upd.marked)
            # Revocations are counted for the life of this process and surfaced on
            # Health. A claim that had to be taken back is worth more attention
            # than one that stood, and a count that only ever appeared in a log
            # line is a count nobody reads (BUG-20260911-077).
            self.covered_by_revoked += len(upd.revoked)
        return marked

    # -- API ------------------------------------------------------------------
    def _recorder_state(self) -> dict[str, Any]:
        """Is the stream consumer running? Read from ITS lock file, not from the store.

        Factored out because a degraded dashboard still has to answer it -- when
        the analytical store is unreadable, "is the recorder still landing frames?"
        is the most important question on the page, and the answer has nothing to
        do with the store.
        """
        lock = self.landing / ".ingest.lock"
        recorder: dict[str, Any] = {"running": False}
        if lock.exists():
            try:
                txt = lock.read_text().strip()
                recorder = {"running": _pid_alive(txt), "lock": txt}
            except OSError:
                pass
        return recorder

    def api_status(self) -> dict[str, Any]:
        recorder = self._recorder_state()
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
        # `book` selects the standing book (the default for floor_ask,
        # collection_bid and immediacy_cost -- Operator, 2026-09-10) or the old
        # interval extremum as `book=observed`. Passed through untouched; the
        # engine owns the per-metric default and the basis says which was used.
        with self.lock:
            return self.engine.series(
                metric=q.get("metric", "immediacy_cost"),
                collection=self._slug(q),
                denomination=q.get("denom", "ETH"),
                transform=q.get("transform", "ABS"),
                interval=q.get("interval", "5m"),
                range_=q.get("range", "6h"),
                traits=parse_trait_filter(q.get("traits")),
                book=q.get("book"),
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

    def api_trait_series(self, q: dict[str, str]) -> dict[str, Any]:
        """PR-6's hero panel: everything the trait chart draws, on one grid.

        Minimal on purpose -- traits, interval, range, denomination. There is no
        `transform` and no `book`: the Operator's decision fixes the book to
        STANDING, and a % -change view of a floor whose baseline is a hole is a
        number with no basis. The engine owns every rule; this passes through.
        """
        s, e = self._window(q)
        with self.lock:
            return self.engine.trait_set_series(
                self._slug(q), parse_trait_filter(q.get("traits")), s, e,
                q.get("interval", "5m"), q.get("denom", "ETH"))

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
            return self.engine.makers(self._slug(q), s, e, limit=min(100, int(q.get("limit", "10"))))

    def api_lifetimes(self, q: dict[str, str]) -> dict[str, Any]:
        s, e = self._window(q)
        with self.lock:
            return self.engine.bid_lifetimes(self._slug(q), s, e)

    # -- PR-8 -----------------------------------------------------------------
    def _survival_args(self, q: dict[str, str]) -> dict[str, Any]:
        """The filter row of the bid-lifetime panel, parsed once for both endpoints.

        `as_of` is EXPLICIT and defaults to the end of the window, never to
        "now": `order_lives.exit_reason` is stored as of the fold, and the
        estimator has to be told which instant the question is about or it will
        answer with hindsight (metrics.survival docstring).
        """
        s, e = self._window(q)
        # a mini-map brush overrides the window for the survival panel only
        if q.get("from") or q.get("to"):
            fs, ts = iso_to_ts(q.get("from")), iso_to_ts(q.get("to"))
            if fs is not None:
                s = fs
            if ts is not None:
                e = ts
        as_of = q.get("as_of")
        as_of_ts = iso_to_ts(as_of) if as_of else None
        if as_of and as_of_ts is None:
            raise ValueError(f"as_of must be an ISO timestamp; got {as_of!r}")
        band: tuple[float | None, float | None] | None = None
        if q.get("price_band"):
            parts = q["price_band"].split(":")
            if len(parts) != 2:
                raise ValueError("price_band must be 'min:max' in ETH, either side may be empty")
            try:
                band = (float(parts[0]) if parts[0] else None, float(parts[1]) if parts[1] else None)
            except ValueError as exc:
                raise ValueError(f"price_band is not numeric: {q['price_band']!r}") from exc
        makers = [m for m in (q.get("maker") or "").split(",") if m]
        return {"collection": self._slug(q), "start": s, "end": e,
                "as_of": as_of_ts if as_of_ts is not None else e,
                "kind": q.get("kind", "item_received_bid"),
                "traits": parse_trait_filter(q.get("traits")),
                "maker": makers or None, "price_band": band}

    def api_survival(self, q: dict[str, str]) -> dict[str, Any]:
        """Queries under the lock; the bootstrap OUTSIDE it (BUG-20260910-064).

        `survival_prepare` is every part that touches sqlite. The cluster
        bootstrap that follows is B x n arithmetic over a list already in
        memory -- seconds of it on a real corpus -- and holding the writer lock
        across that blocks every other panel and the background normalizer.
        """
        a = self._survival_args(q)
        with self.lock:
            prep = self.engine.survival_prepare(**a)
        return self.engine.survival(**a, prepared=prep)

    def api_survival_drill(self, q: dict[str, str]) -> dict[str, Any]:
        a = self._survival_args(q)
        try:
            lo, hi = float(q.get("lo", "")), float(q.get("hi", ""))
        except ValueError as exc:
            raise ValueError("survival_drill needs numeric lo and hi (seconds)") from exc
        if not (hi > lo >= 0):
            raise ValueError(f"survival_drill needs 0 <= lo < hi; got lo={lo} hi={hi}")
        return_page = int(q.get("page", "0"))
        with self.lock:
            return self.engine.survival_drill(bin_lo=lo, bin_hi=hi, page=return_page,
                                              page_size=min(200, int(q.get("size", "50"))), **a)

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
        notes = [p for p in problems if not is_integrity_failure(p)]
        # Trait-offer criteria vs the traits table (dataeng §4.2): a criterion
        # with no matching trait value can never match a token, and that reads
        # as "no trait-offer depth" -- a quiet market -- unless it is surfaced.
        coverage = {}
        try:
            with self.lock:
                coverage = {slug: self.engine.criteria_coverage(slug) for slug in self.slugs}
            for slug, cov in coverage.items():
                if cov.get("alert"):
                    n_tok = self.norm.conn.execute("SELECT COUNT(*) FROM traits WHERE collection=?", (slug,)).fetchone()[0]
                    notes.append(f"{slug}: {cov['missing']} of {cov['distinct_criteria']} trait-offer criteria match no "
                                 f"trait value in the store ({'traits table is empty -- run traits.command' if n_tok == 0 else 'casing or spelling differs from the metadata'})")
        except Exception as exc:  # noqa: BLE001 - the audit must still report checksums
            notes.append(f"criteria coverage check failed: {type(exc).__name__}: {exc}")
        # Crossed standing book, for EVERY watched collection (tech-lead re-review
        # 2026-09-10, item 5). A crossed book -- best ask below best collection
        # offer at some instant -- is a reconstruction defect: a stale ask, a
        # misparsed price, a left-truncated leg. It is never an arbitrage, and
        # docs/05 rule 5 says a surprisingly good result is evidence of a bug.
        #
        # Before this, the alarm existed in exactly two places, and neither of
        # them is where an operator looks: a `negative_buckets` integer in the
        # basis of the ONE collection currently selected on the Prices panel, and
        # one log line per process. Health is the panel that is meant to say "the
        # record is wrong", so the count belongs here, for every slug on the
        # watchlist, whether or not it is the one on screen.
        crossed: dict[str, Any] = {}
        try:
            with self.lock:
                for slug in self.slugs:
                    b = self.engine.series(metric="immediacy_cost", collection=slug,
                                           interval="1h", range_="24h",
                                           book="standing")["basis"]
                    crossed[slug] = {"negative_buckets": int(b.get("negative_buckets") or 0),
                                     "interval": "1h", "range": "24h",
                                     "buckets": b.get("buckets")}
            for slug, c in crossed.items():
                if c["negative_buckets"] > 0:
                    notes.append(
                        f"{slug}: {c['negative_buckets']} bucket(s) had ask < collection offer in the "
                        f"last 24h — book reconstruction bug, escalate (docs/05 rule 5)")
        except Exception as exc:  # noqa: BLE001 - the audit must still report checksums
            notes.append(f"crossed-book check failed: {type(exc).__name__}: {exc}")
        return {"at": _now_iso(), "mode": "shallow (checksums; run status.command for the deep audit)",
                "failures": [p for p in problems if is_integrity_failure(p)],
                "notes": notes, "criteria_coverage": coverage, "crossed_book": crossed}

    # -- PR-10: is the store itself still readable? ---------------------------
    def store_quick_check(self) -> dict[str, Any]:
        """`PRAGMA quick_check`, at most once every QUICK_CHECK_TTL_SECONDS.

        Corruption in the analytical store is the one fault every other number
        on this page is downstream of, and until BUG-20260910-067 nothing looked
        for it: the store was found malformed by a crash loop, not by a check.
        `quick_check` is the cheap half of `integrity_check` -- it verifies page
        structure and index content without the (much slower) cross-index
        consistency pass -- but it still reads every page, so it is cached rather
        than run per request, and the response says how old the answer is.

        A corrupt store makes the PRAGMA itself raise. That is reported as a
        failed check, never as a 500: an operator whose store is malformed needs
        the Health page to load and say so. When the store could not be opened at
        ALL -- the degraded case -- there is no `norm.conn` to ask, so this opens
        a throwaway read-only connection and lets it fail, which is the honest
        answer rather than a check that quietly does not run.
        """
        now = time.monotonic()
        cached = self._quick_check
        if cached is not None and (now - self._quick_check_mono) < QUICK_CHECK_TTL_SECONDS:
            return {**cached, "cached": True, "age_seconds": round(now - self._quick_check_mono, 1)}
        t0 = time.monotonic()
        try:
            # quick_check(1) stops at the first fault: we need to know THAT the
            # store is broken, not to enumerate every broken page.
            if self.norm is not None:
                with self.lock:
                    rows = self.norm.conn.execute("PRAGMA quick_check(1)").fetchall()
            else:
                probe = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
                try:
                    rows = probe.execute("PRAGMA quick_check(1)").fetchall()
                finally:
                    probe.close()
            result = [str(r[0]) for r in rows] or ["(no result)"]
            ok = result == ["ok"]
        except Exception as exc:  # noqa: BLE001 - a malformed store must still render
            result, ok = [f"{type(exc).__name__}: {exc}"], False
        entry = {
            "ok": ok, "result": result[:5], "at": _now_iso(),
            "took_ms": round((time.monotonic() - t0) * 1000),
            "ttl_seconds": QUICK_CHECK_TTL_SECONDS,
            "note": "PRAGMA quick_check reads every page, so it runs at most once per "
                    f"{QUICK_CHECK_TTL_SECONDS // 60} minutes and this answer may be that old. "
                    "'ok' means the store's pages and indexes are structurally sound; it is not "
                    "a statement about whether the FOLD is correct.",
        }
        if not ok:
            entry["rebuild"] = rebuild_recipe(self.db_path)
        self._quick_check, self._quick_check_mono = entry, time.monotonic()
        return {**entry, "cached": False, "age_seconds": 0.0}

    # -- PR-9: ledger, wallets, health ---------------------------------------
    def _ledger_filters(self, q: dict[str, str]) -> dict[str, Any]:
        """The ledger's filter row, parsed once for the table, the count and the chart.

        Every refusal names the field. A filter this cannot read is a 400, never a
        filter quietly dropped: a dropped filter returns MORE rows than asked for
        and the extra ones look like data.
        """
        s, e = self._window(q)
        if q.get("start") or q.get("end"):
            fs, ts = iso_to_ts(q.get("start")), iso_to_ts(q.get("end"))
            if q.get("start") and fs is None:
                raise ValueError(f"start must be an ISO timestamp; got {q['start']!r}")
            if q.get("end") and ts is None:
                raise ValueError(f"end must be an ISO timestamp; got {q['end']!r}")
            s, e = (fs if fs is not None else s), (ts if ts is not None else e)
        band: dict[str, float | None] = {"price_min": None, "price_max": None}
        if q.get("price_band"):
            parts = q["price_band"].split(":")
            if len(parts) != 2:
                raise ValueError("price_band must be 'min:max' in ETH, either side may be empty")
            try:
                band = {"price_min": float(parts[0]) if parts[0] else None,
                        "price_max": float(parts[1]) if parts[1] else None}
            except ValueError as exc:
                raise ValueError(f"price_band is not numeric: {q['price_band']!r}") from exc
        types = [t for t in (q.get("type") or "").split(",") if t]
        return {"start": s, "end": e,
                "traits": parse_trait_filter(q.get("traits")),
                "event_types": types or None,
                "token": q.get("token") or None,
                "maker": q.get("maker") or None,
                "taker": q.get("taker") or None,
                "order_hash": q.get("order_hash") or None,
                "has_price": q.get("has_price") in ("1", "true", "yes"),
                **band}

    def api_ledger(self, q: dict[str, str]) -> dict[str, Any]:
        f = self._ledger_filters(q)
        slug = self._slug(q)
        with self.lock:
            if q.get("mode") == "chart":
                return self.engine.ledger_chart(slug, cap=int(q.get("cap") or 0) or None, **f)
            return self.engine.ledger(slug, sort=q.get("sort", "valid_ts"),
                                      direction=q.get("dir", "desc"),
                                      cursor=q.get("cursor") or None,
                                      limit=int(q.get("limit", "200")), **f)

    def api_wallets(self, q: dict[str, str]) -> dict[str, Any]:
        s, e = self._window(q)
        slug = self._slug(q)
        with self.lock:
            out = self.engine.wallets(slug, s, e, limit=int(q.get("limit", "50")),
                                      min_events=int(q.get("min_events", "0")))
            out["adjacency"] = self.engine.counterparty_adjacency(
                slug, s, e, limit=min(40, int(q.get("adjacency_limit", "40"))))
        return out

    def api_wallet(self, q: dict[str, str], address: str) -> dict[str, Any]:
        if not address:
            raise ValueError("/api/wallet/<address> needs an address")
        s, e = self._window(q)
        with self.lock:
            return self.engine.wallet(self._slug(q), address, s, e)

    def _degraded_health(self) -> dict[str, Any]:
        """Health when the analytical store could not be opened at all.

        Same top-level shape as `api_health`, so the view renders with no special
        case and no undefined field -- every store-derived count is `None` rather
        than 0, because 0 is a claim about a store nobody could read. What it adds
        is `degraded`, which carries the fault and the rebuild recipe.

        Everything here comes from somewhere OTHER than the analytical store: the
        recorder's own lock file, the landing zone's manifests, and the store's
        lock file and size on disk. That is the point -- the store being gone must
        not take the answers with it (BUG-20260910-067, tech-lead B1).
        """
        unavailable = self.store_unavailable()
        gaps = self.api_gaps()
        try:
            problems = verify_manifest(self.landing, deep=False)
            failures = [p for p in problems if is_integrity_failure(p)]
            notes = [p for p in problems if not is_integrity_failure(p)]
        except Exception as exc:  # noqa: BLE001 - the page must render regardless
            failures, notes = [], [f"landing-zone audit failed: {type(exc).__name__}: {exc}"]
        notes = [*notes, "the ANALYTICAL store is unreadable; the landing zone above is a "
                         "separate, append-only store and this audit is about the landing zone"]
        store_bytes = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                store_bytes += p.stat().st_size
        landing_bytes = (sum(f.stat().st_size for f in self.landing.rglob("*") if f.is_file())
                         if self.landing.exists() else 0)
        counts = {t: None for t in ("events", "order_lives", "order_criteria", "unparsed",
                                    "tokens", "traits", "watermarks")}
        return {
            "at": _now_iso(), "display_timezone": self.tz, "watchlist": self.slugs,
            "degraded": unavailable,
            "recorder": self._recorder_state(),
            "gaps": {"open": len(self.store.open_gaps()),
                     "awaiting_backfill": len(self.store.unbackfilled_gaps()),
                     "recent": gaps[-10:][::-1], "total_recorded": len(gaps)},
            "integrity": {"at": _now_iso(),
                          "mode": "landing zone only (the analytical store is unreadable)",
                          "failures": failures, "notes": notes},
            "crossed_book": {"by_collection": {}, "alarms": None,
                             "note": "not measurable: the crossed-book check reads the "
                                     "analytical store, which is unreadable"},
            "criteria_coverage": {},
            "normalizer": {
                "last_fold_at": None, "took_ms": None,
                "error": unavailable["detail"], "refresh_seconds": self.refresh,
                "files_checked": None, "files_read": None,
                "files_failed": None, "files_short": None,
                "rows_added": None, "unparsed": None, "last_error": unavailable["detail"],
                "files_note": "no fold has run: the analytical store could not be opened. "
                              "The recorder is unaffected and is still landing frames; "
                              "rebuilding re-folds every one of them.",
                "retrying_every_seconds": STORE_REOPEN_SECONDS,
            },
            "store": {**counts, "bytes": store_bytes, "path": str(self.db_path),
                      "landing_bytes": landing_bytes,
                      "last_observed_at": None, "last_valid_at": None,
                      "events_last_60s": None},
            "store_writer": store_writer_info(self.db_path),
            "quick_check": self.store_quick_check(),
            "dedup": self.api_dedup(),
            "lives_method": self.api_lives_method(),
            "status": "warn",
            "ethusd": {"rate": None, "at": None,
                       "provider": "not readable: the analytical store is unreadable"},
        }

    def api_dedup(self) -> dict[str, Any]:
        """The redundant stream's duplicate picture. Always present, flag or no flag.

        WHY THIS EXISTS AT ALL (E-W5). `events` is a faithful fold of BOTH
        append-only landing zones, so under a redundant stream the same event is
        two rows there, and the same trait offer is two sets of rows in
        `order_criteria` -- whose primary key is `(run, seq, idx)`, which is
        per-connection by construction. The COVERS join still resolves correctly,
        so nothing is WRONG; but any raw count over `order_criteria` DOUBLES, and
        the first "trait offers by criteria" figure read off it would be 2x. This
        block is what stops that number being read raw: it says whether a second
        connection is on, how many rows each connection contributed, and what the
        de-duplicated criteria count is.

        Dedup itself is resolved in `order_lives`, not here and not in a view
        (E-W4/C8) -- this endpoint only MEASURES it.

        Every fraction below carries its numerator and its denominator, and is
        None rather than 0.0 when there is nothing to divide by.
        """
        s = self.redundant
        out: dict[str, Any] = {
            "enabled": s.enabled,
            "label_b": s.label,
            "landing_roots": {lbl: str(p) for lbl, p in self.landing_roots},
            "dedup_key_fields": list(DEDUP_KEY_FIELDS),
            "order_criteria_note":
                ("`order_criteria` has a PER-CONNECTION primary key (run, seq, idx), so with a "
                 "second connection running every criteria count over it is DOUBLE. Read "
                 "`criteria_rows_deduped` below, never a raw COUNT(*) over order_criteria "
                 "(E-W5)."),
        }
        if self.norm is None:
            return {**out, "available": False,
                    "note": "the analytical store is unreadable; no duplicate measurement is possible"}
        with self.lock:
            c = self.norm.conn
            counts = redundancy.dedup_counts(
                c, window_seconds=s.window_seconds, now_ts=time.time(), label_b=s.label)
            out["monitor"] = self.dup_monitor.evaluate(counts)
            row = c.execute(
                "SELECT COUNT(*), SUM(dedup_key IS NULL) FROM events").fetchone()
            out["store_total_events_n"] = row[0] or 0
            out["store_undedupable_n"] = row[1] or 0
            # A row folded before PR-10 existed has no dedup_key and no `conn`,
            # and `valid_at` was ALREADY coalesced with `sent_at`, so it cannot
            # say whether it carried an `event_timestamp`. Those rows are counted
            # separately: adding them to the un-dedupable rate would report a
            # migration as a property of the market.
            out["pre_migration_rows_n"] = c.execute(
                "SELECT COUNT(*) FROM events WHERE conn IS NULL").fetchone()[0]
            out["criteria_rows_raw_n"] = c.execute(
                "SELECT COUNT(*) FROM order_criteria").fetchone()[0]
            out["criteria_rows_deduped_n"] = c.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT e.dedup_key, oc.idx "
                "  FROM order_criteria oc JOIN events e ON e.run=oc.run AND e.seq=oc.seq "
                " WHERE e.dedup_key IS NOT NULL) "
            ).fetchone()[0] + c.execute(
                "SELECT COUNT(*) FROM order_criteria oc JOIN events e "
                "  ON e.run=oc.run AND e.seq=oc.seq WHERE e.dedup_key IS NULL").fetchone()[0]
            out["lives_duplicates_merged_n"] = c.execute(
                "SELECT COALESCE(SUM(duplicates_merged), 0) FROM order_lives").fetchone()[0]
            out["lives_terminations_undedupable_n"] = c.execute(
                "SELECT COALESCE(SUM(terminations_undedupable), 0) FROM order_lives").fetchone()[0]
            # Where the one-row-per-key fold could not tell a duplicate from a
            # repeat delivery, store-wide (BUG-20260911-073). The windowed figure
            # is in `monitor`; this is the running total, which is the one that
            # answers "how much of this store is affected at all".
            out["lives_multiplicity_disagreements_n"] = c.execute(
                "SELECT COALESCE(SUM(multiplicity_disagreements), 0) FROM order_lives").fetchone()[0]
        # Coverage annotations currently standing, and how many this process has had
        # to TAKE BACK. A revocation means a gap was briefly marked "the other
        # connection covered this" on evidence that later proved wrong -- the
        # SIGKILL race in BUG-20260911-077. A non-zero count here is not an error;
        # it is the correction working, and it is worth seeing.
        out["covered_by_n"] = len([g for g in self.store.all_gaps() if g.get("covered_by")])
        out["covered_by_revoked_since_start_n"] = self.covered_by_revoked
        out["covered_by_revoked_scope"] = "this dashboard process; not persisted, resets on restart"
        out["available"] = True
        return out

    def api_lives_method(self) -> dict[str, Any]:
        """Which fold rules produced the rows in `order_lives` (BUG-20260911-076).

        `ORDER_LIVES_METHOD` was written on every row and read by nothing, so a
        store upgraded in place carried rows from two different folds with no
        signal anywhere. The folding WRITER re-folds on open. A READER cannot --
        a second writer on one SQLite store is how `analytics.sqlite` became
        "database disk image is malformed" on 2026-09-10 -- so it reports instead,
        and Health goes to `warn` with a sentence naming the fix.
        """
        if self.norm is None:
            return {"available": False, "mixed": False,
                    "note": "the analytical store is unreadable; the fold's method version "
                            "cannot be read either"}
        with self.lock:
            st = self.norm.lives_method()
        out = {"available": True, **st,
               "refold_on_open": self.norm.refold_stats.get("refolded", False),
               "refold_seconds": self.norm.refold_stats.get("seconds", 0.0)}
        if st["mixed"]:
            out["action_required"] = (
                f"order_lives holds rows folded by method_version "
                f"{st['min'] if not st['null_rows'] else 'NULL'}-{st['max']} and this build "
                f"writes {st['current']}. Rows from the older rules can carry counts the "
                f"current rules would not produce -- under method 2, a doubled "
                f"terminations_seen on any order with no further events. "
                f"Run rebuild-store.command or restart the dashboard.")
        return out

    def api_health(self, q: dict[str, str]) -> dict[str, Any]:
        """Everything the Health view asks: can I trust the other tabs?

        This endpoint does no measuring of its own. It aggregates what
        `api_status`, `api_gaps` and `api_audit` already produce, plus the two
        numbers nothing else surfaces -- how big the store is on disk, and when
        the last fold happened -- so the view is one request rather than four and
        cannot render three of them and silently drop the fourth.

        `files_failed` / `files_short` are hoisted out of the normalizer's stats
        onto the top level because they are the BUG-058 signature: "115 files
        read, 0 rows added" with no error anywhere an operator looks. A count
        that lives three keys deep in a status blob is a count nobody reads.
        """
        if self.norm is None:
            return self._degraded_health()
        status = self.api_status()
        audit = self.api_audit()
        gaps = self.api_gaps()
        stats = (status.get("normalizer") or {}).get("stats") or {}
        db = self.root / self.cfg["analytical"]["path"]
        store_bytes = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                store_bytes += p.stat().st_size
        crossed = audit.get("crossed_book") or {}
        dedup = self.api_dedup()
        lives_method = self.api_lives_method()
        with self.lock:
            counts = {t: self.norm.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("events", "order_lives", "order_criteria", "unparsed",
                                "tokens", "traits", "watermarks")}
        return {
            "at": _now_iso(), "display_timezone": self.tz, "watchlist": self.slugs,
            "recorder": status["recorder"],
            "gaps": {"open": status["gaps"]["open"],
                     "awaiting_backfill": status["gaps"]["awaiting_backfill"],
                     "recent": gaps[-10:][::-1], "total_recorded": len(gaps)},
            "integrity": {"at": audit["at"], "mode": audit["mode"],
                          "failures": audit["failures"], "notes": audit["notes"]},
            "crossed_book": {"by_collection": crossed,
                             "alarms": sum(int(c.get("negative_buckets") or 0) for c in crossed.values()),
                             "note": "a crossed standing book (best ask below best collection offer) is a "
                                     "reconstruction defect, never an arbitrage (docs/05 rule 5)"},
            "criteria_coverage": audit.get("criteria_coverage") or {},
            "normalizer": {
                "last_fold_at": (status.get("normalizer") or {}).get("at"),
                "took_ms": (status.get("normalizer") or {}).get("took_ms"),
                "error": (status.get("normalizer") or {}).get("error"),
                "refresh_seconds": self.refresh,
                "files_checked": stats.get("files_checked"), "files_read": stats.get("files_read"),
                "files_failed": stats.get("files_failed"), "files_short": stats.get("files_short"),
                "rows_added": stats.get("rows_added"), "unparsed": stats.get("unparsed"),
                "last_error": stats.get("last_error"),
                "files_note": "files_failed = could not be read at all (usually a codec missing on "
                              "the reading machine); files_short = the manifest records more frames "
                              "than we read back. Either one means rows are missing and no other "
                              "number on this page will say so (BUG-058).",
            },
            "store": {**counts, "bytes": store_bytes, "path": str(db),
                      "landing_bytes": status["landing_bytes"],
                      "last_observed_at": status["store"]["last_observed_at"],
                      "last_valid_at": status["store"]["last_valid_at"],
                      "events_last_60s": status["store"]["events_last_60s"]},
            # Who owns the store's write side, and whether the store is still
            # readable at all (BUG-20260910-067). Both are top-level: the writer
            # identity is the thing an operator needs when a second dashboard is
            # suspected, and a quick_check buried in `store` is a check nobody reads.
            "store_writer": self.norm.writer_info(),
            "quick_check": self.store_quick_check(),
            # BUG-20260911-076. `order_lives` rows are stamped with the fold rules
            # that made them. A store opened READ-ONLY cannot re-fold -- that would
            # be a second writer (BUG-20260910-067) -- so when it finds rows from an
            # older rule set it has to SAY so rather than serve them silently. Under
            # method 2 a stale row carries a doubled `terminations_seen`.
            "lives_method": lives_method,
            "dedup": dedup,
            # count over `events` or `order_criteria` doubles the moment it IS on
            # and nothing else on this page would say so (E-W5). `status` is
            # 'warn' when duplicates have collapsed while both connections are
            # healthy -- the flattering-direction failure (dataeng failure mode 3).
            "status": ("warn" if lives_method.get("mixed")
                       else (dedup.get("monitor") or {}).get("status", "ok")),
            "ethusd": status["ethusd"],
        }

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
        "/api/trait_series": dash.api_trait_series,
        "/api/book": dash.api_book,
        "/api/tape": dash.api_tape,
        "/api/makers": dash.api_makers,
        "/api/lifetimes": dash.api_lifetimes,
        "/api/survival": dash.api_survival,
        "/api/survival_drill": dash.api_survival_drill,
        "/api/mix": dash.api_mix,
        "/api/gaps": lambda q: dash.api_gaps(),
        "/api/traits": dash.api_traits,
        "/api/screener": dash.api_screener,
        "/api/audit": lambda q: dash.api_audit(),
        "/api/ledger": dash.api_ledger,
        "/api/wallets": dash.api_wallets,
        "/api/health": dash.api_health,
    }
    # `/api/wallet/<address>` is the one route with a path parameter. Kept out of
    # `routes` rather than bent into it: the address is data and belongs in the
    # path per design §8.2.3, and a prefix match here is clearer than a regex
    # table for one entry.
    WALLET_PREFIX = "/api/wallet/"

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
            handler = routes.get(u.path)
            if handler is None and u.path.startswith(WALLET_PREFIX):
                from urllib.parse import unquote
                addr = unquote(u.path[len(WALLET_PREFIX):])
                handler = lambda qq, a=addr: dash.api_wallet(qq, a)  # noqa: E731
            if handler is not None:
                # DEGRADED: the analytical store could not be opened. Everything
                # except DEGRADED_ROUTES reads it, so those answer 503 -- "I
                # cannot answer this right now, and here is why and what to do"
                # -- rather than a 500 with a sqlite traceback, or worse, a page
                # that is simply blank. The page's own fetch helper renders
                # `error` per panel, so Health and the shell still draw.
                # BUG-20260910-067, tech-lead B1.
                if dash.degraded() and u.path not in DEGRADED_ROUTES:
                    self._send(503, json.dumps(dash.store_unavailable(), default=str).encode(),
                               "application/json")
                    return
                try:
                    payload = handler(q)
                    self._send(200, json.dumps(payload, default=str).encode(), "application/json")
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                except sqlite3.DatabaseError as exc:
                    # The store went bad UNDER a request. Same answer as above,
                    # and the next `_loop` tick will find it and degrade properly.
                    log.error("the analytical store failed during a request: %s", exc)
                    body = {**dash.store_unavailable(), "detail": f"{type(exc).__name__}: {exc}"}
                    self._send(503, json.dumps(body, default=str).encode(), "application/json")
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
    """Bind the port FIRST, open the store second. The order is the whole point.

    BUG-20260910-067. This used to construct `Dashboard(...)` -- which opens
    `data/analytics.sqlite` and starts the thread that WRITES to it -- and only
    then bind 127.0.0.1:8765. With a second dashboard already listening, every
    launchd retry of this one opened the store, folded new frames into it, and
    then died on "Address already in use". 177 retries, ten seconds apart, two
    writers alternating on one SQLite store with processes killed mid-write, and
    a 2.8 GB store that ended as "database disk image is malformed".

    Binding first makes the doomed retry cost nothing: the OSError is raised
    before a single byte of the store is opened, let alone written.
    """
    dcfg = cfg.get("dashboard") or {}
    host = host or dcfg.get("host", "127.0.0.1")
    port = port if port is not None else int(dcfg.get("port", 8765))
    if host not in ("127.0.0.1", "localhost", "::1"):
        # REQ-N-13. Refuse rather than warn: a dashboard over the whole
        # landing zone bound to 0.0.0.0 is the exposure the requirement exists to prevent.
        raise ValueError(f"dashboard host must be loopback (REQ-N-13); got {host!r}")
    # The handler class is a placeholder: it cannot be built until the Dashboard
    # exists, and the Dashboard must not exist until the bind has succeeded. The
    # socket is listening from here on, so a connection that arrives during the
    # first fold waits in the backlog rather than being refused.
    httpd = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)
    dash: Dashboard | None = None
    try:
        # `Dashboard(...)` degrades rather than raising on a MALFORMED store
        # (see `_open_store`): it comes up with `norm=None` and serves Health.
        # It still raises StoreWriterBusyError if another process owns the store.
        dash = Dashboard(root, cfg, slugs)
        httpd.RequestHandlerClass = make_handler(dash)
        dash.start()
    except BaseException:
        # tech-lead B2. Closing the socket is not enough: if the failure came
        # AFTER the Normalizer opened -- `make_handler`, `start()`, a
        # KeyboardInterrupt during the first fold -- the store's writer lock is
        # held by an object nobody will ever close, and a retry in this same
        # process would be refused by our own stale lock.
        httpd.server_close()
        if dash is not None and dash.norm is not None:
            try:
                dash.norm.close()
            except Exception as exc:  # noqa: BLE001 - never mask the original failure
                log.warning("closing the analytical store after a failed start also failed: "
                            "%s: %s", type(exc).__name__, exc)
        raise
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"dashboard    {url}   (localhost only; ctrl-c to stop)")
    print(f"store        {root / cfg['analytical']['path']}")
    if dash.degraded():
        err = dash.store_error or {}
        print("")
        print("*** DEGRADED: the analytical store could not be opened ***")
        print(f"    {err.get('error')}")
        print(f"    {err.get('rebuild')}")
        print("    The page and Health still load; every data panel will say the same thing.")
        print(f"    Retrying the store every {STORE_REOPEN_SECONDS:.0f}s -- rebuild it and the "
              f"page comes back on its own, no restart needed.")
        print("")
    else:
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
        # Each step guarded: an impatient second Ctrl-C partway through must not
        # skip `norm.close()`, which is what checkpoints the WAL and releases the
        # store's writer lock (BUG-20260910-067).
        # `norm` may be None (degraded) or may have been opened by a retry while
        # we were serving -- read it here, not at the top of `serve()`.
        steps = [dash.stop, httpd.server_close]
        if dash.norm is not None:
            steps.append(dash.norm.close)
        for step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - shut every part down we can
                log.warning("shutdown step %s failed: %s: %s",
                            getattr(step, "__name__", step), type(exc).__name__, exc)
            except KeyboardInterrupt:
                log.warning("interrupted during shutdown; continuing to close the store")
