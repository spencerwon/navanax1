// app/worker.js — Monte Carlo / sweep worker (module worker).
// Every worker redraws the same seeded parameter samples with drawSamples({n, seed})
// and simulates only its slice [i0, i1). The main thread merges slices in sample
// order, so pooled results equal simulateMC()/simulateSweep() exactly.
// EDUCATIONAL MODEL — NOT MEDICAL ADVICE.

import {
  simulate, simulateSweep, drawSamples, makeScenario, makeWaterLoad, makeSaltLoad, SCENARIOS, computeInfluence, computeInfluenceAll,
} from '../engine/index.js';

const sampleCache = new Map(); // `${n}:${seed}` -> samples

function samplesFor(n, seed) {
  const key = `${n}:${seed}`;
  if (!sampleCache.has(key)) {
    if (sampleCache.size > 4) sampleCache.clear();
    sampleCache.set(key, drawSamples({ n, seed }).samples);
  }
  return sampleCache.get(key);
}

/** Rebuild a scenario from a structured-clone-safe spec (functions can't cross postMessage).
 *  Keep in sync with scenarioFromSpec() in ui.js. */
function scenarioFromSpec(spec) {
  if (spec.kind === 'named') return SCENARIOS[spec.id];
  if (spec.kind === 'water') return makeWaterLoad(...spec.args);
  if (spec.kind === 'salt') return makeSaltLoad(...spec.args);
  return makeScenario({
    id: spec.id || 'custom', title: spec.title || 'Custom intervention', description: spec.description || '',
    tEnd: spec.tEnd, dt: spec.dt ?? 1 / 60, events: spec.events || [], overrides: spec.overrides || [],
  });
}

self.onmessage = (ev) => {
  const msg = ev.data;
  const t0 = performance.now();
  try {
    if (msg.type === 'mc') {
      const samples = samplesFor(msg.n, msg.seed);
      const sc = scenarioFromSpec(msg.scenario);
      const out = Object.fromEntries(msg.keys.map((k) => [k, []]));
      let t = null;
      for (let i = msg.i0; i < msg.i1; i++) {
        const r = simulate({ params: samples[i], scenario: sc, dt: msg.dt, outEvery: msg.outEvery });
        t = r.t;
        for (const k of msg.keys) out[k].push(r.states[k] || r.derived[k]);
      }
      // Pack each key as one flat buffer (slice-major) for zero-copy transfer.
      const T = t ? t.length : 0;
      const packed = {}; const transfer = [];
      for (const k of msg.keys) {
        const buf = new Float64Array((msg.i1 - msg.i0) * T);
        out[k].forEach((arr, j) => buf.set(arr, j * T));
        packed[k] = buf; transfer.push(buf.buffer);
      }
      const tt = Float64Array.from(t || []);
      transfer.push(tt.buffer);
      self.postMessage({ id: msg.id, ok: true, t: tt, T, count: msg.i1 - msg.i0, data: packed,
        ms: performance.now() - t0 }, transfer);
    } else if (msg.type === 'sweep') {
      const r = simulateSweep({ n: msg.n, seed: msg.seed, values: msg.values });
      self.postMessage({ id: msg.id, ok: true, values: r.values, metrics: r.metrics, rejected: r.rejected,
        ms: performance.now() - t0 });
    } else if (msg.type === 'influence') {
      // Default sensitivity screen (audit F-01): one scenario, 24 h, ~53 runs; fills the evidence
      // chips at first paint until the union below arrives (BUG-20261003-178).
      self.postMessage({ id: msg.id, ok: true, result: computeInfluence(), ms: performance.now() - t0 });
    } else if (msg.type === 'influenceAll') {
      // Influence union behind the evidence chips (HREQ-U-12); 805 runs, about 24 s, once per page load.
      self.postMessage({ id: msg.id, ok: true, result: computeInfluenceAll(), ms: performance.now() - t0 });
    } else if (msg.type === 'ping') {
      self.postMessage({ id: msg.id, ok: true });
    }
  } catch (err) {
    self.postMessage({ id: msg.id, ok: false, error: String(err && err.stack || err) });
  }
};
