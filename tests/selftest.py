"""Standard-library-only self-test for the Phase 0 core.

Runnable with a bare `python3 tests/selftest.py` -- no pytest, no zstandard, no
websockets. It exercises the logic that is actually tricky (rolling, frame
flushing, crash recovery, manifest integrity, ordering, budget arithmetic) using
the gzip codec, which is a genuinely framed format and therefore a faithful
stand-in for zstd's behaviour.

The zstd and websockets bindings are thin wrappers around this logic; the logic
is what is worth testing here.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from navanax.codec import GzipCodec  # noqa: E402
from navanax.errors import NavanaxError  # noqa: E402
from navanax.governor import Priority, RestGovernor, TokenBucket  # noqa: E402
from navanax.landing import (  # noqa: E402
    LandingZoneWriter,
    is_integrity_failure,
    read_file,
    verify_manifest,
)
from navanax.opstore import OperationalStore  # noqa: E402
from navanax.stream import StreamConsumer, normalize_frame  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    suffix = f" -- {detail}" if detail and not cond else ""
    PASS.append(name) if cond else FAIL.append(f"{name}{suffix}")
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"\n        {detail}" if detail and not cond else ""))


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.t = start
        self.mono = 0.0

    def now(self) -> datetime:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)
        self.mono += seconds


def frame(slug: str, ets: str, event: str = "item_listed",
          price: str = "530000000000000000") -> str:
    return json.dumps(
        {
            "topic": f"collection:{slug}",
            "event": event,
            "payload": {
                "event_type": event,
                "payload": {
                    "collection": {"slug": slug},
                    "base_price": price,
                    "event_timestamp": ets,
                    "item": {"nft_id": f"ethereum/0xabc/{ets[-2:]}"},
                },
            },
            "ref": None,
        },
        separators=(",", ":"),
    )


class FakeWriter:
    """Stand-in for LandingZoneWriter with the full interface the consumer uses."""

    run_id = "run-fake"

    def __init__(self) -> None:
        self.landed: list[tuple[str, str | None, str | None]] = []
        self.control: list[str] = []
        self.gaps: list[Any] = []
        self.flushes = 0

    @property
    def sequence(self) -> int:
        return len(self.landed)

    def write(self, raw, topic=None, event_timestamp=None, received_at=None,
              control=False):
        self.landed.append((raw, topic, event_timestamp))
        if control:
            self.control.append(raw)
        return len(self.landed)

    def flush(self) -> None:
        self.flushes += 1

    def record_gap(self, gap) -> None:
        self.gaps.append(gap)

    def close_gap_record(self, gap, ended_at) -> None:
        gap.ended_at = ended_at
        self.gaps.append(gap)


# ---------------------------------------------------------------------------
def test_roundtrip_and_verbatim(tmp: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 9, 2, 15, 0, tzinfo=timezone.utc))
    w = LandingZoneWriter(tmp / "landing", "run-a", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic, flush_events=3)
    raws = [frame("argonauts", f"2026-09-09T02:15:{i:02d}Z") for i in range(10)]
    for r in raws:
        w.write(r, topic="collection:argonauts",
                event_timestamp=json.loads(r)["payload"]["payload"]["event_timestamp"])
    w.close()

    files = sorted((tmp / "landing" / "stream").rglob("*.jsonl.gz"))
    check("landing: one file written", len(files) == 1, f"got {len(files)}")

    envs = list(read_file(files[0], GzipCodec()))
    check("landing: all events readable", len(envs) == 10, f"got {len(envs)}")
    check("landing: raw stored VERBATIM", [e["raw"] for e in envs] == raws,
          "re-serialisation would reorder keys and break reprocessing")
    check("landing: sequence is monotonic from 1",
          [e["_seq"] for e in envs] == list(range(1, 11)))
    check("landing: envelope carries run id", all(e["_run"] == "run-a" for e in envs))


def test_crash_recovery(tmp: Path) -> None:
    """The property REQ-D-26a exists to provide.

    Write events, flush frames, then TRUNCATE the file mid-frame -- simulating a
    process killed while writing. Every event in a completed frame must survive.
    """
    clock = FakeClock(datetime(2026, 9, 9, 3, 0, 0, tzinfo=timezone.utc))
    w = LandingZoneWriter(tmp / "crash", "run-b", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic,
                          flush_events=5, flush_seconds=1e9)
    for i in range(12):
        w.write(frame("argonauts", f"2026-09-09T03:00:{i:02d}Z"),
                topic="collection:argonauts",
                event_timestamp=f"2026-09-09T03:00:{i:02d}Z")
    w.flush()   # frames now closed for the first 10; 2 remain buffered
    path = sorted((tmp / "crash" / "stream").rglob("*.jsonl.gz"))[0]
    blob = path.read_bytes()
    w.close()   # V13: a writer that is never closed leaks its flusher thread

    # Simulate a kill mid-write: lop off the tail.
    truncated = blob[: int(len(blob) * 0.8)]
    victim = tmp / "crash_truncated.jsonl.gz"
    victim.write_bytes(truncated)

    recovered = list(read_file(victim, GzipCodec(), tolerate_truncation=True))
    check("crash recovery: truncated file still yields complete frames",
          len(recovered) > 0,
          "a naive single-frame stream would yield ZERO here -- this is the "
          "whole point of the frame-flush cadence")
    check("crash recovery: recovered events are well-formed",
          all("raw" in e and "_seq" in e for e in recovered))
    seqs = [e["_seq"] for e in recovered]
    check("crash recovery: recovered prefix is contiguous from 1",
          seqs == list(range(1, len(seqs) + 1)), f"got {seqs}")


def test_hour_rolling(tmp: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 9, 4, 59, 30, tzinfo=timezone.utc))
    w = LandingZoneWriter(tmp / "roll", "run-c", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic)
    w.write(frame("argonauts", "2026-09-09T04:59:30Z"), event_timestamp="2026-09-09T04:59:30Z")
    clock.advance(60)  # crosses the hour boundary
    w.write(frame("argonauts", "2026-09-09T05:00:30Z"), event_timestamp="2026-09-09T05:00:30Z")
    w.close()
    hours = sorted(p.name for p in (tmp / "roll" / "stream" / "dt=2026-09-09").iterdir())
    check("rolling: new file per UTC hour", hours == ["hour=04", "hour=05"], f"got {hours}")


def test_manifest_integrity(tmp: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 9, 6, 0, 0, tzinfo=timezone.utc))
    root = tmp / "manifest"
    w = LandingZoneWriter(root, "run-d", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic)
    for i in range(5):
        w.write(frame("argonauts", f"2026-09-09T06:00:{i:02d}Z"),
                event_timestamp=f"2026-09-09T06:00:{i:02d}Z")
    w.close()

    check("manifest: verifies clean", verify_manifest(root) == [])

    mf = json.loads((root / "_manifest" / "2026-09-09.json").read_text())
    rec = mf["files"][0]
    check("manifest: records event count", rec["event_count"] == 5)
    check("manifest: records sha256", bool(rec["sha256"]))
    check("manifest: records event-time span",
          rec["first_event_timestamp"] == "2026-09-09T06:00:00Z"
          and rec["last_event_timestamp"] == "2026-09-09T06:00:04Z")

    # Tamper -- exactly what the weekly audit must catch.
    victim = root / rec["filename"]
    victim.write_bytes(victim.read_bytes() + b"\x00")
    problems = verify_manifest(root)
    check("manifest: DETECTS tampering (S0a)",
          len(problems) == 1 and "CHECKSUM MISMATCH" in problems[0], f"got {problems}")


def test_event_time_range_resolution(tmp: Path) -> None:
    """Late-arriving events must not be clipped by directory-name selection."""
    clock = FakeClock(datetime(2026, 9, 9, 8, 0, 0, tzinfo=timezone.utc))
    root = tmp / "range"
    w = LandingZoneWriter(root, "run-e", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic)
    # Arrives in hour 08 but happened in hour 07 -- the out-of-order case.
    w.write(frame("argonauts", "2026-09-09T07:30:00Z"), event_timestamp="2026-09-09T07:30:00Z")
    w.write(frame("argonauts", "2026-09-09T08:00:05Z"), event_timestamp="2026-09-09T08:00:05Z")
    w.close()
    hits = w.manifest.files_for_event_range("2026-09-09T07:00:00Z", "2026-09-09T07:59:59Z")
    check("manifest: event-time range finds the late-arriving event",
          len(hits) == 1 and hits[0]["hour"] == "08",
          "resolving by directory name would have missed this entirely")


def test_gap_recording(tmp: Path) -> None:
    store = OperationalStore(tmp / "ops.db")
    gid = store.open_gap("run-f", "connection reset", ["collection:argonauts"])
    check("gaps: opens", len(store.open_gaps()) == 1)
    store.close_gap(gid)
    check("gaps: closes", len(store.open_gaps()) == 0)
    check("gaps: closed gap awaits backfill", len(store.unbackfilled_gaps()) == 1)

    store.save_checkpoint("opensea:argonauts", "run-f", 42, "2026-09-09T09:00:00Z")
    cp = store.get_checkpoint("opensea:argonauts")
    check("checkpoint: round-trips", cp is not None and cp["last_seq"] == 42)

    store.upsert_onboarding("argonauts", state="tokens", items_total=9210, requests_est=94)
    store.upsert_onboarding("argonauts", items_done=4000, requests_spent=21)
    st = store.onboarding_status()[0]
    check("onboarding: progress tracked",
          st["items_total"] == 9210 and st["items_done"] == 4000 and st["state"] == "tokens")


def test_dotenv_loading(tmp: Path) -> None:
    """Regression for BUG-20260909-001: entry points never read .env."""
    import os

    from navanax.dotenv import DotenvError, load, parse, require

    check("dotenv: strips quotes", parse('K="v"')["K"] == "v")
    check("dotenv: handles export prefix", parse("export K=v")["K"] == "v")
    check("dotenv: strips inline comment", parse("K=v # note")["K"] == "v")
    check("dotenv: strips whitespace", parse("K =  v  ")["K"] == "v")
    check("dotenv: skips comments/blanks", parse("# c\n\nK=v") == {"K": "v"})

    d = tmp / "envtest"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".env").write_text("OPENSEA_API_KEY=fake_key_value\n")
    os.environ.pop("OPENSEA_API_KEY", None)
    check("dotenv: require() reads .env when env var absent",
          require("OPENSEA_API_KEY", path=d / ".env") == "fake_key_value",
          "this is the exact bug: a correct .env reported as 'key not set'")

    os.environ["OPENSEA_API_KEY"] = "from_environment"
    check("dotenv: real env var wins over file",
          require("OPENSEA_API_KEY", path=d / ".env") == "from_environment")
    os.environ.pop("OPENSEA_API_KEY", None)

    # The macOS trap: TextEdit saves Rich Text, file looks fine on screen.
    (d / "rtf.env").write_bytes(rb"{\rtf1\ansi OPENSEA_API_KEY=x}")
    try:
        load(d / "rtf.env")
        ok = False
    except DotenvError as exc:
        ok = "Rich Text" in str(exc) and "Make Plain Text" in str(exc)
    check("dotenv: detects TextEdit RTF and says how to fix it", ok,
          "an RTF .env is invisible in the editor and must be caught by bytes")

    (d / "missing").mkdir(exist_ok=True)
    try:
        require("OPENSEA_API_KEY", path=d / "missing" / ".env")
        ok2 = False
    except DotenvError as exc:
        ok2 = "cp .env.example .env" in str(exc)
    check("dotenv: missing file gives an actionable message", ok2)


def test_governor_budget() -> None:
    t = {"v": 0.0}
    bucket = TokenBucket(capacity=120, per_seconds=3600, clock=lambda: t["v"])
    check("governor: starts full", bucket.available() == 120)

    for _ in range(120):
        bucket.try_consume()
    check("governor: exhausts at capacity", bucket.available() == 0)
    check("governor: refuses when empty", bucket.try_consume() is False)

    t["v"] += 30.0  # one token per 30 seconds at the measured 120/hr
    check("governor: refills at 120/hr", abs(bucket.available() - 1.0) < 1e-6,
          f"got {bucket.available()}")

    t["v"] += 36000.0
    check("governor: caps at capacity", bucket.available() == 120)

    bucket.observe_429()
    check("governor: 429 zeroes the local model", bucket.available() == 0)
    check("governor: 429 counted", bucket.state.consecutive_429 == 1)

    bucket.observe_headers({"x-ratelimit-remaining": "17"})
    t["v"] += 36000.0
    check("governor: server header caps local optimism", bucket.available() == 17,
          f"got {bucket.available()}")


def test_governor_priority() -> None:
    """MAINTENANCE must starve before INTERACTIVE."""
    t = {"v": 0.0}
    bucket = TokenBucket(capacity=120, per_seconds=3600, clock=lambda: t["v"])
    gov = RestGovernor(bucket)
    for _ in range(100):
        bucket.try_consume()          # 20 left
    avail = bucket.available()
    check("governor: 20 tokens remain", abs(avail - 20) < 1e-6, f"got {avail}")

    async def scenario() -> tuple[bool, bool]:
        interactive_ok = False
        maintenance_ok = False
        # Either outcome is a legitimate "denied": the wait times out, or the
        # governor raises BudgetExhaustedError. Name both rather than blind-catching.
        #
        # asyncio.TimeoutError and builtin TimeoutError are the SAME class on
        # Python 3.11+ and DIFFERENT classes on 3.10. This file is the
        # stdlib-only smoke test and must run anywhere, so list both; on 3.11+
        # the tuple simply contains a duplicate, which is harmless.
        denied = (TimeoutError, asyncio.TimeoutError, NavanaxError)  # noqa: UP041
        try:
            await asyncio.wait_for(gov.acquire(Priority.INTERACTIVE), timeout=0.3)
            interactive_ok = True
        except denied:
            pass
        try:
            await asyncio.wait_for(gov.acquire(Priority.MAINTENANCE), timeout=0.3)
            maintenance_ok = True
        except denied:
            pass
        return interactive_ok, maintenance_ok

    i_ok, m_ok = asyncio.run(scenario())
    check("governor: INTERACTIVE granted with 20 left", i_ok)
    check("governor: MAINTENANCE starved below its 24-token floor", not m_ok,
          "low-priority work must not consume the last of the budget")


def test_stream_parsing() -> None:
    class NullStore:
        def open_gap(self, *a, **k): return 1
        def close_gap(self, *a, **k): pass
        def save_checkpoint(self, *a, **k): pass
        def get_checkpoint(self, *a, **k): return None

    w = FakeWriter()
    c = StreamConsumer("fake-key", ["argonauts"], w, NullStore())  # type: ignore[arg-type]

    f1 = frame("argonauts", "2026-09-09T10:00:00Z")
    c.handle_frame(f1)
    check("stream: extracts nested event_timestamp",
          w.landed[-1][2] == "2026-09-09T10:00:00Z", f"got {w.landed[-1][2]}")
    check("stream: lands frame verbatim", w.landed[-1][0] == f1)

    # V9: the server answers a heartbeat with a phx_reply on topic "phoenix",
    # NOT with an {"event":"heartbeat"} frame. The old fixture asserted the
    # shape we SEND, so stats.heartbeats was 0 in production and 1 in the test.
    c.handle_frame(json.dumps(["1", "hb", "phoenix", "phx_reply",
                               {"status": "ok", "response": {}}]))
    check("stream: a real phoenix phx_reply IS counted as a heartbeat",
          c.stats.heartbeats == 1,
          "this is the frame the server actually sends")
    check("stream: heartbeat not counted as a market event", c.stats.events == 1)
    check("stream: the frame we SEND is also tolerated",
          c.handle_frame('{"topic":"phoenix","event":"heartbeat","payload":{},"ref":"1"}')
          is not None and c.stats.heartbeats == 2)

    before = len(w.landed)
    c.handle_frame("{not json")
    check("stream: unparseable frame STILL landed", len(w.landed) == before + 1,
          "discarding it would destroy the evidence needed to diagnose it")
    check("stream: the heartbeat was landed as a control frame",
          any(t == "__control__" for _, t, _ in w.landed),
          "control frames are the only on-disk evidence of what we subscribed to")

    c.handle_frame(frame("argonauts", "2026-09-09T09:59:00Z"))   # older -> out of order
    check("stream: out-of-order arrival counted", c.stats.out_of_order == 1)
    check("stream: max event ts unchanged by older event",
          c.stats.max_event_ts == "2026-09-09T10:00:00Z")

    check("stream: subscribes per collection, not wildcard",
          c._topics() == ["collection:argonauts"])
    joins = c.join_messages()
    check("stream: phx_join well-formed",
          json.loads(joins[0])["event"] == "phx_join"
          and json.loads(joins[0])["topic"] == "collection:argonauts")


def test_irrecoverable_classification() -> None:
    from navanax.stream import IRRECOVERABLE

    check("REQ-D-09a: item_received_bid is NOT irrecoverable",
          "item_received_bid" not in IRRECOVERABLE,
          "it is an item-level offer, recoverable via the events endpoint's `offer` type")
    check("REQ-D-09a: cancellations are irrecoverable",
          "item_cancelled" in IRRECOVERABLE)
    check("REQ-D-09a: order invalidations are irrecoverable",
          "order_invalidate" in IRRECOVERABLE and "order_revalidate" in IRRECOVERABLE)


def test_phoenix_v2_arrays() -> None:
    """Regression for BUG-20260909-002: OpenSea speaks Phoenix v2 (arrays)."""
    v1 = {"topic": "collection:argonauts", "event": "item_listed",
          "payload": {"x": 1}, "ref": "1"}
    n1 = normalize_frame(v1)
    check("phoenix: v1 map still normalizes",
          n1 is not None and n1["event"] == "item_listed"
          and n1["topic"] == "collection:argonauts")

    # [join_ref, ref, topic, event, payload] -- the shape that crashed us
    v2 = ["1", "1", "collection:argonauts", "phx_reply",
          {"status": "ok", "response": {}}]
    n2 = normalize_frame(v2)
    check("phoenix: v2 ARRAY normalizes (the crash case)",
          n2 is not None and n2["event"] == "phx_reply"
          and n2["topic"] == "collection:argonauts",
          "this is the exact frame that raised AttributeError on .get()")
    check("phoenix: v2 payload reachable",
          n2 is not None and n2["payload"]["status"] == "ok")

    check("phoenix: junk returns None, not a crash",
          normalize_frame("just a string") is None
          and normalize_frame([1, 2]) is None
          and normalize_frame(None) is None)

    class W:
        run_id = "run-v2"
        def __init__(self): self.landed = []
        def write(self, raw, topic=None, event_timestamp=None, received_at=None):
            self.landed.append((raw, topic, event_timestamp))
            return len(self.landed)
        def flush(self): pass
        def record_gap(self, gap): pass
    class S:
        def open_gap(self, *a, **k): return 1
        def close_gap(self, *a, **k): pass

    w = W()
    c = StreamConsumer("k", ["argonauts"], w, S())  # type: ignore[arg-type]
    ev = json.dumps(["1", None, "collection:argonauts", "item_listed",
                     {"event_type": "item_listed",
                      "payload": {"event_timestamp": "2026-09-09T07:10:00Z"}}])
    c.handle_frame(ev)
    check("phoenix: consumer survives a v2 event frame", c.stats.events == 1)
    check("phoenix: event_timestamp still extracted from v2",
          w.landed[-1][2] == "2026-09-09T07:10:00Z", f"got {w.landed[-1][2]}")
    check("phoenix: topic still extracted from v2",
          w.landed[-1][1] == "collection:argonauts")

    before = len(w.landed)
    c.handle_frame('["only","three","items"]')
    check("phoenix: unrecognised shape STILL LANDED verbatim",
          len(w.landed) == before + 1,
          "never drop bytes we may not be able to fetch again")


# ===========================================================================
# Regression tests for the five blocking findings the Validator raised on
# PR #1. Every one of these fails against the code as it was when the suite
# was 66/66 green -- which is the only thing that makes them worth having.
# ===========================================================================
def test_idle_flush_cadence(tmp: Path) -> None:
    """BUG-20260909-006. REQ-D-26a's TIME-based flush, which did not exist.

    The old `_maybe_flush_frame` was reachable only from `write()`, so the
    5-second cadence fired when the next event arrived rather than when 5
    seconds passed. For a thin collection -- preflight saw ZERO events in 60s
    on Argonauts -- that is the normal regime, and the loss is total.
    """
    clock = FakeClock(datetime(2026, 9, 9, 8, 0, 0, tzinfo=timezone.utc))
    root = tmp / "idle"
    w = LandingZoneWriter(root, "run-idle", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic,
                          flush_seconds=5.0, flush_events=1000, auto_flush=False)

    # One item_cancelled -- REQ-D-09a IRRECOVERABLE, and exactly the event type
    # the live smoke test actually observed on Argonauts.
    w.write(frame("argonauts", "2026-09-09T08:00:00Z", event="item_cancelled"),
            topic="collection:argonauts", event_timestamp="2026-09-09T08:00:00Z")

    path = sorted((root / "stream").rglob("*.jsonl.gz"))[0]
    check("idle flush: nothing on disk before the cadence fires",
          path.stat().st_size == 0)

    clock.advance(7200)          # two hours of silence
    w.tick()                     # the cadence, driven without a write

    size = path.stat().st_size
    check("idle flush: the frame IS closed after flush_seconds with no traffic",
          size > 0,
          "old behaviour: 0 bytes, because the timer only ran inside write()")

    # Simulate SIGKILL: never call close(). Read what a crash would leave.
    recovered = list(read_file(path, GzipCodec(), tolerate_truncation=True))
    check("idle flush: the event survives a kill with no clean shutdown",
          len(recovered) == 1 and "item_cancelled" in recovered[0]["raw"],
          f"recovered {len(recovered)} events; this event is IRRECOVERABLE via REST")

    mf = json.loads((root / "_manifest" / "2026-09-09.json").read_text())
    rec = [f for f in mf["files"] if f["filename"] == str(path.relative_to(root))]
    check("idle flush: the open file is in the manifest before it closes",
          len(rec) == 1 and rec[0]["status"] == "open" and rec[0]["event_count"] == 1,
          f"got {rec}")

    w.close()
    check("idle flush: closing marks the record closed and checksums it",
          json.loads((root / "_manifest" / "2026-09-09.json").read_text())
          ["files"][0]["status"] == "closed")


def test_verify_sees_orphans_and_open_files(tmp: Path) -> None:
    """BUG-20260909-007. verify_manifest walked manifest -> disk only.

    A file on disk that the manifest did not know about was invisible to the
    one audit whose entire job is to notice that something is wrong, and the
    audit returned "verified clean".
    """
    clock = FakeClock(datetime(2026, 9, 9, 9, 0, 0, tzinfo=timezone.utc))
    root = tmp / "orphan"
    w = LandingZoneWriter(root, "run-orph", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic,
                          flush_events=2, auto_flush=False)
    for i in range(4):
        w.write(frame("argonauts", f"2026-09-09T09:00:{i:02d}Z"),
                event_timestamp=f"2026-09-09T09:00:{i:02d}Z")
    w.flush()

    problems = verify_manifest(root)
    check("verify: an unclosed file is reported, not certified clean",
          any(p.startswith("OPEN") for p in problems), f"got {problems}")
    check("verify: an unclosed file is a NOTE, not an integrity failure",
          not any(is_integrity_failure(p) for p in problems), f"got {problems}")

    w.close()
    check("verify: clean after a clean close", verify_manifest(root) == [])

    # Now make a genuine orphan: a real data file the manifest has no record of.
    mpath = root / "_manifest" / "2026-09-09.json"
    data = json.loads(mpath.read_text())
    victim = data["files"][0]["filename"]
    data["files"] = []
    mpath.write_text(json.dumps(data, indent=2))

    problems = verify_manifest(root)
    check("verify: DETECTS an orphaned data file (S0a)",
          any(p.startswith("ORPHAN") and victim in p for p in problems),
          f"got {problems}")
    check("verify: orphan counts as an integrity failure",
          any(is_integrity_failure(p) for p in problems))
    check("verify: orphan report says how much data is recoverable from it",
          any("4 events recoverable" in p for p in problems), f"got {problems}")


def test_join_reply_is_read(tmp: Path) -> None:
    """BUG-20260909-005. A refused phx_join used to be invisible.

    `handle_frame` returned control frames before anything read their status,
    so an expired key or a wrong slug produced a process that connects,
    heartbeats, logs nothing, and records nothing.
    """
    store = OperationalStore(tmp / "join.db")
    w = FakeWriter()
    c = StreamConsumer("k", ["argonauts", "chromie-squiggle"], w, store,
                       run_id="run-join")

    joins = [json.loads(m) for m in c.join_messages()]
    refs = {j["ref"]: j["topic"] for j in joins}
    check("join: each join is tracked by its ref", len(refs) == 2)

    ok_ref = joins[0]["ref"]
    c.handle_frame(json.dumps([ok_ref, ok_ref, joins[0]["topic"], "phx_reply",
                               {"status": "ok", "response": {}}]))
    check("join: an accepted join is recorded as subscribed",
          c.stats.joined == ["collection:argonauts"], f"got {c.stats.joined}")

    bad_ref = joins[1]["ref"]
    c.handle_frame(json.dumps([bad_ref, bad_ref, joins[1]["topic"], "phx_reply",
                               {"status": "error",
                                "response": {"reason": "unauthorized"}}]))
    check("join: a REFUSED join is recorded, not swallowed",
          "collection:chromie-squiggle" in c.stats.join_rejected,
          f"got {c.stats.join_rejected}")
    check("join: a refused join opens a gap in the register",
          len(store.open_gaps()) == 1, f"got {store.open_gaps()}")
    check("join: that gap is marked NOT backfillable",
          store.open_gaps()[0]["backfillable"] == 0)
    check("join: control frames are landed as evidence",
          sum(1 for _, t, _ in w.landed if t == "__control__") == 2,
          "the reply frame is the only on-disk record of what we subscribed to")

    # A join that is never answered at all is the same failure, quieter.
    c2 = StreamConsumer("k", ["argonauts"], FakeWriter(), store, run_id="run-to")
    c2.join_messages()
    c2._check_join_timeouts()
    check("join: an UNANSWERED join is treated as a rejection",
          "collection:argonauts" in c2.stats.join_rejected,
          "silence is the failure mode an expired key actually produces")


def test_clean_close_records_gap_and_backs_off(tmp: Path) -> None:
    """BUG-20260909-008. A clean close recorded no gap and did not back off.

    The old loop reset backoff to 1.0 and continued with NO sleep, so a peer
    closing politely in a loop spun the event loop hot while recording nothing
    -- and raised no exception, so it looked like a run of successes.
    """
    store = OperationalStore(tmp / "close.db")
    w = FakeWriter()
    connects = {"n": 0}

    class ClosingWS:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def send(self, _m): pass
        def __aiter__(self): return self
        async def __anext__(self): raise StopAsyncIteration

    def factory(_url):
        connects["n"] += 1
        return ClosingWS()

    c = StreamConsumer("k", ["argonauts"], w, store, run_id="run-close",
                       connect_factory=factory, max_backoff=5.0)

    async def drive() -> None:
        task = asyncio.create_task(c.run())
        await asyncio.sleep(0.4)
        c.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    check("clean close: a gap IS recorded when the peer closes unasked",
          any("closed by peer" in g["reason"] for g in store.open_gaps()),
          f"got {[g['reason'] for g in store.open_gaps()]}")
    check("clean close: backoff is applied, the loop does not spin",
          connects["n"] <= 3,
          f"reconnected {connects['n']} times in 0.4s -- old code had no sleep "
          f"on this path at all")
    check("clean close: at least one reconnect actually happened",
          connects["n"] >= 1)


def test_restart_records_downtime_gap(tmp: Path) -> None:
    """BUG-20260909-009. `save_checkpoint` was called only by the test suite.

    With no last-known-alive timestamp, the window between two runs left no
    trace, and downstream an absence of events is indistinguishable from a
    quiet market.
    """
    store = OperationalStore(tmp / "restart.db")
    c1 = StreamConsumer("k", ["argonauts"], FakeWriter(), store, run_id="run-1")

    check("restart: no gap on the very first run ever",
          c1.record_downtime_gap() is None and store.open_gaps() == [])
    ck = store.get_checkpoint("opensea:stream")
    check("restart: the first run writes a checkpoint",
          ck is not None and ck["run_id"] == "run-1")

    w2 = FakeWriter()
    c2 = StreamConsumer("k", ["argonauts"], w2, store, run_id="run-2")
    gid = c2.record_downtime_gap()
    check("restart: the SECOND run records the downtime as a gap", gid is not None)

    with store.connect() as conn:
        row = dict(conn.execute("SELECT * FROM gap_register WHERE id=?", (gid,)).fetchone())
    check("restart: the gap starts at the previous run's last checkpoint",
          row["started_at"] == ck["updated_at"],
          f"gap starts {row['started_at']}, previous run last alive {ck['updated_at']}")
    check("restart: the gap is closed, not left dangling",
          row["ended_at"] is not None)
    check("restart: the gap names the run that stopped",
          "run-1" in row["reason"], row["reason"])
    check("restart: the gap reaches the landing-zone manifest too",
          len(w2.gaps) == 1 and w2.gaps[0].started_at == ck["updated_at"])


def test_codec_multiframe_contract() -> None:
    """BUG-20260909-010. The zstd multi-frame question, answered by running it.

    `stream_reader`'s `read_across_frames` default CHANGED in python-zstandard
    0.23.0. Below that, a multi-frame file decompressed to its first frame and
    raised nothing -- and the manifest checksum, taken over compressed bytes,
    would still have matched. Rather than pin a version and hope, the codec
    walks frames itself and this contract is checked at ingest startup.
    """
    from navanax.codec import RawCodec, verify_codec_roundtrip

    for codec in (GzipCodec(), RawCodec()):
        try:
            verify_codec_roundtrip(codec, frames=5)
            ok, why = True, ""
        except Exception as exc:  # noqa: BLE001
            ok, why = False, str(exc)
        check(f"codec: {codec.name} passes the 5-frame round-trip + truncation contract",
              ok, why)

    try:
        import zstandard  # noqa: F401

        from navanax.codec import ZstdCodec
    except ImportError:
        print("        (zstandard not installed here -- the SAME contract runs at "
              "ingest startup on the machine that has it)")
        return
    try:
        verify_codec_roundtrip(ZstdCodec(level=3), frames=5)
        ok, why = True, ""
    except Exception as exc:  # noqa: BLE001
        ok, why = False, str(exc)
    check(f"codec: zstd passes the 5-frame contract (python-zstandard "
          f"{zstandard.__version__})", ok, why)



# ===========================================================================
# Regression tests for the SECOND validator review -- findings V1..V6, all of
# which were introduced or left unbuilt by the fixes for BUG-005..010.
# ===========================================================================
def test_verify_detects_decoder_truncation(tmp: Path) -> None:
    """V1. The audit could not see BUG-010's failure mode even after BUG-007.

    A byte-perfect file whose DECODER returns a prefix: the sha256 is over the
    compressed bytes, so it still matches, and the audit reported clean. The
    manifest has held the event count since BUG-007; nothing compared with it.
    """
    clock = FakeClock(datetime(2026, 9, 9, 11, 0, 0, tzinfo=timezone.utc))
    root = tmp / "trunc"
    w = LandingZoneWriter(root, "run-t", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic,
                          flush_events=2, auto_flush=False)
    for i in range(6):
        w.write(frame("argonauts", f"2026-09-09T11:00:{i:02d}Z"),
                event_timestamp=f"2026-09-09T11:00:{i:02d}Z")
    w.close()
    check("V1: a healthy closed file verifies clean", verify_manifest(root) == [])

    # Simulate a decoder that returns only the first frame -- BUG-010 exactly.
    # The FILE is untouched; only the reading of it is short.
    import navanax.landing as L
    real = L._recoverable_event_count
    L._recoverable_event_count = lambda fp: 2          # first frame only
    try:
        problems = verify_manifest(root)
    finally:
        L._recoverable_event_count = real

    check("V1: DETECTS a decoder returning a prefix of an intact file",
          any(p.startswith("COUNT MISMATCH") for p in problems), f"got {problems}")
    check("V1: a count mismatch is an integrity failure, not a note",
          any(is_integrity_failure(p) for p in problems))
    check("V1: the message says the file is intact and the decoder is not",
          any("DECODER" in p for p in problems), f"got {problems}")


def test_control_frames_do_not_inflate_event_count(tmp: Path) -> None:
    """V2. BUG-005's fix started landing control frames as market events.

    The BUG-005 scenario -- expired key, join refused, an hour of heartbeats,
    zero market data -- produced a manifest reading `event_count: 121`, which
    is the number the ING event-rate monitor watches. BUG-005's own failure
    mode, re-entering through the door BUG-005's fix opened.
    """
    clock = FakeClock(datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc))
    root = tmp / "control"
    w = LandingZoneWriter(root, "run-c2", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic,
                          flush_events=1, auto_flush=False)
    for i in range(20):
        w.write(f'{{"heartbeat":{i}}}', topic="__control__", control=True)
    w.close()

    mf = json.loads((root / "_manifest" / "2026-09-09.json").read_text())
    rec = mf["files"][0]
    check("V2: control frames do NOT count as market events",
          rec["event_count"] == 0, f"event_count={rec['event_count']}")
    check("V2: they are counted separately, not discarded",
          rec["control_count"] == 20, f"control_count={rec.get('control_count')}")
    check("V2: a control-only file has no event-time span",
          rec["first_event_timestamp"] is None)

    # ...and must not be returned by every range query for all time.
    from navanax.landing import ManifestWriter
    hits = ManifestWriter(root).files_for_event_range(
        "2020-01-01T00:00:00Z", "2020-12-31T23:59:59Z")
    check("V2: a control-only file is NOT matched by an unrelated range query",
          hits == [], f"got {[h['filename'] for h in hits]}")


def test_gap_is_filed_under_every_day_it_spans(tmp: Path) -> None:
    """V4. A 57-hour weekend outage was filed only under the Friday.

    docs/07 says a reader MUST resolve through the manifest. Asking "were there
    gaps on Sunday?" returned none, from the artifact it was told to trust.
    """
    from navanax.landing import GapRecord
    clock = FakeClock(datetime(2026, 9, 8, 9, 0, 0, tzinfo=timezone.utc))
    root = tmp / "spangap"
    w = LandingZoneWriter(root, "run-g2", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic, auto_flush=False)
    w.record_gap(GapRecord(started_at="2026-09-05T23:50:00Z",
                           ended_at="2026-09-08T08:00:00Z",
                           reason="process not running (weekend)",
                           run_id="run-g2", topics=["collection:argonauts"]))
    days = sorted(p.stem for p in (root / "_manifest").glob("*.json"))
    check("V4: the gap appears in EVERY daily manifest it spans",
          days == ["2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08"],
          f"got {days}")
    sunday = json.loads((root / "_manifest" / "2026-09-07.json").read_text())
    check("V4: a reader asking about the middle day finds the gap",
          len(sunday["gaps"]) == 1
          and sunday["gaps"][0]["started_at"] == "2026-09-05T23:50:00Z")
    w.close()


def test_concurrent_manifest_writes_lose_nothing(tmp: Path) -> None:
    """V3. Manifest read-modify-write had no cross-process lock.

    Measured before the fix: 295 of 600 gap records lost, verify clean. A lost
    FILE record shows up as an ORPHAN; a lost GAP record is undetectable by
    anything, forever -- and a missing gap is how "we were not watching" comes
    to read as "nothing happened".
    """
    import threading as th

    from navanax.landing import GapRecord, ManifestWriter
    root = tmp / "race"
    root.mkdir(parents=True, exist_ok=True)
    writers = [ManifestWriter(root) for _ in range(4)]
    per = 40

    def hammer(mw: ManifestWriter, tag: int) -> None:
        for i in range(per):
            mw.record_gap("2026-09-09", GapRecord(
                started_at="2026-09-09T13:00:00Z", ended_at=None,
                reason=f"w{tag}-{i}", run_id=f"w{tag}"))

    threads = [th.Thread(target=hammer, args=(mw, i)) for i, mw in enumerate(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads((root / "_manifest" / "2026-09-09.json").read_text())
    check("V3: no gap record is lost under concurrent writers",
          len(data["gaps"]) == 4 * per,
          f"wrote {4 * per}, manifest holds {len(data['gaps'])}")
    check("V3: every record is distinct (no overwrite)",
          len({g["reason"] for g in data["gaps"]}) == 4 * per)


def test_rejection_gap_is_not_reopened_every_reconnect(tmp: Path) -> None:
    """V6. Joins are re-issued on every reconnect; each rejection opened a gap.

    Measured: 200 collections x 20 reconnects = 4,000 permanently-open gaps and
    89.6s of synchronous manifest fsync inside the asyncio event loop, blocking
    the reader long enough to miss the heartbeat and force another reconnect.
    Trigger is a 7-day key expiring -- a certainty, not a hypothetical.
    """
    store = OperationalStore(tmp / "storm.db")
    w = FakeWriter()
    c = StreamConsumer("k", ["argonauts"], w, store, run_id="run-storm")

    for _ in range(20):                      # twenty reconnects, all refused
        joins = [json.loads(m) for m in c.join_messages()]
        ref = joins[0]["ref"]
        c.handle_frame(json.dumps([ref, ref, joins[0]["topic"], "phx_reply",
                                   {"status": "error",
                                    "response": {"reason": "unauthorized"}}]))

    check("V6: one gap per topic, not one per reconnect",
          len(store.open_gaps()) == 1,
          f"20 refused reconnects opened {len(store.open_gaps())} gaps")

    # ...and a topic that later succeeds must have its gap RETRACTED. Asserting
    # a hole that does not exist is its own kind of wrong.
    joins = [json.loads(m) for m in c.join_messages()]
    ref = joins[0]["ref"]
    c.handle_frame(json.dumps([ref, ref, joins[0]["topic"], "phx_reply",
                               {"status": "ok", "response": {}}]))
    check("V6/V11: a recovered subscription closes its rejection gap",
          store.open_gaps() == [], f"still open: {store.open_gaps()}")
    check("V6/V11: and clears the rejection from stats",
          c.stats.join_rejected == {}, f"got {c.stats.join_rejected}")


def test_flusher_thread_runs_on_a_real_clock(tmp: Path) -> None:
    """V8. Every cadence assertion used auto_flush=False and drove tick() by hand.

    tick() was tested; the thread that calls it in production was not. That is
    this project's recurring shape -- requirement, docstring and log agreeing on
    behaviour only half of which is exercised -- so this one uses the real
    clock and the real thread, and pays 1.2 seconds for it.
    """
    import threading as th
    import time as _t

    before = th.active_count()
    root = tmp / "flusher"
    w = LandingZoneWriter(root, "run-th", codec=GzipCodec(),
                          flush_seconds=0.5, flush_events=10_000)  # auto_flush default
    check("V8: the flusher thread is actually started by default",
          th.active_count() == before + 1, f"{before} -> {th.active_count()}")

    w.write(frame("argonauts", "2026-09-09T14:00:00Z", event="item_cancelled"),
            topic="collection:argonauts", event_timestamp="2026-09-09T14:00:00Z")
    path = sorted((root / "stream").rglob("*.jsonl.gz"))[0]

    deadline = _t.monotonic() + 5.0
    while path.stat().st_size == 0 and _t.monotonic() < deadline:
        _t.sleep(0.05)

    check("V8: an idle writer's frame is closed by the THREAD, unaided",
          path.stat().st_size > 0,
          "no tick() call, no second write -- only the daemon flusher")
    recovered = list(read_file(path, GzipCodec(), tolerate_truncation=True))
    check("V8: and the event is recoverable without a clean shutdown",
          len(recovered) == 1 and "item_cancelled" in recovered[0]["raw"])

    w.close()
    _t.sleep(0.1)
    check("V8: close() stops the thread", th.active_count() == before,
          f"expected {before}, got {th.active_count()}")


def test_gap_spans_every_day_through_the_consumer_path(tmp: Path) -> None:
    """BUG-023. The tech-lead gate caught what the first fix and its test missed.

    `test_gap_is_filed_under_every_day_it_spans` asserted `_dates_spanned` by
    calling `record_gap` with BOTH endpoints known. Production never does that:
    a live gap is opened with `ended_at=None` at the moment the outage starts,
    when it has zero duration, and `_close_gap` only ever touched the
    disposable SQLite store. So a 57-hour weekend outage on a RUNNING process
    still produced one day's manifest and a gap that said `ended_at: null`
    forever -- a reader could not tell a three-second reconnect from a lost
    weekend.

    This test drives the REAL consumer methods. That distinction is the whole
    finding: a test that proves the helper is not a test that proves the path.
    """
    store = OperationalStore(tmp / "spanpath.db")
    clock = FakeClock(datetime(2026, 9, 4, 18, 0, 0, tzinfo=timezone.utc))
    root = tmp / "spanpath"
    w = LandingZoneWriter(root, "run-span", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic, auto_flush=False)
    c = StreamConsumer("k", ["argonauts"], w, store, run_id="run-span")

    import navanax.stream as S
    real_now = S._now
    S._now = clock.now
    try:
        c._open_gap("connection closed by peer without a stop request")
        days_at_open = sorted(p.stem for p in (root / "_manifest").glob("*.json"))
        check("BUG-023: a gap is recorded the moment it opens, not only on close",
              days_at_open == ["2026-09-04"], f"got {days_at_open}")

        clock.advance(57 * 3600)          # Friday 18:00 -> Monday 03:00
        c._close_gap()
    finally:
        S._now = real_now

    days = sorted(p.stem for p in (root / "_manifest").glob("*.json"))
    check("BUG-023: on close, the gap appears in EVERY day it spanned",
          days == ["2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07"],
          f"got {days} -- the fix that only covered the restart path gave "
          f"['2026-09-04']")

    sunday = json.loads((root / "_manifest" / "2026-09-06.json").read_text())
    check("BUG-023: the middle day carries the gap", len(sunday["gaps"]) == 1)
    check("BUG-023: and it records when the gap ENDED",
          sunday["gaps"][0]["ended_at"] is not None,
          "every live gap used to say ended_at: null forever")

    friday = json.loads((root / "_manifest" / "2026-09-04.json").read_text())
    check("BUG-023: the open record is REPLACED, not duplicated",
          len(friday["gaps"]) == 1 and friday["gaps"][0]["ended_at"] is not None,
          f"got {friday['gaps']}")
    check("BUG-023: the gap carries the id that links it to the register",
          friday["gaps"][0]["gap_id"] is not None,
          "without an id, a closure cannot find the record it completes")
    w.close()


def test_rest_budget_config_is_actually_read(tmp: Path) -> None:
    """BUG-015 / REQ-N-09. The config block that looked like a knob and turned nothing.

    `rest_budget` carried the only provenance-tagged MEASURED numbers in the
    repo and was parsed by nothing: an operator could change `capacity: 120`
    and see no effect and no error.
    """
    from navanax.governor import governor_from_config

    g = governor_from_config({"rest_budget": {
        "capacity": 55, "per_seconds": 3600,
        "reserve": {"INTERACTIVE": 0, "SIGNAL": 2, "BACKFILL": 6, "MAINTENANCE": 11}}})
    check("BUG-015: capacity comes from config, not from code",
          g.bucket.state.capacity == 55.0, f"got {g.bucket.state.capacity}")
    check("BUG-015: reserve floors come from config too",
          g.reserve[Priority.BACKFILL] == 6.0 and g.reserve[Priority.MAINTENANCE] == 11.0)

    d = governor_from_config({})
    check("BUG-015: sane measured defaults when the block is absent",
          d.bucket.state.capacity == 120.0)

    try:
        governor_from_config({"rest_budget": {"reserve": {"URGENT": 5}}})
        raised = False
    except ValueError as exc:
        raised = "URGENT" in str(exc) and "INTERACTIVE" in str(exc)
    check("BUG-015: an unknown priority in config FAILS LOUDLY",
          raised, "a silently ignored config key is how a knob turns nothing")


def test_no_superseded_rate_limit_in_operator_text(tmp: Path) -> None:
    """BUG-014. The falsified 600/hr survived in the files the operator reads first.

    BUG-003 was marked fixed while README line 33, setup.command and
    preflight's machine-readable JSON still carried the number one live
    response had falsified -- and preflight's `est/10` arithmetic under-reported
    onboarding time by 5x in the artifact the docs get corrected FROM.
    """
    import re as _re
    # A line ASSERTS the number if it puts 600 next to a rate word. A line that
    # says 600 was wrong is the correction, not the defect, so historical
    # references are allowed and only claims are flagged.
    asserts = _re.compile(r"600[^\n]{0,40}?(read|request|call|hour|hr|budget)",
                          _re.IGNORECASE)
    # A line that shows the CORRECTION is the lesson being recorded, not the
    # defect. Anything that pairs the old number with the new one, or names it
    # as superseded, is allowed.
    corrects = _re.compile(r"not the 600|was an|unsourc|assum|previously|falsifi|"
                           r"were 0/|BUG-2026|rescaled|->\s*120|→\s*120|"
                           r"real limit is 120|corrected", _re.IGNORECASE)
    # QA-AUDIT finding. This used to scan a HARDCODED LIST of seven files, and
    # .claude/agents/*.md was not on it -- so the falsified figure survived,
    # unqualified, in FIVE agent charters, including the data-engineer's, where
    # it was headed "The constraint that shapes everything you build", and
    # research.md's, where it was labelled "verified 2026-09-09" on the very
    # day it was measured at 120. The ledger's own resolution text claimed the
    # test "scans every operator-facing file... so this class cannot recur
    # silently". It recurred silently in five files. Scan the whole repo, the
    # way tools/buglog.py already does for bug ids.
    # Scan every file in the working tree that is not deliberately excluded --
    # tracked AND untracked-but-not-ignored.
    #
    # Tech-lead re-review, 2026-09-10: the previous enumeration was plain
    # `git ls-files`, i.e. TRACKED ONLY, on the theory that "a generated
    # artifact in the working tree is output, not an assertion". That theory is
    # false here and the failure mode is the same one BUG-014 already recurred
    # under. At the time of this fix 39 of the repo's 78 files were tracked;
    # every file this very PR touches -- src/navanax/metrics.py,
    # src/navanax/dashboard.py, src/navanax/ui/index.html, config/assumptions.yaml,
    # docs/08_DASHBOARD.md, four agent charters -- was untracked and therefore
    # UNSCANNED. A file being new is not evidence that it makes no claim; it is
    # the state every file passes through on the day it is written, which is
    # exactly the day a falsified figure gets copied into it.
    #
    # `--exclude-standard` still honours .gitignore, so the genuinely generated
    # artifacts stay out: `tools/preflight_report.json` is gitignored precisely
    # so the report the documents get corrected FROM cannot itself become a
    # stale claim in the repo. That exclusion is expressed once, in .gitignore,
    # where the operator can see it -- not implicitly by whether someone has
    # run `git add` yet.
    import subprocess
    skip_dirs = {".git", "__pycache__", ".venv", "node_modules", "data", ".sync"}
    binary_suffixes = {".xlsx", ".gz", ".zst", ".db", ".pyc", ".png", ".jpg", ".jpeg",
                       ".gif", ".pdf", ".sqlite", ".zip", ".so", ".dylib"}
    try:
        listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                                cwd=ROOT, capture_output=True, text=True,
                                timeout=30, check=True).stdout.split()
        candidates = [ROOT / t for t in listed]
    except (OSError, subprocess.SubprocessError):
        # No git binary, or ROOT is not a repository (a tarball of the source, a
        # CI image without git). Walk the tree instead: an unscanned repo would
        # PASS this test silently, which is the one outcome it must never have.
        candidates = sorted(p for p in ROOT.rglob("*") if p.is_file())
    offenders = []
    scanned: set[str] = set()
    for f in candidates:
        if not f.is_file() or f.suffix.lower() in binary_suffixes:
            continue
        if any(part in skip_dirs for part in f.relative_to(ROOT).parts):
            continue
        if f.name in ("BUGS.md", "bugs.yaml", "selftest.py"):
            continue   # the bug log's job is to RECORD the wrong number
        name = str(f.relative_to(ROOT))
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        scanned.add(name)
        for i, line in enumerate(lines, 1):
            if asserts.search(line) and not corrects.search(line):
                offenders.append(f"{name}:{i}: {line.strip()[:70]}")
    # The enumeration itself is the thing that regressed, so assert it directly:
    # these four are operator-facing and were UNTRACKED when this was fixed. If
    # the enumeration ever narrows back to `git ls-files` (tracked only), this
    # fails loudly instead of the 600/hr scan passing over a shrunken corpus.
    must_scan = ["src/navanax/metrics.py", "src/navanax/dashboard.py",
                 "src/navanax/ui/index.html", "config/assumptions.yaml"]
    unscanned = [m for m in must_scan if m not in scanned and (ROOT / m).is_file()]
    check("BUG-014: the scan reaches UNTRACKED operator-facing files, not just tracked ones",
          not unscanned, f"never opened -> {unscanned} (scanned {len(scanned)} files)")
    check("BUG-014: no operator-facing file still asserts the 600/hr figure",
          not offenders, "still present -> " + " | ".join(offenders))


def test_single_instance_degrades_on_unsupported_filesystem(tmp: Path) -> None:
    """BUG-027. The single-instance guard could not tell "busy" from "unsupported".

    BUG-022's guard caught OSError without checking errno. EOPNOTSUPP is what
    an SMB/AFP share and some NFS mounts reply, and treating it as contention
    made ingest REFUSE TO START, telling the operator to stop a process that
    does not exist. On a local disk it never fires; on a network volume it
    stops collection entirely -- and a day not recorded cannot be bought back,
    so failing closed is the wrong direction for this particular guard.
    """
    import errno as _errno

    from navanax.cli import _single_instance

    lock = tmp / "lockdir" / ".ingest.lock"

    def flock_raising(err: int):
        def _f(_fd, _op):
            raise OSError(err, _errno.errorcode.get(err, "?"))
        return _f

    import fcntl as _fcntl
    real = _fcntl.flock
    try:
        # Unsupported filesystem: proceed, loudly.
        _fcntl.flock = flock_raising(_errno.EOPNOTSUPP)
        try:
            with _single_instance(lock):
                proceeded = True
        except RuntimeError:
            proceeded = False
        check("BUG-027: an unsupported filesystem does NOT stop ingestion",
              proceeded,
              "refusing here loses days of history to a filesystem quirk")

        # Real contention: refuse, and say why.
        _fcntl.flock = flock_raising(_errno.EWOULDBLOCK)
        try:
            with _single_instance(lock):
                refused = False
        except RuntimeError as exc:
            refused = "already running" in str(exc)
        check("BUG-027: genuine contention still REFUSES to start", refused,
              "two writers on one landing zone lose gap records undetectably")
    finally:
        _fcntl.flock = real


def test_governor_recovers_from_a_429() -> None:
    """BUG-012. One 429 used to disable REST for the life of the process.

    `observe_429` set `server_remaining = 0`; `try_consume` takes
    `min(tokens, server_remaining)`; and only a header from a SUCCESSFUL
    response could raise it again -- which required a token. The recovery path
    required the very resource the failure removed, and the outage presented as
    something unrelated.
    """
    t = [0.0]
    b = TokenBucket(capacity=120, per_seconds=3600, clock=lambda: t[0])
    check("BUG-012: a healthy bucket grants", b.try_consume(1))

    b.observe_429()
    check("BUG-012: a 429 stops REST immediately", not b.try_consume(1))
    check("BUG-012: and the wait is REPORTED, not silent",
          b.seconds_until(1) > 0, f"got {b.seconds_until(1)}")

    t[0] += 31
    check("BUG-012: the block EXPIRES and the governor heals itself",
          b.try_consume(1),
          "before the fix nothing could ever raise server_remaining again")

    # Repeated 429s must back off further, not reset to the same short wait.
    b.observe_429()
    first = b.seconds_until(1)
    b.observe_429()
    check("BUG-012: repeated 429s back off further", b.seconds_until(1) > first,
          f"{first} -> {b.seconds_until(1)}")

    # A server-supplied reset time is believed over our guess.
    # A server-supplied reset time is believed over our own guess. Advance the
    # clock enough for the LOCAL bucket to refill too, so this asserts the
    # server block lifting rather than accidentally asserting token refill.
    t2 = [0.0]
    b2 = TokenBucket(capacity=120, per_seconds=3600, clock=lambda: t2[0])
    b2.observe_headers({"x-ratelimit-reset": str(time.time() - 1)})
    b2.observe_429()
    t2[0] += 60                     # 2 tokens at 120/hr
    check("BUG-012: an ALREADY-PASSED server reset lifts the block",
          b2.try_consume(1),
          "x-ratelimit-reset was parsed at governor.py:150 and read nowhere")

    t3 = [0.0]
    b3 = TokenBucket(capacity=120, per_seconds=3600, clock=lambda: t3[0])
    b3.observe_429()
    check("BUG-012: blocked while the 429 stands", b3.state.server_remaining == 0)
    b3.observe_success()
    check("BUG-012: a success clears the block outright",
          b3.state.server_block_until is None,
          "the block must not outlive the evidence for it")
    t3[0] += 60
    check("BUG-012: and REST works again once tokens refill", b3.try_consume(1))


def test_gaps_are_labelled_by_what_can_actually_be_recovered(tmp: Path) -> None:
    """BUG-013. Every gap claimed backfillable=True, contradicting REQ-D-09a.

    A single boolean cannot describe the window honestly: sales, listings and
    offers can be re-fetched; cancellations and order invalidate/revalidate
    cannot, ever. Writing True told a downstream reader that a hole it can
    never fill was fillable -- and the landing zone is append-only, so every
    gap written with the wrong flag stayed wrong permanently.
    """
    from navanax.stream import BACKFILLABLE, IRRECOVERABLE

    store = OperationalStore(tmp / "classes.db")
    w = FakeWriter()
    c = StreamConsumer("k", ["argonauts"], w, store, run_id="run-cls")
    c._open_gap("peer closed")

    g = w.gaps[-1]
    check("BUG-013: a gap over ALL_EVENTS is NOT fully backfillable",
          g.backfillable is False,
          "it spans item_cancelled and order_invalidate, which REST cannot return")
    check("BUG-013: the irrecoverable classes are named, not implied",
          set(g.irrecoverable_classes) == set(IRRECOVERABLE) & set(c.events),
          f"got {g.irrecoverable_classes}")
    check("BUG-013: the recoverable classes are named too",
          set(g.backfillable_classes) == set(BACKFILLABLE) & set(c.events),
          f"got {g.backfillable_classes}")
    check("BUG-013: the register agrees with the manifest",
          store.open_gaps()[0]["backfillable"] == 0)

    # A subscription with nothing unrecoverable in it IS fully backfillable --
    # the flag has to be able to say yes, or it carries no information.
    c2 = StreamConsumer("k", ["argonauts"], FakeWriter(), store, run_id="run-cls2",
                        events=["item_listed", "item_sold"])
    rec, lost, full = c2._gap_classes()
    check("BUG-013: a recoverable-only subscription is marked backfillable",
          full is True and lost == [], f"lost={lost}")


def test_missing_event_timestamp_is_counted(tmp: Path) -> None:
    """BUG-017. An unorderable event landed silently and nothing counted it.

    REQ-D-07 orders on event_timestamp and never on arrival, so an event
    without one drops out of every manifest-driven range query. We could not
    say how often it happened, which means we could not say whether it
    mattered.
    """
    store = OperationalStore(tmp / "noets.db")
    w = FakeWriter()
    c = StreamConsumer("k", ["argonauts"], w, store, run_id="run-ets")

    c.handle_frame(json.dumps(["1", None, "collection:argonauts", "item_listed",
                               {"event_type": "item_listed", "payload": {}}]))
    check("BUG-017: an event with no timestamp is still LANDED",
          len(w.landed) == 1, "the bytes are the record; never drop them")
    check("BUG-017: ...and is COUNTED", c.stats.missing_event_ts == 1)
    check("BUG-017: broken down by event type, so the cause is findable",
          c.stats.missing_event_ts_by_type == {"item_listed": 1},
          f"got {c.stats.missing_event_ts_by_type}")

    c.handle_frame(frame("argonauts", "2026-09-09T15:00:00Z"))
    d = c.stats.as_dict()
    check("BUG-017: the rate is reported as a percentage, with its count",
          d["missing_event_ts_pct"] == 50.0 and d["missing_event_ts"] == 1,
          f"got {d['missing_event_ts_pct']}% of {d['events']}")


def test_disk_failure_is_not_blamed_on_the_stream(tmp: Path) -> None:
    """BUG-018. A full disk was recorded as an ingestion gap and retried forever.

    The run loop's `except Exception` caught the writer's OSError, opened a gap
    blaming OpenSea for a local failure, reconnected, and failed again --
    recording gaps it could not write either.
    """
    from navanax.errors import LandingZoneWriteError

    store = OperationalStore(tmp / "disk.db")

    class FullDisk(FakeWriter):
        def write(self, *a, **k):
            raise OSError(28, "No space left on device")

    c = StreamConsumer("k", ["argonauts"], FullDisk(), store, run_id="run-disk")
    try:
        c.handle_frame(frame("argonauts", "2026-09-09T16:00:00Z"))
        raised = None
    except LandingZoneWriteError as exc:
        raised = exc
    except OSError:
        raised = "bare OSError"

    check("BUG-018: a write failure raises a LOCAL error, not a stream error",
          isinstance(raised, LandingZoneWriteError), f"got {raised!r}")
    check("BUG-018: it halts ingestion rather than reconnecting",
          raised is not None and raised.halts_ingestion is True,
          "there is nothing to reconnect to; retrying cannot make the disk bigger")
    check("BUG-018: no gap is opened blaming the stream",
          store.open_gaps() == [], f"got {store.open_gaps()}")
    check("BUG-018: the message names the real cause",
          "landing zone" in str(raised).lower())


def test_dead_priority_queue_is_gone() -> None:
    """BUG-016. `_Waiter` and `_waiters` read as a priority queue that was not one.

    Nothing appended to them and nothing read them. Dead code that implies
    capability is worse than absent code: it answers the question "is ordering
    handled?" with a yes that is not true.
    """
    import navanax.governor as G

    check("BUG-016: the dead _Waiter class is removed",
          not hasattr(G, "_Waiter"))
    g = RestGovernor()
    check("BUG-016: and the unused queue is gone from the governor",
          not hasattr(g, "_waiters") and not hasattr(g, "_seq"))
    check("BUG-016: the real ordering mechanism -- reserve floors -- is intact",
          g.reserve[Priority.INTERACTIVE] < g.reserve[Priority.MAINTENANCE],
          "INTERACTIVE must be able to draw when MAINTENANCE cannot")


def test_governor_adapts_capacity_from_server(tmp: Path) -> None:
    """BUG-029. config/base.yaml said the governor reads x-ratelimit-limit and adapts.

    Nothing read the header. capacity was fixed at construction. The same
    config block records that 120 came from a Cloudflare cache HIT and may be
    wrong -- and named this non-existent mechanism as the reason that was safe.
    Fifth instance of the operator-facing-claim-versus-code shape.
    """
    t = [0.0]
    b = TokenBucket(capacity=120, per_seconds=3600, clock=lambda: t[0])
    check("BUG-029: starts on the configured capacity",
          b.state.capacity == 120.0 and b.state.capacity_source == "config")

    b.observe_headers({"x-ratelimit-limit": "300", "x-ratelimit-remaining": "299"})
    check("BUG-029: capacity ADAPTS to the server's stated limit",
          b.state.capacity == 300.0, f"got {b.state.capacity}")
    check("BUG-029: the refill rate follows it",
          abs(b.state.refill_per_second - 300 / 3600) < 1e-9)
    check("BUG-029: provenance records that the server overrode config",
          b.state.capacity_source.startswith("header") and b.state.capacity_changes == 1)

    b.observe_headers({"x-ratelimit-limit": "60"})
    check("BUG-029: a LOWER server limit clamps outstanding tokens too",
          b.state.capacity == 60.0 and b.state.tokens <= 60.0,
          f"capacity={b.state.capacity} tokens={b.state.tokens}")
    b.observe_headers({"x-ratelimit-limit": "garbage"})
    check("BUG-029: an unparseable header changes nothing", b.state.capacity == 60.0)


def test_truthful_zero_remaining_is_not_discarded() -> None:
    """BUG-030. A 200 carrying `remaining: 0` -- "that was your last one" -- was erased.

    observe_response applied headers, then observe_success cleared any
    server_remaining that read 0. The governor then granted against its local
    model and earned a 429 it had been explicitly warned about. Only a ceiling
    WE imposed after a 429 is ours to lift.
    """
    t = [0.0]
    g = RestGovernor(bucket=TokenBucket(capacity=120, per_seconds=3600, clock=lambda: t[0]))
    g.observe_response(200, {"x-ratelimit-remaining": "0"})
    check("BUG-030: the server's 'remaining: 0' on a SUCCESS stands",
          g.bucket.state.server_remaining == 0, f"got {g.bucket.state.server_remaining}")
    check("BUG-030: and the governor does NOT grant against it",
          not g.bucket.try_consume(1),
          "granting here earns a 429 the server just warned about")

    # ...but a ceiling we imposed ourselves after a 429 IS lifted on success.
    g2 = RestGovernor(bucket=TokenBucket(capacity=120, per_seconds=3600, clock=lambda: t[0]))
    g2.observe_response(429, {})
    check("BUG-030: a 429 imposes our own ceiling",
          g2.bucket.state.server_remaining == 0 and g2.bucket.state.remaining_from_429)
    g2.observe_response(200, {})
    check("BUG-030: a later success lifts OUR ceiling",
          g2.bucket.state.server_remaining is None)

    # ...and a delta-seconds reset header is not mistaken for a 1970 epoch.
    g3 = RestGovernor(bucket=TokenBucket(capacity=120, per_seconds=3600, clock=lambda: t[0]))
    g3.observe_response(429, {"x-ratelimit-reset": "60"})
    check("BUG-030 (item 6): a delta reset header is treated as seconds from now",
          g3.bucket.state.server_reset_at is not None
          and g3.bucket.state.server_reset_at > time.time() + 30,
          f"got {g3.bucket.state.server_reset_at}")


def test_backfill_worklist_is_not_vacuous(tmp: Path) -> None:
    """BUG-031. BUG-013 redefined backfillable and the worklist query never noticed.

    `unbackfilled_gaps()` filtered `backfillable=1`, which under the default
    subscription is never true -- so `navanax status` read "awaiting backfill
    0" permanently while six re-fetchable classes sat in every reconnect gap.
    """
    store = OperationalStore(tmp / "worklist.db")
    w = FakeWriter()
    c = StreamConsumer("k", ["argonauts"], w, store, run_id="run-wl")
    c._open_gap("peer closed")
    c._close_gap()

    work = store.unbackfilled_gaps()
    check("BUG-031: a closed reconnect gap IS on the backfill worklist",
          len(work) == 1, f"got {len(work)} -- the old query returned 0 forever")
    check("BUG-031: the worklist row names what can be recovered",
          "item_sold" in json.loads(work[0]["backfillable_classes"]),
          f"got {work[0].get('backfillable_classes')}")
    check("BUG-031: ...and what cannot",
          "item_cancelled" in json.loads(work[0]["irrecoverable_classes"]))
    check("BUG-031: the gap is still honestly NOT fully backfillable",
          work[0]["backfillable"] == 0)

    # A gap with NOTHING recoverable must stay off the worklist.
    gid = store.open_gap("run-wl", "rejected", topics=["collection:x"],
                         backfillable=False, backfillable_classes=[],
                         irrecoverable_classes=["item_cancelled"])
    store.close_gap(gid)
    check("BUG-031: a gap with no recoverable classes is NOT worklisted",
          len(store.unbackfilled_gaps()) == 1)


def test_startup_gate_refuses_a_lying_codec() -> None:
    """BUG-032. The gate is what stands between a bad binding and lost data.

    CI's first run against real zstandard turned the gate red for the wrong
    reason: its "find the last frame" heuristic assumed the writer emits no
    epilogue frame on close(), which gzip's does not and zstd's does. The
    cut damaged an empty frame, all five content frames survived, and the gate
    -- correctly by its own rule -- refused. A gate whose expectation is wrong
    is worse than no gate, so this proves the rewritten gate on two things:
    that it PASSES honest codecs, and that it FAILS each of the three ways a
    codec can lie, using deliberately broken wrappers.
    """
    from navanax.codec import GzipCodec, verify_codec_roundtrip

    class FirstFrameOnly(GzipCodec):
        """BUG-010's shape: a healthy multi-frame file reads back as frame 0."""
        name = "gzip-firstframe"

        def decompress(self, data):
            return super().decompress_truncated(data[: data.find(b"\x1f\x8b\x08", 1)])

    class RecoversOnlyFrameZero(GzipCodec):
        """The crash-recovery failure: truncation loses everything but frame 0."""
        name = "gzip-recover0"

        def decompress_truncated(self, data):
            full = super().decompress_truncated(data)
            return full[: full.find(b"\n") + 1]

    class ReturnsPrefixOnDamage(GzipCodec):
        """V5 / BUG-024: strict mode hands back a partial result instead of raising."""
        name = "gzip-lenient"

        def decompress(self, data):
            try:
                return super().decompress(data)
            except Exception:  # noqa: BLE001 - the lie under test
                return super().decompress_truncated(data)

    class EpilogueWriter(GzipCodec):
        """A writer that, like real zstd, appends an empty frame on close()."""
        name = "gzip-epilogue"

        def writer(self, fh):
            inner = super().writer(fh)

            class W:
                def write(self, d): inner.write(d)
                def flush_frame(self): inner.flush_frame()
                def close(self):
                    inner.close()
                    inner.write(b"")
                    import gzip as _g
                    import io as _io
                    m = _io.BytesIO()
                    with _g.GzipFile(fileobj=m, mode="wb", mtime=0):
                        pass
                    fh.write(m.getvalue())
            return W()

    def outcome(codec):
        try:
            verify_codec_roundtrip(codec, frames=5)
            return "PASS"
        except RuntimeError as exc:
            return str(exc)

    check("gate: passes an honest codec", outcome(GzipCodec()) == "PASS")
    check("gate: passes a writer that appends an empty epilogue frame (the CI failure)",
          outcome(EpilogueWriter()) == "PASS",
          "the first gate cut into the epilogue, damaged nothing, and refused")
    r = outcome(FirstFrameOnly())
    check("gate: REFUSES a codec that reads back only the first frame",
          "multi-frame round-trip" in r, r[:120])
    r = outcome(RecoversOnlyFrameZero())
    check("gate: REFUSES a codec whose recovery keeps only frame 0",
          "recovered" in r and "not a pass" in r, r[:120])
    r = outcome(ReturnsPrefixOnDamage())
    check("gate: REFUSES a codec that returns a prefix instead of raising",
          "did NOT raise" in r, r[:120])


def test_secrets_gate_knows_what_a_key_looks_like(tmp: Path) -> None:
    """BUG-033. The REQ-N-11 gate fired on `OPENSEA_API_KEY=fake_key_value`.

    First time it ever ran. A gate that cannot tell a 14-character placeholder
    from a 32-character key teaches everyone to click past it. This proves the
    replacement on both sides: it must stay quiet on the real test fixtures
    and this whole suite, and it must fire on a key-shaped value, a GitHub
    token, and a seed phrase -- in a test file, because tests are not exempt.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import secrets_check as sc

    check("secrets: the suite's own fixtures are NOT flagged",
          sc.scan([ROOT / "tests" / "selftest.py"]) == [],
          f"got {sc.scan([ROOT / 'tests' / 'selftest.py'])}")

    d = tmp / "leak"
    d.mkdir(exist_ok=True)
    (d / "oops_test.py").write_text(
        'OPENSEA_API_KEY = "' + "a1b2c3d4" * 4 + '"\n')          # 32 chars, key-shaped
    (d / "token.yaml").write_text("token: github_pat_" + "X" * 30 + "\n")
    (d / "wallet.txt").write_text(
        "mnemonic: apple brave crane dance eagle fable grape house image "
        "juice knife lemon\n")
    (d / "fine.py").write_text(
        'OPENSEA_API_KEY = "fake"\nexport OPENSEA_API_KEY=...\nkey = os.environ["OPENSEA_API_KEY"]\n')

    hits = sc.scan(sorted(d.iterdir()))
    kinds = {h.split(": ", 1)[1] for h in hits}
    check("secrets: a 32-character key assignment IS flagged, even in a test file",
          "credential assignment" in kinds, f"got {hits}")
    check("secrets: a GitHub token IS flagged wherever it appears",
          "github token" in kinds, f"got {hits}")
    check("secrets: a seed phrase IS flagged (no agent may hold one, docs/02 §6.3)",
          "seed phrase" in kinds, f"got {hits}")
    check("secrets: placeholders, docs and env lookups are NOT flagged",
          not any("fine.py" in h for h in hits), f"got {hits}")


def test_cert_failure_names_the_fix_once(tmp: Path) -> None:
    """BUG-035. First live run: CERTIFICATE_VERIFY_FAILED x7, backing off, gap
    opened blaming "stream error". The stream was fine. The python.org macOS
    build ships no root certificates until its Install Certificates.command is
    run, and nothing in the project said so. A local problem wearing an
    upstream error's clothes must be named, once, with the fix.
    """
    import logging
    import ssl

    from navanax.tls import cert_failure_hint, is_cert_failure, ssl_context

    check("tls: the context verifies (never CERT_NONE)",
          ssl_context().verify_mode == ssl.CERT_REQUIRED)
    check("tls: recognises the verification failure family",
          is_cert_failure(ssl.SSLCertVerificationError(
              1, "certificate verify failed: unable to get local issuer certificate")))
    check("tls: does NOT fire on ordinary network errors",
          not is_cert_failure(ConnectionResetError("peer reset")))
    hint = cert_failure_hint()
    check("tls: the hint says it is not an outage and names an actual fix",
          "NOT an OpenSea outage" in hint and "certifi" in hint)

    store = OperationalStore(tmp / "tls.db")
    calls = {"n": 0}

    class Boom:
        async def __aenter__(self):
            raise ssl.SSLCertVerificationError(
                1, "certificate verify failed: unable to get local issuer certificate")
        async def __aexit__(self, *a): return False

    def factory(_url):
        calls["n"] += 1
        return Boom()

    c = StreamConsumer("k", ["argonauts"], FakeWriter(), store, run_id="run-tls",
                       connect_factory=factory, max_backoff=0.2)
    records: list[logging.LogRecord] = []
    h = logging.Handler()
    h.emit = records.append  # type: ignore[assignment]
    logging.getLogger("navanax.stream").addHandler(h)
    try:
        async def drive():
            t = asyncio.create_task(c.run())
            await asyncio.sleep(0.6)
            c.stop()
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        asyncio.run(drive())
    finally:
        logging.getLogger("navanax.stream").removeHandler(h)

    hints = [r for r in records if r.levelno >= logging.ERROR
             and "NOT an OpenSea outage" in r.getMessage()]
    check("tls: the hint is logged at ERROR", len(hints) >= 1,
          f"{calls['n']} connect attempts, 0 hints")
    check("tls: ...exactly ONCE, not on every retry", len(hints) == 1,
          f"{len(hints)} hints across {calls['n']} attempts")
    check("tls: the gap is still recorded (we really were not recording)",
          len(store.open_gaps()) == 1)


def test_window_close_is_a_clean_stop() -> None:
    """BUG-037. Closing the Terminal window (SIGHUP) killed the process
    without running any finally: last frame lost, no checkpoint, file left
    `open`. The first live run ended exactly that way. A signal must become
    an orderly stop.
    """
    import os
    import signal

    from navanax.cli import _install_shutdown_handlers

    class Stoppable:
        def __init__(self): self.stopped = False
        def stop(self): self.stopped = True

    outcome = {}

    async def drive():
        c = Stoppable()
        async def forever():
            await asyncio.sleep(3600)
        task = asyncio.create_task(forever())
        loop = asyncio.get_running_loop()
        _install_shutdown_handlers(loop, task, c)
        loop.call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        try:
            await asyncio.wait_for(task, timeout=2.0)
            outcome["how"] = "returned"
        except asyncio.CancelledError:
            outcome["how"] = "cancelled"
        except asyncio.TimeoutError:
            outcome["how"] = "timeout -- the signal did nothing"
        outcome["stopped"] = c.stopped
        # restore default so the rest of the suite is unaffected
        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass

    asyncio.run(drive())
    check("BUG-037: SIGTERM cancels the run task instead of killing the process",
          outcome.get("how") == "cancelled", f"got {outcome}")
    check("BUG-037: ...and asks the consumer to stop cleanly first",
          outcome.get("stopped") is True)


def test_error_hierarchy() -> None:
    from navanax.errors import (
        BacktestIntegrityError,
        DataIntegrityError,
        InsufficientSampleError,
        LeakageDetectedError,
        Severity,
    )

    check("errors: DataIntegrityError halts ingestion",
          DataIntegrityError("x").halts_ingestion is True)
    check("errors: BacktestIntegrityError does NOT halt ingestion",
          BacktestIntegrityError("x").halts_ingestion is False,
          "halting ingestion for a backtest bug manufactures a real S2 gap")
    check("errors: BacktestIntegrityError halts backtests",
          BacktestIntegrityError("x").halts_backtests is True)
    check("errors: LeakageDetectedError is S0b",
          LeakageDetectedError("x").severity == Severity.S0B)
    check("errors: InsufficientSample is an ERROR not a warning",
          issubclass(InsufficientSampleError, Exception)
          and InsufficientSampleError("x").severity == Severity.S1)
    e = DataIntegrityError("bad", expected="ETH", received="USD", record_id="tok-1")
    check("errors: message carries locating context",
          "tok-1" in str(e) and "ETH" in str(e), str(e))


def main() -> int:
    """Run every `test_*` function in this module, in definition order.

    BUG-20260909-038. The suite used to run from a HAND-MAINTAINED list of
    function names. Three times in one day a test was written, passed a
    review, and never executed, because adding it to the list is a separate
    step that a string-replace silently skipped. One of them was the codec
    gate's own self-test -- the claim "the gate has a regression test" was
    true of the file and false of the run. Discovery removes the step.
    """
    import inspect

    tmp = Path(tempfile.mkdtemp(prefix="navanax-selftest-"))
    try:
        print("=" * 72)
        print("NAVANAX PHASE 0 SELF-TEST  (stdlib only: gzip codec stands in for zstd)")
        print("=" * 72)
        g = globals()
        tests = [(name, fn) for name, fn in g.items()
                 if name.startswith("test_") and callable(fn)]
        tests.sort(key=lambda nf: inspect.getsourcelines(nf[1])[1])
        for name, fn in tests:
            print(f"\n--- {name} ---")
            if inspect.signature(fn).parameters:
                fn(tmp)
            else:
                fn()
        print("\n" + "=" * 72)
        print(f"{len(tests)} test functions, {len(PASS)} passed, {len(FAIL)} failed")
        if FAIL:
            print("\nFAILURES:")
            for f in FAIL:
                print("  " + f)
        print("=" * 72)
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)




# ===========================================================================
# Dashboard: normalizer, metric engine, server. Fixtures below are REAL frames
# captured from the live Argonauts stream on 2026-09-09 (trimmed, hashes
# shortened) -- the BUG-002 lesson is that fixtures written from the author's
# assumptions inherit the author's bugs.
# ===========================================================================
REAL_BID = ["1", None, "collection:argonauts", "item_received_bid", {
    "event_type": "item_received_bid", "version": 1, "sent_at": "2026-09-09T10:18:41.216000Z",
    "payload": {"base_price": "880000000000000000", "chain": "ethereum",
                "collection": {"slug": "argonauts"}, "created_date": "2026-09-09T10:18:37.000000Z",
                "event_timestamp": "2026-09-09T10:18:39.860000Z",
                "expiration_date": "2026-09-09T10:48:37.000000Z",
                "item": {"nft_id": "ethereum/0x387c41b0b2f1128de44db1bcf8baad085f26392c", "chain": {"name": "ethereum"}},
                "maker": {"address": "0x0d9ec524ed52f109c530a28c91fe190c44c0babd"},
                "order_hash": "0x39f82750fe2f69c61a699517eeb4d06d32ea3b9d",
                "payment_token": {"address": "0xc02a", "decimals": 18, "eth_price": "0.88",
                                  "name": "Wrapped Ethereum", "symbol": "WETH", "usd_price": "2190.1264"},
                "protocol_data": {"parameters": {"consideration": [
                    {"itemType": 2, "token": "0x387c41b0b2f1128de44db1bcf8baad085f26392c",
                     "identifierOrCriteria": "7531", "startAmount": "1", "endAmount": "1"}]}},
                "quantity": 1, "taker": None}}]
REAL_CANCEL = ["1", None, "collection:argonauts", "item_cancelled", {
    "event_type": "item_cancelled", "version": 2, "sent_at": "2026-09-09T10:19:02.375000Z",
    "payload": {"base_price": "800000000000000000", "chain": "ethereum",
                "collection": {"slug": "argonauts"}, "event_timestamp": "2026-09-09T10:19:02.350000Z",
                "expiration_date": "2026-09-09T10:48:56.000000Z", "is_private": False,
                "listing_date": "2026-09-09T10:18:56.000000Z", "listing_type": None,
                "maker": {"address": "0x0d9ec524ed52f109c530a28c91fe190c44c0babd"},
                "order_hash": "0x39f82750fe2f69c61a699517eeb4d06d32ea3b9d",
                "payment_token": {"decimals": 18, "eth_price": "0.8", "symbol": "WETH", "usd_price": "1991.024"},
                "quantity": 1, "taker": None, "transaction": None,
                "item": {"nft_id": "ethereum/0x387c41b0b2f1128de44db1bcf8baad085f26392c/7531"}}}]
REAL_INVALIDATE = ["1", None, "collection:argonauts", "order_invalidate", {
    "event_type": "order_invalidate", "version": 2, "sent_at": "2026-09-09T10:18:42.742000Z",
    "payload": {"chain": "ethereum", "collection": {"slug": "argonauts"},
                "event_timestamp": "2026-09-09T10:18:42.160000Z",
                "item": {"nft_id": "ethereum/0x387c41b0b2f1128de44db1bcf8baad085f26392c/4842"},
                "order_hash": "0x7cb446f85c8c0599afb6b5b1f87f26e3a1cab486"}}]
REAL_COLL_OFFER = ["1", None, "collection:argonauts", "collection_offer", {
    "event_type": "collection_offer", "version": 1, "sent_at": "2026-09-09T10:19:24.041000Z",
    "payload": {"asset_contract_criteria": {"address": "0x387c41b0b2f1128de44db1bcf8baad085f26392c"},
                "base_price": "348000000000000000", "chain": "ethereum",
                "collection": {"slug": "argonauts"}, "collection_criteria": {"slug": "argonauts"},
                "event_timestamp": "2026-09-09T10:19:23.500000Z",
                "expiration_date": "2026-09-10T10:19:23.000000Z",
                "maker": {"address": "0xbffec906aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                "order_hash": "0xco11ec7i0n0ffer0000000000000000000000000",
                "payment_token": {"decimals": 18, "eth_price": "0.348", "symbol": "WETH", "usd_price": "866.1"},
                "quantity": 1}}]
# A trait offer in the shape the stream actually sends (FACTS 2026-09-09: 39 of
# them in 40 minutes). `trait_criteria` is the single form and may be null;
# `trait_criteria_list` is an AND across entries and 16 of 39 had ONLY that form;
# `numeric_trait_criteria_list` carries ranges we cannot evaluate against the
# string traits table. Criteria here are patched per-test; this is the shape.
REAL_TRAIT_OFFER = ["1", None, "collection:argonauts", "trait_offer", {
    "event_type": "trait_offer", "version": 1, "sent_at": "2026-09-09T10:19:31.402000Z",
    "payload": {"base_price": "410000000000000000", "chain": "ethereum",
                "collection": {"slug": "argonauts"},
                "event_timestamp": "2026-09-09T10:19:31.000000Z",
                "expiration_date": "2026-09-10T10:19:31.000000Z",
                "maker": {"address": "0xtra170ffer00000000000000000000000000000"},
                "order_hash": "0x7ra170ffer000000000000000000000000000001",
                "payment_token": {"decimals": 18, "eth_price": "0.41", "symbol": "WETH",
                                  "usd_price": "1020.5"},
                "quantity": 1, "item": None,
                "trait_criteria": {"trait_type": "Print", "trait_name": "Unclaimed"},
                "trait_criteria_list": [{"trait_type": "Print", "trait_name": "Unclaimed"}]}}]
# Real, from the same capture: the first Argonauts sale and listing ever recorded.
REAL_LISTING = ["1", None, "collection:argonauts", "item_listed", {
    "event_type": "item_listed", "sent_at": "2026-09-09T10:27:22.000000Z",
    "payload": {"base_price": "1590000000000000000", "chain": "ethereum", "collection": {"slug": "argonauts"},
                "event_timestamp": "2026-09-09T10:27:21.795000Z", "expiration_date": "2026-10-09T10:27:21.000000Z",
                "is_private": False, "listing_date": "2026-09-09T10:27:21.000000Z", "listing_type": None,
                "item": {"nft_id": "ethereum/0x387c41b0b2f1128de44db1bcf8baad085f26392c/4027"},
                "maker": {"address": "0x217a9b455146ee1e89c57cde48ba883b786548dc"},
                "order_hash": "0xbf626b87d23e4cf4b20ce2ebb2002526c786133b1b6c5ce644fc1f7280b82d38",
                "payment_token": {"address": "0x0000000000000000000000000000000000000000", "decimals": 18,
                                  "eth_price": "1.59", "name": "Ethereum", "symbol": "ETH", "usd_price": "3959.5929"},
                "quantity": 1, "taker": None}}]
REAL_SALE = ["1", None, "collection:argonauts", "item_sold", {
    "event_type": "item_sold", "sent_at": "2026-09-09T10:26:12.000000Z",
    "payload": {"sale_price": "1430000000000000000", "chain": "ethereum", "closing_date": "2026-09-09T10:26:11.000000Z",
                "collection": {"slug": "argonauts"}, "event_timestamp": "2026-09-09T10:26:11.000000Z",
                "is_private": False, "listing_type": None,
                "item": {"nft_id": "ethereum/0x387c41b0b2f1128de44db1bcf8baad085f26392c/8119"},
                "maker": {"address": "0xb78bf0baea59c9d108b43f10a013dfbce5717d1c"},
                "taker": {"address": "0x395b9f085048faaa4bcc6c63a98abefb164e8e61"},
                "order_hash": "0xaef8bbe92f2f02f22b48e534518eee5d74e66226cd004bf14df2856b229c17b3",
                # NOTE the semantics: on a SALE these are the TOKEN'S RATE (WETH/ETH, ETH/USD),
                # not the order's value as they are on bids and listings.
                "payment_token": {"address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "decimals": 18,
                                  "eth_price": "1.000863124510729", "name": "Wrapped Ethereum", "symbol": "WETH",
                                  "usd_price": "2493.1"},
                "quantity": 1,
                "transaction": {"hash": "0x5f4b0611f92bcd0b33e9df47e5bd0e18429a3d35a2faf8bf122e52c60445f62e",
                                "timestamp": "1788949571"}}}]
DOC_LISTING, DOC_SALE = REAL_LISTING, REAL_SALE   # names kept for the tests below


def _env(seq: int, raw: list, recv: str, run: str = "run-ui") -> dict:
    return {"_seq": seq, "_run": run, "_recv": recv, "_topic": raw[2], "_ets": raw[4]["payload"].get("event_timestamp"),
            "raw": json.dumps(raw, separators=(",", ":"))}


def test_normalizer_parses_real_frames() -> None:
    """Structure only: every field the metrics need, from frames OpenSea actually sent."""
    from navanax.normalize import parse_event

    r = parse_event(_env(1, REAL_BID, "2026-09-09T10:20:16.619812Z"))
    check("parse: item_received_bid -> row", r is not None and r["event_type"] == "item_received_bid")
    check("parse: token id recovered from the Seaport consideration (nft_id lacks it)",
          r["token_id"] == "7531", f"got {r['token_id']}")
    check("parse: contract from nft_id", r["contract"] == "0x387c41b0b2f1128de44db1bcf8baad085f26392c")
    check("parse: ETH and USD both stored, primary recorded as the ORDER's own values (REQ-F-02)",
          r["price_eth"] == 0.88 and r["price_usd"] == 2190.1264)
    check("parse: implied ETH/USD carried for later audit against a second provider",
          abs(r["implied_ethusd"] - 2488.78) < 0.01, f"got {r['implied_ethusd']}")
    check("parse: wei kept as text, never a float", r["price_wei"] == "880000000000000000")
    check("parse: both timestamps -- observed (ours) and valid (market's)",
          r["observed_at"] == "2026-09-09T10:20:16.619812Z" and r["valid_at"] == "2026-09-09T10:18:39.860000Z"
          and r["valid_ts"] < r["observed_ts"], "the stream delivered a 97-second-old event on connect")
    check("parse: maker, order_hash, expiration",
          r["maker"].startswith("0x0d9ec524") and r["order_hash"].startswith("0x39f8")
          and r["expiration_at"] == "2026-09-09T10:48:37.000000Z")

    c = parse_event(_env(2, REAL_CANCEL, "2026-09-09T10:20:16.7Z"))
    check("parse: item_cancelled -> row with order_hash for lifecycle matching",
          c["event_type"] == "item_cancelled" and c["order_hash"] == r["order_hash"] and c["token_id"] == "7531")
    inv = parse_event(_env(3, REAL_INVALIDATE, "2026-09-09T10:20:16.8Z"))
    check("parse: order_invalidate has no price and that is fine",
          inv["price_eth"] is None and inv["order_hash"].startswith("0x7cb4"))
    co = parse_event(_env(4, REAL_COLL_OFFER, "2026-09-09T10:20:17Z"))
    check("parse: collection_offer -> contract from asset_contract_criteria, no token id",
          co["contract"].startswith("0x387c") and co["token_id"] is None and co["price_eth"] == 0.348)
    s = parse_event(_env(5, REAL_SALE, "2026-09-09T10:26:13Z"))
    check("parse: item_sold uses sale_price and carries taker + tx",
          s["price_wei"] == "1430000000000000000" and s["taker"].startswith("0x395b") and s["tx_hash"].startswith("0x5f4b"))
    check("parse: a SALE's eth_price is a RATE, not a value -- price is units x rate (found in real data)",
          abs(s["price_eth"] - 1.43 * 1.000863124510729) < 1e-9 and s["price_basis"] == "units_x_rate",
          f"got {s['price_eth']} basis={s['price_basis']} -- trusting the field gives 1.00, a 30% error")
    check("parse: ...and USD follows the same rule", abs(s["price_usd"] - 1.43 * 2493.1) < 1e-6)
    lst = parse_event(_env(6, REAL_LISTING, "2026-09-09T10:27:23Z"))
    check("parse: a LISTING's eth_price is the order value, and the row says which basis was used",
          lst["price_eth"] == 1.59 and lst["price_basis"] == "order_value")
    check("parse: a BID's eth_price is the order value too", r["price_basis"] == "order_value")
    two = json.loads(json.dumps(REAL_COLL_OFFER))
    two[4]["payload"].update({"base_price": "453000000000000000", "quantity": 2,
                              "payment_token": {"decimals": 18, "eth_price": "0.906", "symbol": "WETH",
                                                "usd_price": "2257.0"}})
    t = parse_event(_env(7, two, "2026-09-09T10:30:00Z"))
    check("parse: a 2-item offer reports its TOTAL; the row stores the PER-ITEM price (29 real rows in hour one)",
          abs(t["price_eth"] - 0.453) < 1e-12 and abs(t["price_usd"] - 1128.5) < 1e-9
          and t["quantity"] == 2 and t["price_basis"] == "order_value", f"got {t['price_eth']} {t['price_basis']}")
    check("parse: control frames -> None",
          parse_event({"_topic": "__control__", "raw": "[]"}) is None)
    check("parse: junk -> None, never an exception",
          parse_event({"_topic": "x", "raw": "not json"}) is None)


def test_normalizer_is_incremental(tmp: Path) -> None:
    """Reads only new frames each pass; an OPEN file's later frames arrive on the next pass."""
    from navanax.normalize import Normalizer

    clock = FakeClock(datetime(2026, 9, 9, 10, 20, 0, tzinfo=timezone.utc))
    root = tmp / "lz-norm"
    w = LandingZoneWriter(root, "run-ui", codec=GzipCodec(), clock=clock.now,
                          monotonic=clock.monotonic, flush_events=2, auto_flush=False)
    for raw in (REAL_BID, REAL_CANCEL, REAL_INVALIDATE, REAL_COLL_OFFER):
        w.write(json.dumps(raw), topic="collection:argonauts",
                event_timestamp=raw[4]["payload"]["event_timestamp"])
        clock.advance(1)
    w.flush()   # 4 events in closed frames, file still OPEN
    n = Normalizer(root, tmp / "lz-norm.sqlite")
    s1 = n.sync()
    check("normalizer: first pass reads every complete frame of an open file",
          s1["rows_added"] == 4, f"got {s1}")
    s2 = n.sync()
    check("normalizer: second pass with nothing new adds nothing", s2["rows_added"] == 0, f"got {s2}")

    w.write(json.dumps(DOC_LISTING), topic="collection:argonauts", event_timestamp="2026-09-09T10:30:00.000000Z")
    w.write(json.dumps(DOC_SALE), topic="collection:argonauts", event_timestamp="2026-09-09T10:40:00.000000Z")
    w.flush()
    s3 = n.sync()
    check("normalizer: new frames on the SAME open file are picked up", s3["rows_added"] == 2, f"got {s3}")
    w.close()
    s4 = n.sync()
    check("normalizer: closing the file adds no duplicates", s4["rows_added"] == 0
          and n.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 6)
    s5 = n.sync()
    check("normalizer: a fully-read closed file is skipped entirely", s5["files_read"] == 0, f"got {s5}")
    n.close()


def test_metric_engine_contract(tmp: Path) -> None:
    """The MetricRequest tuple (docs/06 §3): intervals from config, transforms,
    immediacy undefined when either side is missing, basis on every response."""
    from navanax.metrics import MetricEngine, apply_transform, bucket_of, load_intervals
    from navanax.normalize import Normalizer, refresh_order_lives

    iv = load_intervals(ROOT / "config" / "intervals.yaml")
    check("metrics: every interval Spencer listed is in config, not code",
          all(k in iv["intervals"] for k in ("1m", "3h", "6h", "1h", "1d", "2d", "1w", "1mo", "2mo", "3mo", "6mo", "1y", "2y"))
          and all(k in iv["anchored"] for k in ("HTD", "DTD", "MTD", "YTD")))

    # calendar arithmetic is not duration arithmetic (docs/06 §1.3)
    ts = datetime(2026, 9, 9, 15, 30, tzinfo=timezone.utc).timestamp()
    b = datetime.fromtimestamp(bucket_of(ts, iv["intervals"]["3mo"], "America/Chicago"), tz=timezone.utc)
    check("metrics: a 3-month bucket starts on a calendar quarter in the DISPLAY timezone",
          b.astimezone(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d %H:%M") == "2026-07-01 00:00", f"got {b}")
    b1h = bucket_of(ts, iv["intervals"]["1h"], "America/Chicago")
    check("metrics: sub-day buckets are UTC-aligned", b1h == datetime(2026, 9, 9, 15, tzinfo=timezone.utc).timestamp())

    v, basis = apply_transform([None, 2.0, 3.0, None, 1.0], "PCT")
    check("metrics: PCT baseline is the first non-null value and is reported",
          basis["baseline_value"] == 2.0 and v[2] == 0.5 and v[4] == -0.5 and v[0] is None and v[3] is None)
    lv, _ = apply_transform([1.0, 1.5, 1.0], "LOG")
    check("metrics: LOG is symmetric under reversal (docs/06 §2.2)",
          abs(lv[1] - 0.405465) < 1e-5 and abs(lv[2]) < 1e-12, f"got {lv}")
    bp, _ = apply_transform([100.0, 101.0], "BPS")
    check("metrics: BPS = PCT x 10,000", abs(bp[1] - 100.0) < 1e-9)

    # a small store: one bid, one collection offer, one listing, one sale
    n = Normalizer(tmp / "empty-lz", tmp / "me.sqlite")
    for i, (raw, recv) in enumerate(((REAL_BID, "10:20:16"), (REAL_CANCEL, "10:20:16"),  # noqa: B007
                                     (REAL_COLL_OFFER, "10:20:17"),
                                     (DOC_LISTING, "10:30:01"), (DOC_SALE, "10:40:01")), 1):
        from navanax.normalize import COLS, parse_event
        row = parse_event(_env(i, raw, f"2026-09-09T{recv}Z"))
        row["file"] = "f"
        n.conn.execute(f"INSERT INTO events ({','.join(COLS)}) VALUES ({','.join('?'*len(COLS))})",
                       tuple(row.get(c) for c in COLS))
    n.conn.commit()
    # These rows were inserted straight into `events`; in production sync() folds
    # order_lives on every pass. The standing book is read from that relation now.
    refresh_order_lives(n.conn)
    eng = MetricEngine(n.conn, iv, "America/Chicago")
    now = datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc)

    s = eng.series(metric="immediacy_cost", collection="argonauts", interval="1h", range_="6h", now=now)
    defined = [(t, v) for t, v in zip(s["t"], s["raw"], strict=True) if v is not None]
    check("metrics: immediacy_cost = lowest ask - highest collection offer, in the ONE interval with both",
          len(defined) == 1 and abs(defined[0][1] - (1.59 - 0.348)) < 1e-9, f"got {s['raw']}")
    check("metrics: the series is on the FULL bucket grid -- 6 hourly buckets in a 6h window, 5 of them null holes (tech-lead F1)",
          len(s["t"]) == 6 and s["basis"]["buckets"] == 6 and s["basis"]["undefined_buckets"] == 5, f"got {s['t']}")
    check("metrics: ...and % of ask alongside (REQ-F-13a: both percentage and absolute)",
          abs([p for p in s["pct_of_ask"] if p is not None][0] - (1.59 - 0.348) / 1.59) < 1e-9)
    s5 = eng.series(metric="immediacy_cost", collection="argonauts", interval="5m", range_="6h",
                    now=now, book="observed")
    check("metrics (book='observed'): at 5m the offer and the listing fall in different buckets "
          "-> UNDEFINED, not filled -- the OLD behaviour, still reachable and labelled",
          all(v is None for v in s5["raw"]) and s5["basis"]["undefined_buckets"] == len(s5["raw"]) and len(s5["raw"]) == 72,
          f"got {len(s5['raw'])}")
    check("metrics (book='observed'): ...and the response warns that it is not the docs/01 §3.2 quantity",
          "interval" in s5["basis"]["observed_book_warning"].lower()
          and "BUG-20260910-057" in s5["basis"]["observed_book_warning"])
    s5s = eng.series(metric="immediacy_cost", collection="argonauts", interval="5m", range_="6h", now=now)
    # The correction BUG-20260910-057 exists for: the offer (valid at 10:19:23,
    # expires 2026-09-10) and the listing (valid at 10:27:21, expires 2026-10-09)
    # are both STANDING from 10:27:21 onward, so all seven 5m buckets from 10:25
    # to 11:00 have a real spread. The interval-extremum version called every one
    # of them undefined because the two legs were SEEN in different buckets.
    defined5 = [v for v in s5s["raw"] if v is not None]
    check("metrics (book='standing', the default): the two legs are both RESTING after 10:27 so the "
          "spread is defined in every 5m bucket after it -- the extremum version saw none of them",
          len(defined5) == 7 and all(abs(v - (1.59 - 0.348)) < 1e-9 for v in defined5)
          and s5s["basis"]["book"] == "standing", f"got {defined5}")
    u = eng.series(metric="floor_ask", collection="argonauts", denomination="USD", interval="1h", range_="6h", now=now)
    check("metrics: USD denomination uses the event's own USD at its timestamp",
          [v for v in u["raw"] if v is not None] == [3959.5929] and u["raw"].count(None) == 5)
    fa = eng.series(metric="floor_ask", collection="argonauts", interval="1h", range_="6h", now=now)
    check("metrics: a plain metric with two observations hours apart has holes between them, not a line",
          fa["raw"].index(1.59) > 0 and None in fa["raw"], f"got {fa['raw']}")
    try:
        eng.series(metric="floor_ask", collection="argonauts", interval="1m", range_="YTD", now=now)
        check("metrics: an absurd grid is refused, not silently thinned", False)
    except ValueError as exc:
        check("metrics: an absurd grid is refused, not silently thinned", "buckets" in str(exc))
    check("metrics: basis says how buckets are aligned (UTC sub-day, local midnight day+)",
          s["basis"]["bucket_alignment"] == "UTC"
          and eng.series(metric="floor_ask", collection="argonauts", interval="1d", range_="7d", now=now)["basis"]["bucket_alignment"].startswith("local midnight"))
    check("metrics: every response carries its basis (docs/06 §2.2)",
          all(k in u["basis"] for k in ("metric", "denomination", "transform", "interval", "range", "wash_filter",
                                        "as_of", "display_timezone", "baseline_value")))
    check("metrics: wash_filter is honestly reported as raw (no filter exists)", u["basis"]["wash_filter"] == "raw")
    try:
        eng.series(metric="floor_ask", collection="argonauts", interval="7m", now=now)
        bad = False
    except ValueError:
        bad = True
    check("metrics: an interval not in config is REFUSED, not improvised", bad)

    book = eng.live_book("argonauts", now=now)
    # now=11:00: the bid was CANCELLED at 10:19:02 (and would also have expired at 10:48);
    # the listing (#4027, a different order from the #8119 sale) still stands; so does the offer.
    check("metrics: live book -- the cancelled bid is gone, the unsold listing stands, the collection offer stands",
          book["item_bids"] == [] and len(book["asks"]) == 1 and book["asks"][0]["token_id"] == "4027"
          and len(book["collection_offers"]) == 1,
          f"got bids={len(book['item_bids'])} asks={len(book['asks'])} coll={len(book['collection_offers'])}")
    lt = eng.bid_lifetimes("argonauts", 0, now.timestamp())
    # PR-3 / REQ-F-19: the duration is still cancel.valid_ts - bid.valid_ts and
    # `n` still counts orders, but at n = 1 the percentiles are WITHHELD, not
    # printed with a warning beside them (BUG-20260910-057, Q-V3). The duration
    # itself is checked in test_percentiles_are_withheld_below_min_n, above n.
    check("metrics: bid lifetime carries its n, and at n = 1 every percentile is withheld",
          lt["n"] == 1 and lt["median_s"] is None and lt["p10_s"] is None and lt["p90_s"] is None
          and lt["percentiles_reliable"] is False and lt["percentiles_withheld"] is True, f"got {lt}")
    n.close()


def test_top_item_bid_declares_the_book_it_has(tmp: Path) -> None:
    """Tech-lead re-review 2026-09-10, T3. `top_item_bid` carried no `book` key,
    and three separate untruths followed from that one omission:

      1. `series(book='standing')` was refused with the reason "it is a count or
         flow metric, not a resting-book quantity". `top_item_bid` is a PRICE
         metric and it does have a resting book -- STANDING_KINDS['item_bid'].
         The real reason is leg discipline: an item bid is per-token, and the
         union bid leg that would make it comparable is PR-5. A refusal that
         misstates why is worse than no refusal, because it is the sentence the
         next person reasons from.
      2. The basis printed `book: "n/a (top_item_bid is a count/flow metric...)"`,
         so the Prices panel told the Operator the wrong thing about a line it
         was drawing.
      3. `observed_book_warning` is gated on `"book" in spec`, so the one price
         line on that panel that is ALWAYS an interval extremum was the only one
         carrying no warning that it is one.

    The DEFAULT does not change -- it stays `observed`. What changes is that the
    refusal, the basis and the warning now say something true.
    """
    from navanax.metrics import (
        METRICS,
        STANDING_KINDS,
        STANDING_NOT_OFFERED,
        MetricEngine,
        load_intervals,
    )
    from navanax.normalize import COLS, Normalizer, parse_event, refresh_order_lives

    iv = load_intervals(ROOT / "config" / "intervals.yaml")
    n = Normalizer(tmp / "tib-lz", tmp / "tib.sqlite")
    for i, (raw, recv) in enumerate(((REAL_BID, "10:20:16"), (REAL_COLL_OFFER, "10:20:17"),
                                     (DOC_LISTING, "10:30:01")), 1):
        row = parse_event(_env(i, raw, f"2026-09-09T{recv}Z"))
        row["file"] = "f"
        n.conn.execute(f"INSERT INTO events ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                       tuple(row.get(c) for c in COLS))
    n.conn.commit()
    refresh_order_lives(n.conn)
    eng = MetricEngine(n.conn, iv, "America/Chicago")
    now = datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc)

    spec = METRICS["top_item_bid"]
    check("top_item_bid: the metric declares the standing-book kind it actually has, "
          "and its default stays 'observed' (the Operator's leg discipline is unchanged)",
          spec.get("book") == "item_bid" and spec["book"] in STANDING_KINDS
          and spec.get("book_default") == "observed",
          f"book={spec.get('book')!r} default={spec.get('book_default')!r}")

    s = eng.series(metric="top_item_bid", collection="argonauts", interval="1h", range_="6h", now=now)
    check("top_item_bid: the basis reads book 'observed' -- not the false 'n/a (count/flow...)'",
          s["basis"]["book"] == "observed", f"got {s['basis']['book']!r}")
    check("top_item_bid: ...and the observed-book warning is ATTACHED to the line that most needs it",
          "observed_book_warning" in s["basis"]
          and "BUG-20260910-057" in s["basis"]["observed_book_warning"]
          and "top_item_bid" in s["basis"]["observed_book_warning"],
          f"basis keys {sorted(s['basis'])}")
    check("top_item_bid: the DEFAULT series is unchanged -- still the interval extremum, 0.88 ETH",
          0.88 in s["raw"], f"got {s['raw']}")

    try:
        eng.series(metric="top_item_bid", collection="argonauts", interval="1h",
                   range_="6h", now=now, book="standing")
        refusal = ""
    except ValueError as exc:
        refusal = str(exc)
    low = refusal.lower()
    check("top_item_bid: book='standing' is STILL refused -- the union bid leg is PR-5", bool(refusal))
    check("top_item_bid: ...and the refusal names the TRUE reason (per-token, PR-5), never 'count or flow'",
          "per-token" in low and "pr-5" in low
          and "count" not in low and "flow" not in low, refusal[:240])
    check("top_item_bid: the reason the caller is handed is the one the module documents",
          STANDING_NOT_OFFERED["top_item_bid"] in refusal)

    # The primitive is reachable, which is what makes the refusal a routing
    # decision rather than a missing capability.
    st = eng.standing_series("item_bid", "argonauts", now.timestamp() - 6 * 3600,
                             now.timestamp(), iv["intervals"]["1h"], "ETH", None, now)
    check("top_item_bid: standing_series('item_bid', ...) still answers directly -- only the "
          "metric default is withheld",
          st["basis"]["book"] == "standing" and len(st["median"]) == 6, f"got {list(st['basis'])}")

    # A count metric must keep the OTHER reason, unchanged: the two refusals are
    # different facts and collapsing them is what caused this.
    try:
        eng.series(metric="sales_count", collection="argonauts", interval="1h",
                   range_="6h", now=now, book="standing")
        cnt = ""
    except ValueError as exc:
        cnt = str(exc)
    check("counts keep the other refusal: a count metric really has no resting book",
          "count or flow" in cnt.lower(), cnt[:160])
    n.close()


def test_min_n_for_percentiles_cannot_drift_from_assumptions_yaml() -> None:
    """Tech-lead re-review 2026-09-10, item 3. `min_n_for_percentiles: 30` is written
    twice -- once in config/assumptions.yaml (ASM-021, where the Operator and the
    reviewer read it) and once as metrics.MIN_N_FOR_PERCENTILES (where it is
    enforced). Nothing tied them together, so editing the assumption register
    would have changed the documented threshold and not the code, and the register
    is the artifact a reviewer trusts. REQ-N-09 says the threshold belongs in
    config; until the MetricEngine constructor is allowed to take it, this
    assertion is what keeps the two copies honest.
    """
    import yaml

    from navanax.metrics import MIN_N_FOR_PERCENTILES

    doc = yaml.safe_load((ROOT / "config" / "assumptions.yaml").read_text())
    hits = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "min_n_for_percentiles" in node:
                hits.append(node["min_n_for_percentiles"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    check("assumptions: config/assumptions.yaml states min_n_for_percentiles exactly once",
          len(hits) == 1, f"found {hits}")
    check("assumptions: the stated threshold IS the one metrics.py enforces (ASM-021 cannot drift)",
          bool(hits) and hits[0] == MIN_N_FOR_PERCENTILES,
          f"assumptions.yaml says {hits} · metrics.MIN_N_FOR_PERCENTILES is {MIN_N_FOR_PERCENTILES}")


def test_dashboard_serves_localhost_only(tmp: Path) -> None:
    """REQ-N-13, and a smoke test of every API route over real HTTP."""
    import http.client
    import shutil as _sh
    import threading as _th
    from http.server import ThreadingHTTPServer

    from navanax.dashboard import Dashboard, make_handler, serve

    root = tmp / "dashroot"
    (root / "config").mkdir(parents=True)
    for f in ("base.yaml", "intervals.yaml", "watchlist.yaml"):
        _sh.copy(ROOT / "config" / f, root / "config" / f)
    import yaml
    cfg = yaml.safe_load((root / "config" / "base.yaml").read_text())
    clock = FakeClock(datetime(2026, 9, 9, 10, 20, 0, tzinfo=timezone.utc))
    w = LandingZoneWriter(root / cfg["landing"]["root"], "run-d", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic, auto_flush=False)
    for raw in (REAL_BID, REAL_COLL_OFFER, DOC_LISTING):
        w.write(json.dumps(raw), topic="collection:argonauts", event_timestamp=raw[4]["payload"]["event_timestamp"])
    w.close()

    try:
        serve(root, cfg, ["argonauts"], host="0.0.0.0", port=0, open_browser=False)
        refused = False
    except ValueError as exc:
        refused = "REQ-N-13" in str(exc)
    check("dashboard: REFUSES to bind anything but loopback (REQ-N-13)", refused)

    dash = Dashboard(root, cfg, ["argonauts"])
    dash._sync_once()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dash))
    port = httpd.server_address[1]
    t = _th.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path)
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, body
        st, body = get("/api/status")
        j = json.loads(body)
        check("dashboard: /api/status", st == 200 and j["store"]["events"] == 3, f"{st} {body[:120]}")
        for path in ("/api/meta", "/api/series?metric=floor_ask&collection=argonauts&interval=1h&range=YTD",
                     "/api/multi?collection=argonauts&interval=1h&range=YTD", "/api/book?collection=argonauts",
                     "/api/tape?collection=argonauts", "/api/makers?collection=argonauts&range=YTD",
                     "/api/lifetimes?collection=argonauts&range=YTD", "/api/mix?range=YTD", "/api/gaps", "/api/audit",
                     "/api/survival?collection=argonauts&range=YTD",
                     "/api/survival_drill?collection=argonauts&range=YTD&lo=0&hi=10"):
            st, body = get(path)
            check(f"dashboard: {path.split('?')[0]} -> 200 JSON", st == 200 and body[:1] in (b"{", b"["),
                  f"{st} {body[:100]}")
        st, body = get("/api/series?metric=nope&collection=argonauts")
        check("dashboard: a bad request is a 400 with the reason, not a crash",
              st == 400 and b"unknown metric" in body)
        # PR-3: `book` is a query param the engine owns the default for, and the
        # response always says which book produced the number.
        st, body = get("/api/series?metric=immediacy_cost&collection=argonauts&interval=1h&range=YTD")
        check("dashboard: /api/series defaults immediacy_cost to the STANDING book",
              st == 200 and json.loads(body)["basis"]["book"] == "standing", f"{st} {body[:160]}")
        st, body = get("/api/series?metric=immediacy_cost&collection=argonauts&interval=1h&range=YTD&book=observed")
        j = json.loads(body)
        check("dashboard: ...and book=observed reaches the engine and is labelled as what it is",
              st == 200 and j["basis"]["book"] == "observed"
              and "BUG-20260910-057" in j["basis"]["observed_book_warning"], f"{st} {body[:160]}")
        st, body = get("/api/series?metric=sales_count&collection=argonauts&interval=1h&range=YTD&book=standing")
        check("dashboard: asking a flow metric for a standing book is a 400 with the reason, "
              "not a number computed from a book that does not exist",
              st == 400 and b"no standing-book variant" in body, f"{st} {body[:160]}")
        # PR-8: the survival endpoints refuse a malformed request rather than
        # guessing a bin or a price band, and the refusal says which field.
        st, body = get("/api/survival_drill?collection=argonauts&range=YTD&lo=10&hi=5")
        check("dashboard: /api/survival_drill refuses an inverted bin with a 400 and the reason",
              st == 400 and b"lo < hi" in body, f"{st} {body[:120]}")
        st, body = get("/api/survival?collection=argonauts&range=YTD&price_band=cheap")
        check("dashboard: /api/survival refuses a price band it cannot parse, rather than ignoring it "
              "-- a silently dropped filter is a wrong answer that looks right",
              st == 400 and b"price_band" in body, f"{st} {body[:120]}")
        st, body = get("/api/survival?collection=argonauts&range=YTD&as_of=yesterday")
        check("dashboard: /api/survival refuses an unparseable as_of instead of defaulting to now",
              st == 400 and b"as_of" in body, f"{st} {body[:120]}")
        st, body = get("/")
        check("dashboard: serves the page", st == 200 and b"navanax" in body.lower())
        st, _ = get("/../pyproject.toml")
        check("dashboard: no path traversal out of the ui dir", st == 404)
    finally:
        httpd.shutdown()
        httpd.server_close()
        dash.norm.close()


def test_traits_pipeline(tmp: Path) -> None:
    """Trait onboarding (REQ-F-07): metadata parsing tolerates the common shapes,
    the token list resumes from its cursor, and the fallback is budgeted so a
    broken metadata host cannot spend the hour's REST allowance."""
    import asyncio

    from navanax.opstore import OperationalStore
    from navanax.traits import TraitsJob, ensure_schema, open_store, parse_attributes, trait_values

    check("traits: ERC-721 attributes list parses, numbers become strings",
          parse_attributes({"attributes": [{"trait_type": "Eyes", "value": "Laser"}, {"trait_type": "Level", "value": 3}]})
          == [("Eyes", "Laser"), ("Level", "3")])
    check("traits: dict-shaped and `traits`-keyed metadata both parse",
          parse_attributes({"traits": {"Background": "Blue"}}) == [("Background", "Blue")])
    check("traits: junk metadata yields no traits, no crash",
          parse_attributes({"attributes": "nope"}) == [] and parse_attributes({}) == [])

    class FakeRest:
        """Two pages of tokens, then a per-token endpoint. Counts every call."""
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.requests_made = 0
        async def get(self, path, params=None, *, priority=None, retries=3):
            self.calls.append(path)
            self.requests_made += 1
            if path.endswith("/nfts") and "collection/" in path:
                if not (params or {}).get("next"):
                    return 200, {"nfts": [{"identifier": "1", "contract": "0xc", "name": "Argo #1",
                                           "metadata_url": "ipfs://Qm1"}], "next": "page2"}
                return 200, {"nfts": [{"identifier": "2", "contract": "0xc", "name": "Argo #2",
                                       "metadata_url": None}], "next": None}
            if "/contract/0xc/nfts/2" in path:
                return 200, {"nft": {"traits": [{"trait_type": "Eyes", "value": "Laser"}]}}
            return 404, {}

    conn = open_store(tmp / "an.sqlite")
    ensure_schema(conn)
    ops = OperationalStore(tmp / "traits-ops.db")
    rest = FakeRest()
    job = TraitsJob(conn, rest, ops, slug="argonauts", opensea_fallback_budget=1)
    job._fetch_json = lambda url: {"attributes": [{"trait_type": "Background", "value": "Blue"}]}  # no network
    out = asyncio.run(job.list_tokens())
    check("traits: token list walks every page through the governed client", out["pages"] == 2 and out["tokens"] == 2)
    st = next(r for r in ops.onboarding_status() if r["collection_slug"] == "argonauts")
    check("traits: onboarding advances tokens -> traits and records reads spent",
          st["state"] == "traits" and st["requests_spent"] == 2, str(st))
    check("traits: a second list_tokens is a no-op, not a second 47-read walk",
          asyncio.run(job.list_tokens()).get("skipped") and len(rest.calls) == 2)

    res = asyncio.run(job.fetch_traits())
    check("traits: direct metadata_url is used when present; OpenSea fallback only when it is not",
          res["ok"] == 2 and rest.calls.count("/chain/ethereum/contract/0xc/nfts/2") == 1
          and not any("/nfts/1" in c for c in rest.calls), str(rest.calls))
    vals = trait_values(conn, "argonauts")
    check("traits: filter panel sees every trait type with counts",
          vals == {"Background": [{"value": "Blue", "n": 1}], "Eyes": [{"value": "Laser", "n": 1}]}, str(vals))
    check("traits: ipfs:// resolves through the configured gateway",
          job._resolve_url("ipfs://Qm1/meta.json") == "https://ipfs.io/ipfs/Qm1/meta.json")
    check("traits: onboarding reaches complete", any(r["state"] == "complete" for r in ops.onboarding_status()))
    conn.close()

    from navanax.traits import is_opensea_host
    check("traits: OpenSea-hosted metadata is recognised (subdomains too), other hosts are not",
          is_opensea_host("https://api.opensea.io/api/v2/x") and is_opensea_host("https://i.seadn.io/a.json")
          and not is_opensea_host("https://ipfs.io/ipfs/Qm") and not is_opensea_host("https://notopensea.io/x"))

    # -- second store: a token on an OpenSea host, a token whose host fails, a two-valued trait
    conn = open_store(tmp / "an2.sqlite")
    ensure_schema(conn)
    ops2 = OperationalStore(tmp / "traits-ops2.db")

    class Rest2(FakeRest):
        async def get(self, path, params=None, *, priority=None, retries=3):
            self.calls.append(path)
            self.requests_made += 1
            if path.endswith("/nfts") and "collection/" in path:
                return 200, {"nfts": [
                    {"identifier": "10", "contract": "0xc", "name": "OS-hosted", "metadata_url": "https://api.opensea.io/meta/10"},
                    {"identifier": "11", "contract": "0xc", "name": "flaky", "metadata_url": "https://flaky.example/11"},
                    {"identifier": "12", "contract": "0xc", "name": "two-hats", "metadata_url": "https://ok.example/12"}], "next": None}
            if "/contract/0xc/nfts/10" in path:
                return 200, {"nft": {"traits": [{"trait_type": "Eyes", "value": "Laser"}]}}
            return 500, {}

    rest2 = Rest2()
    job2 = TraitsJob(conn, rest2, ops2, slug="argonauts", opensea_fallback_budget=1)
    attempts = {"flaky": 0}
    real_fetch = job2._fetch_json
    def fetch(url):
        if "opensea" in url:
            return real_fetch(url)                # must raise before any network
        if "flaky" in url:
            attempts["flaky"] += 1
            raise OSError("gateway down")
        return {"attributes": [{"trait_type": "Hat", "value": "Red"}, {"trait_type": "Hat", "value": "Blue"}]}
    job2._fetch_json = fetch
    asyncio.run(job2.list_tokens())
    res2 = asyncio.run(job2.fetch_traits())
    check("traits: a metadata_url on an OpenSea host is never fetched directly -- it goes through the governed fallback (F4)",
          rest2.calls.count("/chain/ethereum/contract/0xc/nfts/10") == 1
          and conn.execute("SELECT traits_source FROM tokens WHERE token_id='10'").fetchone()[0] == "opensea_nft", str(rest2.calls))
    check("traits: the fallback budget binds -- with budget 1 already spent, the flaky token gets NO OpenSea read",
          not any("/nfts/11" in c for c in rest2.calls), str(rest2.calls))
    check("traits: a token that failed keeps traits_at NULL so the next run retries it (F12)",
          conn.execute("SELECT traits_at, traits_error FROM tokens WHERE token_id='11'").fetchone()[0] is None
          and res2["failed"] == 1 and res2["ok"] == 2)
    check("traits: a two-valued trait keeps BOTH values (F13)",
          sorted(v for (v,) in conn.execute("SELECT value FROM traits WHERE token_id='12' AND trait_type='Hat'")) == ["Blue", "Red"])
    st2 = next(r for r in ops2.onboarding_status() if r["collection_slug"] == "argonauts")
    check("traits: onboarding stays 'traits' (not complete) while a token is unresolved, and reads spent = 1 list + 1 fallback",
          st2["state"] == "traits" and st2["requests_spent"] == 2, str(st2))
    asyncio.run(job2.fetch_traits())
    check("traits: the re-run retries only the unresolved token", attempts["flaky"] == 2)
    conn.close()


def test_list_pass_keeps_traits_it_is_given(tmp: Path) -> None:
    """BUG-20260909-054: the collection list endpoint carries `traits` (verified for
    Argonauts from a raw pull). The list pass used to discard them and plan
    9,161 per-token reads. Now a list entry with traits is stored at once; one
    without still goes through metadata_url / the budgeted fallback."""
    import asyncio

    from navanax.opstore import OperationalStore
    from navanax.traits import TraitsJob, open_store

    class Rest:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.requests_made = 0
        async def get(self, path, params=None, *, priority=None, retries=3):
            self.calls.append(path)
            self.requests_made += 1
            if path.endswith("/nfts") and "collection/" in path:
                return 200, {"nfts": [
                    {"identifier": "1", "contract": "0xc", "name": "A", "metadata_url": None,
                     "traits": [{"trait_type": "Cloak", "value": "Death"}, {"trait_type": "Bones", "value": "Bone"}]},
                    {"identifier": "2", "contract": "0xc", "name": "B", "metadata_url": None, "traits": []},
                    {"identifier": "3", "contract": "0xc", "name": "C", "metadata_url": None}], "next": None}
            if "/contract/0xc/nfts/" in path:
                return 200, {"nft": {"traits": [{"trait_type": "Cloak", "value": "Clergy"}]}}
            return 404, {}

    conn = open_store(tmp / "lp.sqlite")
    ops = OperationalStore(tmp / "lp-ops.db")
    rest = Rest()
    job = TraitsJob(conn, rest, ops, slug="argonauts", opensea_fallback_budget=5)
    r1 = asyncio.run(job.list_tokens())
    check("list pass: traits on the list response are stored during the list pass, source opensea_nft_list",
          r1["traits_from_list"] == 1
          and conn.execute("SELECT traits_source, traits_at IS NOT NULL FROM tokens WHERE token_id='1'").fetchone() == ("opensea_nft_list", 1)
          and sorted(conn.execute("SELECT trait_type, value FROM traits WHERE token_id='1'")) == [("Bones", "Bone"), ("Cloak", "Death")],
          str(r1))
    check("list pass: an empty or absent traits list leaves traits_at NULL (nothing invented)",
          [r[0] for r in conn.execute("SELECT traits_at FROM tokens WHERE token_id IN ('2','3') ORDER BY token_id")] == [None, None])
    r2 = asyncio.run(job.fetch_traits())
    check("list pass: the per-token fallback runs ONLY for tokens the list gave no traits for -- 2 reads, not 3",
          sorted(c for c in rest.calls if "/contract/" in c) == ["/chain/ethereum/contract/0xc/nfts/2", "/chain/ethereum/contract/0xc/nfts/3"]
          and r2["attempted"] == 2 and r2["ok"] == 2, str(rest.calls))
    check("list pass: the list-sourced token keeps its list traits (fallback never overwrote it)",
          conn.execute("SELECT value FROM traits WHERE token_id='1' AND trait_type='Cloak'").fetchone()[0] == "Death")
    conn.close()


def test_import_explorer_cache(tmp: Path) -> None:
    """`navanax import-traits`: zero REST, observation time = the cache's generated
    time, never overwrites, diffs what it cannot write, exact counts, idempotent."""
    from navanax.cli import main as cli_main
    from navanax.traits import import_explorer_cache, open_store

    gen = "2026-09-08T18:29:52Z"
    cache = {
        "1": {"id": "1", "name": "Argonaut #1", "image": "https://x/1.svg", "rank": 3, "price": 0.5,
              "traits": {"Cloak": "Death", "Bones": "Bone"}},
        "2": {"id": "2", "name": "Argonaut #2", "image": "https://x/2.svg", "traits": {"Cloak": "Clergy"}},
        "3": {"id": "3", "name": "Argonaut #3", "image": "https://x/3.svg", "traits": {"Cloak": "Clergy", "Relic": "Gold"}},
        "4": {"id": "4", "name": "Argonaut #4", "image": None, "traits": {}},
    }
    conn = open_store(tmp / "imp.sqlite")
    # token 2 already has traits from OpenSea and AGREES; token 3 has traits and DISAGREES on Cloak;
    # token 9 is in the table and not in the cache; token 1 is in the table with no traits yet.
    conn.execute("INSERT INTO tokens (collection, token_id, listed_at) VALUES ('argonauts','1','2026-09-09T00:00:00Z')")
    for tid in ("2", "3"):
        conn.execute("INSERT INTO tokens (collection, token_id, listed_at, traits_at, traits_source) "
                     "VALUES ('argonauts',?,'2026-09-09T00:00:00Z','2026-09-09T01:00:00Z','opensea_nft')", (tid,))
    conn.execute("INSERT INTO tokens (collection, token_id, listed_at) VALUES ('argonauts','9','2026-09-09T00:00:00Z')")
    conn.executemany("INSERT INTO traits VALUES ('argonauts',?,?,?)",
                     [("2", "Cloak", "Clergy"), ("3", "Cloak", "Death"), ("3", "Relic", "Gold")])
    conn.commit()

    r = import_explorer_cache(conn, cache, slug="argonauts", generated_at=gen)
    check("import: exact counts -- 1 imported (token 1, present without traits), 2 skipped, 1 inserted (4), 1 without traits",
          (r["imported"], r["skipped_already_had"], r["tokens_inserted"], r["cache_tokens_without_traits"]) == (1, 2, 1, 1), str(r))
    check("import: set arithmetic -- 1 cache id not in the table (4), 1 table id not in the cache (9)",
          r["in_cache_not_in_table"] == 1 and r["in_table_not_in_cache"] == 1, str(r))
    check("import: diff per trait_type -- token 2 Cloak agrees, token 3 Relic agrees, token 3 Cloak disagrees",
          r["diffed_agree"] == 2 and r["diffed_disagree"] == 1
          and r["disagreements"] == [{"token_id": "3", "trait_type": "Cloak", "stored": ["Death"], "cache": ["Clergy"]}], str(r))
    check("import: the disagreeing token was NOT overwritten (the store keeps its own observation)",
          conn.execute("SELECT value FROM traits WHERE token_id='3' AND trait_type='Cloak'").fetchone()[0] == "Death"
          and conn.execute("SELECT traits_source FROM tokens WHERE token_id='3'").fetchone()[0] == "opensea_nft")
    row = conn.execute("SELECT traits_at, traits_source, contract, image_url, metadata_url FROM tokens WHERE token_id='1'").fetchone()
    check("import: traits_at is the cache's generated time, not now; source explorer_cache; known contract filled in",
          row == (gen, "explorer_cache", "0x387c41b0b2f1128de44db1bcf8baad085f26392c", "https://x/1.svg", None), str(row))
    check("import: traits written verbatim from `traits` only -- rank/price never enter the store",
          sorted(conn.execute("SELECT trait_type, value FROM traits WHERE token_id='1'")) == [("Bones", "Bone"), ("Cloak", "Death")]
          and "rank" not in {c[1] for c in conn.execute("PRAGMA table_info(tokens)")})
    check("import: a cache token with no traits is inserted but keeps traits_at NULL",
          conn.execute("SELECT traits_at FROM tokens WHERE token_id='4'").fetchone() == (None,))
    before = sorted(conn.execute("SELECT * FROM tokens")) + sorted(conn.execute("SELECT * FROM traits"))
    r2 = import_explorer_cache(conn, cache, slug="argonauts", generated_at=gen)
    after = sorted(conn.execute("SELECT * FROM tokens")) + sorted(conn.execute("SELECT * FROM traits"))
    check("import: idempotent -- a second run inserts and imports nothing and the tables are byte-identical",
          before == after and r2["imported"] == 0 and r2["tokens_inserted"] == 0 and r2["diffed_disagree"] == 1, str(r2))
    conn.close()

    # -- the CLI: reads summary.json for the timestamp, exits 1 on a disagreement, 0 when clean
    root = tmp / "imp-root"
    (root / "config").mkdir(parents=True)
    (root / "config" / "base.yaml").write_text("environment: local\nanalytical:\n  path: data/an.sqlite\n"
                                               "opstore:\n  path: data/ops.db\nlanding:\n  root: data/landing\n")
    (root / "config" / "watchlist.yaml").write_text("collections:\n  - slug: argonauts\n")
    d = tmp / "explorer"
    d.mkdir()
    (d / "tokens.json").write_text(json.dumps(cache))
    (d / "summary.json").write_text(json.dumps({"generated": 1788808192}))
    rc = cli_main(["--root", str(root), "import-traits", str(d / "tokens.json"), "--collection", "argonauts"])
    conn = open_store(root / "data" / "an.sqlite")
    check("import CLI: clean import exits 0 and stamps traits_at from summary.json `generated`",
          rc == 0 and conn.execute("SELECT traits_at FROM tokens WHERE token_id='1'").fetchone()[0]
          == datetime.fromtimestamp(1788808192, tz=timezone.utc).isoformat().replace("+00:00", "Z"))
    conn.execute("UPDATE traits SET value='Death' WHERE token_id='2' AND trait_type='Cloak'")
    conn.commit()
    conn.close()
    rc = cli_main(["--root", str(root), "import-traits", str(d / "tokens.json"), "--collection", "argonauts"])
    check("import CLI: a disagreement exits non-zero so the Operator sees it", rc == 1)
    (d / "summary.json").unlink()
    rc = cli_main(["--root", str(root), "import-traits", str(d / "tokens.json")])
    check("import CLI: with no timestamp source it refuses rather than stamping `now`", rc == 2)


# ---------------------------------------------------------------------------
# BUG-20260909-055 -- the silent-overwrite class in the trait-cache import.
# One test per blocking finding, each of which FAILS on the code as it stood.
# ---------------------------------------------------------------------------
def _cache_root(tmp: Path, name: str) -> Path:
    """A minimal --root a `navanax import-traits` run can be pointed at."""
    root = tmp / name
    (root / "config").mkdir(parents=True)
    (root / "config" / "base.yaml").write_text(
        "environment: local\nanalytical:\n  path: data/an.sqlite\n"
        "opstore:\n  path: data/ops.db\nlanding:\n  root: data/landing\n")
    (root / "config" / "watchlist.yaml").write_text("collections:\n  - slug: argonauts\n")
    return root


def test_import_duplicate_token_ids_are_never_a_silent_overwrite(tmp: Path) -> None:
    """B1. Two cache entries resolving to ONE token id used to be written twice --
    the second silently overwriting the first, and `imported` counting both. A
    repeated id whose traits DISAGREE is evidence about the cache, so it is a
    disagreement and nothing is written for that token."""
    from navanax.traits import import_explorer_cache, open_store

    gen = "2026-09-08T18:29:52Z"
    cache = {
        "7":     {"id": "7", "name": "Argonaut #7", "traits": {"Cloak": "Death"}},
        "seven": {"id": "7", "name": "Argonaut #7", "traits": {"Cloak": "Clergy"}},
        "8":     {"id": "8", "name": "Argonaut #8", "traits": {"Bones": "Bone"}},
        "8-again": {"id": "8", "name": "Argonaut #8", "traits": {"Bones": "Bone"}},
    }
    conn = open_store(tmp / "dup.sqlite")
    r = import_explorer_cache(conn, cache, slug="argonauts", generated_at=gen)
    check("dup ids: four entries resolving to two tokens import ONE token, not four "
          "(the repeated-but-identical id is collapsed, the contradictory one is refused)",
          r["imported"] == 1 and r["duplicate_ids_same"] == 1 and r["duplicate_ids_conflicting"] == 1,
          str({k: v for k, v in r.items() if k != "disagreements"}))
    check("dup ids: the contradictory id is reported like an import disagreement, naming both cache keys",
          len(r["duplicate_id_disagreements"]) == 1
          and r["duplicate_id_disagreements"][0]["token_id"] == "7"
          and sorted(r["duplicate_id_disagreements"][0]["keys"]) == ["7", "seven"],
          str(r["duplicate_id_disagreements"]))
    check("dup ids: NOTHING is written for the contradictory token -- no traits, no traits_at",
          conn.execute("SELECT COUNT(*) FROM traits WHERE token_id='7'").fetchone()[0] == 0
          and conn.execute("SELECT traits_at FROM tokens WHERE token_id='7'").fetchone() in (None, (None,)))
    check("dup ids: the token whose duplicate agreed is written exactly once, from the first entry",
          sorted(conn.execute("SELECT trait_type, value FROM traits WHERE token_id='8'")) == [("Bones", "Bone")])
    conn.close()


def test_list_pass_counts_only_the_traits_it_actually_wrote(tmp: Path) -> None:
    """B2. `traits_from_list` counted every list entry that CARRIED traits, including
    ones `_store(only_if_missing=True)` refused to write -- so the number reported
    to the Operator was the cache's size, not the store's gain. Worse, an OpenSea
    value that DISAGREED with what was already stored was dropped with no count
    at all."""
    import asyncio

    from navanax.opstore import OperationalStore
    from navanax.traits import TraitsJob, open_store

    class Rest:
        def __init__(self) -> None:
            self.requests_made = 0
        async def get(self, path, params=None, *, priority=None, retries=3):
            self.requests_made += 1
            if path.endswith("/nfts") and "collection/" in path:
                return 200, {"nfts": [
                    # 1: already stored from metadata_url, and OpenSea DISAGREES
                    {"identifier": "1", "contract": "0xc", "metadata_url": None,
                     "traits": [{"trait_type": "Cloak", "value": "Clergy"}]},
                    # 2: already stored, and OpenSea AGREES
                    {"identifier": "2", "contract": "0xc", "metadata_url": None,
                     "traits": [{"trait_type": "Cloak", "value": "Death"}]},
                    # 3: nothing stored -- this is the only real write
                    {"identifier": "3", "contract": "0xc", "metadata_url": None,
                     "traits": [{"trait_type": "Cloak", "value": "Bone"}]}], "next": None}
            return 404, {}

    conn = open_store(tmp / "lpcount.sqlite")
    conn.execute("INSERT INTO tokens (collection, token_id, listed_at, traits_at, traits_source) "
                 "VALUES ('argonauts','1','2026-09-09T00:00:00Z','2026-09-09T01:00:00Z','metadata_url')")
    conn.execute("INSERT INTO tokens (collection, token_id, listed_at, traits_at, traits_source) "
                 "VALUES ('argonauts','2','2026-09-09T00:00:00Z','2026-09-09T01:00:00Z','metadata_url')")
    conn.executemany("INSERT INTO traits VALUES ('argonauts',?,?,?)",
                     [("1", "Cloak", "Death"), ("2", "Cloak", "Death")])
    conn.commit()
    job = TraitsJob(conn, Rest(), OperationalStore(tmp / "lpcount-ops.db"), slug="argonauts")
    r1 = asyncio.run(job.list_tokens())
    check("list count: `traits_from_list` counts WRITES only -- 1 of 3 entries, not 3",
          r1["traits_from_list"] == 1, str(r1))
    check("list count: an entry skipped because the store already agreed is counted separately",
          r1["traits_skipped_same"] == 1, str(r1))
    check("list count: an entry skipped whose value DISAGREES is counted and reported, never dropped silently",
          r1["traits_skipped_conflict"] == 1
          and r1["traits_disagreements"] == [{"token_id": "1", "trait_type": "Cloak",
                                              "stored": ["Death"], "list": ["Clergy"]}], str(r1))
    check("list count: the disagreeing token keeps its own observation (nothing overwritten)",
          conn.execute("SELECT value FROM traits WHERE token_id='1'").fetchone()[0] == "Death"
          and conn.execute("SELECT traits_source FROM tokens WHERE token_id='1'").fetchone()[0] == "metadata_url")
    check("list count: `_store` reports whether it wrote, so a caller can count writes",
          job._store("3", [("Cloak", "Bone")], "opensea_nft_list", None, only_if_missing=True) is False
          and job._store("3", [("Cloak", "Bone")], "opensea_nft_list", None) is True)
    conn.close()


def test_import_reconciliation_uses_resolved_token_ids(tmp: Path) -> None:
    """B3. `in_cache_not_in_table` / `in_table_not_in_cache` were built from the
    cache's KEYS while every row was written under `entry["id"]`. A cache keyed by
    anything but the bare token id therefore reported a total mismatch -- every
    token 'missing' on both sides -- while the import itself was fine."""
    from navanax.traits import import_explorer_cache, open_store

    gen = "2026-09-08T18:29:52Z"
    cache = {
        "argonaut-4": {"id": "4", "name": "Argonaut #4", "traits": {"Cloak": "Death"}},
        "argonaut-5": {"id": "5", "name": "Argonaut #5", "traits": {"Cloak": "Clergy"}},
    }
    conn = open_store(tmp / "recon.sqlite")
    for tid in ("4", "5", "6"):
        conn.execute("INSERT INTO tokens (collection, token_id, listed_at) VALUES ('argonauts',?,'x')", (tid,))
    conn.commit()
    r = import_explorer_cache(conn, cache, slug="argonauts", generated_at=gen)
    check("reconciliation: the sets are built from RESOLVED ids -- 0 cache ids missing from the table, "
          "1 table id (6) missing from the cache",
          r["in_cache_not_in_table"] == 0 and r["in_table_not_in_cache"] == 1, str(r))
    check("reconciliation: the rows really were written under the resolved id, not the key",
          conn.execute("SELECT COUNT(*) FROM traits WHERE token_id IN ('4','5')").fetchone()[0] == 2
          and conn.execute("SELECT COUNT(*) FROM tokens WHERE token_id LIKE 'argonaut-%'").fetchone()[0] == 0)
    conn.close()


def test_import_cli_refuses_an_impossible_generated_timestamp(tmp: Path) -> None:
    """B4. `--generated` (and summary.json's `generated`) took any float. A future
    epoch was written straight into `traits_at` -- a bitemporal lie the store has
    no way to detect later -- and a millisecond epoch, which is what every JS
    tool emits, crashed with a raw `ValueError` out of `fromtimestamp`."""
    from navanax.cli import main as cli_main
    from navanax.traits import open_store

    root = _cache_root(tmp, "ts-root")
    d = tmp / "ts-explorer"
    d.mkdir()
    cache = {"1": {"id": "1", "name": "Argonaut #1", "traits": {"Cloak": "Death"}}}
    (d / "tokens.json").write_text(json.dumps(cache))
    args = ["--root", str(root), "import-traits", str(d / "tokens.json"), "--collection", "argonauts"]

    def run(*extra) -> object:
        try:
            return cli_main(args + list(extra))
        except SystemExit as exc:                     # argparse's own refusal is still a refusal
            return int(exc.code or 0)
        except Exception as exc:                      # noqa: BLE001 - a raw crash IS the finding
            return f"crashed: {type(exc).__name__}: {exc}"

    ms = 1788808192000                                 # milliseconds, the JS default
    check("generated: a millisecond epoch is refused with a message, not a raw ValueError",
          run("--generated", str(ms)) == 2, str(run("--generated", str(ms))))
    future = datetime.now(timezone.utc).timestamp() + 86400
    check("generated: a time in the FUTURE is refused -- traits_at must never claim to be tomorrow",
          run("--generated", str(future)) == 2, str(run("--generated", str(future))))
    check("generated: an epoch before 2020-01-01 is refused",
          run("--generated", "0") == 2, str(run("--generated", "0")))
    check("generated: a non-numeric value is refused with a message",
          run("--generated", "yesterday") == 2, str(run("--generated", "yesterday")))
    (d / "summary.json").write_text(json.dumps({"generated": ms}))
    check("generated: the same bounds apply to summary.json's `generated`, not just --generated",
          run() == 2, str(run()))
    db = root / "data" / "an.sqlite"
    check("generated: after every refusal the store holds NO token -- nothing was written",
          not db.exists() or open_store(db).execute(
              "SELECT COUNT(*) FROM tokens").fetchone()[0] == 0)
    (d / "summary.json").write_text(json.dumps({"generated": 1788808192}))
    check("generated: a plausible epoch still imports cleanly", run() == 0)


def test_config_assumptions_registry_exists(tmp: Path) -> None:
    """B5. `config/assumptions.yaml` is the assumptions registry docs/06 §4.4
    requires -- the one file that lists every judgement the code embodies. It was
    deleted by an unrelated change. Nothing referenced it, so nothing noticed."""
    import yaml
    p = ROOT / "config" / "assumptions.yaml"
    check("assumptions: config/assumptions.yaml exists", p.exists(), str(p))
    data = yaml.safe_load(p.read_text()) if p.exists() else {}
    ids = {a.get("id") for a in (data.get("assumptions") or [])}
    check("assumptions: it parses and still carries ASM-020 (the standing-order definition)",
          "ASM-020" in ids, str(sorted(ids)))
    check("assumptions: ...and ASM-021, the standing-book reporting rules PR-3 decided",
          "ASM-021" in ids, str(sorted(ids)))
    for aid in ("ASM-020", "ASM-021"):
        asm = next((a for a in (data.get("assumptions") or []) if a.get("id") == aid), {})
        check(f"assumptions: {aid} names its layer, owner, rationale and the code it governs",
              all(asm.get(k) for k in ("layer", "owner", "rationale", "code", "value")), str(sorted(asm)))


def test_import_malformed_entries_are_counted_never_stored(tmp: Path) -> None:
    """A cache entry that is not an object, and a trait whose value is a list or an
    object, are structure we cannot record. Storing `str(value)` would write the
    Python repr `{'a': 1}` into `traits.value` as though it were a trait. They are
    skipped and counted."""
    from navanax.traits import import_explorer_cache, open_store

    gen = "2026-09-08T18:29:52Z"
    cache = {
        "1": {"id": "1", "traits": {"Cloak": "Death"}},
        "2": ["not", "an", "object"],
        "3": "neither is this",
        "4": {"id": "4", "traits": {"Cloak": {"nested": "object"}, "Bones": ["a", "list"], "Relic": "Gold"}},
        "5": {"id": "5", "traits": "a string, not a mapping"},
    }
    conn = open_store(tmp / "malformed.sqlite")
    try:
        r = import_explorer_cache(conn, cache, slug="argonauts", generated_at=gen)
    except Exception as exc:  # noqa: BLE001 - a crash on junk input IS the finding
        r = {"crashed": f"{type(exc).__name__}: {exc}"}
    check("malformed: junk entries and non-scalar trait values are counted, not crashed on",
          r.get("malformed") == 5, str(r))
    check("malformed: a non-scalar trait value NEVER reaches the store as a Python repr",
          [v for (v,) in conn.execute("SELECT value FROM traits")
           if v.startswith(("{", "[")) or "'" in v] == [])
    check("malformed: the scalar traits alongside a malformed one are still imported",
          sorted(conn.execute("SELECT trait_type, value FROM traits WHERE token_id='4'")) == [("Relic", "Gold")])
    conn.close()


def test_import_records_its_provenance(tmp: Path) -> None:
    """Which file, of which bytes, with which digest, generated when -- recorded on
    the row (`explorer_cache:<sha256[:12]>`) and in a `trait_imports` table. Without
    it "this token's traits came from the Explorer cache" names no particular
    cache, and two caches that disagree are indistinguishable after the fact."""
    import hashlib

    from navanax.traits import import_explorer_cache, open_store

    gen = "2026-09-08T18:29:52Z"
    cache = {"1": {"id": "1", "traits": {"Cloak": "Death"}}}
    blob = json.dumps(cache).encode()
    sha = hashlib.sha256(blob).hexdigest()
    conn = open_store(tmp / "prov.sqlite")
    r = import_explorer_cache(conn, cache, slug="argonauts", generated_at=gen,
                              cache_file="/x/tokens.json", cache_bytes=len(blob), cache_sha256=sha)
    check("provenance: traits_source names the exact cache file by digest",
          conn.execute("SELECT traits_source FROM tokens WHERE token_id='1'").fetchone()[0]
          == f"explorer_cache:{sha[:12]}", str(r.get("traits_source")))
    row = conn.execute("SELECT cache_file, cache_bytes, cache_sha256, generated_at FROM trait_imports").fetchone()
    check("provenance: a trait_imports row records file, bytes, digest and the cache's generated time",
          row == ("/x/tokens.json", len(blob), sha, gen), str(row))
    counts = json.loads(conn.execute("SELECT counts_json FROM trait_imports").fetchone()[0])
    check("provenance: the row carries the run's counts, so a later reader can audit it without the cache",
          counts.get("imported") == 1, str(counts))
    check("provenance: with no digest given the source stays the plain `explorer_cache` (no fake precision)",
          import_explorer_cache(conn, {"2": {"id": "2", "traits": {"Cloak": "Bone"}}},
                                slug="argonauts", generated_at=gen)["traits_source"] == "explorer_cache")
    conn.close()


def test_unknown_contract_does_not_burn_a_fallback_slot(tmp: Path) -> None:
    """`fallback_left -= 1` ran BEFORE the `if contract:` check, so a token whose
    contract we do not know -- exactly what an imported cache leaves behind for a
    slug with no entry in KNOWN_CONTRACTS -- spent a budgeted OpenSea slot on a
    read that was never made, and a token that could have used it went without."""
    import asyncio

    from navanax.opstore import OperationalStore
    from navanax.traits import TraitsJob, open_store

    class Rest:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.requests_made = 0
        async def get(self, path, params=None, *, priority=None, retries=3):
            self.calls.append(path)
            self.requests_made += 1
            return 200, {"nft": {"traits": [{"trait_type": "Cloak", "value": "Death"}]}}

    conn = open_store(tmp / "slot.sqlite")
    conn.execute("INSERT INTO tokens (collection, token_id, contract, listed_at) VALUES ('argonauts','1',NULL,'x')")
    conn.execute("INSERT INTO tokens (collection, token_id, contract, listed_at) VALUES ('argonauts','2','0xc','x')")
    conn.commit()
    rest = Rest()
    job = TraitsJob(conn, rest, OperationalStore(tmp / "slot-ops.db"), slug="argonauts",
                    opensea_fallback_budget=1)
    res = asyncio.run(job.fetch_traits())
    check("fallback slot: the contract-less token consumes no slot, so the one budgeted read goes to the token that can use it",
          rest.calls == ["/chain/ethereum/contract/0xc/nfts/2"], str(rest.calls))
    check("fallback slot: that token really got its traits; the contract-less one stays unresolved for the next run",
          res["ok"] == 1 and conn.execute("SELECT traits_at FROM tokens WHERE token_id='1'").fetchone()[0] is None)
    conn.close()


def test_import_reports_a_contract_mismatch(tmp: Path) -> None:
    """`COALESCE(contract, ?)` fills a blank and keeps what is there -- which is
    right -- but it also HIDES the case where the list pass stored one contract and
    KNOWN_CONTRACTS says another. One of the two is wrong, and a wrong contract
    sends every fallback read to the wrong collection."""
    from navanax.traits import KNOWN_CONTRACTS, import_explorer_cache, open_store

    gen = "2026-09-08T18:29:52Z"
    conn = open_store(tmp / "contract.sqlite")
    conn.execute("INSERT INTO tokens (collection, token_id, contract, listed_at) "
                 "VALUES ('argonauts','1','0xdeadbeef','x')")
    conn.execute("INSERT INTO tokens (collection, token_id, contract, listed_at) VALUES "
                 "('argonauts','2',?,'x')", (KNOWN_CONTRACTS["argonauts"],))
    conn.commit()
    r = import_explorer_cache(conn, {"1": {"id": "1", "traits": {"Cloak": "Death"}},
                                     "2": {"id": "2", "traits": {"Cloak": "Bone"}}},
                              slug="argonauts", generated_at=gen)
    check("contract: a stored contract that differs from the one being imported is reported, not swallowed by COALESCE",
          r["contract_mismatches"] == 1
          and r["contract_mismatch_examples"][0]["token_id"] == "1"
          and r["contract_mismatch_examples"][0]["stored"] == "0xdeadbeef", str(r.get("contract_mismatch_examples")))
    check("contract: the stored value is still not overwritten -- reporting is not deciding",
          conn.execute("SELECT contract FROM tokens WHERE token_id='1'").fetchone()[0] == "0xdeadbeef")
    conn.close()


def test_post_import_coverage_and_case_near_misses(tmp: Path) -> None:
    """After an import: how many tokens now have traits, which order criteria match
    no trait value at all, and which trait types/values differ ONLY by case. Values
    are stored verbatim precisely so a casing mismatch stays visible; the check is
    what makes it visible instead of a silently empty filter."""
    from navanax.traits import case_near_misses, criteria_trait_coverage, open_store, trait_coverage

    conn = open_store(tmp / "cov.sqlite")
    for tid in ("1", "2", "3"):
        conn.execute("INSERT INTO tokens (collection, token_id, listed_at, traits_at) VALUES "
                     "('argonauts',?,'x',?)", (tid, "2026-09-09T00:00:00Z" if tid != "3" else None))
    conn.executemany("INSERT INTO traits VALUES ('argonauts',?,?,?)",
                     [("1", "Cloak", "Death"), ("2", "cloak", "death")])
    conn.commit()
    cov = trait_coverage(conn, "argonauts")
    check("coverage: 'N of M tokens now have traits' is exact and counts tokens, not trait rows",
          (cov["with_traits"], cov["tokens"]) == (2, 3), str(cov))
    nm = case_near_misses(conn, "argonauts")
    check("near miss: 'Cloak' and 'cloak' are reported as one case-folded group",
          nm["trait_type_near_misses"] == [{"casefolded": "cloak", "variants": ["Cloak", "cloak"]}], str(nm))
    check("near miss: 'Death' and 'death' are reported too, with the type they sit under",
          nm["value_near_misses"] == [{"trait_type": "cloak", "casefolded": "death",
                                       "variants": ["Death", "death"]}] and nm["alert"] is True, str(nm))
    check("coverage: with no order_criteria table the criteria check says so rather than reporting 0 missing",
          criteria_trait_coverage(conn, "argonauts")["available"] is False)
    conn.execute("CREATE TABLE events (run TEXT, seq INTEGER, collection TEXT)")
    conn.execute("CREATE TABLE order_criteria (run TEXT, seq INTEGER, trait_type TEXT, value TEXT, kind TEXT)")
    conn.execute("INSERT INTO events VALUES ('r',1,'argonauts')")
    conn.executemany("INSERT INTO order_criteria VALUES ('r',1,?,?,'string')",
                     [("Cloak", "Death"), ("Cloak", "Clergy")])
    conn.commit()
    cc = criteria_trait_coverage(conn, "argonauts")
    check("coverage: a criterion with no matching trait value is counted and named",
          cc["distinct_criteria"] == 2 and cc["missing"] == 1
          and cc["missing_pairs"] == [{"trait_type": "Cloak", "value": "Clergy"}], str(cc))
    conn.close()


def test_import_traits_command_finds_its_cache_without_asking(tmp: Path) -> None:
    """`import-traits.command` is a double-click: it asks for nothing, finds the
    cache itself, and when it cannot, prints every path it looked at rather than a
    bare failure."""
    text = (ROOT / "import-traits.command").read_text() if (ROOT / "import-traits.command").exists() else ""
    check("import-traits.command: exists and is executable",
          (ROOT / "import-traits.command").exists() and os.access(ROOT / "import-traits.command", os.X_OK))
    check("import-traits.command: never prompts for a path -- no `read -p` before the run",
          "read -r -p \"Enter the path" not in text and "$1" not in text.split("# ---")[0], text[:0])
    check("import-traits.command: looks in a cache/ folder and in whatever config records",
          "cache/" in text and "explorer_cache_path" in text)
    check("import-traits.command: prints where it looked when the cache is absent",
          "looked in" in text.lower() or "looked for" in text.lower())
    check("traits.command: says pass 2 finds nothing for Argonauts (metadata_url is NULL) "
          "and that the list pass now carries traits",
          "metadata_url is NULL" in (ROOT / "traits.command").read_text()
          and "opensea_nft_list" in (ROOT / "traits.command").read_text())


def test_trait_filtered_metrics(tmp: Path) -> None:
    """Under a trait filter: item bids on matching tokens count, collection offers
    count (they bid on every token), trait offers do NOT (criteria unknown), and
    a derived spread says which leg is filtered (tech-lead F2, F3)."""
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import COLS, Normalizer, parse_event
    from navanax.traits import ensure_schema

    iv = load_intervals(ROOT / "config" / "intervals.yaml")
    n = Normalizer(tmp / "empty-lz", tmp / "tf.sqlite")
    ensure_schema(n.conn)
    n.conn.execute("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES ('argonauts','1','a','x')")
    n.conn.execute("INSERT INTO traits VALUES ('argonauts','1','Background','Blue')")
    def put(raw, recv, seq, **over):
        rr = parse_event(_env(seq, raw, recv))
        rr["file"] = "f"
        rr.update(over)
        n.conn.execute(f"INSERT INTO events ({','.join(COLS)}) VALUES ({','.join('?'*len(COLS))})", tuple(rr.get(c) for c in COLS))
    put(REAL_BID, "2026-09-09T10:20:16Z", 1, token_id="1", price_eth=0.3)
    put(REAL_COLL_OFFER, "2026-09-09T10:20:17Z", 2, price_eth=0.348)
    put(REAL_COLL_OFFER, "2026-09-09T10:20:18Z", 3, event_type="trait_offer", token_id=None, price_eth=0.9)
    put(DOC_LISTING, "2026-09-09T10:30:01Z", 4, token_id="1", price_eth=1.59)
    n.conn.commit()
    eng = MetricEngine(n.conn, iv, "America/Chicago")
    now = datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc)
    last = lambda s: [v for v in s["raw"] if v is not None][-1]  # noqa: E731
    check("trait metrics: unfiltered bid_count sees all three bids",
          last(eng.series(metric="bid_count", collection="argonauts", interval="1h", range_="6h", now=now)) == 3.0)
    f = {"Background": ["Blue"]}
    check("trait metrics: filtered to a matching trait -> item bid + collection offer, trait offer excluded = 2",
          last(eng.series(metric="bid_count", collection="argonauts", interval="1h", range_="6h", now=now, traits=f)) == 2.0)
    # BUG-20260910-060. This check used to read "filtered to a trait no token has
    # -> only the collection offer = 1 (F2)" and it PASSED, because it was the
    # defect written down as intended behaviour: a collection offer bids on every
    # token, but a bid on every token is not a bid on any token of an EMPTY set.
    # The check now asserts the rule instead of the implementation, and the
    # dedicated property test is
    # test_filtered_counts_are_null_when_the_filter_selects_no_token.
    none = {"Background": ["Red"]}
    s_none = eng.series(metric="bid_count", collection="argonauts", interval="1h",
                        range_="6h", now=now, traits=none)
    check("trait metrics: filtered to a trait NO token has -> undefined on every bucket, not "
          "the collection-offer count and not 0 (BUG-20260910-060; this check used to assert 1.0)",
          all(v is None for v in s_none["raw"]) and s_none["basis"]["empty_token_set"] is True,
          str(s_none["raw"]))
    s = eng.series(metric="immediacy_cost", collection="argonauts", interval="1h", range_="6h", now=now, traits=f)
    check("trait metrics: the derived spread under a filter declares its legs (F3)",
          "legs" in s["basis"] and "trait-filtered" in s["basis"]["legs"]["floor_ask"]
          and "collection-wide" in s["basis"]["legs"]["collection_bid"], str(s["basis"].get("legs")))
    check("trait metrics: no legs line when there is no filter",
          "legs" not in eng.series(metric="immediacy_cost", collection="argonauts", interval="1h", range_="6h", now=now)["basis"])
    n.close()


def test_rest_client_accounting(tmp: Path) -> None:
    """rest.py: a retried call is two governor acquisitions, two ledger rows and
    two on the attempt counter -- the number the budget saw, not the number the
    caller asked for (tech-lead F5)."""
    import asyncio

    from navanax.governor import RestGovernor, TokenBucket
    from navanax.rest import RestClient

    clock = [1000.0]
    async def fake_sleep(sec):          # advance a fake clock instead of waiting: backoff is real, the test is instant
        clock[0] += max(sec, 0.01)
    gov = RestGovernor(TokenBucket(capacity=120, per_seconds=3600, clock=lambda: clock[0]), sleep=fake_sleep)
    acquired = {"n": 0}
    real_acquire = gov.acquire
    async def counting_acquire(*a, **k):
        acquired["n"] += 1
        return await real_acquire(*a, **k)
    gov.acquire = counting_acquire
    ledger: list[tuple] = []
    rc = RestClient("k", gov, ledger=lambda *a, **k: ledger.append((a, k)), run_id="t")
    responses = [(429, {"retry-after": "0"}, {"e": "slow down"}), (200, {"x-ratelimit-remaining": "7"}, {"ok": 1})]
    rc._do = lambda path, params: responses.pop(0)
    status, body = asyncio.run(rc.get("/x", {"a": 1}))
    check("rest: 429 then 200 returns the 200", status == 200 and body == {"ok": 1})
    check("rest: two attempts = two governor acquisitions", acquired["n"] == 2)
    check("rest: two attempts = two ledger rows, the last with remaining=7",
          len(ledger) == 2 and ledger[-1][1]["remaining"] == 7, str(ledger))
    check("rest: requests_made counts attempts, not calls", rc.requests_made == 2)
    check("rest: the governor saw the 429", gov.stats["denied_429"] == 1)


def test_series_gap_masking(tmp: Path) -> None:
    """REQ-F-15: a bucket inside an ingestion gap is undefined for EVERY metric,
    counts included -- zero sales while we were not listening is not a fact."""
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import COLS, Normalizer, parse_event

    iv = load_intervals(ROOT / "config" / "intervals.yaml")
    n = Normalizer(tmp / "empty-lz", tmp / "gap.sqlite")
    row = parse_event(_env(1, DOC_SALE, "2026-09-09T10:40:01Z"))
    row["file"] = "f"
    n.conn.execute(f"INSERT INTO events ({','.join(COLS)}) VALUES ({','.join('?'*len(COLS))})", tuple(row.get(c) for c in COLS))
    n.conn.commit()
    eng = MetricEngine(n.conn, iv, "America/Chicago")
    now = datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc)
    s = eng.series(metric="sales_count", collection="argonauts", interval="1h", range_="6h", now=now)
    check("metrics: a COUNT bucket with no event while listening is 0, not a hole",
          s["raw"][:5] == [0.0] * 5 and s["raw"][5] == 1.0 and s["basis"]["undefined_buckets"] == 0, f"got {s['raw']}")
    gap_start = datetime(2026, 9, 9, 7, 30, tzinfo=timezone.utc).timestamp()
    gap_end = datetime(2026, 9, 9, 8, 10, tzinfo=timezone.utc).timestamp()
    s = eng.series(metric="sales_count", collection="argonauts", interval="1h", range_="6h", now=now, gaps=[(gap_start, gap_end)])
    check("metrics: the two hourly buckets a 07:30-08:10 gap touches are undefined; the rest untouched",
          s["raw"] == [0.0, 0.0, None, None, 0.0, 1.0] and s["basis"]["gap_masked_buckets"] == 2, f"got {s['raw']}")
    s = eng.series(metric="sales_count", collection="argonauts", interval="1h", range_="6h", now=now, gaps=[(gap_start, None)])
    check("metrics: an OPEN gap masks every bucket from its start onward, the sale included",
          s["raw"] == [0.0, 0.0, None, None, None, None], f"got {s['raw']}")
    n.close()


def test_screener_sort_and_filter(tmp: Path) -> None:
    """REQ-F-07: single and multi-trait filters (AND across types, OR within a type),
    every column sortable, nulls last, live prices from the store."""
    from navanax.metrics import MetricEngine, load_intervals, parse_trait_filter, token_filter_sql
    from navanax.normalize import COLS, Normalizer, iso_to_ts, parse_event, refresh_order_lives
    from navanax.traits import ensure_schema

    f = parse_trait_filter("Background:Blue|Red;Eyes:Laser")
    check("screener: filter values are trimmed like types are (F16)", parse_trait_filter("Background: Blue | Red") == {"Background": ["Blue", "Red"]})
    check("screener: filter grammar parses type:val|val;type:val", f == {"Background": ["Blue", "Red"], "Eyes": ["Laser"]})
    check("screener: blank spec means no filter", parse_trait_filter("") == {} and parse_trait_filter(None) == {})
    sql, args = token_filter_sql("argonauts", f, alias="e")
    check("screener: AND across trait types, OR (IN) within one, all parameterised",
          sql.count("token_id IN (SELECT") == 2 and "value IN (?,?)" in sql and args.count("argonauts") == 2 and "Blue" in args, sql)
    check("screener: no filter -> no SQL", token_filter_sql("argonauts", {}) == ("", []))

    n = Normalizer(tmp / "empty-lz", tmp / "sc.sqlite")
    ensure_schema(n.conn)
    toks = [("1", "Argo #1", "Blue", "Laser"), ("2", "Argo #2", "Red", "Plain"), ("3", "Argo #3", "Blue", "Plain")]
    for tid, name, bg, eyes in toks:
        n.conn.execute("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES (?,?,?,?)", ("argonauts", tid, name, "2026-09-09T00:00:00Z"))
        n.conn.executemany("INSERT INTO traits VALUES (?,?,?,?)",
                           [("argonauts", tid, "Background", bg), ("argonauts", tid, "Eyes", eyes)])
    # one live listing on token 1 at 0.5 ETH (from the real listing frame, token id patched)
    row = parse_event(_env(1, DOC_LISTING, "2026-09-09T10:30:01Z"))
    row["file"] = "f"
    row["token_id"] = "1"
    row["price_eth"] = 0.5
    row["price_usd"] = 1250.0
    n.conn.execute(f"INSERT INTO events ({','.join(COLS)}) VALUES ({','.join('?'*len(COLS))})", tuple(row.get(c) for c in COLS))
    n.conn.commit()
    refresh_order_lives(n.conn)          # sync() does this on every pass in production
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    now = datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc)

    r = eng.screener("argonauts", now=now)
    check("screener: unfiltered returns every token with its trait dict and the trait columns",
          r["total"] == 3 and r["trait_types"] == ["Background", "Eyes"] and r["rows"][0]["traits"] == {"Background": "Blue", "Eyes": "Laser"})
    r = eng.screener("argonauts", traits={"Background": ["Blue"]}, now=now)
    check("screener: single filter", [x["token_id"] for x in r["rows"]] == ["1", "3"])
    r = eng.screener("argonauts", traits={"Background": ["Blue"], "Eyes": ["Laser"]}, now=now)
    check("screener: multi-filter is AND across types", [x["token_id"] for x in r["rows"]] == ["1"])
    r = eng.screener("argonauts", traits={"Background": ["Blue", "Red"]}, now=now)
    check("screener: multi-value within a type is OR", r["total"] == 3)
    r = eng.screener("argonauts", sort="Eyes", direction="desc", now=now)
    check("screener: sort by a trait column", [x["traits"]["Eyes"] for x in r["rows"]] == ["Plain", "Plain", "Laser"])
    r = eng.screener("argonauts", sort="lowest_ask", direction="asc", now=now)
    check("screener: sort by price puts the priced token first and nulls last, ETH by default",
          r["rows"][0]["token_id"] == "1" and r["rows"][0]["lowest_ask"] == 0.5 and r["rows"][-1]["lowest_ask"] is None, str(r["rows"]))
    r = eng.screener("argonauts", sort="lowest_ask", denom="USD", now=now)
    check("screener: USD denomination uses price_usd", r["rows"][0]["lowest_ask"] == 1250.0)
    r = eng.screener("argonauts", page=1, page_size=2, now=now)
    check("screener: pagination", r["total"] == 3 and len(r["rows"]) == 1 and r["page"] == 1)
    r = eng.screener("argonauts", sort="DROP TABLE tokens", now=now)
    check("screener: unknown sort column falls back safely", r["sort"] == "token_id")

    # -- lifecycle: a cancelled ask, an expired ask, a sold token, a mixed-type trait column
    def put(raw, recv, seq, **over):
        rr = parse_event(_env(seq, raw, recv))
        rr["file"] = "f"
        rr["valid_at"], rr["valid_ts"] = recv, iso_to_ts(recv)     # the fixture frames carry their own event time; pin ours
        rr.update(over)
        n.conn.execute(f"INSERT INTO events ({','.join(COLS)}) VALUES ({','.join('?'*len(COLS))})", tuple(rr.get(c) for c in COLS))
    put(DOC_LISTING, "2026-09-09T10:31:00Z", 10, token_id="2", order_hash="0xcancelme", price_eth=0.7, price_usd=1750.0)
    put(REAL_CANCEL, "2026-09-09T10:32:00Z", 11, token_id="2", order_hash="0xcancelme")
    put(DOC_LISTING, "2026-09-09T10:33:00Z", 12, token_id="3", order_hash="0xexpired", price_eth=0.9, price_usd=2250.0,
        expiration_at="2026-09-09T10:50:00Z", expiration_ts=iso_to_ts("2026-09-09T10:50:00Z"))
    put(DOC_LISTING, "2026-09-09T10:34:00Z", 13, token_id="3", order_hash="0xsoldme", price_eth=0.8, price_usd=2000.0)
    put(DOC_SALE, "2026-09-09T10:35:00Z", 14, token_id="3", order_hash="0xsoldme", price_eth=0.8, price_usd=2000.0)
    n.conn.execute("INSERT INTO traits VALUES (?,?,?,?)", ("argonauts", "1", "Level", "5"))
    n.conn.execute("INSERT INTO traits VALUES (?,?,?,?)", ("argonauts", "2", "Level", "Unranked"))
    n.conn.commit()
    refresh_order_lives(n.conn)
    r = eng.screener("argonauts", sort="lowest_ask", now=now)
    asks = {x["token_id"]: x["lowest_ask"] for x in r["rows"]}
    check("screener: cancelled, expired and filled asks are NOT standing; only token 1's survives",
          asks == {"1": 0.5, "2": None, "3": None}, str(asks))
    check("screener: last sale is recorded for the sold token", {x["token_id"]: x["last_sale"] for x in r["rows"]}["3"] == 0.8)
    book = eng.live_book("argonauts", now=now)
    check("screener and live book agree on the same store: one standing ask, token 1 at 0.5",
          [(a["token_id"], a["price_eth"]) for a in book["asks"]] == [("1", 0.5)], str(book["asks"]))
    later = datetime(2026, 9, 9, 10, 40, tzinfo=timezone.utc)
    check("screener: expiry is compared numerically -- before 10:50 the expiring ask still stands",
          eng.screener("argonauts", now=later)["rows"][2]["lowest_ask"] == 0.9)
    r = eng.screener("argonauts", sort="Level", now=now)
    check("screener: a trait column mixing numbers and words sorts (numbers first, then words, then missing) instead of 500ing",
          [x["token_id"] for x in r["rows"]] == ["1", "2", "3"])
    n.conn.execute("UPDATE tokens SET name=NULL WHERE token_id='1'")
    n.conn.commit()
    r = eng.screener("argonauts", sort="name", now=now)
    check("screener: a missing name sorts LAST ascending", [x["token_id"] for x in r["rows"]][-1] == "1")
    r = eng.screener("argonauts", sort="name", direction="desc", now=now)
    check("screener: ...and last descending too", [x["token_id"] for x in r["rows"]][-1] == "1")
    n.close()


def test_ui_contract() -> None:
    """The page is the only thing Spencer sees; these are the design rules he set
    (BUG-041 white control box, BUG-042 UTC on screen) pinned so they cannot regress."""
    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()
    check("ui: dark colour scheme declared so macOS cannot paint native controls white (BUG-041)",
          'color-scheme" content="dark"' in html and "color-scheme: dark" in html)
    check("ui: selects are custom-drawn (appearance:none) with an explicit dark option background",
          "appearance:none" in html and "select option{background:" in html)
    check("ui: every timestamp goes through the display timezone from /api/status (BUG-042)",
          "display_timezone" in html and "timeZone:S.tz" in html and "plotT(" in html)
    check("ui: hover cards have an explicit high-contrast background and font (BUG-043)",
          "hoverlabel:{bgcolor:C.hoverBg,bordercolor:C.hoverBd" in html and "namelength:-1" in html
          and "--hover-bg:" in html and "--hover-border:" in html)
    check("ui: USD is shown to the cent", "minimumFractionDigits:2,maximumFractionDigits:2" in html)
    check("ui: Austin FC Verde is the accent; the old blue accent is gone", "#00B140" in html and "#58a6ff" not in html.lower())
    check("ui: trait filters and the screener are wired to the trait endpoints",
          "/api/traits" in html and "/api/screener" in html and "traits:traitSpec()" in html)
    check("ui: no curve is drawn between observations (no spline interpolation)", "shape:'spline'" not in html)
    check("ui: undefined values stay holes", "connectgaps:false" in html and "connectgaps:true" not in html)
    # The PROPERTY, not a list of call sites (tech-lead re-review): every ${...}
    # interpolation that touches a store-derived, third-party-controlled field
    # must pass through esc()/safeImg() or a numeric/time formatter. token_id
    # comes from the Seaport order (chosen by whoever placed it), maker/taker are
    # wallets, names/values/urls come from metadata hosts, reasons from manifests.
    import re
    tainted = (".token_id", ".maker", ".taker", ".name", ".reason", ".value", ".traits[", ".image_url",
               ".error", "irrecoverable", ".event_type")
    safe = ("esc(", "safeImg(", "fmt(", "money(", "money2(", "clock(", "age(", "hhmm(",
            ".includes(")                          # a boolean test emits 'checked' or '', never the value
    leaks = []
    exprs: list[str] = []
    work = html
    inner = re.compile(r"\$\{([^{}]*)\}")          # innermost first: nested ${} would let an outer esc( vouch for an inner leak
    while True:
        found = inner.findall(work)
        if not found:
            break
        exprs.extend(found)
        work = inner.sub("@@", work)
    for expr in exprs:
        if any(f in expr for f in tainted) and not any(w in expr for w in safe):
            leaks.append(expr[:70])
    check("ui: EVERY interpolation of a third-party field is escaped or formatted (F6, property not call-site list)",
          not leaks and "const esc=" in html and "const safeImg=" in html, "; ".join(leaks))
    check("ui: esc() covers the five HTML metacharacters",
          all(c in html.split("const esc=")[1].split("\n")[0] for c in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;")))
    check("ui: the basis line prints the bucket alignment and, under a filter, which leg is filtered",
          "bucket_alignment" in html and "b.legs" in html)

    # -----------------------------------------------------------------------
    # PR-7 -- chart interaction and the palette split.
    #
    # The defect this pins (factcheck D-V3) is not "two tokens share a hex". It is
    # that ONE hex, #19C95A, was simultaneously the brand accent AND four unrelated
    # data roles, so a green mark on the page could be a collection offer, a sale,
    # an event-mix bar, a sparkline, or a piece of chrome. The assertions below are
    # the PROPERTY, not a list of the four call sites: any future literal, and any
    # future token reuse, fails here.
    # -----------------------------------------------------------------------
    root_body = html.split(":root{", 1)[1].split("}", 1)[0]
    outside_root = html.replace(root_body, "", 1)

    def _tokens(block: str) -> dict[str, str]:
        return {m.group(1): m.group(2).upper()
                for m in re.finditer(r"--([a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{3,8})\s*;", block)}

    def _slice(start: str, end: str) -> dict[str, str]:
        seg = root_body.split(start, 1)
        return _tokens(seg[1].split(end, 1)[0]) if len(seg) > 1 else {}

    chrome = _slice("/* @chrome", "/* @status")
    status = _slice("/* @status", "/* @data")
    data = _slice("/* @data", "/* @end-tokens")
    check("ui/palette: the :root block declares @chrome / @status / @data classes",
          bool(chrome) and bool(status) and bool(data),
          f"chrome={len(chrome)} status={len(status)} data={len(data)}")

    dupes = [f"{a}={b} both {h}" for h, names in
             {h: [n for n, v in data.items() if v == h] for h in set(data.values())}.items()
             if len(names) > 1 for a, b in [(names[0], names[1])]]
    check("ui/palette: no two DATA-role tokens share a hex (a mark's colour names its role)",
          not dupes, "; ".join(dupes))

    reserved = {**chrome, **status}
    collisions = [f"--{n} {h} == --{m}" for n, h in data.items()
                  for m, v in reserved.items() if v == h]
    check("ui/palette: no DATA-role token equals a chrome or status token (D-V3: verde is chrome only)",
          not collisions, "; ".join(collisions))
    check("ui/palette: --coll is off #19C95A and --sale is pure white, not --text (D-V3, D-W3)",
          data.get("coll") not in (None, chrome.get("verde-2"), chrome.get("verde"))
          and data.get("sale") == "#FFFFFF" and data.get("sale") != chrome.get("text"),
          f"--coll={data.get('coll')} --sale={data.get('sale')} --text={chrome.get('text')}")
    check("ui/palette: the roles DESIGN §5.4 names all exist as tokens",
          all(k in data for k in ("ask", "bid", "coll", "trait-offer", "sale", "spread", "cancel")),
          f"have {sorted(data)}")

    # No chart literal may bypass the tokens. Two halves: a data hex must not appear
    # anywhere outside :root, and the whole <script> block must carry no colour
    # literal at all -- every mark reads its hex back out of :root via getComputedStyle,
    # so the CSS and Plotly can never drift apart.
    leaked = sorted({h for h in data.values()
                     if re.search(re.escape(h), outside_root, re.IGNORECASE)})
    check("ui/palette: no data-role hex appears outside the :root block (the four #19C95A literals are gone)",
          not leaked, ", ".join(leaked))
    script = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    script_hex = sorted(set(re.findall(r"#[0-9A-Fa-f]{6}(?![0-9A-Za-z_-])", script)))
    check("ui/palette: the chart script contains NO colour literal -- every mark reads a token",
          not script_hex and "getComputedStyle(document.documentElement)" in script,
          ", ".join(script_hex))

    # Interaction. Note what is deliberately NOT asserted: nothing here claims to have
    # fixed wheel-zoom. scrollZoom was already false by default and was never enabled
    # (D-W1); it is written explicitly only so a future Plotly default cannot turn it on.
    #
    # Everything below reads a PARSED object, never the raw file. Tech-lead re-review,
    # 2026-09-10: the modebar/scrollZoom assertions used to be `"displayModeBar:false"
    # in html`, and the block comment above `const L=()` at index.html:201-202 contains
    # both of those exact strings while explaining the decision. So the test was
    # satisfied by the PROSE and would have stayed green with `displayModeBar:true` in
    # the code -- it asserted that the rule is documented, not that it is in force.
    # `_obj()` brace-matches the real object, so only the code can satisfy it.
    def _obj(src: str, opener: str) -> str:
        """Body of the `{...}` that follows `opener`, by brace matching."""
        i = src.index(opener) + len(opener)
        depth, j = 1, i
        while depth and j < len(src):
            depth += (src[j] == "{") - (src[j] == "}")
            j += 1
        return src[i:j - 1]

    def _flags(body: str) -> dict[str, str]:
        """Top-level `key:value` pairs of a flat JS object literal, values verbatim."""
        return {m.group(1): m.group(2).strip()
                for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*:\s*([^,{}]+)", body)}

    cfg = _flags(_obj(html, "const CFG={"))
    check("ui/charts: the modebar is gone -- asserted on the PARSED CFG, not the doc-comment",
          cfg.get("displayModeBar") == "false", f"CFG.displayModeBar={cfg.get('displayModeBar')!r}")
    check("ui/charts: scrollZoom is pinned false in CFG so a future Plotly default cannot enable it (D-W1)",
          cfg.get("scrollZoom") == "false", f"CFG.scrollZoom={cfg.get('scrollZoom')!r}")
    check("ui/charts: no modebar button list is configured -- the bar is gone, not curated",
          "modeBarButtonsToRemove" not in html)
    # The shared layout L() is the one place both axes are configured; read its two
    # axis literals rather than grepping the whole file, so a per-panel override
    # (the event-mix panel legitimately locks x -- it is a horizontal bar chart, not
    # a time axis, and nothing brushes it) cannot mask a regression in the default.
    layout = html.split("const L=()=>(", 1)[1].split("const CFG=", 1)[0]

    lx, ly = _obj(layout, "xaxis:{"), _obj(layout, "yaxis:{")
    check("ui/charts: the shared layout LOCKS y so a drag can never rescale price",
          "fixedrange:true" in ly, ly[:120])
    check("ui/charts: the shared layout leaves x FREE -- shift-drag time selection needs it (D-W2)",
          "fixedrange:false" in lx and "dragmode:'select'" in layout, lx[:120])
    check("ui/charts: our own range chips replace the toolbar, and a reset chip proves a selection",
          "const RANGES=['1h','6h','24h','7d','30d']" in html
          and "plotly_selected" in html and "data-reset" in html)
    check("ui/charts: the hover header carries the bucket time in the display timezone",
          "hoverformat:'%b %d, %H:%M:%S '+zone()" in html and "timeZoneName:'short'" in html)
    check("ui/charts: hover rows carry coverage and n where the API supplies them",
          "s.coverage[i]" in html and "%{customdata}" in html)

    # Numbers and the KPI rule the Operator set on 2026-09-10.
    check("ui/kpi: USD aggregates are to the cent -- compact()/abbreviated totals are gone",
          "compact=" not in html and "notation:'compact'" not in html
          and "money2(sum(vol),S.denom)" in html)
    check("ui/kpi: the delta is now vs the value 24 HOURS AGO, with both timestamps printed",
          "iNow=v.length-1" in html and "vThen=v[0]" in html
          and "24h ago ${stamp(t[0])}" in html and "% = now vs 24 h ago" in html)
    check("ui/kpi: a hole at either endpoint prints its reason instead of a percentage",
          "no observation 24h ago" in html and "no observation in the current hour" in html)
    check("ui/basis: the standing-book basis is rendered -- book, coverage, crossed-book alarm",
          "book: ${b.book}" in html and "coverage: median" in html and "CROSSED standing book" in html)
    check("ui/basis: a withheld percentile band is printed, never silently dropped",
          "p10–p90 withheld (n<${minN})" in html)
    check("ui/basis: a filter that selects NO token says so under EVERY chart, not only the "
          "trait panel — the Activity bars go null under it too (BUG-20260910-060)",
          "b.empty_token_set?" in html and "b.empty_token_set_note" in html)


def test_ui_range_chips_name_an_anchored_range() -> None:
    """Tech-lead re-review 2026-09-10, S4. The <select> offers nine ranges; the chip
    row offers five. The other four -- HTD, DTD, MTD, YTD -- are ANCHORED: "today"
    is 3 hours long at 03:00 and 23 at 23:00, so there is no fixed-width chip they
    could match, and `r===cur` lit none of them.

    Nothing lit is not neutral. Five chips, none highlighted, is what the row also
    looks like the instant before a range is chosen, so the panel silently stopped
    saying what window it was drawing exactly when the window was the least
    obvious one. The repair is to NAME the range in the row rather than to invent
    four more chips of invisible width: the row must always answer "what am I
    looking at", and for an anchored range only a name can.
    """
    import re
    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()

    def _obj(src: str, opener: str) -> str:
        i = src.index(opener) + len(opener)
        depth, j = 1, i
        while depth and j < len(src):
            depth += (src[j] == "{") - (src[j] == "}")
            j += 1
        return src[i:j - 1]

    # The premise, read off the page itself rather than assumed: the four anchored
    # ranges really are offered by the <select> and really are absent from RANGES.
    chip_ranges = re.search(r"const RANGES=\[([^\]]*)\]", html).group(1).replace("'", "").split(",")
    sel = html.split('<select id="range">', 1)[1].split("</select>", 1)[0]
    options = dict(re.findall(r'<option value="([^"]+)"[^>]*>([^<]+)</option>', sel))
    anchored = [r for r in options if r not in chip_ranges]
    check("ui/chips: the range <select> offers four anchored ranges that no chip can match",
          sorted(anchored) == ["DTD", "HTD", "MTD", "YTD"],
          f"select={sorted(options)} chips={chip_ranges}")

    body = _obj(html, "function chipsFor(id){")
    check("ui/chips: a chip lights ONLY on an exact match, so an anchored range lights nothing "
          "-- no chip is allowed to stand in for 'today'",
          "class=\"${r===cur?'on':''}\"" in body, body[:200])
    check("ui/chips: ...and when the range is anchored the row prints its NAME instead, "
          "so an unlit row is never the only thing the panel says",
          "isChipRange(cur)?''" in body and 'class="rname"' in body
          and "esc(rangeName())" in body, body[:400])
    check("ui/chips: the name comes from the <select>'s own option text, so the two can never "
          "disagree about what 'MTD' is called",
          "const rangeName=()=>{const s=$('#range'),o=s.options[s.selectedIndex]" in html)
    check("ui/chips: the label is muted, not another accent competing with the lit chip",
          re.search(r"\.chips \.rname\{[^}]*color:var\(--muted\)", html) is not None)
    # Split defensively: if the label is missing the check above has already
    # failed, and a crash here would abort the whole suite instead of reporting.
    tail = body.split('class="rname"', 1)[1].split("</span>", 1)[0] if 'class="rname"' in body else "<button"
    check("ui/chips: it is a label, not a button -- clicking it must not set a range",
          "<button" not in tail, tail[:120])


def test_ui_palette_delta_e_figures_match_the_measurements() -> None:
    """Tech-lead re-review 2026-09-10, S4 (second). The :root comment reported the
    --coll separations as "vs --bid 11.4 · vs --ask 11.4 · vs --verde-2 12.3". The
    tech-lead's own run of the validator gives 12.3 against --ask, 11.4 against
    --bid and 13.6 against --verde-2 -- the ask and verde figures were wrong and
    the ask/bid pair had been transposed onto one number.

    This pins the REVIEWER'S MEASUREMENTS, not a recomputation: the figures come
    from the dataviz validator (OKLab dE x100, Machado CVD sim, worst of
    normal/protan/deutan) which is not vendored here, so re-deriving them in the
    suite would be pinning a second implementation rather than the measurement.
    What this makes impossible is the comment drifting away from the numbers
    somebody actually ran -- the palette block is the only place a future reader
    is told how far apart these hues are, and a wrong number there is worse than
    no number, because it retires the question.
    """
    import re
    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()
    measured = {"--ask": "12.3", "--bid": "11.4", "--verde-2": "13.6"}
    line = next((ln for ln in html.splitlines() if "--coll" in ln and "vs --" in ln), "")
    stated = dict(re.findall(r"vs (--[a-z0-9-]+) (\d+\.\d)", line))
    check("ui/palette: the comment reports --coll against all three neighbours it names",
          set(stated) == set(measured), f"stated {stated} on line: {line.strip()[:110]}")
    wrong = {k: (stated.get(k), v) for k, v in measured.items() if stated.get(k) != v}
    check("ui/palette: every stated ΔE is the tech-lead's measured figure (2026-09-10)",
          not wrong, "; ".join(f"{k}: comment says {a}, measured {b}" for k, (a, b) in wrong.items()))

    # PR-6 re-picks --trait-offer. Same discipline, same reason: these are the figures
    # the dataviz validator actually printed (worst of protan/deutan, OKLab dE x100,
    # measured on --surface #141B17), and the comment is the only place a future reader
    # is told how far apart these hues are.
    to_measured = {"--bid": "15.8", "--cancel": "15.4", "--ask": "27.7", "--coll": "25.7"}
    to_line = next((ln for ln in html.splitlines() if "--trait-offer vs --" in ln), "")
    to_stated = dict(re.findall(r"vs (--[a-z0-9-]+) (\d+\.\d)", to_line))
    check("ui/palette: the comment reports --trait-offer against every hue it shares a panel with",
          set(to_stated) == set(to_measured), f"stated {to_stated} on line: {to_line.strip()[:120]}")
    to_wrong = {k: (to_stated.get(k), v) for k, v in to_measured.items() if to_stated.get(k) != v}
    check("ui/palette: every stated --trait-offer ΔE is the validator's measured figure (PR-6)",
          not to_wrong, "; ".join(f"{k}: comment says {a}, measured {b}" for k, (a, b) in to_wrong.items()))
    tok_hex = re.search(r"--trait-offer\s*:\s*(#[0-9A-Fa-f]{6})", html).group(1).upper()
    check("ui/palette: --trait-offer is no longer #C792EA -- it was ΔE 5.0 from --bid under "
          "deuteranopia and 14.6 under NORMAL vision, and PR-6 is the panel that finally draws it",
          tok_hex != "#C792EA" and tok_hex == "#B266FF", tok_hex)

    # PR-8's exit stack. Same discipline again: these are the hexes the validator was
    # actually run on (dataviz scripts/validate_palette.js, --mode dark --surface
    # #141B17 --pairs all), and the run is recorded in the :root comment and docs/08
    # §4b.1. The first draft -- #F0A202 / #7E8F87 / #5C7C8A -- was chosen by eye and
    # measured worst-CVD 2.6 / worst-normal 7.7, so this pins the measured set rather
    # than the shape of a comment: swapping a hex without re-running the validator
    # fails here.
    exits = {n: re.search(rf"--{n}\s*:\s*(#[0-9A-Fa-f]{{6}})", html).group(1).upper()
             for n in ("invalidated", "expired", "censored")}
    check("ui/palette: the PR-8 exit roles are the MEASURED hexes -- #007711 / #5544FF / #EEAA00, "
          "worst-CVD 16.8 and worst-normal 27.1 on the five-colour stack",
          exits == {"invalidated": "#007711", "expired": "#5544FF", "censored": "#EEAA00"}, str(exits))
    check("ui/palette: the eyeballed first draft (#F0A202 / #7E8F87 / #5C7C8A, worst-CVD 2.6) is "
          "gone from the tokens and survives only as the recorded measurement that condemned it",
          not any(h in {"#F0A202", "#7E8F87", "#5C7C8A"} for h in exits.values()), str(exits))


# ===========================================================================
# Unattended running: launchd LaunchAgents (PR-1).
#
# launchctl does not exist off macOS, so nothing here loads a job. What CAN be
# tested anywhere -- and is where the real risk lies -- is that the generated
# property lists are valid XML with the right keys, and that `ingest` honours
# the exit-code contract a supervisor depends on. A malformed plist does not
# fail loudly: launchd simply never runs the job, and a recorder that silently
# never started is indistinguishable from a quiet market.
# ===========================================================================
def _launchd():
    sys.path.insert(0, str(ROOT / "tools"))
    import launchd  # noqa: PLC0415 - deliberately late: tools/ is not a package
    return launchd


def test_launchd_plists_are_valid_and_correct(tmp: Path) -> None:
    import plistlib
    ld = _launchd()
    proj = tmp / "proj"
    (proj / "src").mkdir(parents=True, exist_ok=True)

    parsed = {}
    for label in ld.ALL_LABELS:
        raw = ld.render(label, proj, python="/usr/bin/python3")
        try:
            parsed[label] = plistlib.loads(raw)
            ok = True
        except Exception as exc:  # noqa: BLE001 - the point of the assertion
            ok, parsed[label] = False, {}
            check(f"launchd: {label} plist parses as XML", False, str(exc))
        if ok:
            check(f"launchd: {label} plist parses as XML", True)

    rec = parsed[ld.RECORDER]
    check("launchd: recorder Label matches the filename it is installed under",
          rec.get("Label") == "com.navanax.recorder")
    check("launchd: recorder runs `navanax.cli ingest --supervised`",
          rec.get("ProgramArguments") ==
          ["/usr/bin/python3", "-m", "navanax.cli", "ingest", "--supervised"],
          repr(rec.get("ProgramArguments")))
    check("launchd: recorder has KeepAlive true -- a crash is restarted",
          rec.get("KeepAlive") is True)
    check("launchd: recorder has RunAtLoad true -- it starts at login",
          rec.get("RunAtLoad") is True)
    check("launchd: recorder throttles restarts to ~10s, so a failing job cannot hot-loop",
          rec.get("ThrottleInterval") == 10, repr(rec.get("ThrottleInterval")))
    check("launchd: recorder ExitTimeOut exceeds the default, so SIGTERM has time to flush the final frame",
          isinstance(rec.get("ExitTimeOut"), int) and rec["ExitTimeOut"] >= 30,
          repr(rec.get("ExitTimeOut")))
    check("launchd: recorder WorkingDirectory is the ABSOLUTE project path",
          rec.get("WorkingDirectory") == str(proj.resolve())
          and Path(rec["WorkingDirectory"]).is_absolute(), repr(rec.get("WorkingDirectory")))
    check("launchd: recorder log path is absolute and under data/logs/",
          rec.get("StandardOutPath") == str(proj.resolve() / "data" / "logs" / "recorder.log")
          and rec.get("StandardErrorPath") == rec.get("StandardOutPath"),
          repr(rec.get("StandardOutPath")))
    env = rec.get("EnvironmentVariables") or {}
    # launchd agents do NOT read .zprofile: whatever PATH is here is the whole PATH.
    for needed in ("/Library/Frameworks/Python.framework/Versions/3.14/bin",
                   "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"):
        check(f"launchd: recorder PATH covers {needed}", needed in env.get("PATH", ""),
              env.get("PATH", ""))
    check("launchd: recorder output is unbuffered, or the log looks empty for minutes",
          env.get("PYTHONUNBUFFERED") == "1")
    check("launchd: no API key is written into the plist -- the CLI reads .env itself",
          not any("OPENSEA" in k or "KEY" in k.upper() for k in env),
          ", ".join(env))

    dash = parsed[ld.DASHBOARD]
    check("launchd: dashboard runs with --no-browser (nothing to open at login)",
          "--no-browser" in (dash.get("ProgramArguments") or []))
    check("launchd: dashboard is pinned to port 8765",
          dash.get("ProgramArguments", [])[-2:] == ["--port", "8765"],
          repr(dash.get("ProgramArguments")))
    check("launchd: dashboard has KeepAlive and RunAtLoad",
          dash.get("KeepAlive") is True and dash.get("RunAtLoad") is True)

    tr = parsed[ld.TRAITS]
    check("launchd: traits runs the `traits` command",
          tr.get("ProgramArguments", [])[-1] == "traits", repr(tr.get("ProgramArguments")))
    check("launchd: traits is scheduled daily at 03:30 local",
          tr.get("StartCalendarInterval") == {"Hour": 3, "Minute": 30},
          repr(tr.get("StartCalendarInterval")))
    check("launchd: traits is NOT RunAtLoad -- metered budget is never spent before the Operator is told (tech-lead PR-1 S3)",
          tr.get("RunAtLoad") is False)
    # The one that would quietly drain the metered REST budget forever.
    check("launchd: traits has NO KeepAlive -- it is a job that is SUPPOSED to finish, "
          "and restarting it in a loop would drain the 120/hr REST bucket",
          "KeepAlive" not in tr, repr(tr.get("KeepAlive")))

    ka = parsed[ld.KEEPAWAKE]
    check("launchd: keepawake runs Apple's caffeinate with -i (no idle sleep) and "
          "-s (only on AC power)",
          ka.get("ProgramArguments") == ["/usr/bin/caffeinate", "-i", "-s"],
          repr(ka.get("ProgramArguments")))
    check("launchd: keepawake is NOT part of the autostart set -- changing when the "
          "Mac sleeps is opt-in",
          ld.KEEPAWAKE not in ld.AUTOSTART_LABELS
          and set(ld.AUTOSTART_LABELS) == {ld.RECORDER, ld.DASHBOARD, ld.TRAITS})

    # Every installed job writes to its own log, or a tail shows the wrong process.
    logs = [parsed[lbl]["StandardOutPath"] for lbl in ld.ALL_LABELS]
    check("launchd: every job logs to a DISTINCT file", len(set(logs)) == len(logs))


def test_launchd_render_cli(tmp: Path) -> None:
    """The .command files shell out to this; a broken CLI means no plist at all."""
    import plistlib
    import subprocess
    out = tmp / "agents" / "com.navanax.recorder.plist"
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "launchd.py"), "render",
         "com.navanax.recorder", "--root", str(tmp), "--out", str(out)],
        capture_output=True, text=True, timeout=60)
    check("launchd cli: `render --out` exits 0 and creates the parent directory",
          r.returncode == 0 and out.exists(), r.stderr[:300])
    if out.exists():
        try:
            d = plistlib.loads(out.read_bytes())
            check("launchd cli: the written file parses", d.get("Label") == "com.navanax.recorder")
        except Exception as exc:  # noqa: BLE001
            check("launchd cli: the written file parses", False, str(exc))
    check("launchd cli: no .tmp file is left behind (an interrupted write must not "
          "leave a half-plist launchd would refuse)",
          not list((tmp / "agents").glob("*.tmp")))

    bad = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "launchd.py"), "render", "com.navanax.nope",
         "--root", str(tmp)],
        capture_output=True, text=True, timeout=60)
    check("launchd cli: an unknown label is refused non-zero, not rendered blank",
          bad.returncode != 0 and "unknown label" in bad.stderr, bad.stderr[:200])

    lbl = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "launchd.py"), "labels"],
        capture_output=True, text=True, timeout=60)
    check("launchd cli: `labels` lists exactly the three autostart jobs",
          lbl.stdout.split() == ["com.navanax.recorder", "com.navanax.dashboard",
                                 "com.navanax.traits"], lbl.stdout)


def _supervised_root(tmp: Path, name: str, *, env_file: str | None) -> Path:
    """A throwaway project root: gzip codec (no zstandard needed), one collection."""
    root = tmp / name
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "base.yaml").write_text(
        "version: 1\nenvironment: local\n"
        "opensea:\n  stream_url: \"wss://127.0.0.1:1/socket/websocket\"\n"
        "  heartbeat_seconds: 30\n  max_backoff_seconds: 60\n"
        "landing:\n  root: \"data/landing\"\n  codec: gzip\n  codec_level: 1\n"
        "  roll_bytes: 1048576\n  flush_seconds: 5\n  flush_events: 100\n"
        "opstore:\n  path: \"data/ops.db\"\n"
        "analytical:\n  path: \"data/analytics.sqlite\"\n"
    )
    (root / "config" / "watchlist.yaml").write_text(
        "version: 1\ncollections:\n  - slug: argonauts\n    chain: ethereum\n")
    if env_file is not None:
        (root / ".env").write_text(env_file)
    return root


def test_ingest_supervised_exit_code_contract(tmp: Path) -> None:
    """launchd KeepAlive restarts on ANY exit. What matters is that a refusal is
    NON-ZERO, prompt, and says why -- an ingest that hangs instead of exiting is
    a recorder that records nothing and never gets restarted.
    """
    import subprocess

    def run(root: Path, timeout: float = 90.0):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("OPENSEA_API_KEY", None)   # the parent's real key must not leak in
        env.pop("NAVANAX_ENV", None)
        try:
            return subprocess.run(
                [sys.executable, "-m", "navanax.cli", "--root", str(root),
                 "ingest", "--supervised"],
                capture_output=True, text=True, timeout=timeout, env=env, cwd=str(ROOT))
        except subprocess.TimeoutExpired:
            return None

    # -- no .env: configuration refusal, exit 2 --------------------------------
    r = run(_supervised_root(tmp, "no-env", env_file=None), timeout=60)
    if r is None:
        check("ingest --supervised: refuses promptly when .env is missing "
              "(a hang here is a recorder that never records and is never restarted)",
              False, "timed out")
    else:
        out = r.stdout + r.stderr
        check("ingest --supervised: exit 2 when .env is missing", r.returncode == 2,
              f"exit={r.returncode}: {out[-400:]}")
        check("ingest --supervised: the refusal names the key and where to get one",
              "OPENSEA_API_KEY" in out and "opensea.io" in out, out[-400:])
        check("ingest --supervised: prints a run banner with its pid, so one restart "
              "can be told from the next in a shared log file",
              "supervised" in out and "pid=" in out, out[:200])

    # -- another process holds the landing-zone lock: exit 4 -------------------
    # The guarantee that makes autostart-install safe: two recorders can never
    # write to one landing zone, because the second REFUSES rather than racing.
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is None:
        check("ingest --supervised: exit 4 when another recorder holds the lock",
              True, "skipped: no fcntl on this platform")
    else:
        root = _supervised_root(tmp, "locked", env_file="OPENSEA_API_KEY=placeholder\n")
        lock = root / "data" / "landing" / ".ingest.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.seek(0)
            fh.truncate()
            fh.write("pid=999999 started=2026-09-09T00:00:00+00:00\n")
            fh.flush()
            r2 = run(root, timeout=60)
        if r2 is None:
            check("ingest --supervised: exit 4 when another recorder holds the lock",
                  False, "timed out -- it should refuse immediately, not wait")
        else:
            out2 = r2.stdout + r2.stderr
            check("ingest --supervised: exit 4 when another recorder holds the lock",
                  r2.returncode == 4, f"exit={r2.returncode}: {out2[-400:]}")
            check("ingest --supervised: the lock refusal explains WHY two writers are "
                  "forbidden (silently lost gap records)",
                  "REFUSING TO INGEST" in out2 and "LOSE manifest" in out2, out2[-400:])


def test_ingest_supervised_sigterm_is_a_clean_stop(tmp: Path) -> None:
    """`launchctl bootout` sends SIGTERM. If that were not a clean stop, every
    uninstall, every logout and every reinstall would drop the final frame --
    up to ~7.5 s of events that cannot be re-fetched (BUG-20260909-037).

    No network is needed: the consumer cannot reach the unroutable stream URL in
    the fixture config, so it sits in its reconnect-and-record-a-gap loop, which
    is exactly the state SIGTERM has to interrupt cleanly.
    """
    import signal
    import subprocess

    root = _supervised_root(tmp, "sigterm", env_file="OPENSEA_API_KEY=placeholder\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("OPENSEA_API_KEY", None)
    env.pop("NAVANAX_ENV", None)
    p = subprocess.Popen(
        [sys.executable, "-m", "navanax.cli", "--root", str(root), "ingest", "--supervised"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=str(ROOT))
    time.sleep(4)          # let it get into the run loop
    running = p.poll() is None
    p.send_signal(signal.SIGTERM)
    try:
        out = p.communicate(timeout=45)[0]
        code = p.returncode
    except subprocess.TimeoutExpired:
        p.kill()
        out, code = p.communicate()[0], None

    check("ingest --supervised: stays up while the stream is unreachable rather than "
          "exiting (a rejected key or an outage must not kill the recorder)",
          running, "it exited on its own before SIGTERM was sent")
    check("ingest --supervised: SIGTERM exits 0 -- so `launchctl bootout`, logout and "
          "reinstall are clean stops, not kills that drop the final frame",
          code == 0, f"exit={code}: {(out or '')[-500:]}")
    check("ingest --supervised: the shutdown says which signal stopped it",
          "SIGTERM" in (out or ""), (out or "")[-500:])
    lock = root / "data" / "landing" / ".ingest.lock"
    check("ingest --supervised: the landing-zone lock is released on exit, so the "
          "launchd restart is not refused by its own predecessor",
          not lock.exists() or _flock_is_free(lock))


def _flock_is_free(lock: Path) -> bool:
    try:
        import fcntl
    except ImportError:
        return True
    with lock.open("a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return True


def test_ingest_exit_codes_are_documented_and_distinct() -> None:
    """A supervisor and a human both read these numbers. They must not collide,
    and autostart-status.command prints a legend that has to match the code.
    """
    from navanax import cli
    codes = {"OK": cli.EXIT_OK, "CONFIG": cli.EXIT_CONFIG, "CODEC": cli.EXIT_CODEC,
             "ALREADY_RUNNING": cli.EXIT_ALREADY_RUNNING, "FATAL": cli.EXIT_FATAL}
    check("ingest: every exit code is distinct", len(set(codes.values())) == len(codes),
          repr(codes))
    check("ingest: only a clean stop is zero",
          codes["OK"] == 0 and all(v != 0 for k, v in codes.items() if k != "OK"),
          repr(codes))
    legend = (ROOT / "autostart-status.command").read_text()
    missing = [f"{k}={v}" for k, v in codes.items()
               if f"     {v}  " not in legend]
    check("ingest: autostart-status.command's legend covers every exit code "
          "(a code with no explanation is a number the operator cannot act on)",
          not missing, ", ".join(missing))
    # BUG class: a bare RuntimeError from deep in the stream used to be reported
    # to the operator as "another ingest is already running".
    check("ingest: the single-instance refusal has its own exception type, so an "
          "unrelated RuntimeError cannot be reported as a lock conflict",
          issubclass(cli.SingleInstanceError, RuntimeError)
          and cli.SingleInstanceError is not RuntimeError)


def test_autostart_install_refuses_before_touching_anything(tmp: Path) -> None:
    """The installer writes to ~/Library/LaunchAgents. Both of its refusals must
    fire BEFORE it writes anything, and must say what to do instead.
    """
    import shutil as _shutil
    import subprocess

    script = ROOT / "autostart-install.command"
    check("autostart-install.command exists and is executable",
          script.exists() and os.access(script, os.X_OK))

    # 1. Not macOS. This machine is not a Mac, so this runs for real.
    sandbox_home = tmp / "home-notmac"
    (sandbox_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(sandbox_home)
    r = subprocess.run(["bash", str(script)], input="\n", capture_output=True,
                       text=True, timeout=120, env=env)
    out = r.stdout + r.stderr
    on_mac = os.uname().sysname == "Darwin"
    if on_mac:
        check("autostart-install: refuses on a non-Mac", True,
              "skipped: this IS a Mac, tested via the stubbed-uname case below")
    else:
        check("autostart-install: refuses on a non-Mac, non-zero, naming the OS",
              r.returncode != 0 and "REFUSED" in out and "macOS" in out, out[-300:])
        check("autostart-install: the non-Mac refusal says nothing was changed",
              "Nothing was changed" in out, out[-300:])
    check("autostart-install: a refused run installs NO plist",
          not list((sandbox_home / "Library" / "LaunchAgents").glob("*.plist")))

    # 2. macOS, but no .env. Stub `uname` so the script believes it is on a Mac,
    #    and run it from a copy in an empty directory so there is no .env.
    sandbox = tmp / "nodotenv"
    (sandbox / "bin").mkdir(parents=True, exist_ok=True)
    stub = sandbox / "bin" / "uname"
    stub.write_text('#!/bin/bash\nif [ "$1" = "-s" ]; then echo Darwin; else echo Darwin; fi\n')
    stub.chmod(0o755)
    _shutil.copy2(script, sandbox / "autostart-install.command")
    env2 = dict(os.environ)
    env2["PATH"] = f"{sandbox / 'bin'}:{env2.get('PATH', '')}"
    env2["HOME"] = str(sandbox_home)
    r2 = subprocess.run(["bash", str(sandbox / "autostart-install.command")], input="\n",
                        capture_output=True, text=True, timeout=120, env=env2)
    out2 = r2.stdout + r2.stderr
    check("autostart-install: with no .env it refuses non-zero rather than installing "
          "three jobs that would restart every 10s forever and record nothing",
          r2.returncode != 0 and "REFUSED" in out2 and ".env" in out2, out2[-400:])
    check("autostart-install: the .env refusal tells the operator the exact fix",
          ".env.example" in out2, out2[-400:])
    check("autostart-install: still no plist installed after the .env refusal",
          not list((sandbox_home / "Library" / "LaunchAgents").glob("*.plist")))


def test_autostart_scripts_are_consistent() -> None:
    """Cheap cross-checks that catch a rename in one file and not the other."""
    ld = _launchd()
    install = (ROOT / "autostart-install.command").read_text()
    uninstall = (ROOT / "autostart-uninstall.command").read_text()
    status = (ROOT / "autostart-status.command").read_text()

    for lbl in ld.AUTOSTART_LABELS:
        check(f"autostart: install and uninstall both handle {lbl}",
              lbl in install and lbl in uninstall and lbl in status)
    check("autostart: uninstall does not silently leave the opt-in keep-awake job "
          "unmentioned", ld.KEEPAWAKE in uninstall)
    check("autostart: install renders plists through tools/launchd.py rather than a "
          "heredoc (plistlib cannot emit invalid XML; a heredoc can)",
          "tools/launchd.py render" in install)
    check("autostart: install stops a hand-started recorder before installing, using "
          "the pid in the landing-zone lock",
          ".ingest.lock" in install and "kill -TERM" in install)
    check("autostart: install falls back to the older `launchctl load` on older macOS",
          "launchctl bootstrap" in install and "launchctl load -w" in install)
    check("autostart: uninstall removes the plist FILES, not just the loaded jobs",
          "rm -f" in uninstall and "LaunchAgents" in uninstall)
    check("autostart: install states the sleep caveat launchd cannot fix",
          "SLEEP" in install.upper() and "caffeinate" in install)
    check("autostart: install does NOT install the keep-awake job itself",
          "keepawake-install.command" in install
          and 'render "com.navanax.keepawake"' not in install)
    check("autostart: install says closing the window changes nothing",
          "CLOSING THIS WINDOW CHANGES NOTHING" in install)
    for name in ("keepawake-install.command", "keepawake-uninstall.command",
                 "autostart-status.command", "autostart-uninstall.command"):
        p = ROOT / name
        check(f"autostart: {name} exists and is executable",
              p.exists() and os.access(p, os.X_OK))
    ka = (ROOT / "keepawake-install.command").read_text()
    check("keepawake-install: explains the battery/lid trade-off before acting",
          "BATTERY" in ka.upper() and "LID" in ka.upper() and "AC power" in ka)
    check("keepawake-install: requires an explicit yes -- it changes machine "
          "behaviour, not just this project's",
          'ANSWER" != "yes"' in ka or '"$ANSWER" != "yes"' in ka)


def test_environments_doc_documents_unattended_running() -> None:
    doc = (ROOT / "docs" / "04_ENVIRONMENTS.md").read_text()
    check("docs/04: has a 'Running unattended' section",
          "Running unattended" in doc)
    for term in ("launchd", "KeepAlive", "data/logs", "autostart-uninstall.command",
                 "sleep"):
        check(f"docs/04 §8 explains: {term}", term in doc)
    check("docs/04 §8 records the exit-code contract a supervisor depends on",
          "exit code" in doc.lower() and "ThrottleInterval" in doc)
    readme = (ROOT / "README.md").read_text()
    check("README points at the unattended-running section",
          "autostart-install.command" in readme)


# ===========================================================================
# PR-2 (order_lives) and PR-4 (order_criteria). Every check below was written
# against the OLD code first and fails there -- the tech-lead's gate, docs/03 §9.
# ===========================================================================
def _lives_store(tmp: Path, name: str):
    """A store with the traits schema and a `put(frame, time, seq, **overrides)`
    that pins valid_ts to the time given, so a lifecycle can be laid out exactly."""
    from navanax.normalize import COLS, Normalizer, iso_to_ts, parse_event
    from navanax.traits import ensure_schema

    n = Normalizer(tmp / "empty-lz", tmp / name)
    ensure_schema(n.conn)

    def put(raw, recv, seq, **over):
        rr = parse_event(_env(seq, raw, recv))
        rr["file"] = "f"
        rr["valid_at"], rr["valid_ts"] = recv, iso_to_ts(recv)
        rr.update(over)
        n.conn.execute(f"INSERT INTO events ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                       tuple(rr.get(c) for c in COLS))
        for c in rr.get("criteria") or []:
            n.conn.execute(
                "INSERT OR REPLACE INTO order_criteria VALUES (?,?,?,?,?,?,?,?)",
                (rr["run"], rr["seq"], c["idx"], c["kind"], c["trait_type"], c["value"],
                 c["num_min"], c["num_max"]))
    return n, put


def test_order_lives_primitive(tmp: Path) -> None:
    """One row per order_hash, one definition of "ended" (quant §1.0, tech-lead PR-2).

    Every assertion here fails against the pre-order_lives code, which had three
    different terminator lists in one module and no relation to put them in.
    """
    from navanax.metrics import MetricEngine, load_intervals, standing_sql
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, "lives.sqlite")
    noexp = {"expiration_at": None, "expiration_ts": None}
    # a bid cancelled TWICE: a duplicate cancel is one life, not two (T1)
    put(REAL_BID, "2026-09-09T10:00:00Z", 1, order_hash="0xdup", token_id="1", **noexp)
    put(REAL_CANCEL, "2026-09-09T10:00:10Z", 2, order_hash="0xdup", token_id="1")
    put(REAL_CANCEL, "2026-09-09T10:00:20Z", 3, order_hash="0xdup", token_id="1")
    # invalidated, then revalidated: the order re-opened (T3a)
    put(REAL_BID, "2026-09-09T10:00:00Z", 4, order_hash="0xreval", token_id="2", **noexp)
    put(REAL_INVALIDATE, "2026-09-09T10:00:05Z", 5, order_hash="0xreval", token_id="2")
    put(REAL_INVALIDATE, "2026-09-09T10:00:07Z", 6, order_hash="0xreval", token_id="2",
        event_type="order_revalidate")
    # a terminator we cannot place in time (T3c)
    put(REAL_BID, "2026-09-09T10:00:00Z", 7, order_hash="0xnullterm", token_id="3", **noexp)
    put(REAL_CANCEL, "2026-09-09T10:00:15Z", 8, order_hash="0xnullterm", token_id="3",
        valid_at=None, valid_ts=None)
    # a collection offer good for five: five units of depth, not one (T3b)
    put(REAL_COLL_OFFER, "2026-09-09T10:00:00Z", 9, order_hash="0xqty5", quantity=5, **noexp)
    # an order that expires with no terminator ever seen
    put(DOC_LISTING, "2026-09-09T10:00:00Z", 10, order_hash="0xexpire", token_id="4",
        expiration_at="2026-09-09T10:05:00Z", expiration_ts=iso_to_ts("2026-09-09T10:05:00Z"))
    # a termination whose placement we never saw: left-truncated (metric 12)
    put(REAL_CANCEL, "2026-09-09T10:00:30Z", 11, order_hash="0xorphan", token_id="5")
    n.conn.commit()
    events_before = n.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    refresh_order_lives(n.conn)

    def life(h: str) -> dict:
        cur = n.conn.execute("SELECT * FROM order_lives WHERE order_hash=?", (h,))
        r = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], r, strict=True)) if r else {}

    def standing(h: str, at: str) -> bool:
        sql, a = standing_sql("e", iso_to_ts(at))
        return bool(n.conn.execute(
            f"SELECT EXISTS(SELECT 1 FROM events e WHERE e.order_hash=? AND {sql})",
            (h, *a)).fetchone()[0])

    check("order_lives: one row per order_hash, six orders from eleven events",
          n.conn.execute("SELECT COUNT(*) FROM order_lives").fetchone()[0] == 6,
          str(n.conn.execute("SELECT order_hash FROM order_lives").fetchall()))
    d = life("0xdup")
    check("order_lives (T1): two cancels on one order are ONE life, terminated by the FIRST",
          d["t_term"] == iso_to_ts("2026-09-09T10:00:10Z") and d["terminations_seen"] == 2
          and d["exit_reason"] == "cancelled", str(d))
    r = life("0xreval")
    check("order_lives (T3a): invalidate followed by revalidate is not a termination",
          r["exit_reason"] == "censored" and r["t_term"] is None and r["revalidated"] == 1, str(r))
    check("order_lives (T3a): ...so the revalidated order is standing", standing("0xreval", "2026-09-09T10:10:00Z"))
    u = life("0xnullterm")
    check("order_lives (T3c): a terminator with valid_ts NULL is `unknown`, counted, not dropped",
          u["exit_reason"] == "unknown" and u["t_term"] is None and u["terminations_seen"] == 1, str(u))
    check("order_lives (T3c): ...and does NOT leave the order standing forever",
          not standing("0xnullterm", "2026-09-09T10:10:00Z"))
    q = life("0xqty5")
    check("order_lives (T3b): quantity is carried from the placement", q["quantity"] == 5, str(q))
    x = life("0xexpire")
    check("order_lives: expiry is a DERIVED exit at expiration_ts, source='derived'",
          x["exit_reason"] == "expired" and x["exit_source"] == "derived"
          and x["t_term"] == iso_to_ts("2026-09-09T10:05:00Z"), str(x))
    check("order_lives: inferring expiry writes NO market event -- the record is untouched",
          n.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
          and n.conn.execute("SELECT COUNT(*) FROM events WHERE event_type LIKE '%expir%'").fetchone()[0] == 0)
    check("order_lives: standing before its expiry, not after",
          standing("0xexpire", "2026-09-09T10:03:00Z") and not standing("0xexpire", "2026-09-09T10:06:00Z"))
    o = life("0xorphan")
    check("order_lives: a termination with no placement is an orphan -- placement_seen=0, t_place never imputed",
          o["placement_seen"] == 0 and o["t_place"] is None and o["exit_reason"] == "cancelled", str(o))
    check("order_lives: an orphan is not standing (we never saw it placed)",
          not standing("0xorphan", "2026-09-09T10:00:31Z"))
    check("order_lives: the fold records which rules made the row",
          d["method_version"] >= 1 and life("0xqty5")["scope_kind"] == "collection")

    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    book = eng.live_book("argonauts", now=datetime(2026, 9, 9, 10, 10, tzinfo=timezone.utc))
    hashes = {r["order_hash"] for v in ("asks", "item_bids", "collection_offers") for r in book[v]}
    check("live book reads the one definition: the revalidated bid and the qty-5 offer stand, "
          "the cancelled, the untimed and the orphan do not",
          hashes == {"0xreval", "0xqty5"}, str(hashes))
    check("live book (T3b): depth counts UNITS, so one quantity-5 offer is five",
          book["depth"]["collection_offers"] == 5, str(book["depth"]))
    n.close()


def test_bid_lifetimes_censoring_and_orphans(tmp: Path) -> None:
    """BUG-049: the old estimator's `n` was a count of bid x cancel PAIRS."""
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, "lt.sqlite")
    noexp = {"expiration_at": None, "expiration_ts": None}
    put(REAL_BID, "2026-09-09T10:00:00Z", 1, order_hash="0xa", token_id="1", **noexp)
    put(REAL_CANCEL, "2026-09-09T10:00:10Z", 2, order_hash="0xa", token_id="1")
    put(REAL_CANCEL, "2026-09-09T10:00:20Z", 3, order_hash="0xa", token_id="1")
    put(REAL_BID, "2026-09-09T10:00:00Z", 4, order_hash="0xb", token_id="2", **noexp)   # never ends
    put(REAL_CANCEL, "2026-09-09T10:00:30Z", 5, order_hash="0xc", token_id="3")         # orphan
    n.conn.commit()
    refresh_order_lives(n.conn)
    pairs = n.conn.execute(
        """SELECT COUNT(*) FROM events b JOIN events c ON c.order_hash = b.order_hash
           WHERE b.event_type='item_received_bid' AND c.event_type='item_cancelled'
             AND c.valid_ts >= b.valid_ts""").fetchone()[0]
    check("bid lifetimes (BUG-049): the old unbounded join really does cross-product -- 2 pairs, 1 bid",
          pairs == 2, f"got {pairs}")
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    lt = eng.bid_lifetimes("argonauts", iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts("2026-09-09T11:00:00Z"))
    check("bid lifetimes: n counts ORDERS, and at n = 1 the percentiles are withheld (REQ-F-19), "
          "so the duration is checked on the relation itself, not through a percentile",
          lt["n"] == 1 and lt["median_s"] is None
          and n.conn.execute("SELECT t_term - t_place FROM order_lives WHERE order_hash='0xa'"
                             ).fetchone()[0] == 10.0, str(lt))
    check("bid lifetimes: a bid still standing at window end is censored -- counted, not dropped",
          lt["censored_n"] == 1 and lt["orders_at_risk"] == 2, str(lt))
    check("bid lifetimes: the orphan rate is reported WITH its counts (project rule 4)",
          lt["orphan_terminations"] == 1 and lt["terminations_in_window"] == 2
          and abs(lt["orphan_rate"] - 0.5) < 1e-9, str(lt))
    check("bid lifetimes: percentiles still carry their reliability flag at small n",
          lt["percentiles_reliable"] is False and lt["min_n_for_percentiles"] == 30)
    n.close()


def test_order_criteria_parsing_and_migration(tmp: Path) -> None:
    """PR-4: the criteria a trait offer carries, parsed, stored, migrated, re-foldable."""
    import copy
    import sqlite3

    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import Normalizer, parse_event
    from navanax.traits import ensure_schema

    def crit_of(mutate) -> dict:
        raw = copy.deepcopy(REAL_TRAIT_OFFER)
        mutate(raw[4]["payload"])
        return parse_event(_env(1, raw, "2026-09-09T10:19:32Z"))

    r = parse_event(_env(1, REAL_TRAIT_OFFER, "2026-09-09T10:19:32Z"))
    check("criteria: the single/list form parses to one string criterion",
          r["criteria_n"] == 1 and r["criteria_numeric_n"] == 0
          and r["criteria"] == [{"idx": 0, "kind": "string", "trait_type": "Print",
                                 "value": "Unclaimed", "num_min": None, "num_max": None}], str(r["criteria"]))

    def only_list(p):
        p["trait_criteria"] = None
        p["trait_criteria_list"] = [{"trait_type": "Cloak", "trait_name": "Ivory"},
                                    {"trait_type": "Print", "trait_name": "Unclaimed"}]
    r2 = crit_of(only_list)
    check("criteria: the list-only form (16 of 39 real offers) is an AND, one row per entry, idx 0..n-1",
          r2["criteria_n"] == 2 and [c["idx"] for c in r2["criteria"]] == [0, 1]
          and [c["trait_type"] for c in r2["criteria"]] == ["Cloak", "Print"], str(r2["criteria"]))

    def numeric(p):
        p["numeric_trait_criteria_list"] = [{"trait_type": "Level", "min": 3, "max": 9}]
    r3 = crit_of(numeric)
    check("criteria: numeric criteria are kept as their own kind, with idx after the string ones",
          r3["criteria_n"] == 1 and r3["criteria_numeric_n"] == 1
          and r3["criteria"][1] == {"idx": 1, "kind": "numeric", "trait_type": "Level",
                                    "value": None, "num_min": 3.0, "num_max": 9.0}, str(r3["criteria"]))

    def unreadable(p):
        p["trait_criteria"] = None
        p["trait_criteria_list"] = [{"trait_type": None, "trait_name": None}]
    r4 = crit_of(unreadable)
    check("criteria: a trait offer we could not read sets criteria_n = 0, NOT NULL (the loud-failure marker)",
          r4["criteria_n"] == 0 and r4["criteria"] == [], str(r4["criteria_n"]))
    check("criteria: an event that carries no criteria at all has criteria_n NULL, not 0",
          parse_event(_env(1, REAL_BID, "2026-09-09T10:20:16Z"))["criteria_n"] is None)

    def cased(p):
        p["trait_criteria"] = {"trait_type": " Print ", "trait_name": "  UNCLAIMED  "}
        p["trait_criteria_list"] = None
    r5 = crit_of(cased)
    check("criteria: values are stored VERBATIM -- stripped, never case-folded (traits.py's rule)",
          r5["criteria"][0]["value"] == "UNCLAIMED" and r5["criteria"][0]["trait_type"] == "Print",
          str(r5["criteria"]))

    # -- additive migration onto a store an earlier version built ------------
    legacy = tmp / "legacy.sqlite"
    lc = sqlite3.connect(str(legacy))
    lc.executescript("""CREATE TABLE events (run TEXT NOT NULL, seq INTEGER NOT NULL, file TEXT,
        observed_at TEXT, valid_at TEXT, observed_ts REAL, valid_ts REAL, event_type TEXT,
        collection TEXT, order_hash TEXT, expiration_at TEXT, expiration_ts REAL,
        PRIMARY KEY (run, seq));
        INSERT INTO events (run, seq, event_type, collection) VALUES ('r', 1, 'trait_offer', 'argonauts');""")
    lc.commit()
    lc.close()
    ln = Normalizer(tmp / "empty-lz", legacy)
    lcols = {row[1] for row in ln.conn.execute("PRAGMA table_info(events)")}
    check("criteria: _migrate adds both columns to a store built by an earlier version (additive, expiration_ts precedent)",
          {"criteria_n", "criteria_numeric_n"} <= lcols)
    check("criteria: migration keeps the existing row and leaves the new columns NULL -- "
          "the criteria are only in the landing zone, so a re-fold is what fills them",
          ln.conn.execute("SELECT COUNT(*), criteria_n FROM events").fetchone() == (1, None))
    rf = ln.refold_criteria()
    check("criteria: refold_criteria REFUSES rather than silently no-opping when no raw frame is stored here",
          rf["raw_frames_available"] is False and rf["action_required"] == "reset_for_refold() then sync()"
          and rf["trait_offers_needing_refold"] == 1, str(rf))
    ln.close()

    # -- through the real path: landing zone -> sync -> criteria rows --------
    clock = FakeClock(datetime(2026, 9, 9, 10, 19, 0, tzinfo=timezone.utc))
    root = tmp / "lz-crit"
    w = LandingZoneWriter(root, "run-crit", codec=GzipCodec(), clock=clock.now,
                          monotonic=clock.monotonic, flush_events=2, auto_flush=False)
    for raw in (REAL_TRAIT_OFFER, REAL_BID):
        w.write(json.dumps(raw), topic="collection:argonauts",
                event_timestamp=raw[4]["payload"]["event_timestamp"])
        clock.advance(1)
    w.flush()
    w.close()
    n = Normalizer(root, tmp / "crit.sqlite")
    ensure_schema(n.conn)
    s1 = n.sync()
    check("criteria: sync writes the criteria rows alongside the events",
          s1["rows_added"] == 2 and s1["criteria_rows"] == 1
          and n.conn.execute("SELECT COUNT(*) FROM order_criteria").fetchone()[0] == 1, str(s1))
    check("criteria: sync also refreshes order_lives, so the standing book is never a fold behind",
          s1["lives_refreshed"] == 2, str(s1))
    s2 = n.sync()
    check("criteria: a second sync adds nothing and duplicates nothing",
          s2["rows_added"] == 0 and n.conn.execute("SELECT COUNT(*) FROM order_criteria").fetchone()[0] == 1)

    landing_files = sorted(p.name for p in root.rglob("*") if p.is_file())
    cleared = n.reset_for_refold()
    check("criteria: reset_for_refold clears the DERIVED rows only",
          cleared["events"] == 2 and cleared["order_criteria"] == 1 and cleared["watermarks"] == 1
          and n.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0, str(cleared))
    check("criteria: reset_for_refold does not touch the landing zone -- not one byte (docs/07 §4)",
          sorted(p.name for p in root.rglob("*") if p.is_file()) == landing_files)
    s3 = n.sync()
    check("criteria: the re-fold is idempotent -- same events, same criteria rows, from the same frames",
          s3["rows_added"] == 2 and s3["criteria_rows"] == 1
          and n.conn.execute("SELECT COUNT(*) FROM order_criteria").fetchone()[0] == 1, str(s3))
    check("criteria: the re-folded trait offer now carries criteria_n on the event row",
          n.conn.execute("SELECT criteria_n, criteria_numeric_n FROM events "
                         "WHERE event_type='trait_offer'").fetchone() == (1, 0))

    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    cov = eng.criteria_coverage("argonauts")
    check("criteria: the casing validation ALERTS when a criterion has no matching trait value",
          cov["distinct_criteria"] == 1 and cov["missing"] == 1 and cov["alert"] is True, str(cov))
    n.conn.execute("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES ('argonauts','1','a','x')")
    n.conn.execute("INSERT INTO traits VALUES ('argonauts','1','Print','unclaimed')")   # wrong case
    n.conn.commit()
    check("criteria: a case-only difference is still a miss -- verbatim on both sides is what makes it visible",
          eng.criteria_coverage("argonauts")["missing"] == 1)
    n.conn.execute("INSERT INTO traits VALUES ('argonauts','1','Print','Unclaimed')")
    n.conn.commit()
    check("criteria: ...and clears once the exact value exists",
          eng.criteria_coverage("argonauts")["missing"] == 0)
    n.close()


def test_trait_offer_matching_rule(tmp: Path) -> None:
    """BUG-051: the blanket exclusion becomes an evidence-based verdict, with a guard.

    The named property test (dataeng §4.3, tech-lead E-V8, blocking) is here:
    a trait offer with criteria_n = 0 matches NOTHING.
    """
    import copy

    from navanax.metrics import MetricEngine, criteria_cover_sql, load_intervals
    from navanax.normalize import refresh_order_lives

    n, put = _lives_store(tmp, "cover.sqlite")
    for tid, print_v in (("1", "Unclaimed"), ("2", "Claimed")):
        n.conn.execute("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES (?,?,?,?)",
                       ("argonauts", tid, f"Argo #{tid}", "2026-09-09T00:00:00Z"))
        n.conn.executemany("INSERT INTO traits VALUES (?,?,?,?)",
                           [("argonauts", tid, "Print", print_v),
                            ("argonauts", tid, "Palette", "Seafoam")])

    def offer(seq, hash_, mutate, **over):
        raw = copy.deepcopy(REAL_TRAIT_OFFER)
        mutate(raw[4]["payload"])
        put(raw, "2026-09-09T10:00:00Z", seq, order_hash=hash_,
            expiration_at=None, expiration_ts=None, **over)

    def one(tt, tn):
        def m(p):
            p["trait_criteria"] = {"trait_type": tt, "trait_name": tn}
            p["trait_criteria_list"] = None
        return m

    def nothing(p):
        p["trait_criteria"] = None
        p["trait_criteria_list"] = None

    def numeric(p):
        p["numeric_trait_criteria_list"] = [{"trait_type": "Level", "min": 1, "max": 5}]

    offer(1, "0xcov", one("Print", "Unclaimed"), price_eth=0.41)     # COVERS  Print:Unclaimed
    offer(2, "0xbad", nothing, price_eth=9.99)                       # unparsed: criteria_n = 0
    offer(3, "0xnum", numeric, price_eth=8.88)                       # string + numeric: UNKNOWN
    offer(4, "0xpar", one("Palette", "Seafoam"), price_eth=0.42)     # PARTIAL under Print:Unclaimed
    offer(5, "0xdis", one("Print", "Claimed"), price_eth=0.43)       # DISJOINT
    put(REAL_COLL_OFFER, "2026-09-09T10:00:00Z", 6, order_hash="0xcoll",
        expiration_at=None, expiration_ts=None)
    n.conn.commit()
    refresh_order_lives(n.conn)
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    now = datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc)

    def counted(traits):
        s = eng.series(metric="bid_count", collection="argonauts", interval="1h",
                       range_="6h", now=now, traits=traits)
        return [v for v in s["raw"] if v][-1]

    check("trait matching: with no filter every bid still counts (6 orders)", counted(None) == 6.0)
    check("trait matching (BUG-051): under Print:Unclaimed the COVERING trait offer counts -- "
          "the blanket exclusion is gone; only it and the collection offer pass",
          counted({"Print": ["Unclaimed"]}) == 2.0, str(counted({"Print": ["Unclaimed"]})))
    def covered(traits) -> set:
        sq, ag = criteria_cover_sql("e", traits)
        return {r[0] for r in n.conn.execute(
            f"SELECT order_hash FROM events e WHERE e.event_type='trait_offer' AND {sq}", ag)}

    every_filter = ({}, {"Print": ["Unclaimed"]}, {"Print": ["Claimed"]}, {"Palette": ["Seafoam"]},
                    {"Print": ["Unclaimed"], "Palette": ["Seafoam"]}, {"Nothing": ["At all"]})
    # BUG-20260910-060: the second half of this check used to read
    # `counted({"Nothing": ["At all"]}) == 1.0` -- a filter on a trait type no token
    # has, expected to return the collection-offer count. That was the defect
    # written down as intended behaviour; a bid on every token is not a bid on any
    # token of an empty set. It is now the rule: undefined on every bucket.
    nothing_at_all = eng.series(metric="bid_count", collection="argonauts", interval="1h",
                                range_="6h", now=now, traits={"Nothing": ["At all"]})
    check("trait matching: the PROPERTY TEST -- a trait offer with criteria_n = 0 matches NOTHING, "
          "under every filter (dataeng §4.3: with no criteria rows the NOT EXISTS is vacuously "
          "true and it would otherwise match every filter and every token)",
          all("0xbad" not in covered(f) for f in every_filter)
          and counted({"Print": ["Unclaimed"]}) == 2.0
          and all(v is None for v in nothing_at_all["raw"]),
          str([sorted(covered(f)) for f in every_filter]))
    got = covered({"Print": ["Unclaimed"]})
    check("trait matching: COVERS is exactly the offers whose every criterion the filter guarantees",
          got == {"0xcov"}, str(got))
    got2 = covered({"Print": ["Unclaimed", "Claimed"]})
    check("trait matching: a multi-value clause guarantees nothing, so it COVERS nothing "
          "(S(F) is not a subset of S(C) when the filter admits both values)", got2 == set(), str(got2))

    v = eng.trait_offer_verdicts("argonauts", {"Print": ["Unclaimed"]}, 0, now.timestamp())
    check("trait matching: the five verdicts are reported separately and never summed",
          (v["covers"], v["partial_n"], v["disjoint"], v["unknown_numeric"], v["unparsed"]) == (1, 1, 1, 1, 1),
          str(v))
    check("trait matching: a PARTIAL offer carries |S(F) n S(C)| AND |S(F)| -- never a bare number",
          v["partial"][0]["overlap_tokens"] == 1 and v["partial"][0]["filter_tokens"] == 1
          and v["partial"][0]["order_hash"] == "0xpar", str(v["partial"]))
    check("trait matching: numeric criteria are UNKNOWN, which is excluded AND counted, never TRUE",
          v["unknown_numeric"] == 1 and "0xnum" not in got)
    s = eng.series(metric="immediacy_cost", collection="argonauts", interval="1h",
                   range_="6h", now=now, traits={"Print": ["Unclaimed"]})
    check("trait matching: the legs line now describes the COVER rule, not 'criteria not stored'",
          "COVER" in s["basis"]["legs"]["collection_bid"]
          and "criteria not stored" not in s["basis"]["legs"]["collection_bid"],
          s["basis"]["legs"]["collection_bid"])
    n.close()


# ===========================================================================
# PR-3 (BUG-20260910-057): immediacy_cost becomes the standing-book quantity
# docs/01 §3.2 defines, and percentiles are withheld rather than flagged.
#
# Every check below fails against the pre-PR-3 code, and fails for a reason, not
# by accident: `standing_series` / `standing_spread` did not exist, `series()`
# took no `book`, and `bid_lifetimes` returned a percentile at any n. The
# before/after of each is in the PR description.
# ===========================================================================
NO_EXP = {"expiration_at": None, "expiration_ts": None}
H11 = datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc)


def _standing_engine(tmp: Path, name: str, rows: list[tuple], fold_at: str = "2026-09-09T12:00:00Z"):
    """A store holding `rows` = [(fixture, iso, seq, overrides)], folded to order_lives."""
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, name)
    for raw, iso, seq, over in rows:
        put(raw, iso, seq, **over)
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts(fold_at))
    return n, MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")


def test_standing_series_is_time_weighted_over_the_bucket(tmp: Path) -> None:
    """A floor is what is STANDING, for as long as it stood (Operator, 2026-09-10).

    Fails today: `MetricEngine.standing_series` does not exist, and `floor_ask`
    is `MIN(price) over the events SEEN in the bucket` -- a price that may have
    lived for one second of the hour, reported as the hour's floor, with no
    coverage, no n and no dispersion beside it.
    """
    from navanax.metrics import time_weighted_quantile
    from navanax.normalize import iso_to_ts

    ts = iso_to_ts
    # One listing at 1.0, standing 10:00 -> 10:20 of a 10:00 bucket, then cancelled.
    n, eng = _standing_engine(tmp, "st1.sqlite", [
        (DOC_LISTING, "2026-09-09T10:00:00Z", 1, {"order_hash": "0xL", "token_id": "1",
                                                  "price_eth": 1.0, **NO_EXP}),
        (REAL_CANCEL, "2026-09-09T10:20:00Z", 2, {"order_hash": "0xL", "token_id": "1"}),
    ])
    s = eng.standing_series("ask", "argonauts", ts("2026-09-09T09:00:00Z"),
                            ts("2026-09-09T11:00:00Z"), "1h", "ETH", None, H11)
    check("standing series: coverage is the SECONDS the leg stood over the bucket seconds -- "
          "20 minutes of an hour is 1/3, not '1 observation'",
          abs(s["coverage"][1] - 1 / 3) < 1e-9 and s["standing_seconds"][1] == 1200.0,
          f"got coverage={s['coverage']} standing={s['standing_seconds']}")
    check("standing series: the median of a book with one order in it is that order's price",
          s["median"][1] == 1.0 and s["n"][1] == 1, str(s["median"]))
    check("standing series: a bucket where NOTHING stood is null with coverage 0 and n 0 -- "
          "never 0.0, because 0 is a price and 'no standing ask' is not the price zero",
          s["median"][0] is None and s["coverage"][0] == 0.0 and s["n"][0] == 0,
          f"got {s['median'][0]!r} / {s['coverage'][0]!r} / {s['n'][0]!r}")
    fa = eng.series(metric="floor_ask", collection="argonauts", interval="1h",
                    range_="2h", now=H11)
    check("standing series: ...and the same hole reaches the chart through series(), as null",
          fa["raw"][0] is None and fa["raw"][1] == 1.0 and fa["basis"]["book"] == "standing",
          str(fa["raw"]))
    check("standing series: every response says the book is LEFT-TRUNCATED -- the reconstructed "
          "floor is an upper bound, because orders resting before we connected are invisible",
          fa["basis"]["left_truncated"] is True and "upper bound" in fa["basis"]["left_truncation_note"])
    n.close()

    # Two overlapping listings: 1.0 standing all hour, 0.8 standing only the first
    # 20 minutes. The interval extremum calls the hour's floor 0.8. It was 0.8 for
    # a third of the hour and 1.0 for two thirds.
    n2, eng2 = _standing_engine(tmp, "st2.sqlite", [
        (DOC_LISTING, "2026-09-09T10:00:00Z", 1, {"order_hash": "0xHI", "token_id": "1",
                                                  "price_eth": 1.0, **NO_EXP}),
        (DOC_LISTING, "2026-09-09T10:00:00Z", 2, {"order_hash": "0xLO", "token_id": "2",
                                                  "price_eth": 0.8, **NO_EXP}),
        (REAL_CANCEL, "2026-09-09T10:20:00Z", 3, {"order_hash": "0xLO", "token_id": "2"}),
    ])
    s2 = eng2.standing_series("ask", "argonauts", ts("2026-09-09T10:00:00Z"),
                              ts("2026-09-09T11:00:00Z"), "1h", "ETH", None, H11)
    old = eng2.series(metric="floor_ask", collection="argonauts", interval="1h",
                      range_="1h", now=H11, book="observed")
    check("standing series: with 0.8 standing 20 min and 1.0 standing 60, the time-weighted "
          "median is 1.0 -- the level that actually held -- where the interval extremum says 0.8",
          s2["median"][0] == 1.0 and old["raw"][0] == 0.8, f"standing={s2['median']} observed={old['raw']}")
    check("standing series: both listings are counted and the book was covered the whole bucket",
          s2["n"][0] == 2 and abs(s2["coverage"][0] - 1.0) < 1e-9, str(s2))
    segs = [(1200.0, 0.8), (2400.0, 1.0)]
    check("standing series: the dispersion is over TIME, so [p10, p90] spans both levels -- "
          "0.8 held a third of the bucket and is the p10, 1.0 held the rest and is the p90",
          time_weighted_quantile(segs, 0.10) == 0.8 and time_weighted_quantile(segs, 0.90) == 1.0
          and time_weighted_quantile(segs, 0.5) == 1.0)
    n2.close()


def test_immediacy_cost_is_a_standing_book_spread(tmp: Path) -> None:
    """REQ-F-13a / docs/01 §3.2: the spread between legs that COEXISTED.

    Fails today three ways: `standing_spread` does not exist; `series()` takes no
    `book`; and the old path renders a NEGATIVE spread on the front page, which
    a KPI card reads as free arbitrage.
    """
    # An ask at 0.5 and a collection offer at 0.4, both standing the whole hour --
    # plus a SECOND collection offer at 0.6 alive only 10:30 -> 10:40. For those
    # ten minutes the book is crossed: the best bid is above the best ask.
    rows = [
        (DOC_LISTING, "2026-09-09T10:00:00Z", 1, {"order_hash": "0xASK", "token_id": "1",
                                                  "price_eth": 0.5, **NO_EXP}),
        (REAL_COLL_OFFER, "2026-09-09T10:00:00Z", 2, {"order_hash": "0xBID", "price_eth": 0.4, **NO_EXP}),
        (REAL_COLL_OFFER, "2026-09-09T10:30:00Z", 3, {"order_hash": "0xCROSS", "price_eth": 0.6, **NO_EXP}),
        (REAL_CANCEL, "2026-09-09T10:40:00Z", 4, {"order_hash": "0xCROSS", "token_id": None}),
    ]
    n, eng = _standing_engine(tmp, "sp1.sqlite", rows)
    s = eng.series(metric="immediacy_cost", collection="argonauts", interval="1h",
                   range_="1h", now=H11)
    check("immediacy_cost: a bucket whose standing book CROSSED at any tau is null and counted "
          "as an alarm, not charted -- ask < bid is a reconstruction defect, never an arbitrage",
          s["raw"][0] is None and s["basis"]["negative_buckets"] == 1
          and s["basis"]["book"] == "standing", f"got {s['raw']} basis={s['basis'].get('negative_buckets')}")
    old = eng.series(metric="immediacy_cost", collection="argonauts", interval="1h",
                     range_="1h", now=H11, book="observed")
    check("immediacy_cost (book='observed'): ...and this is exactly what the old path put on the "
          "front page for that bucket -- MIN(ask seen) - MAX(offer seen) = 0.5 - 0.6 = -0.1",
          abs(old["raw"][0] - (0.5 - 0.6)) < 1e-9 and old["basis"]["book"] == "observed",
          f"got {old['raw']}")
    n.close()

    # The control: the same book WITHOUT the crossing offer. The bucket has a
    # value, so the null above is caused by the crossing and not by absence.
    n2, eng2 = _standing_engine(tmp, "sp2.sqlite", rows[:2])
    ok = eng2.series(metric="immediacy_cost", collection="argonauts", interval="1h",
                     range_="1h", now=H11)
    check("immediacy_cost: the same book without the crossing offer is a real number, so the null "
          "above is the alarm firing and not an empty bucket",
          abs(ok["raw"][0] - (0.5 - 0.4)) < 1e-9 and ok["basis"]["negative_buckets"] == 0,
          f"got {ok['raw']}")
    check("immediacy_cost: coverage is BOTH-legs seconds over bucket seconds, and both legs are "
          "reported on the same tau samples",
          abs(ok["coverage"][0] - 1.0) < 1e-9 and ok["parts"]["floor_ask"][0] == 0.5
          and ok["parts"]["collection_bid"][0] == 0.4, str(ok.get("coverage")))
    check("immediacy_cost: the counts travel with the number, one per leg (project rule 4)",
          ok["n_ask"][0] == 1 and ok["n_bid"][0] == 1, str(ok.get("n_ask")))

    # The legs need never have coexisted under the old rule. Here they do not:
    # the ask is gone before the offer arrives.
    n3, eng3 = _standing_engine(tmp, "sp3.sqlite", [
        (DOC_LISTING, "2026-09-09T10:00:00Z", 1, {"order_hash": "0xA1", "token_id": "1",
                                                  "price_eth": 0.5, **NO_EXP}),
        (REAL_CANCEL, "2026-09-09T10:10:00Z", 2, {"order_hash": "0xA1", "token_id": "1"}),
        (REAL_COLL_OFFER, "2026-09-09T10:20:00Z", 3, {"order_hash": "0xB1", "price_eth": 0.4, **NO_EXP}),
    ])
    never = eng3.series(metric="immediacy_cost", collection="argonauts", interval="1h",
                        range_="1h", now=H11)
    never_old = eng3.series(metric="immediacy_cost", collection="argonauts", interval="1h",
                            range_="1h", now=H11, book="observed")
    check("immediacy_cost: legs that never coexisted produce NO spread -- the old path subtracted "
          "an ask that was cancelled at 10:10 from an offer that arrived at 10:20 and called it 0.1",
          never["raw"][0] is None and never["coverage"][0] == 0.0
          and abs(never_old["raw"][0] - 0.1) < 1e-9,
          f"standing={never['raw']} observed={never_old['raw']}")
    n2.close()
    n3.close()


def test_crossed_book_alarm_relogs_for_a_new_bucket(tmp: Path) -> None:
    """Tech-lead re-review 2026-09-10, item 5 (second half). The alarm was deduped
    on `(collection, interval, denomination)` for the LIFE OF THE PROCESS.

    The dashboard re-renders every 10 s, so some cap is needed -- an alarm that
    floods the log is an alarm nobody reads. But that key throws away the one
    thing that distinguishes a re-render from a new event. Once ANY bucket on a
    collection had crossed, every LATER crossing on that collection was swallowed:
    a dashboard left open overnight logs the 09:00 crossing and never mentions the
    14:00 one, which is the crossing that means the reconstruction broke again.
    Keying on the bucket start keeps the flood control and restores the signal.
    """
    import logging

    from navanax import metrics as M

    # Two SEPARATE crossings, four hours apart: 10:30-10:40 and 14:30-14:40.
    base = [
        (DOC_LISTING, "2026-09-09T09:00:00Z", 1, {"order_hash": "0xASK", "token_id": "1",
                                                  "price_eth": 0.5, **NO_EXP}),
        (REAL_COLL_OFFER, "2026-09-09T09:00:00Z", 2, {"order_hash": "0xBID", "price_eth": 0.4, **NO_EXP}),
        (REAL_COLL_OFFER, "2026-09-09T10:30:00Z", 3, {"order_hash": "0xX1", "price_eth": 0.6, **NO_EXP}),
        (REAL_CANCEL, "2026-09-09T10:40:00Z", 4, {"order_hash": "0xX1", "token_id": None}),
        (REAL_COLL_OFFER, "2026-09-09T14:30:00Z", 5, {"order_hash": "0xX2", "price_eth": 0.7, **NO_EXP}),
        (REAL_CANCEL, "2026-09-09T14:40:00Z", 6, {"order_hash": "0xX2", "token_id": None}),
    ]
    n, eng = _standing_engine(tmp, "relog.sqlite", base, fold_at="2026-09-09T16:00:00Z")

    records: list[logging.LogRecord] = []
    h = logging.Handler()
    h.emit = records.append  # type: ignore[assignment]
    logging.getLogger("navanax.metrics").addHandler(h)
    M._NEGATIVE_LOGGED.clear()
    noon = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc)
    try:
        def render(now):
            return eng.series(metric="immediacy_cost", collection="argonauts",
                              interval="1h", range_="6h", now=now)

        first = render(noon)
        check("crossed alarm: the 10:00 bucket crossed and is nulled and counted",
              first["basis"]["negative_buckets"] == 1, str(first["raw"]))
        after_first = len(records)
        check("crossed alarm: the first render of a crossed bucket LOGS", after_first == 1,
              f"{after_first} records")
        for _ in range(5):
            render(noon)
        check("crossed alarm: re-rendering the SAME crossed bucket stays deduped -- the dashboard "
              "redraws every 10 s and a flooded log is a log nobody reads",
              len(records) == after_first, f"{len(records)} records after 6 renders")

        second = render(later)
        check("crossed alarm: four hours on, the window holds BOTH crossed buckets -- 10:00 (already "
              "reported) and 14:00 (new)",
              second["basis"]["negative_buckets"] == 2, str(second["raw"]))
        check("crossed alarm: ...and the NEW one logs, exactly once -- the old (collection, interval, "
              "denom) key swallowed every crossing after the first for the life of the process",
              len(records) == after_first + 1, f"{len(records)} records; expected {after_first + 1}")
        msg = records[-1].getMessage()
        check("crossed alarm: the new line reports ONE newly-seen bucket and names it, so the "
              "already-reported 10:00 bucket is not re-announced alongside it",
              "1 newly-seen bucket" in msg and "14:00" in msg and "10:00" not in msg
              and "BUG-20260910-057" in msg, msg[:220])
        for _ in range(3):
            render(later)
        check("crossed alarm: the second bucket is deduped too, once it has been reported",
              len(records) == after_first + 1, f"{len(records)} records")
    finally:
        logging.getLogger("navanax.metrics").removeHandler(h)
        M._NEGATIVE_LOGGED.clear()
    n.close()


def test_audit_surfaces_crossed_books_for_every_watched_collection(tmp: Path) -> None:
    """Tech-lead re-review 2026-09-10, item 5 (first half). A crossed standing book
    was visible in exactly two places, neither of which an operator looks at: a
    `negative_buckets` integer inside the basis of the ONE collection currently
    selected on the Prices panel, and a log line. Health is the panel that exists
    to say "the record is wrong", so `/api/audit` now reports the count for every
    slug on the watchlist and puts an escalation note in `notes` when it is > 0.
    """
    import threading

    from navanax.dashboard import Dashboard
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import iso_to_ts, refresh_order_lives

    # Anchored to the real clock, because api_audit asks for "the last 24h" as of
    # now -- there is no `now` to inject, which is itself the point: this is what
    # the running dashboard computes.
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    t = lambda h, m=0: (now - timedelta(hours=h, minutes=-m)).isoformat().replace("+00:00", "Z")  # noqa: E731

    n, put = _lives_store(tmp, "audit-crossed.sqlite")
    put(DOC_LISTING, t(5), 1, order_hash="0xASK", token_id="1", price_eth=0.5, **NO_EXP)
    put(REAL_COLL_OFFER, t(5), 2, order_hash="0xBID", price_eth=0.4, **NO_EXP)
    put(REAL_COLL_OFFER, t(3), 3, order_hash="0xCROSS", price_eth=0.9, **NO_EXP)   # crosses the ask
    put(REAL_CANCEL, t(3, 20), 4, order_hash="0xCROSS", token_id=None)
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts(t(0)))

    landing = tmp / "audit-crossed-lz"
    landing.mkdir(parents=True, exist_ok=True)
    d = Dashboard.__new__(Dashboard)
    d.landing, d.norm, d.slugs = landing, n, ["argonauts"]
    d.lock = threading.Lock()           # api_audit takes it twice, sequentially, as the real one does
    d.engine = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"),
                            "America/Chicago")

    audit = d.api_audit()
    check("audit: /api/audit reports a crossed-book count for EVERY watched collection, "
          "not only the one the Prices panel happens to be showing",
          set(audit.get("crossed_book", {})) == {"argonauts"}, str(audit.get("crossed_book")))
    cb = audit["crossed_book"]["argonauts"]
    check("audit: it is the standing spread over the last 24h at 1h, with its bucket count",
          cb["interval"] == "1h" and cb["range"] == "24h" and cb["buckets"] == 24, str(cb))
    check("audit: the crossing is counted", cb["negative_buckets"] >= 1, str(cb))
    notes = [x for x in audit["notes"] if "ask < collection offer" in x]
    check("audit: ...and it is escalated in `notes`, in the words docs/05 rule 5 asks for",
          len(notes) == 1
          and notes[0].startswith(f"argonauts: {cb['negative_buckets']} bucket(s) had ask < "
                                  "collection offer in the last 24h")
          and "book reconstruction bug, escalate (docs/05 rule 5)" in notes[0],
          str(audit["notes"]))
    n.close()

    # The control: the same book with no crossing raises no note and reports zero.
    n2, put2 = _lives_store(tmp, "audit-clean.sqlite")
    put2(DOC_LISTING, t(5), 1, order_hash="0xASK", token_id="1", price_eth=0.5, **NO_EXP)
    put2(REAL_COLL_OFFER, t(5), 2, order_hash="0xBID", price_eth=0.4, **NO_EXP)
    n2.conn.commit()
    refresh_order_lives(n2.conn, now_ts=iso_to_ts(t(0)))
    d2 = Dashboard.__new__(Dashboard)
    d2.landing, d2.norm, d2.slugs = landing, n2, ["argonauts"]
    d2.lock = threading.Lock()
    d2.engine = MetricEngine(n2.conn, load_intervals(ROOT / "config" / "intervals.yaml"),
                             "America/Chicago")
    clean = d2.api_audit()
    check("audit: an uncrossed book reports 0 and adds NO note -- the alarm is not always-on",
          clean["crossed_book"]["argonauts"]["negative_buckets"] == 0
          and not [x for x in clean["notes"] if "ask < collection offer" in x],
          str(clean["crossed_book"]))
    n2.close()


def test_percentiles_are_withheld_below_min_n(tmp: Path) -> None:
    """REQ-F-19 / Q-V3: below the minimum n a percentile is REFUSED, not flagged.

    Fails today: `bid_lifetimes` returned p10/median/p90 at any n and handed the
    caller a `percentiles_reliable` flag, which `ui/index.html:215` rendered as a
    warning string next to the numbers. docs/00:199 says the system SHALL make
    quoting such a number impossible; a number on screen is a number that gets
    quoted.
    """
    from navanax.metrics import MIN_N_FOR_PERCENTILES, pct
    from navanax.normalize import iso_to_ts

    check("percentiles: the minimum is one named constant, not a literal in each caller",
          MIN_N_FOR_PERCENTILES == 30)
    sample = sorted(float(i) for i in range(MIN_N_FOR_PERCENTILES - 1))
    check("percentiles: pct() returns None one short of the minimum and a number at it",
          pct(sample, 0.5) is None and pct([*sample, 99.0], 0.5) is not None
          and pct([], 0.5) is None)

    ts = iso_to_ts
    # 29 bids, each placed and cancelled inside the window: one short of the minimum.
    rows: list[tuple] = []
    for i in range(29):
        rows.append((REAL_BID, "2026-09-09T10:00:00Z", 2 * i + 1,
                     {"order_hash": f"0xb{i}", "token_id": str(i), "price_eth": 0.1 + i / 100, **NO_EXP}))
        rows.append((REAL_CANCEL, "2026-09-09T10:00:30Z", 2 * i + 2,
                     {"order_hash": f"0xb{i}", "token_id": str(i)}))
    n, eng = _standing_engine(tmp, "pctl.sqlite", rows)
    lt = eng.bid_lifetimes("argonauts", ts("2026-09-09T09:00:00Z"), ts("2026-09-09T11:00:00Z"))
    check("percentiles: at n = 29 bid_lifetimes withholds ALL THREE -- a median is a percentile too",
          lt["n"] == 29 and lt["p10_s"] is None and lt["median_s"] is None and lt["p90_s"] is None
          and lt["percentiles_withheld"] is True, str(lt))
    check("percentiles: ...and the refusal carries its own reason -- n and the minimum, so the "
          "caller can say WHY the number is missing",
          lt["min_n_for_percentiles"] == 30 and lt["percentiles_reliable"] is False)
    n.close()

    # 30 standing asks: the standing series now has enough distinct orders for a band.
    rows30 = [(DOC_LISTING, "2026-09-09T10:00:00Z", i + 1,
               {"order_hash": f"0xa{i}", "token_id": str(i), "price_eth": 1.0 + i / 100, **NO_EXP})
              for i in range(30)]
    n2, eng2 = _standing_engine(tmp, "pctl2.sqlite", rows30)
    s = eng2.standing_series("ask", "argonauts", ts("2026-09-09T10:00:00Z"),
                             ts("2026-09-09T11:00:00Z"), "1h", "ETH", None, H11)
    check("percentiles: a standing series with 30 distinct orders reports its [p10, p90] band",
          s["n"][0] == 30 and s["p10"][0] is not None and s["p90"][0] is not None, str(s))
    n2.close()

    n3, eng3 = _standing_engine(tmp, "pctl3.sqlite", rows30[:29])
    s29 = eng3.standing_series("ask", "argonauts", ts("2026-09-09T10:00:00Z"),
                               ts("2026-09-09T11:00:00Z"), "1h", "ETH", None, H11)
    check("percentiles: one order short, the band is withheld and counted -- but the MEDIAN "
          "stands, because it is the level of the book and not a quantile of a sample",
          s29["n"][0] == 29 and s29["p10"][0] is None and s29["p90"][0] is None
          and s29["median"][0] == 1.0
          and s29["basis"]["percentiles_suppressed_buckets"] == 1, str(s29))
    n3.close()


def test_expiry_is_never_in_the_future(tmp: Path) -> None:
    """Tech-lead PR-2 review, S1: an order whose expiration has NOT arrived is
    censored (still standing), never 'expired'. Recording a future end is
    imputing the future (docs/06 §4.3); on a synthetic corpus it fabricated 5%
    of lives and drained the censored count to 1 of 46,814 on the real one."""
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, "expiry.sqlite")
    exp = "2026-09-09T11:00:00Z"
    put(REAL_BID, "2026-09-09T10:00:00Z", 1, order_hash="0xfuture", token_id="1",
        expiration_at=exp, expiration_ts=iso_to_ts(exp))
    put(REAL_BID, "2026-09-09T10:00:00Z", 2, order_hash="0xnoexp", token_id="2",
        expiration_at=None, expiration_ts=None)
    n.conn.commit()
    t_before = iso_to_ts("2026-09-09T10:30:00Z")
    refresh_order_lives(n.conn, now_ts=t_before)
    row = n.conn.execute("SELECT exit_reason, t_term FROM order_lives WHERE order_hash='0xfuture'").fetchone()
    check("expiry: before the expiration time the life is CENSORED with no t_term", row == ("censored", None), str(row))
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    lt = eng.bid_lifetimes("argonauts", iso_to_ts("2026-09-09T09:00:00Z"), t_before)
    check("expiry: bid_lifetimes counts it as censored, not ended", lt["censored_n"] == 2 and lt["n"] == 0, str(lt))
    t_after = iso_to_ts("2026-09-09T11:30:00Z")
    # incremental refresh with NO new events for this hash must still flip it
    refresh_order_lives(n.conn, hashes=["0xsomethingelse"], now_ts=t_after)
    row = n.conn.execute("SELECT exit_reason, t_term, exit_source FROM order_lives WHERE order_hash='0xfuture'").fetchone()
    check("expiry: once the expiration has passed, an incremental refresh marks it expired (derived) at expiration_ts",
          row == ("expired", iso_to_ts(exp), "derived"), str(row))
    row = n.conn.execute("SELECT exit_reason FROM order_lives WHERE order_hash='0xnoexp'").fetchone()
    check("expiry: an order with no expiration stays censored", row == ("censored",))
    n.close()


def test_orphan_open_gaps_are_closed_by_the_successor(tmp: Path) -> None:
    """Tech-lead PR-1 review, S1: a gap left open by a run that died is closed by
    the next run at the dead run's last checkpoint -- otherwise one stale open
    record nulls every bucket on every chart forever, and launchd restarts make
    that routine."""
    from navanax.landing import GapRecord, LandingZoneWriter
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import Normalizer

    root = tmp / "orphan-lz"
    store = OperationalStore(tmp / "orphan.db")
    w1 = LandingZoneWriter(root, run_id="run-dead", codec=GzipCodec())
    c1 = StreamConsumer("k", ["argonauts"], w1, store, run_id="run-dead")
    c1.record_downtime_gap()                                  # first run: checkpoint only
    gid = store.open_gap("run-dead", "connection reset", ["collection:argonauts"])
    w1.record_gap(GapRecord(started_at="2026-09-09T10:00:00Z", ended_at=None, reason="connection reset",
                            run_id="run-dead", topics=["collection:argonauts"], gap_id=gid))
    w1.close()                                                # the run dies here, gap still open
    store.save_checkpoint("opensea:stream", "run-dead", 10, "2026-09-09T10:05:00Z")
    ck = store.get_checkpoint("opensea:stream")
    check("orphan: the dead run left an open gap in register and manifest",
          len(store.open_gaps()) == 1 and len(w1.manifest.open_gaps()) == 1)

    # what the dashboard would have done with it: every bucket null, forever
    nrm = Normalizer(root, tmp / "orphan.sqlite")
    from navanax.dashboard import Dashboard
    d = Dashboard.__new__(Dashboard)
    d.landing, d.norm = root, nrm
    spans = Dashboard._gap_spans(d)
    check("orphan: before the fix an open gap masks to infinity", spans and spans[0][1] is None, str(spans))

    w2 = LandingZoneWriter(root, run_id="run-next", codec=GzipCodec())
    c2 = StreamConsumer("k", ["argonauts"], w2, store, run_id="run-next")
    c2.record_downtime_gap()
    w2.close()
    reg = store.open_gaps()
    man = w2.manifest.open_gaps()
    check("orphan: the successor closes the dead run's gap in the register AND the manifest",
          reg == [] and man == [], f"register open {len(reg)}, manifest open {len(man)}")
    with store.connect() as conn:
        row = dict(conn.execute("SELECT * FROM gap_register WHERE id=?", (gid,)).fetchone())
    check("orphan: it is closed at the dead run's last checkpoint, where the downtime gap begins",
          row["ended_at"] == ck["updated_at"], f"{row['ended_at']} vs {ck['updated_at']}")
    d._gap_cache = None
    spans = Dashboard._gap_spans(d)
    check("orphan: no span is open-ended any more; the record shows two bounded gaps back to back",
          spans and all(e is not None for _, e in spans) and len(spans) == 2, str(spans))
    closed = [g for g in json.loads((root / "_manifest" / "2026-09-09.json").read_text())["gaps"] if g.get("gap_id") == gid]
    check("orphan: the manifest record says who closed it and why", len(closed) == 1 and "closed by successor run-next" in closed[0]["reason"])
    eng = MetricEngine(nrm.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    from datetime import timedelta
    now = datetime.now(timezone.utc) + timedelta(hours=3)   # both gaps end at the successor's wall-clock start
    s = eng.series(metric="sales_count", collection="argonauts", interval="1h", range_="6h", now=now, gaps=spans)
    check("orphan: buckets after the closed gaps are defined again (0 sales while listening), not null",
          s["raw"][-1] == 0.0 and s["basis"]["gap_masked_buckets"] < len(s["raw"]), str(s["raw"]))
    nrm.close()


def test_sync_counts_unreadable_files_instead_of_reporting_zero_rows(tmp: Path) -> None:
    """BUG-058: a landing file the reader cannot decode (wrong codec on this
    machine, corrupt file) was logged and skipped, and sync() reported it as
    READ with zero rows. The orchestrator ran a corpus fold on a machine without
    zstandard and got '115 files read, 0 rows added, ALL GATES GREEN'."""
    from navanax.landing import LandingZoneWriter
    from navanax.normalize import Normalizer

    root = tmp / "badcodec-lz"
    w = LandingZoneWriter(root, "run-x", codec=GzipCodec())
    w.write(frame("argonauts", "2026-09-09T10:00:00Z"), topic="collection:argonauts", event_timestamp="2026-09-09T10:00:00Z")
    w.close()
    # corrupt the one data file in place (test fixture only; never the real landing zone)
    files = [p for p in root.rglob("*.jsonl.gz")]
    check("bug-058: fixture wrote one landing file", len(files) == 1)
    files[0].write_bytes(b"\x1f\x8bthis is not gzip data at all")
    n = Normalizer(root, tmp / "badcodec.sqlite")
    stats = n.sync()
    check("bug-058: an unreadable file is COUNTED (failed or short), never reported as read-with-zero-rows",
          (stats["files_failed"] + stats["files_short"]) == 1 and stats["rows_added"] == 0
          and stats["last_error"] and "jsonl.gz" in stats["last_error"], str(stats))
    n.close()


# ===========================================================================
# PR-5: the trait chart's metric layer (`MetricEngine.trait_set_series`).
#
# Every check in this section fails against the pre-PR-5 code, and fails for a
# reason rather than by accident: `trait_set_series` did not exist, there was no
# union bid leg (the standing legs were reachable only one at a time and
# `STANDING_NOT_OFFERED` said in as many words that the union "is PR-5"), no
# single-clause floors, no `winning_leg`, and no PARTIAL/UNKNOWN counts on a
# price response.
#
# The fixture is one collection of four tokens with two trait types, so that
# S(F_combined) is a strict subset of each single-clause set -- which is what
# makes the ordering property below have any content.
# ===========================================================================
def test_ui_trait_chart_is_the_shape_the_operator_decided() -> None:
    """PR-6. The Prices card BECOMES the trait chart under a filter, and it draws
    exactly what the Operator decided on 2026-09-10 -- no more lines, no fewer.

    Fails today: `traitChart`, `/api/trait_series`, the custom legend and the
    categorical ramp do not exist, and `prices()` drew the same three
    collection-wide series whether a trait filter was on or not -- so a trait
    floor and the collection floor were the same pixels with a different filter
    silently applied to only some of them.
    """
    import re
    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()
    script = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]

    def _obj(src: str, opener: str) -> str:
        i = src.index(opener) + len(opener)
        depth, j = 1, i
        while depth and j < len(src):
            depth += (src[j] == "{") - (src[j] == "}")
            j += 1
        return src[i:j - 1]

    check("ui/trait chart: the Prices card switches to the trait chart when traitSpec() is "
          "non-empty -- one panel, never two price panels showing different token sets",
          "if(traitSpec()){await traitChart();return}" in script
          and 'id="h-prices"' in html and 'id="tlegend"' in html)
    check("ui/trait chart: it calls the PR-5 endpoint and passes the filter, interval, range "
          "and denomination -- and nothing else, because the engine owns every rule",
          "J('/api/trait_series?'+qs({collection:p.collection,traits:p.traits,"
          "interval:p.interval,range:p.range,denom:S.denom})" in script.replace("\n", ""))
    body = _obj(script, "async function traitChart(){")
    # Line-by-line, read off the real trace literals rather than the doc comment.
    traces = re.findall(r"line:\{color:(C\.[a-z]+|CAT\[i\]),width:([0-9.]+)(,dash:'([a-z]+)')?\}", body)
    check("ui/trait chart: the baseline collection floor is --faint, DOTTED, 1.4 -- and it is "
          "the FIRST trace pushed, so it sits UNDER everything (z-order is trace order)",
          traces and traces[0] == ("C.faint", "1.4", ",dash:'dot'", "dot")
          and body.index("name:'collection floor'") < body.index("if(!multi)"), str(traces[:1]))
    single = body.split("if(!multi){", 1)[1].split("}else{", 1)[0]
    check("ui/trait chart: single clause draws EXACTLY the trait lowest ask (--ask, solid, 2.4) "
          "and the trait highest bid (--bid, solid, 2.4) over that baseline -- three lines total",
          single.count("tr.push({") == 2
          and "line:{color:C.ask,width:2.4}" in single and "line:{color:C.bid,width:2.4}" in single
          and "dash" not in single, single[:200])
    multi = body.split("}else{", 1)[1]
    check("ui/trait chart: multi-clause draws the combined (AND) floor at --ask weight 3.0 "
          "-- weight encodes ROLE (3.0 the set you asked for, 2.0 a component, 1.4 a reference)",
          "line:{color:C.ask,width:3.0}" in multi and "line:{color:CAT[i],width:2.0}" in multi)
    check("ui/trait chart: the categorical ramp is FIXED ORDER and never cycled -- a 5th clause "
          "gets no generated hue, it gets a printed notice naming how many are drawn",
          "const CAT=[C.bid,C.coll,C.trait,C.cancel], CAT_MAX=4" in script
          and "slice(0,CAT_MAX)" in multi
          and "clauses drawn individually" in multi and "CAT[i%" not in script)
    check("ui/trait chart: every series keeps its holes -- connectgaps:false on all of them, "
          "and markers stay on below ~120 observed points so 8 listings draw 8 marks",
          body.count("connectgaps:false") == body.count("tr.push({")
          and body.count("mode:mode(") >= 4, f"{body.count('connectgaps:false')} vs {body.count('tr.push({')}")
    check("ui/trait chart: the hover carries value+unit, coverage, per-leg n AND which leg set "
          "the number -- a line whose population changes and does not say so is leg-mixing",
          "cov ${(100*cov[i]).toFixed(0)}%" in script and "leg: ${b.winning_leg[i]}" in script
          and "n item ${fmt(b.n_item[i],0)}" in script)
    check("ui/trait chart: the legend is OURS (Plotly's is off) and each row carries n tokens, "
          "observed/total buckets and the last value; the bid row also carries per-leg n",
          "showlegend:false" in body and "$('#tlegend').innerHTML=lg.join('')" in body
          and "n=${fmt(n,0)} token" in script and "${obsN(a)}/${a.length} buckets" in script
          and "trait offers that COVER this filter n=${medOf(b.n_trait_offer_cover)}" in script)
    check("ui/trait chart: the trait-offer legend swatch wears the RE-PICKED --trait-offer token, "
          "read out of :root like every other mark -- no literal",
          "color:${C.trait}" in script and "trait:tok('--trait-offer')" in script)
    check("ui/trait chart: the basis prints PARTIAL / UNKNOWN / UNPARSED counts and says PARTIAL "
          "is never summed -- the panel that most needs the verdicts is the one that shows them",
          "PARTIAL · ${fmt(b.unknown_offers,0)} UNKNOWN" in script
          and "b.partial_note" in script and "d.overlap_tokens" in script)
    check("ui/trait chart: the global transform control is refused here rather than misapplied "
          "-- the endpoint returns levels, and a % change off a hole has no basis",
          "S.transform!=='ABS'" in script and "does not apply to this panel" in script
          and "const thov=()" in script)
    check("ui/trait chart: still no spline and still no smoothing anywhere on the page",
          "shape:'spline'" not in html and "connectgaps:true" not in html)


def _trait_chart_store(tmp: Path, name: str):
    """Four tokens, two clauses, one standing book. Returns (normalizer, engine).

        token  Print       Palette    standing ask   standing item bid
          1    Unclaimed   Seafoam        1.20             -
          2    Unclaimed   Ivory          0.90            0.30
          3    Claimed     Seafoam        -               0.95   <- outside S(F)
          4    Claimed     Ivory          0.70             -

    plus one collection offer at 0.348 and four trait offers: one COVERing
    `Print: Unclaimed` at 0.41, one on `Palette: Seafoam` (PARTIAL under a
    Print filter) at 0.42, one with NO criteria at 9.99 (the loud-failure
    marker -- if the guard ever fails, this is the number that appears), and one
    with numeric criteria at 8.88 (UNKNOWN).
    """
    import copy

    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, name)
    rows = {"1": ("Unclaimed", "Seafoam"), "2": ("Unclaimed", "Ivory"),
            "3": ("Claimed", "Seafoam"), "4": ("Claimed", "Ivory")}
    for tid, (pr, pa) in rows.items():
        n.conn.execute("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES (?,?,?,?)",
                       ("argonauts", tid, f"Argo #{tid}", "2026-09-09T00:00:00Z"))
        n.conn.executemany("INSERT INTO traits VALUES (?,?,?,?)",
                           [("argonauts", tid, "Print", pr), ("argonauts", tid, "Palette", pa)])
    seq = 0

    def at(raw, **over):
        nonlocal seq
        seq += 1
        put(raw, "2026-09-09T10:00:00Z", seq, **over, **NO_EXP)

    for tid, px in (("1", 1.20), ("2", 0.90), ("4", 0.70)):
        at(DOC_LISTING, order_hash=f"0xask{tid}", token_id=tid, price_eth=px)
    at(REAL_BID, order_hash="0xbid2", token_id="2", price_eth=0.30)
    at(REAL_BID, order_hash="0xbid3", token_id="3", price_eth=0.95)
    at(REAL_COLL_OFFER, order_hash="0xcoll", price_eth=0.348)

    def offer(hash_, mutate, px):
        raw = copy.deepcopy(REAL_TRAIT_OFFER)
        mutate(raw[4]["payload"])
        at(raw, order_hash=hash_, price_eth=px)

    def one(tt, tn):
        def m(p):
            p["trait_criteria"] = {"trait_type": tt, "trait_name": tn}
            p["trait_criteria_list"] = None
        return m

    def nothing(p):
        p["trait_criteria"] = None
        p["trait_criteria_list"] = None

    def numeric(p):
        p["numeric_trait_criteria_list"] = [{"trait_type": "Level", "min": 1, "max": 5}]

    offer("0xtcov", one("Print", "Unclaimed"), 0.41)
    offer("0xtpar", one("Palette", "Seafoam"), 0.42)
    offer("0xtbad", nothing, 9.99)
    offer("0xtnum", numeric, 8.88)
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts("2026-09-09T12:00:00Z"))
    return n, MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")


def _win(tmp_engine, traits, tmp=None):
    """The 09:00-11:00 window at 1 h: bucket 0 is empty, bucket 1 holds the book."""
    from navanax.normalize import iso_to_ts
    return tmp_engine.trait_set_series(
        "argonauts", traits, iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts("2026-09-09T11:00:00Z"),
        "1h", "ETH", H11)


def test_trait_set_series_bid_leg_is_a_union_that_names_its_winner(tmp: Path) -> None:
    """The Operator's single-clause chart: one bid line, three legs, each with its own n.

    Fails today: `MetricEngine.trait_set_series` does not exist. The three
    standing legs existed separately and `STANDING_NOT_OFFERED` said the union
    "is PR-5" in as many words -- there was no way to ask for the trait group's
    highest bid at all, and `top_item_bid` deliberately refused to stand in for it.
    """
    n, eng = _trait_chart_store(tmp, "traitchart1.sqlite")
    r = _win(eng, {"Print": ["Unclaimed"]})
    b = r["trait_bid"]
    check("trait chart: the bid is the MAX over the union of the three legs -- the COVERing "
          "trait offer at 0.41 beats the item bid at 0.30 and the collection offer at 0.348",
          b["median"][1] == 0.41, str(b["median"]))
    check("trait chart: `winning_leg` NAMES the leg that set the number on screen, per bucket "
          "-- a line that silently swaps population is the leg-mixing quant §1 metric 1 forbids",
          b["winning_leg"][1] == "trait_offer" and b["winning_leg"][0] is None, str(b["winning_leg"]))
    check("trait chart: each leg carries its OWN n and kind; the three are never summed",
          (b["n_item"][1], b["n_trait_offer_cover"][1], b["n_collection"][1]) == (1, 1, 1),
          f"item={b['n_item']} cover={b['n_trait_offer_cover']} coll={b['n_collection']}")
    check("trait chart: the item-bid leg is TOKEN-SCOPED -- the 0.95 bid on token 3, which the "
          "filter excludes, never reaches the line (it would have won by a wide margin)",
          b["median"][1] == 0.41 and b["n_item"][1] == 1, str(b))
    check("trait chart (BUG-051 guard, on a PRICE this time): the criteria-less trait offer at "
          "9.99 matches nothing, so the number it would have produced never appears",
          9.99 not in [v for v in b["median"] if v is not None] and r["unparsed_offers"] == 1,
          str(b["median"]))
    check("trait chart: numeric-criteria offers are UNKNOWN -- excluded AND counted, never TRUE; "
          "8.88 is not on the chart and the count is on the response",
          8.88 not in [v for v in b["median"] if v is not None] and r["unknown_offers"] == 1,
          f"unknown={r['unknown_offers']} median={b['median']}")
    check("trait chart: the ask line is the trait group's LOWEST STANDING ask over S(F)",
          r["trait_ask"]["median"][1] == 0.90 and r["trait_ask"]["median"][0] is None,
          str(r["trait_ask"]["median"]))
    check("trait chart: every series is on the FULL bucket grid with the hole preserved",
          len(r["keys"]) == 2 and len(b["median"]) == 2
          and len(r["baseline_ask"]["median"]) == 2 and r["basis"]["undefined_buckets"] == 1,
          str(r["basis"]))
    check("trait chart: the basis says which book, which legs, and how many tokens the filter picks",
          r["basis"]["book"] == "standing" and r["matching_tokens"] == 2
          and set(r["basis"]["legs"]) == {"item", "trait_offer", "collection"}, str(r["basis"]))
    n.close()


def test_trait_set_series_collection_offer_covers_every_filter(tmp: Path) -> None:
    """`C = {}` is a subset of every token's traits, so a collection offer is bid
    depth for EVERY filter -- not by a special branch, but because that is what an
    empty criteria set means (dataeng §4.3, E-V9).

    Fails today: there is no union bid leg to carry it. `collection_bid` alone was
    reachable, but nothing combined it with the other two, so "the trait group's
    highest bid" could not be asked for.
    """
    n, eng = _trait_chart_store(tmp, "traitchart2.sqlite")
    filters = [{"Print": ["Unclaimed"]}, {"Print": ["Claimed"]}, {"Palette": ["Seafoam"]},
               {"Palette": ["Ivory"]}, {"Print": ["Claimed"], "Palette": ["Ivory"]},
               {"Print": ["Unclaimed"], "Palette": ["Seafoam"]}]
    ns = [_win(eng, f)["trait_bid"]["n_collection"][1] for f in filters]
    check("trait chart: the collection offer stands in the bid leg under EVERY filter, "
          "including one whose AND-set is a single token", all(x == 1 for x in ns), str(ns))
    r = _win(eng, {"Print": ["Claimed"], "Palette": ["Ivory"]})
    b = r["trait_bid"]
    check("trait chart: where it is the only leg standing, it sets the line and is named as such",
          b["median"][1] == 0.348 and b["winning_leg"][1] == "collection"
          and (b["n_item"][1], b["n_trait_offer_cover"][1]) == (0, 0), str(b))
    n.close()


def test_trait_set_series_partial_offers_are_counted_never_summed(tmp: Path) -> None:
    """PARTIAL is a verdict, not a fraction of depth (dataeng §4.3, project rule 4).

    Fails today: no price response carried a verdict count at all. The counts
    existed only on `trait_offer_verdicts`, which the chart never called, so a
    PARTIAL offer was invisible on the panel that most needs to know about it.
    """
    n, eng = _trait_chart_store(tmp, "traitchart3.sqlite")
    r = _win(eng, {"Print": ["Unclaimed"]})
    check("trait chart: the 0.42 offer on Palette:Seafoam is PARTIAL under a Print filter and is "
          "NOT in the bid line -- it does not bid on token 2, which the filter selects",
          r["trait_bid"]["median"][1] == 0.41 and r["partial_offers"] == 1,
          f"median={r['trait_bid']['median']} partial={r['partial_offers']}")
    d = r["partial_detail"][0]
    check("trait chart: ...and it is reported with |S(F) n S(C)| AND |S(F)|, never a bare count",
          d["overlap_tokens"] == 1 and d["filter_tokens"] == 2 and d["order_hash"] == "0xtpar",
          str(d))
    check("trait chart: the basis says out loud that allocating a PARTIAL offer's quantity as "
          "depth is a JUDGEMENT and belongs in ANALYSIS, not here",
          "never summed" in r["basis"]["partial_note"].lower()
          and "judgement" in r["basis"]["partial_note"].lower(), r["basis"]["partial_note"])
    check("trait chart: UNPARSED and UNKNOWN are counted SEPARATELY -- summing a parser gap into "
          "a schema limit would retire the question of which one is growing",
          (r["unknown_offers"], r["unparsed_offers"]) == (1, 1), str(r))
    n.close()


def test_trait_set_series_empty_and_set_is_null_never_zero(tmp: Path) -> None:
    """A bucket where the AND-set has no standing ask is a HOLE.

    Two different emptinesses, and both must be null: the filter selects tokens
    but none of them is listed, and the filter selects no tokens at all. Zero is a
    price. "No standing ask" is not the price zero, and on a book with eight
    listings in forty minutes the difference is most of the chart.

    Fails today: `trait_set_series` does not exist, and the metric that stood in
    for a trait floor -- `floor_ask` with `book='observed'` -- returned the lowest
    price SEEN in the interval, so a bucket with no standing ask showed whatever
    had been briefly quoted and cancelled inside it.
    """
    n, eng = _trait_chart_store(tmp, "traitchart4.sqlite")
    r = _win(eng, {"Print": ["Claimed"], "Palette": ["Seafoam"]})     # token 3: real, unlisted
    check("trait chart: the AND-set is a real, non-empty token set with NO standing ask -- "
          "the bucket is null, not 0",
          r["matching_tokens"] == 1 and all(v is None for v in r["trait_ask"]["median"]),
          f"n={r['matching_tokens']} ask={r['trait_ask']['median']}")
    r2 = _win(eng, {"Print": ["Unclaimed"], "Palette": ["Nonesuch"]})  # no token at all
    check("trait chart: an AND-set that is EMPTY is also null, and says so with its count",
          r2["matching_tokens"] == 0 and all(v is None for v in r2["trait_ask"]["median"])
          and r2["basis"]["empty_token_set"] is True,
          f"n={r2['matching_tokens']} ask={r2['trait_ask']['median']}")
    check("trait chart: ...and the BID is null too. A collection offer is not token-scoped and "
          "covers the empty set vacuously, so without the guard the panel drew a confident bid "
          "line (0.348) for a trait group with ZERO members -- a bid on tokens this filter does "
          "not select. That is the flattering-direction failure docs/05 rule 5 is about.",
          all(v is None for v in r2["trait_bid"]["median"])
          and all(v is None for v in r2["trait_bid"]["winning_leg"]),
          str(r2["trait_bid"]["median"]))
    check("trait chart: ...and the per-leg counts still report what WAS standing, so the "
          "withheld line is visible as a withholding rather than as an empty book",
          r2["trait_bid"]["n_collection"][1] == 1, str(r2["trait_bid"]["n_collection"]))
    check("trait chart: the note names the universe the guard actually counts -- TOKENS, not "
          "traits, because a populated traits table with an empty token list lands here too (F7)",
          "TOKENS table" in r2["basis"]["empty_token_set_note"]
          and "empty `tokens` table" in r2["basis"]["empty_token_set_note"]
          and r2["basis"]["token_set_universe"] == "tokens",
          r2["basis"]["empty_token_set_note"][:160])
    check("trait chart: a filter that DOES select tokens is not caught by the guard",
          r["basis"]["empty_token_set"] is False and r["trait_bid"]["median"][1] is not None,
          str(r["basis"]["empty_token_set"]))
    check("trait chart: 0.0 appears nowhere in any price series -- not once, under either filter",
          not any(v == 0.0 for r_ in (r, r2)
                  for arr in (r_["trait_ask"]["median"], r_["trait_bid"]["median"],
                              r_["baseline_ask"]["median"])
                  for v in arr if v is not None),
          str([r["trait_ask"]["median"], r2["trait_bid"]["median"]]))
    n.close()


def test_filtered_counts_are_null_when_the_filter_selects_no_token(tmp: Path) -> None:
    """BUG-20260910-060: a bid on every token is not a bid on any token of an empty set.

    `_bucketed`'s filter clause is three disjuncts -- collection offers (no
    token, so they pass), trait offers whose criteria COVER F, and token-level
    events matching every clause. Each is right on its own. The SET of them is
    wrong when `S(F) = {}`, because `S(F) ⊆ S(C)` is vacuously true for the empty
    set, so the two token-less branches kept matching a filter that selects
    nothing. `bid_count` and `event_count` under an impossible filter counted
    bids on tokens the filter does not select.

    **This is not an edge case here, it is the default.** `traits` and `tokens`
    have 0 rows until the Explorer import lands, so EVERY filter is impossible
    and this was 100% of the filtered bid count -- the Activity chart's `bids`
    bars under any trait filter.

    Fails today by returning a number: `bid_count` = 1.0 (the collection offer)
    and `event_count` = 1.0 where both must be None, and `sales_count` = 0.0
    where None is meant -- a 0 says the trait group was quiet, and the truth is
    that there is no trait group.
    """
    n, eng = _trait_chart_store(tmp, "emptyfilter.sqlite")
    nope = {"Print": ["Nonesuch"]}

    def ser(metric, traits, **kw):
        return eng.series(metric=metric, collection="argonauts", interval="1h",
                          range_="2h", now=H11, traits=traits, **kw)

    # The COUNT/SUM metrics are the ones that leaked, and this fixture is where the
    # leak was: the collection offer and any COVERing trait offer matched a filter
    # selecting nothing. Every one of them must be null, not 0.
    for m in ("bid_count", "event_count", "sales_count", "cancel_count",
              "listing_count", "volume"):
        s = ser(m, nope)
        check(f"empty filter: {m} is null on EVERY bucket -- never 0, never a count of "
              "token-less events",
              all(v is None for v in s["raw"]) and s["basis"]["empty_token_set"] is True,
              f"{m} raw={s['raw']}")
    # F4 (tech-lead): the PRICE metrics are null on THIS fixture whether or not the
    # guard exists -- no token has `Nonesuch`, so there are no listings and no item
    # bids to find. Asserting the guard here would be an over-claim, so this fixture
    # asserts only that they are null and SAYS why, and the load-bearing case (a
    # populated `traits` with an empty token list, where the book does match the
    # filter) lives in test_empty_token_set_guard_holds_on_the_standing_branches_too.
    for m in ("floor_ask", "top_item_bid"):
        s = ser(m, nope)
        check(f"empty filter: {m} is null too -- though on THIS fixture it would be null "
              "without the guard as well (no token has the value, so there is nothing to "
              "find); the case where the guard is what makes it null is the disagreeing store",
              all(v is None for v in s["raw"]) and s["basis"]["empty_token_set"] is True,
              f"{m} raw={s['raw']}")
    s = ser("bid_count", nope)
    check("empty filter: the basis says WHY, and names the first thing to check -- the TOKENS "
          "table, which is what the guard counts (F7)",
          "there is no trait group" in s["basis"]["empty_token_set_note"]
          and "TOKENS table" in s["basis"]["empty_token_set_note"]
          and "empty `tokens` table" in s["basis"]["empty_token_set_note"]
          and s["basis"]["token_set_universe"] == "tokens",
          s["basis"]["empty_token_set_note"][:160])
    imm = ser("immediacy_cost", nope)
    check("empty filter: the derived spread is null too -- its ask leg is narrowed, so there "
          "is no ask, and a spread with one leg is not a spread",
          all(v is None for v in imm["raw"]) and imm["basis"]["empty_token_set"] is True,
          str(imm["raw"]))

    # The deliberate carve-out, unchanged: `collection_bid` is NOT narrowed by a
    # trait filter (leg discipline, quant §1 metric 1), so it still reports the
    # collection-wide offer -- and now says, in the same basis, that the filter
    # selects no token and this number is not about it.
    # BUG-060's own guard, exercised DIRECTLY (tech-lead R2): series()'s
    # blanket pass (BUG-061) is a superset, so without this call reverting the
    # _bucketed guard leaves every suite green while the fix is gone.
    from navanax.metrics import load_intervals as _li
    _iv = _li(ROOT / "config" / "intervals.yaml")["intervals"]["1h"]
    _s, _e = ser("bid_count", nope)["basis"]["range"]["start"], ser("bid_count", nope)["basis"]["range"]["end"]
    from navanax.normalize import iso_to_ts as _its
    direct = eng._bucketed("bid_count", "argonauts", "ETH", _its(_s), _its(_e), _iv, nope)
    check("empty filter: _bucketed ITSELF returns no bucket for a filter selecting no token (BUG-060 guard, "
          "independent of series()' blanket pass)", direct == {}, str(direct))
    cb = ser("collection_bid", nope, book="observed")
    check("empty filter: collection_bid is deliberately NOT narrowed and still reports the "
          "collection-wide offer, with the basis saying the number is not about the filter",
          cb["raw"][1] == 0.348 and cb["basis"]["empty_token_set"] is True
          and "deliberately NOT narrowed" in cb["basis"]["empty_token_set_note"], str(cb["raw"]))

    # ...and a filter that DOES select tokens counts exactly what it did before.
    good = {"Print": ["Unclaimed"]}
    b = ser("bid_count", good)
    check("empty filter: with a filter that selects tokens the counts are unchanged -- one item "
          "bid on a token in S(F), the collection offer, and the one COVERing trait offer = 3",
          b["raw"][1] == 3.0 and b["basis"]["empty_token_set"] is False, str(b["raw"]))
    check("empty filter: with NO filter at all the guard never fires -- 2 item bids, "
          "1 collection offer and 4 trait offers = 7",
          ser("bid_count", None)["raw"][1] == 7.0
          and ser("bid_count", None)["basis"]["empty_token_set"] is False,
          str(ser("bid_count", None)["raw"]))
    check("empty filter: a real filter still gets a real ZERO where a bucket was quiet -- the "
          "guard must not turn 'we watched and nothing happened' into a hole",
          ser("sales_count", good)["raw"] == [0.0, 0.0],
          str(ser("sales_count", good)["raw"]))
    n.close()


def test_trait_offer_verdicts_query_count_is_o_distinct_pairs(tmp: Path) -> None:
    """B1 (tech-lead, S1): the reach of a criterion is a property of the TRAIT TABLE,
    not of the offer that names it, so it must be looked up once per distinct
    `(trait_type, value)` pair -- not once per (offer, criterion).

    Measured by the tech-lead on the real shape: 27,694 traits queries for 48
    distinct pairs, 8.6 s at 200k lives with a filter selecting 500 of 9,000
    tokens at 1h/24h. This fixture is a modest synthetic -- 2,000 trait offers,
    2 criteria each, over 2,000 tokens -- and it fails today by issuing ~4,000
    traits lookups plus ~2,000 criteria queries where 40-odd of each is the work
    that actually exists.

    The query count is asserted with `sqlite3.Connection.set_trace_callback`, not
    with a stopwatch: a timing threshold on shared CI hardware is a flaky test,
    and the thing that is actually wrong is the COUNT. The wall time is printed
    beside it as evidence, with a generous ceiling that only catches a
    catastrophic regression.
    """
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import COLS, Normalizer, iso_to_ts, parse_event
    from navanax.traits import ensure_schema

    N_TOKENS, N_OFFERS, N_TYPES, N_VALUES = 2000, 2000, 4, 12
    n = Normalizer(tmp / "empty-lz", tmp / "verdictperf.sqlite")
    ensure_schema(n.conn)
    types = [f"T{i}" for i in range(N_TYPES)]
    values = [f"V{i}" for i in range(N_VALUES)]
    n.conn.executemany("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES (?,?,?,?)",
                       [("argonauts", str(t), f"#{t}", "2026-09-09T00:00:00Z") for t in range(N_TOKENS)])
    n.conn.executemany("INSERT INTO traits VALUES (?,?,?,?)",
                       [("argonauts", str(t), ty, values[(t + i) % N_VALUES])
                        for t in range(N_TOKENS) for i, ty in enumerate(types)])
    rr0 = parse_event(_env(0, REAL_TRAIT_OFFER, "2026-09-09T10:00:00Z"))
    rows, crits = [], []
    for k in range(N_OFFERS):
        rr = dict(rr0)
        rr.update(file="f", run="run-ui", seq=k, order_hash=f"0x{k:040x}",
                  valid_at="2026-09-09T10:00:00Z", valid_ts=iso_to_ts("2026-09-09T10:00:00Z"),
                  criteria_n=2, criteria_numeric_n=0, price_eth=0.4)
        rows.append(tuple(rr.get(c) for c in COLS))
        # Two criteria per offer, drawn from the same small pool of pairs, so the
        # DISTINCT pair count is tiny while the (offer x criterion) count is not.
        for i in (0, 1):
            crits.append(("run-ui", k, i, "string", types[(k + i) % N_TYPES],
                          values[(k * 7 + i) % N_VALUES], None, None))
    n.conn.executemany(f"INSERT INTO events ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})", rows)
    n.conn.executemany("INSERT INTO order_criteria VALUES (?,?,?,?,?,?,?,?)", crits)
    n.conn.commit()
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")

    seen: list[str] = []
    n.conn.set_trace_callback(seen.append)
    t0 = time.monotonic()
    v = eng.trait_offer_verdicts("argonauts", {"T0": [values[0]]},
                                 iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts("2026-09-09T11:00:00Z"))
    took = time.monotonic() - t0
    n.conn.set_trace_callback(None)
    traits_q = sum(1 for q in seen if "FROM traits" in q)
    crit_q = sum(1 for q in seen if "order_criteria" in q)
    pairs = v["distinct_criteria_pairs"]
    check("verdict perf: the traits table is queried once per DISTINCT (trait_type, value) pair, "
          "not once per (offer, criterion) -- the reach of a criterion is a property of the "
          "trait table, not of the offer that names it",
          traits_q <= pairs + 2 and pairs <= N_TYPES * N_VALUES,
          f"{traits_q} traits queries for {pairs} distinct pairs over {N_OFFERS} offers")
    check("verdict perf: the criteria rows are fetched in ONE grouped query, not one per offer",
          crit_q <= 2, f"{crit_q} order_criteria queries for {N_OFFERS} offers")
    check("verdict perf: total queries are O(distinct pairs), not O(offers x criteria) -- "
          f"under {N_OFFERS // 10} for {N_OFFERS} offers x 2 criteria",
          len(seen) < N_OFFERS // 10, f"{len(seen)} queries total, {took:.2f}s")
    check(f"verdict perf: and it runs well inside the budget ({took:.2f}s, ceiling 3.0s)",
          took < 3.0, f"{took:.2f}s")
    # The verdicts themselves must be identical to the one-query-per-lookup version.
    check("verdict perf: memoising changes the COST and not one verdict -- every offer lands in "
          "exactly one bucket and the five still sum to the offer count",
          v["covers"] + v["partial_n"] + v["disjoint"] + v["unknown_numeric"] + v["unparsed"]
          == N_OFFERS, str({k: v[k] for k in ("covers", "partial_n", "disjoint",
                                              "unknown_numeric", "unparsed")}))
    n.close()


def test_partial_detail_is_capped_but_the_count_never_is(tmp: Path) -> None:
    """F3 (tech-lead, S2): an uncapped `partial` list reached 4.1 MB on the fixture.

    The detail is a SAMPLE and the count is the answer. Capping the count instead
    of the list would make the cap silently become the answer -- the exact shape
    of BUG-047 (`requests_spent` counting the wrong thing) one module over.

    Fails today: `partial_n` was `len(partial)`, so the two could not disagree
    and there was nothing to cap.
    """
    from navanax.metrics import PARTIAL_DETAIL_CAP, MetricEngine, load_intervals
    from navanax.normalize import COLS, Normalizer, iso_to_ts, parse_event
    from navanax.traits import ensure_schema

    N_OFFERS = PARTIAL_DETAIL_CAP * 3 + 7
    n = Normalizer(tmp / "empty-lz", tmp / "partialcap.sqlite")
    ensure_schema(n.conn)
    # Two tokens, two trait types. The filter is on Print; every offer is on
    # Palette, so every one of them OVERLAPS without COVERING -> all PARTIAL.
    for tid in ("1", "2"):
        n.conn.execute("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES (?,?,?,?)",
                       ("argonauts", tid, f"#{tid}", "2026-09-09T00:00:00Z"))
        n.conn.executemany("INSERT INTO traits VALUES (?,?,?,?)",
                           [("argonauts", tid, "Print", "Unclaimed"),
                            ("argonauts", tid, "Palette", "Seafoam" if tid == "1" else "Ivory")])
    rr0 = parse_event(_env(0, REAL_TRAIT_OFFER, "2026-09-09T10:00:00Z"))
    rows, crits = [], []
    for k in range(N_OFFERS):
        rr = dict(rr0)
        rr.update(file="f", run="run-ui", seq=k, order_hash=f"0x{k:040x}",
                  valid_at="2026-09-09T10:00:00Z", valid_ts=iso_to_ts("2026-09-09T10:00:00Z"),
                  criteria_n=1, criteria_numeric_n=0, price_eth=0.4)
        rows.append(tuple(rr.get(c) for c in COLS))
        crits.append(("run-ui", k, 0, "string", "Palette", "Seafoam", None, None))
    n.conn.executemany(f"INSERT INTO events ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})", rows)
    n.conn.executemany("INSERT INTO order_criteria VALUES (?,?,?,?,?,?,?,?)", crits)
    n.conn.commit()
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    v = eng.trait_offer_verdicts("argonauts", {"Print": ["Unclaimed"]},
                                 iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts("2026-09-09T11:00:00Z"))
    check("partial cap: the DETAIL is capped so the response cannot reach megabytes",
          len(v["partial"]) == PARTIAL_DETAIL_CAP and PARTIAL_DETAIL_CAP == 50,
          f"{len(v['partial'])} entries, cap {PARTIAL_DETAIL_CAP}")
    check("partial cap: the COUNT is the true count and is computed separately -- a cap that "
          "silently becomes the answer is worse than no detail at all",
          v["partial_n"] == N_OFFERS, f"partial_n={v['partial_n']} of {N_OFFERS}")
    check("partial cap: the response SAYS it was truncated and what the cap was, so nobody "
          "reads the sample as the population",
          v["partial_truncated"] is True and v["partial_detail_cap"] == PARTIAL_DETAIL_CAP
          and "SAMPLE" in v["note"], str({k: v[k] for k in ("partial_truncated", "partial_detail_cap")}))
    check("partial cap: every capped entry still carries |S(F) n S(C)| AND |S(F)| -- the cap "
          "changes how many are shown, never what each one says (project rule 4)",
          all(d["overlap_tokens"] == 1 and d["filter_tokens"] == 2 for d in v["partial"]),
          str(v["partial"][0]))
    small = eng.trait_offer_verdicts("argonauts", {"Palette": ["Seafoam"]},
                                     iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts("2026-09-09T11:00:00Z"))
    check("partial cap: under the cap nothing is truncated and the flag says so",
          small["partial_truncated"] is False and small["partial_n"] == len(small["partial"]),
          str(small["partial_n"]))
    n.close()


def _disagreeing_store(tmp: Path, name: str):
    """A store where `traits` is populated and `tokens` is EMPTY.

    A real intermediate state of the trait onboarding, and the one that makes the
    empty-token-set guard load-bearing rather than redundant: `token_filter_sql`
    (and so `_standing_live`, and so every standing series) resolves membership
    against `traits`, while |S(F)| is counted over `tokens`. With the two
    disagreeing, the book has listings the filter matches and |S(F)| is still 0.
    """
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, name)
    n.conn.executemany("INSERT INTO traits VALUES (?,?,?,?)",
                       [("argonauts", "1", "Print", "Unclaimed"),
                        ("argonauts", "1", "Palette", "Seafoam")])
    # NOTE: no INSERT INTO tokens. That is the whole fixture.
    put(DOC_LISTING, "2026-09-09T10:00:00Z", 1, order_hash="0xask1", token_id="1",
        price_eth=1.20, **NO_EXP)
    put(REAL_BID, "2026-09-09T10:00:00Z", 2, order_hash="0xbid1", token_id="1",
        price_eth=0.30, **NO_EXP)
    put(REAL_COLL_OFFER, "2026-09-09T10:00:00Z", 3, order_hash="0xcoll", price_eth=0.348, **NO_EXP)
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts("2026-09-09T12:00:00Z"))
    return n, MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")


def test_empty_token_set_guard_holds_on_the_standing_branches_too(tmp: Path) -> None:
    """B2/F4 (tech-lead, S1): the guard was only on the `_bucketed` branch.

    `series()` has four branches — standing spread, standing series, derived, and
    `_bucketed` — and BUG-20260910-060's guard was applied to one of them. The
    other three walked straight past it while `basis.empty_token_set_note`
    asserted, in the same response, that *"every bucket of this metric is
    undefined"*. A basis that contradicts its own arrays is worse than no basis.

    It is not a theoretical gap, and this is the fixture that shows why F4's
    earlier version over-claimed: with `Print: Nonesuch` the standing ask is null
    because no token has that trait, so the guard is never what makes it null and
    the test proved nothing about the guard. Here `traits` is populated and
    `tokens` is empty — a real intermediate state of trait onboarding — so
    `_standing_live` (which resolves membership against `traits`) DOES match the
    listing while |S(F)| (counted over `tokens`) is 0. Only the guard can null it.

    Fails today: `floor_ask` returns 1.20, `immediacy_cost` returns 0.852, and
    `trait_set_series`' ask line draws, all with `empty_token_set: true` beside
    them.
    """
    from navanax.normalize import iso_to_ts

    n, eng = _disagreeing_store(tmp, "disagree.sqlite")
    f = {"Print": ["Unclaimed"]}

    def ser(metric, **kw):
        return eng.series(metric=metric, collection="argonauts", interval="1h",
                          range_="2h", now=H11, traits=f, **kw)

    check("guard/standing: the fixture really does disagree -- the traits table matches the "
          "filter and the token list is empty, so a standing series CAN find the listing",
          eng._token_set_size("argonauts", f) == 0
          and len(eng._standing_live("ask", "argonauts", "ETH",
                                     iso_to_ts("2026-09-09T09:00:00Z"),
                                     iso_to_ts("2026-09-09T11:00:00Z"), f)) == 1,
          "if either half of this fails the rest of the test proves nothing")

    for metric in ("floor_ask", "immediacy_cost"):
        s = ser(metric)                                  # standing is the default for both
        check(f"guard/standing: {metric} on the STANDING book is null on every bucket -- the "
              "guard, not the absence of data, is what makes it null",
              s["basis"]["book"] == "standing" and all(v is None for v in s["raw"])
              and s["basis"]["empty_token_set"] is True, f"{metric} raw={s['raw']}")
    imm = ser("immediacy_cost")
    check("guard/standing: the spread's LEG arrays are blanked with it -- leaving the "
          "collection-wide bid leg populated invites subtracting two legs of a metric that "
          "has just been declared meaningless",
          all(v is None for leg in imm["parts"].values() for v in leg)
          and all(v is None for v in imm["pct_of_ask"]), str(imm["parts"]))
    check("guard/standing: and so are the band, the coverage and the counts -- every one of "
          "them is a claim about a group that does not exist",
          all(v is None for k in ("p10", "p90", "coverage") for v in imm.get(k, []))
          and all(v == 0 for k in ("n_ask", "n_bid") for v in imm.get(k, [])),
          str({k: imm.get(k) for k in ("p10", "coverage", "n_ask")}))
    fa = ser("floor_ask")
    check("guard/standing: floor_ask's own band and n go with it",
          all(v is None for k in ("p10", "p90", "coverage") for v in fa.get(k, []))
          and all(v == 0 for v in fa.get("n", [])), str(fa.get("coverage")))
    check("guard/standing: the note now says the universe is TOKENS, because that is what the "
          "guard counts -- telling the Operator to check `traits` when `traits` is the "
          "populated half is the wrong instruction (F7)",
          "TOKENS table" in fa["basis"]["empty_token_set_note"]
          and fa["basis"]["token_set_universe"] == "tokens"
          and "empty `tokens` table" in fa["basis"]["empty_token_set_note"],
          fa["basis"]["empty_token_set_note"][:160])
    obs = ser("floor_ask", book="observed")
    check("guard/standing: the observed book is guarded too, on the same fixture",
          all(v is None for v in obs["raw"]) and obs["basis"]["empty_token_set"] is True,
          str(obs["raw"]))
    cb = ser("collection_bid")
    check("guard/standing: collection_bid stays the metric-level carve-out and still reports "
          "the collection-wide offer -- the guard blanks metrics the filter NARROWS",
          any(v is not None for v in cb["raw"]) and cb["basis"]["empty_token_set"] is True
          and "deliberately NOT narrowed" in cb["basis"]["empty_token_set_note"], str(cb["raw"]))

    r = eng.trait_set_series("argonauts", f, iso_to_ts("2026-09-09T09:00:00Z"),
                             iso_to_ts("2026-09-09T11:00:00Z"), "1h", "ETH", H11)
    check("guard/standing: trait_set_series nulls its ASK line too -- the comment that 'no "
          "tokens means no listings' was an assumption that tokens and traits agree, and "
          "nothing enforces that",
          r["matching_tokens"] == 0 and r["basis"]["empty_token_set"] is True
          and all(v is None for v in r["trait_ask"]["median"])
          and all(v is None for v in r["trait_ask"]["coverage"])
          and all(v == 0 for v in r["trait_ask"]["n"]), str(r["trait_ask"]["median"]))
    check("guard/standing: ...and its bid line, and the baseline is UNTOUCHED because it is "
          "not filtered at all",
          all(v is None for v in r["trait_bid"]["median"])
          and any(v is not None for v in r["baseline_ask"]["median"]),
          str(r["baseline_ask"]["median"]))
    m = eng.trait_set_series("argonauts", {"Print": ["Unclaimed"], "Palette": ["Seafoam"]},
                             iso_to_ts("2026-09-09T09:00:00Z"),
                             iso_to_ts("2026-09-09T11:00:00Z"), "1h", "ETH", H11)
    check("guard/standing: every single-clause floor is nulled too, each on ITS OWN token set",
          len(m["single_floors"]) == 2
          and all(v is None for fl in m["single_floors"] for v in fl["median"])
          and all(fl["matching_tokens"] == 0 for fl in m["single_floors"]),
          str([fl["median"] for fl in m["single_floors"]]))
    n.close()


def test_trait_series_endpoint_refuses_an_unknown_interval(tmp: Path) -> None:
    """F6 (tech-lead, S3): an unknown interval reached the page as a bare 500.

    REQ-N-09: an interval not in `config/intervals.yaml` is refused, not
    improvised. `series()` has always refused with a ValueError naming the valid
    ids, which the handler turns into a 400; `trait_set_series` indexed the dict
    directly, so a typo in a URL produced `KeyError: '5min'` and an HTTP 500 with
    a body nobody can act on.

    Fails today with KeyError, not ValueError.
    """
    import threading

    from navanax.dashboard import Dashboard

    n, eng = _trait_chart_store(tmp, "badinterval.sqlite")
    d = Dashboard.__new__(Dashboard)
    d.engine, d.slugs, d.lock = eng, ["argonauts"], threading.Lock()
    d.intervals, d.tz = eng.intervals, "America/Chicago"
    try:
        d.api_trait_series({"collection": "argonauts", "interval": "5min", "range": "24h"})
        check("trait endpoint: an unknown interval is refused", False, "no exception raised")
    except ValueError as exc:
        check("trait endpoint: an unknown interval raises ValueError -- which the handler maps "
              "to 400, not the KeyError that reached the page as a bare 500",
              "5min" in str(exc) and "1h" in str(exc), str(exc)[:140])
    except KeyError as exc:
        check("trait endpoint: an unknown interval raises ValueError, not KeyError", False,
              f"KeyError({exc}) -- this is the 500")
    # The route table maps ValueError to 400 and everything else to 500; that
    # mapping is the reason the exception TYPE is the fix.
    src = (ROOT / "src" / "navanax" / "dashboard.py").read_text()
    check("trait endpoint: the handler still maps ValueError to 400 with the message as the body",
          "except ValueError as exc:" in src and 'self._send(400, json.dumps({"error": str(exc)})' in src)
    ok = d.api_trait_series({"collection": "argonauts", "interval": "1h", "range": "24h"})
    check("trait endpoint: a known interval is unaffected", ok["basis"]["interval_id"] == "1h")
    n.close()


def test_trait_set_series_combined_floor_bounds_every_single_clause_floor(tmp: Path) -> None:
    """THE PROPERTY, and note its DIRECTION -- the intuitive one is backwards.

    `S(F_combined) = S(clause_1) n S(clause_2) n ...` is a SUBSET of each
    single-clause set. The minimum over a subset is >= the minimum over the
    superset, so at every bucket where both are defined:

        combined floor  >=  each single-clause floor  >=  ...  and the
        unfiltered baseline is <= all of them.

    A run where a single-clause floor came out ABOVE the combined floor would
    mean the AND-set is not a subset of that clause's set -- a broken token
    filter, or a trait table where one token carries two values of one type and
    the join is duplicating rather than intersecting. It would look like a
    trait premium and it would be a bug (project rule 5).

    Fails today: `single_floors` does not exist -- the multi-clause mode of the
    chart had no metric behind it at all.
    """
    n, eng = _trait_chart_store(tmp, "traitchart5.sqlite")
    r = _win(eng, {"Print": ["Unclaimed"], "Palette": ["Seafoam"]})
    combined, base = r["trait_ask"]["median"], r["baseline_ask"]["median"]
    check("trait chart: multi-clause mode returns the combined floor plus one floor per clause",
          r["basis"]["mode"] == "multi" and len(r["single_floors"]) == 2
          and {f["clause"] for f in r["single_floors"]} == {"Print", "Palette"},
          str([f["clause"] for f in r["single_floors"]]))
    check("trait chart: the fixture actually exercises the property -- the AND-set is ONE token "
          "and each clause alone selects two, so the floors are genuinely different numbers",
          r["matching_tokens"] == 1
          and [f["matching_tokens"] for f in r["single_floors"]] == [2, 2],
          str([(f["clause"], f["matching_tokens"]) for f in r["single_floors"]]))
    viol = [(f["clause"], i, f["median"][i], combined[i])
            for f in r["single_floors"] for i in range(len(combined))
            if f["median"][i] is not None and combined[i] is not None
            and f["median"][i] > combined[i] + 1e-12]
    check("trait chart: PROPERTY -- at every bucket where both are defined, the combined (AND) "
          "floor is >= every single-clause floor, because the AND-set is a subset of each",
          not viol and combined[1] == 1.20, f"violations {viol}; combined {combined}")
    low = [(i, base[i], combined[i]) for i in range(len(base))
           if base[i] is not None and combined[i] is not None and base[i] > combined[i] + 1e-12]
    check("trait chart: ...and the unfiltered baseline is at or below all of them",
          not low and base[1] == 0.70, f"violations {low}; baseline {base}")
    n.close()


def test_trait_set_series_baseline_is_present_and_unfiltered(tmp: Path) -> None:
    """The pale dotted line underneath is the COLLECTION floor, not a filtered one.

    Fails today: no response carried a filtered series and its unfiltered
    baseline together, so the page had to fire a second, differently-parameterised
    request to draw the comparison -- two windows, two `now`s, and no guarantee
    they were the same grid.
    """
    n, eng = _trait_chart_store(tmp, "traitchart6.sqlite")
    for f in ({"Print": ["Unclaimed"]}, {"Print": ["Claimed"], "Palette": ["Ivory"]}):
        r = _win(eng, f)
        b = r["baseline_ask"]
        check(f"trait chart: the baseline under {sorted(f)} is the UNFILTERED collection floor "
              "(0.70, token 4 -- which the filter may exclude entirely)",
              b["median"][1] == 0.70 and b["basis"]["kind"] == "ask", str(b["median"]))
        check("trait chart: ...on the SAME grid as the filtered series, so the two can never "
              "be drawn against different windows",
              b["keys"] == r["keys"] and r["trait_ask"]["keys"] == r["keys"],
              f"{b['keys']} vs {r['keys']}")
    n.close()


def test_trait_series_endpoint_passes_the_filter_through(tmp: Path) -> None:
    """PR-6's endpoint: `/api/trait_series`, minimal, engine-owned.

    Fails today: the route does not exist and `Dashboard.api_trait_series` is
    not defined, so the page had nothing to call.
    """
    import threading

    from navanax.dashboard import Dashboard

    n, eng = _trait_chart_store(tmp, "traitchart7.sqlite")
    d = Dashboard.__new__(Dashboard)
    d.engine, d.slugs, d.lock = eng, ["argonauts"], threading.Lock()
    d.intervals = eng.intervals
    d.tz = "America/Chicago"
    out = d.api_trait_series({"collection": "argonauts", "traits": "Print:Unclaimed",
                              "interval": "1h", "range": "24h", "denom": "ETH"})
    check("trait endpoint: it passes traits, interval, range and denomination through to the engine "
          "and adds no calculation of its own",
          out["basis"]["trait_filter"] == {"Print": ["Unclaimed"]}
          and out["basis"]["interval_id"] == "1h" and out["basis"]["denomination"] == "ETH"
          and out["matching_tokens"] == 2, str(out["basis"])[:200])
    check("trait endpoint: the response is JSON-serialisable exactly as the handler sends it",
          isinstance(json.dumps(out, default=str), str))
    src = (ROOT / "src" / "navanax" / "dashboard.py").read_text()
    check("trait endpoint: the route table names it, so the page can actually reach it",
          '"/api/trait_series": dash.api_trait_series' in src)

    # The contract between the page and the API, written down. `traitChart()` reads
    # exactly these; a rename on either side that this list does not catch shows up in
    # the browser as `undefined` in a legend or a silently missing line, which on a
    # panel full of legitimate holes is indistinguishable from a quiet market.
    multi = d.api_trait_series({"collection": "argonauts", "range": "24h", "interval": "1h",
                                "traits": "Print:Unclaimed;Palette:Seafoam"})
    missing = [k for k in ("t", "trait_ask", "trait_bid", "baseline_ask", "single_floors",
                           "matching_tokens", "partial_offers", "partial_detail",
                           "unknown_offers", "unparsed_offers", "basis") if k not in multi]
    missing += [f"trait_bid.{k}" for k in ("median", "coverage", "winning_leg", "n_item",
                                           "n_trait_offer_cover", "n_collection")
                if k not in multi["trait_bid"]]
    missing += [f"basis.{k}" for k in ("book", "mode", "clauses", "denomination", "interval_id",
                                       "buckets", "undefined_buckets", "matching_tokens",
                                       "wash_filter", "timezone", "ask_rule", "bid_rule", "legs",
                                       "partial_offers", "unknown_offers", "unparsed_offers",
                                       "partial_note", "unknown_note", "left_truncation_note",
                                       "gap_note") if k not in multi["basis"]]
    missing += [f"single_floors[].{k}" for k in ("clause", "values", "median", "coverage", "n",
                                                 "matching_tokens")
                if multi["single_floors"] and k not in multi["single_floors"][0]]
    missing += [f"{s}.{k}" for s in ("trait_ask", "baseline_ask") for k in ("median", "coverage", "n", "keys")
                if k not in multi[s]]
    check("trait endpoint: every field `traitChart()` reads is present in the response",
          not missing, ", ".join(missing))
    check("trait endpoint: a two-clause filter returns multi mode with one floor per clause",
          multi["basis"]["mode"] == "multi" and multi["basis"]["clauses"] == 2
          and len(multi["single_floors"]) == 2, str(multi["basis"]["mode"]))
    n.close()


def test_trait_set_series_ordering_survives_a_duplicated_traits_row(tmp: Path) -> None:
    """The ordering property, on the shape that would break it (tech-lead gate, PR-5).

    `test_..._combined_floor_bounds_every_single_clause_floor` names the failure
    mode -- "a `traits` table where one token carries two values of one type and
    the join is duplicating rather than intersecting" -- and then does not build
    it: every token in that fixture has exactly one value per type, so the
    duplicating join is never exercised. A property test that cannot see its own
    counterexample is a test of the happy path with a warning attached.

    A token with two values of one trait type is not a corruption. It is what a
    multi-value attribute looks like in OpenSea metadata, and it arrives the
    first time the Explorer cache is imported for a collection that has one.
    Here token 1 carries `Palette: Seafoam` AND `Palette: Ivory`, and token 2
    carries `Print: Unclaimed` AND `Print: Claimed`.

    What must hold: `S(F_combined)` is still a SUBSET of every single-clause set,
    so the combined floor stays at or ABOVE each of them and the unfiltered
    baseline stays at or below all of them. A duplicating join would inflate
    `|S(F)|`, pull a token into the AND-set that satisfies only one clause, and
    render as a trait premium (docs/05 rule 5).

    It holds because the filter is an `IN (SELECT ...)` per clause, ANDed --
    `token_filter_sql`, metrics.py:380 -- not a JOIN per clause. `IN` is a
    membership test and cannot multiply rows; `|S(F)|` is `COUNT(*)` over
    `tokens`, one row per token, so it cannot double-count either. This test
    pins that, so a future rewrite to a JOIN fails here rather than on screen.
    """
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, "traitdup.sqlite")
    rows = {"1": ("Unclaimed", "Seafoam"), "2": ("Unclaimed", "Ivory"),
            "3": ("Claimed", "Seafoam"), "4": ("Claimed", "Ivory")}
    for tid, (pr, pa) in rows.items():
        n.conn.execute("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES (?,?,?,?)",
                       ("argonauts", tid, f"Argo #{tid}", "2026-09-09T00:00:00Z"))
        n.conn.executemany("INSERT INTO traits VALUES (?,?,?,?)",
                           [("argonauts", tid, "Print", pr), ("argonauts", tid, "Palette", pa)])
    # THE FIXTURE: two tokens each carrying a SECOND value of a type they already have.
    n.conn.executemany("INSERT INTO traits VALUES (?,?,?,?)",
                       [("argonauts", "1", "Palette", "Ivory"),
                        ("argonauts", "2", "Print", "Claimed")])
    seq = 0

    def at(raw, **over):
        nonlocal seq
        seq += 1
        put(raw, "2026-09-09T10:00:00Z", seq, **over, **NO_EXP)

    for tid, px in (("1", 1.20), ("2", 0.90), ("4", 0.70)):
        at(DOC_LISTING, order_hash=f"0xask{tid}", token_id=tid, price_eth=px)
    at(REAL_COLL_OFFER, order_hash="0xcoll", price_eth=0.348)
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts("2026-09-09T12:00:00Z"))
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")

    dup = n.conn.execute("SELECT COUNT(*) FROM traits WHERE collection='argonauts' "
                         "AND token_id='1' AND trait_type='Palette'").fetchone()[0]
    check("trait chart (dup traits): the fixture really does hold two values of one type for one "
          "token -- without this the property test never sees its own counterexample", dup == 2, str(dup))

    viol, low, sizes = [], [], []
    for f in ({"Print": ["Unclaimed"], "Palette": ["Seafoam"]},
              {"Print": ["Unclaimed"], "Palette": ["Ivory"]},
              {"Print": ["Claimed"], "Palette": ["Ivory"]},
              {"Print": ["Claimed"], "Palette": ["Seafoam"]}):
        r = _win(eng, f)
        comb, base = r["trait_ask"]["median"], r["baseline_ask"]["median"]
        sizes.append((r["matching_tokens"], [s["matching_tokens"] for s in r["single_floors"]]))
        viol += [(f, s["clause"], i, s["median"][i], comb[i])
                 for s in r["single_floors"] for i in range(len(comb))
                 if s["median"][i] is not None and comb[i] is not None
                 and s["median"][i] > comb[i] + 1e-12]
        low += [(f, i, base[i], comb[i]) for i in range(len(base))
                if base[i] is not None and comb[i] is not None and base[i] > comb[i] + 1e-12]
    check("trait chart (dup traits): PROPERTY -- the combined (AND) floor is still >= every "
          "single-clause floor. A duplicating join would pull a token satisfying only ONE clause "
          "into the AND-set and render it as a trait premium (docs/05 rule 5)",
          not viol, f"violations {viol}")
    check("trait chart (dup traits): ...and the unfiltered baseline is still at or below all of them",
          not low, f"violations {low}")
    check("trait chart (dup traits): |S(F)| is never inflated by the duplicate -- the AND-set is "
          "at most as large as either clause alone, on every filter",
          all(c <= min(s) for c, s in sizes), str(sizes))
    check("trait chart (dup traits): the duplicate WIDENS the single-clause sets it belongs to "
          "(Palette:Ivory now reaches 3 tokens, Print:Claimed 3) -- the fixture is live, not inert",
          sizes[1][1] == [2, 3] and sizes[2][1] == [3, 3], str(sizes))
    n.close()


def test_merge_leg_maxima_reports_a_tie_as_every_leg_that_holds_it(tmp: Path) -> None:
    """A tie names BOTH legs. Nothing tested this (tech-lead gate, PR-5).

    `merge_leg_maxima` documents ties as its central design choice -- "two legs
    quoting the same best price is a fact about the book, and naming one of them
    would be an invention" -- and docs/08 §4a.1 promises the Operator the string
    `"item+collection"` by name. Every fixture in the suite had three legs at
    three different prices, so replacing the tie branch with `pass` (first leg
    wins by iteration order) passed the entire suite. That is a documented
    behaviour with no coverage: the leg-mixing guard would have gone silently
    one-sided, and on a book this thin two makers quoting the same round number
    is common, not exotic.

    Here the item bid and the collection offer are BOTH 0.50 and both beat the
    COVERing trait offer at 0.41.
    """
    import copy

    from navanax.metrics import MetricEngine, load_intervals, merge_leg_maxima
    from navanax.normalize import iso_to_ts, refresh_order_lives

    # -- the primitive, directly: ties, and no invented precedence -------------
    legs = {"collection": [(0.0, 10.0, 0.5)], "item": [(0.0, 10.0, 0.5)],
            "trait_offer": [(0.0, 4.0, 0.9)]}
    got = merge_leg_maxima(legs)
    check("merge_leg_maxima: while a third leg is strictly highest it alone is named",
          got[0][2] == (0.9, ("trait_offer",)), str(got))
    check("merge_leg_maxima: when it drops out, the two legs tied at the max are BOTH named, "
          "in a tuple -- not one of them picked by dict order",
          got[-1][2][0] == 0.5 and sorted(got[-1][2][1]) == ["collection", "item"], str(got))
    rev = merge_leg_maxima({k: legs[k] for k in reversed(list(legs))})
    check("merge_leg_maxima: reversing the leg order names the SAME set of legs -- a result that "
          "depends on dict iteration order is a precedence rule nobody decided",
          len(rev[-1][2][1]) == 2 and sorted(rev[-1][2][1]) == sorted(got[-1][2][1]),
          f"{rev[-1]} vs {got[-1]}")

    # -- and end to end, as `winning_leg` on the chart -------------------------
    n, put = _lives_store(tmp, "traittie.sqlite")
    for tid, pr in (("1", "Unclaimed"), ("2", "Unclaimed"), ("3", "Claimed")):
        n.conn.execute("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES (?,?,?,?)",
                       ("argonauts", tid, f"Argo #{tid}", "2026-09-09T00:00:00Z"))
        n.conn.execute("INSERT INTO traits VALUES (?,?,?,?)", ("argonauts", tid, "Print", pr))
    seq = 0

    def at(raw, **over):
        nonlocal seq
        seq += 1
        put(raw, "2026-09-09T10:00:00Z", seq, **over, **NO_EXP)

    at(DOC_LISTING, order_hash="0xask1", token_id="1", price_eth=1.20)
    at(REAL_BID, order_hash="0xbid1", token_id="1", price_eth=0.50)    # item leg
    at(REAL_COLL_OFFER, order_hash="0xcoll", price_eth=0.50)           # collection leg, SAME price
    raw = copy.deepcopy(REAL_TRAIT_OFFER)
    raw[4]["payload"]["trait_criteria"] = {"trait_type": "Print", "trait_name": "Unclaimed"}
    raw[4]["payload"]["trait_criteria_list"] = None
    at(raw, order_hash="0xtcov", price_eth=0.41)                       # COVERing, but lower
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts("2026-09-09T12:00:00Z"))
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    b = _win(eng, {"Print": ["Unclaimed"]})["trait_bid"]
    check("trait chart: two legs tied at the best bid put BOTH names on screen, joined and sorted "
          "-- docs/08 §4a.1 promises the Operator this exact string",
          b["median"][1] == 0.50 and b["winning_leg"][1] == "collection+item", str(b["winning_leg"]))
    check("trait chart: ...and each tied leg still carries its OWN n; the tie is not a merge",
          (b["n_item"][1], b["n_collection"][1], b["n_trait_offer_cover"][1]) == (1, 1, 1), str(b))
    n.close()


AS_OF = "2026-09-09T10:02:00Z"          # the fold time every survival fixture below asks about


def _survival_fixture(tmp: Path, name: str):
    """The six-life fixture the KM numbers below are hand-computed from.

    Placed so that, AT `AS_OF` = 10:02:00, the six durations are exactly
    10, 20, 30, 40, 50, 60 seconds with one of each exit and ONE censored:

        0xa  cancelled   at +10s      0xd  still standing, placed 10:01:20 -> censored at 40s
        0xb  cancelled   at +20s      0xe  invalidated at +50s
        0xc  filled      at +30s      0xf  expires     at +60s (derived, no event row)

    Six makers, six tokens, so n_eff = 6 and every life is its own episode.
    """
    from navanax.normalize import iso_to_ts, refresh_order_lives
    n, put = _survival_store(tmp, name)
    seq = 0

    def at(raw, when, **over):
        nonlocal seq
        seq += 1
        put(raw, when, seq, **over)
    at(REAL_BID, "2026-09-09T10:00:00Z", order_hash="0xa", token_id="1", maker="0xm1", price_eth=0.5, **NO_EXP)
    at(REAL_CANCEL, "2026-09-09T10:00:10Z", order_hash="0xa", token_id="1")
    at(REAL_BID, "2026-09-09T10:00:00Z", order_hash="0xb", token_id="2", maker="0xm2", price_eth=0.5, **NO_EXP)
    at(REAL_CANCEL, "2026-09-09T10:00:20Z", order_hash="0xb", token_id="2")
    at(REAL_BID, "2026-09-09T10:00:00Z", order_hash="0xc", token_id="3", maker="0xm3", price_eth=0.5, **NO_EXP)
    at(REAL_SALE, "2026-09-09T10:00:30Z", order_hash="0xc", token_id="3")
    at(REAL_BID, "2026-09-09T10:01:20Z", order_hash="0xd", token_id="4", maker="0xm4", price_eth=0.5, **NO_EXP)
    at(REAL_BID, "2026-09-09T10:00:00Z", order_hash="0xe", token_id="5", maker="0xm5", price_eth=0.5, **NO_EXP)
    at(REAL_INVALIDATE, "2026-09-09T10:00:50Z", order_hash="0xe", token_id="5")
    at(REAL_BID, "2026-09-09T10:00:00Z", order_hash="0xf", token_id="6", maker="0xm6", price_eth=0.5,
       expiration_at="2026-09-09T10:01:00Z", expiration_ts=iso_to_ts("2026-09-09T10:01:00Z"))
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts(AS_OF))
    return n


def _survival_store(tmp: Path, name: str):
    return _lives_store(tmp, name)


def _survival_engine(n):
    from navanax.metrics import MetricEngine, load_intervals
    return MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")


def test_survival_kaplan_meier_matches_a_hand_computed_fixture(tmp: Path) -> None:
    """PR-8, quant §3.2. Every number here was computed by hand before the code ran.

    Kaplan-Meier over six lives, one of them right-censored:

        t=10  n=6 d=1  S = 5/6                = 0.833333
        t=20  n=5 d=1  S = 5/6 * 4/5 = 4/6    = 0.666667
        t=30  n=4 d=1  S = 4/6 * 3/4 = 3/6    = 0.500000
        t=40             (0xd is CENSORED here: it leaves the risk set, contributes
                          no death, and the curve does not step)
        t=50  n=2 d=1  S = 1/2 * 1/2          = 0.250000
        t=60  n=1 d=1  S = 0.25 * 0/1         = 0.000000

    Greenwood at t=30:  v = 1/(6*5) + 1/(5*4) + 1/(4*3) = 1/30 + 1/20 + 1/12 = 1/6
      sigma = sqrt(1/6) / |ln 0.5| = 0.4082483 / 0.6931472 = 0.5889778
      CI95  = [0.5^exp(+1.96*sigma), 0.5^exp(-1.96*sigma)] = [0.110943, 0.803713]

    Aalen-Johansen, four causes:
      F_cancelled(60)  = 1*(1/6) + (5/6)*(1/5)            = 1/6 + 1/6 = 0.333333
      F_filled(60)     = (4/6)*(1/4)                      = 0.166667
      F_invalidated(60)= (3/6)*(1/2)                      = 0.250000
      F_expired(60)    = (1/4)*(1/1)                      = 0.250000
      SUM = 1.000000 = 1 - S(60).   1 - KM per cause would NOT sum to this.

    RMST(tau*=45) = 1*10 + (5/6)*10 + (4/6)*10 + (1/2)*15 = 10 + 8.33333 + 6.66667 + 7.5 = 32.5
      with P(not ended by 45) = S(45) = 0.5 printed beside it.
    Residual: S(50 | age 30) = S(50)/S(30) = 0.25/0.5 = 0.5.

    Every assertion fails today for the same reason: `MetricEngine.survival` does
    not exist, and `bid_lifetimes` -- the only estimator there was -- drops the
    censored life entirely and has no notion of a cause.
    """
    from navanax.normalize import iso_to_ts

    n = _survival_fixture(tmp, "km.sqlite")
    eng = _survival_engine(n)
    r = eng.survival("argonauts", iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts(AS_OF), iso_to_ts(AS_OF),
                     tau=45.0, residual_ages=[30.0], residual_horizons=[20.0])
    km = r["km"]
    want_t = [0.0, 10.0, 20.0, 30.0, 50.0, 60.0]
    want_s = [1.0, 5 / 6, 4 / 6, 3 / 6, 0.25, 0.0]
    check("survival (KM): the curve steps at the five DEATH times and not at the censoring",
          km["t"] == want_t, f"{km['t']}")
    check("survival (KM): S(t) matches the hand computation at every step, censored life included",
          all(abs(a - b) < 1e-12 for a, b in zip(km["s"], want_s, strict=True)), f"{km['s']}")
    check("survival (KM): the censored life stays in the RISK SET until it is censored -- "
          "n at t=30 is 4, which is the whole point (bid_lifetimes drops it and reports 5 ended)",
          km["n_at_risk"] == [6, 6, 5, 4, 2, 1], f"{km['n_at_risk']}")
    check("survival: n counts orders, censored_n counts the ones still standing at as_of",
          (r["n"], r["ended_n"], r["censored_n"]) == (6, 5, 1), str((r["n"], r["ended_n"], r["censored_n"])))

    i30 = km["t"].index(30.0)
    check("survival (Greenwood, log-log): the band at t=30 is the hand-computed [0.110943, 0.803713] "
          "-- NOT S +/- 1.96 SE, which leaves [0,1] in the tails",
          abs(km["greenwood_lower"][i30] - 0.110943) < 5e-6
          and abs(km["greenwood_upper"][i30] - 0.803713) < 5e-6,
          f"{km['greenwood_lower'][i30]}, {km['greenwood_upper'][i30]}")
    contained = [(t, lo, s, hi) for t, lo, s, hi in
                 zip(km["t"], km["greenwood_lower"], km["s"], km["greenwood_upper"], strict=True)
                 if lo is not None and not (lo <= s <= hi)]
    check("survival (Greenwood): the band CONTAINS S at every t where it is defined",
          not contained, str(contained))
    check("survival (Greenwood): the band is null -- never clamped -- where it is undefined "
          "(S=1 at t=0 has no log, and n=d at t=60 makes the variance infinite)",
          km["greenwood_lower"][-1] is None and km["greenwood_upper"][-1] is None,
          f"{km['greenwood_lower']}")

    bad = [(t, tot, 1 - s) for t, s, tot in
           zip(km["t"], km["s"], [sum(r["cif"][c][i] for c in r["causes"]) for i in range(len(km["t"]))],
               strict=True) if abs(tot - (1 - s)) > 1e-12]
    check("survival (Aalen-Johansen): the CIFs sum to 1 - S(t) at EVERY t -- the identity that "
          "1 - KM per cause does not have, and the reason each cause is not overstated",
          not bad, str(bad))
    last = len(km["t"]) - 1
    check("survival (AJ): each cause's incidence is the hand-computed value at t=60",
          all(abs(r["cif"][c][last] - w) < 1e-12 for c, w in
              (("cancelled", 1 / 3), ("filled", 1 / 6), ("invalidated", 0.25), ("expired", 0.25))),
          str({c: r["cif"][c][last] for c in r["causes"]}))

    check("survival (RMST): the area under the step curve to tau*=45 is 32.5 s",
          abs(r["rmst"]["rmst_s"] - 32.5) < 1e-9, str(r["rmst"]))
    check("survival (RMST): ...and P(not ended by tau*) = S(45) = 0.5 travels WITH it -- "
          "an RMST without it is half a number (quant §3.2)",
          abs(r["rmst"]["p_alive_at_tau"] - 0.5) < 1e-12, str(r["rmst"]))
    res = next(x for x in r["residual"] if x["age_s"] == 30.0 and x["horizon_s"] == 20.0)
    check("survival (residual): S(t+L | age=t) = S(50)/S(30) = 0.5",
          abs(res["p_still_standing"] - 0.5) < 1e-12, str(res))
    far = eng.survival("argonauts", iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts(AS_OF), iso_to_ts(AS_OF),
                       tau=10_000.0)
    check("survival (RMST): a tau* beyond the largest observed duration is REFUSED, not "
          "extrapolated by extending the last step flat (docs/06 §4.3: holes are never filled)",
          far["rmst"]["rmst_s"] is None and "not extrapolated" in (far["rmst"]["note"] or ""),
          str(far["rmst"]))
    n.close()


def test_survival_censors_a_life_that_ended_after_as_of(tmp: Path) -> None:
    """`exit_reason` is stored AS OF THE FOLD. The estimator must re-read it against
    the `as_of` it was asked about, or it learns the future.

    One bid placed at 10:00:00 and cancelled at 10:01:40. Folded at 10:02:00 the
    row says `cancelled`. Asked "what did this look like at 10:00:50", the life
    must be CENSORED at 50 s -- not ended at 100 s, which is a termination that
    had not happened yet.

    Fails today: nothing takes an `as_of` at all. `bid_lifetimes` reads
    `exit_reason` straight off the row.
    """
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, "asof.sqlite")
    put(REAL_BID, "2026-09-09T10:00:00Z", 1, order_hash="0xz", token_id="1", maker="0xm1", **NO_EXP)
    put(REAL_CANCEL, "2026-09-09T10:01:40Z", 2, order_hash="0xz", token_id="1")
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts(AS_OF))
    check("survival (as_of): the stored fold really does say `cancelled` -- this is the trap",
          n.conn.execute("SELECT exit_reason FROM order_lives WHERE order_hash='0xz'").fetchone()[0]
          == "cancelled")
    eng = _survival_engine(n)
    s0, e0 = iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts(AS_OF)
    early = eng.survival("argonauts", s0, e0, iso_to_ts("2026-09-09T10:00:50Z"))
    check("survival (as_of): a life with t_term > as_of is CENSORED at as_of, not ended",
          (early["ended_n"], early["censored_n"], early["ended_after_as_of_n"]) == (0, 1, 1), str(early))
    check("survival (as_of): ...and its duration is as_of - t_place = 50 s, so the curve never steps",
          early["km"]["t"] == [0.0] and early["km"]["s"] == [1.0]
          and early["strip"][0]["duration_s"] == 50.0, str(early["km"]))
    late = eng.survival("argonauts", s0, e0, e0)
    check("survival (as_of): asked at 10:02:00 the SAME row is an exit at 100 s",
          (late["ended_n"], late["censored_n"]) == (1, 0) and late["km"]["t"] == [0.0, 100.0],
          str(late["km"]))
    with_unknown = n.conn.execute("SELECT COUNT(*) FROM order_lives WHERE exit_reason='unknown'").fetchone()[0]
    check("survival (as_of): the fixture has no untimed terminator, and the estimator still "
          "reports the count rather than leaving the caller to assume zero",
          with_unknown == 0 and late["unknown_terminator_n"] == 0)
    n.close()


def test_survival_n_eff_is_maker_episodes_not_orders(tmp: Path) -> None:
    """n_eff = maker-episode clusters (quant §2.1, §3.3). One maker requoting the
    same token three times inside epsilon is ONE observation of independence, not
    three, and the band has to be built from that count.

    Six lives, six orders. Three of them are 0xbot requoting token 1 at 10 s
    intervals (epsilon = 60 s), so n_eff = 4: one bot episode plus three
    one-order episodes. Fails today: there is no episode concept anywhere.
    """
    from navanax.metrics import EPISODE_GAP_SECONDS, episode_ids
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, "eff.sqlite")
    seq = 0

    def at(raw, when, **over):
        nonlocal seq
        seq += 1
        put(raw, when, seq, **over)
    for i, when in enumerate(("2026-09-09T10:00:00Z", "2026-09-09T10:00:10Z", "2026-09-09T10:00:20Z")):
        at(REAL_BID, when, order_hash=f"0xq{i}", token_id="1", maker="0xbot", **NO_EXP)
    for i, m in enumerate(("0xp", "0xq", "0xr")):
        at(REAL_BID, "2026-09-09T10:00:00Z", order_hash=f"0xs{i}", token_id=str(i + 2), maker=m, **NO_EXP)
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts(AS_OF))
    eng = _survival_engine(n)
    r = eng.survival("argonauts", iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts(AS_OF), iso_to_ts(AS_OF))
    check("survival (n_eff): one maker requoting one token inside epsilon collapses to ONE cluster, "
          "so n_eff (4) < n (6) -- the band is built from 4, not 6",
          (r["n"], r["n_eff"]) == (6, 4), str((r["n"], r["n_eff"])))
    check("survival (n_eff): epsilon is reported with the number it produced",
          r["episode_gap_s"] == EPISODE_GAP_SECONDS and r["min_clusters"] == 30, str(r["episode_gap_s"]))
    spread = episode_ids([{"maker": "0xbot", "scope_kind": "item", "token_id": "1", "t_place": 0.0},
                          {"maker": "0xbot", "scope_kind": "item", "token_id": "1",
                           "t_place": EPISODE_GAP_SECONDS + 1.0}])
    check("survival (n_eff): the same maker on the same token a full epsilon later is a NEW episode "
          "-- a stop and a restart, not one continuous quote",
          spread[0] != spread[1], str(spread))
    same = episode_ids([{"maker": None, "scope_kind": "item", "token_id": "1", "t_place": 0.0},
                        {"maker": None, "scope_kind": "item", "token_id": "9", "t_place": 5000.0}])
    check("survival (n_eff): lives with NO maker share one cluster -- the conservative direction, "
          "because inventing independence we cannot see narrows the band",
          same[0] == same[1], str(same))
    n.close()


def test_survival_below_the_cluster_minimum_refuses_percentiles_and_strips(tmp: Path) -> None:
    """REQ-F-19 as quant §3.3 sharpens it: the binding minimum is 30 maker-episode
    CLUSTERS, not 30 orders. Below it the panel draws every observation and the
    estimator refuses every percentile -- refuses, does not flag (docs/00:199).

    Fails today: `bid_lifetimes` guards on the ORDER count, so 38,286 quotes from
    3 makers would sail past a 30-order threshold and print percentiles about
    three machines as if they were about the market.
    """
    from navanax.normalize import iso_to_ts

    n = _survival_fixture(tmp, "strip.sqlite")
    eng = _survival_engine(n)
    r = eng.survival("argonauts", iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts(AS_OF), iso_to_ts(AS_OF))
    check("survival (min n): at n_eff = 6 < 30 the mode is `strip`, not `curve`",
          r["mode"] == "strip" and r["n_eff"] == 6, str((r["mode"], r["n_eff"])))
    check("survival (min n): percentiles are None -- all three, because a median is a percentile too",
          r["percentiles"] is None and r["percentiles_withheld"] is True, str(r["percentiles"]))
    check("survival (min n): the refusal says WHY, in clusters, so the page can print the reason "
          "instead of a blank",
          "n_eff = 6" in r["percentiles_withheld_reason"] and "30" in r["percentiles_withheld_reason"],
          r["percentiles_withheld_reason"])
    strip = r["strip"]
    check("survival (min n): `strip` carries EVERY observation, one row per life, with its exit",
          strip is not None and len(strip) == 6
          and sorted(x["duration_s"] for x in strip) == [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
          str(strip and [x["duration_s"] for x in strip]))
    check("survival (min n): the censored life is labelled `censored` on the strip, never `cancelled` "
          "-- it has not exited",
          sorted(x["exit_reason"] for x in strip)
          == ["cancelled", "cancelled", "censored", "expired", "filled", "invalidated"],
          str(sorted(x["exit_reason"] for x in strip)))
    n.close()


def test_survival_cluster_bootstrap_is_deterministic_and_wider_than_greenwood(tmp: Path) -> None:
    """The band the panel draws is a maker-episode cluster bootstrap (factcheck D-W5),
    and it is reproducible: `random.Random(seed)` and nothing else.

    Fails today for the obvious reason and for a second one worth stating: the
    design proposal specified a Greenwood band, and Greenwood assumes independent
    observations. On a book quoted by three bots it is roughly sqrt(n/n_eff) too
    narrow, so shipping it would put false precision on the front page.
    """
    from navanax.metrics import cluster_bootstrap, km_curve
    from navanax.normalize import iso_to_ts

    n = _survival_fixture(tmp, "boot.sqlite")
    eng = _survival_engine(n)
    args = ("argonauts", iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts(AS_OF), iso_to_ts(AS_OF))
    a = eng.survival(*args, bootstrap_b=200)
    b = eng.survival(*args, bootstrap_b=200)
    check("survival (bootstrap): two runs at the same seed give the SAME band, to the bit",
          a["band"]["s_lower"] == b["band"]["s_lower"] and a["band"]["s_upper"] == b["band"]["s_upper"],
          str(a["band"]["s_lower"])[:120])
    c = eng.survival(*args, bootstrap_b=200, seed=1)
    check("survival (bootstrap): a different seed gives a different band -- the seed is real, "
          "not decoration",
          c["band"]["s_lower"] != a["band"]["s_lower"], str(c["band"]["s_lower"])[:120])
    check("survival (bootstrap): the band, its B and its seed are reported together",
          a["band"]["b_used"] == 200 and a["band"]["seed"] == 20260910
          and a["band"]["b_requested"] == 200, str({k: a["band"][k] for k in ("b_used", "seed", "b_requested")}))
    check("survival (bootstrap): a CIF band is produced per cause on the same grid",
          all(len(a["band"]["cif_lower"][cz]) == len(a["band"]["grid"]) for cz in a["causes"]),
          str([len(a["band"]["cif_lower"][cz]) for cz in a["causes"]]))

    # the property that makes the cluster bootstrap the right band: on data where
    # every life belongs to ONE maker-episode, resampling clusters cannot vary at
    # all, and a band of width zero is the honest answer to "how independent is this?"
    obs = [(float(i + 1), True, "cancelled") for i in range(40)]
    one = cluster_bootstrap({"one-episode": obs}, [10.0, 20.0], b=50, seed=7)
    check("survival (bootstrap): 40 orders inside ONE maker-episode resample to a band of width "
          "zero -- 40 quotes from one bot are one observation of independence, and an "
          "order-level bootstrap would have manufactured a tight, confident band instead",
          one["s_lower"] == one["s_upper"], str((one["s_lower"], one["s_upper"])))
    gw = km_curve(obs)
    check("survival (bootstrap): ...while Greenwood on the same 40 orders reports a band with "
          "real width, which is exactly the false precision D-W5 warns about",
          gw["greenwood_lower"][5] is not None and gw["greenwood_upper"][5] - gw["greenwood_lower"][5] > 0.05,
          str((gw["greenwood_lower"][5], gw["greenwood_upper"][5])))

    # The property that actually matters, and the one D-W5's "~15x too narrow"
    # estimate is about: when each cluster has its OWN characteristic lifetime --
    # the bot case, many quotes from few behaviours -- the cluster band must be
    # materially WIDER than both Greenwood and an order-level bootstrap. On
    # homogeneous clusters all three agree, which is why asserting only the
    # degenerate case above would not have caught a bootstrap that resamples the
    # wrong unit.
    import random as _rnd

    from navanax.metrics import survival_grid
    rng = _rnd.Random(11)
    het: list[tuple[float, bool, str | None]] = []
    hby: dict[str, list[tuple[float, bool, str | None]]] = {}
    for cl in range(20):
        scale = rng.uniform(2, 40)               # this cluster's own behaviour
        for _ in range(40):
            o = (round(rng.expovariate(1 / scale) + 0.5, 3), True, "cancelled")
            het.append(o)
            hby.setdefault(f"c{cl}", []).append(o)
    hkm = km_curve(het)
    hg = survival_grid(hkm["t"])
    i = len(hg) // 2
    j = hkm["t"].index(hg[i])
    gw_w = hkm["greenwood_upper"][j] - hkm["greenwood_lower"][j]
    hb = cluster_bootstrap(hby, hg, b=150, seed=5)
    cl_w = hb["s_upper"][i] - hb["s_lower"][i]
    ob = cluster_bootstrap({f"o{k}": [o] for k, o in enumerate(het)}, hg, b=150, seed=5)
    ob_w = ob["s_upper"][i] - ob["s_lower"][i]
    check("survival (bootstrap): with 800 orders from 20 clusters that each quote differently, the "
          "MAKER-EPISODE band is materially wider than Greenwood -- resampling orders instead "
          "would have shipped a band a bot's repetition made look precise (D-W5)",
          cl_w > 2 * gw_w and cl_w > 2 * ob_w,
          f"cluster {cl_w:.4f} vs greenwood {gw_w:.4f} vs order-level {ob_w:.4f}")
    n.close()


def test_survival_filters_compose(tmp: Path) -> None:
    """trait AND maker AND price band -- an intersection, not three separate views.

    Four lives over three tokens. Each filter alone selects three of them; all
    three together select exactly one. If any pair were OR-ed, or one were
    ignored, the combined count would not be 1. Fails today: no such call exists.
    """
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, "filters.sqlite")
    for tid, pr in (("1", "Unclaimed"), ("2", "Unclaimed"), ("3", "Claimed")):
        n.conn.execute("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES (?,?,?,?)",
                       ("argonauts", tid, f"Argo #{tid}", "2026-09-09T00:00:00Z"))
        n.conn.execute("INSERT INTO traits VALUES (?,?,?,?)", ("argonauts", tid, "Print", pr))
    rows = (("0xf1", "1", "0xA", 0.50), ("0xf2", "2", "0xA", 1.50),
            ("0xf3", "1", "0xB", 0.50), ("0xf4", "3", "0xA", 0.50))
    for i, (h, tid, mk, pr) in enumerate(rows):
        put(REAL_BID, "2026-09-09T10:00:00Z", i + 1, order_hash=h, token_id=tid, maker=mk,
            price_eth=pr, **NO_EXP)
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts(AS_OF))
    eng = _survival_engine(n)
    s0, e0 = iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts(AS_OF)

    def nn(**kw):
        return eng.survival("argonauts", s0, e0, e0, **kw)["n"]
    check("survival (filters): unfiltered is all four lives", nn() == 4, str(nn()))
    check("survival (filters): the trait clause alone selects the three lives on Unclaimed tokens",
          nn(traits={"Print": ["Unclaimed"]}) == 3, str(nn(traits={"Print": ["Unclaimed"]})))
    check("survival (filters): the maker clause alone selects 0xA's three lives",
          nn(maker="0xA") == 3, str(nn(maker="0xA")))
    check("survival (filters): the price band alone selects the three lives at 0.50",
          nn(price_band=(0.1, 1.0)) == 3, str(nn(price_band=(0.1, 1.0))))
    check("survival (filters): trait AND maker AND band is the INTERSECTION -- one life, not three "
          "and not seven",
          nn(traits={"Print": ["Unclaimed"]}, maker="0xA", price_band=(0.1, 1.0)) == 1,
          str(nn(traits={"Print": ["Unclaimed"]}, maker="0xA", price_band=(0.1, 1.0))))
    check("survival (filters): a filter that selects nothing returns n = 0 with the curve at 1, "
          "never an empty response the page has to guess about",
          eng.survival("argonauts", s0, e0, e0, maker="0xNOBODY")["n"] == 0)
    n.close()


def test_survival_drill_carries_both_clocks_and_the_distance_to_floor(tmp: Path) -> None:
    """The drill list (design §4.3). Both timestamps ride along deliberately:
    `observed_at - valid_at` is our stream lag, and a bid whose whole life is
    shorter than the lag was never reachable -- a fact about strategy feasibility
    that is invisible unless both clocks are on the row.

    Fails today: there is no drill endpoint and no distance-to-floor anywhere.
    """
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, "drill.sqlite")
    n.conn.execute("INSERT INTO tokens (collection, token_id, name, listed_at) VALUES (?,?,?,?)",
                   ("argonauts", "1", "Argo #1", "2026-09-09T00:00:00Z"))
    n.conn.execute("INSERT INTO traits VALUES (?,?,?,?)", ("argonauts", "1", "Print", "Unclaimed"))
    put(DOC_LISTING, "2026-09-09T09:59:00Z", 1, order_hash="0xask", token_id="1", price_eth=1.20, **NO_EXP)
    put(REAL_CANCEL, "2026-09-09T10:00:10Z", 6, order_hash="0xask", token_id="1")
    put(REAL_BID, "2026-09-09T10:00:00Z", 2, order_hash="0xb1", token_id="1", maker="0xm1", price_eth=0.90,
        observed_at="2026-09-09T10:00:02.500000Z",
        observed_ts=iso_to_ts("2026-09-09T10:00:02.500000Z"), **NO_EXP)
    put(REAL_CANCEL, "2026-09-09T10:00:06Z", 3, order_hash="0xb1", token_id="1")
    # a bid placed after the only ask was cancelled: no floor was KNOWN, so the distance is a HOLE
    put(REAL_BID, "2026-09-09T10:00:20Z", 4, order_hash="0xb2", token_id="9", maker="0xm2", price_eth=0.10, **NO_EXP)
    put(REAL_CANCEL, "2026-09-09T10:00:25Z", 5, order_hash="0xb2", token_id="9")
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts(AS_OF))
    eng = _survival_engine(n)
    s0, e0 = iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts(AS_OF)
    d = eng.survival_drill("argonauts", s0, e0, e0, 4.0, 8.0)
    check("survival (drill): the bin holds both five-second-ish lives, with a maker count for the header",
          d["total"] == 2 and d["makers"] == 2, str({k: d[k] for k in ("total", "makers")}))
    row = next(r for r in d["rows"] if r["order_hash"] == "0xb1")
    check("survival (drill): BOTH clocks are on the row, and the stream lag is the difference",
          row["placed_at_valid"].startswith("2026-09-09T10:00:00")
          and row["placed_at_observed"].startswith("2026-09-09T10:00:02.5")
          and abs(row["stream_lag_s"] - 2.5) < 1e-6, str(row))
    check("survival (drill): distance to floor is bid - the COLLECTION-wide standing ask at the "
          "instant of placement (0.90 - 1.20 = -0.30), not the ask at some other time",
          abs(row["floor_ask_at_placement_eth"] - 1.20) < 1e-9
          and abs(row["distance_to_floor_eth"] + 0.30) < 1e-9, str(row))
    check("survival (drill): the exit reason, the lifetime and the traits are on the row",
          row["exit_reason"] == "cancelled" and abs(row["lifetime_s"] - 6.0) < 1e-9
          and row["traits"] == {"Print": "Unclaimed"}, str(row))
    hole = next(r for r in d["rows"] if r["order_hash"] == "0xb2")
    check("survival (drill): with no ask standing at that instant the distance is None -- never "
          "the last floor seen, never estimated (docs/06 §4.3)",
          hole["floor_ask_at_placement_eth"] is None and hole["distance_to_floor_eth"] is None, str(hole))
    check("survival (drill): the floor basis says which floor it is, so `-0.30` cannot be read as "
          "a distance to the ask on THAT token",
          "STANDING ask at the instant of placement" in d["floor_basis"], d["floor_basis"])
    empty = eng.survival_drill("argonauts", s0, e0, e0, 1000.0, 2000.0)
    check("survival (drill): a bin with nothing in it is an empty list with its counts, not an error",
          empty["total"] == 0 and empty["rows"] == [], str(empty))
    n.close()


def test_survival_constants_cannot_drift_from_assumptions_yaml() -> None:
    """Same device as ASM-021's: the register is the artifact a reviewer trusts, so
    it must not be able to disagree with the code it describes. epsilon, B, the
    seed and the cluster minimum are all judgements two reasonable people could
    argue about, which is the test for "assumption" (docs/06 §4.1)."""
    import yaml

    from navanax.metrics import (
        EPISODE_GAP_SECONDS,
        MIN_CLUSTERS_FOR_SURVIVAL,
        SURVIVAL_BOOTSTRAP_B,
        SURVIVAL_BOOTSTRAP_SEED,
        SURVIVAL_BOOTSTRAP_WORK_CAP,
    )
    doc = yaml.safe_load((ROOT / "config" / "assumptions.yaml").read_text())
    asm = next((a for a in (doc.get("assumptions") or []) if a.get("id") == "ASM-022"), {})
    check("assumptions: ASM-022 exists and names its layer, owner, rationale and code",
          all(asm.get(k) for k in ("layer", "owner", "rationale", "code", "value")), str(sorted(asm)))
    v = asm.get("value") or {}
    check("assumptions: ASM-022's epsilon IS metrics.EPISODE_GAP_SECONDS",
          v.get("episode_gap_seconds") == EPISODE_GAP_SECONDS,
          f"{v.get('episode_gap_seconds')} vs {EPISODE_GAP_SECONDS}")
    check("assumptions: ASM-022's B, seed, work cap and cluster minimum ARE the ones the code uses",
          (v.get("bootstrap_b"), v.get("bootstrap_seed"), v.get("bootstrap_work_cap"),
           v.get("min_clusters_for_survival_percentiles"))
          == (SURVIVAL_BOOTSTRAP_B, SURVIVAL_BOOTSTRAP_SEED, SURVIVAL_BOOTSTRAP_WORK_CAP,
              MIN_CLUSTERS_FOR_SURVIVAL), str(v))
    check("assumptions: ASM-022 does NOT restate min_n_for_percentiles -- one threshold, one "
          "statement of it, or the register grows two answers to one question",
          "min_n_for_percentiles" not in v, str(sorted(v)))


def test_ui_survival_panel_is_the_shape_the_design_specifies() -> None:
    """DESIGN §4.1-4.3, as corrected by factcheck D-W4/D-W5/D-W6. The four-cell
    table (n / p10 / median / p90) is gone; the panel is a step curve with a
    cluster-bootstrap band, an exit-reason histogram, a placement-time mini-map
    that brushes, and a drill list.

    Fails today: `#life` renders a four-cell table off `/api/lifetimes`.
    """
    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()
    check("ui/survival: the four-cell lifetime table is gone",
          "/api/lifetimes" not in html and "no bid→cancel pairs in window" not in html)
    check("ui/survival: the panel reads /api/survival and /api/survival_drill",
          "/api/survival?" in html and "/api/survival_drill?" in html)
    # B4. Scoped to the ONE trace the reader sees, not to "the file contains 'hv'
    # somewhere". The band's two invisible edge traces are also 'hv'; before this
    # was scoped, switching the visible curve to a straight interpolation between
    # event times -- which is the actual lie DESIGN §4.1a forbids -- left the
    # assertion green on the band's shapes alone.
    curve = html.split("name:'still standing'", 1)[1].split("}", 1)[0] if "name:'still standing'" in html else ""
    check("ui/survival: the VISIBLE curve trace is a step (its own line.shape is 'hv'), so switching "
          "it to a straight interpolation fails here even though the band traces are still 'hv'",
          "shape:'hv'" in curve and "shape:'spline'" not in html, f"line of the curve trace: {curve[:90]!r}")
    check("ui/survival: the band is drawn as a fill, and it is the CLUSTER BOOTSTRAP band, "
          "not Greenwood (D-W5)",
          "tonexty" in html and "band.s_lower" in html and "greenwood" not in html.split("<script>")[1])
    check("ui/survival: the x-axis is logarithmic BY DEFAULT -- lifetimes span 1 s to hours",
          "SURV.log?'log':'linear'" in html and "log:true" in html)
    check("ui/survival: exit reasons are a STACKED histogram per duration bin",
          "barmode:'stack'" in html and "by_reason" in html)
    check("ui/survival: the mini-map is a separate wall-clock strip that BRUSHES the window, and "
          "the brush snaps to whole placement buckets rather than doing local->UTC arithmetic",
          "s-mini" in html and "placements" in html and "SURV.from=P.t[i]" in html
          and "type:'date'" in html)
    check("ui/survival: clicking a histogram bin opens the drill list under the card",
          "plotly_click" in html and "survDrill" in html)
    check("ui/survival: the drill row carries both clocks, the exit reason, maker, price, token, "
          "traits and the distance to floor",
          all(k in html for k in ("placed_at_valid", "placed_at_observed", "exit_reason",
                                  "distance_to_floor_eth", "stream_lag_s")))
    head = (html.split("$('#s-head').textContent=", 1)[1].split("function survBasis", 1)[0]
            if "$('#s-head').textContent=" in html else "")
    check("ui/survival: the HEADER TEMPLATE prints all three counts -- ended, still standing "
          "(censored) and n_eff -- so deleting n_eff from that one string fails here rather than "
          "being vouched for by the word appearing in a basis line further down",
          "${fmt(r.ended_n,0)} ended" in head and "still standing (censored)" in head
          and "n_eff = ${fmt(r.n_eff,0)}" in head, f"header template: {head[:150]!r}")
    check("ui/survival: below the cluster minimum the page says `strip, no curve` and draws every "
          "observation instead",
          "strip, no curve" in html and "mode==='strip'" in html)
    check("ui/survival: the filter row carries trait chips, maker, price band and the window",
          all(k in html for k in ("s-maker", "s-band", "traitSpec()", "/api/makers")))
    check("ui/survival: the direction warning travels from the estimator's basis onto the page, so "
          "a shorter median cannot be read as a bug in the new code (D-W4)",
          "basis.direction_warning" in html and "left_truncation_note" in html)


def test_docs_palette_table_matches_the_root_block() -> None:
    """Tech-lead gate on PR-8, B1/B3. `docs/08 §4b.1` carries the only table a reader
    consults for "what colour is that mark", and for a day after PR-6 it said
    `--trait-offer` `#C792EA` while `:root` said `#B266FF`. Nothing compared them.

    This parses BOTH -- the markdown table and the `@data` slice of `:root` -- and
    fails on any disagreement in either direction, including a token added to one
    and not the other. A stale hex in the documentation is worse than no hex,
    because it retires the question.
    """
    import re
    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()
    doc = (ROOT / "docs" / "08_DASHBOARD.md").read_text()
    root_body = html.split(":root{", 1)[1].split("}", 1)[0]
    data_block = root_body.split("/* @data", 1)[1].split("/* @end-tokens", 1)[0]
    # `--name:#hex;` and the one rgba token, from the CSS
    css = {m.group(1): m.group(2).upper()
           for m in re.finditer(r"--([a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6}|rgba\([^)]*\))\s*;", data_block)}
    # `| `--name` | `#hex` |` from the markdown table
    md = {m.group(1): m.group(2).upper()
          for m in re.finditer(r"\|\s*`--([a-z0-9-]+)`\s*\|\s*`(#[0-9A-Fa-f]{6}|rgba\([^)]*\))`\s*\|", doc)}
    check("docs/08: the @data table lists every token :root defines, and no others",
          set(md) == set(css), f"docs-only {sorted(set(md) - set(css))} · css-only {sorted(set(css) - set(md))}")
    wrong = {k: (md.get(k), v) for k, v in css.items() if md.get(k) != v}
    check("docs/08: every hex in the table IS the hex in :root (a stale doc hex cannot ship)",
          not wrong, "; ".join(f"--{k}: docs {a}, css {b}" for k, (a, b) in wrong.items()))
    check("docs/08: the table carries all eleven @data tokens, the three PR-8 exits included",
          len(css) == 11 and {"invalidated", "expired", "censored"} <= set(css), f"{sorted(css)}")
    # The measured figures the tech-lead required recorded, in both places.
    check("docs/08: the measured CVD/normal figures for the exit stack are recorded, not asserted",
          "16.8" in doc and "27.1" in doc and "16.2" in doc and "23.3" in doc)
    check("ui/palette: ...and the same measurements are in the :root comment beside the hexes",
          all(x in data_block for x in ("16.8", "27.1", "16.2", "23.3", "validate_palette.js")))
    check("docs/08: the failed FIRST draft is recorded too -- a palette that was chosen by eye and "
          "measured at 2.6 is the evidence that the second one was measured at all",
          "2.6" in doc and "7.7" in doc and "#F0A202" in data_block)
    check("ui/survival: stacked segments carry a 2px --surface separator (dataviz mark specs, and "
          "the secondary encoding a near-floor pair requires)",
          "line:{width:2,color:C.surface}" in html and "surface:tok('--surface')" in html)


def test_survival_drill_episode_is_a_real_cluster_id(tmp: Path) -> None:
    """N2. `survival_drill` shipped `episode: None` on every row: the id was assigned
    in `survival()` after `_survival_rows` returned, so the drill -- which calls
    `_survival_rows` directly -- never got one. A field that is always null beside a
    header that reports `n_eff` in exactly those units is worse than no field.
    """
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, "episode.sqlite")
    seq = 0

    def at(raw, when, **over):
        nonlocal seq
        seq += 1
        put(raw, when, seq, **over)
    # one maker requoting token 1 three times inside epsilon -> ONE episode
    for i, when in enumerate(("2026-09-09T10:00:00Z", "2026-09-09T10:00:10Z", "2026-09-09T10:00:20Z")):
        at(REAL_BID, when, order_hash=f"0xe{i}", token_id="1", maker="0xbot", **NO_EXP)
        at(REAL_CANCEL, "2026-09-09T10:00:30Z", order_hash=f"0xe{i}", token_id="1")
    at(REAL_BID, "2026-09-09T10:00:00Z", order_hash="0xother", token_id="2", maker="0xp", **NO_EXP)
    at(REAL_CANCEL, "2026-09-09T10:00:30Z", order_hash="0xother", token_id="2")
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts(AS_OF))
    eng = _survival_engine(n)
    s0, e0 = iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts(AS_OF)
    d = eng.survival_drill("argonauts", s0, e0, e0, 0.0, 100.0)
    eps = [r["episode"] for r in d["rows"]]
    check("survival (drill): every row carries an episode id -- none of them None",
          len(eps) == 4 and all(e for e in eps), str(eps))
    bot = {r["episode"] for r in d["rows"] if r["maker"] == "0xbot"}
    check("survival (drill): the bot's three requotes share ONE episode id, and the other maker's "
          "life has a different one -- the id is the real cluster, not a per-row placeholder",
          len(bot) == 1 and len(set(eps)) == 2, str(sorted(set(eps))))
    r = eng.survival("argonauts", s0, e0, e0)
    check("survival (drill): the distinct episode ids on the drill ARE the n_eff in the header -- "
          "the two numbers cannot drift because they come from one assignment",
          r["n_eff"] == len(set(eps)) == 2, f"n_eff {r['n_eff']} vs drill {sorted(set(eps))}")
    n.close()


def test_survival_response_stays_small_at_forty_thousand_lives(tmp: Path) -> None:
    """BUG-20260910-064. The response carried seven arrays one entry per distinct
    event time. At 40,000 lives that measured **2.29 MB** of JSON for a panel about
    1,100 px wide -- BUG-063's shape one module over, and on the panel that will
    have the most rows behind it of anything on the page.

    The curve is now thinned onto its own log-spaced grid for TRANSPORT ONLY:
    every retained point is an actual point of the estimate (indices are selected,
    never averaged), t = 0 and the final step are always kept, and the percentiles,
    RMST, residuals and bootstrap are all computed from the FULL curve before the
    thinning runs.

    The fixture writes `order_lives` rows directly rather than folding 80,000
    events, because what is under test is the response, not the normalizer.
    """
    import json
    import time as _time

    import navanax.metrics as _M
    from navanax.metrics import SURVIVAL_GRID_MAX, downsample_km, km_curve

    n, _put = _lives_store(tmp, "big.sqlite")
    cols = ("order_hash", "collection", "event_type", "scope_kind", "token_id", "maker", "quantity",
            "price_eth", "price_usd", "t_place", "t_place_observed", "t_term", "exit_reason",
            "exit_source", "exit_event_type", "expiration_ts", "placement_seen", "revalidated",
            "terminations_seen", "criteria_n", "criteria_numeric_n", "method_version")
    rng = __import__("random").Random(1)
    t0 = 1_757_000_000.0
    rows = []
    for i in range(40_000):
        tp = t0 + i * 0.5
        d = round(rng.expovariate(1 / 9.0) + 0.4, 3)
        ended = rng.random() < 0.93
        rows.append((f"0x{i:06x}", "argonauts", "item_received_bid", "item", str(i % 900),
                     f"0xm{i % 40:02d}", 1, 0.5, 900.0, tp, tp, (tp + d) if ended else None,
                     "cancelled" if ended else "censored", "observed" if ended else None,
                     None, None, 1, 0, 1 if ended else 0, None, None, 1))
    n.conn.executemany(f"INSERT INTO order_lives ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
    n.conn.commit()
    eng = _survival_engine(n)
    began = _time.time()
    r = eng.survival("argonauts", t0 - 10, t0 + 40_000, t0 + 40_000, bootstrap_b=2)
    took = _time.time() - began
    body = json.dumps(r, default=str)
    check("survival (size): the response at 40,000 lives is under 1 MB",
          len(body) < 1_000_000, f"{len(body)} bytes")
    check("survival (size): ...because the curve is thinned onto its grid -- a few hundred points "
          "standing for tens of thousands of event times, and the response says both numbers",
          r["km"]["downsampled"] is True and r["km"]["points"] <= 2 * SURVIVAL_GRID_MAX + 2
          and r["km"]["event_times"] > 10_000
          and len(r["km"]["s"]) == len(r["km"]["t"]) == r["km"]["points"],
          str({k: r["km"][k] for k in ("points", "event_times", "downsampled")}))
    check("survival (size): the CIFs are thinned onto the SAME points, so the response cannot "
          "carry a curve and an incidence on two different grids",
          all(len(r["cif"][c]) == r["km"]["points"] for c in r["causes"]),
          str([len(r["cif"][c]) for c in r["causes"]]))
    check("survival (size): the thinning is index SELECTION, not interpolation -- the last step is "
          "kept, so S still ends where the estimate ends",
          r["km"]["s"][-1] == min(r["km"]["s"]) and r["km"]["t"][0] == 0.0 and r["km"]["s"][0] == 1.0,
          f"first {r['km']['s'][:2]} last {r['km']['s'][-1]}")
    check("survival (size): percentiles come from the FULL curve, not the thinned one -- the median "
          "is not on the transport grid",
          r["percentiles"] is not None and r["percentiles"]["median_s"] not in r["km"]["t"][1:],
          str(r["percentiles"]))
    check("survival (size): and it stays inside a few seconds at that size",
          took < 5.0, f"{took:.2f}s")

    # the identity still holds on every point that survives the thinning
    bad = [i for i, s in enumerate(r["km"]["s"])
           if abs(sum(r["cif"][c][i] for c in r["causes"]) - (1 - s)) > 1e-9]
    check("survival (size): SUM_c F_c = 1 - S still holds EXACTLY at every retained point",
          not bad, str(bad[:5]))

    # the untrimmed shape, to keep the defect visible (BUG-063's discipline)
    full = km_curve([(round(rng.expovariate(1 / 9.0) + 0.4, 4), True, "cancelled") for _ in range(5_000)])
    check("survival (size): downsample_km is a no-op below the cap, so a small panel is never thinned",
          downsample_km(km_curve([(1.0, True, "cancelled"), (2.0, False, None)]))["downsampled"] is False
          and downsample_km(full)["downsampled"] is True, str(len(full["t"])))

    # the bootstrap work cap: reported, never silent. Exercised with the cap
    # temporarily lowered, because triggering it at its real value costs the
    # 2,000,000 resampled observations it exists to bound.
    cap = _M.SURVIVAL_BOOTSTRAP_WORK_CAP
    try:
        _M.SURVIVAL_BOOTSTRAP_WORK_CAP = 100
        small = _survival_fixture(tmp, "cap.sqlite")
        e2 = _survival_engine(small)
        from navanax.normalize import iso_to_ts
        c = e2.survival("argonauts", iso_to_ts("2026-09-09T09:00:00Z"), iso_to_ts(AS_OF),
                        iso_to_ts(AS_OF), bootstrap_b=1000)
        check("survival (size): a bootstrap cut by the work cap SAYS SO, with both B values and the "
              "direction of the error -- a cut that is not reported is a band that quietly means "
              "something else",
              c["band"]["b_used"] < c["band"]["b_requested"] == 1000
              and "cut from 1000" in (c["band"]["note"] or "")
              and "wider-tailed" in (c["band"]["note"] or ""), str(c["band"].get("note")))
        small.close()
    finally:
        _M.SURVIVAL_BOOTSTRAP_WORK_CAP = cap
    n.close()



# ===========================================================================
# PR-9 -- the view split: the Event Ledger, the Wallets view, Health, and the
# hash router. Every test below fails on the page and the endpoints as they
# stood before PR-9.
# ===========================================================================
def _ledger_store(tmp: Path, name: str = "ledger.sqlite"):
    """Twelve events on one collection: tokens 1..12 so #10 vs #9 is decidable,
    three makers, two sales between a pair of wallets, one un-numbered token."""
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import iso_to_ts, refresh_order_lives

    n, put = _lives_store(tmp, name)
    seq = 0

    def at(raw, minute, **over):
        nonlocal seq
        seq += 1
        put(raw, f"2026-09-09T10:{minute:02d}:00Z", seq, **over, **NO_EXP)

    # bids on tokens 1..12, alternating makers, one per minute
    for i in range(1, 13):
        at(REAL_BID, i, order_hash=f"0xbid{i:02d}", token_id=str(i),
           maker=f"0xmk{i % 3}", price_eth=0.1 * i)
    # a token whose id is not a number at all -- token_num must be NULL, not 0
    at(REAL_BID, 20, order_hash="0xbidX", token_id="argo-x", maker="0xmk0", price_eth=9.0)
    # two sales, 0xseller -> 0xbuyer, so the wallet card has counterparties
    at(DOC_LISTING, 30, order_hash="0xsold1", token_id="3", maker="0xseller",
       taker="0xbuyer", price_eth=1.5, event_type="item_sold")
    at(DOC_LISTING, 31, order_hash="0xsold2", token_id="4", maker="0xseller",
       taker="0xbuyer", price_eth=1.6, event_type="item_sold")
    # and one cancel, so 0xmk1 has an ended bid life
    at(DOC_LISTING, 35, order_hash="0xbid01", token_id="1", maker="0xmk1",
       price_eth=None, event_type="item_cancelled")
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts("2026-09-09T12:00:00Z"))
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    return n, eng, put


_LEDGER_WINDOW = ("2026-09-09T09:00:00Z", "2026-09-09T12:00:00Z")


def _lwin():
    from navanax.normalize import iso_to_ts
    return {"start": iso_to_ts(_LEDGER_WINDOW[0]), "end": iso_to_ts(_LEDGER_WINDOW[1])}


def test_ledger_keyset_pages_are_stable_under_inserts(tmp: Path) -> None:
    """design §8.1.2. Offset pagination over a table an append-only writer is
    adding 48 rows/s to skips and repeats rows: everything shifts down by however
    many arrived between page 1 and page 2. A keyset cursor is anchored to a ROW,
    so rows inserted afterwards cannot move the rows already returned.

    Fails before PR-9: `MetricEngine.ledger` does not exist.
    """
    from navanax.normalize import iso_to_ts, refresh_order_lives
    n, eng, put = _ledger_store(tmp, "ledger-keyset.sqlite")
    w = _lwin()
    p1 = eng.ledger("argonauts", sort="valid_ts", direction="desc", limit=5, **w)
    check("ledger: a page is keyset-paginated and hands back a cursor, never an offset",
          len(p1["rows"]) == 5 and p1["next_cursor"] and p1["basis"]["pagination"].startswith("keyset"),
          str({k: p1[k] for k in ("has_more", "total_estimate", "exact")}))

    # the stream keeps writing between the two requests -- five NEWER events
    for i in range(5):
        put(REAL_BID, f"2026-09-09T11:{i:02d}:00Z", 900 + i,
            order_hash=f"0xnew{i}", token_id=str(50 + i), maker="0xmk9", price_eth=5.0, **NO_EXP)
    n.conn.commit()
    refresh_order_lives(n.conn, now_ts=iso_to_ts("2026-09-09T12:00:00Z"))

    p2 = eng.ledger("argonauts", sort="valid_ts", direction="desc", limit=5,
                    cursor=p1["next_cursor"], **w)
    k = lambda r: (r["valid_at"], r["order_hash"], r["event_type"])   # noqa: E731
    first, second = [k(r) for r in p1["rows"]], [k(r) for r in p2["rows"]]
    check("ledger: page 2 repeats NOTHING from page 1 even though five newer rows landed "
          "between the two requests -- the cursor is anchored to a row, not to an offset",
          not (set(first) & set(second)), f"overlap {sorted(set(first) & set(second))}")
    check("ledger: ...and page 2 continues strictly below page 1's last key, so nothing "
          "between them was skipped either",
          all(r["valid_at"] <= p1["rows"][-1]["valid_at"] for r in p2["rows"]),
          f"{p1['rows'][-1]['valid_at']} then {[r['valid_at'] for r in p2['rows']]}")
    check("ledger: the inserted rows are NOT in page 2 -- they sort above page 1's window "
          "and a keyset page cannot reach back up",
          not any(r["order_hash"].startswith("0xnew") for r in p2["rows"]),
          str([r["order_hash"] for r in p2["rows"]]))

    # a cursor issued for one query must not be usable on another
    bad = False
    try:
        eng.ledger("argonauts", sort="token_num", direction="desc", limit=5,
                   cursor=p1["next_cursor"], **w)
    except ValueError as exc:
        bad = "different query" in str(exc)
    check("ledger: a cursor from a DIFFERENT sort or filter is refused, not silently reused "
          "-- continuing would interleave two result sets", bad)

    # walking every page visits every row exactly once
    seen, cur, guard = [], None, 0
    while guard < 50:
        guard += 1
        pg = eng.ledger("argonauts", sort="valid_ts", direction="desc", limit=4, cursor=cur, **w)
        seen += [k(r) for r in pg["rows"]]
        cur = pg["next_cursor"]
        if not cur:
            break
    check("ledger: walking the cursor to the end visits every matching row exactly once",
          len(seen) == len(set(seen)) == pg["total_estimate"],
          f"{len(seen)} rows, {len(set(seen))} distinct, total_estimate {pg['total_estimate']}")
    n.close()


def test_ledger_refuses_a_sort_it_has_no_index_for(tmp: Path) -> None:
    """design §8.1.2: "a sort on a non-indexed column is refused with a message
    naming the indexed ones -- not silently slow". A four-million-row full scan
    that eventually answers teaches the Operator the ledger is broken; a refusal
    that names five working columns teaches him what the store can do.

    Fails before PR-9: there is no ledger and no sort whitelist.
    """
    from navanax.metrics import LEDGER_SORTS, LEDGER_UNSORTABLE
    n, eng, _ = _ledger_store(tmp, "ledger-sort.sqlite")
    w = _lwin()
    msg = ""
    try:
        eng.ledger("argonauts", sort="price_usd", **w)
    except ValueError as exc:
        msg = str(exc)
    check("ledger: an unindexed sort is REFUSED, and the refusal names every column that works",
          "price_usd" in msg and all(c in msg for c in LEDGER_SORTS), msg[:200])
    check("ledger: ...and it says WHY this one cannot be served, rather than 'unsupported'",
          "rate moves" in msg, msg[:200])
    injected = ""
    try:
        eng.ledger("argonauts", sort="valid_ts; DROP TABLE events--", **w)
    except ValueError as exc:
        injected = str(exc)
    check("ledger: an arbitrary string never reaches SQL -- the whitelist refuses it first",
          "cannot sort by" in injected
          and n.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0, injected[:120])
    check("ledger: every sortable column names the index that makes it a range scan, and "
          "that index really exists in the store (the whitelist cannot drift from the schema)",
          all(n.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?",
                             (idx,)).fetchone()[0] == 1 for _, idx, _ in LEDGER_SORTS.values()),
          str([idx for _, idx, _ in LEDGER_SORTS.values()]))
    check("ledger: the columns the page shows but cannot sort are listed WITH their reason, "
          "so the UI can grey the header instead of offering a sort that 400s",
          set(LEDGER_UNSORTABLE) >= {"price_usd", "token_id", "taker", "lag"}
          and all(LEDGER_UNSORTABLE.values()), str(sorted(LEDGER_UNSORTABLE)))
    n.close()


def test_ledger_token_number_sorts_numerically(tmp: Path) -> None:
    """The Operator asked for this by name: `#10` sorts AFTER `#9`, not before it.
    `token_id` is TEXT in the store, so a lexical sort is the wrong answer that
    looks like an answer.

    Fails before PR-9: there is no `token_num` column, no index on it, and no
    ledger to sort by it.
    """
    n, eng, _ = _ledger_store(tmp, "ledger-token.sqlite")
    w = _lwin()
    r = eng.ledger("argonauts", sort="token_num", direction="asc", limit=50,
                   event_types=["item_received_bid"], **w)
    nums = [x["token_num"] for x in r["rows"] if x["token_num"] is not None]
    check("ledger: token_num ascending is 1,2,...,10,11,12 -- #10 after #9, not after #1",
          nums == sorted(nums) and nums[:12] == list(range(1, 13)), str(nums))
    ids = [x["token_id"] for x in r["rows"] if x["token_num"] is not None]
    check("ledger: ...and it is the LEXICAL order that would have been wrong, so this is a "
          "real distinction on this fixture rather than a coincidence of the ids",
          ids != sorted(ids), str(ids))
    check("ledger: a token_id that is not a plain number has token_num NULL -- never 0, which "
          "would sort it in front of token #1 as though it were one",
          any(x["token_id"] == "argo-x" and x["token_num"] is None for x in r["rows"]),
          str([(x["token_id"], x["token_num"]) for x in r["rows"]][-3:]))
    stored = n.conn.execute(
        "SELECT token_id, token_num FROM events WHERE token_id IN ('9','10','argo-x') "
        "GROUP BY token_id ORDER BY token_num").fetchall()
    check("ledger: token_num is DERIVED from token_id by the store, so the two can never "
          "disagree (it is a generated column, not a copy)",
          dict(stored) == {"9": 9, "10": 10, "argo-x": None}, str(stored))
    rng = eng.ledger("argonauts", sort="token_num", direction="asc", limit=50, token="4-6", **w)
    check("ledger: the token filter takes a range as well as an exact id",
          sorted({x["token_num"] for x in rng["rows"]}) == [4, 5, 6],
          str(sorted({x["token_num"] for x in rng["rows"]})))
    n.close()

    # -- the additive migration, on a store an earlier version built ---------
    # The precedent is `expiration_ts` (normalize._migrate): add the column, derive it
    # from one already present, never touch the landing zone. token_num is GENERATED,
    # so the migration writes no data at all -- which is why it cannot half-succeed.
    import sqlite3 as _sq
    legacy = tmp / "legacy-tokennum.sqlite"
    lc = _sq.connect(str(legacy))
    lc.executescript(
        """CREATE TABLE events (run TEXT NOT NULL, seq INTEGER NOT NULL, file TEXT,
             observed_at TEXT, valid_at TEXT, observed_ts REAL, valid_ts REAL, event_type TEXT,
             collection TEXT, token_id TEXT, order_hash TEXT, maker TEXT, taker TEXT,
             price_eth REAL, price_usd REAL, expiration_at TEXT, expiration_ts REAL,
             PRIMARY KEY (run, seq));
           INSERT INTO events (run,seq,event_type,collection,token_id,valid_ts,observed_ts)
             VALUES ('r',1,'item_listed','argonauts','9',1,1),('r',2,'item_listed','argonauts','10',2,2),
                    ('r',3,'item_listed','argonauts','argo-x',3,3);""")
    lc.commit()
    lc.close()
    from navanax.normalize import LEDGER_INDEXES, Normalizer
    ln = Normalizer(tmp / "empty-lz", legacy)
    xcols = {r[1] for r in ln.conn.execute("PRAGMA table_xinfo(events)")}
    got = dict(ln.conn.execute("SELECT token_id, token_num FROM events ORDER BY seq"))
    check("ledger/migration: _migrate adds token_num to a store an earlier version built, "
          "additively, and it is populated from token_id with no re-fold and no write",
          "token_num" in xcols and got == {"9": 9, "10": 10, "argo-x": None}, str(got))
    have = {r[0] for r in ln.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    check("ledger/migration: ...and every ledger index is built on the migrated store, so the "
          "sorts the whitelist offers are the sorts the store can actually serve",
          all(name in have for name, _, _ in LEDGER_INDEXES),
          str([nm for nm, _, _ in LEDGER_INDEXES if nm not in have]))
    ln.close()


def test_ledger_chart_states_its_cap_and_never_samples_silently(tmp: Path) -> None:
    """design §8.1.3. "Chart this selection" caps at 5,000 rows and prints
    `charting the 5,000 most recent of 41,208`. A silent sample is a chart whose
    shape is an artefact of the cap.
    """
    from navanax.metrics import LEDGER_CHART_CAP
    n, eng, _ = _ledger_store(tmp, "ledger-chart.sqlite")
    w = _lwin()
    c = eng.ledger_chart("argonauts", cap=4, **w)
    check("ledger/chart: one series per event type, never one line across types -- a listing "
          "and a bid are not two readings of one quantity",
          set(c["series"]) <= {"item_received_bid", "item_sold", "item_listed", "item_cancelled",
                               "collection_offer", "trait_offer"} and len(c["series"]) >= 1,
          str(list(c["series"])))
    check("ledger/chart: above the cap it says so, with BOTH numbers",
          c["capped"] and c["charted"] == 4 and c["matched"] > 4
          and "most recent" in (c["basis"]["cap_note"] or ""), str(c["basis"]["cap_note"]))
    full = eng.ledger_chart("argonauts", **w)
    check("ledger/chart: under the cap nothing is trimmed and cap_note is absent",
          not full["capped"] and full["basis"]["cap_note"] is None
          and full["cap"] == LEDGER_CHART_CAP, str(full["cap"]))
    check("ledger/chart: points carry their own timestamps and nothing is interpolated "
          "between two observations -- the basis says so where the reader is",
          "interpolated" in full["basis"]["hole_note"]
          and all(len(s["t"]) == len(s["price_eth"]) for s in full["series"].values()))
    n.close()


def test_wallet_card_percentages_carry_their_counts(tmp: Path) -> None:
    """Project rule 4, and design §8.2.1: "every percentage carries its count, in
    the card, not in a tooltip". `61.4%` is not a number anyone can check;
    `61.4% of 62,218` is.

    Fails before PR-9: there is no wallet endpoint, and `makers()` computed its
    share in the PAGE (`100*r.events/T`), where nothing could assert on it.
    """
    from navanax.metrics import share
    n, eng, _ = _ledger_store(tmp, "wallet-pct.sqlite")
    w = _lwin()
    lst = eng.wallets("argonauts", w["start"], w["end"], limit=20)
    bad = [r["address"] for r in lst["rows"]
           if not isinstance(r.get("events_share"), dict)
           or set(r["events_share"]) != {"pct", "n", "of"}]
    check("wallets: every share in the ranked list is {pct, n, of}, never a bare percentage",
          lst["rows"] and not bad, str(bad))
    check("wallets: the denominator is stated and is the real one",
          all(r["events_share"]["of"] == lst["total_events_with_maker"] for r in lst["rows"])
          and lst["total_events_with_maker"] > 0, str(lst["total_events_with_maker"]))

    p = eng.wallet("argonauts", "0xmk1", w["start"], w["end"])
    b = p["behaviour"]
    pcts = [(k, v) for k, v in b.items() if isinstance(v, dict) and "pct" in v]
    check("wallets/card: every percentage on the address card carries its count and its "
          "denominator -- events share, cancel ratio, fill ratio",
          len(pcts) >= 3 and all(set(v) == {"pct", "n", "of"} for _, v in pcts),
          str([k for k, _ in pcts]))
    check("wallets/card: 0 of 0 is UNDEFINED, not 0% -- a wallet that placed no bids has no "
          "fill ratio, and printing 0.0% would be a claim about a sample that does not exist",
          share(0, 0)["pct"] is None and share(3, 0)["pct"] is None
          and abs(share(3, 12)["pct"] - 25.0) < 1e-9)
    life = b["bid_life"]
    check("wallets/card: the median bid life carries BOTH n and n_eff -- one wallet's quotes "
          "are not independent observations and the uncertainty belongs to the clusters",
          "n" in life and "n_eff" in life and life["n_eff"] <= life["n"] + 1
          and "n_eff" in life["n_eff_rule"], str({k: life[k] for k in ("n", "n_eff", "median_s")}))
    check("wallets/card: percentiles are WITHHELD below the minimum n rather than printed "
          "with a warning beside them, and the response says which happened",
          (life["median_s"] is None) == life["percentiles_withheld"],
          str({k: life[k] for k in ("n", "median_s", "percentiles_withheld")}))
    seller = eng.wallet("argonauts", "0xseller", w["start"], w["end"])
    cp = seller["counterparties"]
    check("wallets/card: counterparties come from item_sold maker/taker pairs, with each "
          "counterparty's share of this address's trades and its count",
          cp["trades"] == 2 and cp["rows"] and cp["rows"][0]["address"] == "0xbuyer"
          and cp["rows"][0]["trades"] == 2
          and set(cp["rows"][0]["share_of_trades"]) == {"pct", "n", "of"}, str(cp))
    adj = eng.counterparty_adjacency("argonauts", w["start"], w["end"])
    i, j = adj["addresses"].index("0xseller"), adj["addresses"].index("0xbuyer")
    check("wallets: the counterparty graph is an ADJACENCY MATRIX whose cell states the trade "
          "count exactly -- deterministic, no physics library, no seeded layout",
          adj["matrix"][i][j] == 2 and adj["matrix"][j][i] == 0 and adj["trades"] == 2,
          str(adj["matrix"]))
    n.close()


def test_wallet_profile_has_no_field_for_an_off_chain_identity(tmp: Path) -> None:
    """The charter boundary, enforced by the SCHEMA rather than by discipline
    (.claude/agents/market-analyst.md; design §8.2.1): the wallet profile object
    has no field for a legal name, employer, company or location, so there is
    nowhere in the record for one to leak from. A reviewer checks the schema, not
    the rendering code.

    This is a PROPERTY over the whole object at any depth, not a list of the keys
    that exist today -- a future `notes` or `who_is_this` field fails here.
    """
    from navanax.metrics import WALLET_PROFILE_KEYS, _assert_address_only
    n, eng, _ = _ledger_store(tmp, "wallet-schema.sqlite")
    w = _lwin()
    p = eng.wallet("argonauts", "0xmk1", w["start"], w["end"])

    def keys(o, out=None):
        out = [] if out is None else out
        if isinstance(o, dict):
            for k, v in o.items():
                out.append(str(k))
                keys(v, out)
        elif isinstance(o, list):
            for v in o:
                keys(v, out)
        return out

    all_keys = keys(p)
    # Through the REAL matcher, not a naive substring loop: the matcher is segment
    # based on purpose (`tokens` contains `ens`), and a test that re-implements it
    # badly tests the re-implementation. `_assert_address_only` raises on the first
    # offending key and names it.
    leak = ""
    try:
        _assert_address_only(p)
    except ValueError as exc:
        leak = str(exc)
    check("wallets/schema: NO key anywhere in the profile -- at any depth -- could hold a "
          "name, company, employer, location, city, a handle or any free text that could "
          "become one",
          not leak, leak[:200])
    check("wallets/schema: ...and the walk really did look at every key, not just the top level",
          len(all_keys) > 30 and "n_eff" in all_keys, f"{len(all_keys)} keys")
    check("wallets/schema: the top-level key set is FIXED and declared, so a new field is a "
          "deliberate change to a constant rather than something that appeared",
          set(p) == set(WALLET_PROFILE_KEYS), str(sorted(set(p) ^ set(WALLET_PROFILE_KEYS))))
    refused = False
    try:
        _assert_address_only({"address": "0x1", "cluster": {"members": [{"real_name": "x"}]}})
    except ValueError as exc:
        refused = "address-level only" in str(exc) and "real" in str(exc)
    check("wallets/schema: the guard is what refuses it -- adding an identity field raises in "
          "the layer that BUILDS the profile, not in the layer that draws it", refused)
    check("wallets/schema: chain-sourced facts are marked NOT COLLECTED rather than "
          "fabricated -- null, never 0, because 0 reads as 'this address holds nothing'",
          p["chain"]["collected"] is False and p["chain"]["holdings"] is None
          and p["chain"]["first_funded_by"] is None
          and "not collected yet" in p["chain"]["why_not_collected"],
          str(p["chain"]))
    check("wallets/schema: a cluster is a hypothesis with evidence attached, so with no "
          "evidence there is no cluster -- the word never appears without its count",
          p["cluster"]["id"] is None and p["cluster"]["evidence_count"] == 0
          and "evidence" in p["cluster"]["evidence_rule"])
    check("wallets/schema: a flag renders only if its rows can be listed, and none can yet",
          p["flags"] == [] and "rows can be listed" in p["basis"]["flags_rule"])
    n.close()


def test_health_surfaces_the_alarms_nothing_else_does(tmp: Path) -> None:
    """The Health view answers "can I trust the other tabs?" -- so the three
    numbers that mean the answer is NO must be on it, in one response:

      * the crossed-book alarm count (docs/05 rule 5: a crossed standing book is
        a reconstruction defect, never an arbitrage);
      * criteria coverage (a trait-offer criterion matching no trait value reads
        as "no trait-offer depth", i.e. a quiet market, unless it is surfaced);
      * files_failed / files_short (BUG-058: "115 files read, 0 rows added" with
        no error anywhere an operator looks).

    Fails before PR-9: there is no /api/health, and the crossed-book count lived
    only in the basis of whichever collection happened to be selected.
    """
    import http.client
    import shutil as _sh
    import threading as _th
    from http.server import ThreadingHTTPServer

    from navanax.dashboard import Dashboard, make_handler

    root = tmp / "healthroot"
    (root / "config").mkdir(parents=True)
    for f in ("base.yaml", "intervals.yaml", "watchlist.yaml"):
        _sh.copy(ROOT / "config" / f, root / "config" / f)
    import yaml
    cfg = yaml.safe_load((root / "config" / "base.yaml").read_text())
    clock = FakeClock(datetime(2026, 9, 9, 10, 20, 0, tzinfo=timezone.utc))
    w = LandingZoneWriter(root / cfg["landing"]["root"], "run-h", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic, auto_flush=False)
    for raw in (REAL_BID, REAL_COLL_OFFER, DOC_LISTING):
        w.write(json.dumps(raw), topic="collection:argonauts",
                event_timestamp=raw[4]["payload"]["event_timestamp"])
    w.close()
    dash = Dashboard(root, cfg, ["argonauts"])
    dash._sync_once()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dash))
    port = httpd.server_address[1]
    t = _th.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", path)
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, body

        st, body = get("/api/health")
        h = json.loads(body) if st == 200 else {}
        check("health: /api/health answers 200 with one object, not four requests the view "
              "could render three of", st == 200 and isinstance(h, dict), f"{st} {body[:120]}")
        check("health: the crossed-book alarm is a COUNT on this page, for every watched "
              "collection -- not a field in the basis of whichever chart is on screen",
              "crossed_book" in h and "alarms" in h["crossed_book"]
              and set(h["crossed_book"]["by_collection"]) == {"argonauts"}
              and "never an arbitrage" in h["crossed_book"]["note"],
              str(h.get("crossed_book"))[:200])
        check("health: criteria coverage is here, because a criterion that matches no trait "
              "value reads as a quiet market rather than as a broken join",
              "criteria_coverage" in h and "argonauts" in h["criteria_coverage"],
              str(list(h.get("criteria_coverage", {}))))
        nz = h.get("normalizer", {})
        check("health: files_failed AND files_short are on the TOP level of the normalizer "
              "block, not three keys deep in a status blob (BUG-058)",
              "files_failed" in nz and "files_short" in nz and nz["files_failed"] == 0
              and nz["files_short"] == 0 and "BUG-058" in nz["files_note"], str(nz)[:220])
        check("health: recorder state, gaps open and awaiting backfill",
              "running" in h["recorder"] and "open" in h["gaps"]
              and "awaiting_backfill" in h["gaps"], str(h.get("gaps")))
        check("health: the integrity audit travels with it -- failures AND notes",
              "failures" in h["integrity"] and "notes" in h["integrity"])
        check("health: store size on disk and the row counts behind every other tab",
              h["store"]["bytes"] > 0 and h["store"]["events"] == 3
              and "order_lives" in h["store"] and "landing_bytes" in h["store"], str(h["store"])[:200])
        check("health: the last fold time, so a page that has stopped updating says so",
              nz.get("last_fold_at") and "refresh_seconds" in nz, str(nz.get("last_fold_at")))

        # the ledger and wallet routes answer over real HTTP too, and refuse properly
        for path in ("/api/ledger?collection=argonauts&range=YTD",
                     "/api/ledger?collection=argonauts&range=YTD&mode=chart",
                     "/api/wallets?collection=argonauts&range=YTD",
                     "/api/wallet/0x0d9ec524ed52f109c530a28c91fe190c44c0babd?collection=argonauts&range=YTD"):
            st, body = get(path)
            check(f"health/routes: {path.split('?')[0]} -> 200 JSON",
                  st == 200 and body[:1] == b"{", f"{st} {body[:120]}")
        st, body = get("/api/ledger?collection=argonauts&range=YTD&sort=price_usd")
        check("health/routes: /api/ledger refuses an unindexed sort with a 400 naming the "
              "columns that work, rather than a full scan that eventually answers",
              st == 400 and b"price_usd" in body and b"valid_ts" in body, f"{st} {body[:200]}")
        st, body = get("/api/ledger?collection=argonauts&range=YTD&token=nope")
        check("health/routes: a token filter it cannot read is a 400, never a dropped filter "
              "-- a dropped filter returns more rows than were asked for",
              st == 400 and b"token" in body, f"{st} {body[:160]}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        dash.norm.close()


def test_ui_router_keeps_every_panel_and_adds_the_views(tmp: Path) -> None:
    """PR-9's whole risk in one test. Splitting one page into views is a MOVE, and
    a move loses things: the failure mode is a panel that is still in the code, no
    longer in any view, and therefore never drawn again while every one of its own
    tests stays green because they read the file rather than the layout.

    So: every view id exists, and every panel that existed before PR-9 still
    exists EXACTLY ONCE and is inside exactly one view.
    """
    import re
    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()

    views = ["market", "traits", "flow", "wallets", "health"]
    missing = [v for v in views if f'id="v-{v}"' not in html]
    check("ui/router: every view has a section with its own id",
          not missing, f"missing {missing}")
    # A5 / BUG-20260910-068's sibling finding: every behavioural substring below is
    # asserted against the script with COMMENTS REMOVED. Three of these used to be
    # satisfied by the block comment that EXPLAINS the router, so a page whose router
    # body had been deleted -- comment intact -- passed. See
    # `test_ui_router_assertions_read_code_not_comments` for the mutation proofs.
    code = _script_code(html)
    check("ui/router: routes are HASH routes so a view is linkable and a deep link restores it",
          "location.hash='#/'+" in code.replace(" ", "")
          and "window.addEventListener('hashchange',route)" in code.replace(" ", "")
          and "#/market" in html)
    check("ui/router: global state -- collection, interval, range, denomination, transform and "
          "the trait filter -- lives in the hash and survives a reload",
          all(k in code for k in ("GLOBAL_KEYS", "writeHash", "readHash",
                                  "localStorage.setItem", "history.replaceState")))
    check("ui/router: a per-view DEFAULT range/interval applies on first visit only; after the "
          "Operator changes it, his choice sticks",
          "VIEW_DEFAULTS" in code and "S.touched.range" in code and "S.touched.interval" in code)

    # Every panel that existed before the split, by the id the old page drew into.
    panels = ["kpis", "p-prices", "tlegend", "b-prices", "h-prices", "h-prices-sub",
              "book", "p-imm", "b-imm", "p-mix", "p-act", "b-act", "makers", "tape",
              "screener", "gaps", "audit", "s-bar", "s-head", "s-curve", "s-hist",
              "s-mini", "s-drill", "b-surv", "s-sub"]
    sidebar = ["traits", "chips", "tsum", "onboard"]        # global chrome, not a view panel
    counts = {p: len(re.findall(rf'id="{re.escape(p)}"', html)) for p in panels + sidebar}
    wrong = {p: c for p, c in counts.items() if c != 1}
    check("ui/router: EVERY panel the page had before the split still exists, exactly once -- "
          "nothing was dropped on the floor and nothing was duplicated into two views",
          not wrong, str(wrong))

    # and each one is inside a view, not orphaned between them
    bodies = {}
    for v in views:
        seg = html.split(f'id="v-{v}"', 1)
        bodies[v] = seg[1].split('<section class="view"', 1)[0] if len(seg) > 1 else ""
    homeless = [p for p in panels if not any(f'id="{p}"' in b for b in bodies.values())]
    check("ui/router: ...and every one of them is inside a view, so switching tabs cannot "
          "leave a panel rendered under the wrong one", not homeless, str(homeless))
    aside = html.split("<aside>", 1)[1].split("</aside>", 1)[0] if "<aside>" in html else ""
    check("ui/router: the trait sidebar stays OUTSIDE the views, because the filter is global "
          "-- one filter, applied on every tab, not four copies that can disagree",
          all(f'id="{p}"' in aside for p in sidebar),
          str([p for p in sidebar if f'id="{p}"' not in aside]))
    check("ui/router: the Prices card is ONE card the router MOVES between the Market and "
          "Traits grids -- two copies would be two price panels on different token sets",
          'id="card-prices"' in html and html.count('id="card-prices"') == 1
          and "anchor-market-hero" in code and "anchor-traits-hero" in code
          and "function hostHero()" in code and ".after(" in code)

    check("ui/router: the trait sidebar is GLOBAL -- the filter applies on every view and is "
          "shown as a read-only chip row where the 272 px of checkboxes do not earn their place",
          "SIDEBAR_VIEWS.includes(S.view)" in code and 'id="roChips"' in html)
    check("ui/router: only the active view's panels are fetched, so switching tabs is not four "
          "views' worth of queries",
          "VIEW_LOADERS" in code and "await Promise.all(VIEW_LOADERS[S.view].map" in code)


def test_ui_ledger_and_wallets_panels_are_the_shape_the_design_specifies() -> None:
    """design §8.1 and §8.2, and the two rules that make them honest:
    every percentage with its count, and a cap that is stated rather than sampled.
    """
    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()
    script = _script_code(html)          # A5: comments never satisfy an assertion about behaviour
    check("ui/ledger: the Flow view carries the event ledger and it reads /api/ledger",
          'id="ledger"' in html and "/api/ledger?" in script)
    check("ui/ledger: it pages by CURSOR, never by offset -- the page cannot ask for a page "
          "number the API refuses to serve",
          "LG.cursor=r.next_cursor" in script.replace(" ", "")
          and "cursor:LG.cursor" in script.replace(" ", ""))
    check("ui/ledger: sorting is server-side against the whitelist, and a column the API "
          "cannot sort is shown as unsortable instead of offering a click that 400s",
          "r.sortable" in script and "unsortable" in script)
    check("ui/ledger: every column the design names is rendered -- time, type, token #, price "
          "in ETH and USD, maker, taker, order hash and the exit reason",
          all(k in script for k in ("token_num", "price_eth", "price_usd", "exit_reason",
                                    "order_hash", "price_basis", "lag_s")))
    check("ui/ledger: USD is to the cent on every row, through the same formatter as the rest "
          "of the page", "money2(x.price_usd,'USD')" in script)
    check("ui/ledger: 'chart this selection' asks for mode=chart, draws MARKERS always, keeps "
          "holes, and prints the cap rather than sampling silently",
          "mode:'chart'" in script and "c.basis.cap_note" in script
          and "connectgaps:false" in script and "lines+markers" in script)
    check("ui/wallets: the address card reads /api/wallets and /api/wallet/",
          "/api/wallets?" in script and "/api/wallet/" in script)
    check("ui/wallets: every percentage is printed FROM the {pct,n,of} object, so the page "
          "cannot show a percentage the API did not send a count with",
          "const pctOf=" in script and "s.of" in script and "s.n" in script)
    check("ui/wallets: chain-sourced facts render as 'not collected yet', never as a zero and "
          "never invented", "not collected yet" in html and "w.chain.collected" in script)
    check("ui/wallets: the counterparty graph is a HEAT-MAP (a Plotly heatmap trace), not a "
          "force graph -- deterministic, no second library, and a cell states the count",
          "type:'heatmap'" in script and "%{z:,d} trade" in script
          and not any(k in html for k in ("d3-force", "forceSimulation", "d3.forceLink")))
    check("ui/wallets: the heat-map scale is SEQUENTIAL and built from a palette token -- "
          "counts have no meaningful midpoint, so a diverging scale would invent one",
          "colorscale:[[0,rgba(C.coll" in script and "single hue" in script.lower())
    check("ui/wallets: a cluster prints its evidence count, and a flag renders only when its "
          "rows can be listed", "evidence_count" in script and "flags_rule" in script)
    check("ui/health: the Health view surfaces the recorder, gaps, the audit, the crossed-book "
          "alarm count, criteria coverage, files_failed/files_short, store size and last fold",
          all(k in script for k in ("/api/health", "crossed_book", "criteria_coverage",
                                    "files_failed", "files_short", "last_fold_at")))


def test_ui_escaping_matches_its_sink() -> None:
    """BUG-20260910-066. `esc()` is the page's HTML escaper, and escaping is
    correct at exactly one boundary: a string being interpolated into `innerHTML`.
    A string assigned to `.textContent` is never parsed as markup, so escaping it
    is a DOUBLE encoding -- and the one string on this page that contains a `<` is
    the sentence explaining why a percentile is missing (`n_eff = 0 ... < 30`).
    The Operator was shown `&lt;` in the one place REQ-F-19 most needs him to read
    a plain sentence.

    `test_ui_contract` asserts the dangerous direction (an unescaped third-party
    field reaching innerHTML) and by design says nothing about this one. This test
    is the other half: every esc() call site is checked against the sink it feeds.
    """
    import re
    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()
    script = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]

    # The two textContent sinks that carry server prose. For each, take the whole
    # assignment -- the template plus every local it interpolates -- and assert no
    # esc() survives anywhere in it.
    def region(start: str, end: str) -> str:
        return script.split(start, 1)[1].split(end, 1)[0] if start in script else ""

    head = region("function survHead(r){", "function survBasis(r){")
    basis = region("function survBasis(r){", "async function surv(){")
    check("ui/escaping: the survival HEADER builds plain text for .textContent -- no esc(), so "
          "`n_eff < 30` reads as `<` and not as `&lt;` (BUG-20260910-066)",
          "$('#s-head').textContent=" in head and "esc(" not in head,
          "; ".join(re.findall(r"esc\([^)]*\)", head)))
    check("ui/escaping: ...and so does the survival BASIS line",
          "$('#b-surv').textContent=" in basis and "esc(" not in basis,
          "; ".join(re.findall(r"esc\([^)]*\)", basis)))
    check("ui/escaping: the fix is annotated with the bug id, because the obvious move on "
          "re-reading this code is to add esc() back",
          "BUG-20260910-066" in script and "textContent" in script.split("BUG-20260910-066", 1)[1][:400])

    # The property, over the whole page: an esc() call may only appear inside a
    # template that is heading for innerHTML / an attribute -- never inside one of
    # the plain-text sinks. Checked structurally: every `X.textContent=` assignment
    # in the file, through to the end of its statement, must be esc()-free.
    leaks = []
    for m in re.finditer(r"\$\('#[\w-]+'\)\.textContent\s*=", script):
        seg, depth, i = "", 0, m.end()
        while i < len(script):                      # to the end of the assignment
            ch = script[i]
            depth += (ch in "([{") - (ch in ")]}")
            if ch in ";\n" and depth <= 0 and not script[i:i + 2] == "\n +":
                break
            seg += ch
            i += 1
        if "esc(" in seg:
            leaks.append(seg[:80])
    check("ui/escaping: NO .textContent assignment anywhere on the page escapes its value -- "
          "the property, not the two call sites the bug was found in",
          not leaks, " | ".join(leaks[:3]))
    # ...and the dangerous direction is still covered, by the test that owns it.
    check("ui/escaping: the innerHTML boundary still escapes -- esc() is alive and used",
          script.count("esc(") > 40 and "const esc=" in script, f"{script.count('esc(')} call sites")


# ===========================================================================
# BUG-20260910-067 -- TWO WRITERS ON ONE DERIVED STORE.
#
# On 2026-09-10 `data/analytics.sqlite` (2.8 GB) became "database disk image is
# malformed" and the launchd dashboard crash-looped 177 times. The mechanism:
# `serve()` constructed `Dashboard(...)` -- which opens the store and starts the
# thread that WRITES to it -- BEFORE binding 127.0.0.1:8765. With a second
# dashboard already listening, every launchd retry opened the store, folded new
# frames into it, and then died on "Address already in use". Two writers
# alternating on one SQLite store, ten seconds apart, with processes killed
# mid-write.
#
# Every check below was written against the OLD code first and fails there
# (docs/03 §9). The store is derived and was rebuilt from the landing zone; the
# landing zone was not touched.
# ===========================================================================
def _dash_root(tmp: Path, name: str):
    """A project root with config, a landing zone, and three real frames in it."""
    import shutil as _sh

    import yaml
    root = tmp / name
    (root / "config").mkdir(parents=True)
    for f in ("base.yaml", "intervals.yaml", "watchlist.yaml"):
        _sh.copy(ROOT / "config" / f, root / "config" / f)
    cfg = yaml.safe_load((root / "config" / "base.yaml").read_text())
    clock = FakeClock(datetime(2026, 9, 9, 10, 20, 0, tzinfo=timezone.utc))
    w = LandingZoneWriter(root / cfg["landing"]["root"], f"run-{name}", codec=GzipCodec(),
                          clock=clock.now, monotonic=clock.monotonic, auto_flush=False)
    for raw in (REAL_BID, REAL_COLL_OFFER, DOC_LISTING):
        w.write(json.dumps(raw), topic="collection:argonauts",
                event_timestamp=raw[4]["payload"]["event_timestamp"])
    w.close()
    return root, cfg


def test_dashboard_binds_before_it_opens_the_store(tmp: Path) -> None:
    """The ordering fix. A dashboard that cannot bind must never touch the store.

    The test holds the port with a plain listening socket -- the second dashboard,
    from this process's point of view -- and spies on `Normalizer` construction,
    because constructing one is exactly the moment the store is opened and the
    fold thread becomes possible. Against the old `serve()` the spy fires and the
    sqlite file appears on disk before the OSError is raised; that is the bug,
    177 times over.
    """
    import argparse
    import contextlib
    import io
    import socket

    from navanax import cli as cli_mod
    from navanax import dashboard as dash_mod

    root, cfg = _dash_root(tmp, "bindfirst")
    db = root / cfg["analytical"]["path"]
    lock = db.with_name(db.name + ".lock")

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]

    built: list[Any] = []
    real = dash_mod.Normalizer

    class SpyNormalizer(real):                      # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            built.append((a, k))
            super().__init__(*a, **k)

    dash_mod.Normalizer = SpyNormalizer
    raised: BaseException | None = None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                dash_mod.serve(root, cfg, ["argonauts"], host="127.0.0.1", port=port,
                               open_browser=False)
            except OSError as exc:
                raised = exc
    finally:
        dash_mod.Normalizer = real

    check("bind-first: serve() fails on the BIND when the port is already held",
          isinstance(raised, OSError), repr(raised))
    check("bind-first: the Normalizer -- which opens the store and starts the thread that "
          "WRITES to it -- was never constructed, so a doomed retry folds nothing "
          "(BUG-20260910-067)",
          built == [], f"constructed {len(built)} time(s)")
    check("bind-first: ...and neither the store nor its writer lock was created at all",
          not db.exists() and not lock.exists(), f"db={db.exists()} lock={lock.exists()}")

    args = argparse.Namespace(root=str(root), port=port, no_browser=True)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = cli_mod.cmd_dashboard(args)
    msg = err.getvalue()
    check("bind-first: `navanax dashboard` exits 2 on a port conflict -- the supervisor "
          "contract, so launchd's restart is throttled and the reason is in the log",
          rc == 2 and rc == cli_mod.DASH_EXIT_REFUSED, f"exit {rc}")
    check("bind-first: ...and the message still says another dashboard is probably running, "
          "and now also says the store was not opened",
          "another dashboard already running" in msg and "NOT opened" in msg, msg[:220])
    check("bind-first: the refused CLI run left no store behind either",
          not db.exists(), str(db))
    holder.close()


def test_normalizer_writer_lock_is_one_writer_per_store(tmp: Path) -> None:
    """docs/07 §1: one writer owns the read-write connection, readers attach read-only.

    That was a documented pattern with nothing enforcing it. Now the folding
    writer holds an exclusive `flock` on `<store>.lock` for its lifetime, a second
    one is refused by name and pid, and a reader opens `mode=ro` and cannot write
    even if it tries.
    """
    import errno as _errno
    import os as _os
    import sqlite3 as _sq

    from navanax.normalize import (
        LOCK_UNSUPPORTED_ERRNOS,
        Normalizer,
        StoreWriterBusyError,
        writer_lock_path,
    )

    (tmp / "empty-lz").mkdir(exist_ok=True)
    db = tmp / "onewriter.sqlite"
    w1 = Normalizer(tmp / "empty-lz", db)
    lock = writer_lock_path(db)
    check("writer lock: the lock file sits next to the sqlite file -- one lock per STORE, "
          "not per project root",
          lock == db.with_name("onewriter.sqlite.lock") and lock.exists(), str(lock))
    info = w1.writer_info()
    check("writer lock: it records the holding pid and when it was taken",
          info["pid"] == _os.getpid() and info["this_process"] is True
          and bool(info["since"]) and info["alive"] is True, str(info))

    busy: StoreWriterBusyError | None = None
    try:
        Normalizer(tmp / "empty-lz", db)
    except StoreWriterBusyError as exc:
        busy = exc
    check("writer lock: a SECOND folding writer on the same store is REFUSED, not admitted "
          "-- this is the whole of BUG-20260910-067",
          busy is not None, "a second Normalizer(writer=True) was allowed to open the store")
    check("writer lock: the refusal names the pid that holds it and the two ways out "
          "(stop that process, or open read-only)",
          busy is not None and busy.pid == _os.getpid()
          and f"kill {_os.getpid()}" in str(busy) and "writer=False" in str(busy)
          and "BUG-20260910-067" in str(busy), str(busy)[:260])

    rd = Normalizer(tmp / "empty-lz", db, writer=False)
    check("writer lock: a READER opens the same store while the writer holds the lock -- "
          "the lock is on FOLDING, never on querying",
          rd.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0)
    ro_enforced = False
    try:
        rd.conn.execute("INSERT INTO unparsed (run, seq, file, reason) VALUES ('r',1,'f','x')")
    except _sq.OperationalError as exc:
        ro_enforced = "readonly" in str(exc).lower()
    check("writer lock: the reader is read-only because SQLITE says so (mode=ro), not "
          "because the code politely does not write", ro_enforced)
    for fn in ("sync", "reset_for_refold", "refold_criteria"):
        refused = False
        try:
            getattr(rd, fn)()
        except RuntimeError as exc:
            refused = "writer=False" in str(exc)
        check(f"writer lock: a reader's {fn}() refuses rather than becoming the second writer",
              refused)
    rd.close()

    missing = False
    try:
        Normalizer(tmp / "empty-lz", tmp / "never-folded.sqlite", writer=False)
    except FileNotFoundError:
        missing = True
    check("writer lock: opening a store that does not exist read-only REFUSES rather than "
          "creating an empty one, which a reader would then report as 'no events'", missing)

    w1.close()
    released = True
    try:
        w2 = Normalizer(tmp / "empty-lz", db)
        w2.close()
    except StoreWriterBusyError:
        released = False
    check("writer lock: close() releases it, so the next start is clean and a crash-looping "
          "supervisor is not locked out forever", released)

    cli_src = (ROOT / "src" / "navanax" / "cli.py").read_text()
    check("writer lock: the store lock and the ingest lock share ONE list of 'this filesystem "
          "cannot flock' errnos, so the two cannot drift apart (tech-lead finding #4)",
          "LOCK_UNSUPPORTED_ERRNOS" in cli_src
          and _errno.EOPNOTSUPP in LOCK_UNSUPPORTED_ERRNOS
          and _errno.EWOULDBLOCK not in LOCK_UNSUPPORTED_ERRNOS,
          sorted(LOCK_UNSUPPORTED_ERRNOS))


def test_traits_writer_waits_and_takes_no_fold_lock(tmp: Path) -> None:
    """The traits job writes `tokens`/`traits` and folds nothing -- so it must NOT
    take the fold lock (that would make the daily traits job and the dashboard
    mutually exclusive), but it MUST have a busy timeout, or a fold's batch insert
    turns into a spurious "database is locked" that reads like a real failure.
    """
    from navanax.normalize import (
        BUSY_TIMEOUT_MS,
        Normalizer,
        StoreWriterBusyError,
        writer_lock_path,
    )
    from navanax.traits import open_store

    (tmp / "empty-lz").mkdir(exist_ok=True)
    db = tmp / "traitslock.sqlite"
    folder = Normalizer(tmp / "empty-lz", db)          # the fold writer, holding the lock
    conn = None
    try:
        conn = open_store(db)
    except StoreWriterBusyError:
        pass
    check("traits: the traits job opens the store WHILE the fold holds the writer lock -- it "
          "writes tokens/traits, folds no events, and is not a second folding writer",
          conn is not None)
    if conn is not None:
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        check("traits: ...and it opens with a busy_timeout of at least 5 s, so a fold's batch "
              "insert makes it WAIT rather than fail with 'database is locked'",
              bt >= 5000, f"busy_timeout={bt} ms")
        conn.execute("INSERT OR REPLACE INTO tokens (collection, token_id, listed_at) "
                     "VALUES ('argonauts','1','2026-09-10T00:00:00Z')")
        conn.commit()
        check("traits: ...and it can still actually write its own two tables",
              conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 1)
        conn.close()
    folder.close()

    fresh = tmp / "traits-first.sqlite"
    c2 = open_store(fresh)
    check("traits: open_store takes no fold-writer lock of its own ...",
          not writer_lock_path(fresh).exists(), str(writer_lock_path(fresh)))
    admitted = True
    try:
        n2 = Normalizer(tmp / "empty-lz", fresh)
        n2.close()
    except StoreWriterBusyError:
        admitted = False
    check("traits: ...so a store the traits job touched first still admits a folding writer",
          admitted)
    c2.close()

    src = (ROOT / "src" / "navanax" / "traits.py").read_text()
    check("traits: traits.py takes no lock at all -- the fold lock is the fold's, and only "
          "the fold's (the word appears here once, in the docstring saying why)",
          "fcntl" not in src and "LOCK_EX" not in src, src.count("flock"))
    check("traits: the busy timeout is STATED -- an explicit PRAGMA, and the store's one "
          "constant rather than a second number. `sqlite3.connect(timeout=...)` sets the same "
          "thing invisibly, and an invisible guarantee is one refactor from being lost",
          "PRAGMA busy_timeout" in src and "BUSY_TIMEOUT_MS" in src and BUSY_TIMEOUT_MS >= 5000,
          f"{BUSY_TIMEOUT_MS} ms")


def test_launchd_dashboard_cannot_retry_every_ten_seconds(tmp: Path) -> None:
    """The second layer. Binding first makes a doomed retry harmless; throttling
    makes the loop legible. 177 restarts in half an hour is a wall of banners
    nobody reads; at 30 s the same conflict is 120 an hour and says so in the log.
    """
    import plistlib
    ld = _launchd()
    dash = plistlib.loads(ld.render(ld.DASHBOARD, tmp))
    rec = plistlib.loads(ld.render(ld.TRAITS, tmp))
    recorder = plistlib.loads(ld.render(ld.RECORDER, tmp))
    check("launchd: the DASHBOARD throttles restarts to at least 30 s, so a port conflict "
          "cannot retry every 10 s (BUG-20260910-067)",
          dash.get("ThrottleInterval", 0) >= 30, repr(dash.get("ThrottleInterval")))
    check("launchd: the RECORDER's 10 s is unchanged -- a recorder that is down is losing "
          "history that cannot be bought back, and its restart writes a gap record",
          recorder.get("ThrottleInterval") == 10 and rec.get("ThrottleInterval") == 10,
          f"recorder={recorder.get('ThrottleInterval')} traits={rec.get('ThrottleInterval')}")
    check("launchd: the dashboard still restarts at all -- the page is how the Operator sees "
          "the record, so KeepAlive stays on",
          dash.get("KeepAlive") is True and dash.get("RunAtLoad") is True)
    ld_src = (ROOT / "tools" / "launchd.py").read_text()
    check("launchd: the interval names the incident, because 30 looks arbitrary on re-reading "
          "and the obvious tidy-up is to fold it back into THROTTLE_SECONDS",
          "DASHBOARD_THROTTLE_SECONDS" in ld_src and "BUG-20260910-067" in ld_src)
    doc = (ROOT / "docs" / "04_ENVIRONMENTS.md").read_text()
    cmd = (ROOT / "autostart-install.command").read_text()
    check("launchd: docs/04 and the installer text state the dashboard's interval, so the "
          "three artefacts cannot disagree about what the machine will do",
          "ThrottleInterval: 30" in doc and "30 seconds" in cmd,
          f"doc={'ThrottleInterval: 30' in doc} cmd={'30 seconds' in cmd}")


def test_health_names_the_store_writer_and_caches_quick_check(tmp: Path) -> None:
    """Health has to say the store is still READABLE, and who owns its write side.

    Before this, corruption in the analytical store was discovered by a crash
    loop, not by a check: nothing on any page looked at the store's integrity, and
    nothing named the process that was folding into it. `quick_check` is cached
    for ten minutes because it reads every page -- on the Operator's 2.8 GB store
    that is seconds, and per-request it would hold the writer lock each time.
    """
    import http.client
    import os as _os
    import threading as _th
    from http.server import ThreadingHTTPServer

    from navanax.dashboard import QUICK_CHECK_TTL_SECONDS, Dashboard, make_handler

    root, cfg = _dash_root(tmp, "writerhealth")
    dash = Dashboard(root, cfg, ["argonauts"])
    dash._sync_once()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dash))
    port = httpd.server_address[1]
    _th.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def get(path: str):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", path)
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, body

        st, body = get("/api/health")
        h = json.loads(body) if st == 200 else {}
        sw = h.get("store_writer") or {}
        check("health: /api/health names the process that owns the store's WRITE side -- pid "
              "and since -- so 'is a second dashboard folding into this?' is answerable from "
              "the page (BUG-20260910-067)",
              sw.get("pid") == _os.getpid() and bool(sw.get("since"))
              and sw.get("this_process") is True, str(sw)[:200])
        qc = h.get("quick_check") or {}
        check("health: ...and reports PRAGMA quick_check, so corruption is visible on Health "
              "before it is fatal rather than being found by a crash loop",
              qc.get("ok") is True and qc.get("result") == ["ok"], str(qc)[:200])
        check("health: the first call actually ran it, and the response says how old the "
              "answer is and how often it can be re-run",
              qc.get("cached") is False and qc.get("ttl_seconds") == QUICK_CHECK_TTL_SECONDS
              and QUICK_CHECK_TTL_SECONDS == 600, str(qc)[:200])

        st2, body2 = get("/api/health")
        qc2 = (json.loads(body2) or {}).get("quick_check") or {}
        check("health: the SECOND request is served from the cache -- a full-page read per "
              "request would make Health the slowest tab and hold the writer lock each time",
              qc2.get("cached") is True and qc2.get("at") == qc.get("at")
              and qc2.get("age_seconds") >= 0, str(qc2)[:200])

        dash._quick_check_mono -= (QUICK_CHECK_TTL_SECONDS + 1)   # age the cache past its TTL
        qc3 = dash.store_quick_check()
        check("health: ...and once the TTL has passed it runs again, so a store that goes bad "
              "in the next ten minutes is still reported",
              qc3.get("cached") is False and qc3.get("at") != qc.get("at"), str(qc3)[:200])

        html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()
        check("health/ui: the Health view renders both new fields -- the integrity check and "
              "the fold writer's pid -- and nothing else on the page changed",
              "h.quick_check" in html and "h.store_writer" in html
              and "quick_check ok" in html and "fold writer" in html)
    finally:
        httpd.shutdown()
        httpd.server_close()
        dash.stop()
        dash.norm.close()



# ===========================================================================
# PR-9, round 2 -- the tech-lead's blocking findings A1-A7 and C1.
# ===========================================================================
def _ledger_plan_store(tmp: Path, name: str, n_rows: int = 0):
    """A store for the plan / walk tests. `n_rows` bulk rows for the timing case.

    Rows are inserted straight into `events` rather than through `parse_event`:
    what is under test is the QUERY PLAN and the keyset, not the parser, and 200k
    frames through the parser would make the suite about the parser's speed.
    """
    from navanax.metrics import MetricEngine, load_intervals
    from navanax.normalize import Normalizer
    from navanax.traits import ensure_schema
    n = Normalizer(tmp / "empty-lz", tmp / name)
    ensure_schema(n.conn)
    if n_rows:
        base = 1_757_000_000.0
        rows = [("r", i, "f", "2026-09-09T10:00:00Z", "2026-09-09T10:00:00Z",
                 base + i, base + i, ("item_listed", "item_received_bid", "item_sold")[i % 3],
                 "argonauts", str(i % 977), f"0xh{i}", f"0xmk{i % 53}", None, 0.001 * (i % 991))
                for i in range(n_rows)]
        n.conn.executemany(
            "INSERT INTO events (run,seq,file,observed_at,valid_at,observed_ts,valid_ts,"
            "event_type,collection,token_id,order_hash,maker,taker,price_eth) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        n.conn.commit()
        n.conn.execute("ANALYZE")
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    return n, eng


def test_ledger_page_uses_its_index_under_a_time_window(tmp: Path) -> None:
    """A1 (S1). `dashboard._window` is UNCONDITIONAL -- every ledger request the page
    makes carries `valid_ts >= ? AND valid_ts < ?`. Given that range predicate,
    SQLite prefers `ix_events_coll_valid` and then sorts the whole window into a
    TEMP B-TREE, so five of the six sorts paid for an in-memory sort of every
    matching row **on every page**. The keyset was constant-time and the sort was
    not, which is the same defect as BUG-040 one query over.

    The fix is three things, and this test is about the two that are load-bearing:
    an ORDER BY that is the index's own column order `(key, valid_ts, rowid)`, and
    `+e.valid_ts` on the sorts whose index does not lead with a timestamp -- the
    unary plus being SQLite's documented way to keep a term out of the index
    constraint. The third, `INDEXED BY`, is **deliberately un-exercised insurance**:
    with the other two in place the planner already chooses the right index, so no
    assertion here can fail on its absence; it is there so that a future schema or
    statistics change cannot silently move the query onto a different index, and it
    would fail loudly rather than degrade.

    THE PLANS ARE READ AGAINST THE 200,000-ROW STORE, not an empty one. On an empty
    table SQLite has no reason to skip-scan and every plan looks fine, so a test run
    against a fixture would have passed before the fix as well as after it -- which
    is the whole failure mode being guarded here.

    There is deliberately NO wall-clock assertion. The measured numbers (864 ms ->
    0.2 ms for page 1, 7-9 ms at 12,000 rows deep) belong in the bug ledger, where
    they are evidence; as a threshold in CI they would be a flaky test that fails
    under load and teaches everyone to re-run the suite. The PLAN is the property,
    and the plan is deterministic.
    """
    import navanax.metrics as _M
    from navanax.metrics import LEDGER_SORTS
    n, eng = _ledger_plan_store(tmp, "plan.sqlite", n_rows=200_000)
    w = {"start": 1_757_000_000.0 - 10, "end": 1_757_000_000.0 + 300_000}

    def survey():
        """(bad plans, plans that missed their index) over every sort x direction."""
        bad, missing = [], []
        for sort in LEDGER_SORTS:
            for direction in ("asc", "desc"):
                q = eng.ledger_query_plan("argonauts", sort=sort, direction=direction,
                                          limit=200, **w)
                text = " | ".join(q["plan"])
                if "TEMP B-TREE" in text.upper():
                    bad.append(f"{sort}/{direction}")
                if q["index"] not in text:
                    missing.append(f"{sort}/{direction}: wanted {q['index']}, got {text}")
        return bad, missing

    bad_plan, missing_idx = survey()
    check("ledger/plan: over 200,000 rows, with a time window present -- which every request "
          "from the page has -- NO sort falls back to a TEMP B-TREE (A1)",
          not bad_plan, "; ".join(bad_plan))
    check("ledger/plan: ...and every one of them scans the index the basis NAMES, so "
          "`basis.index` is a fact about the query rather than a hope",
          not missing_idx, "; ".join(missing_idx))

    p1 = eng.ledger("argonauts", sort="maker", direction="desc", limit=5, **w)
    q = eng.ledger_query_plan("argonauts", sort="maker", direction="desc", limit=5,
                              cursor=p1["next_cursor"], **w)
    check("ledger/plan: a cursor page keeps the index too -- the deep page is the one that "
          "could not afford a sort",
          "TEMP B-TREE" not in " ".join(q["plan"]).upper() and q["index"] in " ".join(q["plan"]),
          str(q["plan"]))
    check("ledger/plan: the basis says the index is FORCED and names the keyset tuple, so a "
          "reader is not left to assume the planner cooperated",
          p1["basis"]["index_forced"] is True
          and "valid_ts" in p1["basis"]["pagination"] and "rowid" in p1["basis"]["pagination"],
          str({k: p1["basis"][k] for k in ("index", "index_forced", "pagination")}))

    # The `+valid_ts` de-optimisation is SCOPED: on the default sort the window is
    # both the filter and the order, and there it must stay an index RANGE. Widening
    # it to every sort would turn the page the Operator actually looks at into a scan.
    vplan = " ".join(eng.ledger_query_plan("argonauts", sort="valid_ts", limit=100, **w)["plan"])
    mplan = " ".join(eng.ledger_query_plan("argonauts", sort="maker", limit=100, **w)["plan"])
    check("ledger/plan: on the DEFAULT sort the time window is still an index RANGE "
          "(`valid_ts>? AND valid_ts<?`), not a per-row filter",
          "valid_ts>?" in vplan.replace(" ", "") and "valid_ts<?" in vplan.replace(" ", ""), vplan)
    check("ledger/plan: on the other sorts it is deliberately NOT an index constraint -- that "
          "is what stops the planner skip-scanning and then sorting (A1)",
          "valid_ts>?" not in mplan.replace(" ", ""), mplan)

    # -- the mutation: remove ONLY the unary plus and re-read the planner ----
    # 8 of the 12 plans must regress. Exactly 8, and which 8 is the point:
    #   * valid_ts (2)    -- never used the plus; its index leads with valid_ts.
    #   * observed_ts (2) -- its index is (collection, observed_ts), only two columns,
    #                        so there is no third slot for the range term to skip-scan
    #                        into and the plan was never at risk.
    #   * the other four sorts x two directions = 8 -- these are the ones whose index
    #                        has valid_ts as its trailing column, which is exactly the
    #                        shape SQLite will skip-scan to reach.
    real_where = _M.MetricEngine._ledger_where

    def no_plus(self, collection, **kw):
        kw["window_plus"] = False
        return real_where(self, collection, **kw)

    try:
        _M.MetricEngine._ledger_where = no_plus
        bad2, _ = survey()
        check("ledger/plan (mutation): removing ONLY the unary plus puts a TEMP B-TREE back "
              "into exactly 8 of the 12 plans -- so the plus is what the assertions above are "
              "proving, and it is not incidental",
              len(bad2) == 8, f"{len(bad2)} regressed: {sorted(bad2)}")
        check("ledger/plan (mutation): ...and it is the four sorts whose index has valid_ts as "
              "its TRAILING column -- valid_ts and observed_ts are untouched, because neither "
              "has a slot for the range term to skip-scan into",
              {b.split("/")[0] for b in bad2} == {"token_num", "maker", "event_type", "price_eth"},
              f"regressed on {sorted({b.split('/')[0] for b in bad2})}")
    finally:
        _M.MetricEngine._ledger_where = real_where
    after, _ = survey()
    check("ledger/plan (mutation): the plus was restored and every plan is clean again",
          not after, "; ".join(after))
    n.close()


def test_ledger_keyset_walks_nulls_and_ties_on_every_sort(tmp: Path) -> None:
    """A3 / A4 (S2). Two branches of the keyset carried the whole correctness of
    pagination and neither had a test:

      * the NULL branch -- SQLite orders NULLs first ASC and last DESC, and
        `key < NULL` is NULL rather than true, so a naive keyset stops dead at the
        first null-keyed row and silently truncates the result;
      * the rowid tiebreak -- at 48 events/s many rows share a timestamp, and
        without a total order a page boundary that lands inside a tie either
        repeats rows or skips them.

    The property is the only thing worth asserting: walking the cursor to the end
    must visit EVERY row exactly once, on every sort, in both directions, over a
    fixture that is deliberately full of nulls and ties. Both branches are then
    removed, one at a time, to prove the walk catches each.
    """
    import navanax.metrics as _M
    n, eng = _ledger_plan_store(tmp, "walk.sqlite")
    base = 1_757_000_000.0
    rows = []
    for i in range(60):
        rows.append(("r", i, "f", "2026-09-09T10:00:00Z", "2026-09-09T10:00:00Z",
                     base + (i % 5),                 # observed_ts: heavy ties
                     (base + (i % 5)) if i % 7 else None,          # valid_ts: NULLs
                     ("item_listed", "item_sold")[i % 2], "argonauts",
                     (str(i % 4) if i % 3 else None),               # token_id -> token_num NULLs + ties
                     f"0xh{i}",
                     (f"0xmk{i % 3}" if i % 4 else None),           # maker NULLs + ties
                     None,
                     ((i % 3) * 0.5 if i % 5 else None)))           # price_eth NULLs + ties
    n.conn.executemany(
        "INSERT INTO events (run,seq,file,observed_at,valid_at,observed_ts,valid_ts,"
        "event_type,collection,token_id,order_hash,maker,taker,price_eth) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    n.conn.commit()
    everything = {r[0] for r in n.conn.execute("SELECT order_hash FROM events")}
    check("ledger/walk: the fixture really does contain nulls and ties on every sortable "
          "column, or this test proves nothing",
          len(everything) == 60
          and n.conn.execute("SELECT COUNT(*) FROM events WHERE valid_ts IS NULL").fetchone()[0] > 0
          and n.conn.execute("SELECT COUNT(*) FROM events WHERE maker IS NULL").fetchone()[0] > 0
          and n.conn.execute("SELECT COUNT(*) FROM events WHERE token_num IS NULL").fetchone()[0] > 0
          and n.conn.execute("SELECT COUNT(*) FROM events WHERE price_eth IS NULL").fetchone()[0] > 0
          and n.conn.execute(
              "SELECT MAX(c) FROM (SELECT COUNT(*) c FROM events GROUP BY observed_ts)").fetchone()[0] > 5)

    def walk(sort, direction):
        seen, cur, guard = [], None, 0
        while guard < 200:
            guard += 1
            pg = eng.ledger("argonauts", sort=sort, direction=direction, limit=7, cursor=cur)
            seen += [r["order_hash"] for r in pg["rows"]]
            cur = pg["next_cursor"]
            if not cur:
                break
        return seen

    lost, dupes = [], []
    for sort in _M.LEDGER_SORTS:
        for direction in ("asc", "desc"):
            seen = walk(sort, direction)
            if set(seen) != everything:
                lost.append(f"{sort}/{direction}: missed {len(everything - set(seen))}")
            if len(seen) != len(set(seen)):
                dupes.append(f"{sort}/{direction}: {len(seen) - len(set(seen))} repeated")
    check("ledger/walk: every sort, both directions, visits EVERY row -- nulls included",
          not lost, "; ".join(lost))
    check("ledger/walk: ...and repeats none of them -- the rowid tiebreak makes the order total",
          not dupes, "; ".join(dupes))

    # -- prove the two branches by removing them, one at a time --------------
    real = _M._keyset_clause

    def no_nulls(cols, vals, direction):
        """The naive version: `key < ?` with no null handling at all."""
        op = "<" if direction == "desc" else ">"
        sql = "0"
        args: list = []
        for col, v in zip(reversed(cols), reversed(vals), strict=True):
            sql = f"({col} {op} ? OR ({col} = ? AND {sql}))"
            args = [v, v] + args
        return sql, args

    def no_tiebreak(cols, vals, direction):
        """Everything but the rowid: the order is no longer total."""
        return real(cols[:-1], vals[:-1], direction)

    try:
        _M._keyset_clause = no_nulls
        broke = [s for s in _M.LEDGER_SORTS if set(walk(s, "desc")) != everything]
        check("ledger/walk (mutation): removing the NULL branch loses rows on the sorts whose "
              "column is nullable -- so the branch is what the walk above is proving",
              {"maker", "price_eth", "token_num"} <= set(broke), f"broke on {sorted(broke)}")
        _M._keyset_clause = no_tiebreak
        bad = []
        for s in _M.LEDGER_SORTS:
            seen = walk(s, "desc")
            if set(seen) != everything or len(seen) != len(set(seen)):
                bad.append(s)
        check("ledger/walk (mutation): removing the rowid tiebreak breaks the walk on a fixture "
              "this full of ties -- rows are skipped or repeated at every page boundary",
              bad, f"still clean on {sorted(set(_M.LEDGER_SORTS) - set(bad))}")
    finally:
        _M._keyset_clause = real
    n.close()


def test_ledger_chart_caption_counts_what_it_draws(tmp: Path) -> None:
    """A2 (S1). The caption said `charting the 5,000 most recent of N` where N was
    (a) counted WITHOUT the price predicate the chart itself applies, so it named
    more rows than were ever chartable, and (b) printed as an exact figure even
    when it was the count cap -- a floor rendered as a fact, in the one sentence
    whose whole job is to say the picture is incomplete.
    """
    from navanax.metrics import LEDGER_COUNT_CAP
    n, eng = _ledger_plan_store(tmp, "chartcap.sqlite")
    base = 1_757_000_000.0
    rows = []
    for i in range(50):
        rows.append(("r", i, "f", "2026-09-09T10:00:00Z", "2026-09-09T10:00:00Z", base + i, base + i,
                     "item_listed", "argonauts", str(i), f"0xh{i}", "0xmk", None,
                     (1.0 + i) if i < 20 else None))       # only 20 of the 50 carry a price
    n.conn.executemany(
        "INSERT INTO events (run,seq,file,observed_at,valid_at,observed_ts,valid_ts,"
        "event_type,collection,token_id,order_hash,maker,taker,price_eth) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    n.conn.commit()
    w = {"start": base - 10, "end": base + 1000}
    c = eng.ledger_chart("argonauts", cap=5, **w)
    check("ledger/chart: `matched` counts the rows the chart can actually DRAW -- with the "
          "price predicate the chart query applies, not the 50 rows the table has (A2)",
          c["matched"] == 20 and c["charted"] == 5 and c["capped"] is True,
          str({k: c[k] for k in ("matched", "charted", "capped")}))
    check("ledger/chart: ...and the caption names that same number, so the sentence under the "
          "chart is about the chart",
          "of 20" in (c["basis"]["cap_note"] or ""), str(c["basis"]["cap_note"]))
    check("ledger/chart: the caption says the count is exact, because at this size it is",
          c["matched_exact"] is True and "floor" not in (c["basis"]["cap_note"] or ""),
          str(c["basis"]["cap_note"]))
    check("ledger/chart: the predicate is STATED, so a reader can see why the chart draws "
          "fewer rows than the table shows",
          "price" in (c["basis"].get("matched_predicate") or ""), str(c["basis"].get("matched_predicate")))

    # above the count cap the same sentence must read as a floor, exactly as count_note does
    import navanax.metrics as _M
    old = _M.LEDGER_COUNT_CAP
    try:
        _M.LEDGER_COUNT_CAP = 4
        c2 = eng.ledger_chart("argonauts", cap=3, **w)
        check("ledger/chart: above the count cap the caption reads as a FLOOR and says so -- "
              "a capped count rendered as exact is a lie in the sentence that exists to "
              "admit the picture is incomplete (A2)",
              c2["matched_exact"] is False and "more than" in (c2["basis"]["cap_note"] or "")
              and ("floor" in (c2["basis"]["cap_note"] or "")),
              str(c2["basis"]["cap_note"]))
    finally:
        _M.LEDGER_COUNT_CAP = old
    check("the cap constant was restored", _M.LEDGER_COUNT_CAP == LEDGER_COUNT_CAP)
    n.close()


def test_wallet_identity_guard_matches_the_markers_it_claims(tmp: Path) -> None:
    """A6 (S4). The guard's comment promised to catch a field that could hold an
    off-chain identity; the marker list did not contain the words a leak would most
    plausibly arrive under -- `notes`, `alias`, `handle`, `owner`, `label`,
    `twitter` -- and nothing tested it below the top level.

    The matcher is SEGMENT-based, and that is not a detail: `ens` is a substring of
    `tokens` and `tag` of `vintage`, so a substring rule on the short markers would
    fire on `distinct_tokens_touched` and teach everyone to delete the guard.
    """
    from navanax.metrics import (
        IDENTITY_KEY_MARKERS,
        IDENTITY_SUBSTRING_MARKERS,
        _assert_address_only,
    )
    for m in ("notes", "note", "alias", "handle", "ens", "owner", "label", "tag",
              "twitter", "discord", "telegram", "name", "company", "employer",
              "location", "email", "phone",
              # R2-6: `note` alone was half a rule. A field called `comment`,
              # `memo`, `remark`, `description`, `about` or `free_text` is the same
              # field under a different name, and `contact` is the one that would
              # arrive already holding an email address.
              "comment", "memo", "remark", "description", "about", "free_text", "contact"):
        check(f"wallets/guard: {m!r} is a marker the guard actually carries",
              m in IDENTITY_KEY_MARKERS, str(sorted(IDENTITY_KEY_MARKERS)))
    probes = ({"cluster": {"members": [{"address": "0x1", "notes": "he told me his name"}]}},
              {"chain": {"self_attached_handle": "x"}},
              {"behaviour": {"owner_label": "x"}},
              {"a": {"b": {"c": [{"twitter": "@x"}]}}},
              {"alias": None},
              {"ens_name": None},
              # R2-6: every ordinary name for free text, at depth
              {"cluster": {"members": [{"comment": "spoke to him at a conference"}]}},
              {"memo": None}, {"remark": None}, {"description": None},
              {"about": None}, {"free_text": None}, {"contact": None},
              {"behaviour": {"analyst_comments": []}})
    caught = []
    for probe in probes:
        try:
            _assert_address_only(probe)
        except ValueError as exc:
            caught.append(str(exc))
    check("wallets/guard: a nested `cluster.members[].notes` -- the most plausible shape a leak "
          "would actually take -- is refused, and so is every other probe at every depth, "
          "including every ordinary name for a free-text field (R2-6)",
          len(caught) == len(probes), f"only {len(caught)} of {len(probes)} refused")
    check("wallets/guard: the refusal names the path and the marker, so it can be acted on",
          caught and "members" in caught[0] and "notes" in caught[0], caught[0][:160] if caught else "")
    ok = []
    for benign in ({"distinct_tokens_touched": 1}, {"events": 1}, {"collection_offers": 1},
                   {"counterparties": {"trades": 1}}, {"first_funded_by": None},
                   {"cancel_ratio": {"pct": None, "n": 0, "of": 0}},
                   {"count_cap": 1}, {"sales_as_seller": 0}, {"episode_gap_s": 60.0}):
        try:
            _assert_address_only(benign)
            ok.append(True)
        except ValueError as exc:
            ok.append(str(exc))
    check("wallets/guard: it does NOT fire on `distinct_tokens_touched` (which contains `ens`) "
          "or on any other real field -- a guard that cries wolf gets deleted",
          all(x is True for x in ok), str([x for x in ok if x is not True]))
    check("wallets/guard: the short, ambiguous markers are segment-only and the long ones are "
          "also checked as substrings, and the code says which is which",
          set(IDENTITY_SUBSTRING_MARKERS) < set(IDENTITY_KEY_MARKERS)
          and "ens" not in IDENTITY_SUBSTRING_MARKERS and "tag" not in IDENTITY_SUBSTRING_MARKERS
          and "name" in IDENTITY_SUBSTRING_MARKERS, str(sorted(IDENTITY_SUBSTRING_MARKERS)))

    # and the live profile still passes its own guard, with the markers widened
    n, eng, _ = _ledger_store(tmp, "guard-live.sqlite")
    w = _lwin()
    p = eng.wallet("argonauts", "0xmk1", w["start"], w["end"])
    check("wallets/guard: the profile this build returns passes the WIDENED guard -- so the "
          "prose fields were renamed rather than the guard being narrowed to fit them",
          isinstance(p, dict) and "chain" in p)
    check("wallets/guard: and there is no field for a self-attached handle at all now. The "
          "charter permits one; the SCHEMA is the enforcement, so a field that is null today "
          "and populated later is exactly the hole the guard exists to close",
          "self_attached_handle" not in json.dumps(p), "")
    n.close()


def test_writer_info_enforced_is_not_a_lie_on_a_reader(tmp: Path) -> None:
    """A7 (S4). `enforced: False` on a READER is indistinguishable from `enforced:
    False` on a writer whose filesystem has no flock -- and those are opposite
    facts. The first is "this process never asked for the lock"; the second is
    "this process asked and could not be protected, and a second writer could
    corrupt the store". Health renders the field, so the two must not print alike.
    """
    from navanax.normalize import Normalizer
    (tmp / "empty-lz").mkdir(exist_ok=True)
    db = tmp / "enforced.sqlite"
    w = Normalizer(tmp / "empty-lz", db)
    wi = w.writer_info()
    check("writer lock: on the WRITER, `enforced` is still the boolean it always was",
          isinstance(wi["enforced"], bool), str(wi["enforced"]))
    rd = Normalizer(tmp / "empty-lz", db, writer=False)
    ri = rd.writer_info()
    check("writer lock: on a READER, `enforced` is NOT False -- a reader never takes the "
          "lock, so 'not enforced' would be read as 'unprotected writer' (A7)",
          ri["enforced"] is not False, str(ri["enforced"]))
    check("writer lock: ...it says it is not applicable, and says why",
          isinstance(ri["enforced"], str) and "not applicable" in ri["enforced"]
          and "reader" in ri["enforced"], str(ri["enforced"]))
    check("writer lock: the reader still reports the writer's pid, so the field it CAN answer "
          "is unchanged", ri["pid"] == wi["pid"] and ri["this_process"] is False, str(ri))
    rd.close()
    w.close()


def test_buglog_check_fails_on_a_duplicate_bug_id(tmp: Path) -> None:
    """C1 (S3). `--check` collected ids into a SET, so two entries sharing an id
    collapsed into one and the gate stayed green. The ledger is the source of
    truth for what a bug id MEANS; two meanings under one id is the one defect
    that makes every other entry unciteable -- and it is exactly what happens when
    two branches allocate the same number, which this repo is doing right now with
    059-061.
    """
    import re
    import shutil as _sh
    import subprocess as _sp
    import sys as _sys
    scratch = tmp / "buglog-dupe"
    scratch.mkdir(exist_ok=True)
    _sh.copytree(ROOT / "docs" / "logs", scratch / "logs", dirs_exist_ok=True)
    # a real second entry under an id that already exists
    y = scratch / "logs" / "bugs.yaml"
    first = re.search(r"^- id: (BUG-\d{8}-\d{3})", y.read_text(), re.M).group(1)
    y.write_text(y.read_text().rstrip("\n") + f"""

- id: {first}
  summary: "a second entry under an id that is already taken"
  severity: S4
  priority: P3
  error_class: INF
  status: open
  logged_at: "2026-09-10T23:00:00-05:00"
  occurred_at: "2026-09-10T23:00:00-05:00"
  detected_by: "the duplicate-id test"
  detection_channel: tech-lead
  branch: test
  locations: []
  requirement: REQ-N-12
  data_impact: "none -- this entry exists only inside a temporary copy"
  root_cause: "test fixture"
  monitor_gap: "test fixture"
""")
    import importlib.util as _iu
    spec = _iu.spec_from_file_location("buglog_probe", ROOT / "tools" / "buglog.py")
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import yaml as _yaml
    data = _yaml.safe_load(y.read_text())
    old_md, old_ledger = mod.MARKDOWN, mod.LEDGER
    try:
        mod.MARKDOWN = scratch / "logs" / "BUGS.md"
        problems = mod.check(data)
    finally:
        mod.MARKDOWN, mod.LEDGER = old_md, old_ledger
    dupes = [p for p in problems if "duplicate" in p.lower() or "twice" in p.lower()]
    check("buglog: --check FAILS on a duplicate bug id, and names it (C1)",
          dupes and first in dupes[0], f"problems: {problems[:3]}")
    # and the real ledger has none
    real = _sp.run([_sys.executable, "tools/buglog.py", "--check"], cwd=ROOT,
                   capture_output=True, text=True)
    check("buglog: the real ledger has no duplicate id today, so this gate is not already "
          "failing when it lands", real.returncode == 0, real.stdout[-300:] + real.stderr[-300:])


def _script_code(html: str) -> str:
    """The page's script with COMMENTS REMOVED.

    A5. Three assertions in the PR-9 UI tests were satisfied by the block comment
    that explains the feature rather than by the feature: deleting the router's
    body and keeping its docstring left them green. Every substring assertion
    about behaviour must read this, never the raw file. Strings are left alone --
    a `//` inside a URL is not a comment, and neither is one inside a template.
    """
    src = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    out, i, n = [], 0, len(src)
    quote = None
    while i < n:
        ch = src[i]
        if quote:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def test_ui_router_assertions_read_code_not_comments() -> None:
    """A5 (S3). `test_ui_router_*` asserted on substrings of the whole file, and
    several of those substrings appear in the block comments that EXPLAIN the
    router. So a page whose router body had been deleted, keeping its comment,
    passed -- the test asserted that the rule is documented, not that it is in
    force. That is the same defect the tech-lead already fixed once in
    `test_ui_contract` (the modebar/scrollZoom checks, 2026-09-10).

    Each of the three structural claims below is proved by a mutation that leaves
    the comments untouched.
    """
    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()
    code = _script_code(html)
    check("ui/router (A5): the comment stripper works -- prose that only appears in a comment "
          "is gone, and a `//` inside a string is not treated as one",
          "the router MOVES it" not in code and "https://" in html and "esc(" in code,
          "stripper removed too much or too little")

    def p_route(c: str) -> bool:
        """route() itself reads the hash into the global controls.

        `applyGlobals(q)` is asserted BY NAME AND ARGUMENT, not as `applyGlobals(`.
        route() has two branches -- the URL branch (`applyGlobals(q)`) and the
        bare-URL/localStorage branch -- and a substring test for the bare call name
        is satisfied by either one. Deleting the URL branch alone is the regression
        that matters: it is what makes a deep link stop restoring its state, and it
        leaves the other call site behind to vouch for it.
        """
        body = c.split("function route(){", 1)[1].split("\n/*", 1)[0] if "function route(){" in c else ""
        return ("applyGlobals(q)" in body                                  # the URL branch
                and "applyGlobals(Object.fromEntries(p.entries()))" in body  # the stored branch
                and "readHash()" in body
                and "classList.toggle('on'" in body and "VIEWS.forEach" in body)

    def p_hero(c: str) -> bool:
        """hostHero() performs a real DOM move, not just a class change."""
        body = c.split("function hostHero(){", 1)[1].split("function sidebarMode()", 1)[0] \
            if "function hostHero(){" in c else ""
        return ".after(" in body and "anchor-traits-hero" in body and "card-prices" in body

    def p_cursor(c: str) -> bool:
        """The ledger's next page is fetched FROM next_cursor, not from a page number."""
        return ("cursor:LG.cursor" in c.replace(" ", "")
                and "LG.cursor=r.next_cursor" in c.replace(" ", "")
                and "page=" not in c.split("/api/ledger?", 1)[1][:200])

    check("ui/router (A5): route()'s BODY applies the hash to the global controls and toggles "
          "exactly one view on -- asserted on code, so deleting the body fails here",
          p_route(code))
    check("ui/router (A5): hostHero()'s BODY performs a real DOM move (`.after(`) of the one "
          "prices card between the two anchors", p_hero(code))
    check("ui/router (A5): the ledger's next page URL is built from `next_cursor` -- the page "
          "never sends an offset or a page number", p_cursor(code))

    # -- the three mutations, each leaving every comment in place ------------
    m1 = code.replace("applyGlobals(q)", "0").replace("applyGlobals(Object.fromEntries(p.entries()))", "0")
    check("ui/router (A5, mutation): a route() that stops applying the hash FAILS the check "
          "-- the comment above it is untouched", not p_route(m1))
    m1b = code.replace("applyGlobals(q)", "0")     # ONLY the URL branch; the other call stays
    check("ui/router (A5, mutation): deleting ONLY the deep-link branch fails too -- the "
          "surviving `applyGlobals` on the localStorage path must not vouch for it (R2-3)",
          not p_route(m1b))
    m2 = code.replace("a.after(c)", "c.classList.add('on')")
    check("ui/router (A5, mutation): a hostHero() that only restyles instead of MOVING the "
          "card fails -- two price panels is the defect it prevents", not p_hero(m2))
    m3 = code.replace("cursor:LG.cursor||''", "page:LG.page||0")
    check("ui/router (A5, mutation): a ledger that pages by number instead of by cursor fails",
          not p_cursor(m3))

    # and the router claims that used to be satisfied by prose are re-asserted on code
    for claim, needle in (("the hash is written back", "history.replaceState"),
                          ("state is mirrored to localStorage", "localStorage.setItem"),
                          ("a per-view default is skipped once he has chosen", "S.touched.range"),
                          ("only the active view's loaders run", "VIEW_LOADERS[S.view].map")):
        check(f"ui/router (A5): {claim} -- in the CODE, not in a comment", needle in code, needle)

# ===========================================================================
# BUG-20260910-067, second round (tech-lead B1/B2). SURVIVING a malformed store.
#
# The first round stopped the dashboard CREATING one. It did nothing about the
# incident's actual END state: with `data/analytics.sqlite` already malformed,
# `Dashboard.__init__` -> `Normalizer(writer=True)` -> `PRAGMA journal_mode=WAL`
# raises `sqlite3.DatabaseError` -- which `cmd_dashboard` did not catch -- so the
# dashboard died with a raw traceback and an undocumented exit 1, and KeepAlive
# repeated that every 30 s. The page an operator needs in order to LEARN what is
# wrong was the one thing a corrupt store took away.
#
# Every check below was written against the code as it stood after round 1 and
# fails there (docs/03 §9).
# ===========================================================================
def _garbage_store(db: Path) -> None:
    """A store at the store path that reproduces the incident's exact fault.

    Not random bytes and not a non-database: a REAL SQLite file whose schema page
    has been scribbled over, which is what a process killed mid-write leaves
    behind. It matters that it is this and not `b"junk"`, because the two fail
    differently and only one of them is BUG-20260910-067:

        junk with a valid magic  -> "file is not a database"
        interior pages corrupted -> opens fine; `quick_check` reports the fault
        PAGE 1 corrupted         -> "database disk image is malformed" on the
                                    first PRAGMA, out of `Normalizer.__init__`

    The third is what the Operator's 2.8 GB store did, and the one the degraded
    path exists for. `sqlite3.connect()` itself succeeds on all three -- it is
    lazy -- so the failure always lands on the first statement, never on open.
    """
    db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    import sqlite3 as _sq
    c = _sq.connect(str(db))
    c.executescript("CREATE TABLE t(a, b); CREATE INDEX ix ON t(a);")
    c.executemany("INSERT INTO t VALUES (?,?)", [(i, "x" * 200) for i in range(2000)])
    c.commit()
    c.close()
    raw = bytearray(db.read_bytes())
    for off in range(100, 4096):        # page 1 past the file header: the schema page
        raw[off] = 0x5A
    db.write_bytes(bytes(raw))
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)


def _serving(dash: Any):
    """Start a real HTTP server on a free port for `dash`; returns (port, httpd, get)."""
    import http.client
    import threading as _th
    from http.server import ThreadingHTTPServer

    from navanax.dashboard import make_handler
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dash))
    port = httpd.server_address[1]
    _th.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path: str):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body
    return port, httpd, get


def test_dashboard_serves_degraded_on_a_malformed_store(tmp: Path) -> None:
    """A corrupt store must not take the page down: it is where the recipe lives.

    Fails before this round: `Dashboard(...)` raises `sqlite3.DatabaseError` out
    of `__init__` on the first line of this test, so there is no page to ask.
    """
    import sqlite3 as _sq

    from navanax.dashboard import Dashboard

    root, cfg = _dash_root(tmp, "degraded")
    db = root / cfg["analytical"]["path"]
    _garbage_store(db)

    dash = None
    try:
        dash = Dashboard(root, cfg, ["argonauts"])
    except _sq.DatabaseError as exc:
        raised = f"raised {type(exc).__name__}: {exc}"
    else:
        raised = ""
    check("degraded: Dashboard comes up on a MALFORMED store instead of raising out of "
          "__init__ -- which is what became a raw traceback and an undocumented exit 1, "
          "repeated by KeepAlive every 30 s (BUG-20260910-067)", dash is not None, raised)
    if dash is None:
        return
    check("degraded: it knows it is degraded, and carries the fault and the recipe",
          dash.degraded() and dash.norm is None and dash.store_error is not None
          and "malformed" in (dash.store_error.get("error") or "").lower()
          and "DERIVED" in (dash.store_error.get("rebuild") or ""),
          str(dash.store_error)[:200])

    _port, httpd, get = _serving(dash)
    try:
        st, body = get("/api/health")
        h = json.loads(body) if st == 200 else {}
        check("degraded: /api/health still answers 200 -- the one page that explains the "
              "fault must not be the one the fault removes",
              st == 200 and isinstance(h, dict), f"{st} {body[:160]}")
        qc = h.get("quick_check") or {}
        check("degraded: ...and quick_check says the store is bad rather than quietly not "
              "running, and carries the rebuild recipe",
              qc.get("ok") is False and "rebuild" in qc
              and any("malformed" in r.lower() or "DatabaseError" in r
                      for r in qc.get("result") or []), str(qc)[:220])
        deg = h.get("degraded") or {}
        check("degraded: ...and Health names the fault, the store, and what to do about it -- "
              "move it aside under a dated name, never delete and never edit",
              deg.get("error") == "analytical store is malformed"
              and "corrupt-" in (deg.get("rebuild") or "")
              and "landing" in (deg.get("rebuild") or "").lower(), str(deg)[:220])
        check("degraded: every store-derived count is null, never 0 -- 0 is a claim about a "
              "store nobody could read",
              h["store"]["events"] is None and h["normalizer"]["files_read"] is None
              and h["crossed_book"]["alarms"] is None, str(h["store"])[:180])
        check("degraded: the RECORDER's state and the gap register still answer -- they come "
              "from the landing zone, which the fault has nothing to do with",
              "running" in h["recorder"] and "open" in h["gaps"]
              and h["store"]["landing_bytes"] > 0, str(h["gaps"])[:160])

        st, body = get("/api/series?metric=floor_ask&collection=argonauts&interval=1h&range=YTD")
        j = json.loads(body)
        check("degraded: a data endpoint answers 503 with the reason and the recipe -- not a "
              "500 with a sqlite traceback, and not a plausible-looking empty series",
              st == 503 and j.get("error") == "analytical store is malformed"
              and "rebuild" in j, f"{st} {body[:200]}")
        for path in ("/api/status", "/api/book?collection=argonauts", "/api/ledger?collection=argonauts",
                     "/api/wallets?collection=argonauts", "/api/traits?collection=argonauts",
                     "/api/audit"):
            st, _ = get(path)
            check(f"degraded: {path.split('?')[0]} -> 503", st == 503, str(st))
        for path in ("/api/meta", "/api/gaps"):
            st, _ = get(path)
            check(f"degraded: {path} still answers 200 -- it reads no analytical store", st == 200,
                  str(st))

        st, body = get("/")
        check("degraded: the page itself still loads, so there is somewhere to read all of "
              "the above", st == 200 and b"navanax" in body.lower(), str(st))
    finally:
        httpd.shutdown()
        httpd.server_close()
        dash.stop()


def test_dashboard_picks_up_a_rebuilt_store_without_a_restart(tmp: Path) -> None:
    """The recipe has to be ONE step. Move the corrupt file aside; the running
    dashboard folds a fresh store on its next probe. If the Operator also had to
    remember to restart it, half the time he would not, and the page would still
    be empty with nothing saying why.
    """
    from navanax.dashboard import STORE_REOPEN_SECONDS, Dashboard

    root, cfg = _dash_root(tmp, "rebuilt")
    db = root / cfg["analytical"]["path"]
    _garbage_store(db)
    dash = Dashboard(root, cfg, ["argonauts"])
    _port, httpd, get = _serving(dash)
    try:
        st, _ = get("/api/series?metric=floor_ask&collection=argonauts&interval=1h&range=YTD")
        check("rebuild: while the store is bad, the series endpoint is 503", st == 503, str(st))
        check("rebuild: the probe interval is a minute -- long enough not to hammer a big "
              "store, short enough that 'it comes back on its own' is true",
              STORE_REOPEN_SECONDS == 60, str(STORE_REOPEN_SECONDS))

        # What rebuild-store.command does: move it aside. Nothing else.
        db.rename(db.with_name(db.name + ".corrupt-test"))
        dash._reopen_mono = 0.0          # let the next tick probe immediately
        dash._retry_open()

        check("rebuild: one probe tick later the dashboard has opened a fresh store and is "
              "the folding writer again -- no restart, no second step",
              not dash.degraded() and dash.norm is not None and dash.store_error is None,
              str(dash.store_error)[:160])
        st, body = get("/api/series?metric=floor_ask&collection=argonauts&interval=1h&range=YTD")
        check("rebuild: ...and the data endpoints are 200 again", st == 200, f"{st} {body[:160]}")
        st, body = get("/api/health")
        h = json.loads(body)
        check("rebuild: ...and Health drops the degraded block and reports a clean store, "
              "re-checked rather than served from the verdict about the OLD file",
              h.get("degraded") is None and h["quick_check"]["ok"] is True
              and h["store"]["events"] == 3, str(h.get("quick_check"))[:180])
        check("rebuild: the corrupt file was MOVED, never deleted -- it is evidence until "
              "the Operator says otherwise", db.with_name(db.name + ".corrupt-test").exists())
    finally:
        httpd.shutdown()
        httpd.server_close()
        dash.stop()
        if dash.norm is not None:
            dash.norm.close()


def test_cmd_dashboard_maps_an_unreadable_store_to_a_documented_exit_code(tmp: Path) -> None:
    """The belt to the degradation's braces. A `sqlite3.DatabaseError` that
    somehow still escapes `serve()` must be a NAMED exit code and a sentence, not
    exit 1 and a stack trace -- because under KeepAlive that is a silent loop.
    """
    import argparse
    import contextlib
    import io
    import sqlite3 as _sq

    from navanax import cli as cli_mod

    root, cfg = _dash_root(tmp, "exitcode")
    _garbage_store(root / cfg["analytical"]["path"])

    import navanax.dashboard as dash_mod
    real_dash = dash_mod.Dashboard

    class Undegradable(real_dash):        # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            raise _sq.DatabaseError("database disk image is malformed")

    dash_mod.Dashboard = Undegradable
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            rc = cli_mod.cmd_dashboard(argparse.Namespace(root=str(root), port=0, no_browser=True))
    finally:
        dash_mod.Dashboard = real_dash
    msg = err.getvalue()
    check("exit code: an unreadable store the dashboard cannot degrade around exits 5, a "
          "DOCUMENTED code -- never 1 with a traceback, which KeepAlive repeats in silence",
          rc == 5 and rc == cli_mod.DASH_EXIT_STORE_MALFORMED, f"exit {rc}")
    check("exit code: ...and the message is the same recipe the page would have given, not "
          "sqlite's stack",
          "UNREADABLE" in msg and "DERIVED" in msg and "corrupt-" in msg
          and "Traceback" not in msg, msg[:240])
    codes = {cli_mod.DASH_EXIT_OK, cli_mod.DASH_EXIT_REFUSED,
             cli_mod.DASH_EXIT_STORE_BUSY, cli_mod.DASH_EXIT_STORE_MALFORMED}
    check("exit code: the four dashboard codes are distinct, so a supervisor log says which "
          "fault happened", len(codes) == 4, str(sorted(codes)))
    doc = (ROOT / "docs" / "04_ENVIRONMENTS.md").read_text()
    block = doc.split("`dashboard` owes launchd", 1)[-1][:1400]
    check("exit code: docs/04's contract table lists every one of them, so the number in the "
          "log can be looked up",
          all(f"| `{c}` |" in block for c in sorted(codes)),
          [c for c in sorted(codes) if f"| `{c}` |" not in block])


def test_serve_releases_the_writer_lock_when_startup_fails_late(tmp: Path) -> None:
    """tech-lead B2. `serve()`'s failure path closed the socket and nothing else.

    If the failure lands AFTER the Normalizer opened -- `make_handler`, `start()`,
    a Ctrl-C during the first fold -- the store's exclusive lock is held by an
    object nobody will ever close, and a retry inside the same process is refused
    by our own stale lock. The OS releases it when the process ends, which is
    exactly why this is easy to miss and worth a test.
    """
    from navanax import dashboard as dash_mod
    from navanax.normalize import Normalizer, StoreWriterBusyError, writer_lock_path

    root, cfg = _dash_root(tmp, "latefail")
    db = root / cfg["analytical"]["path"]
    real_make = dash_mod.make_handler

    def boom(dash: Any):                  # fails AFTER Dashboard opened the store
        raise RuntimeError("handler construction failed")

    dash_mod.make_handler = boom
    raised: BaseException | None = None
    try:
        dash_mod.serve(root, cfg, ["argonauts"], host="127.0.0.1", port=0, open_browser=False)
    except RuntimeError as exc:
        raised = exc
    finally:
        dash_mod.make_handler = real_make
    check("late failure: the original error propagates -- the cleanup must not swallow it",
          isinstance(raised, RuntimeError) and "handler construction" in str(raised), repr(raised))
    check("late failure: the store's writer lock file was written, so the store really was "
          "opened before the failure", writer_lock_path(db).exists())
    freed = True
    try:
        n = Normalizer(root / cfg["landing"]["root"], db)
        n.close()
    except StoreWriterBusyError as exc:
        freed = False
        detail = str(exc)[:160]
    check("late failure: ...and it was RELEASED on the way out, so a retry in this same "
          "process is not refused by our own stale lock (tech-lead B2)",
          freed, "" if freed else detail)


def test_rebuild_store_command_moves_aside_and_refuses_a_live_writer(tmp: Path) -> None:
    """The double-click. It must be safe in the one case that matters -- a
    dashboard is running and has the file open -- and it must never delete."""
    import os as _os
    import subprocess

    from navanax.normalize import Normalizer

    cmd = ROOT / "rebuild-store.command"
    check("rebuild.command: exists and is executable",
          cmd.exists() and _os.access(cmd, _os.X_OK))
    text = cmd.read_text()
    check("rebuild.command: never deletes, and says so -- it renames, and the old file is "
          "the Operator's to bin",
          "rm " not in text and "unlink" not in text and "Trash" in text and ".rename(" in text)
    check("rebuild.command: says the landing zone is untouched, because that is the fact "
          "that makes a rebuild safe rather than a loss",
          "landing" in text.lower() and "DERIVED" in text)

    root, cfg = _dash_root(tmp, "rebuildcmd")
    db = root / cfg["analytical"]["path"]
    n = Normalizer(root / cfg["landing"]["root"], db)      # a live "dashboard" holding the lock
    n.sync()
    env = {**_os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run(["bash", str(cmd), str(root)], env=env, input="\n",
                       capture_output=True, text=True, timeout=120)
    check("rebuild.command: REFUSES while a live process holds the fold-writer lock -- "
          "moving a store out from under a writer is how one corrupt store becomes two",
          r.returncode == 3 and "REFUSING" in r.stdout and str(_os.getpid()) in r.stdout,
          (r.stdout + r.stderr)[-300:])
    check("rebuild.command: ...and names the pid and the command that stops it, and says "
          "nothing was changed",
          f"kill {_os.getpid()}" in r.stdout and "Nothing has been changed" in r.stdout
          and db.exists(), r.stdout[-300:])

    n.close()                                              # the dashboard quits
    r2 = subprocess.run(["bash", str(cmd), str(root)], env=env, input="\n",
                        capture_output=True, text=True, timeout=120)
    aside = list(db.parent.glob(db.name + ".corrupt-*"))
    check("rebuild.command: with no live writer it moves the store aside under a DATED name "
          "and leaves the path free for a fresh fold",
          r2.returncode == 0 and not db.exists() and len(aside) == 1,
          f"rc={r2.returncode} aside={[p.name for p in aside]} {r2.stdout[-200:]}")
    check("rebuild.command: ...and tells the Operator the running dashboard rebuilds by "
          "itself, so there is no second step to forget",
          "within a minute" in r2.stdout and "re-folds" in r2.stdout, r2.stdout[-300:])
    check("rebuild.command: it is a rebuild, not a repair -- nothing in it tries to fix the "
          "corrupt file in place", "recover" not in r2.stdout.lower())


def test_quick_check_reports_a_malformed_store_instead_of_raising(tmp: Path) -> None:
    """`store_quick_check` has to work in the degraded case, where there is no
    connection to ask. It opens a throwaway read-only one and lets it fail, which
    is the honest answer -- a check that quietly does not run reads as 'fine'."""
    from navanax.dashboard import Dashboard

    root, cfg = _dash_root(tmp, "qcbad")
    db = root / cfg["analytical"]["path"]
    _garbage_store(db)
    dash = Dashboard(root, cfg, ["argonauts"])
    try:
        qc = dash.store_quick_check()
        check("quick_check: on a store that cannot even be OPENED it returns a failed check "
              "rather than raising -- Health renders it, so raising would take the page down "
              "at the moment the page matters most",
              qc.get("ok") is False and qc.get("result"), str(qc)[:200])
        check("quick_check: ...and it names the sqlite fault, so the Health page shows what "
              "the log shows",
              any("malformed" in r.lower() or "not a database" in r.lower()
                  or "DatabaseError" in r for r in qc["result"]), str(qc["result"]))
        check("quick_check: ...and a failed check carries the rebuild recipe, because the "
              "next question after 'it is corrupt' is always 'so what do I do'",
              "corrupt-" in (qc.get("rebuild") or ""), str(qc.get("rebuild"))[:160])
        cached = dash.store_quick_check()
        check("quick_check: the failed verdict is cached like any other, so a Health tab left "
              "open does not re-probe a broken file every refresh",
              cached.get("cached") is True and cached.get("at") == qc.get("at"))
    finally:
        dash.stop()


def test_degraded_page_and_docs_carry_the_rebuild_recipe(tmp: Path) -> None:
    """One recipe, in one place, said the same way by the page, the CLI, the
    docs and the double-click. A recovery procedure that four artefacts state
    four ways is a procedure nobody follows correctly under pressure."""
    from navanax.normalize import rebuild_recipe

    recipe = rebuild_recipe(Path("/x/data/analytics.sqlite"))
    check("recipe: it is derived from the store path, names a DATED destination, and says "
          "the landing zone is untouched",
          "corrupt-" in recipe and "/x/data/analytics.sqlite" in recipe
          and "DERIVED" in recipe and "landing" in recipe.lower(), recipe[:200])
    check("recipe: it forbids the two things that would make the loss real -- deleting it, "
          "and editing it", "Never delete it, never edit it" in recipe, recipe[-160:])

    html = (ROOT / "src" / "navanax" / "ui" / "index.html").read_text()
    check("recipe/ui: Health renders the degraded block with the fault AND the recipe, above "
          "the stat row rather than below it",
          "hz-degraded" in html and "h.degraded" in html
          and html.index('id="hz-degraded"') < html.index('id="hz-stats"'))
    check("recipe/ui: the degraded block escapes the server strings it interpolates -- it "
          "goes to innerHTML, which is the boundary esc() exists for",
          "esc(h.degraded.rebuild" in html and "esc(h.degraded.detail" in html)

    cmd = (ROOT / "rebuild-store.command").read_text()
    doc7 = (ROOT / "docs" / "07_STORAGE_AND_RECORDING.md").read_text()
    doc8 = (ROOT / "docs" / "08_DASHBOARD.md").read_text()
    check("recipe/docs: rebuild-store.command is named in docs/07 and docs/08, so the recipe "
          "on the page and the recipe in the documents are the same recipe",
          "rebuild-store.command" in doc7 and "rebuild-store.command" in doc8)
    check("recipe/docs: ...and all four say the same dated-rename move",
          all("corrupt-" in t for t in (recipe, cmd, doc7, doc8)))
    check("recipe/docs: docs/08 documents DEGRADED mode -- what still answers, what 503s, "
          "and that it retries on its own",
          "degraded" in doc8.lower() and "503" in doc8)



def test_degraded_reopen_probe_is_not_throttled_by_refresh_seconds(tmp: Path) -> None:
    """R2-5. The reopen probe ran on the FOLD tick, so `refresh_seconds` capped it.

    `_retry_open`'s own `STORE_REOPEN_SECONDS` rate limit could only make the probe
    LESS frequent than `refresh`, never more. At the documented `refresh_seconds:
    3600` a rebuilt store therefore sat unnoticed for up to an hour, and
    `rebuild-store.command` -- whose entire claim is "move the file aside and the
    running dashboard picks it up" -- silently stopped being a one-click fix.

    The two settings answer different questions and must not be one number:
    `refresh` is how stale the MARKET DATA may be; `STORE_REOPEN_SECONDS` is how
    long the Operator waits after fixing the store. An operator who sets an hourly
    fold is not asking to wait an hour after a rebuild.

    This drives the real `_loop` thread rather than calling `_retry_open()`
    directly -- the defect is in the WAIT, and a test that calls the probe by hand
    cannot see it. `STORE_REOPEN_SECONDS` is overridden small so the suite does not
    sleep; at the shipped value of 60 s the same arithmetic gives recovery within
    ~70 s on an hourly fold, versus up to 3600 s before.
    """
    import time as _t

    from navanax import dashboard as dash_mod
    from navanax.dashboard import Dashboard

    root, cfg = _dash_root(tmp, "reopen-throttle")
    db = root / cfg["analytical"]["path"]
    _garbage_store(db)
    cfg = json.loads(json.dumps(cfg))
    cfg.setdefault("dashboard", {})["refresh_seconds"] = 3600      # the documented slow fold

    real = dash_mod.STORE_REOPEN_SECONDS
    dash = None
    try:
        dash_mod.STORE_REOPEN_SECONDS = 0.05
        dash = Dashboard(root, cfg, ["argonauts"])
        check("reopen: the dashboard came up DEGRADED on the corrupt store, as it must",
              dash.degraded() and dash.store_error is not None, str(dash.store_error)[:120])
        check("reopen: ...with the hourly fold interval the Operator configured",
              dash.refresh == 3600.0, str(dash.refresh))
        dash.start()
        db.rename(db.with_name(db.name + ".corrupt-test"))          # rebuild-store.command
        deadline = _t.monotonic() + 20
        while dash.degraded() and _t.monotonic() < deadline:
            _t.sleep(0.05)
        check("reopen: the probe fires on ITS OWN interval, not on the fold interval -- the "
              "rebuilt store is picked up without waiting an hour and without a restart (R2-5)",
              not dash.degraded() and dash.norm is not None and dash.store_error is None,
              f"still degraded after 20 s of wall clock with refresh={dash.refresh}s; "
              f"{str(dash.store_error)[:160]}")
        check("reopen: at the SHIPPED constant the same arithmetic is min(3600, 60) = 60 s per "
              "probe, so an hourly fold recovers inside ~70 s rather than inside an hour",
              real == 60 and min(3600.0, real) == 60, f"STORE_REOPEN_SECONDS={real}")
    finally:
        if dash is not None:
            dash.stop(timeout=5.0)
            if dash.norm is not None:
                dash.norm.close()
        dash_mod.STORE_REOPEN_SECONDS = real

    # The property, read off the loop itself: while degraded the wait is bounded by
    # STORE_REOPEN_SECONDS. Asserted on the source as well as behaviourally, because
    # the behavioural half needs a real thread and a real clock and would be the
    # first thing deleted if it ever went flaky.
    src = (ROOT / "src" / "navanax" / "dashboard.py").read_text()
    body = src.split("def _loop(self)", 1)[1].split("def _retry_open", 1)[0]
    check("reopen: `_loop` waits min(refresh, STORE_REOPEN_SECONDS) while it has no store, "
          "and the plain refresh once it does",
          "min(self.refresh, STORE_REOPEN_SECONDS)" in body
          and "self.norm is None" in body, body[:200])

# ===========================================================================
# The double-click files the Operator actually uses, RUN rather than read.
#
# There are exactly two he touches day to day, and the split between them is
# the whole point: open-dashboard.command LOOKS and update.command DEPLOYS.
# A second dashboard started by hand beside the background one is what
# corrupted the analytical store on 2026-09-10 (BUG-20260910-067), so "the
# viewer never starts anything" is a correctness property, not tidiness.
#
# These tests run the real scripts in a sandbox. Two names are rewritten in the
# sandbox copy before it runs, and it matters that both are rewrites of the
# real file rather than a re-implementation of it:
#
#   launchctl -> a name that is not on any PATH, so the "no launchctl here"
#       branch runs on a Mac as well as on Linux. Without this, running the
#       suite on the Operator's own machine would `bootout` his live recorder
#       -- a test that stops production recording is worse than no test, and
#       the hours it loses cannot be bought back (docs/04 §8.1).
#
#   8765 -> a port this test owns, so the assertions do not depend on whether
#       the real dashboard happens to be up, and nothing here can reach it.
# ===========================================================================

_GIT_ID = ("-c", "user.email=selftest@navanax.local",
           "-c", "user.name=Navanax Selftest",
           "-c", "commit.gpgsign=false")


def _git(cwd: Path, env: dict, *args: str):
    import subprocess
    return subprocess.run(["git", *_GIT_ID, *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=180)


def _fake_home_env(home: Path) -> dict:
    """An environment whose HOME (and git config) is a throwaway directory.

    update.command writes to `$HOME/Library/LaunchAgents` on the paths that get
    that far, and git reads `$HOME/.gitconfig`. Both are redirected so a test
    run cannot touch the real ones.
    """
    (home / ".config").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(var, None)
    return env


def _sandbox_script(name: str, dest: Path, port: int) -> str:
    """Copy the REAL .command file, rewriting `launchctl` and the port.

    Returns the rewritten text. See the block comment above for why these two
    substitutions exist; everything else -- every branch, every refusal, every
    message -- is the shipped file's own.
    """
    text = (ROOT / name).read_text()
    text = text.replace("launchctl", "launchctl-not-on-this-machine")
    text = text.replace("8765", str(port))
    dest.write_text(text)
    dest.chmod(0o755)
    return text


def _init_repo(path: Path, env: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    r = _git(path, env, "init", "-q", "-b", "main")
    if r.returncode != 0:                          # git older than 2.28
        _git(path, env, "init", "-q")
        _git(path, env, "symbolic-ref", "HEAD", "refs/heads/main")


def _update_sandbox(tmp: Path, name: str, port: int) -> tuple[Path, Path, dict]:
    """A fake origin with two commits on main, and a clone of it at commit one.

    The clone is what update.command runs in, so `git pull --ff-only` has a real
    remote to fast-forward from. update.command is COMMITTED into the repo
    rather than dropped beside it -- an untracked copy of the script would make
    the working tree dirty and the script would (correctly) refuse every time.
    """
    base = tmp / name
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = _fake_home_env(home)

    origin = base / "origin"
    _init_repo(origin, env)
    (origin / ".gitignore").write_text("data/\n")
    (origin / "README.md").write_text("# Sandbox\n")
    _sandbox_script("update.command", origin / "update.command", port)
    _git(origin, env, "add", "-A")
    _git(origin, env, "commit", "-q", "-m", "initial commit")

    live = base / "live"
    _git(base, env, "clone", "-q", str(origin), str(live))

    # The commit the update will pull. It deliberately touches neither
    # pyproject.toml nor tools/launchd.py, so the conditional steps stay off.
    (origin / "README.md").write_text("# Sandbox\nthe line the update brings\n")
    _git(origin, env, "add", "-A")
    _git(origin, env, "commit", "-q", "-m", "feat(sandbox): the commit update.command pulls")
    return live, origin, env


def test_update_command_refuses_a_dirty_tree_and_a_branch_that_is_not_main(tmp: Path) -> None:
    """Both refusals must fire before anything is pulled, and both must name
    what is wrong specifically enough to act on.

    The branch refusal is the load-bearing one. The live checkout is shared with
    whatever Claude session is working in it, and that session may have it parked
    on its own branch mid-review. A deploy script that "helpfully" switches to
    main throws that work away silently, so this one refuses and hands the
    Operator a sentence to send instead.
    """
    import subprocess

    script = ROOT / "update.command"
    check("update.command: exists and is executable",
          script.exists() and os.access(script, os.X_OK))
    text = script.read_text()
    check("update.command: never switches, merges or resets the branch itself -- the only "
          "git command that moves this checkout is a fast-forward pull",
          "git checkout" not in text and "git switch" not in text
          and "git merge" not in text and "git reset" not in text
          and "--ff-only" in text)
    check("update.command: guards launchctl, which does not exist off macOS",
          "command -v launchctl" in text)

    if shutil.which("git") is None:
        check("update.command: refusals exercised", True, "skipped: no git on this machine")
        return

    live, _origin, env = _update_sandbox(tmp, "update-refuse", 1)

    # 1. A dirty tree. A tracked file edited and not committed.
    (live / "README.md").write_text("# Sandbox\nan edit the Operator made by hand\n")
    r = subprocess.run(["bash", str(live / "update.command")], input="\n", env=env,
                       capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    check("update: refuses a dirty working tree with exit 1 rather than pulling over "
          "uncommitted edits",
          r.returncode == 1 and "REFUSED" in out, f"rc={r.returncode} {out[-400:]}")
    check("update: ...and NAMES the files, so the Operator can tell Claude what is in the way",
          "README.md" in out and "not committed" in out, out[-400:])
    check("update: ...and nothing was pulled -- the clone is still on the first commit",
          _git(live, env, "log", "--oneline").stdout.count("\n") == 1,
          _git(live, env, "log", "--oneline").stdout)

    # 2. A branch that is not main, on a clean tree.
    _git(live, env, "checkout", "-q", "--", "README.md")
    _git(live, env, "checkout", "-q", "-b", "feat/explorer-panes")
    r2 = subprocess.run(["bash", str(live / "update.command")], input="\n", env=env,
                        capture_output=True, text=True, timeout=180)
    out2 = r2.stdout + r2.stderr
    check("update: refuses a checkout that is not on main, with exit 1",
          r2.returncode == 1 and "REFUSED" in out2, f"rc={r2.returncode} {out2[-400:]}")
    check("update: ...printing the exact sentence to send Claude, with the real branch name "
          "in it (Claude cannot fix a branch it has not been told the name of)",
          "the live checkout is on branch feat/explorer-panes; switch it to main" in out2,
          out2[-500:])
    check("update: ...and it did NOT switch the branch itself",
          _git(live, env, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
          == "feat/explorer-panes")


def test_update_command_fast_forwards_prints_the_range_and_reads_health(tmp: Path) -> None:
    """The happy path, end to end, against a fake origin and a fake dashboard.

    Three things have to be true and are all easy to get wrong:
      * the pull is a fast-forward and the Operator is shown WHICH commits
        arrived -- "it updated" is not a reviewable statement;
      * with no launchctl (any machine that is not a Mac) it says so and the
        run still succeeds, because the code on disk really is updated;
      * data/ is not read, written or moved. The record is the one thing in
        this project that cannot be recreated.
    """
    import http.server
    import json as _json
    import subprocess
    import threading

    if shutil.which("git") is None:
        check("update.command: fast-forward exercised", True, "skipped: no git on this machine")
        return

    body = _json.dumps({
        "quick_check": {"ok": True, "result": "ok", "seconds": 0.4},
        "store_writer": {"pid": 4242, "alive": True, "since": "2026-09-11T00:00:00Z"},
        "unrelated": "ignored",
    }).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                    # noqa: N802
            if self.path != "/api/health":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):                           # keep the suite quiet
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        live, _origin, env = _update_sandbox(tmp, "update-ff", port)

        # The irreplaceable half of the project, in the sandbox. gitignored, so
        # it does not make the tree dirty -- exactly as data/ is in the real one.
        landing = live / "data" / "landing"
        landing.mkdir(parents=True, exist_ok=True)
        frame = landing / "2026-09-11T00.jsonl.zst"
        frame.write_bytes(b"an irreplaceable frame")
        before = (frame.read_bytes(), frame.stat().st_mtime_ns)

        old = _git(live, env, "rev-parse", "--short", "HEAD").stdout.strip()
        r = subprocess.run(["bash", str(live / "update.command")], input="\n", env=env,
                           capture_output=True, text=True, timeout=300)
        out = r.stdout + r.stderr
        new = _git(live, env, "rev-parse", "--short", "HEAD").stdout.strip()

        check("update: on a clean main it fast-forwards and exits 0",
              r.returncode == 0 and old != new, f"rc={r.returncode} {out[-500:]}")
        check("update: ...and prints the OLD..NEW range and the commits in it, so the Operator "
              "can see what he just deployed",
              f"{old}..{new}" in out
              and "feat(sandbox): the commit update.command pulls" in out, out[-700:])
        check("update: with no launchctl it SAYS SO and still succeeds -- the code on disk is "
              "updated either way, and a hard failure here would read as 'the update broke'",
              "is not available on this machine" in out and "NOT restarted" in out,
              out[-700:])
        check("update: no shell error leaked from the missing launchctl",
              "command not found" not in out, out[-400:])
        check("update: reads /api/health and prints quick_check -- the store's own verdict, "
              "which is a different claim from 'the job is running'",
              "quick_check" in out and '"result": "ok"' in out, out[-600:])
        check("update: ...and store_writer, which names the process holding the fold-writer "
              "lock (BUG-20260910-067)",
              "store_writer" in out and "4242" in out, out[-600:])
        check("update: points the Operator at the viewer rather than at a dashboard he would "
              "have to start himself",
              "open-dashboard.command to view" in out, out[-400:])
        check("update: pyproject.toml did not change in that commit, so it did not reinstall",
              "pyproject.toml unchanged" in out, out[-600:])
        check("update: tools/launchd.py did not change either, so no plist was re-rendered",
              "Generator unchanged" in out, out[-600:])
        check("update: the landing zone under data/ is byte-for-byte untouched, including its "
              "mtime -- an update changes code, never the record",
              frame.exists() and (frame.read_bytes(), frame.stat().st_mtime_ns) == before)

        # The stub above answers with the field names update.command asks for. That
        # proves the script reads them; it does not prove the dashboard still emits
        # them. Rename either key in dashboard.py and the script would quietly print
        # "(absent from /api/health)" on a real machine, which reads as "fine".
        api = (ROOT / "src" / "navanax" / "dashboard.py").read_text()
        script = (ROOT / "update.command").read_text()
        for key in ("quick_check", "store_writer"):
            check(f"update: /api/health really does carry `{key}` -- the script and the "
                  f"endpoint name the same field",
                  f'"{key}":' in api and f'"{key}"' in script)
    finally:
        srv.shutdown()
        srv.server_close()


def test_open_dashboard_command_looks_and_never_starts_a_dashboard(tmp: Path) -> None:
    """The viewer. It answers one question -- is something listening on 8765 --
    and either opens the page or explains why it cannot.

    What it must never do is start a dashboard. On 2026-09-10 a hand-started
    dashboard beside the background one gave two writers on one SQLite store and
    left it `database disk image is malformed` (BUG-20260910-067). The fix in the
    code binds before it opens the store; this is the fix in the Operator's
    hands, and it only holds as long as this file stays a viewer.
    """
    import socket as _socket
    import subprocess

    real = ROOT / "open-dashboard.command"
    check("open-dashboard.command: exists and is executable",
          real.exists() and os.access(real, os.X_OK))
    text = real.read_text()
    check("open-dashboard.command: never starts a dashboard -- it does not invoke the CLI at "
          "all, so there is no path through it that becomes a second writer (BUG-20260910-067)",
          "navanax.cli dashboard" not in text and "navanax.cli" not in text)

    sandbox = tmp / "opendash"
    (sandbox / "bin").mkdir(parents=True, exist_ok=True)
    opened = sandbox / "opened.txt"
    stub = sandbox / "bin" / "open"
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$1" >> "' + str(opened) + '"\n')
    stub.chmod(0o755)

    listener = _socket.socket()
    listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]

    script = sandbox / "open-dashboard.command"
    _sandbox_script("open-dashboard.command", script, port)
    env = _fake_home_env(sandbox / "home")
    env["PATH"] = f"{sandbox / 'bin'}{os.pathsep}{env.get('PATH', '')}"

    try:
        r = subprocess.run(["bash", str(script)], input="\n", env=env,
                           capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        check("open-dashboard: with something listening it reports running and opens the page",
              r.returncode == 0 and "is running" in out, f"rc={r.returncode} {out[-400:]}")
        check("open-dashboard: ...handing the browser the loopback URL and nothing else",
              opened.exists()
              and opened.read_text().strip() == f"http://127.0.0.1:{port}/",
              opened.read_text() if opened.exists() else "(open was never called)")
    finally:
        listener.close()

    r2 = subprocess.run(["bash", str(script)], input="\n", env=env,
                        capture_output=True, text=True, timeout=120)
    out2 = r2.stdout + r2.stderr
    check("open-dashboard: with nothing listening it says NOT running rather than starting one",
          "NOT running" in out2, out2[-500:])
    check("open-dashboard: ...and says in the window that it will not start one itself, so the "
          "Operator does not go looking for a file that would",
          "never starts a dashboard" in out2, out2[-500:])
    check("open-dashboard: ...and did not open a browser at a page that is not there",
          opened.read_text().count("\n") == 1, opened.read_text())


def test_dashboard_webloc_is_a_plist_bookmark_to_the_loopback_dashboard() -> None:
    """The Dock/Finder bookmark. A .webloc is a property list, and a malformed
    one fails the way a bad plist always does -- silently, by doing nothing.
    """
    import plistlib

    p = ROOT / "Navanax Dashboard.webloc"
    check("webloc: exists", p.exists())
    if not p.exists():
        return
    try:
        data = plistlib.loads(p.read_bytes())
        parsed = True
    except Exception as exc:                      # noqa: BLE001 - the point is that it parses
        data, parsed = {}, False
        check("webloc: parses as a property list", False, f"{type(exc).__name__}: {exc}")
    if parsed:
        check("webloc: parses as a property list with a URL key",
              isinstance(data, dict) and "URL" in data, repr(data)[:200])
    check("webloc: points at the loopback dashboard on 8765 -- loopback, because the dashboard "
          "serves this computer only (REQ-N-13)",
          data.get("URL") == "http://127.0.0.1:8765/", repr(data.get("URL")))


def test_readme_and_docs_name_the_files_the_operator_double_clicks() -> None:
    """A launcher that exists and is documented nowhere is a launcher nobody
    uses. BUG-20260909-003 was a README that had gone stale against the code.
    """
    readme = (ROOT / "README.md").read_text()
    for name in ("open-dashboard.command", "update.command", "Navanax Dashboard.webloc"):
        check(f"README names {name}", name in readme)
    doc = (ROOT / "docs" / "04_ENVIRONMENTS.md").read_text()
    check("docs/04 §8 separates viewing from running, which is the distinction that keeps a "
          "second dashboard from ever being started by hand",
          "Viewing vs running" in doc and "open-dashboard.command" in doc)
    check("docs/04 §8 documents the update step, including that it never switches branches",
          "Updating" in doc and "update.command" in doc)


if __name__ == "__main__":
    raise SystemExit(main())
