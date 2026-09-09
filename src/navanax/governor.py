"""REST budget governor -- the single gate every OpenSea REST call passes through.

docs/00_REQUIREMENTS.md REQ-D-01..REQ-D-04.

The free tier allows 120 reads/hour on a token bucket shared across every key on
the account -- MEASURED from live x-ratelimit-limit headers 2026-09-09; the 600
figure this project previously assumed was never verified and is wrong. That is
one request every 30 seconds. The obvious design --
each component calls the API when it needs to -- fails immediately: whichever
component asks first consumes the budget, and it is always the background
backfiller, because it never stops asking. Production ingestion then starves and
the failure presents as an unrelated outage.

So: no component calls REST directly. Everything goes through here, and this
module decides who goes next.

Priority classes (REQ-D-03), strictly ordered:

    INTERACTIVE  Spencer is sitting there waiting
    SIGNAL       a candidate needs confirmation before an idea is emitted
    BACKFILL     historical fill
    MAINTENANCE  routine refresh, reconciliation

Lower classes are starved before higher ones. That is the intent, not a bug: a
backfill that takes an extra hour costs nothing, and a dashboard that hangs for
a minute costs the tool's credibility.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class Priority(IntEnum):
    """Lower value == higher priority (heap order)."""

    INTERACTIVE = 0
    SIGNAL = 1
    BACKFILL = 2
    MAINTENANCE = 3


@dataclass
class BudgetState:
    capacity: float
    tokens: float
    refill_per_second: float
    updated_at: float
    # Populated from response headers when the API supplies them; None means we
    # are running on the local model alone.
    server_remaining: int | None = None
    server_reset_at: float | None = None
    consecutive_429: int = 0
    # BUG-20260909-012: when the server's "no" expires. None means not blocked.
    # Monotonic seconds, not wall clock, so a clock change cannot extend it.
    server_block_until: float | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "tokens": round(self.tokens, 2),
            "refill_per_second": self.refill_per_second,
            "server_remaining": self.server_remaining,
            "consecutive_429": self.consecutive_429,
        }


class TokenBucket:
    """Local model of the server's bucket.

    REQ-D-02: the governor prefers rate-limit headers where the API supplies
    them, and falls back to this local model where it does not. It must not
    depend on header presence for correctness -- OpenSea's published contract
    describes a shared bucket and a 429, and does not promise headers on every
    response.
    """

    def __init__(
        self,
        capacity: float = 120.0,   # measured; see config/base.yaml rest_budget
        per_seconds: float = 3600.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        start_full: bool = True,
    ) -> None:
        self._clock = clock
        self.state = BudgetState(
            capacity=capacity,
            tokens=capacity if start_full else 0.0,
            refill_per_second=capacity / per_seconds,
            updated_at=clock(),
        )
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self.state.updated_at
        if elapsed > 0:
            self.state.tokens = min(
                self.state.capacity,
                self.state.tokens + elapsed * self.state.refill_per_second,
            )
            self.state.updated_at = now

    def available(self) -> float:
        with self._lock:
            self._refill()
            if self.state.server_remaining is not None:
                # Trust the server over the local model when we have it, but
                # never let it inflate our estimate above the local one --
                # a stale header should not license a burst.
                return min(self.state.tokens, float(self.state.server_remaining))
            return self.state.tokens

    def try_consume(self, n: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            self._expire_server_block()
            effective = self.state.tokens
            if self.state.server_remaining is not None:
                effective = min(effective, float(self.state.server_remaining))
            if effective < n:
                return False
            self.state.tokens -= n
            if self.state.server_remaining is not None:
                self.state.server_remaining = max(0, self.state.server_remaining - int(n))
            return True

    def _expire_server_block(self) -> None:
        """Release a server-imposed block once its deadline has passed.

        Without this the governor never recovers from a 429 (BUG-012). With it,
        `server_remaining` is cleared -- back to the LOCAL model, which refills
        on its own -- rather than being set to some guessed positive number we
        have no evidence for.
        """
        until = self.state.server_block_until
        if until is not None and self._clock() >= until:
            self.state.server_block_until = None
            self.state.server_remaining = None
        reset = self.state.server_reset_at
        if (reset is not None and self.state.server_remaining == 0
                and time.time() >= reset):
            self.state.server_reset_at = None
            self.state.server_remaining = None

    def seconds_until(self, n: float = 1.0) -> float:
        with self._lock:
            self._refill()
            self._expire_server_block()
            if self.state.server_remaining == 0:
                until = self.state.server_block_until
                if until is not None:
                    return max(0.0, until - self._clock())
                if self.state.server_reset_at is not None:
                    return max(0.0, self.state.server_reset_at - time.time())
            deficit = n - self.state.tokens
            if deficit <= 0:
                return 0.0
            return deficit / self.state.refill_per_second

    def observe_headers(self, headers: dict[str, str]) -> None:
        """Adopt server-reported budget where present.

        Header names are not part of the published contract, so several
        spellings are accepted and absence is normal, not an error.
        """
        with self._lock:
            for k in ("x-ratelimit-remaining", "X-RateLimit-Remaining", "ratelimit-remaining"):
                if k in headers:
                    try:
                        self.state.server_remaining = int(headers[k])
                    except (TypeError, ValueError):
                        pass
                    break
            for k in ("x-ratelimit-reset", "X-RateLimit-Reset", "ratelimit-reset"):
                if k in headers:
                    try:
                        self.state.server_reset_at = float(headers[k])
                    except (TypeError, ValueError):
                        pass
                    break

    def observe_429(self) -> None:
        """The server said no. Believe it over the local model -- for a while.

        BUG-20260909-012. This used to set `server_remaining = 0` with no way
        back: `try_consume` takes `min(tokens, server_remaining)`, and only a
        header from a SUCCESSFUL response could raise it again -- which
        required a token. So a single 429 disabled REST for the life of the
        process, silently, and the failure presented as an unrelated outage.
        The recovery path required the very resource the failure removed.

        The fix is to make the server's "no" EXPIRE. `server_reset_at` was
        already being parsed and read nowhere; it is the answer the server
        itself gives. Absent that header, fall back to a bounded backoff so the
        governor always recovers on its own.
        """
        with self._lock:
            self.state.tokens = 0.0
            self.state.server_remaining = 0
            self.state.consecutive_429 += 1
            if self.state.server_reset_at is None:
                # No Retry-After / reset header: exponential, capped at 15 min.
                # Long enough not to hammer a limit we have just hit; short
                # enough that an unattended process heals itself overnight.
                delay = min(900.0, 30.0 * (2 ** (self.state.consecutive_429 - 1)))
                self.state.server_block_until = self._clock() + delay

    def observe_success(self) -> None:
        """A call went through. The server's last "no" is now stale evidence.

        `server_remaining` is cleared to None rather than to a guessed positive
        number: None means "fall back to the local token bucket", which refills
        on its own and is the only model we have actual evidence for. Leaving it
        at 0 was the other half of BUG-012 -- the block expired and the ceiling
        did not, so REST stayed disabled anyway.
        """
        with self._lock:
            self.state.consecutive_429 = 0
            self.state.server_block_until = None
            if self.state.server_remaining == 0:
                self.state.server_remaining = None


# BUG-20260909-016. A `_Waiter` dataclass and a `self._waiters` list used to
# live here. Nothing ever appended to them and nothing ever read them, so they
# read as an implemented priority queue that did not exist. Removed rather than
# implemented, because the reserve floors already produce the ordering that
# matters and a real queue is not needed until there are concurrent REST
# callers -- which Phase 0 has none of.
#
# The consequence, stated so it is not a surprise later: ordering between
# classes is EMERGENT from the reserve floors, not guaranteed. INTERACTIVE has
# a floor of 0 and MAINTENANCE a floor of 24, so INTERACTIVE can always draw
# when anything is left and MAINTENANCE starves first -- but a MAINTENANCE call
# arriving while 30 tokens remain will succeed even if an INTERACTIVE call
# arrived a millisecond earlier and is still being awaited. Fix that with a
# real queue when concurrent callers exist, not before.


def governor_from_config(cfg: dict, **kw) -> RestGovernor:
    """Build a governor from `config/base.yaml`'s `rest_budget` block.

    BUG-20260909-015 / REQ-N-09: "no threshold, weight, interval or assumption
    lives in code." The rest_budget block carried the only provenance-tagged
    MEASURED numbers in the repo and was parsed by nothing -- an operator could
    change `capacity: 120` and see no effect and no error. A config block
    nothing reads is worse than no config: it looks like a knob and turns
    nothing.
    """
    rb = (cfg or {}).get("rest_budget") or {}
    reserve_cfg = rb.get("reserve") or {}
    reserve: dict[Priority, float] | None = None
    if reserve_cfg:
        reserve = {}
        for name, value in reserve_cfg.items():
            try:
                reserve[Priority[str(name).upper()]] = float(value)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"config rest_budget.reserve has an unknown priority {name!r}. "
                    f"Valid: {', '.join(p.name for p in Priority)}"
                ) from exc
    bucket = TokenBucket(
        capacity=float(rb.get("capacity", 120.0)),
        per_seconds=float(rb.get("per_seconds", 3600.0)),
    )
    return RestGovernor(bucket=bucket, reserve=reserve, **kw)


class RestGovernor:
    """Priority-ordered gate in front of every REST call.

    Usage:

        async with governor.slot(Priority.INTERACTIVE):
            resp = await http.get(...)
            governor.observe_response(resp.status, resp.headers)

    `observe_response` is not optional. Without it the governor never learns
    about 429s and its local model drifts away from the server's.
    """

    def __init__(
        self,
        bucket: TokenBucket | None = None,
        *,
        reserve: dict[Priority, float] | None = None,
        max_backoff: float = 300.0,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.bucket = bucket or TokenBucket()
        # Floors that keep low-priority work from consuming the last of the
        # budget: BACKFILL may not draw the bucket below 12 tokens, so an
        # INTERACTIVE request arriving later still finds something left.
        self.reserve: dict[Priority, float] = reserve or {
            Priority.INTERACTIVE: 0.0,
            Priority.SIGNAL: 4.0,
            Priority.BACKFILL: 12.0,
            Priority.MAINTENANCE: 24.0,
        }
        self.max_backoff = max_backoff
        self._sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self.stats: dict[str, int] = {
            "granted": 0,
            "denied_429": 0,
            "waits": 0,
            "bypass_attempts": 0,
        }

    def _floor(self, priority: Priority) -> float:
        return self.reserve.get(priority, 0.0)

    async def acquire(self, priority: Priority, cost: float = 1.0,
                      timeout: float | None = None) -> None:
        """Block until this priority may spend `cost` tokens."""
        deadline = None if timeout is None else time.monotonic() + timeout
        backoff = 1.0
        while True:
            async with self._lock:
                available = self.bucket.available()
                if available - self._floor(priority) >= cost and self.bucket.try_consume(cost):
                    self.stats["granted"] += 1
                    return
            self.stats["waits"] += 1
            wait = self.bucket.seconds_until(cost + self._floor(priority))
            if self.bucket.state.consecutive_429:
                # Exponential backoff with full jitter (REQ-D-04). Jitter
                # matters because several workers backing off in lockstep
                # reconverge into another burst.
                backoff = min(self.max_backoff, backoff * 2)
                wait = max(wait, random.uniform(0, backoff))
            wait = max(0.05, min(wait, self.max_backoff))
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    from .errors import BudgetExhaustedError

                    raise BudgetExhaustedError(
                        "REST budget unavailable within timeout",
                        expected=f">={cost} tokens above floor {self._floor(priority)}",
                        received=f"{self.bucket.available():.1f} tokens",
                        priority=priority.name,
                    )
                wait = min(wait, remaining)
            await self._sleep(wait)

    def slot(self, priority: Priority, cost: float = 1.0, timeout: float | None = None):
        return _Slot(self, priority, cost, timeout)

    def observe_response(self, status: int, headers: dict[str, str] | None = None) -> None:
        if headers:
            self.bucket.observe_headers(headers)
        if status == 429:
            self.stats["denied_429"] += 1
            self.bucket.observe_429()
        elif 200 <= status < 300:
            self.bucket.observe_success()

    def note_bypass_attempt(self, where: str) -> None:
        """Record that something tried to call REST without a slot.

        docs/05_BUG_TAXONOMY.md rates GovernorBypassError as S1 even though
        nothing visibly breaks, because an unregulated caller will eventually
        starve production ingestion and the outage will look unrelated.
        """
        from .errors import GovernorBypassError

        self.stats["bypass_attempts"] += 1
        raise GovernorBypassError(
            "REST call attempted outside the governor",
            expected="all REST traffic routed through RestGovernor.slot()",
            received=where,
        )

    def snapshot(self) -> dict[str, Any]:
        return {"bucket": self.bucket.state.snapshot(), "stats": dict(self.stats)}


class _Slot:
    def __init__(self, gov: RestGovernor, priority: Priority, cost: float,
                 timeout: float | None) -> None:
        self._gov = gov
        self._priority = priority
        self._cost = cost
        self._timeout = timeout

    async def __aenter__(self) -> RestGovernor:
        await self._gov.acquire(self._priority, self._cost, self._timeout)
        return self._gov

    async def __aexit__(self, *exc: Any) -> None:
        return None
