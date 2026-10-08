"""Monte Carlo over parameter ranges (port of engine/mc.js; spec §1.2, §3 simulateMC).

* Seeded PRNG: mulberry32 (32-bit state, deterministic across Node, browsers and this port:
  the 32-bit integer semantics of `>>>`, `^`, `|` and Math.imul are emulated exactly).
* Each parameter with a non-degenerate range and `mc` not false is sampled
  independently: uniform on [lo, hi], or log-uniform when hi/lo > 5 (and lo > 0).
* Parameters flagged `mc: false` (scenario conditions, classification thresholds,
  strain-index definition constants) are held at their value and consume no draw.
* Samples whose analytic steady state is infeasible, or whose baseline plasma Na
  falls outside the normal range [na_normal_low, na_normal_high], are REJECTED and
  counted (reported as `rejected`) -- they describe no healthy adult (api.draw_samples).
* Independence between parameters is an assumption (no correlation data in V1).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

__all__ = [
    "LOG_UNIFORM_RATIO", "imul", "mulberry32", "quantile_bands", "quantile_sorted", "sample_params", "sampling_mode",
]

_U32 = 0xFFFFFFFF

#: hi/lo above this (with lo > 0) -> log-uniform sampling (mc.js samplingMode; mirrored in
#: config/health/base.yaml monte_carlo.log_uniform_ratio, checked by the self-test).
LOG_UNIFORM_RATIO = 5


def _to_uint32(x: float) -> int:
    """ECMAScript ToUint32 (what `x >>> 0` does)."""
    if isinstance(x, float):
        if not math.isfinite(x):
            return 0
        x = int(x)              # truncation toward zero, as ToUint32 does
    return x & _U32


def _to_int32(x: int) -> int:
    """ECMAScript ToInt32 of an integer (wrap to [-2**31, 2**31))."""
    x &= _U32
    return x - 0x100000000 if x & 0x80000000 else x


def imul(a: int, b: int) -> int:
    """Math.imul: the low 32 bits of a*b as a SIGNED 32-bit integer."""
    return _to_int32((a & _U32) * (b & _U32))


def mulberry32(seed: float) -> Callable[[], float]:
    """mulberry32 PRNG. Returns a function producing floats in [0, 1).

    Line-by-line transcription of mc.js; every JavaScript bitwise operator converts its
    operands with ToInt32 (or ToUint32 for `>>>`), which `_to_int32`/`_to_uint32` emulate.
    """
    a = _to_uint32(seed)                                  # let a = seed >>> 0;

    def next_() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & _U32                       # a = (a + 0x6D2B79F5) >>> 0;
        t = a                                             # let t = a;
        # t = Math.imul(t ^ (t >>> 15), t | 1);
        t = imul(_to_int32(t ^ (_to_uint32(t) >> 15)), _to_int32(t | 1))
        # t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        t = _to_int32(t ^ _to_int32(t + imul(_to_int32(t ^ (_to_uint32(t) >> 7)),
                                             _to_int32(t | 61))))
        # return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        return _to_uint32(t ^ (_to_uint32(t) >> 14)) / 4294967296

    return next_


def sampling_mode(entry: Mapping[str, Any]) -> str:
    """Is this table entry sampled, and how? Returns 'fixed' | 'uniform' | 'log-uniform'."""
    lo, hi = entry["range"]
    if entry.get("mc") is False or not (hi > lo):
        return "fixed"
    return "log-uniform" if lo > 0 and hi / lo > LOG_UNIFORM_RATIO else "uniform"


def sample_params(table: Mapping[str, Mapping[str, Any]], rng: Callable[[], float]) -> dict[str, Any]:
    """Draw one parameter set from the table using rng(). Iteration order = table key order."""
    p: dict[str, Any] = {}
    for k, e in table.items():
        mode = sampling_mode(e)
        lo, hi = e["range"]
        u = 0 if mode == "fixed" else rng()  # fixed entries consume no random draw
        if mode == "fixed":
            p[k] = e["value"]
        elif mode == "uniform":
            p[k] = lo + (hi - lo) * u
        else:
            p[k] = math.exp(math.log(lo) + (math.log(hi) - math.log(lo)) * u)
    return p


def quantile_sorted(sorted_: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (R type 7) of an ascending-sorted sequence."""
    n = len(sorted_)
    if n == 0:
        return math.nan
    h = (n - 1) * q
    lo, hi = math.floor(h), math.ceil(h)
    return sorted_[lo] + (h - lo) * (sorted_[hi] - sorted_[lo])


def _sort_like_float64array(col: list[float]) -> list[float]:
    """Float64Array.prototype.sort(): numeric ascending, NaN last.

    Python's sort leaves NaN wherever comparisons happen to put it, so NaNs are
    split off first. (-0 and +0 compare equal here; JS orders -0 first. Either order
    gives the same quantile value.)
    """
    finite = [x for x in col if x == x]
    if len(finite) == len(col):
        finite.sort()
        return finite
    finite.sort()
    return finite + [x for x in col if x != x]


def quantile_bands(series: Sequence[Sequence[float]], qs: Sequence[float]) -> list[list[float]]:
    """Per-time-point quantiles across samples: one list per q, each of the series' length."""
    n, T = len(series), len(series[0])
    out = [[0.0] * T for _ in qs]
    for k in range(T):
        col = _sort_like_float64array([series[i][k] for i in range(n)])
        for j, q in enumerate(qs):
            out[j][k] = quantile_sorted(col, q)
    return out
