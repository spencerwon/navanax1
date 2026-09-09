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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from navanax.codec import GzipCodec  # noqa: E402
from navanax.errors import NavanaxError  # noqa: E402
from navanax.governor import Priority, RestGovernor, TokenBucket  # noqa: E402
from navanax.landing import LandingZoneWriter, read_file, verify_manifest  # noqa: E402
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

    d = tmp / "envtest"; d.mkdir(parents=True, exist_ok=True)
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
        load(d / "rtf.env"); ok = False
    except DotenvError as exc:
        ok = "Rich Text" in str(exc) and "Make Plain Text" in str(exc)
    check("dotenv: detects TextEdit RTF and says how to fix it", ok,
          "an RTF .env is invisible in the editor and must be caught by bytes")

    (d / "missing").mkdir(exist_ok=True)
    try:
        require("OPENSEA_API_KEY", path=d / "missing" / ".env"); ok2 = False
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
        # asyncio.TimeoutError and builtin TimeoutError are the SAME class on
        # Python 3.11+ and DIFFERENT classes on 3.10. This is the stdlib-only
        # smoke test and must run anywhere, so list both; on 3.11+ the tuple
        # simply contains a duplicate, which is harmless.
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
    class NullWriter:
        run_id = "run-g"
        def __init__(self): self.landed: list[tuple[str, str | None]] = []
        def write(self, raw, topic=None, event_timestamp=None, received_at=None):
            self.landed.append((raw, event_timestamp)); return len(self.landed)
        def flush(self): pass
        def record_gap(self, gap): pass

    class NullStore:
        def open_gap(self, *a, **k): return 1
        def close_gap(self, *a, **k): pass

    w = NullWriter()
    c = StreamConsumer("fake-key", ["argonauts"], w, NullStore())  # type: ignore[arg-type]

    f1 = frame("argonauts", "2026-09-09T10:00:00Z")
    c.handle_frame(f1)
    check("stream: extracts nested event_timestamp",
          w.landed[-1][1] == "2026-09-09T10:00:00Z", f"got {w.landed[-1][1]}")
    check("stream: lands frame verbatim", w.landed[-1][0] == f1)

    c.handle_frame('{"topic":"phoenix","event":"heartbeat","payload":{},"ref":"1"}')
    check("stream: heartbeat not counted as event", c.stats.events == 1)
    check("stream: heartbeat counted", c.stats.heartbeats == 1)

    c.handle_frame("{not json")
    check("stream: unparseable frame STILL landed", len(w.landed) == 2,
          "discarding it would destroy the evidence needed to diagnose it")

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
            self.landed.append((raw, topic, event_timestamp)); return len(self.landed)
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


def test_error_hierarchy() -> None:
    from navanax.errors import (
        BacktestIntegrityError, DataIntegrityError, InsufficientSampleError,
        LeakageDetectedError, Severity,
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
    tmp = Path(tempfile.mkdtemp(prefix="navanax-selftest-"))
    try:
        print("=" * 72)
        print("NAVANAX PHASE 0 SELF-TEST  (stdlib only: gzip codec stands in for zstd)")
        print("=" * 72)
        for fn in (
            test_roundtrip_and_verbatim, test_crash_recovery, test_hour_rolling,
            test_manifest_integrity, test_event_time_range_resolution, test_gap_recording,
            test_dotenv_loading,
        ):
            print(f"\n--- {fn.__name__} ---")
            fn(tmp)
        for fn0 in (test_governor_budget, test_governor_priority, test_stream_parsing,
                    test_irrecoverable_classification, test_phoenix_v2_arrays,
                    test_error_hierarchy):
            print(f"\n--- {fn0.__name__} ---")
            fn0()
        print("\n" + "=" * 72)
        print(f"{len(PASS)} passed, {len(FAIL)} failed")
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
