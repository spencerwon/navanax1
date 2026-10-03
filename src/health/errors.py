"""Error hierarchy for the health subsystem.

Implements docs/health/05_BUG_TAXONOMY.md §6, mirroring src/navanax/errors.py. The point
of putting severity in the type system is that the halt rules execute without a human
deciding anything: a surface that cannot render the disclaimer renders "unavailable", a
divergence between the two implementations halts release, a registered expectation
that produced no row is an error rather than a quiet gap.

Rules CI enforces:
  * `SurfaceIntegrityError` is never caught and swallowed. Any `except
    SurfaceIntegrityError` that does not re-raise is itself an S1 defect.
  * `ReferenceDivergenceError` halts RELEASE, never computation: the engine keeps
    working, nothing is published until the divergence is explained.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class Severity(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; this must run on 3.10
    """docs/health/05_BUG_TAXONOMY.md §2."""

    S0A = "S0a"  # wrong number on a surface      -> WITHHOLD the value
    S0B = "S0b"  # evidence/equivalence integrity -> HALT RELEASE
    S1 = "S1"  # silent wrongness
    S2 = "S2"  # provenance loss
    S3 = "S3"  # functional break
    S4 = "S4"  # cosmetic


class ErrorClass(str, Enum):  # noqa: UP042 - StrEnum is 3.11+; this must run on 3.10
    """docs/health/05_BUG_TAXONOMY.md §3."""

    MDL = "MDL"  # model
    PRM = "PRM"  # parameter
    EVD = "EVD"  # evidence
    NUM = "NUM"  # numerics
    KBI = "KBI"  # knowledge base integrity
    VAL = "VAL"  # validation harness
    PRS = "PRS"  # presentation
    ETH = "ETH"  # ethics / framing
    CFG = "CFG"  # configuration
    INF = "INF"  # infrastructure
    SEC = "SEC"  # security


class HealthError(Exception):
    """Base. Never raised directly.

    Every raise carries enough context to locate the offending record without a search:
    which parameter, which scenario, which evidence id, which model version.
    """

    severity: Severity = Severity.S3
    error_class: ErrorClass = ErrorClass.INF
    withholds_value: bool = False
    halts_release: bool = False

    def __init__(
        self,
        message: str,
        *,
        expected: Any = None,
        received: Any = None,
        parameter: str | None = None,
        scenario: str | None = None,
        evidence_id: str | None = None,
        model_version: str | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.received = received
        self.parameter = parameter
        self.scenario = scenario
        self.evidence_id = evidence_id
        self.model_version = model_version
        self.context = context

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "severity": self.severity.value,
            "class": self.error_class.value,
            "message": self.message,
            "expected": self.expected,
            "received": self.received,
            "parameter": self.parameter,
            "scenario": self.scenario,
            "evidence_id": self.evidence_id,
            "model_version": self.model_version,
            "withholds_value": self.withholds_value,
            "halts_release": self.halts_release,
            **self.context,
        }

    def __str__(self) -> str:
        bits = [f"[{self.severity.value}/{self.error_class.value}] {self.message}"]
        if self.parameter:
            bits.append(f"parameter={self.parameter}")
        if self.scenario:
            bits.append(f"scenario={self.scenario}")
        if self.evidence_id:
            bits.append(f"evidence={self.evidence_id}")
        if self.expected is not None or self.received is not None:
            bits.append(f"expected={self.expected!r} received={self.received!r}")
        if self.model_version:
            bits.append(f"model={self.model_version}")
        return " | ".join(bits)


# --------------------------------------------------------------------------
# S0a - a number a reader could act on is wrong or wrongly framed.
#       Handler WITHHOLDS the value: the surface shows "unavailable".
# --------------------------------------------------------------------------
class SurfaceIntegrityError(HealthError):
    severity = Severity.S0A
    error_class = ErrorClass.PRS
    withholds_value = True


class MissingDisclaimerError(SurfaceIntegrityError):
    """A result object or surface lacks the in-band disclaimer (HREQ-S-01)."""

    error_class = ErrorClass.ETH


class UnlabelledIndexError(SurfaceIntegrityError):
    """An index (the kidney strain index) is shown without the words that say it is
    an index and not a clinical measure (HREQ-S-04)."""

    error_class = ErrorClass.ETH


class UnlabelledThresholdError(SurfaceIntegrityError):
    """A classification threshold (135 or 145 mmol/L plasma sodium) is drawn as if it
    were a physiological or safety limit, without the label HREQ-S-03 requires."""

    error_class = ErrorClass.ETH


# --------------------------------------------------------------------------
# S0b - evidence or equivalence integrity. HALTS RELEASE, not computation.
# --------------------------------------------------------------------------
class EvidenceIntegrityError(HealthError):
    severity = Severity.S0B
    error_class = ErrorClass.EVD
    halts_release = True


class UnresolvedEvidenceError(EvidenceIntegrityError):
    """A cited evidence id does not exist in the knowledge base, or its DOI/PMID does
    not resolve."""

    error_class = ErrorClass.KBI


class MisquotedSourceError(EvidenceIntegrityError):
    """A quantity attributes to a source a value the source does not state."""


class ReferenceDivergenceError(EvidenceIntegrityError):
    """The Python engine and the JavaScript reference disagree on a golden value beyond
    the equivalence tolerances (tests/health_selftest.py REL_TOL/ABS_TOL, mirrored in
    config/health/base.yaml equivalence; HREQ-P-02). Not raised anywhere yet: a
    divergence fails the golden tests (ADR-0002 errata).

    Every result since the last passing golden run is suspect until the divergence is
    explained. Not a normal test failure: it retroactively taints completed work.
    """

    error_class = ErrorClass.NUM


# --------------------------------------------------------------------------
# S1 - silent wrongness. Alert, non-suppressible.
# --------------------------------------------------------------------------
class SilentWrongnessError(HealthError):
    severity = Severity.S1
    error_class = ErrorClass.MDL


class MissingUncertaintyError(SilentWrongnessError):
    """A point estimate was produced or shown without its band (HREQ-U)."""

    error_class = ErrorClass.PRS


class CalibrationAsValidationError(SilentWrongnessError):
    """An expectation a parameter was tuned to is being counted as a validation pass
    (docs/health/01 §6)."""

    error_class = ErrorClass.VAL


class UnitMismatchError(SilentWrongnessError):
    error_class = ErrorClass.PRM


class ExpectationSkippedError(SilentWrongnessError):
    """A registered expectation produced no harness row (HREQ-P-05).

    The harness returns exactly one row per `expects` entry. Fewer rows means the harness
    shrank, and a harness that can shrink can hide a failing expectation.
    """

    error_class = ErrorClass.VAL


# --------------------------------------------------------------------------
# S2 - provenance loss.
# --------------------------------------------------------------------------
class ProvenanceError(HealthError):
    severity = Severity.S2
    error_class = ErrorClass.KBI


class UngradedValueError(ProvenanceError):
    """A numeric value without a resolvable source or a grade (HREQ-D-01)."""

    error_class = ErrorClass.PRM


class InPlaceEditError(ProvenanceError):
    """A knowledge-base record was changed in place instead of superseded (HREQ-D-04)."""


# --------------------------------------------------------------------------
# S3 - numerics and operations.
# --------------------------------------------------------------------------
class NumericalError(HealthError):
    severity = Severity.S3
    error_class = ErrorClass.NUM


class InfeasibleParametersError(NumericalError):
    """No steady state exists inside the parameter ranges (required urine osmolality
    outside (U_osm_min, U_osm_max), or non-positive urine flow).

    An ERROR, not a warning: the deterministic path refuses; the Monte Carlo sampler
    counts the rejection and reports it (HREQ-P-04).
    """


class StepControlError(NumericalError):
    """The solver was asked for a step that violates its stability bound."""


class NonFiniteTrajectoryError(NumericalError):
    """A simulated trajectory holds a NaN or infinite state, ledger or derived value
    (HREQ-V-07): the run fails and nothing from it is displayed or summarised.

    Context: scenario, the first output time and key that went non-finite, model version.
    """


class OperationalError(HealthError):
    severity = Severity.S3


class PersistenceError(OperationalError):
    pass


class ConfigurationError(OperationalError):
    error_class = ErrorClass.CFG
