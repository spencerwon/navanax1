"""Body-fluid / electrolyte / renal ODE model, V1 (port of engine/model.js).

EDUCATIONAL MODEL -- NOT MEDICAL ADVICE. Not clinically validated.

Units: time h, volume L, amount mmol (mOsm for osmoles), concentration mmol/L,
pressure mmHg, osmolality mOsm/kg (1 kg water ~ 1 L assumed), ADH pg/mL.

Every numbered block below ("M0" ... "M10", each number used once) carries the same
number as the block in reference/metabolic-map-v1/engine/model.js and matches a section of
docs/health/01_METHODOLOGY.md §2.3, so the port can be audited against the reference line
by line. Every parameter p["xxx"] is defined, with citation, range and
evidence grade, in params.json.

Port rules (what "the same model" means here):
  * identifiers are the JavaScript ones: state, ledger and derived keys, the keys of
    the fluxes() and constants() mappings, the input keys (waterIn_Lh, ...);
  * every arithmetic expression keeps the JavaScript operation order, so results agree
    with the reference to round-off (tests/health_selftest.py holds them to 1e-9);
  * JavaScript Math semantics are kept where Python's differ: Math.max/Math.min
    propagate NaN, Math.pow/Math.exp return NaN/Infinity instead of raising, and
    `x || 0` maps NaN to 0. Plain `/` in the hot path still raises ZeroDivisionError
    where JavaScript would return +-Infinity; that needs a zero volume or a degenerate
    parameter and is refused loudly instead of yielding a NaN trajectory;
  * the functions here return NaN exactly where model.js does (rhs(), derived() and
    fluxes() are pinned to the reference on NaN inputs by the fixture section nonFinite),
    but api.simulate() refuses a trajectory that holds one: it raises
    NonFiniteTrajectoryError where index.js returns the NaN trajectory (HREQ-V-07; a
    deliberate deviation, listed in api.py).
State vectors are plain Python lists of floats (no numpy).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from ..errors import InfeasibleParametersError
from .params import default_params, param_table

__all__ = [
    "DERIVED_KEYS", "GFR_NORM_BSA_M2", "IDX", "LEDGER_KEYS", "REFERENCE_PERSON", "STATE_KEYS",
    "InfeasibleParametersError", "InfeasibleParamsError", "baseline_inputs", "constants",
    "default_params", "derived", "fluxes", "initial_state", "param_table", "rhs", "strain",
]

#: Ordered state vector (spec §3.1).
STATE_KEYS: tuple[str, ...] = (
    "V_ecf",           # L    extracellular fluid volume
    "V_icf",           # L    intracellular fluid volume
    "Na_ecf",          # mmol total ECF sodium
    "K_icf",           # mmol total ICF potassium
    "osm_icf_solute",  # mOsm intracellular osmotically active solute
    "ADH",             # pg/mL plasma vasopressin
    "V_gut_water",     # L    unabsorbed water in gut
    "Na_gut",          # mmol unabsorbed Na in gut
    "Thirst",          # 0..1 thirst drive (output signal only in V1)
    "Aldo",            # rel. aldosterone activity, 1 = baseline
    "ANP",             # rel. ANP, 1 = baseline
    "MAP",             # mmHg mean arterial pressure
    "R_auto",          # rel. slow whole-body autoregulation factor on MAP, 1 = baseline (D-2; Guyton)
)
IDX: Mapping[str, int] = MappingProxyType({k: i for i, k in enumerate(STATE_KEYS)})

#: Cumulative-flux ledger integrated alongside the state (for mass-balance audits).
LEDGER_KEYS: tuple[str, ...] = (
    "water_in",   # L    ingested + metabolic water
    "water_out",  # L    urine + insensible + fecal + sweat
    "na_in",      # mmol ingested Na
    "na_out",     # mmol urine + sweat Na
    "k_in",       # mmol ingested K
    "k_out",      # mmol urine K
)

#: Derived (non-integrated) quantities returned by derived().
DERIVED_KEYS: tuple[str, ...] = (
    "Na_plasma",         # mmol/L
    "osm_plasma",        # mOsm/kg (calculated: 2[Na] + glucose/18 + BUN/2.8)
    "osm_icf",           # mOsm/kg
    "TBW",               # L total body water (ECF + ICF)
    "GFR",               # mL/min, absolute (alias of GFR_mlmin, kept for compatibility)
    "GFR_mlmin",         # mL/min, absolute: what this model body filters (D-3)
    "GFR_norm",          # mL/min per 1.73 m² body surface area (clinical convention; D-3)
    "FL_Na",             # mmol/h filtered Na load
    "FE_Na",             # % fractional excretion of Na
    "Na_excr",           # mmol/h urinary Na excretion
    "K_excr",            # mmol/h urinary K excretion
    "U_osm",             # mOsm/kg urine osmolality
    "urine_flow",        # L/h
    "urine_flow_mLmin",  # mL/min
    "C_H2O",             # L/h free-water clearance (urine_flow − osmolar clearance)
    "ADH_target",        # pg/mL instantaneous secretion target
    "strain_transport",  # strain index components (dimensionless loads ≥ 0)
    "strain_excretion",
    "strain_glomerular",
    "strain_concentrating",
    "strain_index",      # 0..1 composite KIDNEY STRAIN INDEX (an index, NOT a clinical measure)
)

#: The person every V1 number describes (decision D-3, approved by Spencer 2026-10-01).
#: Volumes, intakes and GFR_0 in params.json are textbook values for this person.
#: bsa_m2 = 1.73 m² is the conventional body surface area used to normalise GFR
#: ("per 1.73 m²"), so for this person absolute and normalised GFR are equal.
#: V2 will scale volumes and GFR with body size; until then this is fixed.
REFERENCE_PERSON: Mapping[str, Any] = MappingProxyType({
    "name": "reference adult (70 kg)",
    "mass_kg": 70,
    "bsa_m2": 1.73,
    "ageGroup": "adult",
})
#: Body surface area that "normalised" GFR is expressed per (clinical convention).
GFR_NORM_BSA_M2 = 1.73

LN2 = 0.6931471805599453   # == JavaScript Math.LN2 == math.log(2) (nearest double to ln 2)
H_PER_DAY = 24

_N_STATE = len(STATE_KEYS)
_INF = math.inf
_NAN = math.nan


#: The engine raises the taxonomy's error (health.errors, S3/NUM: the deterministic path
#: refuses; the Monte Carlo sampler counts the rejection instead, HREQ-P-04). The short
#: name is kept as an alias; there is no parallel hierarchy in the engine.
InfeasibleParamsError = InfeasibleParametersError


# ---------------------------------------------------------------------------
# JavaScript Math semantics where Python's differ (NaN propagation, no exceptions).
# ---------------------------------------------------------------------------
def _max0(x: float) -> float:
    """Math.max(0, x): NaN propagates (Python's max(0, nan) would return 0)."""
    return 0.0 if x <= 0 else x


def _min1(x: float) -> float:
    """Math.min(1, x): NaN propagates."""
    return 1.0 if x >= 1 else x


def _clamp01(x: float) -> float:
    """clamp01 in model.js: x < 0 ? 0 : x > 1 ? 1 : x (NaN passes through)."""
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _js_min(*xs: float) -> float:
    """Math.min(...xs): NaN if any argument is NaN."""
    best = _INF
    for x in xs:
        if x != x:
            return _NAN
        if x < best:
            best = x
    return best


def _pow(x: float, y: float) -> float:
    """Math.pow(x, y): IEEE results where math.pow raises, and JS's NaN cases."""
    try:
        r = math.pow(x, y)
    except ValueError:          # 0 ** negative, or negative ** non-integer
        if x == 0:
            odd = float(y).is_integer() and y % 2 == 1
            return math.copysign(_INF, x) if odd else _INF
        return _NAN
    except OverflowError:
        odd = float(y).is_integer() and y % 2 == 1
        return -_INF if (x < 0 and odd) else _INF
    # C99 pow(1, NaN) and pow(-1, +-inf) are 1; ECMAScript says NaN for both.
    if r == 1.0 and (y != y or y == _INF or y == -_INF):
        return _NAN
    return r


def _exp(x: float) -> float:
    """Math.exp(x): +Infinity on overflow instead of OverflowError."""
    try:
        return math.exp(x)
    except OverflowError:
        return _INF


def _div(a: float, b: float) -> float:
    """IEEE a / b (used on the cold path in constants(), where a zero can come from params)."""
    try:
        return a / b
    except ZeroDivisionError:
        if a != a or a == 0:
            return _NAN
        return math.copysign(_INF, a) * math.copysign(1.0, b)


def _or0(x: Any) -> Any:
    """JavaScript `x || 0` for a number: undefined/None, 0 and NaN all become 0."""
    return x if (x and x == x) else 0


# ---------------------------------------------------------------------------
# M0. Derived constants and the analytic reference steady state.
#
# The reference steady state is the state at which, with baseline inputs,
# every derivative in rhs() is exactly zero. It is obtained in closed form:
#   1. urine flow must equal net water intake            -> V_ur_ss
#   2. urine solute must equal solute intake (+ urea)     -> S_ss
#   3. urine osmolality U_ss = S_ss / V_ur_ss              (M7)
#   4. invert the ADH -> U_osm Hill curve                  -> ADH_ss
#   5. invert the osmolality -> ADH line (M4)              -> osm_ss -> [Na]_ss
#   6. choose FE0 so Na excretion equals Na intake at [Na]_ss, V_ecf_0, MAP_0.
# Note: osm_icf_solute − 2·K_icf is an exact invariant of the model (non-K ICF
# solute is neither produced nor excreted in V1), so the steady state is unique
# only once V_icf_0 fixes that invariant. That is why it is set analytically.
#
# Cache: the JavaScript caches per params OBJECT (WeakMap + snapshot check, so in-place
# edits of `p` are safe). Python dicts cannot be weakly referenced, so the cache here is
# keyed by the params' CONTENT (the tuple of items): an in-place edit changes the key and
# misses, exactly like the JS snapshot check, and a recycled id() can never return a
# stale entry. Entries are read-only views; the cache is bounded (oldest evicted first).
# ---------------------------------------------------------------------------
_CACHE: dict[tuple[tuple[str, Any], ...], Mapping[str, Any]] = {}
_CACHE_MAX = 4096
_CACHE_LOCK = threading.Lock()


def constants(p: Mapping[str, Any]) -> Mapping[str, Any]:
    """Derived constants and the analytic steady state for params `p` (read-only mapping)."""
    try:
        key: tuple[tuple[str, Any], ...] | None = tuple(p.items())
        hit = _CACHE.get(key)  # type: ignore[arg-type]
    except TypeError:          # an unhashable value: compute, do not cache
        key, hit = None, None
    if hit is not None:
        return hit

    c_other = p["glucose_mgdl"] / 18 + p["bun_mgdl"] / 2.8            # mOsm/kg, non-Na plasma solute
    waterIn_h = p["waterIn_base_Ld"] / H_PER_DAY                       # L/h
    naIn_h = p["naIn_base_mmold"] / H_PER_DAY                          # mmol/h
    kIn_h = p["kIn_base_mmold"] / H_PER_DAY                            # mmol/h
    metab_h = p["water_metabolic_Ld"] / H_PER_DAY                      # L/h
    insens_h = p["insensible_Ld"] / H_PER_DAY                          # L/h
    fecal_h = p["fecal_water_Ld"] / H_PER_DAY                          # L/h
    urea_h = p["urea_excr_mosmd"] / H_PER_DAY                          # mOsm/h
    GFR0_Lh = p["GFR_0"] * 60 / 1000                                   # mL/min -> L/h

    V_ur_ss = waterIn_h + metab_h - insens_h - fecal_h                 # L/h
    S_ss = 2 * naIn_h + 2 * kIn_h + urea_h                             # mOsm/h
    U_ss = _div(S_ss, V_ur_ss)                                         # mOsm/kg
    h = _div(U_ss - p["U_osm_min"], p["U_osm_max"] - p["U_osm_min"])   # fraction of concentrating range

    feasible = V_ur_ss > 0 and h > 0 and h < 1
    ADH_ss = (p["adh_ec50"] * _pow(_div(h, 1 - h), _div(1, p["adh_hill"]))
              if feasible else _NAN)
    osm_ss = p["adh_threshold"] + _div(ADH_ss, p["adh_slope"])
    Na_ss = (osm_ss - c_other) / 2
    FL_ss = GFR0_Lh * Na_ss
    FE0 = _div(naIn_h, FL_ss)                                          # baseline fractional excretion

    # Water-shift conductance from the equilibration time constant (M3):
    # linearised rate = Lp · (osm/V_icf + (osm − c_other)/V_ecf) = 1/tau.
    Lp = _div(1, p["osm_eq_tau_h"] * (_div(osm_ss, p["V_icf_0"])
                                      + _div(osm_ss - c_other, p["V_ecf_0"])))

    C: dict[str, Any] = {
        "feasible": feasible, "c_other": c_other, "waterIn_h": waterIn_h, "naIn_h": naIn_h,
        "kIn_h": kIn_h, "metab_h": metab_h, "insens_h": insens_h, "fecal_h": fecal_h,
        "urea_h": urea_h, "GFR0_Lh": GFR0_Lh,
        "V_ur_ss": V_ur_ss, "S_ss": S_ss, "U_ss": U_ss, "ADH_ss": ADH_ss, "osm_ss": osm_ss,
        "Na_ss": Na_ss, "FL_ss": FL_ss, "FE0": FE0, "Lp": Lp,
        "Treab_ss": FL_ss - naIn_h,
        "k_gut_w": _div(LN2, p["gut_water_thalf_h"]),
        "k_gut_na": _div(LN2, p["gut_na_thalf_h"]),
        "k_adh": _div(LN2, p["adh_thalf_h"]),
        "k_anp": _div(LN2, p["anp_thalf_h"]),
    }
    # Fastest time constant in the system (h) — used by the solver for step control.
    C["tau_min"] = _js_min(p["osm_eq_tau_h"], _div(1, C["k_gut_w"]), _div(1, C["k_gut_na"]),
                           _div(1, C["k_adh"]), _div(1, C["k_anp"]),
                           p["thirst_tau_h"], p["aldo_tau_h"], p["map_tau_h"])
    view = MappingProxyType(C)
    if key is not None:
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[key] = view
    return view


def baseline_inputs(p: Mapping[str, Any]) -> dict[str, float]:
    """Baseline inputs (continuous average diet) for given params."""
    return {
        "waterIn_Lh": p["waterIn_base_Ld"] / H_PER_DAY,
        "naIn_mmolh": p["naIn_base_mmold"] / H_PER_DAY,
        "kIn_mmolh": p["kIn_base_mmold"] / H_PER_DAY,
        "sweat_Lh": 0,
        "exercise": 0,  # accepted for API compatibility; unused in V1
    }


def _js_num(x: float) -> str:
    """`${x}` for the error message (JS prints 1 not 1.0, NaN not nan)."""
    if x != x:
        return "NaN"
    if x in (_INF, -_INF):
        return "Infinity" if x > 0 else "-Infinity"
    if float(x).is_integer() and abs(x) < 1e21:
        return str(int(x))
    return repr(float(x))


def initial_state(p: Mapping[str, Any]) -> list[float]:
    """Reference steady state (spec §3 initialState). Raises if params are physically infeasible."""
    C = constants(p)
    if not C["feasible"]:
        U = C["U_ss"]
        u_txt = (f"{U:.0f}" if math.isfinite(U) else _js_num(U))
        raise InfeasibleParametersError(
            f"initialState: infeasible params (required urine osmolality {u_txt} "
            f"outside ({_js_num(p['U_osm_min'])}, {_js_num(p['U_osm_max'])}) or non-positive "
            f"urine flow {_js_num(C['V_ur_ss'])})",
            expected="U_osm_min < U_ss < U_osm_max and V_ur_ss > 0",
            received={"U_ss": U, "V_ur_ss": C["V_ur_ss"], "U_osm_min": p["U_osm_min"],
                      "U_osm_max": p["U_osm_max"]})
    y = [0.0] * _N_STATE
    y[IDX["V_ecf"]] = p["V_ecf_0"]
    y[IDX["V_icf"]] = p["V_icf_0"]
    y[IDX["Na_ecf"]] = C["Na_ss"] * p["V_ecf_0"]
    y[IDX["K_icf"]] = p["K_icf_0"]
    y[IDX["osm_icf_solute"]] = C["osm_ss"] * p["V_icf_0"]
    y[IDX["ADH"]] = C["ADH_ss"]
    y[IDX["V_gut_water"]] = C["waterIn_h"] / C["k_gut_w"]   # gut content in balance with continuous intake
    y[IDX["Na_gut"]] = C["naIn_h"] / C["k_gut_na"]
    y[IDX["Thirst"]] = _clamp01(p["thirst_slope"] * (C["osm_ss"] - p["thirst_threshold"]))
    y[IDX["Aldo"]] = 1
    y[IDX["ANP"]] = 1
    y[IDX["MAP"]] = p["MAP_0"]
    y[IDX["R_auto"]] = 1
    return [float(v) for v in y]


# ---------------------------------------------------------------------------
# fluxes(): every physiological flux, computed once and shared by rhs(),
# derived() and the mass-balance ledger. inputs may be None (derived only).
# ---------------------------------------------------------------------------
_ZERO_INPUTS: Mapping[str, float] = MappingProxyType(
    {"waterIn_Lh": 0, "naIn_mmolh": 0, "kIn_mmolh": 0, "sweat_Lh": 0, "exercise": 0})


def fluxes(y: Sequence[float], p: Mapping[str, Any], inputs: Mapping[str, Any] | None = None,
           C: Mapping[str, Any] | None = None) -> dict[str, float]:
    """Every flux for state y (keys and order as the object returned by fluxes() in model.js)."""
    if C is None:
        C = constants(p)
    V_ecf, V_icf, Na_ecf, K_icf, S_icf, ADH = y[0], y[1], y[2], y[3], y[4], y[5]
    V_gut, Na_gut, Aldo, ANP, MAP, R_auto = y[6], y[7], y[9], y[10], y[11], y[12]

    vr = V_ecf / p["V_ecf_0"]                     # relative ECF volume
    Na_c = Na_ecf / V_ecf                         # plasma [Na], mmol/L
    osm_e = 2 * Na_c + C["c_other"]               # plasma osmolality, mOsm/kg
    osm_i = S_icf / V_icf                         # ICF osmolality

    # M2. Gut absorption (first-order).
    J_gut_w = C["k_gut_w"] * V_gut                # L/h
    J_gut_na = C["k_gut_na"] * Na_gut             # mmol/h

    # M3. Osmotic water shift ECF -> ICF (positive = into cells).
    J_shift = C["Lp"] * (osm_i - osm_e)           # L/h

    # M4. ADH secretion target: osmotic line whose threshold moves with volume
    #     (hypovolemia lowers threshold, expansion raises it).
    adh_thr = p["adh_threshold"] + p["adh_vol_shift"] * 100 * (vr - 1)
    ADH_target = _max0(p["adh_slope"] * (osm_e - adh_thr))

    # M5. Thirst target (osmotic + hypovolemic), 0..1.
    Thirst_target = _clamp01(p["thirst_slope"] * (osm_e - p["thirst_threshold"])
                             + p["thirst_vol_gain"] * 100 * _max0(1 - vr))

    # M6. Volume-driven hormone and pressure targets.
    Aldo_target = _pow(vr, -p["aldo_vol_exp"])
    ANP_target = _pow(vr, p["anp_vol_exp"])
    # MAP: the long-term volume elasticity map_vol_exp is split into a fast part (cardiac output,
    # within hours) and a slow part delivered through R_auto (Guyton whole-body autoregulation:
    # over days to weeks, raised flow is converted into raised peripheral resistance).
    # At steady state R_auto = vr^(map_auto_frac·map_vol_exp), so MAP_ss = MAP_0·vr^map_vol_exp exactly
    # as before D-2; only the time course changes.
    MAP_target = p["MAP_0"] * _pow(vr, (1 - p["map_auto_frac"]) * p["map_vol_exp"]) * R_auto
    R_auto_target = _pow(vr, p["map_auto_frac"] * p["map_vol_exp"])

    # M7. Kidney.
    GFR = C["GFR0_Lh"] * _pow(MAP / p["MAP_0"], p["gfr_map_exp"]) * _pow(vr, p["gfr_vol_exp"])  # L/h
    FL = GFR * Na_c                                                        # filtered Na, mmol/h
    FE = _min1(C["FE0"]
               * _pow(Aldo, -p["aldo_effect_exp"])                         # aldosterone: retains Na
               * _pow(ANP, p["anp_effect_exp"])                            # ANP: natriuretic
               * _exp(p["pn_gain"] * (MAP - p["MAP_0"]))                   # pressure natriuresis
               * _exp(p["na_osm_gain"] * (Na_c - C["Na_ss"])))             # osmotic natriuresis
    Na_ur = FL * FE                                                        # mmol/h
    K_ur = C["kIn_h"] * _exp(p["k_excr_gain"] * (K_icf / p["K_icf_0"] - 1))  # mmol/h
    solute_ur = 2 * Na_ur + 2 * K_ur + C["urea_h"]                         # mOsm/h
    An = _pow(_max0(ADH), p["adh_hill"])
    U_osm = (p["U_osm_min"] + (p["U_osm_max"] - p["U_osm_min"]) * An
             / (An + _pow(p["adh_ec50"], p["adh_hill"])))
    V_ur = solute_ur / U_osm                                               # L/h
    C_osm = solute_ur / osm_e                                              # osmolar clearance, L/h

    # M8. Inputs and non-renal losses.
    inp = inputs if inputs is not None else _ZERO_INPUTS
    sweat = _or0(inp.get("sweat_Lh"))
    sweat_na = sweat * p["sweat_na_mmolL"]

    return {
        "vr": vr, "Na_c": Na_c, "osm_e": osm_e, "osm_i": osm_i, "J_gut_w": J_gut_w,
        "J_gut_na": J_gut_na, "J_shift": J_shift, "adh_thr": adh_thr, "ADH_target": ADH_target,
        "Thirst_target": Thirst_target, "Aldo_target": Aldo_target, "ANP_target": ANP_target,
        "MAP_target": MAP_target, "R_auto_target": R_auto_target, "GFR": GFR, "FL": FL, "FE": FE,
        "Na_ur": Na_ur, "K_ur": K_ur, "solute_ur": solute_ur, "U_osm": U_osm, "V_ur": V_ur,
        "C_osm": C_osm,
        "waterIn": _or0(inp.get("waterIn_Lh")), "naIn": _or0(inp.get("naIn_mmolh")),
        "kIn": _or0(inp.get("kIn_mmolh")),
        "sweat": sweat, "sweat_na": sweat_na, "metab": C["metab_h"], "insens": C["insens_h"],
        "fecal": C["fecal_h"],
    }


def rhs(t: float, y: Sequence[float], p: Mapping[str, Any], inputs: Mapping[str, Any] | None,
        out: list[float] | None = None, ledger_out: list[float] | None = None,
        C: Mapping[str, Any] | None = None) -> list[float]:
    """dy/dt (spec §3 rhs). t is unused (the model is autonomous; time-dependence
    enters only through `inputs`) but kept in the signature per spec.
    If `ledger_out` (length 6) is supplied, the ledger derivatives are written into it.
    `C` (from constants(p)) may be passed by hot loops to skip the cache lookup.
    """
    if C is None:
        C = constants(p)
    f = fluxes(y, p, inputs, C)
    dy = out if out is not None else [0.0] * _N_STATE
    # M1. Water and solute balances.
    dy[0] = f["J_gut_w"] + f["metab"] - f["J_shift"] - f["V_ur"] - f["insens"] - f["fecal"] - f["sweat"]  # V_ecf
    dy[1] = f["J_shift"]                                         # V_icf
    dy[2] = f["J_gut_na"] - f["Na_ur"] - f["sweat_na"]           # Na_ecf
    dy[3] = f["kIn"] - f["K_ur"]                                 # K_icf (absorbed K enters cells directly)
    dy[4] = 2 * (f["kIn"] - f["K_ur"])                           # osm_icf_solute (K + anion)
    # M4-M6. First-order hormone / drive / pressure dynamics.
    dy[5] = C["k_adh"] * (f["ADH_target"] - y[5])                # ADH (secretion = clearance·target)
    dy[6] = f["waterIn"] - f["J_gut_w"]                          # V_gut_water
    dy[7] = f["naIn"] - f["J_gut_na"]                            # Na_gut
    dy[8] = (f["Thirst_target"] - y[8]) / p["thirst_tau_h"]      # Thirst
    dy[9] = (f["Aldo_target"] - y[9]) / p["aldo_tau_h"]          # Aldo
    dy[10] = C["k_anp"] * (f["ANP_target"] - y[10])              # ANP
    dy[11] = (f["MAP_target"] - y[11]) / p["map_tau_h"]          # MAP
    dy[12] = (f["R_auto_target"] - y[12]) / p["map_auto_tau_h"]  # R_auto (slow, days-weeks)
    # M9. Outputs that do not feed back: the cumulative-flux ledger (integrated beside the
    #     state for the mass-balance audit, LEDGER_KEYS) here, and derived() below, which
    #     assembles DERIVED_KEYS from fluxes() and the strain index (M10).
    if ledger_out is not None:
        ledger_out[0] = f["waterIn"] + f["metab"]
        ledger_out[1] = f["V_ur"] + f["insens"] + f["fecal"] + f["sweat"]
        ledger_out[2] = f["naIn"]
        ledger_out[3] = f["Na_ur"] + f["sweat_na"]
        ledger_out[4] = f["kIn"]
        ledger_out[5] = f["K_ur"]
    return dy


# ---------------------------------------------------------------------------
# M10. KIDNEY STRAIN INDEX
#
# THIS IS AN INDEX, NOT A CLINICAL MEASURE. It has no units, no validated
# thresholds and no diagnostic meaning. It summarises, on a 0..1 scale, how far
# four mechanistically motivated renal workloads are pushed ABOVE the model's
# own baseline:
#   transport     — tubular Na reabsorption (≈ renal O2 consumption; Brezis & Rosen 1995,
#                   Sejersted 1982 [dog])
#   excretion     — Na excretory burden relative to baseline excretion
#   glomerular    — glomerular pressure / hyperfiltration proxy (Brenner 1982):
#                   w_p·(MAP − MAP_0)/scale_pressure + w_f·(GFR/GFR_0 − 1)/scale_filtration,
#                   w_p = w_f = 0.5 (a mean) and scale_filtration = 0.1 by default
#                   (strain_w_glomerular_pressure, strain_w_glomerular_filtration,
#                   strain_scale_filtration: params.json rows since 1.1.0, BUG-20261003-095)
#   concentrating — fraction of the remaining urine-concentrating range in use
#                   (medullary transport demand; Brezis & Rosen 1995)
# raw   = Σ w_i · load_i        (weights and scales: params.json, all E-assumption)
# index = raw / (1 + raw)       (0 at baseline; 0.5 when the weighted load = 1 unit)
# ---------------------------------------------------------------------------
def strain(f: Mapping[str, float], p: Mapping[str, Any], C: Mapping[str, Any],
           MAP: float) -> dict[str, float]:
    """Strain-index components and the composite index (an index, not a clinical measure)."""
    transport = _max0((f["FL"] - f["Na_ur"]) / C["Treab_ss"] - 1) / p["strain_scale_transport"]
    excretion = _max0(f["Na_ur"] / C["naIn_h"] - 1) / p["strain_scale_excretion"]
    glomerular = (p["strain_w_glomerular_pressure"]
                  * _max0((MAP - p["MAP_0"]) / p["strain_scale_pressure"])
                  + p["strain_w_glomerular_filtration"]
                  * _max0((f["GFR"] / C["GFR0_Lh"] - 1) / p["strain_scale_filtration"]))
    concentrating = _max0((f["U_osm"] - C["U_ss"]) / (p["U_osm_max"] - C["U_ss"]))
    raw = (p["strain_w_transport"] * transport + p["strain_w_excretion"] * excretion
           + p["strain_w_glomerular"] * glomerular + p["strain_w_concentrating"] * concentrating)
    return {"transport": transport, "excretion": excretion, "glomerular": glomerular,
            "concentrating": concentrating, "index": raw / (1 + raw)}


def derived(y: Sequence[float], p: Mapping[str, Any],
            C: Mapping[str, Any] | None = None) -> dict[str, float]:
    """Derived quantities for one state (spec §3 derived; block M9), keyed by DERIVED_KEYS in order."""
    if C is None:
        C = constants(p)
    f = fluxes(y, p, None, C)
    s = strain(f, p, C, y[11])
    return {
        "Na_plasma": f["Na_c"],
        "osm_plasma": f["osm_e"],
        "osm_icf": f["osm_i"],
        "TBW": y[0] + y[1],
        "GFR": f["GFR"] * 1000 / 60,
        "GFR_mlmin": f["GFR"] * 1000 / 60,
        "GFR_norm": (f["GFR"] * 1000 / 60) * GFR_NORM_BSA_M2 / REFERENCE_PERSON["bsa_m2"],
        "FL_Na": f["FL"],
        "FE_Na": 100 * f["FE"],
        "Na_excr": f["Na_ur"],
        "K_excr": f["K_ur"],
        "U_osm": f["U_osm"],
        "urine_flow": f["V_ur"],
        "urine_flow_mLmin": f["V_ur"] * 1000 / 60,
        "C_H2O": f["V_ur"] - f["C_osm"],
        "ADH_target": f["ADH_target"],
        "strain_transport": s["transport"],
        "strain_excretion": s["excretion"],
        "strain_glomerular": s["glomerular"],
        "strain_concentrating": s["concentrating"],
        "strain_index": s["index"],
    }
