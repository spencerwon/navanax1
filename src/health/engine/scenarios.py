"""Named interventions (port of engine/scenarios.js, spec §5).

A scenario is a frozen `Scenario` record:
  id, title, description, t_end (h), dt (h), out_every, breakpoints,
  events, overrides, inputs(t, params) -> {waterIn_Lh, naIn_mmolh, kIn_mmolh, sweat_Lh,
  exercise}, validation {summary, expects: [...], evidence: [...], boundary?}, label, sweep?
Inputs are PIECEWISE CONSTANT; every time at which they jump is listed in
`breakpoints` so the solver never integrates across a discontinuity.
Baseline diet (water, Na, K) is continuous and always present unless a scenario
overrides it (no_water_24h removes drinking water).

The `validation` records below are the literature-validation contract. They are copied
verbatim from scenarios.js -- ids, titles, descriptions, events, overrides, tEnd/dt and
every expectation's metric/target/range/kind/evidence/note -- and the self-test compares
them field by field with the JavaScript reference. Do not paraphrase them here; change
them in the reference first and regenerate the golden fixture.

Event and override dicts keep the JavaScript keys (start, durMin, water_L, salt_g, end,
waterIn_Lh, naIn_mmolh) and are read-only views.

Python-only annotations (docs/health/03 §5.1-5.2; the JavaScript reference is unchanged
this phase): an expectation carries a stable `id` ("<scenario_id>/<NN>", 1-based;
HREQ-E-13) and may carry `role` -- "validation" (the default when absent), "calibration"
(with `calibrates`, the parameters tuned to it) or "structural". Only validation rows can
enter the validation totals. These three keys (id, role, calibrates) are the ONLY ones the
self-test allows to differ from the JavaScript records (PY_ONLY_EXPECTATION_KEYS).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Any

from .model import baseline_inputs

__all__ = [
    "MMOL_NA_PER_G_NACL", "PY_ONLY_EXPECTATION_KEYS", "SCENARIOS", "Scenario", "Sweep", "make_salt_load", "make_scenario",
    "make_water_load",
]

#: Keys an expectation record may carry here and not in scenarios.js.
PY_ONLY_EXPECTATION_KEYS: tuple[str, ...] = ("id", "role", "calibrates")

#: mmol Na per gram NaCl (molar mass 58.44 g/mol). Physical constant.
MMOL_NA_PER_G_NACL = 1000 / 58.44

InputsFn = Callable[[float, Mapping[str, Any]], dict[str, float]]


@dataclass(frozen=True, eq=False)
class Sweep:
    """A dose sweep attached to a scenario (salt_load_sweep.sweep in scenarios.js)."""

    variable: str
    unit: str
    values: tuple[float, ...]
    make: Callable[[float], Scenario]
    metrics: tuple[str, ...]


@dataclass(frozen=True, eq=False)
class Scenario:
    """One named intervention. `inputs(t, params)` is the piecewise-constant input schedule."""

    id: str
    title: str
    description: str
    t_end: float
    dt: float
    out_every: int
    breakpoints: tuple[float, ...]
    events: tuple[Mapping[str, Any], ...]
    overrides: tuple[Mapping[str, Any], ...]
    inputs: InputsFn
    validation: dict[str, Any] | None
    label: str | None
    sweep: Sweep | None = None


def _js_str(x: float) -> str:
    """`${x}` in a JavaScript template literal for the numbers scenario ids use (1 not 1.0)."""
    if isinstance(x, float) and x.is_integer() and abs(x) < 1e21:
        return str(int(x))
    return str(x)


def _to_fixed0(x: float) -> str:
    """JavaScript Number.prototype.toFixed(0): exact decimal value, ties away from zero."""
    if x == 0:
        return "0"                      # (-0).toFixed(0) is "0"; (-0.4).toFixed(0) is "-0"
    if x != x or x in (float("inf"), float("-inf")):
        return "NaN" if x != x else ("Infinity" if x > 0 else "-Infinity")
    return str(Decimal(x).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _truthy_number(x: Any) -> bool:
    """JavaScript truthiness of a number: undefined/None, 0 and NaN are false."""
    return bool(x) and x == x


def make_scenario(*, id: str, title: str, description: str, t_end: float,
                  dt: float = 1 / 60, out_every: int = 1,
                  events: Iterable[Mapping[str, Any]] = (),
                  overrides: Iterable[Mapping[str, Any]] = (),
                  validation: dict[str, Any] | None = None,
                  label: str | None = None) -> Scenario:
    """Build a scenario from a list of bolus events on top of the baseline diet.

    event:    {start (h), durMin (minutes), water_L?, salt_g?}
    override: {start, end, waterIn_Lh?, naIn_mmolh?} replaces baseline rates inside [start, end).
    """
    evs = tuple(MappingProxyType(dict(e)) for e in events)
    ovs = tuple(MappingProxyType(dict(o)) for o in overrides)
    bp: dict[float, None] = {}
    for e in evs:
        bp[e["start"]] = None
        bp[e["start"] + e["durMin"] / 60] = None
    for o in ovs:
        bp[o["start"]] = None
        bp[o["end"]] = None
    breakpoints = tuple(sorted(bp))

    def inputs(t: float, p: Mapping[str, Any]) -> dict[str, float]:
        u = baseline_inputs(p)
        for o in ovs:
            if t >= o["start"] and t < o["end"]:
                if "waterIn_Lh" in o:
                    u["waterIn_Lh"] = o["waterIn_Lh"]
                if "naIn_mmolh" in o:
                    u["naIn_mmolh"] = o["naIn_mmolh"]
        for e in evs:
            d = e["durMin"] / 60
            if t >= e["start"] and t < e["start"] + d:
                if _truthy_number(e.get("water_L")):
                    u["waterIn_Lh"] += e["water_L"] / d
                if _truthy_number(e.get("salt_g")):
                    u["naIn_mmolh"] += e["salt_g"] * MMOL_NA_PER_G_NACL / d
        return u

    return Scenario(id=id, title=title, description=description, t_end=t_end, dt=dt,
                    out_every=out_every, breakpoints=breakpoints, events=evs, overrides=ovs,
                    inputs=inputs, validation=validation, label=label or None)


def make_water_load(liters: float, dur_min: float = 10, at: float = 1,
                    t_end: float = 12) -> Scenario:
    """Water bolus: `liters` drunk over `dur_min` minutes starting at `at` h."""
    L, D, A = _js_str(liters), _js_str(dur_min), _js_str(at)
    return make_scenario(
        id=f"water_{L}L_{D}min", title=f"Drink {L} L water over {D} min",
        description=f"Oral water load of {L} L over {D} min at t={A} h on top of baseline diet.",
        t_end=t_end, events=[{"start": at, "durMin": dur_min, "water_L": liters}], validation=None,
    )


def make_salt_load(grams: float, water_l: float = 0.5, dur_min: float = 15, at: float = 1,
                   t_end: float = 72) -> Scenario:
    """Salt bolus: `grams` NaCl with `water_l` water over `dur_min` minutes at `at` h."""
    G, W, D, A = _js_str(grams), _js_str(water_l), _js_str(dur_min), _js_str(at)
    return make_scenario(
        id=f"salt_{G}g", title=f"Eat {G} g salt (+{W} L water)",
        description=(f"Oral NaCl load {G} g ({_to_fixed0(grams * MMOL_NA_PER_G_NACL)} mmol Na) with "
                     f"{W} L water over {D} min at t={A} h, on top of baseline diet."),
        t_end=t_end, events=[{"start": at, "durMin": dur_min, "water_L": water_l, "salt_g": grams}],
        validation=None,
    )


DRINK_START = 1  # h

baseline = make_scenario(
    id="baseline", title="Baseline (nothing happens)",
    description="Continuous average diet (2.1 L/day water, 150 mmol/day Na, 80 mmol/day K). The model should sit exactly at its steady state.",
    t_end=24,
    validation={
        "summary": "All states remain at the analytic steady state (numerical drift only).",
        "expects": [
            {"id": "baseline/01", "metric": "max |Δstate| / scale over 24 h", "target": "< 1e-6", "kind": "numerical"},
        ],
        "evidence": ["ev:guyton-hall-2021"],
    },
)

drink_water_1L = make_scenario(
    id="drink_water_1L", title="Drink 1 L of water in 10 min",
    description="1 L plain water over 10 min at t = 1 h.",
    t_end=12, events=[{"start": DRINK_START, "durMin": 10, "water_L": 1}],
    validation={
        "summary": "Plasma Na dips ~2-4 mmol/L, ADH is suppressed, urine dilutes toward ~50-70 mOsm/kg, water diuresis peaks ~1-2 h after drinking and the load is largely excreted within ~3-4 h.",
        "expects": [
            {"id": "drink_water_1L/01", "metric": "Na_plasma nadir − baseline", "target": "[-5, -1] mmol/L", "range": [-5, -1], "kind": "quantitative", "evidence": ["ev:baylis-1986"], "note": "Spec §8. Baylis: sustained water load lowered osmolality ~7 mOsm/kg (≈ −3.5 mmol/L Na)."},
            {"id": "drink_water_1L/02", "metric": "time to recover |ΔNa| < 0.5 mmol/L after drinking starts", "target": "< 6 h", "range": [0, 6], "kind": "quantitative", "evidence": ["ev:crowe-1987"]},
            {"id": "drink_water_1L/03", "metric": "time of peak urine flow after drinking starts", "target": "~1-2 h (tolerance 0.5-2.5 h)", "range": [0.5, 2.5], "kind": "quantitative", "evidence": ["ev:crowe-1987", "ev:shafiee-2005"]},
            {"id": "drink_water_1L/04", "metric": "peak urine flow", "target": "≈0.5-0.8 L/h (Shafiee 11 mL/min for ~1.4 L; Crowe ~0.76 L/h for 20 mL/kg)", "range": [0.35, 0.9], "kind": "quantitative", "evidence": ["ev:shafiee-2005", "ev:crowe-1987"], "note": "Literature loads were ~1.4 L, so a 1 L load is expected at or below their peak."},
            {"id": "drink_water_1L/05", "metric": "minimum urine osmolality", "target": "≈50-100 mOsm/kg", "range": [40, 150], "kind": "quantitative", "evidence": ["ev:baylis-1986"]},
            {"id": "drink_water_1L/06", "metric": "fraction of the 1 L excreted (above baseline urine) by 3 h after drinking", "target": "most of it (Crowe: ~100% of 20 mL/kg in 2 h in young water-replete men)", "range": [0.5, 1.2], "kind": "quantitative", "evidence": ["ev:crowe-1987"]},
        ],
        "evidence": ["ev:baylis-1986", "ev:crowe-1987", "ev:shafiee-2005", "ev:peronnet-2012"],
    },
)

drink_water_3L_fast = make_scenario(
    id="drink_water_3L_fast", title="Drink 3 L of water in 30 min (hyponatremia risk demo)",
    description="3 L plain water over 30 min at t = 1 h. Shows where plasma Na crosses the 135 mmol/L hyponatremia boundary.",
    t_end=12, events=[{"start": DRINK_START, "durMin": 30, "water_L": 3}],
    validation={
        "summary": "Intake rate (6 L/h) far exceeds maximal free-water excretion (~0.7 L/h), so plasma Na falls several mmol/L and can cross 135 mmol/L (hyponatremia by EAH consensus definition) before the kidney clears the load.",
        "boundary": {"key": "Na_plasma", "value": 135, "direction": "below", "evidence": ["ev:hew-butler-2015"]},
        "expects": [
            {"id": "drink_water_3L_fast/01", "metric": "Na_plasma nadir − baseline", "target": "larger fall than 1 L (≈3x)", "kind": "qualitative", "evidence": ["ev:shafiee-2005"]},
            {"id": "drink_water_3L_fast/02", "metric": "Na_plasma < 135 at any time", "target": "demonstrated in median trajectory (model behaviour, not a validated human threshold)", "kind": "qualitative", "evidence": ["ev:hew-butler-2015"]},
        ],
        "evidence": ["ev:hew-butler-2015", "ev:shafiee-2005", "ev:crowe-1987"],
    },
)

_SALT_10G = make_salt_load(10, 0.5, 15, DRINK_START, 72)
salt_load_10g = make_scenario(
    id="salt_load_10g", title="Eat 10 g salt (171 mmol Na) with 0.5 L water",
    description=_SALT_10G.description,
    t_end=72, events=[dict(e) for e in _SALT_10G.events],
    validation={
        "summary": "Plasma Na rises ~1-3 mmol/L, ADH and thirst rise, urine concentrates, ECF expands by water drawn from cells, and the extra Na is excreted only gradually over ~24-48 h.",
        "expects": [
            {"id": "salt_load_10g/01", "metric": "Na_plasma peak − baseline", "target": "[+0.5, +4] mmol/L", "range": [0.5, 4], "kind": "quantitative", "evidence": ["ev:suckling-2012", "ev:andersen-1999"], "note": "Spec §8. Suckling 2012: +3.13 ± 0.75 after 6 g in soup vs matched unsalted soup (SEM vs SD unverified; context only, not the source of this band); Andersen 1999: +2.7 after IV hypertonic load ≈10% ECF Na."},
            {"id": "salt_load_10g/02", "metric": "ADH peak > baseline", "target": "rise", "kind": "qualitative", "evidence": ["ev:andersen-2002"], "note": "Andersen 2002: AVP 1.4 -> 3.1 pg/mL after hypertonic NaCl."},
            {"id": "salt_load_10g/03", "metric": "fraction of the 171 mmol load excreted (above baseline) by 8 h", "target": "≈25% (Andersen 1999 IV load: 131 vs 81 mmol in 8 h)", "range": [0.1, 0.6], "kind": "quantitative", "evidence": ["ev:andersen-1999"], "note": "IV not oral; supine; time-control also rose. Loose band."},
            {"id": "salt_load_10g/04", "metric": "fraction excreted by 48 h", "target": "majority (delayed natriuresis over 24-48 h)", "range": [0.5, 1.05], "kind": "quantitative", "evidence": ["ev:andersen-1999"], "note": "Spec §5 expectation; the multi-day rhythmicity reported by the Titze group is not modelled."},
            {"id": "salt_load_10g/05", "metric": "urine osmolality rises", "target": "rise (free-water conservation)", "kind": "qualitative", "evidence": ["ev:rakova-2017"]},
        ],
        "evidence": ["ev:suckling-2012", "ev:andersen-1998", "ev:andersen-1999", "ev:andersen-2002", "ev:rakova-2017"],
    },
)

SWEEP_G: tuple[float, ...] = (0, 2.5, 5, 7.5, 10, 12.5, 15, 17.5, 20, 22.5, 25, 27.5, 30)
salt_load_sweep = dataclasses.replace(
    make_scenario(
        id="salt_load_sweep", title="Salt dose sweep 0-30 g (how much salt strains the kidney?)",
        description="Single oral NaCl loads from 0 to 30 g (each with 0.5 L water) at t = 1 h; 48 h per dose. The default inputs() is the 10 g member so the object is also directly simulatable.",
        t_end=48, events=[{"start": DRINK_START, "durMin": 15, "water_L": 0.5, "salt_g": 10}],
        validation={
            "summary": "Peak kidney strain index, peak Δ[Na], peak ECF expansion and 24 h Na excretion should increase monotonically with dose. The strain index is a model index, not a clinical measure.",
            "expects": [
                {"id": "salt_load_sweep/01", "metric": "peak strain_index vs dose", "target": "monotonic increase", "kind": "qualitative", "evidence": ["ev:brezis-rosen-1995", "ev:brenner-1982"]},
                {"id": "salt_load_sweep/02", "metric": "Context, not checked on this chart: 6 g salted minus matched-water control", "target": "+3.13 ± 0.75 mmol/L (Suckling 2012; SEM/SD unverified). This chart shows the rise from each person's own baseline, a different quantity; status: unverified, not counted as a pass", "kind": "unverified", "evidence": ["ev:suckling-2012"], "note": "Audit F-02/F-03 (BUG-0050, BUG-0051). The salted-minus-control comparison is a skipped test in tests/engine.test.mjs until the dispersion is confirmed from the full text."},
            ],
            "evidence": ["ev:suckling-2012", "ev:brezis-rosen-1995", "ev:brenner-1982"],
        },
    ),
    sweep=Sweep(
        variable="salt_g", unit="g NaCl", values=SWEEP_G,
        make=lambda g: make_salt_load(g, 0.5, 15, DRINK_START, 48),
        metrics=("peak_strain_index", "peak_dNa", "peak_dV_ecf", "na_excr_24h", "peak_MAP"),
    ),
)

CHRONIC_SALT_G = 15  # g/day
chronic_high_salt_30d = make_scenario(
    id="chronic_high_salt_30d", title=f"Chronic high salt: {CHRONIC_SALT_G} g/day for 30 days",
    description=f"Diet salt raised from 8.8 g/day (150 mmol) to {CHRONIC_SALT_G} g/day ({_to_fixed0(CHRONIC_SALT_G * 1000 / 58.44)} mmol) at t = 24 h and held for 30 days. MODELED RISK TRAJECTORY, NOT A PREDICTION.",
    t_end=24 * 31, dt=0.25,
    overrides=[{"start": 24, "end": 24 * 31, "naIn_mmolh": CHRONIC_SALT_G * MMOL_NA_PER_G_NACL / 24}],
    label="modeled risk trajectory — not a prediction",
    validation={
        "summary": "Na balance re-establishes within days at a slightly expanded ECF; MAP rises by a few mmHg over 2–4 weeks as slow whole-body autoregulation (state R_auto, decision D-2) adds to the pressure-natriuresis loop (Guyton/Hall), then plateaus.",
        "expects": [
            {"id": "chronic_high_salt_30d/01", "metric": "ΔMAP at day 30 per +100 mmol/day Na", "target": "normotensive meta-analysis ≈2 mmHg (95% CI-derived band 0.7-3.2)", "range": [0.7, 3.2], "kind": "quantitative", "evidence": ["ev:he-2013"], "note": "MAP band derived from He 2013 normotensive SBP/DBP CIs (MAP ≈ DBP + (SBP−DBP)/3), scaled from 75 to 100 mmol/day. Scaling uses the normotensive subgroup's own urinary Na change, −75 mmol/24 h (BMJ full text, Results, read 2026-10-01; equal to the all-trial value, so the band is unchanged). Still approximate: combining SBP and DBP CI end-points is not a true MAP CI (audit F-06, BUG-0054). Parameters map_vol_exp, pn_gain, aldo_vol_exp were chosen with this target in mind (calibration, not independent validation).", "role": "calibration", "calibrates": ["map_vol_exp", "pn_gain", "aldo_vol_exp"]},
            {"id": "chronic_high_salt_30d/02", "metric": "MAP time course", "target": "still rising at day 14; ≥ 95 % of plateau by day 30; < 60 % of plateau by day 3", "kind": "design-target", "evidence": ["ev:he-2013", "ev:guyton-1972"], "note": "Decision D-2. Context: He 2013 trials measured BP after ≥ 4 weeks; Guyton 1972 long-term autoregulation acts over days to weeks. map_auto_tau_h was chosen for this shape (calibration, not independent validation). Test: tests/engine.test.mjs \"chronic high salt: MAP still rising at day 14\".", "role": "calibration", "calibrates": ["map_auto_tau_h"]},
            {"id": "chronic_high_salt_30d/03", "metric": "Na excretion ≈ intake by day 30", "target": "within 2%", "range": [0.98, 1.02], "kind": "quantitative", "evidence": ["ev:guyton-1972"], "note": "Steady-state balance is a structural property of the renal-body-fluid feedback.", "role": "structural"},
            {"id": "chronic_high_salt_30d/04", "metric": "ECF volume change", "target": "model expands ECF (Guyton). Heer 2000 found plasma volume +315 mL but NO total body water gain at very high intake — known divergence (no Na storage compartment in V1).", "kind": "known-divergence", "evidence": ["ev:heer-2000", "ev:wiig-2018"]},
        ],
        "evidence": ["ev:he-2013", "ev:guyton-1972", "ev:hall-2016", "ev:heer-2000", "ev:wiig-2018", "ev:rakova-2017"],
    },
)

no_water_24h = make_scenario(
    id="no_water_24h", title="No water for 24 h",
    description="Drinking water removed for 24 h (t = 1 to 25 h); food Na/K continue; metabolic water continues.",
    t_end=30, overrides=[{"start": 1, "end": 25, "waterIn_Lh": 0}],
    validation={
        "summary": "Plasma osmolality and Na rise, ADH rises, urine concentrates toward its maximum, urine flow falls, thirst rises; ECF and ICF both shrink.",
        "expects": [
            {"id": "no_water_24h/01", "metric": "osmolality rise", "target": "rise of a few mOsm/kg (qualitative; full-text values not verified)", "range": [2, 15], "kind": "semi-quantitative", "evidence": ["ev:phillips-1984"]},
            {"id": "no_water_24h/02", "metric": "ADH at 24 h > baseline", "target": "rise", "kind": "qualitative", "evidence": ["ev:phillips-1984", "ev:robertson-athar-1976"]},
            {"id": "no_water_24h/03", "metric": "U_osm at 24 h", "target": "approaches U_osm_max", "kind": "qualitative", "evidence": ["ev:guyton-hall-2021"]},
            {"id": "no_water_24h/04", "metric": "Thirst at 24 h > baseline", "target": "rise", "kind": "qualitative", "evidence": ["ev:phillips-1984", "ev:hughes-2018"]},
        ],
        "evidence": ["ev:phillips-1984", "ev:robertson-athar-1976", "ev:hughes-2018", "ev:guyton-hall-2021"],
    },
)

#: Every shipped scenario, in the JavaScript key order (read-only).
SCENARIOS: Mapping[str, Scenario] = MappingProxyType({
    "baseline": baseline,
    "drink_water_1L": drink_water_1L,
    "drink_water_3L_fast": drink_water_3L_fast,
    "salt_load_10g": salt_load_10g,
    "salt_load_sweep": salt_load_sweep,
    "chronic_high_salt_30d": chronic_high_salt_30d,
    "no_water_24h": no_water_24h,
})
