// engine/index.js — public API (spec §3).
// EDUCATIONAL MODEL — NOT MEDICAL ADVICE.

import {
  STATE_KEYS, DERIVED_KEYS, LEDGER_KEYS, IDX, defaultParams, paramTable, initialState,
  rhs, derived, constants, fluxes, baselineInputs, REFERENCE_PERSON, GFR_NORM_BSA_M2,
} from './model.js';
import { integrate } from './solver.js';
import { SCENARIOS, makeScenario, makeWaterLoad, makeSaltLoad, MMOL_NA_PER_G_NACL } from './scenarios.js';
import { mulberry32, sampleParams, samplingMode, quantileBands } from './mc.js';

export {
  STATE_KEYS, DERIVED_KEYS, LEDGER_KEYS, IDX, defaultParams, paramTable, initialState, rhs, derived,
  constants, fluxes, baselineInputs, SCENARIOS, makeScenario, makeWaterLoad, makeSaltLoad,
  MMOL_NA_PER_G_NACL, mulberry32, sampleParams, samplingMode, REFERENCE_PERSON, GFR_NORM_BSA_M2,
};

export const MODEL_VERSION = '1.1.0';
export const DISCLAIMER = 'Educational model — not medical advice.';
/** HREQ-S-01: carried beside DISCLAIMER on every result (since 1.1.0; BUG-20261003-104). */
export const VALIDATION_STATUS = 'Not clinically validated.';

/**
 * The `meta` every public result carries: disclaimer, validation status, model version, then
 * `extra` (the same keys, in the same order, as result_meta() in src/health/engine/api.py).
 */
export function resultMeta(extra = {}) {
  return { disclaimer: DISCLAIMER, validation_status: VALIDATION_STATUS, modelVersion: MODEL_VERSION, ...extra };
}

function resolveScenario(s) {
  if (!s) return SCENARIOS.baseline;
  if (typeof s === 'string') {
    const sc = SCENARIOS[s];
    if (!sc) throw new Error(`unknown scenario "${s}". Known: ${Object.keys(SCENARIOS).join(', ')}`);
    return sc;
  }
  return s;
}

/**
 * Run one deterministic simulation.
 * @param {object} o
 * @param {object} [o.params]     parameter values (default: defaultParams())
 * @param {string|object} [o.scenario] scenario id or object (default 'baseline')
 * @param {number} [o.tEnd]       h (default scenario.tEnd)
 * @param {number} [o.dt]         output grid h (default scenario.dt, else 1/60)
 * @param {number} [o.outEvery]   record every k-th grid point (default scenario.outEvery)
 * @param {Float64Array} [o.y0]   initial state (default initialState(params))
 * @returns {{ t: Float64Array, Y: Float64Array[], states: Object<string,Float64Array>,
 *            derived: Object<string,Float64Array>, ledger: Object<string,Float64Array>,
 *            params: object, scenario: string, meta: object }}
 *   Y[i][k] is state STATE_KEYS[i] at time t[k]; states[key] is the same array by name.
 */
export function simulate(o = {}) {
  const sc = resolveScenario(o.scenario);
  const p = { ...(o.params || defaultParams()) };
  const tEnd = o.tEnd ?? sc.tEnd;
  const dt = o.dt ?? sc.dt ?? 1 / 60;
  const outEvery = o.outEvery ?? sc.outEvery ?? 1;
  const y0 = o.y0 ? Float64Array.from(o.y0) : initialState(p);
  const C = constants(p);
  const n = STATE_KEYS.length, nl = LEDGER_KEYS.length;
  const z0 = new Float64Array(n + nl);
  z0.set(y0);
  const led = new Float64Array(nl);
  const deriv = (t, z, inputs, dz) => {
    rhs(t, z, p, inputs, dz, led, C);           // writes dz[0..n-1]
    for (let i = 0; i < nl; i++) dz[n + i] = led[i];
  };
  const maxStep = Math.min(dt, 0.25 * C.tau_min);
  const res = integrate({
    deriv, z0, t0: 0, tEnd, dt, outEvery, breakpoints: sc.breakpoints,
    inputsAt: (t) => sc.inputs(t, p), maxStep,
  });
  const T = res.t.length;
  const Y = STATE_KEYS.map(() => new Float64Array(T));
  const ledger = Object.fromEntries(LEDGER_KEYS.map((k) => [k, new Float64Array(T)]));
  const der = Object.fromEntries(DERIVED_KEYS.map((k) => [k, new Float64Array(T)]));
  for (let k = 0; k < T; k++) {
    const z = res.Z[k];
    for (let i = 0; i < n; i++) Y[i][k] = z[i];
    for (let i = 0; i < nl; i++) ledger[LEDGER_KEYS[i]][k] = z[n + i];
    const d = derived(z, p, C);
    for (const key of DERIVED_KEYS) der[key][k] = d[key];
  }
  const states = Object.fromEntries(STATE_KEYS.map((k, i) => [k, Y[i]]));
  return {
    t: res.t, Y, states, derived: der, ledger, params: p, scenario: sc.id,
    meta: { steps: res.steps, maxStep, dt, outEvery, label: sc.label || null, disclaimer: DISCLAIMER,
      validation_status: VALIDATION_STATUS, modelVersion: MODEL_VERSION },
  };
}

/** Draw `n` accepted parameter samples (see mc.js for the rejection rule). */
export function drawSamples({ n = 64, seed = 1, table = paramTable() } = {}) {
  const rng = mulberry32(seed);
  const samples = [];
  let rejected = 0;
  const maxTries = 50 * n;
  let tries = 0;
  for (; samples.length < n; tries++) {
    if (tries >= maxTries) throw new Error(`drawSamples: only ${samples.length}/${n} feasible samples after ${tries} draws`);
    const p = sampleParams(table, rng);
    const C = constants(p);
    if (!C.feasible || !(C.Na_ss >= p.na_normal_low && C.Na_ss <= p.na_normal_high)) { rejected++; continue; }
    samples.push(p);
  }
  return { samples, rejected, meta: resultMeta({ n, seed, draws: tries }) };
}

/**
 * Monte Carlo over parameter ranges (spec §3 simulateMC).
 * @returns {{ t, keys, q05, q50, q95, dq05, dq50, dq95, samples, rejected, n, seed }}
 *   q**[key]  : quantiles of the absolute value at each time point
 *   dq**[key] : quantiles of the change from each sample's own t=0 value
 *   samples   : the accepted parameter sets (same order as runs)
 */
export function simulateMC({ scenario, tEnd, n = 64, seed = 1, dt, outEvery, keys } = {}) {
  const { samples, rejected } = drawSamples({ n, seed });
  const K = keys || [...STATE_KEYS, ...DERIVED_KEYS];
  const series = Object.fromEntries(K.map((k) => [k, []]));
  const dseries = Object.fromEntries(K.map((k) => [k, []]));
  let t = null;
  for (const p of samples) {
    const r = simulate({ params: p, scenario, tEnd, dt, outEvery });
    t = r.t;
    for (const k of K) {
      const arr = r.states[k] || r.derived[k];
      if (!arr) throw new Error(`simulateMC: unknown key ${k}`);
      series[k].push(arr);
      const d = new Float64Array(arr.length);
      for (let i = 0; i < arr.length; i++) d[i] = arr[i] - arr[0];
      dseries[k].push(d);
    }
  }
  const q05 = {}, q50 = {}, q95 = {}, dq05 = {}, dq50 = {}, dq95 = {};
  for (const k of K) {
    [q05[k], q50[k], q95[k]] = quantileBands(series[k], [0.05, 0.5, 0.95]);
    [dq05[k], dq50[k], dq95[k]] = quantileBands(dseries[k], [0.05, 0.5, 0.95]);
  }
  const scId = resolveScenario(scenario).id;
  return { t, keys: K, q05, q50, q95, dq05, dq50, dq95, samples, rejected, n, seed,
    scenario: scId, disclaimer: DISCLAIMER, validation_status: VALIDATION_STATUS,
    meta: resultMeta({ n, seed, scenario: scId, dt: dt ?? null, rejected }) };
}

/** Summary metrics of one salt-load run (used by the dose sweep). */
export function saltLoadMetrics(r) {
  const d = r.derived, s = r.states, t = r.t;
  const na0 = d.Na_plasma[0], v0 = s.V_ecf[0], map0 = s.MAP[0];
  let peakS = 0, peakdNa = -Infinity, peakdV = -Infinity, peakMAP = -Infinity;
  for (let k = 0; k < t.length; k++) {
    peakS = Math.max(peakS, d.strain_index[k]);
    peakdNa = Math.max(peakdNa, d.Na_plasma[k] - na0);
    peakdV = Math.max(peakdV, s.V_ecf[k] - v0);
    peakMAP = Math.max(peakMAP, s.MAP[k] - map0);
  }
  const k24 = nearestIndex(t, 25); // 24 h after the load at t = 1 h
  const base = r.params.naIn_base_mmold / 24;
  const na_excr_24h = r.ledger.na_out[k24] - r.ledger.na_out[nearestIndex(t, 1)] - 24 * base;
  return { peak_strain_index: peakS, peak_dNa: peakdNa, peak_dV_ecf: peakdV, na_excr_24h, peak_MAP: peakMAP,
    meta: resultMeta({ scenario: r.scenario ?? null }) };
}

/**
 * Salt dose sweep with Monte Carlo bands (common random numbers: the same
 * parameter samples are used at every dose, so the curve shape is not noise).
 * @returns {{ values, unit, metrics: { [metric]: { q05: number[], q50: number[], q95: number[] } }, rejected }}
 */
export function simulateSweep({ n = 64, seed = 1, values, dt = 1 / 30 } = {}) {
  const sw = SCENARIOS.salt_load_sweep.sweep;
  const vals = values || [...sw.values];
  const { samples, rejected } = drawSamples({ n, seed });
  const metrics = Object.fromEntries(sw.metrics.map((m) => [m, { q05: [], q50: [], q95: [] }]));
  for (const g of vals) {
    const sc = sw.make(g);
    const per = Object.fromEntries(sw.metrics.map((m) => [m, []]));
    for (const p of samples) {
      const m = saltLoadMetrics(simulate({ params: p, scenario: sc, dt }));
      for (const key of sw.metrics) per[key].push(m[key]);
    }
    for (const key of sw.metrics) {
      const [a, b, c] = quantileBands(per[key].map((x) => Float64Array.of(x)), [0.05, 0.5, 0.95]);
      metrics[key].q05.push(a[0]); metrics[key].q50.push(b[0]); metrics[key].q95.push(c[0]);
    }
  }
  return { values: vals, unit: sw.unit, metrics, rejected, n, seed, disclaimer: DISCLAIMER,
    validation_status: VALIDATION_STATUS, meta: resultMeta({ n, seed, dt, rejected }) };
}

export function nearestIndex(t, x) {
  let best = 0;
  for (let k = 1; k < t.length; k++) if (Math.abs(t[k] - x) < Math.abs(t[best] - x)) best = k;
  return best;
}

// ---------------------------------------------------------------------------
// Which parameters influence each quantity (audit F-01 / BUG-0049).
// One-at-a-time sensitivity screen instead of hand-written lists:
//   * reference run: default parameters, scenario salt_load_10g, 24 h, dt = 1/20 h;
//   * every parameter in the table (all of them, including the ones held fixed in Monte Carlo,
//     because e.g. the strain-index weights are E-assumptions that define that curve)
//     is raised by +10 % and the run repeated from its own steady state;
//   * effect(param, key) = max_t |x_perturbed(t) − x_default(t)| / scale(key), where
//     scale = peak-to-peak range of the default trajectory of that key over the run
//     (floored at 1e-6·max|x| so a flat quantity cannot divide by zero);
//   * a parameter "feeds" a quantity when its effect exceeds INFLUENCE_DEFAULTS.threshold (1 %).
// A perturbation that makes the steady state infeasible counts as influencing every key
// (effect = Infinity), so the screen can only over-count, never under-count; so does a NaN
// anywhere in a perturbed run's difference (BUG-20261003-116: before 1.1.0 only a NaN in
// the trailing run of a series survived the running maximum).
// Result is cached; one reference run plus one run per parameter row (meta.nParams).
export const INFLUENCE_DEFAULTS = Object.freeze({ scenario: 'salt_load_10g', tEnd: 24, dt: 1 / 20, rel: 0.10, threshold: 0.01 });
let influenceCache = null;

/**
 * One scenario, one direction of the screen: the reference run `ref` (keyed series), then every
 * parameter of `p0` scaled by (1 + rel) and the run repeated from its own steady state.
 * Shared by computeInfluence and computeInfluenceAll so both apply the same effect and
 * threshold rule. Returns { effects: {key: {param: effect}}, params: {key: [ids, strongest
 * first]}, infeasible: [ids] }.
 */
function screenOne(p0, ref, scale, o, rel) {
  const keyed = (r) => ({ ...r.states, ...r.derived, ...r.ledger });
  const keys = Object.keys(ref);
  const effects = Object.fromEntries(keys.map((k) => [k, {}]));
  const infeasible = [];
  for (const name of Object.keys(p0)) {
    const p = { ...p0, [name]: p0[name] * (1 + rel) };
    let run = null;
    try { run = keyed(simulate({ params: p, scenario: o.scenario, tEnd: o.tEnd, dt: o.dt })); } catch { infeasible.push(name); }
    for (const k of keys) {
      if (!run) { effects[k][name] = Infinity; continue; }
      const a = ref[k], b = run[k];
      let m = 0, nan = false;
      for (let i = 0; i < a.length; i++) { const d = Math.abs(b[i] - a[i]); if (d !== d) nan = true; else if (d > m) m = d; }
      effects[k][name] = !nan && Number.isFinite(m) ? m / scale[k] : Infinity; // any NaN -> counted
    }
  }
  const params = {};
  for (const k of keys) {
    params[k] = Object.entries(effects[k]).filter(([, e]) => e > o.threshold).sort((x, y) => y[1] - x[1]).map(([n]) => n);
  }
  return { effects, params, infeasible };
}

/** The reference run of a screen (keyed series) and each key's response scale. */
function screenReference(p0, o) {
  const r = simulate({ params: p0, scenario: o.scenario, tEnd: o.tEnd, dt: o.dt });
  const ref = { ...r.states, ...r.derived, ...r.ledger };
  const scale = {};
  for (const k of Object.keys(ref)) {
    let lo = Infinity, hi = -Infinity, big = 0;
    for (const v of ref[k]) { if (v < lo) lo = v; if (v > hi) hi = v; big = Math.max(big, Math.abs(v)); }
    scale[k] = Math.max(hi - lo, 1e-6 * big, 1e-12);
  }
  return { ref, scale };
}

/** Run the sensitivity screen. Returns { meta, effects: {key: {param: effect}}, params: {key: [param ids, strongest first]} }. */
export function computeInfluence(opts = {}) {
  const o = { ...INFLUENCE_DEFAULTS, ...opts };
  const p0 = defaultParams();
  const { ref, scale } = screenReference(p0, o);
  const { effects, params, infeasible } = screenOne(p0, ref, scale, o, o.rel);
  return { meta: { ...o, infeasible, nParams: Object.keys(p0).length, modelVersion: MODEL_VERSION,
    disclaimer: DISCLAIMER, validation_status: VALIDATION_STATUS }, effects, params };
}

// ---------------------------------------------------------------------------
// HREQ-U-12 (BUG-20261003-096, W-15): the screen per registered scenario, at that scenario's
// own horizon (its tEnd and dt), in both directions, and the union. The default screen above
// is one 24 h window of one scenario in one direction, and it misses parameters whose effect
// grows over a longer horizon (pn_gain on MAP over 30 days) or whose term is clamped to zero
// in that direction (thirst_vol_gain on Thirst: max(0, 1 - vr) is zero while salt expands
// the ECF). Same effect definition, same threshold and same +/-rel as the default screen.
//   runs[scenario][direction] = { effects, params, infeasible }  (direction 'up' = +rel,
//                                 'down' = -rel)
//   feeders[scenario][key]    = parameters feeding key in that scenario in either direction,
//                               strongest first (by the larger of the two effects)
//   union[key]                = parameters feeding key in ANY scenario or direction,
//                               strongest first (by the largest effect anywhere)
// Ties keep parameter-table order (both sorts are stable). Cost: per scenario one reference
// run plus two runs per parameter row; meta.runs counts them.
export const INFLUENCE_DIRECTIONS = Object.freeze({ up: 1, down: -1 });

/** Merge per-direction (or per-scenario) effect maps into a feeder list: a parameter is listed
 *  when its effect exceeds `threshold` in any map, ordered by its largest effect. */
function feedersOf(effectMaps, key, threshold) {
  const best = new Map();
  for (const eff of effectMaps) {
    for (const [name, e] of Object.entries(eff[key])) {
      if (!(e > threshold)) continue;
      if (!best.has(name) || e > best.get(name)) best.set(name, e);
    }
  }
  return [...best.entries()].sort((x, y) => y[1] - x[1]).map(([n]) => n);
}

/**
 * Influence union over every registered scenario (HREQ-U-12).
 * @param {object} [opts]
 * @param {string[]} [opts.scenarios] scenario ids (default: every registered scenario)
 * @param {number} [opts.rel]        relative perturbation (default INFLUENCE_DEFAULTS.rel)
 * @param {number} [opts.threshold]  feeder threshold (default INFLUENCE_DEFAULTS.threshold)
 * @returns {{ meta, runs, feeders, union }}
 */
export function computeInfluenceAll(opts = {}) {
  const rel = opts.rel ?? INFLUENCE_DEFAULTS.rel;
  const threshold = opts.threshold ?? INFLUENCE_DEFAULTS.threshold;
  const ids = opts.scenarios ? [...opts.scenarios] : Object.keys(SCENARIOS);
  const p0 = defaultParams();
  const runs = {}, feeders = {}, horizons = {}, infeasible = {};
  let nRuns = 0;
  let keys = null;
  for (const id of ids) {
    const sc = resolveScenario(id);
    const o = { scenario: sc.id, tEnd: sc.tEnd, dt: sc.dt ?? 1 / 60, threshold };
    const { ref, scale } = screenReference(p0, o);
    nRuns += 1;
    keys = keys || Object.keys(ref);
    runs[sc.id] = {};
    infeasible[sc.id] = {};
    for (const [dir, sign] of Object.entries(INFLUENCE_DIRECTIONS)) {
      const r = screenOne(p0, ref, scale, o, sign * rel);
      nRuns += Object.keys(p0).length;
      runs[sc.id][dir] = r;
      infeasible[sc.id][dir] = r.infeasible;
    }
    horizons[sc.id] = { tEnd: o.tEnd, dt: o.dt };
    const maps = Object.values(runs[sc.id]).map((r) => r.effects);
    feeders[sc.id] = Object.fromEntries(keys.map((k) => [k, feedersOf(maps, k, threshold)]));
  }
  const all = Object.values(runs).flatMap((byDir) => Object.values(byDir).map((r) => r.effects));
  const union = Object.fromEntries((keys || []).map((k) => [k, feedersOf(all, k, threshold)]));
  return {
    meta: resultMeta({ rel, threshold, directions: { up: rel, down: -rel }, scenarios: ids.map((s) => resolveScenario(s).id),
      horizons, runs: nRuns, nParams: Object.keys(p0).length, infeasible }),
    runs, feeders, union,
  };
}

/** The union of a computeInfluenceAll result, recomputed from its per-scenario feeder lists
 *  (a parameter feeds a key when it feeds it in any scenario); the order is not implied. */
export function influenceUnion(all) {
  const out = {};
  for (const byKey of Object.values(all.feeders)) {
    for (const [k, list] of Object.entries(byKey)) {
      out[k] = out[k] || new Set();
      for (const n of list) out[k].add(n);
    }
  }
  return Object.fromEntries(Object.entries(out).map(([k, s]) => [k, [...s].sort()]));
}

/** Install a precomputed screen (e.g. from a Web Worker) so paramsFor() does not recompute.
 *  Accepts a computeInfluence result or a computeInfluenceAll result (then paramsFor reads its
 *  union, HREQ-U-12). */
export function primeInfluence(result) {
  const lists = result && (result.union || result.params);
  if (!lists || typeof lists !== 'object') throw new Error('primeInfluence: bad result');
  influenceCache = result;
}

/** Cached sensitivity screen at INFLUENCE_DEFAULTS. */
export function influence() {
  if (!influenceCache) influenceCache = computeInfluence();
  return influenceCache;
}

/**
 * Parameter ids that move `key` (a state, derived or ledger key, e.g. 'ADH', 'strain_index')
 * by more than 1 % of its response scale in the reference screen, strongest first; in the
 * union of every scenario and direction when a computeInfluenceAll result was primed.
 * Throws on an unknown key.
 */
export function paramsFor(derivedKey) {
  const inf = influence();
  const list = (inf.union || inf.params)[derivedKey];
  if (!list) throw new Error(`paramsFor: unknown quantity "${derivedKey}"`);
  return [...list];
}

/** Parameter-table summary for UI footers: count and share graded ≥ B. */
export function paramSummary() {
  const tab = paramTable();
  const entries = Object.values(tab);
  const good = entries.filter((e) => /^(A-|B-)/.test(e.grade)).length;
  const byGrade = {};
  for (const e of entries) byGrade[e.grade] = (byGrade[e.grade] || 0) + 1;
  return { count: entries.length, atLeastB: good, pctAtLeastB: (100 * good) / entries.length, byGrade,
    meta: resultMeta() };
}
