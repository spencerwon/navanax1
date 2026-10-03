"""Literature-expectation harness: score a simulation against its scenario's `expects`.

EDUCATIONAL MODEL — NOT MEDICAL ADVICE. A "pass" means the model's number falls inside a
band registered from the literature; it is not clinical validation.

Contract (docs/health/03_VALIDATION_AND_TESTING.md §5, docs/health/01_METHODOLOGY.md §6-7):
`evaluate_expectations(scenario, result)` returns EXACTLY ONE row per registered expectation,
in registration order, asserts that count, and never omits one (HREQ-P-05, HREQ-V-12):

    id, metric, kind, role, calibrates, registered, countable, counted, target, range,
    actual, status, reason, evidence, model_version, disclaimer,
    meta {disclaimer, validation_status, modelVersion}
    + band, in_range_share, n, rejected, seed     (when Monte Carlo runs are supplied)

status
    "pass"         lo <= actual <= hi;
    "fail"         outside the range, or not a finite number (a model that cannot answer
                   has failed, not abstained);
    "not_checked"  a non-countable kind (qualitative, design-target, known-divergence,
                   unverified, numerical), a countable expectation with no extractor yet
                   ("NO METRIC IMPLEMENTATION", listed by summarize()), or a run too short to
                   compute the metric. Always with a reason; never counted as a pass.
role (Python-side annotation in scenarios.py; default "validation")
    "calibration"  the parameters in `calibrates` were tuned to this target: evaluated and
                   reported, NEVER counted as validation (calibration is never validation);
    "structural"   a structural property of the model: evaluated, reported, never counted.
counted = kind in COUNTABLE_KINDS and role == "validation" and status in {pass, fail}
(HREQ-V-14). summarize() reports counted passes/fails apart from calibration and structural.

`numerical` rows (baseline drift) are a solver property: the value is computed and shown;
the numerical gate in tests/health_selftest.py enforces it.

Every ranged metric string is mapped to an extractor in METRICS. Event times (drink start,
load size, override window) are read from the scenario record, never re-typed here.
Extractor definitions (default-parameter run unless the caller passes another result):
  * "baseline" values are the values at t = 0 (the analytic steady state);
  * "after drinking starts" / "by N h" are measured from the START of the first event;
  * "time to recover |ΔNa| < 0.5 mmol/L after drinking starts" (drink_water_1L). Neither
    scenarios.js nor anything vendored with V1 defines it (V1's tests/engine.test.mjs was not
    vendored), so it is defined here, explicitly:
        ΔNa(t) = Na_plasma(t) − Na_plasma(0); k_nadir = first index of min Na_plasma;
        recovery = t[k] − t_start for the FIRST output index k > k_nadir with |ΔNa| < 0.5,
        t_start = start of the first event (1 h); 0 if |ΔNa| never reaches 0.5 mmol/L;
        +inf if |ΔNa| is still >= 0.5 at the end of the run.
    The alternative reading -- the time after which |ΔNa| STAYS below 0.5 -- is computed by
    recovery_time_variants() and gives the same value whenever the recovery is monotone, as
    it is in V1 at default parameters (7.05 h; |ΔNa| = 0.575 mmol/L at +6 h, so every
    reading fails the registered < 6 h).
  * urine volume over a window is read from the cumulative ledger: Δwater_out minus the
    constant insensible + fecal losses (no V1 scenario sweats; asserted);
  * excess Na excretion over a window is Δna_out minus the window length times the
    baseline Na intake rate naIn_base_mmold / 24 (as saltLoadMetrics does in index.js).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..errors import ConfigurationError, ExpectationSkippedError
from .api import (
    DISCLAIMER,
    MODEL_VERSION,
    draw_samples,
    nearest_index,
    result_meta,
    simulate,
)
from .mc import quantile_sorted
from .model import STATE_KEYS, constants
from .scenarios import MMOL_NA_PER_G_NACL, SCENARIOS, Scenario

__all__ = ["COUNTABLE_KINDS", "METRICS", "NOT_CHECKED_KINDS", "ROLES", "MetricUnavailable",
           "evaluate_expectations", "max_relative_state_drift", "monte_carlo_runs",
           "recovery_time_variants", "summarize"]

#: The only kinds that count toward pass/fail totals (HREQ-E-14).
COUNTABLE_KINDS: tuple[str, ...] = ("quantitative", "semi-quantitative")

#: Roles an expectation may play (docs/health/01 §6). Only "validation" is ever counted.
ROLES: tuple[str, ...] = ("validation", "calibration", "structural")

#: Kinds that are never scored, with the reason recorded on their row.
NOT_CHECKED_KINDS: Mapping[str, str] = {
    "qualitative": "qualitative expectation: a direction, not a numeric range; not counted "
                   "(docs/health/01 §7.2)",
    "unverified": "unverified source value: never counted as a pass until confirmed",
    "known-divergence": "registered known divergence between the model and the literature",
    "design-target": "design target the parameters were calibrated to; calibration is "
                     "never validation",
    "numerical": "numerical solver property, not a literature claim: enforced by the "
                 "numerical gate (tests/health_selftest.py); value shown for the record",
}

NO_EXTRACTOR = "NO METRIC IMPLEMENTATION"

# Times are compared to the output grid with this slack (h) when deciding a run is long enough.
_T_SLACK = 1e-9


class MetricUnavailable(Exception):
    """The metric cannot be computed from this result (e.g. the run ends too early)."""


# ---------------------------------------------------------------------------
# Helpers over a simulate() result.
# ---------------------------------------------------------------------------
def _index_at(r: Mapping[str, Any], t_target: float) -> int:
    """Grid index of t_target; refuses (MetricUnavailable) when the run does not reach it."""
    t = r["t"]
    if t_target > t[-1] + _T_SLACK or t_target < t[0] - _T_SLACK:
        raise MetricUnavailable(f"run covers t = {t[0]:g}..{t[-1]:g} h; metric needs "
                                f"t = {t_target:g} h")
    return nearest_index(t, t_target)


def _first_event(sc: Scenario) -> Mapping[str, Any]:
    if not sc.events:
        raise MetricUnavailable(f"scenario {sc.id} has no event to measure from")
    return sc.events[0]


def _first_override(sc: Scenario) -> Mapping[str, Any]:
    if not sc.overrides:
        raise MetricUnavailable(f"scenario {sc.id} has no override window")
    return sc.overrides[0]


def _require_no_sweat(sc: Scenario, r: Mapping[str, Any]) -> None:
    """Urine is read from the ledger as water_out minus fixed losses; that needs sweat = 0."""
    p = r["params"]
    probes = [0.0, *sc.breakpoints, r["t"][-1]]
    if any(sc.inputs(t, p).get("sweat_Lh", 0) for t in probes):
        raise MetricUnavailable("scenario sweats; urine volume cannot be read from water_out")


def _urine_volume(sc: Scenario, r: Mapping[str, Any], ta: float, tb: float) -> float:
    """Urine (L) excreted in [ta, tb], from the cumulative ledger."""
    _require_no_sweat(sc, r)
    C = constants(r["params"])
    wo = r["ledger"]["water_out"]
    ia, ib = _index_at(r, ta), _index_at(r, tb)
    span = r["t"][ib] - r["t"][ia]
    return (wo[ib] - wo[ia]) - span * (C["insens_h"] + C["fecal_h"])


def _excess_na_excreted(r: Mapping[str, Any], ta: float, tb: float) -> float:
    """Na (mmol) excreted in [ta, tb] above the baseline excretion rate (= baseline intake)."""
    na_out = r["ledger"]["na_out"]
    ia, ib = _index_at(r, ta), _index_at(r, tb)
    base = r["params"]["naIn_base_mmold"] / 24
    return (na_out[ib] - na_out[ia]) - (r["t"][ib] - r["t"][ia]) * base


def max_relative_state_drift(r: Mapping[str, Any]) -> tuple[float, str, int]:
    """max over states i and times k of |y_i(t_k) − y_i(0)| / scale_i, with
    scale_i = |y_i(0)| (or 1 for a state whose baseline is exactly 0).
    Returns (drift, state key, time index) of the worst case."""
    worst, worst_key, worst_k = 0.0, STATE_KEYS[0], 0
    for key in STATE_KEYS:
        series = r["states"][key]
        y0 = series[0]
        scale = abs(y0) if y0 != 0 else 1.0
        for k, v in enumerate(series):
            d = abs(v - y0) / scale
            if not (d <= worst):           # NaN counts as the worst
                worst, worst_key, worst_k = d, key, k
    return worst, worst_key, worst_k


# ---------------------------------------------------------------------------
# Metric implementations, keyed by (scenario id, metric string exactly as registered).
# ---------------------------------------------------------------------------
def _baseline_drift(sc: Scenario, r: Mapping[str, Any]) -> float:
    return max_relative_state_drift(r)[0]


def _na_nadir(sc: Scenario, r: Mapping[str, Any]) -> float:
    na = r["derived"]["Na_plasma"]
    return min(na) - na[0]


def _na_peak(sc: Scenario, r: Mapping[str, Any]) -> float:
    na = r["derived"]["Na_plasma"]
    return max(na) - na[0]


def _na_recovery_time(sc: Scenario, r: Mapping[str, Any]) -> float:
    """First output time after the Na nadir at which |ΔNa| < 0.5 mmol/L, measured from the
    drinking start (0 if |ΔNa| never reaches 0.5; +inf if it is still >= 0.5 at the end)."""
    start = _first_event(sc)["start"]
    t, na = r["t"], r["derived"]["Na_plasma"]
    k_nadir = min(range(len(na)), key=na.__getitem__)          # first minimum
    if abs(na[k_nadir] - na[0]) < 0.5:
        return 0.0
    for k in range(k_nadir + 1, len(t)):
        if abs(na[k] - na[0]) < 0.5:
            return t[k] - start
    return math.inf


def recovery_time_variants(r: Mapping[str, Any], start: float,
                           threshold: float = 0.5) -> dict[str, float]:
    """The plausible readings of "time to recover |ΔNa| < threshold after drinking starts",
    for reporting next to the harness value (which is `first_after_nadir_from_start`):
        first_after_nadir_from_start  first time after the nadir |ΔNa| < threshold, − start
        stays_below_from_start        time after which |ΔNa| < threshold for good, − start
        first_after_nadir_from_nadir  as the first, measured from the nadir time
        dNa_at_start_plus_6h          ΔNa at start + 6 h (the registered bound)
    Times are +inf when the run ends before recovery."""
    t, na = r["t"], r["derived"]["Na_plasma"]
    dev = [abs(v - na[0]) for v in na]
    k_nadir = min(range(len(na)), key=na.__getitem__)
    first = next((k for k in range(k_nadir + 1, len(t)) if dev[k] < threshold), None)
    above = [k for k in range(len(t)) if dev[k] >= threshold]
    stays = (0 if not above else (above[-1] + 1 if above[-1] + 1 < len(t) else None))
    k6 = nearest_index(t, start + 6) if t[-1] >= start + 6 else None
    return {
        "first_after_nadir_from_start": (0.0 if dev[k_nadir] < threshold else
                                         math.inf if first is None else t[first] - start),
        "stays_below_from_start": (math.inf if stays is None else
                                   0.0 if stays == 0 else t[stays] - start),
        "first_after_nadir_from_nadir": (0.0 if dev[k_nadir] < threshold else
                                         math.inf if first is None else t[first] - t[k_nadir]),
        "dNa_at_start_plus_6h": math.nan if k6 is None else na[k6] - na[0],
    }


def _urine_peak_time(sc: Scenario, r: Mapping[str, Any]) -> float:
    start = _first_event(sc)["start"]
    uf = r["derived"]["urine_flow"]
    k = max(range(len(uf)), key=uf.__getitem__)       # first maximum
    return r["t"][k] - start


def _urine_peak(sc: Scenario, r: Mapping[str, Any]) -> float:
    return max(r["derived"]["urine_flow"])


def _urine_osm_min(sc: Scenario, r: Mapping[str, Any]) -> float:
    return min(r["derived"]["U_osm"])


def _water_load_excreted_3h(sc: Scenario, r: Mapping[str, Any]) -> float:
    """(urine in [start, start+3 h] − 3 h × baseline urine flow) / volume drunk."""
    ev = _first_event(sc)
    start, liters = ev["start"], ev["water_L"]
    ia, ib = _index_at(r, start), _index_at(r, start + 3)
    span = r["t"][ib] - r["t"][ia]
    excess = _urine_volume(sc, r, start, start + 3) - span * r["derived"]["urine_flow"][0]
    return excess / liters


def _salt_load_fraction(hours: float) -> Callable[[Scenario, Mapping[str, Any]], float]:
    def f(sc: Scenario, r: Mapping[str, Any]) -> float:
        ev = _first_event(sc)
        load = ev["salt_g"] * MMOL_NA_PER_G_NACL
        return _excess_na_excreted(r, ev["start"], ev["start"] + hours) / load
    f.__doc__ = (f"Na excreted above baseline in [load start, +{hours:g} h] / Na load "
                 "(salt_g × MMOL_NA_PER_G_NACL).")
    return f


def _chronic_dmap_per_100(sc: Scenario, r: Mapping[str, Any]) -> float:
    """(MAP at override start + 30 days − MAP at override start) per +100 mmol/day Na intake."""
    ov = _first_override(sc)
    start = ov["start"]
    i0, i30 = _index_at(r, start), _index_at(r, start + 30 * 24)
    mp = r["states"]["MAP"]
    d_na_day = ov["naIn_mmolh"] * 24 - r["params"]["naIn_base_mmold"]
    return (mp[i30] - mp[i0]) * 100 / d_na_day


def _chronic_balance_day30(sc: Scenario, r: Mapping[str, Any]) -> float:
    """Na excreted / Na ingested over the 24 h ending at day 30 of the override (ledger)."""
    ov = _first_override(sc)
    t30 = ov["start"] + 30 * 24
    ia, ib = _index_at(r, t30 - 24), _index_at(r, t30)
    led = r["ledger"]
    return (led["na_out"][ib] - led["na_out"][ia]) / (led["na_in"][ib] - led["na_in"][ia])


def _no_water_osm_rise(sc: Scenario, r: Mapping[str, Any]) -> float:
    """Plasma osmolality at the end of the deprivation window minus baseline."""
    ov = _first_override(sc)
    osm = r["derived"]["osm_plasma"]
    return osm[_index_at(r, ov["end"])] - osm[0]


MetricFn = Callable[[Scenario, Mapping[str, Any]], float]

#: (scenario id, metric string as registered in scenarios.py) -> extractor. A plain dict so
#: the self-test can plant extractors (e.g. one returning NaN) and remove them again.
METRICS: dict[tuple[str, str], MetricFn] = {
    ("baseline", "max |Δstate| / scale over 24 h"): _baseline_drift,
    ("drink_water_1L", "Na_plasma nadir − baseline"): _na_nadir,
    ("drink_water_1L", "time to recover |ΔNa| < 0.5 mmol/L after drinking starts"):
        _na_recovery_time,
    ("drink_water_1L", "time of peak urine flow after drinking starts"): _urine_peak_time,
    ("drink_water_1L", "peak urine flow"): _urine_peak,
    ("drink_water_1L", "minimum urine osmolality"): _urine_osm_min,
    ("drink_water_1L",
     "fraction of the 1 L excreted (above baseline urine) by 3 h after drinking"):
        _water_load_excreted_3h,
    ("salt_load_10g", "Na_plasma peak − baseline"): _na_peak,
    ("salt_load_10g", "fraction of the 171 mmol load excreted (above baseline) by 8 h"):
        _salt_load_fraction(8),
    ("salt_load_10g", "fraction excreted by 48 h"): _salt_load_fraction(48),
    ("chronic_high_salt_30d", "ΔMAP at day 30 per +100 mmol/day Na"): _chronic_dmap_per_100,
    ("chronic_high_salt_30d", "Na excretion ≈ intake by day 30"): _chronic_balance_day30,
    ("no_water_24h", "osmolality rise"): _no_water_osm_rise,
}

def _numeric_range(e: Mapping[str, Any]) -> tuple[float, float] | None:
    rng = e.get("range")
    if (isinstance(rng, Sequence) and not isinstance(rng, str) and len(rng) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in rng)):
        return float(rng[0]), float(rng[1])
    return None


def _quantile(sorted_: Sequence[float], q: float) -> float:
    """R type 7, interpolating only between DIFFERENT order statistics, as R does: a metric
    that is +inf for some samples ("never recovered within the run") gives an infinite band
    edge, not the NaN of inf - inf. Equal to mc.quantile_sorted for finite values."""
    n = len(sorted_)
    if n == 0:
        return math.nan
    h = (n - 1) * q
    lo, hi = math.floor(h), math.ceil(h)
    if sorted_[lo] == sorted_[hi]:
        return sorted_[lo]
    return quantile_sorted(sorted_, q)


def _band(values: Sequence[float], bounds: tuple[float, float] | None) -> dict[str, Any]:
    """Per-sample q05/q50/q95 of a metric (NaN sorted last) and the share inside the range."""
    finite = sorted(v for v in values if v == v)
    col = finite + [v for v in values if v != v]
    band = {q: _quantile(col, p) for q, p in (("q05", 0.05), ("q50", 0.5), ("q95", 0.95))}
    share = None
    if bounds is not None and values:
        lo, hi = bounds
        share = sum(1 for v in values if math.isfinite(v) and lo <= v <= hi) / len(values)
    return {"band": band, "in_range_share": share}


def monte_carlo_runs(scenario: str | Scenario, *, n: int, seed: int = 1,
                     **simulate_kwargs: Any) -> dict[str, Any]:
    """Accepted parameter samples (draw_samples) and one simulation per sample, for the
    `mc` argument of evaluate_expectations. Returns {results, n, seed, rejected}."""
    drawn = draw_samples(n=n, seed=seed)
    results = [simulate(params=p, scenario=scenario, **simulate_kwargs) for p in drawn["samples"]]
    return {"results": results, "n": n, "seed": seed, "rejected": drawn["rejected"],
            "meta": result_meta(n=n, seed=seed, rejected=drawn["rejected"])}


def _resolve(scenario: str | Scenario) -> Scenario:
    if isinstance(scenario, Scenario):
        return scenario
    sc = SCENARIOS.get(scenario)
    if sc is None:
        raise KeyError(f'unknown scenario "{scenario}". Known: {", ".join(SCENARIOS)}')
    return sc


def _evaluate_one(sc: Scenario, i: int, e: Mapping[str, Any], r: Mapping[str, Any],
                  mc: Mapping[str, Any] | None) -> dict[str, Any]:
    metric, kind = e.get("metric"), e.get("kind")
    role = e.get("role", "validation")
    if role not in ROLES:
        raise ConfigurationError(f"expectation {metric!r} has unknown role {role!r}",
                                 expected=ROLES, received=role, scenario=sc.id)
    row: dict[str, Any] = {
        "id": e.get("id") or f"{sc.id}/{i + 1:02d}",
        "metric": metric, "kind": kind, "role": role,
        "calibrates": list(e.get("calibrates") or []),
        # docs/health/01 §7.1: V1's expectations were written alongside the model.
        "registered": e.get("registered", "co-developed"),
        "countable": kind in COUNTABLE_KINDS, "counted": False,
        "target": e.get("target"), "range": e.get("range"),
        "actual": None, "status": "not_checked", "reason": "",
        "evidence": list(e.get("evidence") or []),
        "model_version": MODEL_VERSION, "disclaimer": DISCLAIMER,
        "meta": result_meta(scenario=sc.id),
    }
    fn = METRICS.get((sc.id, metric))
    bounds = _numeric_range(e)
    if kind in NOT_CHECKED_KINDS:
        row["reason"] = NOT_CHECKED_KINDS[kind]
        if kind == "numerical" and fn is not None:
            try:
                row["actual"] = float(fn(sc, r))
            except MetricUnavailable as exc:
                row["reason"] += f"; value unavailable: {exc}"
    elif kind not in COUNTABLE_KINDS:
        row["reason"] = f"unknown expectation kind {kind!r}: not scored (registry defect)"
    elif bounds is None:
        row["reason"] = f"countable kind {kind!r} without a numeric range (registry defect)"
    elif fn is None:
        row["reason"] = (f"{NO_EXTRACTOR} for this ranged expectation "
                         "(harness gap: add it to validate.METRICS)")
    else:
        try:
            actual = float(fn(sc, r))
        except MetricUnavailable as exc:
            row["reason"] = f"unavailable: {exc}"
        else:
            lo, hi = bounds
            ok = math.isfinite(actual) and lo <= actual <= hi
            row["actual"] = actual
            row["status"] = "pass" if ok else "fail"
            row["reason"] = (f"{actual:.6g} in [{lo:g}, {hi:g}]" if ok
                             else f"{actual:.6g} outside [{lo:g}, {hi:g}]")
    if role == "calibration":
        row["reason"] += (f"; calibration target (calibrates {', '.join(row['calibrates']) or '?'}):"
                          " reported separately, never counted as validation")
    elif role == "structural":
        row["reason"] += "; structural property: reported separately, never counted as validation"
    row["counted"] = (row["countable"] and role == "validation"
                      and row["status"] in ("pass", "fail"))
    if mc is not None:
        row.update({"band": None, "in_range_share": None, "n": mc["n"], "seed": mc["seed"],
                    "rejected": mc["rejected"]})
        if fn is not None and (kind in COUNTABLE_KINDS or kind == "numerical"):
            try:
                values = [float(fn(sc, rr)) for rr in mc["results"]]
            except MetricUnavailable:
                values = None
            if values is not None:
                row.update(_band(values, bounds))
    return row


def evaluate_expectations(scenario: str | Scenario, result: Mapping[str, Any], *,
                          mc: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Score `result` (a simulate() result for `scenario`) against every registered
    expectation of that scenario. One row per expectation, in order; never omits one.

    `scenario` is an id or a Scenario object (the self-test plants registries this way).
    `mc` (from monte_carlo_runs) adds each metric's per-sample band and in-range share.
    Raises ConfigurationError on duplicate expectation ids or an unknown role, and
    ExpectationSkippedError if a row were ever dropped.
    """
    sc = _resolve(scenario)
    if result.get("scenario") != sc.id:
        raise ValueError(f"result is for scenario {result.get('scenario')!r}, not {sc.id!r}")
    expects = (sc.validation or {}).get("expects") or []
    explicit = [e["id"] for e in expects if e.get("id")]
    dupes = sorted({x for x in explicit if explicit.count(x) > 1})
    if dupes:
        raise ConfigurationError(f"expectation registry of {sc.id} has duplicate ids {dupes}",
                                 expected="unique ids", received=dupes, scenario=sc.id)
    rows = [_evaluate_one(sc, i, e, result, mc) for i, e in enumerate(expects)]
    if len(rows) != len(expects):  # structural guarantee, kept as a loud check (HREQ-V-12)
        raise ExpectationSkippedError("evaluate_expectations dropped a row",
                                      expected=len(expects), received=len(rows),
                                      scenario=sc.id, model_version=MODEL_VERSION)
    return rows


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """docs/health/03 §5.2 rule 6 and HREQ-D-06 (the CLI status line prints these):

        counted_pass, counted_fail   validation rows of a countable kind with status pass/fail
        not_checked                  validation-role rows that were not scored (with reasons)
        counted_by_registration      counted pass/fail split by `registered`
        calibration                  {"n", "statuses": {pass, fail, not_checked}} -- never counted
        structural                   {"n", "statuses": {pass, fail, not_checked}} -- never counted
        calibrated_parameters        sorted union of every calibration row's `calibrates`
        missing_extractors           ids of countable rows that have no extractor yet
        rows, meta                   row count; disclaimer, validation status, model version
    """
    def role_column() -> dict[str, Any]:
        return {"n": 0, "statuses": {"pass": 0, "fail": 0, "not_checked": 0}}

    out: dict[str, Any] = {
        "rows": len(rows), "counted_pass": 0, "counted_fail": 0, "not_checked": 0,
        "counted_by_registration": {}, "calibration": role_column(),
        "structural": role_column(), "calibrated_parameters": [], "missing_extractors": [],
        "meta": result_meta(),
    }
    calibrated: set[str] = set()
    for row in rows:
        if row["reason"].startswith(NO_EXTRACTOR):
            out["missing_extractors"].append(row["id"])
        if row["role"] in ("calibration", "structural"):
            col = out[row["role"]]
            col["n"] += 1
            col["statuses"][row["status"]] += 1
            calibrated.update(row.get("calibrates") or [])
            continue
        if row["counted"]:
            key = "counted_pass" if row["status"] == "pass" else "counted_fail"
            out[key] += 1
            reg = out["counted_by_registration"].setdefault(row["registered"],
                                                             {"pass": 0, "fail": 0})
            reg[row["status"]] += 1
        elif row["status"] == "not_checked":
            out["not_checked"] += 1
    out["calibrated_parameters"] = sorted(calibrated)
    return out
