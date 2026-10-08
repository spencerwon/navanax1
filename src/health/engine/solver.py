"""Classical 4th-order Runge-Kutta, fixed step, event-safe (port of engine/solver.js).

Design (identical to the JavaScript reference; see its header):
 * The caller asks for an output grid t0, t0+dt, ..., tEnd.
 * Each output interval is split at every scenario BREAKPOINT that falls strictly
   inside it, so no RK4 step ever straddles a discontinuity in the inputs
   (e.g. the start/stop of drinking). Inputs are piecewise-constant between
   breakpoints; they are evaluated once per segment at the segment MIDPOINT and
   held constant across all four RK4 stages. This makes intake integrals exact.
 * Each segment is further divided into m equal sub-steps no longer than `max_step`,
   m = max(1, ceil(len / max_step - 1e-12)), which keeps the explicit scheme stable
   even when a coarse output dt is requested (the 30-day chronic scenario).
 * Linear invariants (water and Na mass balance including the cumulative ledger)
   are preserved by RK4 to floating-point round-off.

Every arithmetic expression keeps the JavaScript operation order, so the port
reproduces the reference to round-off (the self-test holds it to 1e-9 relative).
State vectors are plain Python lists of floats.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from typing import Any

# deriv(t, z, inputs, dz_out) writes dz/dt into dz_out (a list of len(z)).
Deriv = Callable[[float, list[float], Any, list[float]], None]


def workspace(n: int) -> dict[str, list[float]]:
    """Reusable RK4 stage buffers {k1, k2, k3, k4, tmp}, each a list of n floats."""
    return {"k1": [0.0] * n, "k2": [0.0] * n, "k3": [0.0] * n, "k4": [0.0] * n,
            "tmp": [0.0] * n}


def rk4_step(deriv: Deriv, t: float, z: list[float], h: float, inputs: Any,
             ws: dict[str, list[float]]) -> None:
    """One RK4 step of size h for dz/dt = deriv(t, z, inputs, dz_out); updates z in place.

    The stage products are written `(0.5 * h) * k` and `(h / 6) * (...)` exactly as the
    JavaScript `0.5 * h * k1[i]` and `(h / 6) * (k1[i] + 2 * k2[i] + 2 * k3[i] + k4[i])`
    evaluate (left to right), so the result is bit-identical.
    """
    n = len(z)
    k1, k2, k3, k4, tmp = ws["k1"], ws["k2"], ws["k3"], ws["k4"], ws["tmp"]
    r = range(n)
    hh = 0.5 * h
    deriv(t, z, inputs, k1)
    tmp[:] = [z[i] + hh * k1[i] for i in r]
    deriv(t + 0.5 * h, tmp, inputs, k2)
    tmp[:] = [z[i] + hh * k2[i] for i in r]
    deriv(t + 0.5 * h, tmp, inputs, k3)
    tmp[:] = [z[i] + h * k3[i] for i in r]
    deriv(t + h, tmp, inputs, k4)
    h6 = h / 6
    z[:] = [z[i] + h6 * (k1[i] + 2 * k2[i] + 2 * k3[i] + k4[i]) for i in r]


def _js_round(x: float) -> int:
    """JavaScript Math.round (halves round toward +infinity), not Python's banker's round."""
    return math.floor(x + 0.5)


def integrate(*, deriv: Deriv, z0: Sequence[float], t_end: float, dt: float,
              inputs_at: Callable[[float], Any], t0: float = 0.0, out_every: float = 1,
              breakpoints: Iterable[float] | None = None,
              max_step: float | None = None) -> dict[str, Any]:
    """Integrate from t0 to t_end on the output grid t0, t0+dt, ..., t_end.

    Args mirror `integrate(o)` in solver.js: `out_every` records every k-th grid point
    (the final point is always recorded); `breakpoints` are times where inputs change
    discontinuously; `inputs_at(t)` is the piecewise-constant input schedule;
    `max_step` is the maximum internal step (None or <= 0 means unlimited).

    Returns {"t": [...], "Z": [[...], ...], "steps": int} with Z[k] the state at t[k].
    Raises ValueError unless dt > 0 and t_end > t0 (NaN fails both, as in JS).
    """
    # JS `Math.max(1, Math.round(o.outEvery || 1))`: 0, None and NaN all mean 1.
    oe = out_every if (out_every and out_every == out_every) else 1
    out_every_i = max(1, _js_round(oe))
    max_step_f = max_step if (max_step is not None and max_step > 0) else math.inf
    if not (dt > 0) or not (t_end > t0):
        raise ValueError("integrate: need dt > 0 and tEnd > t0")
    bps = sorted(b for b in (breakpoints or ()) if b > t0 and b < t_end)

    N = math.ceil((t_end - t0) / dt - 1e-9)           # number of grid intervals

    def grid_t(k: int) -> float:
        return t_end if k >= N else t0 + k * dt

    z = [float(v) for v in z0]
    ws = workspace(len(z))
    t_out: list[float] = [t0]
    Z: list[list[float]] = [list(z)]
    steps = 0

    bi = 0
    nb = len(bps)
    for k in range(N):
        ta, tb = grid_t(k), grid_t(k + 1)
        # segment boundaries: ta, breakpoints in (ta, tb), tb
        s0 = ta
        while bi < nb and bps[bi] <= ta:
            bi += 1
        j = bi
        while True:
            s1 = bps[j] if (j < nb and bps[j] < tb) else tb
            seg = s1 - s0
            if seg > 0:
                inputs = inputs_at(0.5 * (s0 + s1))
                m = max(1, math.ceil(seg / max_step_f - 1e-12))
                h = seg / m
                for i in range(m):
                    rk4_step(deriv, s0 + i * h, z, h, inputs, ws)
                    steps += 1
            if s1 >= tb:
                break
            s0 = s1
            j += 1
        if (k + 1) % out_every_i == 0 or k + 1 == N:
            t_out.append(tb)
            Z.append(list(z))
    return {"t": t_out, "Z": Z, "steps": steps}
