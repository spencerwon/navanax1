"""REST budget governor -- the single gate every OpenSea REST call passes through.

docs/00_REQUIREMENTS.md REQ-D-01..REQ-D-04.

The free tier allows 600 reads/hour on a token bucket shared across every key on
the account. That is roughly one request every six seconds. The obvious design --
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
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable


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
        capacity: float = 600.0,
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
            effective = self.state.tokens
            if self.state.server_remaining is not None:
                effective = min(effective, float(self.state.server_remaining))
            if effective < n:
                return False
            self.state.tokens -= n
            if self.state.server_remaining is not None:
                self.state.server_remaining = max(0, self.state.server_remaining - int(n))
            return True

    def seconds_until(self, n: float = 1.0) -> float:
        with self._lock:
            self._refill()
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
        """The server said no. Believe it over the local model."""
        with self._lock:
            self.state.tokens = 0.0
            self.state.server_remaining = 0
            self.state.consecutive_429 += 1

    def observe_success(self) -> None:
        with self._lock:
            self.state.consecutive_429 = 0


@dataclass(order=True)
class _Waiter:
    priority: int
    seq: int
    event: asyncio.Event = field(compare=False)
    cost: float = field(default=1.0, compare=False)
    cancelled: bool = field(default=False, compare=False)


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
        # budget: BACKFILL may not draw the bucket below 60 tokens, so an
        # INTERACTIVE request arriving later still finds something left.
        self.reserve: dict[Priority, float] = reserve or {
            Priority.INTERACTIVE: 0.0,
            Priority.SIGNAL: 20.0,
            Priority.BACKFILL: 60.0,
            Priority.MAINTENANCE: 120.0,
        }
        self.max_backoff = max_backoff
        self._sleep = sleep or asyncio.sleep
        self._waiters: list[_Waiter] = []
        self._seq = 0
        self._lock = asyncio.Lock()
        self.stats: dict[str, int] = {
            "granted": 0,
            "denied_429": 0,
            "waits": 0,
            "bypass_attempts": 0,
        }

    def _floor(self, priority: Priority) -> float:
        return self.reserve.get(priority, 0.0)

    async def acquire(self, priority: Priority, cost: float = 1.0, timeout: float | None = None) -> None:
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
    def __init__(self, gov: RestGovernor, priority: Priority, cost: float, timeout: float | None) -> None:
        self._gov = gov
        self._priority = priority
        self._cost = cost
        self._timeout = timeout

    async def __aenter__(self) -> RestGovernor:
        await self._gov.acquire(self._priority, self._cost, self._timeout)
        return self._gov

    async def __aexit__(self, *exc: Any) -> None:
        return None
