// engine/solver.js — classical 4th-order Runge–Kutta, fixed step, event-safe.
//
// Design (see README "Solver"):
//  * The caller asks for an output grid t0, t0+dt, ..., tEnd.
//  * Each output interval is split at every scenario BREAKPOINT that falls strictly
//    inside it, so no RK4 step ever straddles a discontinuity in the inputs
//    (e.g. the start/stop of drinking). Inputs are piecewise-constant between
//    breakpoints; they are evaluated once per segment at the segment MIDPOINT and
//    held constant across all four RK4 stages. This makes intake integrals exact.
//  * Each segment is further divided into equal sub-steps no longer than `maxStep`
//    (the model supplies maxStep from its fastest time constant), which keeps the
//    explicit scheme stable even when a coarse output dt is requested
//    (e.g. the 30-day chronic scenario). This is the "adaptive wrapper": the step
//    adapts to stiffness and events, not to an error estimate.
//  * Linear invariants (water and Na mass balance including the cumulative
//    ledger) are preserved by RK4 to floating-point round-off.

/**
 * One RK4 step of size h for dz/dt = deriv(t, z, inputs, dzOut).
 * ws: workspace {k1,k2,k3,k4,tmp} of Float64Arrays of length n (reused, no allocation).
 */
export function rk4Step(deriv, t, z, h, inputs, ws) {
  const n = z.length;
  const { k1, k2, k3, k4, tmp } = ws;
  deriv(t, z, inputs, k1);
  for (let i = 0; i < n; i++) tmp[i] = z[i] + 0.5 * h * k1[i];
  deriv(t + 0.5 * h, tmp, inputs, k2);
  for (let i = 0; i < n; i++) tmp[i] = z[i] + 0.5 * h * k2[i];
  deriv(t + 0.5 * h, tmp, inputs, k3);
  for (let i = 0; i < n; i++) tmp[i] = z[i] + h * k3[i];
  deriv(t + h, tmp, inputs, k4);
  for (let i = 0; i < n; i++) z[i] += (h / 6) * (k1[i] + 2 * k2[i] + 2 * k3[i] + k4[i]);
}

export function workspace(n) {
  return { k1: new Float64Array(n), k2: new Float64Array(n), k3: new Float64Array(n),
    k4: new Float64Array(n), tmp: new Float64Array(n) };
}

/**
 * Integrate from t0 to tEnd.
 * @param {object} o
 * @param {(t:number, z:Float64Array, inputs:object, dz:Float64Array)=>void} o.deriv
 * @param {Float64Array} o.z0          initial augmented state (copied)
 * @param {number} o.t0                start time (h)
 * @param {number} o.tEnd              end time (h)
 * @param {number} o.dt                output grid spacing (h)
 * @param {number} [o.outEvery=1]      record every k-th grid point (final point always recorded)
 * @param {number[]} [o.breakpoints]   times where inputs change discontinuously
 * @param {(t:number)=>object} o.inputsAt  piecewise-constant input schedule
 * @param {number} [o.maxStep=Infinity] maximum internal step (h)
 * @returns {{ t: Float64Array, Z: Float64Array[], steps: number }}  Z[k] = state at t[k]
 */
export function integrate(o) {
  const { deriv, t0 = 0, tEnd, dt, inputsAt } = o;
  const outEvery = Math.max(1, Math.round(o.outEvery || 1));
  const maxStep = o.maxStep > 0 ? o.maxStep : Infinity;
  if (!(dt > 0) || !(tEnd > t0)) throw new Error('integrate: need dt > 0 and tEnd > t0');
  const bps = (o.breakpoints || []).filter((b) => b > t0 && b < tEnd).sort((a, b) => a - b);

  const N = Math.ceil((tEnd - t0) / dt - 1e-9);           // number of grid intervals
  const gridT = (k) => (k >= N ? tEnd : t0 + k * dt);
  const z = Float64Array.from(o.z0);
  const ws = workspace(z.length);
  const tOut = [];
  const Z = [];
  let steps = 0;
  tOut.push(t0); Z.push(Float64Array.from(z));

  let bi = 0;
  for (let k = 0; k < N; k++) {
    const ta = gridT(k), tb = gridT(k + 1);
    // segment boundaries: ta, breakpoints in (ta, tb), tb
    let s0 = ta;
    while (bi < bps.length && bps[bi] <= ta) bi++;
    let j = bi;
    for (;;) {
      const s1 = j < bps.length && bps[j] < tb ? bps[j] : tb;
      const len = s1 - s0;
      if (len > 0) {
        const inputs = inputsAt(0.5 * (s0 + s1));
        const m = Math.max(1, Math.ceil(len / maxStep - 1e-12));
        const h = len / m;
        for (let i = 0; i < m; i++) { rk4Step(deriv, s0 + i * h, z, h, inputs, ws); steps++; }
      }
      if (s1 >= tb) break;
      s0 = s1; j++;
    }
    if ((k + 1) % outEvery === 0 || k + 1 === N) { tOut.push(tb); Z.push(Float64Array.from(z)); }
  }
  return { t: Float64Array.from(tOut), Z, steps };
}
