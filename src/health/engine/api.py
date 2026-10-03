"""Public API of the V1 engine (port of engine/index.js, spec §3).

EDUCATIONAL MODEL -- NOT MEDICAL ADVICE.

Naming: functions take snake_case keyword arguments (`t_end`, `out_every`); the result
dictionaries they return keep the JavaScript keys (`t`, `Y`, `states`, `derived`,
`ledger`, `meta.maxStep`, `q05`, ...), so a consumer of the JavaScript engine reads a
Python result unchanged. Every result carries the disclaimer as a field (HREQ-S-01).

Deliberate deviations from index.js (each one refuses where the reference would return
a number nobody should read; none changes a finite result):
  * simulate() raises health.errors.NonFiniteTrajectoryError (S3/NUM) when any state,
    ledger or derived value of the trajectory is NaN or infinite (HREQ-V-07); index.js
    returns the NaN trajectory. Consequences: simulate_mc() and simulate_sweep() raise
    instead of returning NaN bands, and compute_influence() counts such a perturbation as
    infeasible (effect = Infinity on every key, listed in meta.infeasible), where index.js
    lists only thrown errors there.
  * model.py: a zero divisor in the hot path raises ZeroDivisionError (see its header).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from ..errors import HealthError, InfeasibleParametersError, NonFiniteTrajectoryError
from .mc import mulberry32, quantile_bands, sample_params
from .model import (
    DERIVED_KEYS,
    LEDGER_KEYS,
    STATE_KEYS,
    constants,
    default_params,
    derived,
    initial_state,
    param_table,
    rhs,
)
from .scenarios import SCENARIOS, Scenario
from .solver import integrate

__all__ = [
    "DISCLAIMER", "INFLUENCE_DEFAULTS", "MAX_STEP_FRACTION_OF_TAU_MIN", "MAX_TRIES_PER_SAMPLE",
    "MODEL_VERSION", "VALIDATION_STATUS", "compute_influence", "draw_samples",
    "influence", "nearest_index", "param_summary", "params_for", "prime_influence",
    "resolve_scenario", "result_meta", "salt_load_metrics", "simulate", "simulate_mc", "simulate_sweep",
]

MODEL_VERSION = "1.1.0"
#: Byte-equal to DISCLAIMER in index.js (golden equivalence).
DISCLAIMER = "Educational model — not medical advice."
#: HREQ-S-01 as amended: carried beside the disclaimer on every result as
#: meta.validation_status (config/health/base.yaml model.validation_status). Byte-equal to
#: VALIDATION_STATUS in index.js since model 1.1.0 (BUG-20261003-104), which puts it in the
#: same places: meta of every result, and top-level beside the top-level disclaimer of
#: simulateMC / simulateSweep.
VALIDATION_STATUS = "Not clinically validated."


def result_meta(**extra: Any) -> dict[str, Any]:
    """The `meta` every public result carries: disclaimer, validation status, model version
    (the same keys, in the same order, as resultMeta() in index.js)."""
    return {"disclaimer": DISCLAIMER, "validation_status": VALIDATION_STATUS,
            "modelVersion": MODEL_VERSION, **extra}

# Literal constants of index.js, named so config/health/base.yaml can be checked against them.
#: Solver sub-step bound: maxStep = min(dt, MAX_STEP_FRACTION_OF_TAU_MIN · tau_min).
MAX_STEP_FRACTION_OF_TAU_MIN = 0.25
#: draw_samples gives up after MAX_TRIES_PER_SAMPLE · n draws.
MAX_TRIES_PER_SAMPLE = 50


def _js_max(a: float, b: float) -> float:
    """Math.max(a, b): NaN if either is NaN; +0 beats -0."""
    if a != a or b != b:
        return math.nan
    if a == b == 0:
        return b if math.copysign(1.0, a) < 0 else a
    return a if a > b else b


def _js_min(a: float, b: float) -> float:
    """Math.min(a, b): NaN if either is NaN (Python's min would drop a NaN second argument)."""
    if a != a or b != b:
        return math.nan
    return b if b < a else a


def resolve_scenario(s: str | Scenario | None) -> Scenario:
    """A scenario id (or object) -> Scenario. None/'' means 'baseline'."""
    if not s:
        return SCENARIOS["baseline"]
    if isinstance(s, str):
        sc = SCENARIOS.get(s)
        if sc is None:
            raise KeyError(f'unknown scenario "{s}". Known: {", ".join(SCENARIOS)}')
        return sc
    return s


def simulate(*, params: Mapping[str, Any] | None = None, scenario: str | Scenario | None = None,
             t_end: float | None = None, dt: float | None = None, out_every: int | None = None,
             y0: Sequence[float] | None = None) -> dict[str, Any]:
    """Run one deterministic simulation.

    params     parameter values (default: default_params()); copied, never mutated
    scenario   scenario id or Scenario (default 'baseline')
    t_end      h (default scenario.t_end)
    dt         output grid h (default scenario.dt, else 1/60)
    out_every  record every k-th grid point (default scenario.out_every)
    y0         initial state (default initial_state(params))

    Returns {t, Y, states, derived, ledger, params, scenario, meta}: Y[i][k] is state
    STATE_KEYS[i] at time t[k]; states[key] is the same list by name.
    Raises InfeasibleParametersError when the params have no reference steady state, and
    NonFiniteTrajectoryError when any state, ledger or derived value is NaN or infinite
    (HREQ-V-07; a deliberate deviation: index.js returns the NaN trajectory).
    """
    sc = resolve_scenario(scenario)
    p = dict(params if params is not None else default_params())
    tEnd = t_end if t_end is not None else sc.t_end
    dt_ = dt if dt is not None else (sc.dt if sc.dt is not None else 1 / 60)
    outEvery = out_every if out_every is not None else (
        sc.out_every if sc.out_every is not None else 1)
    y0_ = [float(v) for v in y0] if y0 is not None else initial_state(p)
    C = constants(p)
    n, nl = len(STATE_KEYS), len(LEDGER_KEYS)
    z0 = list(y0_) + [0.0] * nl
    led = [0.0] * nl

    def deriv(t: float, z: list[float], inputs: Any, dz: list[float]) -> None:
        rhs(t, z, p, inputs, dz, led, C)          # writes dz[0..n-1]
        dz[n:n + nl] = led

    maxStep = _js_min(dt_, MAX_STEP_FRACTION_OF_TAU_MIN * C["tau_min"])
    sc_inputs = sc.inputs
    res = integrate(deriv=deriv, z0=z0, t0=0.0, t_end=tEnd, dt=dt_, out_every=outEvery,
                    breakpoints=sc.breakpoints, inputs_at=lambda t: sc_inputs(t, p),
                    max_step=maxStep)
    T = len(res["t"])
    Zs = res["Z"]
    Y = [[z[i] for z in Zs] for i in range(n)]
    ledger = {k: [z[n + i] for z in Zs] for i, k in enumerate(LEDGER_KEYS)}
    der: dict[str, list[float]] = {k: [0.0] * T for k in DERIVED_KEYS}
    for k in range(T):
        d = derived(Zs[k], p, C)
        for key in DERIVED_KEYS:
            der[key][k] = d[key]
    states = {k: Y[i] for i, k in enumerate(STATE_KEYS)}
    _refuse_non_finite(res["t"], states, ledger, der, sc.id)
    return {
        "t": res["t"], "Y": Y, "states": states, "derived": der, "ledger": ledger, "params": p,
        "scenario": sc.id,
        "meta": {"steps": res["steps"], "maxStep": maxStep, "dt": dt_, "outEvery": outEvery,
                 "label": sc.label or None, "disclaimer": DISCLAIMER,
                 "validation_status": VALIDATION_STATUS, "modelVersion": MODEL_VERSION},
    }


def _refuse_non_finite(t: Sequence[float], states: Mapping[str, list[float]],
                       ledger: Mapping[str, list[float]], der: Mapping[str, list[float]],
                       scenario_id: str) -> None:
    """HREQ-V-07: a trajectory holding a NaN or infinite value fails its run.

    Fast path: the sum of every series is finite exactly when no value is NaN or infinite
    (barring an overflow of the sum itself, which the exact scan below then clears).
    Raises NonFiniteTrajectoryError naming the FIRST output time, and the key, that went
    non-finite, and how many values are non-finite in all.
    """
    sections = (("states", states), ("ledger", ledger), ("derived", der))
    if all(math.isfinite(sum(v)) for _, sec in sections for v in sec.values()):
        return
    first: tuple[int, str, str, float] | None = None
    count = 0
    for name, sec in sections:
        for key, vals in sec.items():
            for k, v in enumerate(vals):
                if not math.isfinite(v):
                    count += 1
                    if first is None or k < first[0]:
                        first = (k, name, key, v)
    if first is None:           # the sum overflowed; every value is finite
        return
    k, name, key, v = first
    raise NonFiniteTrajectoryError(
        f"simulate: {name}.{key} is {v!r} at t = {t[k]!r} h (output index {k}); "
        f"{count} non-finite values in the trajectory. The run fails (HREQ-V-07).",
        expected="every state, ledger and derived value finite",
        received={"t": t[k], "index": k, "section": name, "key": key, "value": v,
                  "non_finite_values": count},
        scenario=scenario_id, model_version=MODEL_VERSION,
        t=t[k], key=key, section=name, non_finite_values=count)


def draw_samples(*, n: int = 64, seed: int = 1,
                 table: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Draw `n` accepted parameter samples (see mc.py for the rejection rule).

    Returns {"samples": [...], "rejected": int}. Raises InfeasibleParametersError when
    fewer than n feasible samples are found in MAX_TRIES_PER_SAMPLE·n draws.
    """
    tab = table if table is not None else param_table()
    rng = mulberry32(seed)
    samples: list[dict[str, Any]] = []
    rejected = 0
    max_tries = MAX_TRIES_PER_SAMPLE * n
    tries = 0
    while len(samples) < n:
        if tries >= max_tries:
            raise InfeasibleParametersError(
                f"drawSamples: only {len(samples)}/{n} feasible samples after {tries} draws",
                expected=n, received=len(samples), model_version=MODEL_VERSION,
                rejected=rejected, seed=seed)
        tries += 1
        p = sample_params(tab, rng)
        C = constants(p)
        if not C["feasible"] or not (p["na_normal_low"] <= C["Na_ss"] <= p["na_normal_high"]):
            rejected += 1
            continue
        samples.append(p)
    return {"samples": samples, "rejected": rejected,
            "meta": result_meta(n=n, seed=seed, draws=tries)}


def simulate_mc(*, scenario: str | Scenario | None = None, t_end: float | None = None,
                n: int = 64, seed: int = 1, dt: float | None = None,
                out_every: int | None = None, keys: Sequence[str] | None = None) -> dict[str, Any]:
    """Monte Carlo over parameter ranges (spec §3 simulateMC).

    Returns {t, keys, q05, q50, q95, dq05, dq50, dq95, samples, rejected, n, seed, scenario,
    disclaimer, validation_status, meta}: q**[key] are quantiles of the absolute value at each time point, dq**[key]
    of the change from each sample's own t=0 value; samples are the accepted parameter sets
    (same order as runs).
    """
    drawn = draw_samples(n=n, seed=seed)
    samples, rejected = drawn["samples"], drawn["rejected"]
    K = list(keys) if keys is not None else [*STATE_KEYS, *DERIVED_KEYS]
    series: dict[str, list[list[float]]] = {k: [] for k in K}
    dseries: dict[str, list[list[float]]] = {k: [] for k in K}
    t: list[float] | None = None
    for p in samples:
        r = simulate(params=p, scenario=scenario, t_end=t_end, dt=dt, out_every=out_every)
        t = r["t"]
        for k in K:
            arr = r["states"].get(k)
            if arr is None:
                arr = r["derived"].get(k)
            if arr is None:
                raise KeyError(f"simulateMC: unknown key {k}")
            series[k].append(arr)
            a0 = arr[0]
            dseries[k].append([v - a0 for v in arr])
    q05: dict[str, list[float]] = {}
    q50: dict[str, list[float]] = {}
    q95: dict[str, list[float]] = {}
    dq05: dict[str, list[float]] = {}
    dq50: dict[str, list[float]] = {}
    dq95: dict[str, list[float]] = {}
    for k in K:
        q05[k], q50[k], q95[k] = quantile_bands(series[k], [0.05, 0.5, 0.95])
        dq05[k], dq50[k], dq95[k] = quantile_bands(dseries[k], [0.05, 0.5, 0.95])
    sc_id = resolve_scenario(scenario).id
    return {"t": t, "keys": K, "q05": q05, "q50": q50, "q95": q95, "dq05": dq05, "dq50": dq50,
            "dq95": dq95, "samples": samples, "rejected": rejected, "n": n, "seed": seed,
            "scenario": sc_id, "disclaimer": DISCLAIMER, "validation_status": VALIDATION_STATUS,
            "meta": result_meta(n=n, seed=seed, scenario=sc_id, dt=dt, rejected=rejected)}


def nearest_index(t: Sequence[float], x: float) -> int:
    """Index of the grid time closest to x (the first one on a tie)."""
    best = 0
    for k in range(1, len(t)):
        if abs(t[k] - x) < abs(t[best] - x):
            best = k
    return best


def salt_load_metrics(r: Mapping[str, Any]) -> dict[str, float]:
    """Summary metrics of one salt-load run (used by the dose sweep)."""
    d, s, t = r["derived"], r["states"], r["t"]
    na0, v0, map0 = d["Na_plasma"][0], s["V_ecf"][0], s["MAP"][0]
    peakS, peakdNa, peakdV, peakMAP = 0.0, -math.inf, -math.inf, -math.inf
    for k in range(len(t)):
        peakS = _js_max(peakS, d["strain_index"][k])
        peakdNa = _js_max(peakdNa, d["Na_plasma"][k] - na0)
        peakdV = _js_max(peakdV, s["V_ecf"][k] - v0)
        peakMAP = _js_max(peakMAP, s["MAP"][k] - map0)
    k24 = nearest_index(t, 25)  # 24 h after the load at t = 1 h
    base = r["params"]["naIn_base_mmold"] / 24
    na_out = r["ledger"]["na_out"]
    na_excr_24h = na_out[k24] - na_out[nearest_index(t, 1)] - 24 * base
    return {"peak_strain_index": peakS, "peak_dNa": peakdNa, "peak_dV_ecf": peakdV,
            "na_excr_24h": na_excr_24h, "peak_MAP": peakMAP,
            "meta": result_meta(scenario=r.get("scenario"))}


def simulate_sweep(*, n: int = 64, seed: int = 1, values: Sequence[float] | None = None,
                   dt: float = 1 / 30) -> dict[str, Any]:
    """Salt dose sweep with Monte Carlo bands (common random numbers: the same parameter
    samples are used at every dose, so the curve shape is not noise).

    Returns {values, unit, metrics: {metric: {q05: [...], q50: [...], q95: [...]}}, rejected,
    n, seed, disclaimer, validation_status, meta}.
    """
    sw = SCENARIOS["salt_load_sweep"].sweep
    assert sw is not None  # noqa: S101 - structural invariant of SCENARIOS, not input validation
    vals = list(values) if values is not None else list(sw.values)
    drawn = draw_samples(n=n, seed=seed)
    samples, rejected = drawn["samples"], drawn["rejected"]
    metrics: dict[str, dict[str, list[float]]] = {
        m: {"q05": [], "q50": [], "q95": []} for m in sw.metrics}
    for g in vals:
        sc = sw.make(g)
        per: dict[str, list[float]] = {m: [] for m in sw.metrics}
        for p in samples:
            m = salt_load_metrics(simulate(params=p, scenario=sc, dt=dt))
            for key in sw.metrics:
                per[key].append(m[key])
        for key in sw.metrics:
            a, b, c = quantile_bands([[x] for x in per[key]], [0.05, 0.5, 0.95])
            metrics[key]["q05"].append(a[0])
            metrics[key]["q50"].append(b[0])
            metrics[key]["q95"].append(c[0])
    return {"values": vals, "unit": sw.unit, "metrics": metrics, "rejected": rejected, "n": n,
            "seed": seed, "disclaimer": DISCLAIMER, "validation_status": VALIDATION_STATUS,
            "meta": result_meta(n=n, seed=seed, dt=dt, rejected=rejected)}


# ---------------------------------------------------------------------------
# Which parameters influence each quantity (audit F-01 / BUG-0049).
# One-at-a-time sensitivity screen instead of hand-written lists:
#   * reference run: default parameters, scenario salt_load_10g, 24 h, dt = 1/20 h;
#   * every parameter in the table (all of them, including the ones held fixed in Monte
#     Carlo, because e.g. the strain-index weights are E-assumptions that define that curve)
#     is raised by +10 % and the run repeated from its own steady state;
#   * effect(param, key) = max_t |x_perturbed(t) − x_default(t)| / scale(key), where
#     scale = peak-to-peak range of the default trajectory of that key over the run
#     (floored at 1e-6·max|x| so a flat quantity cannot divide by zero);
#   * a parameter "feeds" a quantity when its effect exceeds INFLUENCE_DEFAULTS threshold (1 %).
# A perturbation that makes the steady state infeasible counts as influencing every key
# (effect = Infinity), so the screen can only over-count, never under-count; so does a NaN
# anywhere in a perturbed run's difference (BUG-20261003-116: before 1.1.0 only a NaN in
# the trailing run of a series survived the running maximum). Here simulate() already
# refuses a NaN trajectory (NonFiniteTrajectoryError -> infeasible), so the NaN rule matters
# only for a run that reaches the comparison; it is kept identical to index.js.
# Cost: one reference run plus one run per parameter row (meta.nParams).
# ---------------------------------------------------------------------------
INFLUENCE_DEFAULTS: Mapping[str, Any] = MappingProxyType({
    "scenario": "salt_load_10g", "tEnd": 24, "dt": 1 / 20, "rel": 0.10, "threshold": 0.01})
_influence_cache: dict[str, Any] | None = None


def compute_influence(*, scenario: str | Scenario | None = None, t_end: float | None = None,
                      dt: float | None = None, rel: float | None = None,
                      threshold: float | None = None) -> dict[str, Any]:
    """Run the sensitivity screen.

    Returns {meta, effects: {key: {param: effect}}, params: {key: [param ids, strongest first]}};
    `meta` uses the JavaScript keys (scenario, tEnd, dt, rel, threshold, infeasible, nParams,
    modelVersion). Arguments left as None take INFLUENCE_DEFAULTS.
    """
    o = dict(INFLUENCE_DEFAULTS)
    for k, v in (("scenario", scenario), ("tEnd", t_end), ("dt", dt), ("rel", rel),
                 ("threshold", threshold)):
        if v is not None:
            o[k] = v
    p0 = default_params()

    def keyed(r: Mapping[str, Any]) -> dict[str, list[float]]:
        return {**r["states"], **r["derived"], **r["ledger"]}

    ref = keyed(simulate(params=p0, scenario=o["scenario"], t_end=o["tEnd"], dt=o["dt"]))
    keys = list(ref)
    scale: dict[str, float] = {}
    for k in keys:
        lo, hi, big = math.inf, -math.inf, 0.0
        for v in ref[k]:
            if v < lo:
                lo = v
            if v > hi:
                hi = v
            big = _js_max(big, abs(v))
        scale[k] = _js_max(_js_max(hi - lo, 1e-6 * big), 1e-12)
    effects: dict[str, dict[str, float]] = {k: {} for k in keys}
    infeasible: list[str] = []
    for name in p0:
        p = {**p0, name: p0[name] * (1 + o["rel"])}
        run: dict[str, list[float]] | None = None
        try:
            run = keyed(simulate(params=p, scenario=o["scenario"], t_end=o["tEnd"], dt=o["dt"]))
        except (HealthError, ArithmeticError, ValueError):
            infeasible.append(name)
        for k in keys:
            if run is None:
                effects[k][name] = math.inf
                continue
            a, b = ref[k], run[k]
            m = 0.0
            nan = False
            for i in range(len(a)):
                dd = abs(b[i] - a[i])
                if dd != dd:
                    nan = True
                elif dd > m:
                    m = dd
            # any NaN -> counted (as in index.js)
            effects[k][name] = m / scale[k] if not nan and math.isfinite(m) else math.inf
    params: dict[str, list[str]] = {}
    for k in keys:
        hits = [(nm, e) for nm, e in effects[k].items() if e > o["threshold"]]
        hits.sort(key=lambda ne: -ne[1])           # stable, strongest first
        params[k] = [nm for nm, _ in hits]
    meta = {**o, "infeasible": infeasible, "nParams": len(p0), "modelVersion": MODEL_VERSION,
            "disclaimer": DISCLAIMER, "validation_status": VALIDATION_STATUS}
    if not isinstance(meta["scenario"], str):
        meta["scenario"] = meta["scenario"].id
    return {"meta": meta, "effects": effects, "params": params}


def prime_influence(result: Mapping[str, Any]) -> None:
    """Install a precomputed screen so params_for() does not recompute."""
    global _influence_cache
    if not result or not isinstance(result.get("params"), Mapping):
        raise ValueError("primeInfluence: bad result")
    _influence_cache = dict(result)


def influence() -> dict[str, Any]:
    """Cached sensitivity screen at INFLUENCE_DEFAULTS (1 + meta.nParams simulations on first
    call)."""
    global _influence_cache
    if _influence_cache is None:
        _influence_cache = compute_influence()
    return _influence_cache


def params_for(derived_key: str) -> list[str]:
    """Parameter ids that move `derived_key` (a state, derived or ledger key) by more than
    1 % of its response scale in the reference screen, strongest first. Raises KeyError on
    an unknown key."""
    lst = influence()["params"].get(derived_key)
    if lst is None:
        raise KeyError(f'paramsFor: unknown quantity "{derived_key}"')
    return list(lst)


_GRADE_AT_LEAST_B = re.compile(r"^(A-|B-)")


def param_summary() -> dict[str, Any]:
    """Parameter-table summary for UI footers: count and share graded >= B."""
    entries = list(param_table().values())
    good = sum(1 for e in entries if _GRADE_AT_LEAST_B.search(e["grade"]))
    by_grade: dict[str, int] = {}
    for e in entries:
        by_grade[e["grade"]] = by_grade.get(e["grade"], 0) + 1
    return {"count": len(entries), "atLeastB": good, "pctAtLeastB": (100 * good) / len(entries),
            "byGrade": by_grade, "meta": result_meta()}
