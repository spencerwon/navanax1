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
    # Scan the files the repo actually CLAIMS -- i.e. tracked files. A
    # generated artifact sitting in the working tree is output, not an
    # assertion; a tracked one is an assertion. `tools/preflight_report.json`
    # was both at once until it was untracked, which is why it is now
    # gitignored: the report the documents get corrected FROM must not itself
    # become a stale claim in the repo.
    import subprocess
    skip_dirs = {".git", "__pycache__", ".venv", "node_modules", "data"}
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                 text=True, timeout=30, check=True).stdout.split()
        candidates = [ROOT / t for t in tracked]
    except (OSError, subprocess.SubprocessError):
        candidates = sorted(ROOT.rglob("*"))
    offenders = []
    for f in candidates:
        if not f.is_file() or f.suffix.lower() in {".xlsx", ".gz", ".zst", ".db", ".pyc"}:
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
        for i, line in enumerate(lines, 1):
            if asserts.search(line) and not corrects.search(line):
                offenders.append(f"{name}:{i}: {line.strip()[:70]}")
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
    s5 = eng.series(metric="immediacy_cost", collection="argonauts", interval="5m", range_="6h", now=now)
    check("metrics: at 5m the offer and the listing fall in different buckets -> UNDEFINED, not filled",
          all(v is None for v in s5["raw"]) and s5["basis"]["undefined_buckets"] == len(s5["raw"]) and len(s5["raw"]) == 72,
          f"got {len(s5['raw'])}")
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
    check("metrics: bid lifetime = cancel.valid_ts - bid.valid_ts, with its n",
          lt["n"] == 1 and abs(lt["median_s"] - 22.49) < 0.01 and lt["percentiles_reliable"] is False, f"got {lt}")
    n.close()


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
                     "/api/lifetimes?collection=argonauts&range=YTD", "/api/mix?range=YTD", "/api/gaps", "/api/audit"):
            st, body = get(path)
            check(f"dashboard: {path.split('?')[0]} -> 200 JSON", st == 200 and body[:1] in (b"{", b"["),
                  f"{st} {body[:100]}")
        st, body = get("/api/series?metric=nope&collection=argonauts")
        check("dashboard: a bad request is a 400 with the reason, not a crash",
              st == 400 and b"unknown metric" in body)
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
    none = {"Background": ["Red"]}
    check("trait metrics: filtered to a trait no token has -> only the collection offer = 1 (F2)",
          last(eng.series(metric="bid_count", collection="argonauts", interval="1h", range_="6h", now=now, traits=none)) == 1.0)
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
    check("ui: hover cards have an explicit high-contrast background and font", "hoverlabel:{bgcolor:'#1A231E'" in html and "namelength:-1" in html)
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
    check("bid lifetimes: n counts ORDERS, and the duration is to the FIRST termination",
          lt["n"] == 1 and lt["median_s"] == 10.0, str(lt))
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
    check("trait matching: the PROPERTY TEST -- a trait offer with criteria_n = 0 matches NOTHING, "
          "under every filter (dataeng §4.3: with no criteria rows the NOT EXISTS is vacuously "
          "true and it would otherwise match every filter and every token)",
          all("0xbad" not in covered(f) for f in every_filter)
          and counted({"Print": ["Unclaimed"]}) == 2.0 and counted({"Nothing": ["At all"]}) == 1.0,
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


if __name__ == "__main__":
    raise SystemExit(main())
