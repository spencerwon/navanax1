#!/usr/bin/env python3
"""PR-0.2 — may one API key hold two OpenSea stream connections at once?

    PYTHONPATH=src python3 tools/probe_two_sockets.py [--seconds 600]

Settles E-U4 in `docs/proposals/TECHLEAD_2026-09-09_factcheck.md` and gates
PR-10 (the redundant stream). Two clients, one key, the same collection, ten
minutes. It answers four questions and one bonus:

  1. Did BOTH sockets connect?
  2. Did BOTH receive market events, or did the second connect and stay silent?
     (A socket that is open and mute is the more dangerous outcome of the two:
     it looks healthy on every dashboard and records nothing.)
  3. Did the second get an HTTP 4xx at the handshake, or a WebSocket close code?
  4. What fraction of events did the two sockets agree on — and how many did
     EACH see that the other did not? That second number is the measurement
     that matters: it is how much a single socket drops, which is the entire
     case for running two.

WHAT THIS SPENDS: zero REST reads. The stream is unmetered. The only cost is
two extra simultaneous connections on the key.

WHAT THIS NEVER TOUCHES: the landing zone (`data/landing/`), `data/ops.db` and
`data/analytics.sqlite` are not opened, not imported and not written. This probe
is not an ingestion path and must never become one — frames it captures go to a
gitignored scratch file under `data/probes/` for inspection, and that file is
NOT part of the historical record (it has no manifest, no checksums and no
sequence numbers, so nothing may ever be folded from it).

THE RECORDER KEEPS RUNNING. Do not stop it to make room. These two sockets are
ADDITIONAL to the recorder's one, so the key will be carrying at least three
simultaneous connections for the duration, and four if anything else of yours is
connected. If `data/logs/recorder.log` shows a disconnect inside the probe
window, that is not an unlucky coincidence — that is the finding, and it means
the account is limited to fewer connections than we are asking for. The probe
reads that log itself (read-only) and reports what appeared during its window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from navanax.stream import MAINNET_WS, normalize_frame  # noqa: E402

MEASUREMENT_PATH = Path("docs/measurements/2026-09-11_two_sockets.md")
PROBE_DIR = Path("data/probes")
RECORDER_LOG = Path("data/logs/recorder.log")

# Lines in the recorder's log that mean it lost its connection. Matched against
# what `navanax.stream` actually logs, not against what it might log.
DISCONNECT_MARKERS = ("reconnecting in", "stream error", "closed cleanly but we did not ask",
                      "NOT SUBSCRIBED TO ANYTHING", "subscription rejected")

DOC_HEADER = """\
# Measurement — two simultaneous stream connections on one API key

*Settles E-U4 (`docs/proposals/TECHLEAD_2026-09-09_factcheck.md` §2) and gates
PR-10, the redundant stream. Written by `tools/probe_two_sockets.py`; each run
appends one dated entry and edits none of the ones above it.*

Two questions live in this file and they are not the same question. **May the
key hold two connections** is a permission question, answered by the handshake.
**Does a second connection see anything the first missed** is a loss-rate
question, answered by the per-connection unique counts — and a run that answers
the first one yes and the second one "nothing unique in ten minutes" is not a
case for redundancy, it is an unmeasured one.

"""


# --------------------------------------------------------------- protocol
# Byte-identical to what `StreamConsumer` sends. `tests/selftest.py` asserts
# that equality rather than trusting this comment, because a probe that
# subscribes differently from the recorder is measuring a different thing.
def join_frame(slug: str, ref: str) -> str:
    return json.dumps({"topic": f"collection:{slug}", "event": "phx_join",
                       "payload": {}, "ref": ref})


def heartbeat_frame(ref: str) -> str:
    return json.dumps({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": ref})


def _dig(payload: Any, field_name: str) -> Any:
    """`order_hash` and `event_timestamp` sit at varying depths by event type.

    Same shape-tolerance as `StreamConsumer.extract_event_timestamp`, for the
    same reason: guessing one depth and finding nothing looks identical to the
    field being absent.
    """
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("payload")):
        if isinstance(candidate, dict):
            v = candidate.get(field_name)
            if v is not None:
                return v
            inner = candidate.get("payload")
            if isinstance(inner, dict) and inner.get(field_name) is not None:
                return inner[field_name]
    return None


def frame_key(msg: dict[str, Any]) -> tuple[str, str, str] | None:
    """(event_type, order_hash, event_timestamp), or None if not all three exist.

    This is deliberately the SAME key PR-10 proposes to dedup on (E-W3), so the
    un-keyable count below is a direct measurement of how many events that
    design cannot dedup. A frame missing any leg is counted as un-keyable, never
    guessed at and never dropped silently.
    """
    payload = msg.get("payload")
    etype = None
    if isinstance(payload, dict) and isinstance(payload.get("event_type"), str):
        etype = payload["event_type"]
    elif isinstance(msg.get("event"), str):
        etype = msg["event"]
    oh = _dig(payload, "order_hash")
    ts = _dig(payload, "event_timestamp")
    if not (isinstance(etype, str) and isinstance(oh, str) and isinstance(ts, str)):
        return None
    return (etype, oh, ts)


def is_market_frame(msg: dict[str, Any]) -> bool:
    topic = msg.get("topic")
    event = msg.get("event")
    return (isinstance(topic, str) and topic.startswith("collection:")
            and isinstance(event, str) and not event.startswith("phx_"))


# --------------------------------------------------------------- tallies
@dataclass
class SocketTally:
    """Everything one connection saw. No I/O; `observe()` is pure enough to test."""

    name: str
    frames: int = 0
    market_frames: int = 0
    keyed: int = 0
    unkeyable: int = 0
    unparseable: int = 0
    join_ok: bool = False
    join_error: str | None = None
    heartbeat_replies: int = 0
    connected_at: float | None = None       # monotonic
    connected_iso: str | None = None
    closed_at: float | None = None
    close_code: int | None = None
    close_reason: str = ""
    http_status: int | None = None
    error: str | None = None
    first_frame_at: float | None = None
    last_frame_at: float | None = None
    # key -> monotonic time of FIRST sight. A duplicate inside one connection
    # keeps the earlier time; it is the same event, not a second one.
    keys: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def observe(self, raw: str, now: float) -> tuple[str, str, str] | None:
        self.frames += 1
        if self.first_frame_at is None:
            self.first_frame_at = now
        self.last_frame_at = now
        try:
            parsed = json.loads(raw)
        except ValueError:
            self.unparseable += 1
            return None
        msg = normalize_frame(parsed)
        if msg is None:
            self.unparseable += 1
            return None
        if msg.get("event") == "phx_reply":
            p = msg.get("payload")
            status = p.get("status") if isinstance(p, dict) else None
            if msg.get("topic") == "phoenix":
                self.heartbeat_replies += 1
            elif status == "ok":
                self.join_ok = True
            elif status is not None:
                self.join_error = f"phx_reply status={status} {json.dumps(p)[:160]}"
            return None
        if msg.get("event") == "phx_error":
            self.join_error = f"phx_error {json.dumps(msg.get('payload'))[:160]}"
            return None
        if not is_market_frame(msg):
            return None
        self.market_frames += 1
        key = frame_key(msg)
        if key is None:
            self.unkeyable += 1
            return None
        self.keyed += 1
        self.keys.setdefault(key, now)
        return key

    def window_keys(self, t0: float, t1: float) -> set[tuple[str, str, str]]:
        """Keys whose first sight ON THIS SOCKET fell inside [t0, t1].

        NOT safe to compare across sockets on its own, and `overlap_report` does
        not: an event the OTHER socket already had before t0 can arrive here
        inside the window (a replay after the later join) and would land in
        exactly one of the two sets, which is a manufactured unique. Use
        `window_sets()` for any comparison; this stays per-socket and raw
        because the exclusion needs both sockets' times to decide.
        """
        return {k for k, t in self.keys.items() if t0 <= t <= t1}

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "keys"}
        d["distinct_keys"] = len(self.keys)
        return d


def common_window(a: SocketTally, b: SocketTally, *, settle: float = 5.0
                  ) -> tuple[float, float] | None:
    """The stretch of time BOTH sockets were connected, minus a settling margin.

    Comparing a socket's whole life against another's is the obvious mistake and
    it manufactures fake uniques: everything the first socket saw before the
    second one joined would count as "missed by B". `settle` drops the first few
    seconds after the later join, when the server is still replaying or
    catching up.
    """
    if a.connected_at is None or b.connected_at is None:
        return None
    t0 = max(a.connected_at, b.connected_at) + settle
    ends = [t for t in (a.closed_at, b.closed_at) if t is not None]
    t1 = min(ends) if ends else max(x for x in (a.last_frame_at, b.last_frame_at, t0)
                                    if x is not None)
    return (t0, t1) if t1 > t0 else None


def window_sets(a: SocketTally, b: SocketTally, t0: float, t1: float
                ) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]],
                           set[tuple[str, str, str]]]:
    """The two comparable key sets for [t0, t1], plus what was excluded and why.

    BUG-20260911-072. Filtering each socket by its OWN first-sight time is not
    enough. Suppose A connects first and sees `0xPRE` well before the common
    window opens; B connects later and the server replays `0xPRE` to it a few
    seconds INSIDE the window. By each socket's own clock the event is
    out-of-window for A and in-window for B, so it counts as "only B saw it" --
    a unique manufactured out of the stagger, pointing in the direction that
    flatters PR-10's case for a second stream.

    An event belongs to the window only if NEITHER socket had already seen it
    when the window opened. So: take each socket's window set, then drop every
    key whose EARLIEST sighting across BOTH sockets is before t0. The dropped
    keys are returned rather than discarded silently -- how many there were is
    itself a measurement of how much the server replays.
    """
    ka, kb = a.window_keys(t0, t1), b.window_keys(t0, t1)
    excluded: set[tuple[str, str, str]] = set()
    for k in ka | kb:
        seen = [t for t in (a.keys.get(k), b.keys.get(k)) if t is not None]
        if seen and min(seen) < t0:
            excluded.add(k)
    return ka - excluded, kb - excluded, excluded


def overlap_report(a: SocketTally, b: SocketTally, window: tuple[float, float] | None
                   ) -> dict[str, Any]:
    """Pairwise agreement, every ratio carrying the count it was computed from.

    A ratio over an empty set is `None`, never 0.0 and never 1.0. "The two
    sockets agreed on 100% of events" is a sentence that reads identically
    whether they agreed on nine hundred events or on zero, and only one of
    those is evidence.
    """
    if window is None:
        return {"window": None, "n_a": 0, "n_b": 0, "n_union": 0, "n_intersection": 0,
                "only_a": 0, "only_b": 0, "jaccard": None,
                "share_of_a_also_in_b": None, "share_of_b_also_in_a": None,
                "n_excluded_pre_window": 0, "window_seconds": 0.0}
    t0, t1 = window
    ka, kb, excluded = window_sets(a, b, t0, t1)
    inter, union = ka & kb, ka | kb
    return {
        "window": [t0, t1],
        "window_seconds": round(t1 - t0, 1),
        "n_excluded_pre_window": len(excluded),
        "n_a": len(ka),
        "n_b": len(kb),
        "n_union": len(union),
        "n_intersection": len(inter),
        "only_a": len(ka - kb),
        "only_b": len(kb - ka),
        "jaccard": (len(inter) / len(union)) if union else None,
        "share_of_a_also_in_b": (len(inter) / len(ka)) if ka else None,
        "share_of_b_also_in_a": (len(inter) / len(kb)) if kb else None,
    }


def verdict(a: SocketTally, b: SocketTally, ov: dict[str, Any]) -> list[str]:
    """Plain sentences. Each one states what it is based on."""
    out: list[str] = []
    both_connected = a.connected_at is not None and b.connected_at is not None
    if not both_connected:
        which = "A" if a.connected_at is None else "B"
        detail = (a if which == "A" else b)
        out.append(
            f"**Two connections were NOT established.** Socket {which} never opened "
            f"(http_status={detail.http_status}, error={detail.error!r}). PR-10 cannot rely on "
            f"one key holding two streams; it needs the two keys with staggered creation dates "
            f"that E-V13 already called for."
        )
        return out
    out.append("**Both sockets connected.** The handshake did not refuse the second connection.")
    if b.http_status is not None:
        out.append(f"...but socket B reported HTTP status {b.http_status} at some point. Read the "
                   f"table before believing the line above.")
    if b.close_code is not None:
        out.append(f"Socket B closed with WebSocket code {b.close_code} "
                   f"({b.close_reason or 'no reason given'}) after "
                   f"{(b.closed_at or 0) - (b.connected_at or 0):.0f}s. A close before the "
                   f"requested duration is a refusal that arrived late.")
    if a.close_code is not None:
        out.append(f"Socket A closed with WebSocket code {a.close_code} "
                   f"({a.close_reason or 'no reason given'}). If B stayed up, the server may be "
                   f"evicting the OLDER connection rather than refusing the newer one — which "
                   f"would mean the recorder is what gets dropped when a probe like this runs.")

    receiving = [t.name for t in (a, b) if t.market_frames > 0]
    if len(receiving) == 2:
        out.append(f"**Both sockets received market events** ({a.market_frames} on A, "
                   f"{b.market_frames} on B).")
    elif len(receiving) == 1:
        mute = b if b.market_frames == 0 else a
        out.append(
            f"**Socket {mute.name} connected and stayed MUTE** — 0 market events while the other "
            f"received {(a if mute is b else b).market_frames}. This is the worse of the two "
            f"failures: an open, silent socket looks healthy to every liveness check we have. "
            f"Treat it as 'two connections are not permitted', not as 'a quiet market'."
        )
        return out
    else:
        out.append(
            "**Neither socket received a market event.** That is a statement about the market's "
            "activity in this window, not about the connection limit, and it settles nothing. "
            "Re-run for longer or during a busier period before concluding anything about E-U4."
        )
        return out

    # BUG-20260911-072. Every sentence below is a claim ABOUT THE COMMON WINDOW,
    # so the window has to exist and have length before any of them may be said.
    # A drop rate computed over a zero-length window, or over an empty union, is
    # a percentage with no denominator behind it -- and "a single socket
    # demonstrably drops events" is exactly the sentence that must never be
    # produced by an absence of evidence.
    win_secs = ov.get("window_seconds") or 0.0
    if ov.get("window") is None or win_secs <= 0:
        out.append(
            "**There was no common window.** The two sockets were never both connected for a "
            "measurable stretch (window 0s, n=0), so nothing here says anything about how much "
            "one socket drops. Re-run; do not read the table above as a loss rate."
        )
        return out
    # Said BEFORE the empty-window sentence, because it is often the reason the
    # window is empty: if every event in it was a replay of something a socket
    # already had, "no keyable events fell inside the window" is true and
    # unreadable without this line.
    if ov.get("n_excluded_pre_window"):
        n_x = ov["n_excluded_pre_window"]
        out.append(
            f"{n_x} event{'' if n_x == 1 else 's'} "
            f"{'was' if n_x == 1 else 'were'} excluded from the comparison because at "
            f"least one socket had already seen them before the window opened (a replay after "
            f"the later join). Counting those would manufacture uniques out of the stagger."
        )
    n_u = ov["n_union"]
    if not n_u:
        out.append(
            f"No keyable events fell inside the {win_secs:.0f}s window both sockets were "
            f"connected for (n=0), so the overlap question is unanswered by this run. An empty "
            f"window is not a measurement of zero loss."
        )
        return out
    only_a, only_b, inter = ov["only_a"], ov["only_b"], ov["n_intersection"]
    out.append(
        f"Over the {ov['window_seconds']:.0f}s both were connected: {ov['n_a']} distinct events on "
        f"A, {ov['n_b']} on B, {inter} seen by both, {n_u} distinct in total. Agreement "
        f"(Jaccard) {ov['jaccard']:.3f} on n={n_u}."
    )
    if only_a == 0 and only_b == 0:
        out.append(
            f"**Neither socket saw anything the other missed (0 unique on each, n={n_u}).** Read "
            f"this carefully. It is what a lossless stream looks like, and it is equally what a "
            f"probe bug looks like — if both sockets were somehow fed from one buffer the answer "
            f"would also be zero. It is ALSO what a window too short to catch a rare drop looks "
            f"like. On this evidence the redundant stream of PR-10 buys nothing measurable; the "
            f"honest statement is 'no loss observed in {ov['window_seconds']:.0f}s at n={n_u}', "
            f"not 'the stream is lossless'."
        )
    else:
        rate = (only_a + only_b) / n_u
        was = "was" if only_a == 1 else "were"
        out.append(
            f"**A single socket demonstrably drops events: {only_a} {was} seen only by A and "
            f"{only_b} only by B, out of {n_u} distinct.** That is {only_a + only_b} of the "
            f"{n_u} events in the union -- {rate:.1%} -- that "
            f"one connection alone would have missed (n={n_u}). This is the measured value of "
            f"PR-10's second stream, and it is the number to put in that PR rather than an "
            f"assumption."
        )
    if a.unkeyable or b.unkeyable:
        n_unk = a.unkeyable + b.unkeyable
        out.append(
            f"{n_unk} market frame{'' if n_unk == 1 else 's'} carried no "
            f"(event_type, order_hash, event_timestamp) key and could not be compared at all "
            f"(A {a.unkeyable}, B {b.unkeyable}). PR-10 must count these as un-dedupable rather "
            f"than merging or double-counting them (E-W3, E-U3); this run says how many there are."
        )
    return out


# --------------------------------------------------------------- recorder log
def read_log_tail(path: Path, offset: int) -> str:
    """Read-only. Whatever the recorder appended since `offset`."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def disconnect_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines()
            if any(m in ln for m in DISCONNECT_MARKERS)]


# --------------------------------------------------------------- the sockets
async def run_socket(name: str, url: str, slug: str, tally: SocketTally, stop: asyncio.Event,
                     sink, *, api_key: str, heartbeat_seconds: float = 30.0,
                     start_delay: float = 0.0, connect_factory=None) -> None:
    """One connection, for the life of the probe. Never reconnects.

    No reconnect on purpose: a probe that silently reconnects would turn "the
    server closed our second connection" into "the second connection worked
    fine", which is precisely the fact being measured.
    """
    if start_delay:
        await asyncio.sleep(start_delay)
    if connect_factory is None:
        from navanax.stream import StreamConsumer
        # The recorder's own connect call -- same TLS context, same ping
        # settings, same max frame size. Reimplementing it here would measure a
        # client we do not run.
        connect_factory = StreamConsumer._default_connect
    ref = 0

    def next_ref() -> str:
        nonlocal ref
        ref += 1
        return str(ref)

    try:
        # Same URL shape as `StreamConsumer._run_once`: the key rides in the
        # query string, which is why it is never echoed to the screen or to the
        # scratch file.
        async with connect_factory(f"{url}?token={api_key}") as ws:
            tally.connected_at = time.monotonic()
            tally.connected_iso = _now_iso()
            print(f"  [{name}] connected at {tally.connected_iso}")
            await ws.send(join_frame(slug, next_ref()))

            async def beat() -> None:
                next_at = time.monotonic() + heartbeat_seconds
                while not stop.is_set():
                    await asyncio.sleep(min(1.0, heartbeat_seconds))
                    if time.monotonic() >= next_at:
                        next_at = time.monotonic() + heartbeat_seconds
                        await ws.send(heartbeat_frame(next_ref()))
            hb = asyncio.create_task(beat())
            try:
                async for raw in ws:
                    if stop.is_set():
                        break
                    text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                    now = time.monotonic()
                    tally.observe(text, now)
                    if sink is not None:
                        sink.write(json.dumps({"conn": name, "at": _now_iso(),
                                               "mono": round(now, 3), "raw": text}) + "\n")
            finally:
                hb.cancel()
                try:
                    await hb
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001 - a dead heartbeat is a clue, not noise
                    print(f"  [{name}] heartbeat task failed: {type(exc).__name__}: {exc}")
            tally.close_code = getattr(ws, "close_code", None)
            reason = getattr(ws, "close_reason", "")
            tally.close_reason = reason if isinstance(reason, str) else ""
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure here IS a result
        tally.error = f"{type(exc).__name__}: {exc}"
        # websockets raises InvalidStatus (>=12) or InvalidStatusCode (<12) when
        # the server refuses the HTTP upgrade. That status code is the direct
        # answer to "is a second connection permitted", so dig it out of either.
        status = getattr(exc, "status_code", None)
        if status is None:
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None)
        if isinstance(status, int):
            tally.http_status = status
        code = getattr(exc, "code", None)
        if tally.close_code is None and isinstance(code, int):
            tally.close_code = code
        rcvd = getattr(exc, "rcvd", None)
        if tally.close_code is None and rcvd is not None and isinstance(
                getattr(rcvd, "code", None), int):
            tally.close_code = rcvd.code
            tally.close_reason = getattr(rcvd, "reason", "") or ""
        print(f"  [{name}] ENDED: {tally.error}"
              + (f"  http_status={tally.http_status}" if tally.http_status else "")
              + (f"  close_code={tally.close_code}" if tally.close_code else ""))
    finally:
        if tally.closed_at is None:
            tally.closed_at = time.monotonic()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------- reporting
def _row(label: str, a: Any, b: Any) -> str:
    return f"| {label} | {a} | {b} |"


def render_entry(a: SocketTally, b: SocketTally, ov: dict[str, Any], meta: dict[str, Any]
                 ) -> str:
    # Spelled out rather than inlined: a ratio and the n it came from travel
    # together in this project, and "n/a (no events)" is the honest rendering of
    # a ratio over an empty set.
    if ov["jaccard"] is None:
        jac = "n/a (no events in the common window)"
    else:
        jac = f"{ov['jaccard']:.3f} on n={ov['n_union']}"
    # A run stopped by Ctrl+C is written, not thrown away -- but it is labelled
    # in the heading, because an entry that reads like a completed ten-minute
    # run and was actually forty seconds is the kind of number that gets quoted
    # later with the wrong n behind it (BUG-20260911-072).
    if meta.get("interrupted"):
        head = (f"## {meta['started']} — interrupted after {meta.get('elapsed_seconds', 0):.0f} s, "
                f"partial (of {meta['seconds']:.0f}s requested), "
                f"`collection:{meta['slug']}`, one key")
        note = ("**This run was stopped early with Ctrl+C and is PARTIAL.** It was asked for "
                f"{meta['seconds']:.0f}s and ran {meta.get('elapsed_seconds', 0):.0f}s. Every "
                "count below is what the probe had seen at the moment it was interrupted; read "
                "it as a lower bound with a shorter window, not as a completed measurement.")
    else:
        head = (f"## {meta['started']} — {meta['seconds']:.0f}s, "
                f"`collection:{meta['slug']}`, one key")
        note = ""
    lines = [
        head,
        "",
        f"Probe `tools/probe_two_sockets.py`. REST reads spent: **0** (the stream is unmetered). "
        f"Raw frames kept at `{meta['sink']}` (gitignored, not part of the record).",
        "",
    ]
    if note:
        lines += [note, ""]
    lines += [
        "| | Socket A | Socket B (the second connection) |",
        "|---|---|---|",
        _row("Connected", a.connected_iso or "**no**", b.connected_iso or "**no**"),
        _row("HTTP status at handshake", a.http_status if a.http_status is not None else "—",
             b.http_status if b.http_status is not None else "—"),
        _row("WebSocket close code", a.close_code if a.close_code is not None else "—",
             b.close_code if b.close_code is not None else "—"),
        _row("Join acknowledged", "yes" if a.join_ok else f"**no** ({a.join_error or 'no reply'})",
             "yes" if b.join_ok else f"**no** ({b.join_error or 'no reply'})"),
        _row("Frames received (all)", a.frames, b.frames),
        _row("Market events", a.market_frames, b.market_frames),
        _row("...keyable", a.keyed, b.keyed),
        _row("...un-keyable (no order_hash / event_timestamp)", a.unkeyable, b.unkeyable),
        _row("Distinct events", len(a.keys), len(b.keys)),
        _row("Heartbeat replies", a.heartbeat_replies, b.heartbeat_replies),
        _row("Ended with", a.error or "clean", b.error or "clean"),
        "",
        f"**Overlap, over the {ov['window_seconds']:.0f}s both were connected** "
        f"(keyed on `(event_type, order_hash, event_timestamp)` — PR-10's proposed dedup key):",
        "",
        "| Quantity | Value |",
        "|---|---|",
        f"| Distinct on A | {ov['n_a']} |",
        f"| Distinct on B | {ov['n_b']} |",
        f"| Seen by both | {ov['n_intersection']} |",
        f"| Distinct in total (union) | {ov['n_union']} |",
        f"| Seen ONLY by A | {ov['only_a']} |",
        f"| Seen ONLY by B | {ov['only_b']} |",
        f"| Excluded: already seen by a socket before the window opened "
        f"| {ov.get('n_excluded_pre_window', 0)} |",
        f"| Overlap ratio (Jaccard) | {jac} |",
        "",
    ]
    for s in verdict(a, b, ov):
        lines += [s, ""]
    lines += ["**Recorder during this window** "
              f"(`{meta['recorder_log']}`, read-only): "
              + (f"{len(meta['recorder_disconnects'])} disconnect line(s)."
                 if meta["recorder_disconnects"] else "no disconnect lines."),
              ""]
    if meta["recorder_disconnects"]:
        lines += ["```"] + meta["recorder_disconnects"][:20] + ["```", "",
                  "A recorder disconnect inside the probe window is the finding: the account "
                  "did not hold the extra connections, and the cost of asking was paid out of "
                  "the historical record.", ""]
    return "\n".join(lines)


def append_entry(path: Path, entry: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DOC_HEADER + entry)
    else:
        old = path.read_text()
        path.write_text(old + ("" if old.endswith("\n") else "\n") + "\n---\n\n" + entry)


# --------------------------------------------------------------- main
async def _run(args, slug: str, sink_path: Path, api_key: str,
               a: SocketTally, b: SocketTally) -> None:
    """The tallies are passed IN, not created here.

    Ctrl+C unwinds out of `asyncio.run`, and a tally created inside this
    function would unwind with it -- which is how "Ctrl+C stops early and still
    writes what it saw" came to print "nothing was written". The caller owns
    them, so whatever they hold at the moment of the interrupt survives it.
    """
    stop = asyncio.Event()
    sink_path.parent.mkdir(parents=True, exist_ok=True)
    with sink_path.open("w", encoding="utf-8") as sink:
        tasks = [
            asyncio.create_task(run_socket("A", args.url, slug, a, stop, sink,
                                           api_key=api_key)),
            asyncio.create_task(run_socket("B", args.url, slug, b, stop, sink,
                                           api_key=api_key, start_delay=args.stagger)),
        ]
        try:
            await asyncio.sleep(args.seconds)
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--seconds", type=float, default=600.0,
                    help="how long to hold both connections (default 600)")
    ap.add_argument("--stagger", type=float, default=5.0,
                    help="seconds to wait before opening the SECOND connection (default 5)")
    ap.add_argument("--settle", type=float, default=5.0,
                    help="seconds after the later join to exclude from the overlap window")
    ap.add_argument("--slug", default=None, help="default: the first watchlist collection")
    ap.add_argument("--url", default=MAINNET_WS)
    ap.add_argument("--out", default=str(MEASUREMENT_PATH))
    args = ap.parse_args(argv)

    try:
        import websockets  # noqa: F401
    except ImportError:
        print("the `websockets` package is not installed here. Run setup.command first.",
              file=sys.stderr)
        return 2

    from navanax.cli import _config
    from navanax.dotenv import DotenvError, require

    root = Path(args.root)
    cfg, slugs = _config(root)
    slug = args.slug or (slugs[0] if slugs else "argonauts")
    try:
        api_key = require("OPENSEA_API_KEY", path=root / ".env")
    except DotenvError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sink_path = root / PROBE_DIR / f"two_sockets_{stamp}.jsonl"
    rec_log = root / RECORDER_LOG
    offset = log_size(rec_log)

    print(f"collection           {slug}")
    print(f"duration             {args.seconds:.0f}s "
          f"(second socket opens {args.stagger:.0f}s after the first)")
    print("REST reads spent     0 -- the stream is unmetered")
    print("writes               docs/measurements/ and a gitignored scratch file.")
    print("                     NOT the landing zone, NOT ops.db, NOT analytics.sqlite.")
    print(f"raw frames           {sink_path}")
    print()
    print("  The RECORDER should stay running. Do not stop it for this.")
    print("  These are two EXTRA connections on your key, so the account will be holding")
    print("  at least three at once (recorder + A + B), four if anything else of yours is")
    print("  connected. If data/logs/recorder.log shows a disconnect during the next")
    print(f"  {args.seconds / 60:.0f} minutes, THAT IS THE FINDING -- it means the account")
    print("  will not hold the connections PR-10 wants. This probe reads that log")
    print("  afterwards and reports it.")
    print()
    print("  Leave this window open. Ctrl+C stops early and still writes what it saw,")
    print("  as an entry marked PARTIAL with the seconds it actually ran.")
    print("-" * 60)

    a, b = SocketTally("A"), SocketTally("B")
    started = _now_iso()
    t_start = time.monotonic()
    interrupted = False
    try:
        asyncio.run(_run(args, slug, sink_path, api_key, a, b))
    except KeyboardInterrupt:
        interrupted = True
        print("\nstopped early by Ctrl+C -- writing what it saw, marked partial")
    elapsed = time.monotonic() - t_start
    # An interrupt unwinds past run_socket's `finally`, so a socket that was
    # still open has no closed_at. Close it at the interrupt rather than letting
    # common_window fall back to last_frame_at, which would end the window at
    # the last event instead of at the moment the Operator stopped it.
    for t in (a, b):
        if t.connected_at is not None and t.closed_at is None:
            t.closed_at = t_start + elapsed

    ov = overlap_report(a, b, common_window(a, b, settle=args.settle))
    tail = read_log_tail(rec_log, offset)
    meta = {"started": started, "seconds": args.seconds, "slug": slug,
            "interrupted": interrupted,
            "elapsed_seconds": round(elapsed, 1),
            "sink": str(sink_path.relative_to(root)) if sink_path.is_relative_to(root)
            else str(sink_path),
            "recorder_log": str(RECORDER_LOG),
            "recorder_disconnects": disconnect_lines(tail)}

    print("-" * 60)
    print(f"  A  connected={a.connected_iso or 'NO'}  frames={a.frames}  "
          f"market={a.market_frames}  distinct={len(a.keys)}  close={a.close_code}")
    print(f"  B  connected={b.connected_iso or 'NO'}  frames={b.frames}  "
          f"market={b.market_frames}  distinct={len(b.keys)}  close={b.close_code}"
          f"  http={b.http_status}")
    print(f"  overlap  union={ov['n_union']}  both={ov['n_intersection']}  "
          f"only-A={ov['only_a']}  only-B={ov['only_b']}  "
          f"jaccard={'n/a' if ov['jaccard'] is None else format(ov['jaccard'], '.3f')}")
    print()
    for s in verdict(a, b, ov):
        print("  " + s.replace("**", ""))
        print()
    if meta["recorder_disconnects"]:
        print(f"  RECORDER: {len(meta['recorder_disconnects'])} disconnect line(s) appeared in "
              f"{RECORDER_LOG} during this probe. That is the finding.")
        for ln in meta["recorder_disconnects"][:10]:
            print("    " + ln)
    else:
        print(f"  RECORDER: no disconnect lines in {RECORDER_LOG} during this probe.")

    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    append_entry(out, render_entry(a, b, ov, meta))
    print()
    if interrupted:
        print(f"recorded in          {out}  (PARTIAL -- interrupted after "
              f"{elapsed:.0f} s of {args.seconds:.0f} s)")
        return 130
    print(f"recorded in          {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
