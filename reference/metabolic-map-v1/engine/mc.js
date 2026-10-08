// engine/mc.js — Monte Carlo over parameter ranges (spec §1.2, §3 simulateMC).
//
// * Seeded PRNG: mulberry32 (32-bit state, deterministic across Node and browsers).
// * Each parameter with a non-degenerate range and `mc !== false` is sampled
//   independently: uniform on [lo, hi], or log-uniform when hi/lo > 5 (and lo > 0).
// * Parameters flagged `mc: false` (scenario conditions, classification thresholds,
//   strain-index definition constants) are held at their value.
// * Samples whose analytic steady state is infeasible, or whose baseline plasma Na
//   falls outside the normal range [na_normal_low, na_normal_high], are REJECTED and
//   counted (reported as `rejected`) — they describe no healthy adult.
// * Independence between parameters is an assumption (no correlation data in V1).

/** mulberry32 PRNG. Returns a function producing floats in [0, 1). */
export function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Is this table entry sampled, and how? Returns 'fixed' | 'uniform' | 'log-uniform'. */
export function samplingMode(entry) {
  const [lo, hi] = entry.range;
  if (entry.mc === false || !(hi > lo)) return 'fixed';
  return lo > 0 && hi / lo > 5 ? 'log-uniform' : 'uniform';
}

/** Draw one parameter set from the table using rng(). Iteration order = table key order. */
export function sampleParams(table, rng) {
  const p = {};
  for (const [k, e] of Object.entries(table)) {
    const mode = samplingMode(e);
    const [lo, hi] = e.range;
    const u = mode === 'fixed' ? 0 : rng(); // fixed entries consume no random draw
    if (mode === 'fixed') p[k] = e.value;
    else if (mode === 'uniform') p[k] = lo + (hi - lo) * u;
    else p[k] = Math.exp(Math.log(lo) + (Math.log(hi) - Math.log(lo)) * u);
  }
  return p;
}

/** Linear-interpolation quantile (R type 7) of an ascending-sorted array. */
export function quantileSorted(sorted, q) {
  const n = sorted.length;
  if (n === 0) return NaN;
  const h = (n - 1) * q;
  const lo = Math.floor(h), hi = Math.ceil(h);
  return sorted[lo] + (h - lo) * (sorted[hi] - sorted[lo]);
}

/**
 * Per-time-point quantiles across samples.
 * @param {Float64Array[]} series  one array per sample, all of equal length T
 * @param {number[]} qs            e.g. [0.05, 0.5, 0.95]
 * @returns {Float64Array[]}       one array per q
 */
export function quantileBands(series, qs) {
  const n = series.length, T = series[0].length;
  const out = qs.map(() => new Float64Array(T));
  const col = new Float64Array(n);
  for (let k = 0; k < T; k++) {
    for (let i = 0; i < n; i++) col[i] = series[i][k];
    col.sort();
    for (let j = 0; j < qs.length; j++) out[j][k] = quantileSorted(col, qs[j]);
  }
  return out;
}
