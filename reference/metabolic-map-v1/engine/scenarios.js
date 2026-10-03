// engine/scenarios.js — named interventions (spec §5).
//
// A scenario is a plain object:
//   { id, title, description, tEnd (h), dt (h), outEvery?, breakpoints: number[],
//     inputs(t, params) -> { waterIn_Lh, naIn_mmolh, kIn_mmolh, sweat_Lh, exercise },
//     validation: { summary, expects: [...], evidence: [...] }, label? }
// Inputs are PIECEWISE CONSTANT; every time at which they jump is listed in
// `breakpoints` so the solver never integrates across a discontinuity.
// Baseline diet (water, Na, K) is continuous and always present unless a scenario
// overrides it (no_water_24h removes drinking water).

import { baselineInputs } from './model.js';

/** mmol Na per gram NaCl (molar mass 58.44 g/mol). Physical constant. */
export const MMOL_NA_PER_G_NACL = 1000 / 58.44;

/**
 * Build a scenario from a list of bolus events on top of the baseline diet.
 * event: { start (h), durMin (minutes), water_L?, salt_g? }
 * override: { start, end, waterIn_Lh?, naIn_mmolh? } replaces baseline rates inside [start, end).
 */
export function makeScenario({ id, title, description, tEnd, dt = 1 / 60, outEvery = 1,
  events = [], overrides = [], validation, label }) {
  const bp = new Set();
  for (const e of events) { bp.add(e.start); bp.add(e.start + e.durMin / 60); }
  for (const o of overrides) { bp.add(o.start); bp.add(o.end); }
  const breakpoints = [...bp].sort((a, b) => a - b);
  function inputs(t, p) {
    const u = baselineInputs(p);
    for (const o of overrides) {
      if (t >= o.start && t < o.end) {
        if (o.waterIn_Lh !== undefined) u.waterIn_Lh = o.waterIn_Lh;
        if (o.naIn_mmolh !== undefined) u.naIn_mmolh = o.naIn_mmolh;
      }
    }
    for (const e of events) {
      const d = e.durMin / 60;
      if (t >= e.start && t < e.start + d) {
        if (e.water_L) u.waterIn_Lh += e.water_L / d;
        if (e.salt_g) u.naIn_mmolh += e.salt_g * MMOL_NA_PER_G_NACL / d;
      }
    }
    return u;
  }
  return Object.freeze({ id, title, description, tEnd, dt, outEvery, breakpoints,
    events: Object.freeze(events.map((e) => Object.freeze({ ...e }))),
    overrides: Object.freeze(overrides.map((o) => Object.freeze({ ...o }))),
    inputs, validation, label: label || null });
}

/** Water bolus: `liters` drunk over `durMin` minutes starting at `at` h. */
export function makeWaterLoad(liters, durMin = 10, at = 1, tEnd = 12) {
  return makeScenario({
    id: `water_${liters}L_${durMin}min`, title: `Drink ${liters} L water over ${durMin} min`,
    description: `Oral water load of ${liters} L over ${durMin} min at t=${at} h on top of baseline diet.`,
    tEnd, events: [{ start: at, durMin, water_L: liters }], validation: null,
  });
}

/** Salt bolus: `grams` NaCl with `waterL` water over `durMin` minutes at `at` h. */
export function makeSaltLoad(grams, waterL = 0.5, durMin = 15, at = 1, tEnd = 72) {
  return makeScenario({
    id: `salt_${grams}g`, title: `Eat ${grams} g salt (+${waterL} L water)`,
    description: `Oral NaCl load ${grams} g (${(grams * MMOL_NA_PER_G_NACL).toFixed(0)} mmol Na) with ` +
      `${waterL} L water over ${durMin} min at t=${at} h, on top of baseline diet.`,
    tEnd, events: [{ start: at, durMin, water_L: waterL, salt_g: grams }], validation: null,
  });
}

const DRINK_START = 1; // h

const baseline = makeScenario({
  id: 'baseline', title: 'Baseline (nothing happens)',
  description: 'Continuous average diet (2.1 L/day water, 150 mmol/day Na, 80 mmol/day K). The model should sit exactly at its steady state.',
  tEnd: 24,
  validation: {
    summary: 'All states remain at the analytic steady state (numerical drift only).',
    expects: [
      { metric: 'max |Δstate| / scale over 24 h', target: '< 1e-6', kind: 'numerical' },
    ],
    evidence: ['ev:guyton-hall-2021'],
  },
});

const drink_water_1L = makeScenario({
  id: 'drink_water_1L', title: 'Drink 1 L of water in 10 min',
  description: '1 L plain water over 10 min at t = 1 h.',
  tEnd: 12, events: [{ start: DRINK_START, durMin: 10, water_L: 1 }],
  validation: {
    summary: 'Plasma Na dips ~2-4 mmol/L, ADH is suppressed, urine dilutes toward ~50-70 mOsm/kg, water diuresis peaks ~1-2 h after drinking and the load is largely excreted within ~3-4 h.',
    expects: [
      { metric: 'Na_plasma nadir − baseline', target: '[-5, -1] mmol/L', range: [-5, -1], kind: 'quantitative', evidence: ['ev:baylis-1986'], note: 'Spec §8. Baylis: sustained water load lowered osmolality ~7 mOsm/kg (≈ −3.5 mmol/L Na).' },
      { metric: 'time to recover |ΔNa| < 0.5 mmol/L after drinking starts', target: '< 6 h', range: [0, 6], kind: 'quantitative', evidence: ['ev:crowe-1987'] },
      { metric: 'time of peak urine flow after drinking starts', target: '~1-2 h (tolerance 0.5-2.5 h)', range: [0.5, 2.5], kind: 'quantitative', evidence: ['ev:crowe-1987', 'ev:shafiee-2005'] },
      { metric: 'peak urine flow', target: '≈0.5-0.8 L/h (Shafiee 11 mL/min for ~1.4 L; Crowe ~0.76 L/h for 20 mL/kg)', range: [0.35, 0.9], kind: 'quantitative', evidence: ['ev:shafiee-2005', 'ev:crowe-1987'], note: 'Literature loads were ~1.4 L, so a 1 L load is expected at or below their peak.' },
      { metric: 'minimum urine osmolality', target: '≈50-100 mOsm/kg', range: [40, 150], kind: 'quantitative', evidence: ['ev:baylis-1986'] },
      { metric: 'fraction of the 1 L excreted (above baseline urine) by 3 h after drinking', target: 'most of it (Crowe: ~100% of 20 mL/kg in 2 h in young water-replete men)', range: [0.5, 1.2], kind: 'quantitative', evidence: ['ev:crowe-1987'] },
    ],
    evidence: ['ev:baylis-1986', 'ev:crowe-1987', 'ev:shafiee-2005', 'ev:peronnet-2012'],
  },
});

const drink_water_3L_fast = makeScenario({
  id: 'drink_water_3L_fast', title: 'Drink 3 L of water in 30 min (hyponatremia risk demo)',
  description: '3 L plain water over 30 min at t = 1 h. Shows where plasma Na crosses the 135 mmol/L hyponatremia boundary.',
  tEnd: 12, events: [{ start: DRINK_START, durMin: 30, water_L: 3 }],
  validation: {
    summary: 'Intake rate (6 L/h) far exceeds maximal free-water excretion (~0.7 L/h), so plasma Na falls several mmol/L and can cross 135 mmol/L (hyponatremia by EAH consensus definition) before the kidney clears the load.',
    boundary: { key: 'Na_plasma', value: 135, direction: 'below', evidence: ['ev:hew-butler-2015'] },
    expects: [
      { metric: 'Na_plasma nadir − baseline', target: 'larger fall than 1 L (≈3x)', kind: 'qualitative', evidence: ['ev:shafiee-2005'] },
      { metric: 'Na_plasma < 135 at any time', target: 'demonstrated in median trajectory (model behaviour, not a validated human threshold)', kind: 'qualitative', evidence: ['ev:hew-butler-2015'] },
    ],
    evidence: ['ev:hew-butler-2015', 'ev:shafiee-2005', 'ev:crowe-1987'],
  },
});

const SALT_10G = makeSaltLoad(10, 0.5, 15, DRINK_START, 72);
const salt_load_10g = makeScenario({
  id: 'salt_load_10g', title: 'Eat 10 g salt (171 mmol Na) with 0.5 L water',
  description: SALT_10G.description,
  tEnd: 72, events: [...SALT_10G.events],
  validation: {
    summary: 'Plasma Na rises ~1-3 mmol/L, ADH and thirst rise, urine concentrates, ECF expands by water drawn from cells, and the extra Na is excreted only gradually over ~24-48 h.',
    expects: [
      { metric: 'Na_plasma peak − baseline', target: '[+0.5, +4] mmol/L', range: [0.5, 4], kind: 'quantitative', evidence: ['ev:suckling-2012', 'ev:andersen-1999'], note: 'Spec §8. Suckling 2012: +3.13 ± 0.75 after 6 g in soup vs matched unsalted soup (SEM vs SD unverified; context only, not the source of this band); Andersen 1999: +2.7 after IV hypertonic load ≈10% ECF Na.' },
      { metric: 'ADH peak > baseline', target: 'rise', kind: 'qualitative', evidence: ['ev:andersen-2002'], note: 'Andersen 2002: AVP 1.4 -> 3.1 pg/mL after hypertonic NaCl.' },
      { metric: 'fraction of the 171 mmol load excreted (above baseline) by 8 h', target: '≈25% (Andersen 1999 IV load: 131 vs 81 mmol in 8 h)', range: [0.1, 0.6], kind: 'quantitative', evidence: ['ev:andersen-1999'], note: 'IV not oral; supine; time-control also rose. Loose band.' },
      { metric: 'fraction excreted by 48 h', target: 'majority (delayed natriuresis over 24-48 h)', range: [0.5, 1.05], kind: 'quantitative', evidence: ['ev:andersen-1999'], note: 'Spec §5 expectation; the multi-day rhythmicity reported by the Titze group is not modelled.' },
      { metric: 'urine osmolality rises', target: 'rise (free-water conservation)', kind: 'qualitative', evidence: ['ev:rakova-2017'] },
    ],
    evidence: ['ev:suckling-2012', 'ev:andersen-1998', 'ev:andersen-1999', 'ev:andersen-2002', 'ev:rakova-2017'],
  },
});

const SWEEP_G = [0, 2.5, 5, 7.5, 10, 12.5, 15, 17.5, 20, 22.5, 25, 27.5, 30];
const salt_load_sweep = Object.freeze({
  ...makeScenario({
    id: 'salt_load_sweep', title: 'Salt dose sweep 0-30 g (how much salt strains the kidney?)',
    description: 'Single oral NaCl loads from 0 to 30 g (each with 0.5 L water) at t = 1 h; 48 h per dose. The default inputs() is the 10 g member so the object is also directly simulatable.',
    tEnd: 48, events: [{ start: DRINK_START, durMin: 15, water_L: 0.5, salt_g: 10 }],
    validation: {
      summary: 'Peak kidney strain index, peak Δ[Na], peak ECF expansion and 24 h Na excretion should increase monotonically with dose. The strain index is a model index, not a clinical measure.',
      expects: [
        { metric: 'peak strain_index vs dose', target: 'monotonic increase', kind: 'qualitative', evidence: ['ev:brezis-rosen-1995', 'ev:brenner-1982'] },
        { metric: 'Context, not checked on this chart: 6 g salted minus matched-water control', target: '+3.13 ± 0.75 mmol/L (Suckling 2012; SEM/SD unverified). This chart shows the rise from each person\'s own baseline, a different quantity; status: unverified, not counted as a pass', kind: 'unverified', evidence: ['ev:suckling-2012'], note: 'Audit F-02/F-03 (BUG-0050, BUG-0051). The salted-minus-control comparison is a skipped test in tests/engine.test.mjs until the dispersion is confirmed from the full text.' },
      ],
      evidence: ['ev:suckling-2012', 'ev:brezis-rosen-1995', 'ev:brenner-1982'],
    },
  }),
  sweep: Object.freeze({
    variable: 'salt_g', unit: 'g NaCl', values: Object.freeze(SWEEP_G),
    make: (g) => makeSaltLoad(g, 0.5, 15, DRINK_START, 48),
    metrics: Object.freeze(['peak_strain_index', 'peak_dNa', 'peak_dV_ecf', 'na_excr_24h', 'peak_MAP']),
  }),
});

const CHRONIC_SALT_G = 15; // g/day
const chronic_high_salt_30d = makeScenario({
  id: 'chronic_high_salt_30d', title: `Chronic high salt: ${CHRONIC_SALT_G} g/day for 30 days`,
  description: `Diet salt raised from 8.8 g/day (150 mmol) to ${CHRONIC_SALT_G} g/day (${(CHRONIC_SALT_G * 1000 / 58.44).toFixed(0)} mmol) at t = 24 h and held for 30 days. MODELED RISK TRAJECTORY, NOT A PREDICTION.`,
  tEnd: 24 * 31, dt: 0.25,
  overrides: [{ start: 24, end: 24 * 31, naIn_mmolh: CHRONIC_SALT_G * MMOL_NA_PER_G_NACL / 24 }],
  label: 'modeled risk trajectory — not a prediction',
  validation: {
    summary: 'Na balance re-establishes within days at a slightly expanded ECF; MAP rises by a few mmHg over 2–4 weeks as slow whole-body autoregulation (state R_auto, decision D-2) adds to the pressure-natriuresis loop (Guyton/Hall), then plateaus.',
    expects: [
      { metric: 'ΔMAP at day 30 per +100 mmol/day Na', target: 'normotensive meta-analysis ≈2 mmHg (95% CI-derived band 0.7-3.2)', range: [0.7, 3.2], kind: 'quantitative', evidence: ['ev:he-2013'], note: 'MAP band derived from He 2013 normotensive SBP/DBP CIs (MAP ≈ DBP + (SBP−DBP)/3), scaled from 75 to 100 mmol/day. Scaling uses the normotensive subgroup\'s own urinary Na change, −75 mmol/24 h (BMJ full text, Results, read 2026-10-01; equal to the all-trial value, so the band is unchanged). Still approximate: combining SBP and DBP CI end-points is not a true MAP CI (audit F-06, BUG-0054). Parameters map_vol_exp, pn_gain, aldo_vol_exp were chosen with this target in mind (calibration, not independent validation).' },
      { metric: 'MAP time course', target: 'still rising at day 14; ≥ 95 % of plateau by day 30; < 60 % of plateau by day 3', kind: 'design-target', evidence: ['ev:he-2013', 'ev:guyton-1972'], note: 'Decision D-2. Context: He 2013 trials measured BP after ≥ 4 weeks; Guyton 1972 long-term autoregulation acts over days to weeks. map_auto_tau_h was chosen for this shape (calibration, not independent validation). Test: tests/engine.test.mjs "chronic high salt: MAP still rising at day 14".' },
      { metric: 'Na excretion ≈ intake by day 30', target: 'within 2%', range: [0.98, 1.02], kind: 'quantitative', evidence: ['ev:guyton-1972'], note: 'Steady-state balance is a structural property of the renal-body-fluid feedback.' },
      { metric: 'ECF volume change', target: 'model expands ECF (Guyton). Heer 2000 found plasma volume +315 mL but NO total body water gain at very high intake — known divergence (no Na storage compartment in V1).', kind: 'known-divergence', evidence: ['ev:heer-2000', 'ev:wiig-2018'] },
    ],
    evidence: ['ev:he-2013', 'ev:guyton-1972', 'ev:hall-2016', 'ev:heer-2000', 'ev:wiig-2018', 'ev:rakova-2017'],
  },
});

const no_water_24h = makeScenario({
  id: 'no_water_24h', title: 'No water for 24 h',
  description: 'Drinking water removed for 24 h (t = 1 to 25 h); food Na/K continue; metabolic water continues.',
  tEnd: 30, overrides: [{ start: 1, end: 25, waterIn_Lh: 0 }],
  validation: {
    summary: 'Plasma osmolality and Na rise, ADH rises, urine concentrates toward its maximum, urine flow falls, thirst rises; ECF and ICF both shrink.',
    expects: [
      { metric: 'osmolality rise', target: 'rise of a few mOsm/kg (qualitative; full-text values not verified)', range: [2, 15], kind: 'semi-quantitative', evidence: ['ev:phillips-1984'] },
      { metric: 'ADH at 24 h > baseline', target: 'rise', kind: 'qualitative', evidence: ['ev:phillips-1984', 'ev:robertson-athar-1976'] },
      { metric: 'U_osm at 24 h', target: 'approaches U_osm_max', kind: 'qualitative', evidence: ['ev:guyton-hall-2021'] },
      { metric: 'Thirst at 24 h > baseline', target: 'rise', kind: 'qualitative', evidence: ['ev:phillips-1984', 'ev:hughes-2018'] },
    ],
    evidence: ['ev:phillips-1984', 'ev:robertson-athar-1976', 'ev:hughes-2018', 'ev:guyton-hall-2021'],
  },
});

export const SCENARIOS = Object.freeze({
  baseline, drink_water_1L, drink_water_3L_fast, salt_load_10g, salt_load_sweep,
  chronic_high_salt_30d, no_water_24h,
});
