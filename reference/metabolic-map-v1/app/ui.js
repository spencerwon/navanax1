// app/ui.js — page controller: worker pool, Monte Carlo runs, playback, charts,
// evidence drawer, entity rail, dose–response, stress test, footer.
// EDUCATIONAL MODEL — NOT MEDICAL ADVICE.

import {
  SCENARIOS, paramTable, DISCLAIMER, MODEL_VERSION, simulate, simulateSweep, drawSamples,
  makeScenario, makeWaterLoad, makeSaltLoad, MMOL_NA_PER_G_NACL, REFERENCE_PERSON, paramsFor, primeInfluence, computeInfluence,
  INFLUENCE_DEFAULTS,
} from '../engine/index.js';
import { quantileBands } from '../engine/mc.js';
import { BandChart, DoseChart, fmt, fmtSigned } from './charts.js';
import { ENTITIES as SEED_ENTITIES, SYSTEMS, LADDER } from './entities.seed.js';
import { KB_AVAILABLE } from './config.js';

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

const TABLE = paramTable();
const PARAMS0 = Object.fromEntries(Object.entries(TABLE).map(([k, v]) => [k, v.value]));
const MC_N = 64, SEED = 1;
const MC_KEYS = ['Na_plasma', 'osm_plasma', 'V_ecf', 'V_icf', 'urine_flow', 'U_osm', 'ADH', 'MAP',
  'strain_index', 'Thirst', 'GFR', 'GFR_norm', 'FE_Na', 'Aldo', 'V_gut_water', 'Na_gut'];
const DELTA_KEYS = ['V_ecf', 'V_icf'];
const VOID_AT_L = 0.4; // display rule for the bladder (DESIGN_DECISIONS.md)

// Display formats: [decimals, unit, label]. Proposed in DESIGN_DECISIONS.md.
const QFMT = {
  Na_plasma: [1, 'mmol/L', 'Plasma Na'], osm_plasma: [0, 'mOsm/kg', 'Osmolality'], V_ecf: [2, 'L', 'ECF volume'],
  V_icf: [2, 'L', 'ICF volume'], urine_flow: [2, 'L/h', 'Urine flow'], U_osm: [0, 'mOsm/kg', 'Urine osmolality'],
  ADH: [1, 'pg/mL', 'ADH'], MAP: [0, 'mmHg', 'MAP'], strain_index: [2, '', 'Strain index'], Thirst: [0, '%', 'Thirst'],
  GFR: [0, 'mL/min', 'GFR (absolute)'], GFR_norm: [0, 'mL/min per 1.73 m²', 'GFR (per 1.73 m²)'], FE_Na: [2, '%', 'FE Na'], Aldo: [2, '× baseline', 'Aldosterone'],
  V_gut_water: [2, 'L', 'Water in gut'], Na_gut: [0, 'mmol', 'Na in gut'],
};

// Which parameters sit behind each displayed quantity (for the evidence drawer).
// Audit F-01 / BUG-0049: chips and drawer use engine paramsFor() (a sensitivity screen over all
// parameters; see engine/index.js). The hand lists below (CORE_NA, STRAIN_P, CHARTS[].params,
// DOSE_PARAMS) are ONLY a fallback if that screen throws; they are known to under-count.
const CORE_NA = ['V_ecf_0', 'glucose_mgdl', 'bun_mgdl', 'adh_threshold', 'adh_slope', 'adh_ec50', 'adh_hill', 'U_osm_min',
  'U_osm_max', 'gut_water_thalf_h', 'gut_na_thalf_h', 'osm_eq_tau_h', 'na_osm_gain', 'urea_excr_mosmd', 'waterIn_base_Ld',
  'naIn_base_mmold', 'insensible_Ld', 'na_normal_low', 'na_normal_high'];
const STRAIN_P = ['strain_w_transport', 'strain_w_excretion', 'strain_w_glomerular', 'strain_w_concentrating',
  'strain_scale_transport', 'strain_scale_excretion', 'strain_scale_pressure', 'GFR_0', 'MAP_0', 'gfr_map_exp', 'gfr_vol_exp', 'U_osm_max'];

const CHARTS = [
  { id: 'na', title: 'Plasma sodium', keys: ['Na_plasma'], unit: 'mmol/L', dec: 1, minSpan: 2,
    refLines: [{ value: 135, label: '135 hyponatremia threshold', tone: 'critical' }, { value: 145, label: '145 upper normal', tone: 'muted' }],
    params: CORE_NA },
  { id: 'osm', title: 'Plasma osmolality', keys: ['osm_plasma'], unit: 'mOsm/kg', dec: 0, minSpan: 4,
    params: [...CORE_NA, 'thirst_threshold'] },
  { id: 'vol', title: 'ECF and ICF volume change', keys: ['V_ecf', 'V_icf'], delta: true, unit: 'L', dec: 2, minSpan: 0.1, signed: true,
    names: ['ECF', 'ICF'], params: ['V_ecf_0', 'V_icf_0', 'K_icf_0', 'osm_eq_tau_h', 'gut_water_thalf_h', 'gut_na_thalf_h', 'water_metabolic_Ld', 'insensible_Ld', 'fecal_water_Ld', 'k_excr_gain'] },
  { id: 'uflow', title: 'Urine flow', keys: ['urine_flow'], unit: 'L/h', dec: 2, minSpan: 0.1, floor0: true,
    params: ['U_osm_min', 'U_osm_max', 'adh_ec50', 'adh_hill', 'urea_excr_mosmd', 'GFR_0', 'na_osm_gain', 'pn_gain', 'aldo_effect_exp', 'anp_effect_exp', 'k_excr_gain'] },
  { id: 'uosm', title: 'Urine osmolality', keys: ['U_osm'], unit: 'mOsm/kg', dec: 0, minSpan: 50,
    params: ['U_osm_min', 'U_osm_max', 'adh_ec50', 'adh_hill', 'adh_threshold', 'adh_slope', 'adh_vol_shift'] },
  { id: 'adh', title: 'ADH (vasopressin)', keys: ['ADH'], unit: 'pg/mL', dec: 1, minSpan: 0.5, floor0: true,
    params: ['adh_threshold', 'adh_slope', 'adh_vol_shift', 'adh_thalf_h', 'glucose_mgdl', 'bun_mgdl'] },
  { id: 'map', title: 'Mean arterial pressure', keys: ['MAP'], unit: 'mmHg', dec: 0, tipDec: 1, minSpan: 4,
    params: ['MAP_0', 'map_vol_exp', 'map_tau_h', 'map_auto_frac', 'map_auto_tau_h', 'pn_gain', 'V_ecf_0', 'aldo_vol_exp', 'aldo_tau_h', 'anp_vol_exp'] },
  { id: 'strain', title: 'Kidney strain index', keys: ['strain_index'], unit: 'index, 0–1', dec: 2, minSpan: 0.05, floor0: true,
    note: 'A model index built from four workloads. It is not a clinical measure and has no validated thresholds.',
    params: STRAIN_P },
];
const DOSE_METRICS = [
  { id: 'peak_strain_index', label: 'Peak strain index', unit: '', dec: 2 },
  { id: 'peak_dNa', label: 'Peak Δ plasma Na', unit: 'mmol/L', dec: 1 },
  { id: 'peak_dV_ecf', label: 'Peak ΔECF', unit: 'L', dec: 2 },
  { id: 'na_excr_24h', label: 'Extra Na out in 24 h', unit: 'mmol', dec: 0 },
  { id: 'peak_MAP', label: 'Peak MAP rise', unit: 'mmHg', dec: 1 },
];
const DOSE_PARAMS = [...STRAIN_P, 'naIn_base_mmold', 'gut_na_thalf_h', 'pn_gain', 'na_osm_gain', 'aldo_vol_exp', 'aldo_tau_h',
  'aldo_effect_exp', 'anp_vol_exp', 'anp_effect_exp', 'anp_thalf_h', 'map_vol_exp', 'map_tau_h'];

// ---------------------------------------------------------------------------
// Scenario specs: structured-clone-safe descriptions rebuilt inside workers.
// Keep in sync with scenarioFromSpec() in worker.js.
function scenarioFromSpec(spec) {
  if (spec.kind === 'named') return SCENARIOS[spec.id];
  if (spec.kind === 'water') return makeWaterLoad(...spec.args);
  if (spec.kind === 'salt') return makeSaltLoad(...spec.args);
  return makeScenario({ id: 'custom', title: 'Custom', tEnd: spec.tEnd, events: spec.events || [] });
}

// ---------------------------------------------------------------------------
// Worker pool sized to navigator.hardwareConcurrency.
class Pool {
  constructor(size) { this.size = size; this.workers = []; this.idle = []; this.queue = []; this.busy = 0; this.ok = false; this.seq = 0; this.onActivity = null; }
  async init() {
    if (typeof Worker === 'undefined') return false;
    try {
      for (let i = 0; i < this.size; i++) {
        const w = new Worker(new URL('./worker.js', import.meta.url), { type: 'module' });
        w.onmessage = (e) => this.done(w, e.data);
        w.onerror = (e) => { e.preventDefault?.(); this.fail(w, e.message || 'worker error'); };
        this.workers.push(w);
      }
      await Promise.race([
        Promise.all(this.workers.map((w) => new Promise((res, rej) => { w.job = { resolve: res, reject: rej }; w.postMessage({ type: 'ping', id: -1 }); }))),
        new Promise((_, rej) => setTimeout(() => rej(new Error('worker start timeout')), 15000)),
      ]);
      this.workers.forEach((w) => { w.job = null; this.idle.push(w); });
      this.ok = true;
    } catch (err) {
      console.warn('Worker pool unavailable, falling back to the main thread:', err);
      this.workers.forEach((w) => w.terminate()); this.workers = []; this.ok = false;
    }
    return this.ok;
  }
  run(msg, tag) {
    return new Promise((resolve, reject) => { this.queue.push({ msg, tag, resolve, reject }); this.pump(); });
  }
  cancel(tag) {
    const keep = [];
    for (const j of this.queue) { if (j.tag === tag) j.reject({ cancelled: true }); else keep.push(j); }
    this.queue = keep;
  }
  pump() {
    while (this.idle.length && this.queue.length) {
      const w = this.idle.pop(); const job = this.queue.shift();
      w.job = job; this.busy++;
      w.postMessage({ ...job.msg, id: ++this.seq });
    }
    this.onActivity?.(this.busy);
  }
  done(w, data) {
    const job = w.job; w.job = null;
    if (!job) return;
    if (data.id !== -1) { this.busy--; this.idle.push(w); }
    data.ok ? job.resolve(data) : job.reject(new Error(data.error));
    if (data.id !== -1) this.pump();
  }
  fail(w, msg) { const job = w.job; w.job = null; job?.reject(new Error(msg)); }
}

/** Main-thread fallback with the same message contract as worker.js. */
function runLocal(msg) {
  return new Promise((resolve) => setTimeout(() => {
    if (msg.type === 'sweep') {
      const r = simulateSweep({ n: msg.n, seed: msg.seed, values: msg.values });
      resolve({ values: r.values, metrics: r.metrics }); return;
    }
    if (msg.type === 'influence') { resolve({ result: computeInfluence() }); return; }
    const { samples } = drawSamples({ n: msg.n, seed: msg.seed });
    const sc = scenarioFromSpec(msg.scenario);
    let t = null; const data = Object.fromEntries(msg.keys.map((k) => [k, []]));
    for (let i = msg.i0; i < msg.i1; i++) {
      const r = simulate({ params: samples[i], scenario: sc, dt: msg.dt, outEvery: msg.outEvery });
      t = r.t; for (const k of msg.keys) data[k].push(r.states[k] || r.derived[k]);
    }
    const T = t.length; const packed = {};
    for (const k of msg.keys) { const b = new Float64Array(data[k].length * T); data[k].forEach((a, j) => b.set(a, j * T)); packed[k] = b; }
    resolve({ t, T, count: msg.i1 - msg.i0, data: packed });
  }, 0));
}

const cores = Math.max(1, Math.min(32, navigator.hardwareConcurrency || 4));
const pool = new Pool(cores);
const exec = (msg, tag) => (pool.ok ? pool.run(msg, tag) : runLocal(msg));

let runSeq = 0;
/** Pooled Monte Carlo. Identical to engine simulateMC() quantiles (same samples, same order). */
async function runMC({ spec, n = MC_N, seed = SEED, keys = MC_KEYS, outEvery, chunk, onProgress, tag }) {
  const parts = Math.max(1, pool.ok ? pool.size * 3 : 4);
  const size = chunk || Math.max(1, Math.ceil(n / parts));
  const jobs = [];
  for (let i0 = 0; i0 < n; i0 += size) jobs.push({ i0, i1: Math.min(n, i0 + size) });
  let doneCount = 0;
  const t0 = performance.now();
  const results = await Promise.all(jobs.map((j) => exec({ type: 'mc', scenario: spec, n, seed, keys, outEvery, ...j }, tag)
    .then((r) => { doneCount += j.i1 - j.i0; onProgress?.(doneCount / n); return { ...j, r }; })));
  const ms = performance.now() - t0;
  const t = results[0].r.t; const T = results[0].r.T;
  const out = { t, n, ms, q05: {}, q50: {}, q95: {}, dq05: {}, dq50: {}, dq95: {} };
  for (const k of keys) {
    const series = [];
    for (const { r } of results) for (let j = 0; j < r.count; j++) series.push(r.data[k].subarray(j * T, (j + 1) * T));
    [out.q05[k], out.q50[k], out.q95[k]] = quantileBands(series, [0.05, 0.5, 0.95]);
    if (DELTA_KEYS.includes(k)) {
      const d = series.map((a) => { const x = new Float64Array(T); for (let i = 0; i < T; i++) x[i] = a[i] - a[0]; return x; });
      [out.dq05[k], out.dq50[k], out.dq95[k]] = quantileBands(d, [0.05, 0.5, 0.95]);
    }
  }
  return out;
}

async function runSweep({ onProgress, tag }) {
  const values = [...SCENARIOS.salt_load_sweep.sweep.values];
  let done = 0;
  const parts = await Promise.all(values.map((v) => exec({ type: 'sweep', n: MC_N, seed: SEED, values: [v] }, tag)
    .then((r) => { done++; onProgress?.(done / values.length); return r; })));
  const metrics = {};
  for (const m of SCENARIOS.salt_load_sweep.sweep.metrics) {
    metrics[m] = { q05: [], q50: [], q95: [] };
    for (const p of parts) for (const q of ['q05', 'q50', 'q95']) metrics[m][q].push(p.metrics[m][q][0]);
  }
  return { values, metrics };
}

// ---------------------------------------------------------------------------
// Evidence + entities loading (prefers the Curator's kb/, falls back to seed / engine).
async function fetchJSON(url) {
  const r = await fetch(url, { cache: 'no-cache' });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
async function loadEntities() {
  if (!KB_AVAILABLE) return { list: SEED_ENTITIES, source: 'app/entities.seed.js (seed)' }; // no kb/ probe (config.js)
  try {
    const kb = await fetchJSON('../kb/entities.json');
    const list = Array.isArray(kb) ? kb : (kb.entities || Object.values(kb));
    const seedById = Object.fromEntries(SEED_ENTITIES.map((e) => [e.id, e]));
    const merged = list.map((e) => ({ ...seedById[e.id], ...e, mesh: seedById[e.id]?.mesh ?? null, system: seedById[e.id]?.system ?? e.system ?? null }));
    // keep seed entities the viewer needs that the KB has not yet covered
    for (const s of SEED_ENTITIES) if (!merged.find((m) => m.id === s.id)) merged.push(s);
    return { list: merged, source: 'kb/entities.json' };
  } catch { return { list: SEED_ENTITIES, source: 'app/entities.seed.js (seed)' }; }
}
async function loadEvidence(kbPresent) {
  // Only request kb/ when its entities file loaded (KB_AVAILABLE in config.js gates the first request).
  const urls = kbPresent ? ['../kb/evidence.json', '../engine/evidence.engine.json'] : ['../engine/evidence.engine.json'];
  for (const url of urls) {
    try {
      const j = await fetchJSON(url);
      const arr = Array.isArray(j) ? j : (j.evidence || Object.values(j));
      return { map: new Map(arr.map((e) => [e.id, e])), source: url.replace('../', '') };
    } catch { /* try next */ }
  }
  return { map: new Map(), source: 'none' };
}

// ---------------------------------------------------------------------------
const S = {
  mode: 'scenario', scenarioId: 'drink_water_1L', spec: { kind: 'named', id: 'drink_water_1L' },
  res: null, bladder: null, playT: 0, idx: 0, playing: false, speed: 1, xUnit: 'h',
  entities: [], evidence: new Map(), entity: null, viewer: null, charts: {}, dose: null, doseMetric: 'peak_strain_index',
  runTag: null,
};

function gradeClass(g) { return (g || 'E').charAt(0); }
function gradeBadge(g) {
  const c = gradeClass(g);
  const b = el('span', `grade ${c}`, c === 'E' ? 'E · assumption' : `${c} · ${g.split('-')[1] || ''}`);
  b.title = g; return b;
}

// ---------------------------------------------------------------------------
// Evidence drawer
let lastFocus = null;
function openDrawer(title, paramNames, extraEvidence = [], note = null) {
  lastFocus = document.activeElement;
  $('drawerTitle').textContent = title;
  const names = [...new Set(paramNames)].filter((n) => TABLE[n]);
  names.sort((a, b) => gradeClass(TABLE[b].grade).localeCompare(gradeClass(TABLE[a].grade)) || a.localeCompare(b));
  const counts = {};
  for (const n of names) { const c = gradeClass(TABLE[n].grade); counts[c] = (counts[c] || 0) + 1; }
  const sum = $('drawerSum'); sum.replaceChildren();
  sum.append(el('span', 'chip', `${names.length} parameters`));
  for (const c of ['A', 'B', 'C', 'D', 'E']) if (counts[c]) { const b = el('span', `grade ${c}`, `${c}: ${counts[c]}`); sum.append(b); }
  const body = $('drawerBody'); body.replaceChildren();
  if (note) body.append(el('p', 'scen-desc', note));
  if (counts.E) {
    const a = el('div', 'alert');
    a.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1l7.5 13H.5zM7.2 6v4.2h1.6V6zm0 5.4V13h1.6v-1.6z"/></svg>';
    a.append(el('span', '', `${counts.E} of ${names.length} values behind this quantity are our own assumptions (grade E), not measurements. The shape of this curve is illustrative until those are replaced with sourced values.`));
    body.append(a);
  }
  for (const n of names) {
    const p = TABLE[n];
    const card = el('div', `prm${gradeClass(p.grade) === 'E' ? ' is-e' : ''}`);
    const h = el('div', 'h'); h.append(el('code', '', n), gradeBadge(p.grade)); card.append(h);
    const v = el('div', 'val');
    v.append(document.createTextNode(`${p.value} ${p.unit}`));
    if (p.range) v.append(el('span', '', `  ·  range ${p.range[0]} to ${p.range[1]}${p.mc === false ? ' (held fixed in Monte Carlo)' : ''}`));
    card.append(v, el('p', '', p.description));
    if (p.notes) card.append(el('p', '', p.notes));
    const ul = el('ul');
    for (const id of p.evidence || []) ul.append(evidenceItem(id));
    card.append(ul); body.append(card);
  }
  if (extraEvidence.length) {
    body.append(el('h3', 'section-title', 'Scenario validation sources'));
    const ul = el('ul', 'srcs'); for (const id of extraEvidence) ul.append(evidenceItem(id)); body.append(ul);
  }
  $('scrim').hidden = false; $('drawer').hidden = false; $('drawerClose').focus();
}
function evidenceItem(id) {
  const li = el('li'); const e = S.evidence.get(id);
  if (!e) { li.textContent = `${id} (record not found)`; return li; }
  li.append(document.createTextNode(`${e.authors?.split(',')[0] || ''}${e.authors?.includes(',') ? ' et al.' : ''} ${e.year || ''}. ${e.title}. `));
  const link = e.doi ? `https://doi.org/${e.doi}` : e.url;
  if (link) { const a = el('a', '', e.doi ? `doi:${e.doi}` : 'source'); a.href = link; a.target = '_blank'; a.rel = 'noopener'; li.append(a); }
  if (e.pmid) li.append(document.createTextNode(` · PMID ${e.pmid}`));
  return li;
}
function closeDrawer() { $('scrim').hidden = true; $('drawer').hidden = true; lastFocus?.focus?.(); }

function evChip(btn, params, fallback = false) {
  const names = [...new Set(params)].filter((n) => TABLE[n]);
  const e = names.filter((n) => gradeClass(TABLE[n].grade) === 'E').length;
  btn.replaceChildren(document.createTextNode(`Evidence · ${names.length}`));
  if (e) btn.append(el('span', 'e', `${e} E`));
  btn.title = `${names.length} parameters, ${e} assumed (grade E). Open the citations.` +
    (fallback ? ' (Fallback hand-written list: the automatic screen failed, so this may under-count.)' : '');
}

// Evidence lists from the engine's sensitivity screen (audit F-01). The screen (~53 runs) is done once in
// a worker at start-up and installed with primeInfluence(); until then chips show "…", and a click
// computes it on the main thread (paramsFor caches). Hand lists are used only if paramsFor throws.
const INF = { failed: false };
const DOSE_KEYS = ['strain_index', 'Na_plasma', 'V_ecf', 'MAP', 'na_out']; // the sweep metrics' sources
const INF_NOTE = `Listed: every parameter whose +${INFLUENCE_DEFAULTS.rel * 100}% change moves this quantity by more than ` +
  `${INFLUENCE_DEFAULTS.threshold * 100}% of its range (one-at-a-time screen, 10 g salt, first 24 h). The 90% band samples every ranged parameter.`;
function influenced(keys, fallback) {
  if (INF.failed) return { list: fallback, fallback: true };
  try { const list = [...new Set(keys.flatMap((k) => paramsFor(k)))]; return { list, fallback: false }; }
  catch (err) { console.warn('paramsFor failed, using hand-written evidence lists:', err); INF.failed = true; return { list: fallback, fallback: true }; }
}
const chartParams = (c) => influenced(c.keys, c.params);
const doseParams = () => influenced(DOSE_KEYS, DOSE_PARAMS);
function chipPending(btn) {
  btn.replaceChildren(document.createTextNode('Evidence · …'));
  btn.title = 'Working out which parameters feed this chart. Click to open the citations.';
}
function refreshChips() {
  for (const c of CHARTS) { const r = chartParams(c); evChip(S.charts[c.id].chip, r.list, r.fallback); }
  const d = doseParams(); evChip($('doseEv'), d.list, d.fallback);
}
async function loadInfluence() {
  try {
    const r = await exec({ type: 'influence' }, 'influence');
    primeInfluence(r.result);
  } catch (err) { console.warn('Influence screen in worker failed; computing on demand:', err); }
  refreshChips();
}

// ---------------------------------------------------------------------------
// Charts
function buildCharts() {
  const host = $('charts');
  for (const c of CHARTS) {
    const card = el('article', 'chart-card');
    const head = el('div', 'chart-head');
    const h = el('h3', '', c.title); h.append(el('span', 'unit', c.unit)); head.append(h);
    const tools = el('div', 'chart-tools');
    const tb = el('button', 'tbl-btn', 'Table'); tb.type = 'button'; tb.setAttribute('aria-expanded', 'false');
    const chip = el('button', 'ev-chip'); chip.type = 'button'; chipPending(chip);
    chip.addEventListener('click', () => {
      const r = chartParams(c); refreshChips();
      openDrawer(c.title, r.list, scenarioEvidence(), [c.note, r.fallback ? null : INF_NOTE].filter(Boolean).join(' '));
    });
    tools.append(tb, chip); head.append(tools); card.append(head);
    if (c.keys.length > 1) {
      const lg = el('div', 'legend');
      c.keys.forEach((k, i) => { const s = el('span'); const sw = el('i'); sw.style.background = `var(--s${i + 1})`; s.append(sw, document.createTextNode(c.names[i])); lg.append(s); });
      card.append(lg);
    }
    const box = el('div', 'chart-box'); card.append(box);
    const tw = el('div', 'tablewrap'); tw.hidden = true; card.append(tw);
    if (c.note) card.append(el('div', 'chart-foot', c.note));
    host.append(card);
    const chart = new BandChart(box, { unit: c.unit === 'index, 0–1' ? '' : c.unit, decimals: c.tipDec ?? c.dec, refLines: c.refLines, minSpan: c.minSpan,
      floor0: c.floor0, signed: c.signed, ariaLabel: `${c.title} chart, median with 90% band. Arrow keys move the cursor, Enter jumps the playhead.`,
      onScrub: (i) => { setPlayIndex(i); } });
    tb.addEventListener('click', () => {
      tw.hidden = !tw.hidden; tb.setAttribute('aria-expanded', String(!tw.hidden));
      if (!tw.hidden) renderTable(c, chart, tw);
    });
    S.charts[c.id] = { cfg: c, chart, tw, chip };
  }
}

function renderTable(c, chart, tw) {
  if (!chart.data) return;
  const t = el('table', 'data'); const hr = el('tr');
  hr.append(el('th', '', S.xUnit === 'd' ? 'Day' : 'Hour'));
  for (const s of chart.data.series) for (const q of ['median', 'q05', 'q95']) hr.append(el('th', '', `${chart.data.series.length > 1 ? s.name + ' ' : ''}${q}`));
  const thead = el('thead'); thead.append(hr); t.append(thead);
  const tb = el('tbody');
  for (const k of chart.tableRows()) {
    const tr = el('tr');
    tr.append(el('td', '', S.xUnit === 'd' ? fmt(chart.data.t[k] / 24, 0) : fmt(chart.data.t[k], 0)));
    for (const s of chart.data.series) for (const q of ['q50', 'q05', 'q95']) tr.append(el('td', '', (c.signed ? fmtSigned : fmt)(s[q][k], c.dec)));
    tb.append(tr);
  }
  t.append(tb); tw.replaceChildren(t);
}

function feedCharts() {
  const R = S.res;
  for (const { cfg, chart, tw } of Object.values(S.charts)) {
    chart.opts.xUnit = S.xUnit;
    const series = cfg.keys.map((k, i) => ({
      name: cfg.names?.[i] || QFMT[k][2], colorVar: `--s${i + 1}`,
      q05: cfg.delta ? R.dq05[k] : R.q05[k], q50: cfg.delta ? R.dq50[k] : R.q50[k], q95: cfg.delta ? R.dq95[k] : R.q95[k],
    }));
    chart.setDim(false);
    chart.setData({ t: R.t, series });
    if (!tw.hidden) renderTable(cfg, chart, tw);
  }
}

// ---------------------------------------------------------------------------
// Playback
function nearestIdx(t, x) {
  let lo = 0, hi = t.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (t[m] <= x) lo = m; else hi = m; }
  return x - t[lo] < t[hi] - x ? lo : hi;
}
function setPlayIndex(i) {
  if (!S.res) return;
  S.idx = Math.max(0, Math.min(S.res.t.length - 1, i));
  S.playT = S.res.t[S.idx];
  $('scrub').value = String(S.idx);
  updateAtPlayhead();
}
function setPlaying(on) {
  S.playing = on && !!S.res;
  $('playBtn').setAttribute('aria-label', S.playing ? 'Pause' : 'Play');
  $('playIcon').innerHTML = S.playing ? '<path d="M3 2h4v12H3zM9 2h4v12H9z"/>' : '<path d="M3 2l11 6-11 6z"/>';
  if (S.playing && S.idx >= S.res.t.length - 1) setPlayIndex(0);
}
let lastTick = performance.now();
function tick(now) {
  const dt = Math.min(0.1, (now - lastTick) / 1000); lastTick = now;
  if (S.playing && S.res) {
    const t = S.res.t; S.playT += dt * S.speed;
    if (S.playT >= t[t.length - 1]) { S.playT = t[t.length - 1]; setPlaying(false); }
    const i = nearestIdx(t, S.playT);
    if (i !== S.idx) { S.idx = i; $('scrub').value = String(i); updateAtPlayhead(); }
  }
  requestAnimationFrame(tick);
}

function med(k, i = S.idx) { return S.res?.q50[k]?.[i]; }

function updateAtPlayhead() {
  const R = S.res; if (!R) return;
  const i = S.idx;
  for (const { chart } of Object.values(S.charts)) chart.setPlayhead(i);
  S.dose?.setPlayhead(-1);
  const h = R.t[i];
  $('hudTime').textContent = S.xUnit === 'd' ? `Day ${fmt(h / 24, 1)}` : `${fmt(h, 2)} h`;
  // stat tiles
  const tiles = [
    ['Plasma Na', 'Na_plasma', 1, 'mmol/L'], ['Osmolality', 'osm_plasma', 0, 'mOsm/kg'],
    ['MAP', 'MAP', 0, 'mmHg'], ['Strain index', 'strain_index', 2, ''],
  ];
  const host = $('stats');
  if (host.children.length !== tiles.length) {
    host.replaceChildren(...tiles.map(() => { const d = el('div', 'stat'); d.append(el('div', 'k'), el('div', 'v'), el('div', 'd')); return d; }));
  }
  tiles.forEach(([lab, k, d, u], j) => {
    const node = host.children[j]; const v = med(k), v0 = med(k, 0);
    node.children[0].textContent = lab;
    node.children[1].replaceChildren(document.createTextNode(fmt(v, d)));
    if (u) node.children[1].append(el('small', '', u));
    const dd = node.children[2]; dd.replaceChildren();
    if (k === 'strain_index') { const sw = el('i', 'sw'); sw.style.background = strainCss(v); dd.append(sw, document.createTextNode('model index')); }
    else dd.textContent = `${fmtSigned(v - v0, k === 'MAP' ? 1 : d)} vs start`;
  });
  // viewer overlay
  const adhAct = Math.max(0, Math.min(1, (med('U_osm') - PARAMS0.U_osm_min) / (PARAMS0.U_osm_max - PARAMS0.U_osm_min)));
  const state = {
    strain: med('strain_index'), bladderL: S.bladder ? S.bladder[i] : 0.15,
    dEcf: R.dq50.V_ecf[i] / R.q50.V_ecf[0], dIcf: R.dq50.V_icf[i] / R.q50.V_icf[0],
    adh: med('ADH'), adhAct, map: med('MAP'), uosm: med('U_osm'), gfr: med('GFR'), gfrNorm: med('GFR_norm'), fena: med('FE_Na'), urine: med('urine_flow'),
  };
  S.viewer?.setState(state);
  $('bpBadge') && ($('bpBadge').replaceChildren(document.createTextNode(`MAP ${fmt(state.map, 0)} `), el('small', '', 'mmHg')));
  $('adhBadge') && ($('adhBadge').replaceChildren(document.createTextNode(`ADH ${fmt(state.adh, 1)} `), el('small', '', 'pg/mL')));
  const th = Math.max(0, Math.min(1, med('Thirst')));
  $('thirstVal').textContent = `${fmt(th * 100, 0)}%`;
  $('thirstBar').style.width = `${th * 100}%`;
  $('thirstMeter').setAttribute('aria-valuenow', String(Math.round(th * 100)));
  $('strainVal').textContent = fmt(state.strain, 2);
  $('strainMark').style.left = `${Math.min(100, state.strain * 100)}%`;
  renderKbLive();
}

// Strain colour stops as RGB triples. Replaced by viewer.js strainStops() (STRAIN_COLOR_RULE)
// once the viewer module loads; this literal is the 'thirds' rule for the no-WebGL fallback.
let STRAIN_RGB = [[0, [156, 74, 68]], [0.33, [240, 160, 32]], [0.66, [208, 59, 59]], [1, [208, 59, 59]]];
const hexRgb = (h) => { const n = parseInt(h.replace('#', ''), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
/** Make the HUD legend bar, its ticks and the stat swatch follow the viewer's strain colour rule. */
function applyStrainRule(stops) {
  STRAIN_RGB = stops.map(([s, hex]) => [s, hexRgb(hex)]);
  const leg = document.querySelector('.strain-legend');
  if (leg) leg.style.background = `linear-gradient(90deg, ${stops.map(([s, hex]) => `${hex} ${Math.round(s * 100)}%`).join(', ')})`;
  const ticks = document.querySelector('.strain-ticks');
  if (ticks) {
    const vals = [...new Set([0, ...stops.map(([s]) => s), 1])];
    ticks.replaceChildren(...vals.map((v) => el('span', '', String(v))));
  }
}
function strainCss(s) {
  const stops = STRAIN_RGB;
  for (let j = 1; j < stops.length; j++) {
    if (s <= stops[j][0] || j === stops.length - 1) {
      const [a, ca] = stops[j - 1], [b, cb] = stops[j]; const f = Math.max(0, Math.min(1, (s - a) / (b - a)));
      return `rgb(${ca.map((x, q) => Math.round(x + (cb[q] - x) * f)).join(',')})`;
    }
  }
  return 'rgb(156,74,68)';
}

/** Bladder volume from the median urine flow; voids at VOID_AT_L (display rule). */
function bladderSeries(t, flow) {
  const b = new Float64Array(t.length); let v = 0.15; b[0] = v;
  for (let k = 1; k < t.length; k++) {
    v += 0.5 * (flow[k] + flow[k - 1]) * (t[k] - t[k - 1]);
    if (v >= VOID_AT_L) v = 0.03;
    b[k] = v;
  }
  return b;
}

// ---------------------------------------------------------------------------
// Running scenarios
function currentScenarioObj() { return scenarioFromSpec(S.spec); }
function scenarioEvidence() { return currentScenarioObj()?.validation?.evidence || []; }

async function runCurrent() {
  if (S.runTag) pool.cancel(S.runTag);
  const tag = `run${++runSeq}`; S.runTag = tag;
  const sc = currentScenarioObj();
  S.xUnit = sc.tEnd > 100 ? 'd' : 'h';
  $('hudScen').textContent = sc.title;
  for (const { chart } of Object.values(S.charts)) chart.setDim(true);
  const prog = $('mcProgress'); prog.style.width = '0%';
  $('mcNote').textContent = `Running ${MC_N} parameter samples on ${pool.ok ? `${pool.size} workers` : 'the main thread'}…`;
  let res;
  try {
    res = await runMC({ spec: S.spec, outEvery: S.xUnit === 'd' ? 4 : undefined, tag, onProgress: (f) => { if (S.runTag === tag) prog.style.width = `${f * 100}%`; } });
  } catch (err) {
    if (err?.cancelled) return;
    console.error(err); $('mcNote').textContent = `Simulation failed: ${err.message}. Reload the page to retry.`; return;
  }
  if (S.runTag !== tag) return;
  S.res = res;
  S.bladder = bladderSeries(res.t, res.q50.urine_flow);
  $('mcNote').textContent = `Monte Carlo n = ${res.n}, seed ${SEED} · median line, 90% band (q05–q95) · ${fmt(res.ms / 1000, 2)} s on ${pool.ok ? `${pool.size} workers` : 'main thread'}`;
  // scrub + speed
  const scrub = $('scrub'); scrub.max = String(res.t.length - 1);
  const sp = $('speedSel'); sp.replaceChildren();
  const opts = S.xUnit === 'd' ? [[24, '1 day/s'], [48, '2 days/s'], [120, '5 days/s'], [240, '10 days/s']]
    : [[0.25, '¼ h/s'], [0.5, '½ h/s'], [1, '1 h/s'], [2, '2 h/s'], [4, '4 h/s']];
  for (const [v, l] of opts) { const o = el('option', '', l); o.value = String(v); sp.append(o); }
  sp.value = S.xUnit === 'd' ? '48' : '1'; S.speed = +sp.value;
  feedCharts();
  // Rest the playhead on the moment of largest plasma-Na change, so the first frame shows the effect.
  const na = res.q50.Na_plasma; let best = 0;
  for (let k = 0; k < na.length; k++) if (Math.abs(na[k] - na[0]) > Math.abs(na[best] - na[0])) best = k;
  setPlaying(false);
  setPlayIndex(best);
}

function selectScenario(id) {
  S.scenarioId = id; S.spec = { kind: 'named', id };
  const sc = SCENARIOS[id];
  $('scenarioDesc').textContent = sc.description + (sc.label ? ` (${sc.label})` : '');
  const ul = $('scenarioExpects').querySelector('ul'); ul.replaceChildren();
  if (sc.validation) {
    ul.append(el('li', '', sc.validation.summary));
    for (const e of sc.validation.expects || []) ul.append(el('li', '', `${e.metric}: ${e.target}`));
  }
  if (id === 'salt_load_sweep') $('dosePanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  runCurrent();
}

function customSpec() {
  const water = +$('cWater').value / 1000, salt = +$('cSalt').value, at = +$('cStart').value, dur = +$('cDur').value;
  if (salt > 0) return { kind: 'salt', args: [salt, water, dur, at, Math.max(24, at + 47)] };
  return { kind: 'water', args: [Math.max(0.0001, water), dur, at, 12] };
}
function updateCustomLabels() {
  const w = +$('cWater').value, s = +$('cSalt').value, at = +$('cStart').value, d = +$('cDur').value;
  $('cWaterOut').textContent = `${fmt(w, 0)} mL`; $('cSaltOut').textContent = `${fmt(s, 1)} g`;
  $('cStartOut').textContent = `${fmt(at, 2)} h`; $('cDurOut').textContent = `${d} min`;
  const parts = []; if (w) parts.push(`${fmt(w, 0)} mL water`); if (s) parts.push(`${fmt(s, 1)} g salt (${fmt(s * MMOL_NA_PER_G_NACL, 0)} mmol Na)`);
  $('cSummary').textContent = parts.length ? `${parts.join(' + ')} over ${d} min at ${fmt(at, 2)} h` : 'Nothing ingested beyond the baseline diet';
}

function setMode(mode) {
  S.mode = mode;
  for (const b of $('modeTabs').children) b.setAttribute('aria-selected', String(b.dataset.mode === mode));
  $('mode-scenario').hidden = mode !== 'scenario'; $('mode-custom').hidden = mode !== 'custom'; $('mode-chronic').hidden = mode !== 'chronic';
  if (mode === 'scenario') selectScenario($('scenarioSel').value);
  else if (mode === 'chronic') { S.spec = { kind: 'named', id: 'chronic_high_salt_30d' }; runCurrent(); }
  else { S.spec = customSpec(); runCurrent(); }
}

// ---------------------------------------------------------------------------
// Dose–response
function buildDose() {
  const seg = $('doseMetric');
  for (const m of DOSE_METRICS) {
    const b = el('button', '', m.label); b.type = 'button'; b.setAttribute('aria-pressed', String(m.id === S.doseMetric));
    b.addEventListener('click', () => { S.doseMetric = m.id; [...seg.children].forEach((x) => x.setAttribute('aria-pressed', String(x === b))); feedDose(); });
    seg.append(b);
  }
  S.dose = new DoseChart($('doseBox'), { xUnit: 'g', decimals: 2, floor0: true, ariaLabel: 'Salt dose versus kidney strain, median with 90% band' });
  chipPending($('doseEv'));
  $('doseEv').addEventListener('click', () => {
    const r = doseParams(); refreshChips();
    openDrawer('Salt dose response', r.list, SCENARIOS.salt_load_sweep.validation.evidence,
      'Each dose runs 48 h. Peak strain index is the highest index value after the load; the index is a model construct, not a clinical measure.' +
      (r.fallback ? '' : ` ${INF_NOTE}`));
  });
  $('doseRun').addEventListener('click', () => doSweep());
}
function feedDose() {
  if (!S.sweep) return;
  const m = DOSE_METRICS.find((x) => x.id === S.doseMetric);
  const d = S.sweep.metrics[m.id];
  S.dose.opts.decimals = m.dec; S.dose.opts.unit = m.unit;
  S.dose.setData({ values: S.sweep.values, q05: d.q05, q50: d.q50, q95: d.q95, colorVar: '--s1', name: m.label });
}
async function doSweep() {
  $('doseRun').disabled = true; S.dose.setDim(true);
  const t0 = performance.now();
  try {
    S.sweep = await runSweep({ tag: 'sweep', onProgress: (f) => { $('doseNote').textContent = `Running doses… ${Math.round(f * 100)}%`; } });
    $('doseNote').textContent = `13 doses × ${MC_N} samples · 90% band · ${fmt((performance.now() - t0) / 1000, 1)} s`;
    S.dose.setDim(false); feedDose();
  } catch (err) { console.error(err); $('doseNote').textContent = `Sweep failed: ${err.message}`; }
  $('doseRun').disabled = false;
}

// ---------------------------------------------------------------------------
// Stress test
async function stressTest() {
  const btn = $('stressBtn'); btn.disabled = true;
  const tbody = $('stressTable').querySelector('tbody'); tbody.replaceChildren();
  const spec = { kind: 'named', id: 'drink_water_1L' };
  for (const n of [64, 256, 1024]) {
    const tr = el('tr'); tr.append(el('td', '', fmt(n, 0)), el('td', '', '…'), el('td', '', ''), el('td', '', ''), el('td', '', '')); tbody.append(tr);
    let minFps = Infinity; const fpsTimer = setInterval(() => { if (S.viewer) minFps = Math.min(minFps, S.viewer.fps); }, 250);
    btn.textContent = `Running ${fmt(n, 0)}…`;
    const r = await runMC({ spec, n, keys: ['Na_plasma'], tag: 'stress', chunk: Math.max(2, Math.ceil(n / ((pool.ok ? pool.size : 1) * 4))) });
    clearInterval(fpsTimer); if (S.viewer) minFps = Math.min(minFps, S.viewer.fps);
    tr.children[1].textContent = `${fmt(r.ms / 1000, 2)} s`;
    tr.children[2].textContent = fmt(n / (r.ms / 1000), 0);
    tr.children[3].textContent = pool.ok ? `${pool.size}` : '1 (main)';
    tr.children[4].textContent = Number.isFinite(minFps) ? fmt(minFps, 0) : '–';
  }
  btn.textContent = 'Run again'; btn.disabled = false;
}

// ---------------------------------------------------------------------------
// Entity rail
function entityById(id) { return S.entities.find((e) => e.id === id); }
function partEntity(part) { return S.entities.find((e) => e.mesh === part); }
// Spec §6 scales. Index = scale number.
const SCALE_LEVELS = ['body', 'system', 'organ', 'tissue', 'cell', 'organelle', 'pathway', 'molecule'];
const SCALE_NAMES = ['Organism', 'System', 'Organ', 'Tissue', 'Cell', 'Organelle', 'Pathway', 'Molecule'];
function levelName(level) {
  return LADDER.find((l) => l.id === level)?.name || SCALE_NAMES[SCALE_LEVELS.indexOf(level)] || level;
}
/** Ladder level of an entity. Seed entities with a 3D part keep their V1 level (schematic stage
 *  or organ); KB entities with no mesh are labelled by their `scale` (BUG-0003). */
function levelOf(e) {
  if (!e) return 'body';
  if (e.mesh?.startsWith('schem:')) return e.mesh.slice(6);
  if (e.mesh || e.scale === 0) return e.scale === 0 ? 'body' : 'organ';
  return SCALE_LEVELS[e.scale] ?? 'organ';
}
/** The rung → entity chain reachable from the current entity (V1: kidney chain has sub-organ schematics). */
function chainFor(e) {
  const chain = { body: entityById('organism:body') };
  if (!e || e.scale === 0) return chain;
  let organ = e;
  while (organ && organ.parent && levelOf(organ) !== 'organ') organ = entityById(organ.parent);
  if (organ && levelOf(organ) === 'organ') chain.organ = organ;
  let cur = organ;
  for (;;) {
    const kid = S.entities.find((x) => x.parent === cur?.id && x.mesh?.startsWith('schem:'));
    if (!kid) break; chain[levelOf(kid)] = kid; cur = kid;
  }
  return chain;
}

/** Rail group for an entity: its own `system`, else the nearest ancestor's (a seed entity's
 *  system, or a KB `system:<id>` entity whose id matches a rail system); null → "Other (KB)". */
function railSystemOf(e) {
  const ids = new Set(SYSTEMS.map((s) => s.id));
  for (let cur = e, hops = 0; cur && hops < 12; cur = entityById(cur.parent), hops++) {
    if (cur.system) return cur.system;
    if (cur.id.startsWith('system:') && ids.has(cur.id.slice(7))) return cur.id.slice(7);
  }
  return null;
}
function railButton(e) {
  const b = el('button', 'ent', e.name); b.type = 'button'; b.dataset.id = e.id;
  if (e.scale > 2 || (e.scale === 1 && !e.mesh)) b.append(el('span', 'sc', SCALE_NAMES[e.scale] || ''));
  b.addEventListener('click', () => selectEntity(e.id));
  return b;
}
function buildRail() {
  const tree = $('tree');
  // KB-only entities (no seed record) are listed after the seed entities of their group,
  // ordered by scale then name.
  const kbOnly = S.entities.filter((x) => !x.mesh && x.scale !== 0 && !SEED_ENTITIES.some((s) => s.id === x.id))
    .sort((a, b) => a.scale - b.scale || a.name.localeCompare(b.name));
  const kbGroup = new Map(kbOnly.map((x) => [x.id, railSystemOf(x)]));
  for (const sys of SYSTEMS) {
    const g = el('div', 'sys');
    const head = el('div', 'sys-head'); head.append(el('span', '', sys.name));
    const sw = el('label', 'switch'); sw.title = `Show ${sys.name.toLowerCase()}`;
    const cb = el('input'); cb.type = 'checkbox'; cb.checked = true; cb.id = `sys-${sys.id}`; cb.setAttribute('aria-label', `Show ${sys.name}`);
    cb.addEventListener('change', () => S.viewer?.setSystemVisible(sys.id, cb.checked));
    sw.append(cb, el('i')); head.append(sw); g.append(head);
    for (const e of S.entities.filter((x) => x.system === sys.id && !kbGroup.has(x.id))) g.append(railButton(e));
    for (const e of kbOnly.filter((x) => kbGroup.get(x.id) === sys.id)) g.append(railButton(e));
    tree.append(g);
  }
  const other = kbOnly.filter((x) => !kbGroup.get(x.id));
  if (other.length) {
    // No 3D part and no system: collapsed by default so the info card stays in reach.
    const d = el('details', 'sys other'); d.id = 'sys-other';
    const sum = el('summary', 'sys-head'); sum.append(el('span', '', 'Other (KB)'), el('span', 'sc', `${other.length}`));
    d.append(sum);
    for (const e of other) d.append(railButton(e));
    tree.append(d);
  }
  const lad = $('ladder');
  LADDER.forEach((r, i) => {
    const b = el('button'); b.type = 'button'; b.dataset.level = r.id;
    b.append(el('span', 'rung', String(i + 1)), el('span', '', r.name), el('span', 'what', ''));
    b.addEventListener('click', () => { const e = chainFor(S.entity)[r.id]; if (e) selectEntity(e.id); });
    lad.append(b);
  });
}

function selectEntity(id) {
  const e = entityById(id); if (!e) return;
  S.entity = e;
  const level = levelOf(e);
  const chain = chainFor(e);
  // A KB entity on a ladder rung that the organ's schematic chain does not reach is shown on its own rung.
  if (LADDER.some((r) => r.id === level) && chain[level]?.id !== e.id && !e.mesh) chain[level] = e;
  for (const b of $('ladder').children) {
    const r = b.dataset.level; const target = chain[r];
    b.disabled = !target; b.setAttribute('aria-current', String(r === level));
    b.querySelector('.what').textContent = target ? target.name : '';
    b.title = target ? `Go to ${target.name}` : 'Sub-organ schematics exist for the kidney chain only in V1';
  }
  for (const b of $('tree').querySelectorAll('.ent')) b.setAttribute('aria-current', String(b.dataset.id === id));
  // The 3D view shows the entity's own part; a KB entity with no mesh shows its nearest
  // ancestor that has one (e.g. AQP2 vesicle → principal-cell schematic), else the body.
  let shown = e;
  for (let hops = 0; shown && !shown.mesh && shown.scale !== 0 && shown.parent && hops < 12; hops++) shown = entityById(shown.parent);
  const viewLevel = shown?.mesh ? levelOf(shown) : 'body';
  const isSchem = ['tissue', 'cell', 'pathway', 'molecule'].includes(viewLevel);
  $('levelChip').textContent = `${levelName(level)} · ${e.name}`;
  $('schemChip').hidden = !isSchem;
  if (S.viewer) {
    const part = shown?.mesh && !shown.mesh.startsWith('schem:') ? shown.mesh : (isSchem ? 'kidney' : null);
    S.viewer.select(viewLevel === 'body' ? null : part);
    S.viewer.setLevel(viewLevel, part);
  }
  renderKb();
}

function renderKb() {
  const e = S.entity; const kb = $('kb'); kb.replaceChildren();
  if (!e) return;
  kb.append(el('h3', '', e.name));
  kb.append(el('div', 'meta', `${e.type} · scale ${e.scale} (${LADDER.find((l) => l.scale === e.scale)?.name || SCALE_NAMES[e.scale] || 'unknown'})`));
  kb.append(el('p', '', e.summary || ''));
  const live = el('div', 'live'); live.id = 'kbLive'; kb.append(live);
  const src = el('div', 'src');
  const ids = e.evidence || [];
  src.textContent = ids.length ? `Source: ${ids.map((id) => S.evidence.get(id)?.title || id).join('; ')}` : '';
  kb.append(src);
  if (S.entitySource.includes('seed')) kb.append(el('div', 'src', 'Entity text is a V1 seed until the curated knowledge base lands.'));
  renderKbLive();
}
function renderKbLive() {
  const live = $('kbLive'); if (!live || !S.entity || !S.res) return;
  live.replaceChildren();
  // D-3: wherever an entity lists GFR, show it both ways (absolute and per 1.73 m²) plus a one-line note.
  const qs = [...new Set((S.entity.quantities || []).flatMap((k) => (k === 'GFR' ? ['GFR', 'GFR_norm'] : [k])))];
  for (const k of qs) {
    const v = med(k); if (v === undefined) continue;
    const [d, u, lab] = QFMT[k] || [2, '', k];
    live.append(el('span', '', `${lab} ${k === 'Thirst' ? fmt(v * 100, 0) : fmt(v, d)}${u ? ` ${u}` : ''}`));
  }
  if (qs.includes('GFR_norm') && med('GFR_norm') !== undefined) live.append(el('p', 'kb-note', GFR_NOTE));
}
// Decision D-3: why the two GFR readouts differ (one line, shown under the kidney/nephron live values).
const GFR_NOTE = `Absolute GFR is what this body filters; "per 1.73 m²" rescales it to a standard body size so people of different sizes can be compared. They are equal here because the model describes one ${REFERENCE_PERSON.name}, whose body surface is 1.73 m².`;

// ---------------------------------------------------------------------------
function renderFooter(evSource) {
  const entries = Object.values(TABLE);
  const good = entries.filter((e) => /^(A-|B-)/.test(e.grade)).length;
  const eCount = entries.filter((e) => /^E/.test(e.grade)).length;
  const f = $('foot'); f.replaceChildren();
  f.append(el('span', 'disclaimer', DISCLAIMER));
  const add = (label, value) => { const s = el('span'); s.append(document.createTextNode(`${label} `), el('strong', 'num', value)); f.append(s); };
  add('Model', `v${MODEL_VERSION}`);
  add('Parameters', fmt(entries.length, 0));
  add('Graded B or better', `${fmt((100 * good) / entries.length, 0)}%`);
  add('Assumed (E)', fmt(eCount, 0));
  const all = el('button', 'ev-chip', 'All parameters and sources'); all.type = 'button';
  all.addEventListener('click', () => openDrawer('Every model parameter', Object.keys(TABLE), [], `Citations from ${evSource}.`));
  f.append(all);
}

// ---------------------------------------------------------------------------
async function main() {
  // static UI first so the page is complete while workers boot
  const sel = $('scenarioSel');
  for (const [id, sc] of Object.entries(SCENARIOS)) { const o = el('option', '', sc.title); o.value = id; sel.append(o); }
  sel.value = S.scenarioId;
  sel.addEventListener('change', () => selectScenario(sel.value));
  $('chronicDesc').textContent = SCENARIOS.chronic_high_salt_30d.description;
  for (const b of $('modeTabs').children) b.addEventListener('click', () => setMode(b.dataset.mode));
  for (const id of ['cWater', 'cSalt', 'cStart', 'cDur']) $(id).addEventListener('input', updateCustomLabels);
  updateCustomLabels();
  $('mode-custom').addEventListener('submit', (e) => { e.preventDefault(); S.spec = customSpec(); runCurrent(); });
  $('playBtn').addEventListener('click', () => setPlaying(!S.playing));
  $('scrub').addEventListener('input', (e) => { setPlaying(false); setPlayIndex(+e.target.value); });
  $('speedSel').addEventListener('change', (e) => { S.speed = +e.target.value; });
  $('drawerClose').addEventListener('click', closeDrawer); $('scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('drawer').hidden) closeDrawer(); });
  $('stressBtn').addEventListener('click', stressTest);
  buildCharts(); buildDose();

  const ents = await loadEntities();
  const ev = await loadEvidence(!ents.source.includes('seed'));
  S.entities = ents.list; S.entitySource = ents.source; S.evidence = ev.map;
  renderFooter(ev.source);
  buildRail();

  // 3D viewer (WebGL may be unavailable; everything else still works)
  try {
    const { Viewer, strainStops } = await import('./viewer.js');
    applyStrainRule(strainStops());
    S.viewer = new Viewer($('view'), {
      onSelect: (part, entityId) => selectEntity(entityId || partEntity(part)?.id),
      onHover: (part) => partEntity(part)?.name || part,
    });
    S.viewer.onFps = (f) => { $('fps').textContent = `${fmt(f, 0)} fps`; };
    const bp = el('div', 'anchor'); bp.id = 'bpBadge'; S.viewer.addAnchor(bp, 'heart', { x: 0, y: 0.075, z: 0 });
    const adh = el('div', 'anchor'); adh.id = 'adhBadge'; S.viewer.addAnchor(adh, 'pituitary', { x: 0, y: -0.03, z: 0.05 });
  } catch (err) {
    console.warn('3D viewer unavailable:', err);
    const m = el('div', 'webgl-msg', 'The 3D view needs WebGL, which this browser has turned off. Charts and controls below still work.');
    $('view').append(m);
  }
  window.__mm = { S, pool, selectEntity, runMC, setPlayIndex }; // test hook
  selectEntity('organism:body');

  // workers
  const dots = $('coreDots');
  dots.replaceChildren(...Array.from({ length: cores }, () => el('i')));
  pool.onActivity = (busy) => { [...dots.children].forEach((d, i) => d.classList.toggle('on', i < busy)); };
  const ok = await pool.init();
  $('coresText').textContent = ok ? `${pool.size} workers` : 'Main thread (workers unavailable)';
  await runCurrent();
  loadInfluence();
  doSweep();
  requestAnimationFrame(tick);
}

main();
