#!/usr/bin/env node
// tools/health_golden.mjs — generate tests/fixtures/health/golden_v1.json from the
// JavaScript reference engine (reference/metabolic-map-v1/engine/index.js).
//
// The fixture is the equivalence contract between the reference and the Python port
// (src/health/engine): tests/health_selftest.py recomputes every number below in Python
// and requires agreement to 1e-9 relative (HREQ-P-02). Regenerate it in the same change
// as any model change, together with a MODEL_VERSION bump (HREQ-X-05):
//
//   /opt/node22/bin/node tools/health_golden.mjs            # (re)write the fixture
//   /opt/node22/bin/node tools/health_golden.mjs --check    # exit 1 if the file is stale
//
// Output is deterministic (no timestamps): the same reference and Node give the same
// bytes. Non-finite numbers are written as {"$float": "Infinity" | "-Infinity" | "NaN"},
// because JSON has no literal for them.
//
// Sections (a)-(e) are the ones the port specification requires; (b) also covers every
// other registered scenario, (b2) the stiff parameter corner (HREQ-V-08) and (b3) the
// divergence-localisation checkpoints; (f)-(k) are extra coverage of the same API (scenario
// records, rejection sampling, sweep, sensitivity screen, steady-state constants, parameter
// summary); (l) pins the one failing literature expectation to the reference; (m)
// solverCoverage holds runs that move the potassium and sweat fluxes, thin the output grid
// (outEvery 7 and 2.5), put breakpoints inside output intervals (dt 0.1 h) and pass unsorted
// breakpoints; (n) nonFinite pins the JavaScript NaN/Infinity semantics the port emulates
// (quantile sort, Math.max and Math.pow on NaN); (o) resultLabels holds, for every public
// result object of index.js, the labelling fields it carries (disclaimer, validation_status,
// meta), taken from the same runs as the sections above (since model 1.1.0,
// BUG-20261003-104); (p) influenceAll is the influence screen per registered scenario at its
// own horizon in both directions and the union (HREQ-U-12, since model 1.1.0), written after
// rejectionMargins. Sections are only ever ADDED: an existing
// section's bytes change only with a MODEL_VERSION bump. Every Monte
// Carlo section first checks that no draw sits within 1e-6 of a rejection boundary
// (HREQ-V-11) and records the smallest margin it saw (rejectionMargins). The header
// carries the SHA-256 of every reference engine file the numbers came from.

import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, readdirSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const ENGINE_DIR = resolve(ROOT, 'reference/metabolic-map-v1/engine');
const ENGINE = resolve(ENGINE_DIR, 'index.js');
// HEALTH_GOLDEN_OUT overrides the fixture path (the self-test uses it to check a planted copy).
const OUT = process.env.HEALTH_GOLDEN_OUT ? resolve(process.env.HEALTH_GOLDEN_OUT)
  : resolve(ROOT, 'tests/fixtures/health/golden_v1.json');
// HEALTH_GOLDEN_REUSE_INFLUENCE_ALL names a fixture whose influenceAll section (and its
// resultLabels entry) is taken verbatim instead of recomputing its 805 runs (about 25 s). A
// test seam for the self-test of the --check logic on a planted copy, which would otherwise
// run the generator twice and leave the default suite over its time budget (HREQ-N-04).
// Accepted only with --check AND HEALTH_GOLDEN_OUT (a planted copy), and announced on stderr:
// the gate and CI set neither variable, so they always recompute every section.
const REUSE_INFLUENCE_ALL = process.env.HEALTH_GOLDEN_REUSE_INFLUENCE_ALL || '';
if (REUSE_INFLUENCE_ALL && !(process.argv.includes('--check') && process.env.HEALTH_GOLDEN_OUT)) {
  console.error('health_golden: HEALTH_GOLDEN_REUSE_INFLUENCE_ALL is a test seam: it needs --check '
    + 'and HEALTH_GOLDEN_OUT (a planted copy); refusing to write or check the real fixture with it');
  process.exit(2);
}
// Size budget: 400 KB until model 1.1.0, raised to 448 KB when the three strain-index
// constants (BUG-20261003-095) added 3 x 40 effects to computeInfluence and 3 keys to every
// drawn sample, and resultLabels was added (the 1.0.1 fixture was 403,140 bytes); raised
// again to 720 KB in the same model version for the influenceAll section (HREQ-U-12,
// BUG-20261003-096: about 262 KB of feeder lists and effects; the fixture went from 413,597 to
// 675,873 bytes, 8.3 % headroom).
const MAX_BYTES = 720 * 1024;

const E = await import(pathToFileURL(ENGINE).href);
const S = await import(pathToFileURL(resolve(ENGINE_DIR, 'solver.js')).href);
const MC = await import(pathToFileURL(resolve(ENGINE_DIR, 'mc.js')).href);

// SHA-256 of every reference engine source the fixture was computed from (every .js file
// and params.json); tests/health_selftest.py compares them with the files on disk.
function engineHashes() {
  const files = readdirSync(ENGINE_DIR).filter((f) => f.endsWith('.js') || f === 'params.json').sort();
  return Object.fromEntries(files.map((f) => [
    `reference/metabolic-map-v1/engine/${f}`,
    createHash('sha256').update(readFileSync(resolve(ENGINE_DIR, f))).digest('hex'),
  ]));
}

const arr = (a) => Array.from(a);
// (o) resultLabels: filled by the sections below from the result objects they compute.
const LABELS = {};
const labelsOf = (r) => Object.fromEntries(['disclaimer', 'validation_status', 'meta']
  .filter((k) => k in r).map((k) => [k, r[k]]));
const withoutMeta = (r) => Object.fromEntries(Object.entries(r).filter(([k]) => k !== 'meta'));
const pick = (a, idx) => idx.map((k) => a[k]);
const uniqSorted = (xs) => [...new Set(xs)].sort((a, b) => a - b);

// (a) mulberry32: first 5 outputs for two seeds.
function goldenMulberry() {
  const out = {};
  for (const seed of [1, 42]) {
    const r = E.mulberry32(seed);
    out[String(seed)] = [r(), r(), r(), r(), r()];
  }
  return out;
}

// (b) default-parameter trajectories of EVERY registered scenario (HREQ-V-08). Indices:
// the required [0, 1, 10, 100, last] plus every `stride`-th grid point (hourly for the
// 1/60 h scenarios of the specification; sparser for the long ones, to stay in budget).
const TRAJ = [
  { scenario: 'baseline', dt: 1 / 60, stride: 60 },
  { scenario: 'drink_water_1L', dt: 1 / 60, stride: 60 },
  { scenario: 'salt_load_10g', dt: 1 / 60, stride: 60 },
  { scenario: 'no_water_24h', dt: undefined, stride: 60 }, // its default dt
  { scenario: 'drink_water_3L_fast', dt: undefined, stride: 60 },
  { scenario: 'salt_load_sweep', dt: undefined, stride: 240 },
  // dt 0.25 h, recorded every 3rd grid point (993 points <= 1000; the step count is
  // unchanged at 41,664), sampled every 3 days.
  { scenario: 'chronic_high_salt_30d', dt: undefined, outEvery: 3, stride: 96 },
];
function trajectory(r, tEnd, stride, outEvery) {
  const T = r.t.length;
  const required = [0, 1, 10, 100, T - 1];
  const strided = [];
  for (let k = 0; k < T; k += stride) strided.push(k);
  const indices = uniqSorted([...required, ...strided]);
  const sec = (obj, keys) => Object.fromEntries(keys.map((k) => [k, pick(obj[k], indices)]));
  return {
    dt: r.meta.dt, tEnd, outEvery: outEvery ?? null, length: T, steps: r.meta.steps,
    maxStep: r.meta.maxStep, requiredIndices: required, indices, t: pick(r.t, indices),
    states: sec(r.states, E.STATE_KEYS), derived: sec(r.derived, E.DERIVED_KEYS),
    ledger: sec(r.ledger, E.LEDGER_KEYS),
  };
}
function goldenTrajectories() {
  const out = {};
  for (const { scenario, dt, stride, outEvery } of TRAJ) {
    const r = E.simulate({ scenario, dt, outEvery });
    if (scenario === 'drink_water_1L') LABELS.simulate = { scenario, dt, ...labelsOf(r) };
    out[scenario] = trajectory(r, E.SCENARIOS[scenario].tEnd, stride, outEvery);
  }
  return out;
}

// (b2) the stiff corner of docs/health/03 §3.5: the five fastest time constants at the
// low ends of their ranges, the slowest at its high end (stiffness ratio 16,000), on the
// 30-day scenario (thinned as above; 101,184 steps) and on drink_water_3L_fast.
const STIFF_CORNER = { osm_eq_tau_h: 0.03, anp_thalf_h: 0.042, thirst_tau_h: 0.1, aldo_tau_h: 0.5,
  map_tau_h: 1, map_auto_tau_h: 480 };
function goldenStiffCorner() {
  const params = { ...E.defaultParams(), ...STIFF_CORNER };
  const runs = {};
  for (const { scenario, outEvery, stride } of [
    { scenario: 'chronic_high_salt_30d', outEvery: 3, stride: 96 },
    { scenario: 'drink_water_3L_fast', outEvery: undefined, stride: 60 },
  ]) {
    runs[scenario] = trajectory(E.simulate({ params, scenario, outEvery }), E.SCENARIOS[scenario].tEnd,
      stride, outEvery);
  }
  return { overrides: STIFF_CORNER, runs };
}

// (b3) divergence-localisation checkpoints (docs/health/03 §4.2): rhs() at the initial
// state with baseline inputs and with drinking inputs, and the augmented state (13 states
// + 6 ledger entries) after ONE RK4 step of 1/60 h from the initial state while drinking.
function goldenCheckpoints() {
  const p = E.defaultParams();
  const C = E.constants(p);
  const n = E.STATE_KEYS.length, nl = E.LEDGER_KEYS.length;
  const y0 = E.initialState(p);
  const sc = E.SCENARIOS.drink_water_1L;
  const tIn = 1.05;
  const drinking = sc.inputs(tIn, p);
  const rhsAt = (inputs) => {
    const led = new Float64Array(nl);
    const dy = E.rhs(0, y0, p, inputs, null, led, C);
    return { dy: arr(dy), ledger: arr(led) };
  };
  const led = new Float64Array(nl);
  const deriv = (t, z, inputs, dz) => { E.rhs(t, z, p, inputs, dz, led, C); for (let i = 0; i < nl; i++) dz[n + i] = led[i]; };
  const z = new Float64Array(n + nl);
  z.set(y0);
  const h = 1 / 60;
  S.rk4Step(deriv, 1, z, h, drinking, S.workspace(n + nl));
  return {
    rhsBaseline: rhsAt(E.baselineInputs(p)),
    rhsDrinking: { scenario: 'drink_water_1L', inputsAt: tIn, inputs: drinking, ...rhsAt(drinking) },
    rk4Step: { scenario: 'drink_water_1L', inputsAt: tIn, t: 1, h, z: arr(z) },
  };
}

// HREQ-V-11: refuse a fixture whose accept/reject decisions sit on a knife edge. A draw
// within 1e-6 (relative) of a rejection boundary could be decided differently by a port
// whose exp/log differ from V8's in the last bit, and the fixture would then test luck.
// Replays the exact draw sequence of drawSamples (same PRNG, same sampler, same order).
const BOUNDARY_MARGIN = 1e-6;
const MARGINS = {};
function assertNoKnifeEdge(label, { n, seed, table = E.paramTable() }) {
  const rng = E.mulberry32(seed);
  let accepted = 0;
  let min = { margin: Infinity, boundary: null, draw: null };
  let draws = 0;
  for (let tries = 0; accepted < n && tries < 50 * n; tries++) {
    const p = E.sampleParams(table, rng);
    const C = E.constants(p);
    const h = (C.U_ss - p.U_osm_min) / (p.U_osm_max - p.U_osm_min);
    // relative margin: |x - b| / max(1, |b|)
    const margins = [
      ['V_ur_ss = 0', C.V_ur_ss, 0], ['h = 0', h, 0], ['h = 1', h, 1],
      ['Na_ss = na_normal_low', C.Na_ss, p.na_normal_low],
      ['Na_ss = na_normal_high', C.Na_ss, p.na_normal_high],
    ].map(([name, x, b]) => [name, Math.abs(x - b) / Math.max(1, Math.abs(b))]);
    for (const [name, m] of margins) if (!(m >= min.margin)) min = { margin: m, boundary: name, draw: tries };
    const edges = margins.filter(([, m]) => !(m > BOUNDARY_MARGIN));
    if (edges.length) {
      console.error(`health_golden: ${label}: draw ${tries} lies within ${BOUNDARY_MARGIN} of the ` +
        `rejection boundary ${edges.map(([e]) => e).join(', ')}; choose another seed`);
      process.exit(1);
    }
    if (C.feasible && C.Na_ss >= p.na_normal_low && C.Na_ss <= p.na_normal_high) accepted++;
    draws = tries + 1;
  }
  MARGINS[label] = { n, seed, draws, minRelativeMargin: min.margin, nearestBoundary: min.boundary,
    atDraw: min.draw, refuseBelow: BOUNDARY_MARGIN };
}

// (c) drawSamples({n: 8, seed: 1}).
function goldenDrawSamples() {
  assertNoKnifeEdge('drawSamples', { n: 8, seed: 1 });
  const d = E.drawSamples({ n: 8, seed: 1 });
  LABELS.drawSamples = { n: 8, seed: 1, ...labelsOf(d) };
  const { samples, rejected } = d;
  return { n: 8, seed: 1, samples, rejected };
}

// (d) simulateMC drink_water_1L, n = 8, seed = 1, three keys, five time indices.
function goldenMC() {
  assertNoKnifeEdge('simulateMC', { n: 8, seed: 1 });
  const keys = ['Na_plasma', 'urine_flow', 'ADH'];
  const r = E.simulateMC({ scenario: 'drink_water_1L', n: 8, seed: 1, keys });
  LABELS.simulateMC = { scenario: 'drink_water_1L', n: 8, seed: 1, keys, ...labelsOf(r) };
  const T = r.t.length;
  const indices = [0, 60, 120, 240, T - 1];
  const band = (q) => Object.fromEntries(keys.map((k) => [k, pick(q[k], indices)]));
  return {
    scenario: 'drink_water_1L', n: 8, seed: 1, keys, length: T, indices, t: pick(r.t, indices),
    q05: band(r.q05), q50: band(r.q50), q95: band(r.q95),
    dq05: band(r.dq05), dq50: band(r.dq50), dq95: band(r.dq95), rejected: r.rejected,
  };
}

// (e) saltLoadMetrics of the 10 g salt load at dt = 1/30 h.
function goldenSaltMetrics() {
  const dt = 1 / 30;
  const m = E.saltLoadMetrics(E.simulate({ scenario: 'salt_load_10g', dt }));
  LABELS.saltLoadMetrics = { scenario: 'salt_load_10g', dt, ...labelsOf(m) };
  return { scenario: 'salt_load_10g', dt, metrics: withoutMeta(m) };
}

// (f) scenario records (the literature-validation contract) and their input schedules.
const PROBE_T = [0, 0.5, 1, 1.05, 1.1, 1.2, 1.25, 1.3, 1.5, 2, 12, 24, 24.5, 25, 26, 100, 743.9, 744];
function scenarioRecord(sc, p, withInputs = true) {
  const rec = {
    id: sc.id, title: sc.title, description: sc.description, tEnd: sc.tEnd, dt: sc.dt,
    outEvery: sc.outEvery, breakpoints: arr(sc.breakpoints),
    events: sc.events.map((e) => ({ ...e })), overrides: sc.overrides.map((o) => ({ ...o })),
    validation: sc.validation, label: sc.label,
  };
  if (withInputs) rec.inputs = PROBE_T.map((t) => [t, sc.inputs(t, p)]);
  return rec;
}
function goldenScenarios() {
  const p = E.defaultParams();
  const out = {};
  for (const [id, sc] of Object.entries(E.SCENARIOS)) {
    out[id] = scenarioRecord(sc, p);
    if (sc.sweep) {
      out[id].sweep = {
        variable: sc.sweep.variable, unit: sc.sweep.unit, values: arr(sc.sweep.values),
        metrics: arr(sc.sweep.metrics),
        made: sc.sweep.values.map((g) => scenarioRecord(sc.sweep.make(g), p, false)),
      };
    }
  }
  const made = [
    E.makeWaterLoad(1), E.makeWaterLoad(0.5, 5, 2, 6), E.makeWaterLoad(1.5, 30),
    E.makeSaltLoad(2.5), E.makeSaltLoad(6, 0.25, 10, 3, 24), E.makeSaltLoad(0),
  ].map((sc) => scenarioRecord(sc, p));
  return { MMOL_NA_PER_G_NACL: E.MMOL_NA_PER_G_NACL, scenarioOrder: Object.keys(E.SCENARIOS), scenarios: out, made };
}

// (g) rejection sampling: a table whose U_osm_max range reaches below the required
// urine osmolality (~573 mOsm/kg), so some draws are infeasible and must be counted.
function goldenRejection() {
  const table = E.paramTable();
  table.U_osm_max.range = [450, 1400];
  table.adh_threshold.range = [270, 290];
  assertNoKnifeEdge('rejection', { n: 8, seed: 7, table });
  const { samples, rejected } = E.drawSamples({ n: 8, seed: 7, table });
  return { n: 8, seed: 7, tableEdits: { U_osm_max: { range: [450, 1400] }, adh_threshold: { range: [270, 290] } },
    samples, rejected };
}

// (h) salt dose sweep, small n.
function goldenSweep() {
  const o = { n: 4, seed: 1, values: [0, 10, 20] };
  assertNoKnifeEdge('simulateSweep', o);
  const r = E.simulateSweep(o);
  LABELS.simulateSweep = { ...o, ...labelsOf(r) };
  return { ...o, dt: 1 / 30, metrics: r.metrics, rejected: r.rejected, unit: r.unit };
}

// (i) sensitivity screen on a reduced, cheap configuration.
function goldenInfluence() {
  const opts = { scenario: 'drink_water_1L', tEnd: 3, dt: 0.1 };
  const r = E.computeInfluence(opts);
  LABELS.computeInfluence = { opts, ...labelsOf(r) };
  return { opts, meta: r.meta, effects: r.effects, params: r.params };
}

// (l) findings: the drink_water_1L expectation "time to recover |ΔNa| < 0.5 mmol/L after
// drinking starts" (range [0, 6] h) measured on the JavaScript reference itself, with the
// harness definition (first output time after the Na nadir at which |ΔNa| < 0.5, minus the
// drinking start), so the Python harness's FAIL is pinned to the reference, not to the port.
// Also its Monte Carlo band and in-range share at n = 8 (what the self-test recomputes) and
// n = 256 (what docs/health/03 §5.1 asks a published status to carry).
function recoveryTime(r, start) {
  const na = r.derived.Na_plasma, t = r.t;
  let kn = 0;
  for (let k = 1; k < na.length; k++) if (na[k] < na[kn]) kn = k;
  if (Math.abs(na[kn] - na[0]) < 0.5) return 0;
  for (let k = kn + 1; k < t.length; k++) if (Math.abs(na[k] - na[0]) < 0.5) return t[k] - start;
  return Infinity;
}
// The plausible readings of the metric (validate.recovery_time_variants mirrors this).
function recoveryVariants(r, start, thr = 0.5) {
  const na = r.derived.Na_plasma, t = r.t;
  const dev = Array.from(na, (v) => Math.abs(v - na[0]));
  let kn = 0;
  for (let k = 1; k < na.length; k++) if (na[k] < na[kn]) kn = k;
  let first = -1;
  for (let k = kn + 1; k < t.length; k++) if (dev[k] < thr) { first = k; break; }
  let lastAbove = -1;
  for (let k = 0; k < t.length; k++) if (dev[k] >= thr) lastAbove = k;
  const stays = lastAbove < 0 ? 0 : (lastAbove + 1 < t.length ? lastAbove + 1 : -1);
  return {
    first_after_nadir_from_start: dev[kn] < thr ? 0 : first < 0 ? Infinity : t[first] - start,
    stays_below_from_start: stays < 0 ? Infinity : stays === 0 ? 0 : t[stays] - start,
    first_after_nadir_from_nadir: dev[kn] < thr ? 0 : first < 0 ? Infinity : t[first] - t[kn],
    dNa_at_start_plus_6h: na[E.nearestIndex(t, start + 6)] - na[0],
  };
}
function bandOf(values, lo, hi) {
  const s = Float64Array.from(values).sort();
  // R type 7, interpolating only between DIFFERENT order statistics (as R does), so a band
  // edge between two never-recovered samples (+Infinity) is Infinity, not Inf - Inf = NaN.
  const q = (x) => { const h = (s.length - 1) * x, a = Math.floor(h), b = Math.ceil(h); return s[a] === s[b] ? s[a] : s[a] + (h - a) * (s[b] - s[a]); };
  return { band: { q05: q(0.05), q50: q(0.5), q95: q(0.95) },
    in_range_share: values.filter((v) => Number.isFinite(v) && v >= lo && v <= hi).length / values.length };
}
function goldenFindings() {
  const id = 'drink_water_1L';
  const sc = E.SCENARIOS[id];
  const start = sc.events[0].start;
  const metric = 'time to recover |ΔNa| < 0.5 mmol/L after drinking starts';
  const exp = sc.validation.expects.find((e) => e.metric === metric);
  const [lo, hi] = exp.range;
  const r = E.simulate({ scenario: id });
  const na = r.derived.Na_plasma;
  const mc = {};
  for (const n of [8, 256]) {
    assertNoKnifeEdge(`findings n=${n}`, { n, seed: 1 });
    const { samples, rejected } = E.drawSamples({ n, seed: 1 });
    const vals = samples.map((p) => recoveryTime(E.simulate({ params: p, scenario: id }), start));
    mc[String(n)] = { n, seed: 1, rejected, ...bandOf(vals, lo, hi) };
  }
  return {
    [`${id}: ${metric}`]: {
      scenario: id, metric, range: exp.range, kind: exp.kind,
      definition: 'first output time after the Na_plasma nadir at which |Na_plasma - Na_plasma[0]| < 0.5, minus the first event start',
      actual: recoveryTime(r, start),
      dNa_6h_after_start: na[E.nearestIndex(r.t, start + 6)] - na[0],
      variants: recoveryVariants(r, start),
      variantsByDt: Object.fromEntries([1 / 30, 0.1].map((dt) => [String(dt),
        recoveryVariants(E.simulate({ scenario: id, dt }), start)])),
      mcMedianTrajectory256: (() => {
        const m = E.simulateMC({ scenario: id, n: 256, seed: 1, keys: ['Na_plasma'] });
        return recoveryVariants({ t: m.t, derived: { Na_plasma: m.q50.Na_plasma } }, start);
      })(),
      mc,
    },
  };
}

// (j) analytic steady state and derived constants at default params; (k) parameter summary.
function goldenConstants() {
  const p = E.defaultParams();
  const C = { ...E.constants(p) };
  delete C._snap;
  const y0 = arr(E.initialState(p));
  return { constants: C, initialState: y0, derived0: E.derived(E.initialState(p), p),
    fluxes0: E.fluxes(E.initialState(p), p, E.baselineInputs(p)) };
}

// (m) solver and port coverage the registered scenarios cannot give (review of 2026-10-03,
// BUG-20261003-132, -133, -135). Every run records the required indices only
// [0, 1, 10, 100, last] plus `extra`, to stay inside the size budget.
//  * baseline with K_icf raised 2 % at t = 0: the potassium flux K_ur and k_excr_gain are
//    exactly inert in every registered scenario (K_icf never leaves K_icf_0);
//  * sweat_potassium: sweat 0.8 L/h over [2, 4) h and K intake x6 over [1, 3) h, the only
//    run in which sweat, sweat Na and K intake are non-zero;
//  * drink_water_1L recorded every 7th and every 2.5th grid point: the final-point rule and
//    JavaScript's Math.round (2.5 -> 3, not 2) of the output thinning;
//  * drink_water_1L and salt_load_10g at dt = 0.1 h: the bolus end (1.1667 h, 1.25 h) falls
//    INSIDE an output interval, so the solver must split it there;
//  * unsorted_breakpoints: a scenario object whose breakpoints array is not sorted.
// A spec scenario is data, so the Python side builds the identical schedule from it:
// inputs(t) = baselineInputs, then for each schedule entry with from <= t < to, `set`
// assigns and `scale` multiplies.
const SPEC_SCENARIOS = {
  sweat_potassium: { id: 'sweat_potassium', tEnd: 12, dt: 1 / 60, outEvery: 1, breakpoints: [1, 2, 3, 4],
    schedule: [{ from: 2, to: 4, set: { sweat_Lh: 0.8 } }, { from: 1, to: 3, scale: { kIn_mmolh: 6 } }] },
  unsorted_breakpoints: { id: 'unsorted_breakpoints', tEnd: 12, dt: 0.1, outEvery: 1,
    breakpoints: [3.05, 1.05, 2.25, 1.55],
    schedule: [{ from: 1.05, to: 1.55, set: { waterIn_Lh: 2 } }, { from: 2.25, to: 3.05, scale: { naIn_mmolh: 20 } }] },
};
function specScenario(spec) {
  return {
    id: spec.id, tEnd: spec.tEnd, dt: spec.dt, outEvery: spec.outEvery, breakpoints: spec.breakpoints.slice(),
    label: null,
    inputs: (t, p) => {
      const u = E.baselineInputs(p);
      for (const s of spec.schedule) {
        if (t >= s.from && t < s.to) {
          for (const [k, v] of Object.entries(s.set || {})) u[k] = v;
          for (const [k, f] of Object.entries(s.scale || {})) u[k] *= f;
        }
      }
      return u;
    },
  };
}
function compactTrajectory(r, tEnd, outEvery, extra = []) {
  const T = r.t.length;
  const required = [0, 1, 10, 100, T - 1];
  const indices = uniqSorted([...required, ...extra.filter((k) => k < T)]);
  const sec = (obj, keys) => Object.fromEntries(keys.map((k) => [k, pick(obj[k], indices)]));
  return {
    dt: r.meta.dt, tEnd, outEvery: outEvery ?? null, length: T, steps: r.meta.steps,
    maxStep: r.meta.maxStep, requiredIndices: required, indices, t: pick(r.t, indices),
    states: sec(r.states, E.STATE_KEYS), derived: sec(r.derived, E.DERIVED_KEYS),
    ledger: sec(r.ledger, E.LEDGER_KEYS),
  };
}
function goldenSolverCoverage() {
  const p = E.defaultParams();
  const runs = {};
  const y0 = E.initialState(p);
  y0[E.IDX.K_icf] *= 1.02;
  runs['baseline_K_icf_x1.02'] = { run: { scenario: 'baseline', tEnd: 24, y0Scale: { K_icf: 1.02 } },
    ...compactTrajectory(E.simulate({ scenario: 'baseline', tEnd: 24, y0 }), 24, undefined, [60, 360]) };
  for (const [name, spec] of Object.entries(SPEC_SCENARIOS)) {
    runs[name] = { run: { spec },
      ...compactTrajectory(E.simulate({ scenario: specScenario(spec) }), spec.tEnd, spec.outEvery,
        name === 'sweat_potassium' ? [180, 240] : [12, 16, 23, 31]) };
  }
  for (const outEvery of [7, 2.5]) {
    runs[`drink_water_1L_outEvery_${outEvery}`] = { run: { scenario: 'drink_water_1L', outEvery },
      ...compactTrajectory(E.simulate({ scenario: 'drink_water_1L', outEvery }), 12, outEvery) };
  }
  for (const scenario of ['drink_water_1L', 'salt_load_10g']) {
    runs[`${scenario}_dt_0.1`] = { run: { scenario, dt: 0.1 },
      ...compactTrajectory(E.simulate({ scenario, dt: 0.1 }), E.SCENARIOS[scenario].tEnd, undefined, [12, 13]) };
  }
  return runs;
}

// (n) JavaScript non-finite semantics the port emulates (BUG-20261003-134): the NaN-last
// Float64Array sort inside quantileBands, Math.max(0, NaN) = NaN in the fluxes (a state with
// ADH = NaN), and Math.pow(1, NaN) = NaN (C99 says 1): at the initial state vr = 1 and
// MAP/MAP_0 = 1, so aldo_vol_exp = NaN and gfr_map_exp = NaN reach pow(1, NaN). Numbers
// only: -0 in the series is written as 0 by JSON, so the self-test carries its own copy.
const QB_SERIES = [[1, NaN, 3], [Infinity, 2, -0], [-Infinity, NaN, 0], [5, 4, Infinity], [2, 3, Infinity]];
const QB_QS = [0.05, 0.5, 0.95, 0, 1];
function goldenNonFinite() {
  const bands = MC.quantileBands(QB_SERIES.map((a) => Float64Array.from(a)), QB_QS).map(arr);
  const at = (params, y) => {
    const C = E.constants(params);
    const inputs = E.baselineInputs(params);
    const led = new Float64Array(E.LEDGER_KEYS.length);
    const dy = E.rhs(0, y, params, inputs, null, led, C);
    return { y: arr(y), dy: arr(dy), ledger: arr(led), fluxes: E.fluxes(y, params, inputs, C),
      derived: E.derived(y, params, C) };
  };
  const p = E.defaultParams();
  const yAdh = E.initialState(p);
  yAdh[E.IDX.ADH] = NaN;
  const pow1 = { aldo_vol_exp: NaN, gfr_map_exp: NaN };
  const pPow = { ...p, ...pow1 };
  return {
    quantileBands: { series: QB_SERIES, qs: QB_QS, bands },
    adhNaN: { overrides: {}, ...at(p, yAdh) },
    pow1NaN: { overrides: pow1, ...at(pPow, E.initialState(pPow)) },
  };
}

// (p) influenceAll (HREQ-U-12, BUG-20261003-096): computeInfluenceAll() at its defaults,
// every registered scenario at its own tEnd and dt, +10 % and -10 %. Written compactly to
// stay in budget: meta, the union, the per-scenario feeder lists, and per scenario and
// direction the feeder lists (params) with their effects aligned to them (effects), rounded
// to INFLUENCE_ALL_DIGITS significant digits. Effects below the threshold are not written:
// they are implied by the lists. The self-test compares drink_water_1L (one reference run
// plus 114) by default and every scenario under --robust (tests/health_selftest.py), and
// tools/health_influence_diff.py diffs a recomputed union against this one (HREQ-V-19).
const INFLUENCE_ALL_DIGITS = 6;
function goldenInfluenceAll() {
  const r = E.computeInfluenceAll();
  LABELS.computeInfluenceAll = labelsOf(r);
  const round = (e) => (Number.isFinite(e) ? Number(e.toPrecision(INFLUENCE_ALL_DIGITS)) : e);
  const runs = {};
  for (const [sc, byDir] of Object.entries(r.runs)) {
    runs[sc] = {};
    for (const [dir, x] of Object.entries(byDir)) {
      runs[sc][dir] = { params: x.params,
        effects: Object.fromEntries(Object.entries(x.params).map(([k, l]) => [k, l.map((n) => round(x.effects[k][n]))])) };
    }
  }
  return { effectDigits: INFLUENCE_ALL_DIGITS, meta: r.meta, union: r.union, feeders: r.feeders, runs };
}
function reusedInfluenceAll(path) {
  const revive = (_k, v) => (v !== null && typeof v === 'object' && !Array.isArray(v)
    && Object.keys(v).length === 1 && '$float' in v ? Number(v.$float) : v);
  const old = JSON.parse(readFileSync(resolve(path), 'utf8'), revive);
  if (!old.influenceAll || !old.resultLabels?.computeInfluenceAll) {
    console.error(`health_golden: ${path} has no influenceAll section to reuse`);
    process.exit(2);
  }
  console.error(`health_golden: influenceAll reused verbatim from ${path} (test seam; that section is not checked)`);
  LABELS.computeInfluenceAll = old.resultLabels.computeInfluenceAll;
  return old.influenceAll;
}

const golden = {
  generator: 'tools/health_golden.mjs',
  regenerate: '/opt/node22/bin/node tools/health_golden.mjs',
  reference: 'reference/metabolic-map-v1/engine/index.js',
  node: process.version,
  modelVersion: E.MODEL_VERSION,
  disclaimer: E.DISCLAIMER,
  validationStatus: E.VALIDATION_STATUS,
  mulberry32: goldenMulberry(),
  engineSha256: engineHashes(),
  trajectories: goldenTrajectories(),
  stiffCorner: goldenStiffCorner(),
  checkpoints: goldenCheckpoints(),
  drawSamples: goldenDrawSamples(),
  simulateMC: goldenMC(),
  saltLoadMetrics: goldenSaltMetrics(),
  scenarios: goldenScenarios(),
  rejection: goldenRejection(),
  simulateSweep: goldenSweep(),
  computeInfluence: goldenInfluence(),
  steadyState: goldenConstants(),
  paramSummary: (() => {
    const s = E.paramSummary();
    LABELS.paramSummary = labelsOf(s);
    return withoutMeta(s);
  })(),
  findings: goldenFindings(),
  solverCoverage: goldenSolverCoverage(),
  nonFinite: goldenNonFinite(),
  resultLabels: LABELS,
};
golden.rejectionMargins = MARGINS;
golden.influenceAll = REUSE_INFLUENCE_ALL ? reusedInfluenceAll(REUSE_INFLUENCE_ALL) : goldenInfluenceAll();

const replacer = (_k, v) => (typeof v === 'number' && !Number.isFinite(v) ? { $float: String(v) } : v);
// Pretty-printed, except that an array of primitives goes on one line (keeps the file
// small and its diffs readable). Numbers are written by JSON.stringify: the shortest
// decimal that round-trips to the same double, which Python's json parses back exactly.
function format(v, depth) {
  const pad = (d) => ' '.repeat(d);
  if (Array.isArray(v)) {
    if (v.every((x) => x === null || typeof x !== 'object')) return JSON.stringify(v);
    return '[\n' + v.map((x) => pad(depth + 1) + format(x, depth + 1)).join(',\n') + '\n' + pad(depth) + ']';
  }
  if (v !== null && typeof v === 'object') {
    const ent = Object.entries(v);
    if (ent.length === 0) return '{}';
    return '{\n' + ent.map(([k, x]) => pad(depth + 1) + JSON.stringify(k) + ': ' + format(x, depth + 1)).join(',\n') +
      '\n' + pad(depth) + '}';
  }
  return JSON.stringify(v);
}
const text = format(JSON.parse(JSON.stringify(golden, replacer)), 0) + '\n';
const bytes = Buffer.byteLength(text, 'utf8');
if (bytes > MAX_BYTES) {
  console.error(`health_golden: fixture is ${bytes} bytes, over the ${MAX_BYTES}-byte budget`);
  process.exit(1);
}

if (process.argv.includes('--check')) {
  // The header's `node` field is provenance (which Node wrote the file), not a value: a
  // fixture is up to date when every other byte is the same, whatever Node runs the
  // check. Node 20.20, 22.22 and 22.23 regenerate it byte-identically (BUG-20261003-171,
  // which was CI reading a valid fixture as stale because only this line differed).
  const cur = existsSync(OUT) ? readFileSync(OUT, 'utf8') : '';
  const NODE_LINE = /^ "node": "v[^"]*",$/m;
  const recorded = (cur.match(NODE_LINE) || [''])[0].replace(/^ "node": "|",$/g, '');
  const curNormalised = cur.replace(NODE_LINE, ` "node": ${JSON.stringify(process.version)},`);
  if (curNormalised !== text) {
    const a = curNormalised.split('\n'), b = text.split('\n');
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i += 1;
    console.error(`health_golden: ${OUT} is stale; regenerate with: ${golden.regenerate}`);
    console.error(`  first difference at line ${i + 1}:`);
    console.error(`    file: ${(a[i] ?? '<end of file>').slice(0, 160)}`);
    console.error(`    now:  ${(b[i] ?? '<end of file>').slice(0, 160)}`);
    process.exit(1);
  }
  const under = recorded && recorded !== process.version
    ? ` (generated under node ${recorded}, checked under ${process.version}: identical)` : '';
  console.log(`health_golden: ${OUT} is up to date (${bytes} bytes)${under}`);
} else {
  mkdirSync(dirname(OUT), { recursive: true });
  writeFileSync(OUT, text);
  console.log(`health_golden: wrote ${OUT} (${bytes} bytes, node ${process.version}, model ${E.MODEL_VERSION})`);
}
