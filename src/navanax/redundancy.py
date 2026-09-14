"""PR-10: a second, redundant stream connection -- behind a flag, default OFF.

WHAT THIS IS FOR, IN ONE PARAGRAPH

One WebSocket connection drops. A clean close costs 1-3 s; a silent half-open
socket costs 21-45 s before the ping timer notices (dataeng §3.c). At the
measured Argonauts rate that worst case is ~1,900 events, about half of them
`item_cancelled` / `order_invalidate`, which no REST call can ever fetch back
(REQ-D-09a). A SECOND connection, already warm, covers the first one's
reconnect window -- it is "a reconnect that already finished". That is all it
buys. It buys nothing against a server-side outage, a sleeping laptop, or a
key expiry, and the honest scope is stated in docs/07 §3.x.

WHAT IT COSTS, STATED BEFORE THE BENEFIT

Disk doubles (~660 MB/day -> ~1.3 GB/day for one collection at the measured
rate), and every raw count over `events` or `order_criteria` doubles with it.
That second cost is the dangerous one, which is why `/api/health` carries a
`dedup` block and why nothing in this module ever quietly merges a row it
cannot prove is a duplicate.

THE THREE RULES THIS MODULE EXISTS TO KEEP

1. **Two stores, one writer each.** Connection B writes to its OWN landing
   root with its OWN `.ingest.lock`. Two writers on one root silently lose
   manifest records -- measured at 295 of 600 gap records, with the audit
   reporting clean (BUG-006/007). Separate roots make the single-writer rule
   hold twice rather than break once.
2. **Two keys, never one.** Free instant keys expire after 7 days (REQ-D-06),
   so two processes sharing one key fail SIMULTANEOUSLY on a known schedule --
   E-V13, the correlated failure that makes the whole exercise pointless. B
   refuses to start if its key is missing or equal to A's.
3. **A gap in A that B covered is still a gap in A.** It is annotated
   `covered_by`, never suppressed. "A was blind and B was not" is a different
   fact from "no gap occurred", and only the first one is true.

THE FLATTERING-DIRECTION FAILURE, AND THE MONITOR THAT CATCHES IT

If the dedup key is too WIDE, duplicates survive, every count doubles, every
rate doubles, and a backtest looks wonderful. That is dataeng failure mode 3
and it is exactly the shape the fifth project rule warns about: a surprisingly
good result is evidence of a bug. The expected duplicate fraction under a
healthy pair is HIGH -- most events should be seen twice. So
`DuplicateFractionMonitor` alarms when the observed duplicate fraction
COLLAPSES TOWARD ZERO while both connections are reporting healthy. It
deliberately does NOT alarm when B is simply down, because then zero duplicates
is the correct answer and an alarm there would train the Operator to ignore it.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from .dotenv import DotenvError, require
from .normalize import iso_to_ts

log = logging.getLogger("navanax.redundancy")

#: The label of the primary connection. Connection A is whatever writes to
#: `landing.root`; it has no config entry because it is not optional.
PRIMARY_LABEL = "a"

#: Defaults for `stream.redundant` when the block is absent entirely. They match
#: `config/base.yaml`; the config file is the authority (REQ-N-09) and this is
#: only what an older config gets so nothing crashes on a missing key.
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "key_env": "OPENSEA_API_KEY_2",
    "landing_root": "data/landing-b",
    "lock_file": "data/landing-b/.ingest.lock",
    "label": "b",
}

MONITOR_DEFAULTS: dict[str, Any] = {
    "window_seconds": 900,
    "min_events": 100,
    "collapse_fraction": 0.5,
    # How many multiplicity disagreements in the window are tolerated before the
    # monitor warns. 0 = any disagreement warns. See the note in
    # `DuplicateFractionMonitor.evaluate`: this may prove noisy if rejoin-replay
    # is common, and it is configuration (REQ-N-09) precisely so the Operator can
    # raise it from measurement rather than from a code change.
    "multiplicity_disagreement_max": 0,
}


class RedundantKeyError(RuntimeError):
    """Connection B cannot start: its key is missing, or it is A's key.

    A distinct type so the CLI can print a plain message and exit with the
    configuration code rather than a traceback. Both branches are refusals
    BEFORE anything is recorded -- a B that shares A's key is worse than no B
    at all, because it looks like redundancy and expires at the same minute.
    """


@dataclass(frozen=True)
class RedundantSettings:
    """`stream.redundant`, resolved. Paths are still relative to the project root."""

    enabled: bool
    key_env: str
    landing_root: str
    lock_file: str
    label: str
    window_seconds: float
    min_events: int
    collapse_fraction: float
    multiplicity_disagreement_max: int

    def root(self, project_root: Path | str) -> Path:
        return Path(project_root) / self.landing_root

    def lock(self, project_root: Path | str) -> Path:
        return Path(project_root) / self.lock_file


def settings(cfg: dict[str, Any]) -> RedundantSettings:
    """Read `stream.redundant` out of a loaded config. Never raises on a missing key."""
    block = ((cfg.get("stream") or {}).get("redundant") or {}) if isinstance(cfg, dict) else {}
    mon = block.get("monitor") or {}
    return RedundantSettings(
        enabled=bool(block.get("enabled", DEFAULTS["enabled"])),
        key_env=str(block.get("key_env", DEFAULTS["key_env"])),
        landing_root=str(block.get("landing_root", DEFAULTS["landing_root"])),
        lock_file=str(block.get("lock_file", DEFAULTS["lock_file"])),
        label=str(block.get("label", DEFAULTS["label"])),
        window_seconds=float(mon.get("window_seconds", MONITOR_DEFAULTS["window_seconds"])),
        min_events=int(mon.get("min_events", MONITOR_DEFAULTS["min_events"])),
        collapse_fraction=float(mon.get("collapse_fraction",
                                        MONITOR_DEFAULTS["collapse_fraction"])),
        multiplicity_disagreement_max=int(
            mon.get("multiplicity_disagreement_max",
                    MONITOR_DEFAULTS["multiplicity_disagreement_max"])),
    )


def landing_roots(project_root: Path | str, cfg: dict[str, Any]) -> list[tuple[str, Path]]:
    """Every landing root the normalizer should fold, as (label, path).

    With the flag off this is exactly one entry, `('a', data/landing)` -- the
    same single root the normalizer has always read. The label is what tells the
    dedup monitor which connection a row came from; it is derived from the ROOT
    the frame was folded out of, so no byte of the landing envelope changes when
    the flag is off (and none changes when it is on either).
    """
    out = [(PRIMARY_LABEL, Path(project_root) / cfg["landing"]["root"])]
    s = settings(cfg)
    if s.enabled:
        out.append((s.label, s.root(project_root)))
    return out


def second_key(project_root: Path | str, cfg: dict[str, Any], primary_key: str) -> str:
    """Connection B's API key, or a refusal that says which of the two faults it is.

    E-V13. Two processes on ONE key is not redundancy: free instant keys expire
    after seven days (REQ-D-06), so both connections die in the same minute, on a
    schedule, and the pair covers nothing at exactly the moment it was bought to
    cover something. Equal keys are refused for that reason and not for tidiness.
    """
    s = settings(cfg)
    try:
        key = require(s.key_env, path=Path(project_root) / ".env")
    except DotenvError as exc:
        raise RedundantKeyError(
            f"connection B needs its OWN OpenSea key in {s.key_env}, and there is none.\n"
            f"  {exc}\n"
            f"  Two connections on ONE key is not redundancy: free instant keys expire after "
            f"7 days (REQ-D-06), so both would fail in the same minute, on a schedule.\n"
            f"  Create a SECOND key on a different day, put it in .env as "
            f"{s.key_env}=..., and start B again.\n"
            f"  Nothing was recorded and connection A is unaffected."
        ) from None
    if key == primary_key:
        raise RedundantKeyError(
            f"connection B's key ({s.key_env}) is the SAME as connection A's "
            f"(OPENSEA_API_KEY). Refusing to start B.\n"
            f"  Two connections on one key fail SIMULTANEOUSLY when that key expires -- free "
            f"instant keys last 7 days (REQ-D-06) -- so the pair would cover nothing at "
            f"precisely the moment it was meant to.\n"
            f"  Put a DIFFERENT key, created on a different day, in {s.key_env}.\n"
            f"  Nothing was recorded and connection A is unaffected."
        )
    return key


# -- gap annotation --------------------------------------------------------
#
# "A gap in A that B covered is still a gap in A" (dataeng §3.a, failure mode 5).
# The annotation is ADDITIVE in both stores: a new nullable column on
# `gap_register` and a new field on the manifest's GapRecord. No existing gap row
# is modified in any other way, and no gap is ever closed, shortened, or removed
# because the other connection happened to be up.

def _ts(value: Any) -> float | None:
    """ISO-8601 -> epoch seconds. Intervals are compared as NUMBERS, never as text.

    Lexical comparison of ISO strings is correct only while every producer uses
    the same precision and the same 'Z'; the manifest and the gap register are
    written by different code paths and there is no rule holding them to one
    format. Converting is three lines and removes the class.
    """
    return iso_to_ts(value) if isinstance(value, str) else None


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sort and coalesce touching/overlapping intervals."""
    out: list[tuple[float, float]] = []
    for lo, hi in sorted(intervals):
        if hi <= lo:
            continue
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _subtract(cover: list[tuple[float, float]],
              blind: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """`cover` minus `blind`. Both are merged first; the result is merged."""
    out = _merge(cover)
    for b_lo, b_hi in _merge(blind):
        nxt: list[tuple[float, float]] = []
        for lo, hi in out:
            if b_hi <= lo or b_lo >= hi:
                nxt.append((lo, hi))
                continue
            if lo < b_lo:
                nxt.append((lo, b_lo))
            if b_hi < hi:
                nxt.append((b_hi, hi))
        out = nxt
    return out


def coverage_intervals(landing_root: Path | str) -> list[tuple[float, float]]:
    """When this landing root had a file OPEN, from its own manifests.

    One interval per landing file: `[opened_at, closed_at]`, and `[opened_at, inf)`
    for a file the manifest still calls open.

    **This is an upper bound on coverage and must never be used alone.** A landing
    file is closed by the writer's `close()`, which does not run when the machine
    sleeps, the process is killed, or the network drops -- so after any unclean
    stop the manifest says "open" forever and this function claims coverage
    through a window in which the connection recorded nothing. Subtracting that
    connection's OWN gap register is what turns the upper bound into a claim
    (see `annotate_gaps_covered_by`); it is the reason this function is private in
    spirit even though it is importable.

    Reads the manifest, never the directory names: files partition by ARRIVAL
    hour while events are ordered by `event_timestamp` (docs/07 §2.6), so a
    directory glob would clip at the boundaries.
    """
    mdir = Path(landing_root) / "_manifest"
    out: list[tuple[float, float]] = []
    if not mdir.exists():
        return out
    for mp in sorted(mdir.glob("*.json")):
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for f in data.get("files", []):
            lo = _ts(f.get("opened_at"))
            if lo is None:
                continue
            hi = _ts(f.get("closed_at")) if f.get("status") == "closed" else None
            out.append((lo, hi if hi is not None else math.inf))
    return _merge(out)


def blind_intervals(store: Any, label: str) -> list[tuple[float, float]]:
    """When the connection labelled `label` was, by its OWN record, not recording.

    Read from the shared gap register, filtered to that connection's own gaps.
    A gap the connection has not closed yet runs to +inf: it is blind now, and
    claiming coverage from a connection that is currently in a gap is the exact
    error this exists to prevent.
    """
    out: list[tuple[float, float]] = []
    for g in store.all_gaps():
        if (g.get("conn_label") or PRIMARY_LABEL) != label:
            continue
        lo = _ts(g.get("started_at"))
        if lo is None:
            continue
        hi = _ts(g.get("ended_at"))
        out.append((lo, hi if hi is not None else math.inf))
    return _merge(out)


def effective_coverage(store: Any, label: str,
                       landing_root: Path | str) -> list[tuple[float, float]]:
    """What connection `label` can actually be said to have covered.

    `coverage_intervals` (it had a file open) MINUS `blind_intervals` (its own
    register says it was down). The subtraction is the whole point, and it is
    BUG-20260911-074: without it a correlated outage -- both processes on one
    sleeping laptop -- annotates A's gap "covered by b" while B's own register
    says B was blind for the same window, which converts "we were not watching"
    into "the other one was", the single most dangerous confusion available here.
    """
    return _subtract(coverage_intervals(landing_root), blind_intervals(store, label))


def covers_whole(cover: list[tuple[float, float]], start: float, end: float) -> bool:
    """Does ONE merged covering interval contain the whole of [start, end]?

    WHOLE, not partial, and deliberately so. A partial annotation would have to
    be read as "some of this gap was covered", and every consumer of the gap
    register -- the backfill worklist, the analysis windows that must exclude a
    gap, the Health count -- treats a gap as a unit. An annotation that meant
    "60% of it" would be true and useless at best, and at worst would be rounded
    up in somebody's head to "covered". If B was up for part of A's gap, A's gap
    is still a hole in A's record and the honest answer is no annotation.
    """
    return any(lo <= start and hi >= end for lo, hi in _merge(cover))


class CoverageUpdate(NamedTuple):
    """What one pass of `annotate_gaps_covered_by` changed. Both directions."""

    marked: list[int]      # gap ids newly annotated `covered_by`
    revoked: list[int]     # gap ids whose annotation the current evidence no longer supports


def annotate_gaps_covered_by(store: Any, label: str,
                             landing_root: Path | str) -> CoverageUpdate:
    """Re-derive `covered_by` for EVERY closed gap, in both directions.

    The gap STAYS a gap in every branch: still in `gap_register`, still open if it
    was open, still counted by `open_gaps()` and `unbackfilled_gaps()`, with its
    start, end, reason, topics and class lists untouched. The only column this
    function can reach is `covered_by`.

    WHY IT CLEARS AS WELL AS SETS (BUG-20260911-077). Coverage is derived from
    evidence that arrives LATE. A connection killed without warning has not yet
    written the gap it is in -- `record_downtime_gap` runs on the next START, not
    on the death -- so for the seconds or hours between the kill and the restart,
    the register shows no gap for it and its last landing file is still "open".
    A fold in that window will annotate, honestly, on the evidence it has. When
    the process comes back and records its downtime, the evidence changes and the
    annotation is false. A first version guarded the write with
    `WHERE covered_by IS NULL`, which made that false claim permanent.

    So every fold recomputes and both transitions are written and logged once --
    once because the write only happens when the value actually changes, so a
    steady state is silent. An annotation is a DERIVED claim about another
    process, held in the operational store (disposable, reconstructible, already
    updated in place by `close_gap`); it is not part of the append-only record and
    correcting it is not editing history. Failing to correct it would be asserting
    "the other one was watching" about a window in which nobody was, which is the
    one claim this system must never make.

    Three gaps are never annotated:

      * one recorded by this same connection -- a connection cannot cover its own
        blindness;
      * one still OPEN -- "covered the whole of it" is not knowable until the gap
        has an end, and a partial claim is refused (see `covers_whole`);
      * one the other connection was itself blind for any part of -- the
        correlated outage, which `effective_coverage` subtracts out.

    It takes the landing ROOT rather than a precomputed interval list on purpose:
    a caller that could pass raw manifest intervals could forget the subtraction,
    and forgetting it is BUG-20260911-074.
    """
    cover = effective_coverage(store, label, landing_root)
    marked: list[int] = []
    revoked: list[int] = []
    for g in store.all_gaps():
        gid = int(g["id"])
        current = g.get("covered_by")
        if (g.get("conn_label") or PRIMARY_LABEL) == label:
            continue                       # not ours to judge either way
        start, end = _ts(g.get("started_at")), _ts(g.get("ended_at"))
        supported = (start is not None and end is not None
                     and covers_whole(cover, start, end))
        if supported and current is None:
            if store.annotate_gap(gid, covered_by=label):
                marked.append(gid)
                log.info("gap %d annotated covered_by=%s: that connection was recording "
                         "through the whole of %s..%s, by its own landing manifest minus its "
                         "own recorded gaps. The gap is UNCHANGED and is still a gap.",
                         gid, label, g.get("started_at"), g.get("ended_at"))
        elif current == label and not supported:
            if store.annotate_gap(gid, covered_by=None):
                revoked.append(gid)
                log.warning("gap %d: covered_by=%s REVOKED. The evidence that supported it no "
                            "longer does -- most likely %s has since recorded its own gap over "
                            "part of %s..%s (a process killed without warning records its "
                            "downtime on the next START, not on the death). The gap is "
                            "UNCHANGED and was always a gap; what changed is the claim that "
                            "another connection covered it.",
                            gid, label, label, g.get("started_at"), g.get("ended_at"))
    return CoverageUpdate(marked, revoked)


# -- the duplicate-fraction monitor ----------------------------------------

@dataclass
class DedupCounts:
    """The duplicate picture over one rolling window. Every fraction carries its n.

    `a_keyable` / `b_keyable` are rows with a dedup key -- i.e. rows that carried
    an `event_timestamp`. `a_undedupable` / `b_undedupable` are rows that did not
    (E-W3): they are COUNTED here and nowhere merged, because without the
    server-assigned timestamp there is no field that is identical across two
    independent sockets and any merge would be a guess.
    """

    window_seconds: float
    a_keyable: int = 0
    b_keyable: int = 0
    a_undedupable: int = 0
    b_undedupable: int = 0
    both: int = 0                 # distinct dedup keys seen by BOTH connections
    a_only: int = 0               # distinct dedup keys seen only by A
    b_only: int = 0
    distinct_keys: int = 0
    # Keys BOTH connections saw, a DIFFERENT number of times (A x 1 / B x 2 -- the
    # shape a rejoin-replay makes). Dedup keeps ONE row per key, so each of these
    # is a place the fold may have undercounted, or a socket replayed; we cannot
    # tell which, and counting is the only honest response. BUG-20260911-073.
    multiplicity_disagreements: int = 0
    # Keys some connection delivered more than once at all.
    repeat_delivery_keys: int = 0

    @staticmethod
    def _frac(num: int, den: int) -> float | None:
        """A fraction, or None when the denominator is zero. Never 0.0 for 'unknown'."""
        return (num / den) if den else None

    def as_dict(self) -> dict[str, Any]:
        # REQ: never a percentage without its count. Every fraction below is
        # emitted next to its numerator AND its denominator, and is None -- not
        # zero -- when there is nothing to divide by.
        return {
            "window_seconds": self.window_seconds,
            "a": {"keyable_n": self.a_keyable, "undedupable_n": self.a_undedupable,
                  "unique_n": self.a_only,
                  "unique_fraction": self._frac(self.a_only, self.a_only + self.both)},
            "b": {"keyable_n": self.b_keyable, "undedupable_n": self.b_undedupable,
                  "unique_n": self.b_only,
                  "unique_fraction": self._frac(self.b_only, self.b_only + self.both)},
            "distinct_keys_n": self.distinct_keys,
            "seen_by_both_n": self.both,
            "multiplicity_disagreements_n": self.multiplicity_disagreements,
            "multiplicity_disagreement_fraction": self._frac(self.multiplicity_disagreements,
                                                             self.both),
            "repeat_delivery_keys_n": self.repeat_delivery_keys,
            "duplicate_fraction_of_a": self._frac(self.both, self.a_only + self.both),
            "duplicate_fraction_of_b": self._frac(self.both, self.b_only + self.both),
            "undedupable_n": self.a_undedupable + self.b_undedupable,
        }


def dedup_counts(conn: Any, *, window_seconds: float, now_ts: float,
                 label_b: str = "b") -> DedupCounts:
    """Count duplicates between A and B over the last `window_seconds` of ARRIVAL.

    Arrival (`observed_ts`), not event time, on purpose: the question is "are the
    two sockets currently seeing the same traffic", and that is a question about
    now, not about when the market moved.
    """
    since = now_ts - window_seconds
    out = DedupCounts(window_seconds=window_seconds)
    rows = conn.execute(
        "SELECT COALESCE(conn, ?) AS c, dedup_key IS NOT NULL AS keyed, COUNT(*) "
        "FROM events WHERE observed_ts >= ? GROUP BY 1, 2",
        (PRIMARY_LABEL, since)).fetchall()
    for c, keyed, n in rows:
        if c == label_b:
            if keyed:
                out.b_keyable += n
            else:
                out.b_undedupable += n
        elif keyed:
            out.a_keyable += n
        else:
            out.a_undedupable += n
    # Two levels of grouping, because the question has two levels: how many
    # copies did each SIDE deliver of each KEY. Every connection that is not the
    # primary counts as side 'b', which is the same split `order_lives`'
    # `deliveries_a` / `deliveries_b` use.
    for in_a, in_b, mn, mx in conn.execute(
            "SELECT MAX(side = 'a'), MAX(side = 'b'), MIN(c), MAX(c) FROM ("
            "  SELECT dedup_key,"
            "         CASE WHEN COALESCE(conn, ?) = ? THEN 'a' ELSE 'b' END side,"
            "         COUNT(*) c"
            "    FROM events WHERE observed_ts >= ? AND dedup_key IS NOT NULL"
            "   GROUP BY dedup_key, side) "
            "GROUP BY dedup_key",
            (PRIMARY_LABEL, PRIMARY_LABEL, since)).fetchall():
        out.distinct_keys += 1
        if mx > 1:
            out.repeat_delivery_keys += 1
        if in_a and in_b:
            out.both += 1
            if mn != mx:
                out.multiplicity_disagreements += 1
        elif in_a:
            out.a_only += 1
        else:
            out.b_only += 1
    return out


@dataclass
class DuplicateFractionMonitor:
    """Alarms when duplicates collapse toward zero while BOTH connections are healthy.

    dataeng failure mode 3, the S0 one. A dedup key that is too wide leaves every
    duplicate in place: counts double, rates double, and the backtest improves.
    The tell is that the OBSERVED duplicate fraction -- which under a healthy
    pair should be high, because most events really are seen twice -- goes to
    zero while both sockets are busy.

    Not alarming when B is down is as important as alarming when it is not. Zero
    duplicates with a dead B is the correct observation, and an alarm there would
    fire constantly through every restart and teach the Operator to ignore the
    one that matters.
    """

    min_events: int = MONITOR_DEFAULTS["min_events"]
    collapse_fraction: float = MONITOR_DEFAULTS["collapse_fraction"]
    multiplicity_disagreement_max: int = MONITOR_DEFAULTS["multiplicity_disagreement_max"]
    label_b: str = "b"
    _state: str | None = field(default=None, repr=False)

    def evaluate(self, counts: DedupCounts) -> dict[str, Any]:
        c = counts.as_dict()
        a_healthy = counts.a_keyable >= self.min_events
        b_healthy = counts.b_keyable >= self.min_events
        frac_a = c["duplicate_fraction_of_a"]
        frac_b = c["duplicate_fraction_of_b"]
        observed = min(x for x in (frac_a, frac_b) if x is not None) \
            if (frac_a is not None or frac_b is not None) else None

        if not (a_healthy and b_healthy):
            down = [lbl for lbl, ok in ((PRIMARY_LABEL, a_healthy), (self.label_b, b_healthy))
                    if not ok]
            status, reason = "ok", (
                f"not evaluated: connection(s) {', '.join(down)} delivered fewer than "
                f"{self.min_events} keyable events in the last {counts.window_seconds:.0f}s "
                f"(a={counts.a_keyable}, b={counts.b_keyable}). Zero duplicates is the CORRECT "
                f"observation when a connection is down, so this is not an alarm.")
        elif observed is not None and observed < self.collapse_fraction:
            status, reason = "warn", (
                f"DUPLICATES HAVE COLLAPSED while both connections are healthy: "
                f"{counts.both} of {counts.a_only + counts.both} of A's keyable events "
                f"were also seen by B "
                f"(fraction {observed:.3f} < {self.collapse_fraction:.2f}), on "
                f"a={counts.a_keyable} / b={counts.b_keyable} keyable events. Most events "
                f"SHOULD be seen twice. The likeliest cause is a dedup key that is too wide, "
                f"which makes every count and every rate double -- a surprisingly good result "
                f"is evidence of a bug, not an edge (docs/05 rule 5).")
        elif counts.multiplicity_disagreements > self.multiplicity_disagreement_max:
            # The OTHER direction, and the one one-per-key dedup creates rather
            # than removes (BUG-20260911-073). A key both connections saw, a
            # different number of times, is a key where either one socket
            # replayed after a rejoin or the other socket dropped a genuine
            # repeat. Dedup keeps one row and cannot tell the two apart, so the
            # count is the honest record of where the answer is uncertain.
            status, reason = "warn", (
                f"{counts.multiplicity_disagreements} of {counts.both} dedup keys seen by "
                f"BOTH connections were delivered a DIFFERENT number of times by each "
                f"(threshold {self.multiplicity_disagreement_max}), on "
                f"a={counts.a_keyable} / b={counts.b_keyable} keyable events in the last "
                f"{counts.window_seconds:.0f}s. Dedup keeps ONE row per key, so each of "
                f"these is a place the fold may have UNDERCOUNTED (one socket dropped a "
                f"genuine repeat) or a socket replayed after a rejoin. The two cannot be "
                f"told apart from the data, which is why this is reported rather than "
                f"resolved.")
        else:
            status, reason = "ok", (
                f"{counts.both} of {counts.a_only + counts.both} of A's keyable events were "
                f"also seen by B, as expected under a healthy pair; the two connections "
                f"agreed on the number of copies of every one of them.")

        transition = status != self._state
        if transition:
            (log.warning if status == "warn" else log.info)(
                "redundancy duplicate monitor: %s -- %s", status, reason)
            self._state = status
        return {"status": status, "reason": reason, "logged_transition": transition,
                "min_events": self.min_events,
                "collapse_fraction": self.collapse_fraction,
                "multiplicity_disagreement_max": self.multiplicity_disagreement_max,
                "a_healthy": a_healthy, "b_healthy": b_healthy, **c}
