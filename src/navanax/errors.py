"""Error hierarchy for the Navanax platform.

Implements docs/05_BUG_TAXONOMY.md §6. The point of putting severity in the type
system rather than in a human's head is that the halt-on-corruption rule then
actually executes, at 2am, without anyone deciding anything.

Two rules that CI enforces:
  * `DataIntegrityError` is never caught and swallowed. Any `except
    DataIntegrityError` that does not re-raise is itself an S1 defect.
  * `BacktestIntegrityError` halts BACKTESTS, never ingestion. The fault is in
    the backtest accessor, which writes nothing to the store; halting ingestion
    would manufacture a real S2 gap in response to a contained bug.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class Severity(str, Enum):
    """docs/05_BUG_TAXONOMY.md §2."""

    S0A = "S0a"  # data corruption      -> HALT INGESTION
    S0B = "S0b"  # backtest integrity   -> HALT BACKTESTS (not ingestion)
    S1 = "S1"  # silent wrongness
    S2 = "S2"  # data loss
    S3 = "S3"  # functional break
    S4 = "S4"  # degradation / cosmetic


class ErrorClass(str, Enum):
    """docs/05_BUG_TAXONOMY.md §3."""

    ING = "ING"
    RTL = "RTL"
    NRM = "NRM"
    TMP = "TMP"
    STA = "STA"
    BTI = "BTI"
    PRS = "PRS"
    CFG = "CFG"
    INF = "INF"
    SEC = "SEC"


class NavanaxError(Exception):
    """Base. Never raised directly.

    Every raise carries enough context to locate the offending record without a
    search: an error message without the record id costs an hour.
    """

    severity: Severity = Severity.S3
    error_class: ErrorClass = ErrorClass.INF
    halts_ingestion: bool = False
    halts_backtests: bool = False

    def __init__(
        self,
        message: str,
        *,
        expected: Any = None,
        received: Any = None,
        record_id: str | None = None,
        ingestion_run_id: str | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.received = received
        self.record_id = record_id
        self.ingestion_run_id = ingestion_run_id
        self.context = context

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "severity": self.severity.value,
            "class": self.error_class.value,
            "message": self.message,
            "expected": self.expected,
            "received": self.received,
            "record_id": self.record_id,
            "ingestion_run_id": self.ingestion_run_id,
            "halts_ingestion": self.halts_ingestion,
            "halts_backtests": self.halts_backtests,
            **self.context,
        }

    def __str__(self) -> str:
        bits = [f"[{self.severity.value}/{self.error_class.value}] {self.message}"]
        if self.record_id:
            bits.append(f"record={self.record_id}")
        if self.expected is not None or self.received is not None:
            bits.append(f"expected={self.expected!r} received={self.received!r}")
        if self.ingestion_run_id:
            bits.append(f"run={self.ingestion_run_id}")
        return " | ".join(bits)


# --------------------------------------------------------------------------
# S0a - data corruption. Handler HALTS INGESTION.
# --------------------------------------------------------------------------
class DataIntegrityError(NavanaxError):
    severity = Severity.S0A
    error_class = ErrorClass.NRM
    halts_ingestion = True


class CorruptRecordError(DataIntegrityError):
    pass


class BitemporalViolationError(DataIntegrityError):
    error_class = ErrorClass.TMP


class ImmutableStoreWriteError(DataIntegrityError):
    """An attempt was made to modify or delete a landing-zone record.

    The landing zone is append-only for every agent at every authority level.
    Reaching this exception means a code path exists that should not.
    """


class ChecksumMismatchError(DataIntegrityError):
    """A landing-zone file's SHA-256 no longer matches its manifest entry."""


# --------------------------------------------------------------------------
# S0b - backtest integrity. HALTS BACKTESTS, NOT ingestion.
# --------------------------------------------------------------------------
class BacktestIntegrityError(NavanaxError):
    severity = Severity.S0B
    error_class = ErrorClass.BTI
    halts_ingestion = False
    halts_backtests = True


class LeakageDetectedError(BacktestIntegrityError):
    """A backtest read a record whose observed_at exceeds the simulation as_of."""


class LeakageTrapPassedError(BacktestIntegrityError):
    """The planted look-ahead strategy reported a profit.

    The backtester's integrity guarantee is broken, and every backtest result
    produced since the last passing trap run is invalid. This is not a normal
    test failure -- it retroactively invalidates completed work.
    """


# --------------------------------------------------------------------------
# S1 - silent wrongness. Alert, non-suppressible.
# --------------------------------------------------------------------------
class SilentWrongnessError(NavanaxError):
    severity = Severity.S1
    error_class = ErrorClass.STA


class UnitMismatchError(SilentWrongnessError):
    error_class = ErrorClass.NRM


class PrecisionLossError(SilentWrongnessError):
    error_class = ErrorClass.NRM


class AssumptionViolationError(SilentWrongnessError):
    pass


class InsufficientSampleError(SilentWrongnessError):
    """Below the docs/01_METHODOLOGY.md §8.2 minimum for this analysis.

    This is an ERROR, not a warning. The system refuses to produce a number it
    cannot support, which is what makes "never a point estimate without its
    uncertainty" structural rather than aspirational.
    """


class GovernorBypassError(SilentWrongnessError):
    """A component called REST without going through the governor.

    S1 even though nothing visibly broke: an unregulated caller will eventually
    exhaust the shared budget and starve production ingestion, and that failure
    will present as an unrelated outage.
    """

    error_class = ErrorClass.RTL


# --------------------------------------------------------------------------
# S2 - data availability. Record the gap. Backfill where REST allows;
#      NEVER interpolate, forward-fill, or synthesize.
# --------------------------------------------------------------------------
class DataAvailabilityError(NavanaxError):
    severity = Severity.S2
    error_class = ErrorClass.ING


class StreamGapError(DataAvailabilityError):
    pass


class IrrecoverableGapError(DataAvailabilityError):
    """An event class the events endpoint cannot backfill (REQ-D-09a).

    Cancellations, order invalidate/revalidate and metadata updates are gone
    after a disconnect. Note item_received_bid is NOT in this set -- it is an
    item-level offer and is recoverable via the events endpoint's `offer` type.
    """


class BackfillFailedError(DataAvailabilityError):
    pass


# --------------------------------------------------------------------------
# Rate limit
# --------------------------------------------------------------------------
class RateLimitError(NavanaxError):
    severity = Severity.S2
    error_class = ErrorClass.RTL


class BudgetExhaustedError(RateLimitError):
    pass


class KeyExpiredError(RateLimitError):
    """Free instant OpenSea keys expire after 7 days.

    A silently expired key is indistinguishable from an API outage, which is a
    bad afternoon. Detect and alert rather than degrading quietly.
    """

    severity = Severity.S1


# --------------------------------------------------------------------------
# S3 - operational
# --------------------------------------------------------------------------
class OperationalError(NavanaxError):
    severity = Severity.S3


class UpstreamUnavailableError(OperationalError):
    error_class = ErrorClass.ING


class SchemaConformanceError(OperationalError):
    error_class = ErrorClass.NRM


class PersistenceError(OperationalError):
    pass


class ConfigurationError(OperationalError):
    error_class = ErrorClass.CFG
