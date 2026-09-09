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
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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


if __name__ == "__main__":
    raise SystemExit(main())
