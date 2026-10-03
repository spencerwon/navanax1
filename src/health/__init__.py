"""Navanax health subsystem: mechanistic physiology models for education and research.

EDUCATIONAL MODEL -- NOT MEDICAL ADVICE. Nothing in this package diagnoses, doses or
recommends; every result the engine returns carries the disclaimer as a field
(HREQ-S-01, docs/health/00_REQUIREMENTS.md).

Subpackages:
    health.engine   the Metabolic Map V1 body-fluid / electrolyte / renal ODE model,
                    ported from the JavaScript reference in reference/metabolic-map-v1/engine
                    and proven equivalent to it by tests/health_selftest.py.
"""

from __future__ import annotations

__version__ = "0.1.0"
