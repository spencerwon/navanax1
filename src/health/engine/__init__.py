"""Metabolic Map V1 engine: body fluid, sodium, the kidney and arterial pressure.

EDUCATIONAL MODEL — NOT MEDICAL ADVICE. Not clinically validated.

A pure-standard-library Python port of the JavaScript reference engine in
reference/metabolic-map-v1/engine (index.js, model.js, solver.js, scenarios.js, mc.js).
tests/health_selftest.py proves the port reproduces the reference to 1e-9 relative on
the golden fixture tests/fixtures/health/golden_v1.json, which tools/health_golden.mjs
generates from the JavaScript.

Module map (each mirrors one JavaScript file, block for block):
    params     params.json loader            (model.js defaultParams / paramTable)
    model      state, fluxes, rhs, derived   (model.js, blocks M0–M10)
    solver     fixed-step RK4 with events    (solver.js)
    scenarios  named interventions           (scenarios.js)
    mc         PRNG, sampler, quantiles      (mc.js)
    api        simulate, Monte Carlo, sweep, sensitivity screen and its union (index.js)
    validate   literature-expectation harness (Python only; no JS counterpart)

Example:
    >>> from health.engine import simulate
    >>> r = simulate(scenario="drink_water_1L")
    >>> round(min(r["derived"]["Na_plasma"]) - r["derived"]["Na_plasma"][0], 1)  # doctest: +SKIP
"""

from __future__ import annotations

from .api import (
    DISCLAIMER,
    INFLUENCE_DEFAULTS,
    INFLUENCE_DIRECTIONS,
    MAX_STEP_FRACTION_OF_TAU_MIN,
    MAX_TRIES_PER_SAMPLE,
    MODEL_VERSION,
    VALIDATION_STATUS,
    compute_influence,
    compute_influence_all,
    draw_samples,
    influence,
    influence_union,
    nearest_index,
    param_summary,
    params_for,
    prime_influence,
    resolve_scenario,
    result_meta,
    salt_load_metrics,
    simulate,
    simulate_mc,
    simulate_sweep,
)
from .mc import (
    LOG_UNIFORM_RATIO,
    imul,
    mulberry32,
    quantile_bands,
    quantile_sorted,
    sample_params,
    sampling_mode,
)
from .model import (
    DERIVED_KEYS,
    GFR_NORM_BSA_M2,
    IDX,
    LEDGER_KEYS,
    REFERENCE_PERSON,
    STATE_KEYS,
    InfeasibleParametersError,
    InfeasibleParamsError,
    baseline_inputs,
    constants,
    default_params,
    derived,
    fluxes,
    initial_state,
    param_table,
    rhs,
)
from .scenarios import (
    MMOL_NA_PER_G_NACL,
    SCENARIOS,
    Scenario,
    Sweep,
    make_salt_load,
    make_scenario,
    make_water_load,
)

__all__ = [
    "DERIVED_KEYS", "DISCLAIMER", "GFR_NORM_BSA_M2", "IDX", "INFLUENCE_DEFAULTS",
    "INFLUENCE_DIRECTIONS",
    "InfeasibleParametersError", "InfeasibleParamsError", "LEDGER_KEYS", "LOG_UNIFORM_RATIO",
    "MAX_STEP_FRACTION_OF_TAU_MIN", "MAX_TRIES_PER_SAMPLE", "MMOL_NA_PER_G_NACL", "MODEL_VERSION",
    "REFERENCE_PERSON", "SCENARIOS", "STATE_KEYS", "Scenario", "Sweep", "VALIDATION_STATUS", "baseline_inputs",
    "compute_influence", "compute_influence_all", "constants", "default_params", "derived", "draw_samples", "fluxes",
    "imul", "influence", "influence_union", "initial_state", "make_salt_load", "make_scenario", "make_water_load",
    "mulberry32", "nearest_index", "param_summary", "param_table", "params_for", "prime_influence",
    "quantile_bands", "quantile_sorted", "resolve_scenario", "result_meta", "rhs", "salt_load_metrics",
    "sample_params", "sampling_mode", "simulate", "simulate_mc", "simulate_sweep",
]
