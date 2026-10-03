"""Standard-library-only self-test for the health engine (Metabolic Map V1, Python port).

Runnable with a bare `python3 tests/health_selftest.py` -- no pytest, no numpy, no Node.
It proves the pure-Python engine in src/health/engine reproduces the JavaScript reference
(reference/metabolic-map-v1/engine) to 1e-9 relative on the golden fixture
tests/fixtures/health/golden_v1.json, which tools/health_golden.mjs generates from the
JavaScript (HREQ-P-02), and it checks the model's own invariants: the analytic steady
state, water / sodium / potassium mass balance, and the literature-expectation harness.

Conventions are those of tests/selftest.py: `check()` records PASS/FAIL per assertion,
every module-level `test_*` function is discovered and run in definition order, a test
needing a third-party module declares it with `@needs(...)` and is SKIPPED (by name,
never counted as passed) when it is absent, and `--no-skips` refuses to exit 0 if anything
was skipped. Exit code 0 only when nothing failed. Unlike tests/selftest.py, an exception
escaping a test is recorded as a FAIL of that test and the run continues, so one broken
test cannot hide the result of the others.

Monte Carlo tests use small n (8 samples for bands, 4 for the dose sweep) to keep the
whole file well under two minutes in pure Python (HREQ-N-04); the golden fixture was
generated with the same n and seeds, so small n loses no equivalence power.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import inspect
import json
import math
import os
import re
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from health import __version__ as HEALTH_VERSION  # noqa: E402
from health.engine import (  # noqa: E402
    DERIVED_KEYS,
    DISCLAIMER,
    IDX,
    INFLUENCE_DEFAULTS,
    LEDGER_KEYS,
    LOG_UNIFORM_RATIO,
    MAX_STEP_FRACTION_OF_TAU_MIN,
    MAX_TRIES_PER_SAMPLE,
    MMOL_NA_PER_G_NACL,
    MODEL_VERSION,
    REFERENCE_PERSON,
    SCENARIOS,
    STATE_KEYS,
    VALIDATION_STATUS,
    baseline_inputs,
    compute_influence,
    constants,
    default_params,
    derived,
    draw_samples,
    fluxes,
    imul,
    initial_state,
    make_salt_load,
    make_water_load,
    mulberry32,
    nearest_index,
    param_summary,
    param_table,
    quantile_bands,
    rhs,
    salt_load_metrics,
    sampling_mode,
    simulate,
    simulate_mc,
    simulate_sweep,
)
from health.engine import api as api_module  # noqa: E402
from health.engine import params as params_module  # noqa: E402
from health.engine import solver as solver_module  # noqa: E402
from health.engine.scenarios import PY_ONLY_EXPECTATION_KEYS  # noqa: E402
from health.engine.solver import rk4_step, workspace  # noqa: E402
from health.engine.validate import (  # noqa: E402
    COUNTABLE_KINDS,
    METRICS,
    NOT_CHECKED_KINDS,
    evaluate_expectations,
    max_relative_state_drift,
    monte_carlo_runs,
    recovery_time_variants,
    summarize,
)
from health.errors import (  # noqa: E402
    ConfigurationError,
    HealthError,
    InfeasibleParametersError,
    NonFiniteTrajectoryError,
)

GOLDEN_PATH = ROOT / "tests" / "fixtures" / "health" / "golden_v1.json"
REFERENCE_ENGINE = ROOT / "reference" / "metabolic-map-v1" / "engine"
REFERENCE_PARAMS = REFERENCE_ENGINE / "params.json"
HEALTH_CONFIG = ROOT / "config" / "health" / "base.yaml"
REGENERATE = "/opt/node22/bin/node tools/health_golden.mjs"

# Equivalence tolerance (HREQ-P-02): |py - js| <= max(REL_TOL * max(|py|, |js|), ABS_TOL).
# ABS_TOL only matters for values near zero (strain components, free-water clearance),
# where round-off of O(1) intermediates leaves ~1e-16 absolute noise.
REL_TOL = 1e-9
ABS_TOL = 1e-12
SAMPLE_REL_TOL = 1e-12        # drawn parameter sets: one exp/log per value, nothing to amplify
STEADY_STATE_DRIFT_MAX = 1e-6  # baseline.validation in scenarios.js: max |Δstate| / scale < 1e-6
MASS_BALANCE_TOL = 1e-9        # relative to the size of the quantities balanced (see the test)
# docs/health/01_METHODOLOGY.md §7.2: V1 registers 24 expectations, by kind.
V1_EXPECTATION_KINDS = {"quantitative": 11, "semi-quantitative": 1, "qualitative": 8,
                        "design-target": 1, "known-divergence": 1, "unverified": 1,
                        "numerical": 1}
# Registered literature expectations the V1 model FAILS with default parameters, in BOTH
# implementations. A fail is a finding, not a bug in the port: the harness must report it
# as a counted fail (never silently), and the range must not be widened to admit it
# (docs/health/01 §7.4). The measured value is pinned to the JavaScript reference by the
# fixture's `findings` section; if the model changes so that it passes, this entry must go.
LITERATURE_FAILS = {
    ("drink_water_1L", "time to recover |ΔNa| < 0.5 mmol/L after drinking starts"):
        "default params recover 7.05 h after drinking starts (|ΔNa| = 0.575 mmol/L at +6 h); "
        "Monte Carlo in-range share 0.32 at n = 256 (JavaScript reference)",
}

PASS: list[str] = []
FAIL: list[str] = []
SKIPPED: list[tuple[str, tuple[str, ...]]] = []   # (test name, the modules that were missing)
#: Set by `--robust`: run the slow gates too (docs/health/03 §3.1 n = 256 drift, §3.4
#: convergence on every scenario and the stiff corner). Off by default to keep the suite
#: well under a minute (HREQ-N-04); the summary line says whether they ran.
ROBUST = False


def robust(fn):
    """Mark a test as a slow gate, run only under `--robust` (and then counted like any
    other). Without the flag it is not run and the summary line says so, by count."""
    fn.robust = True
    return fn


def needs(*modules: str):
    """Declare the third-party modules a test cannot run without (see tests/selftest.py).

    A declared test is recorded as SKIPPED when a module is absent -- by name, with the
    module named -- and never counted as passed. None of the engine tests need one today;
    the declaration is kept so a future optional dependency cannot become a silent hole.
    """
    if not modules:
        raise ValueError("needs() requires at least one module name")

    def deco(fn):
        prior = getattr(fn, "needs_modules", ())
        fn.needs_modules = tuple(dict.fromkeys(prior + tuple(modules)))
        return fn

    return deco


_IMPORTABLE: dict[str, bool] = {}


def module_available(name: str) -> bool:
    """True if `name` can actually be imported here. Importing is the only honest test."""
    if name not in _IMPORTABLE:
        try:
            importlib.import_module(name)
            _IMPORTABLE[name] = True
        except ImportError:
            _IMPORTABLE[name] = False
    return _IMPORTABLE[name]


def needs_binary(name: str, *paths: str):
    """Declare an executable a test cannot run without (`name` on PATH, or one of `paths`).
    Recorded as SKIPPED by name when absent, like a missing module; `--no-skips` makes the
    absence a failure, so the gate and CI never pass by skipping it."""
    def deco(fn):
        fn.needs_binaries = getattr(fn, "needs_binaries", ()) + ((name, paths),)
        return fn
    return deco


def binary_available(name: str, paths: tuple[str, ...]) -> str | None:
    import shutil
    for cand in paths:
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which(name)


def missing_modules(fn) -> tuple[str, ...]:
    """The modules (and binaries) `fn` declared that are not available here. Empty tuple =
    run it."""
    missing = [m for m in getattr(fn, "needs_modules", ()) if not module_available(m)]
    missing += [f"{name} (binary)" for name, paths in getattr(fn, "needs_binaries", ())
                if binary_available(name, paths) is None]
    return tuple(missing)


def check(name: str, cond: bool, detail: str = "") -> None:
    suffix = f" -- {detail}" if detail and not cond else ""
    PASS.append(name) if cond else FAIL.append(f"{name}{suffix}")
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"\n        {detail}" if detail and not cond else ""))


def note(text: str) -> None:
    """Informational line (not a check): what was measured, for the reader of the log."""
    print(f"      {text}")


# ---------------------------------------------------------------------------
# Golden fixture and comparison helpers.
# ---------------------------------------------------------------------------
_GOLDEN: dict[str, Any] | None = None


def _decode_nonfinite(obj: dict[str, Any]) -> Any:
    """tools/health_golden.mjs writes NaN/±Infinity as {"$float": "..."}."""
    if len(obj) == 1 and "$float" in obj:
        return float(obj["$float"])
    return obj


def golden() -> dict[str, Any]:
    global _GOLDEN
    if _GOLDEN is None:
        if not GOLDEN_PATH.exists():
            raise FileNotFoundError(f"{GOLDEN_PATH} is missing; regenerate with: {REGENERATE}")
        _GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"),
                             object_hook=_decode_nonfinite)
    return _GOLDEN


def finding_record(scenario_id: str, metric: str) -> dict[str, Any]:
    """The fixture's JavaScript-side measurement of a failing expectation (section l)."""
    return golden()["findings"][f"{scenario_id}: {metric}"]


def err_ratio(a: float, e: float, rel: float = REL_TOL, abs_: float = ABS_TOL) -> float:
    """|a - e| as a multiple of the allowed tolerance: <= 1 passes, 0 is bit-identical."""
    if a == e or (a != a and e != e):
        return 0.0
    if not (math.isfinite(a) and math.isfinite(e)):
        return math.inf
    allowed = max(rel * max(abs(a), abs(e)), abs_)
    return abs(a - e) / allowed if allowed > 0 else math.inf


class Worst:
    """Tracks the worst comparison of a group so a failure names the key and index."""

    def __init__(self) -> None:
        self.ratio = 0.0
        self.where = ""
        self.n = 0
        self.identical = 0

    def add(self, where: str, a: float, e: float, rel: float = REL_TOL,
            abs_: float = ABS_TOL) -> None:
        r = err_ratio(a, e, rel, abs_)
        self.n += 1
        self.identical += r == 0.0
        if not (r <= self.ratio):          # NaN counts as worst
            self.ratio = r
            self.where = f"{where}: python={a!r} javascript={e!r}"

    @property
    def ok(self) -> bool:
        return self.ratio <= 1.0

    def summary(self) -> str:
        return (f"{self.n} values, {self.identical} bit-identical, worst = "
                f"{self.ratio:.3g} x tolerance" + (f" at {self.where}" if self.where else ""))


def first_difference(a: Any, b: Any, path: str = "") -> str | None:
    """Path and values of the first structural difference between two JSON-like values."""
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        if set(a) != set(b):
            return f"{path or '<root>'}: keys differ: python-only {sorted(set(a) - set(b))}, " \
                   f"javascript-only {sorted(set(b) - set(a))}"
        for k in a:
            d = first_difference(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if (isinstance(a, Sequence) and not isinstance(a, str)
            and isinstance(b, Sequence) and not isinstance(b, str)):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            d = first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if a != b and not (a != a and b != b):
        return f"{path}: python={a!r} javascript={b!r}"
    return None


_SIM_CACHE: dict[tuple[str, float | None, int | None], dict[str, Any]] = {}
# The 30-day scenario is recorded every 3rd grid point (993 points, as in the fixture); the
# RK4 step count (41,664) does not depend on it.
RUN_OUT_EVERY = {"chronic_high_salt_30d": 3}


def sim(scenario_id: str, dt: float | None = None, out_every: int | None = None) -> dict[str, Any]:
    """Default-parameter run, memoised across tests (results are never mutated)."""
    oe = out_every if out_every is not None else RUN_OUT_EVERY.get(scenario_id)
    key = (scenario_id, dt, oe)
    if key not in _SIM_CACHE:
        _SIM_CACHE[key] = simulate(scenario=scenario_id, dt=dt, out_every=oe)
    return _SIM_CACHE[key]


_STIFF: dict[str, dict[str, Any]] = {}


def stiff_corner_params() -> dict[str, Any]:
    """docs/health/03 §3.5: fastest time constants at their low ends, slowest at its high end."""
    return default_params() | golden()["stiffCorner"]["overrides"]


def stiff_corner_run(scenario_id: str) -> dict[str, Any]:
    """A scenario at the stiff corner, recorded as in the fixture (memoised; the 30-day run
    takes ~100k RK4 steps)."""
    if scenario_id not in _STIFF:
        g = golden()["stiffCorner"]["runs"][scenario_id]
        _STIFF[scenario_id] = simulate(params=stiff_corner_params(), scenario=scenario_id,
                                       dt=g["dt"], out_every=g["outEvery"])
    return _STIFF[scenario_id]


def compare_trajectory(r: Mapping[str, Any], g: Mapping[str, Any], label: str) -> Worst:
    w = Worst()
    idx = g["indices"]
    for i, k in enumerate(idx):
        w.add(f"{label} t[{k}]", r["t"][k], g["t"][i])
    for section in ("states", "derived", "ledger"):
        for key, vals in g[section].items():
            series = r[section][key]
            for i, k in enumerate(idx):
                w.add(f"{label} {section}.{key}[{k}] (t={g['t'][i]:.6g} h)", series[k], vals[i])
    return w


# ---------------------------------------------------------------------------
# Golden equivalence: Python port vs JavaScript reference.
# ---------------------------------------------------------------------------
def test_params_json_is_byte_identical_to_reference():
    ours = Path(params_module.__file__).parent / "params.json"
    check("params: src/health/engine/params.json exists", ours.exists(), str(ours))
    check("params: reference/metabolic-map-v1/engine/params.json exists", REFERENCE_PARAMS.exists(),
          str(REFERENCE_PARAMS))
    if ours.exists() and REFERENCE_PARAMS.exists():
        check("params: the Python engine reads a byte-identical copy of the JS parameter table",
              ours.read_bytes() == REFERENCE_PARAMS.read_bytes(),
              "copy reference/metabolic-map-v1/engine/params.json over src/health/engine/params.json")
    tab = param_table()
    p = default_params()
    check("params: 54 parameters, default_params() in table key order",
          list(p) == list(tab) and len(p) == 54, f"{len(p)} params")
    check("params: default_params() values are the table values",
          all(p[k] == tab[k]["value"] for k in tab))
    p["V_ecf_0"] = -1.0
    tab["V_ecf_0"]["range"][0] = -1.0
    check("params: default_params()/param_table() hand out copies (mutation does not leak)",
          default_params()["V_ecf_0"] == 14.0 and param_table()["V_ecf_0"]["range"][0] == 12.0)
    g = golden()
    d = first_difference({k: v for k, v in param_summary().items() if k != "meta"},
                         g["paramSummary"])
    check("params: param_summary() matches JS paramSummary() (24 of 54 graded >= B)",
          d is None, d or "")
    check("version: MODEL_VERSION and DISCLAIMER match the reference that wrote the fixture",
          MODEL_VERSION == g["modelVersion"] == "1.0.1" and DISCLAIMER == g["disclaimer"],
          f"python {MODEL_VERSION!r}, fixture {g['modelVersion']!r}")
    check("version: health package version is 0.1.0", HEALTH_VERSION == "0.1.0", HEALTH_VERSION)


def test_reference_engine_files_match_the_fixture_hashes():
    """The fixture header records the SHA-256 of every reference engine file it was computed
    from. A mismatch means the reference changed and the fixture is stale (regenerate it
    with a MODEL_VERSION bump, HREQ-X-05) -- or someone edited the vendored reference."""
    recorded = golden()["engineSha256"]
    on_disk = {f"reference/metabolic-map-v1/engine/{f.name}": hashlib.sha256(f.read_bytes()).hexdigest()
               for f in sorted(REFERENCE_ENGINE.iterdir())
               if f.suffix == ".js" or f.name == "params.json"}
    check("fixture: records a SHA-256 for every reference engine .js file and params.json",
          set(recorded) == set(on_disk) and len(recorded) >= 7, f"{sorted(recorded)}")
    stale = [k for k in recorded if on_disk.get(k) != recorded[k]]
    check("fixture: every reference engine file on disk has the hash the fixture was made from",
          not stale, f"changed since the fixture was generated: {stale}; regenerate with "
          f"{REGENERATE}")
    ours = hashlib.sha256((Path(params_module.__file__).parent / "params.json").read_bytes()).hexdigest()
    check("fixture: the Python engine's params.json has the reference params.json hash",
          ours == recorded.get("reference/metabolic-map-v1/engine/params.json"), ours)


@needs_binary("node", "/opt/node22/bin/node")
def test_golden_check_ignores_the_recorded_node_version_but_not_a_value():
    """BUG-20261003-171: CI on Node 22.23 read a fixture generated under 22.22 as stale,
    because `tools/health_golden.mjs --check` compared whole files and the header records
    `process.version`. The recorded Node version is provenance, not a value: Node 20.20,
    22.22 and 22.23 regenerate the fixture byte-identically apart from that line. Planted
    on a copy (HEALTH_GOLDEN_OUT): a different recorded version is up to date; one changed
    value is stale, and the first differing line is named."""
    import re
    import subprocess
    import tempfile
    node = binary_available("node", ("/opt/node22/bin/node",))
    text = (ROOT / "tests" / "fixtures" / "health" / "golden_v1.json").read_text(encoding="utf-8")
    check("golden --check: the fixture header records the Node version that wrote it",
          re.search(r'(?m)^ "node": "v\d+\.\d+\.\d+",$', text) is not None)

    def run_check(planted: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "golden.json"
            out.write_text(planted, encoding="utf-8")
            r = subprocess.run([node, str(ROOT / "tools" / "health_golden.mjs"), "--check"],
                               env={**os.environ, "HEALTH_GOLDEN_OUT": str(out)},
                               capture_output=True, text=True, timeout=300, check=False)
            return r.returncode, r.stdout + r.stderr

    code, out = run_check(re.sub(r'(?m)^ "node": "v[^"]*",$', ' "node": "v99.0.0",', text, count=1))
    check("golden --check: a copy that differs only in the recorded Node version is up to "
          "date, and the check says which versions were compared",
          code == 0 and "generated under node v99.0.0" in out and "identical" in out, out[-400:])
    old, new = "0.6270739405881613", "0.6270739405881614"
    check("golden --check: the planted value exists exactly once (mulberry32 seed 1)",
          text.count(old) == 1)
    code, out = run_check(text.replace(old, new, 1))
    check("golden --check: one changed value is stale (exit 1) and the first differing line "
          "is named with both versions of it",
          code == 1 and "stale" in out and "first difference at line" in out and new in out
          and old in out, out[-600:])


def test_model_version_agrees_everywhere():
    """health.engine.MODEL_VERSION == index.js MODEL_VERSION == the fixture's modelVersion.
    The config/health/base.yaml comparison needs PyYAML and lives in
    test_config_agrees_with_engine_and_reference (@needs("yaml"))."""
    js = (REFERENCE_ENGINE / "index.js").read_text(encoding="utf-8")
    m = re.search(r"export const MODEL_VERSION = '([^']+)'", js)
    js_version = m.group(1) if m else None
    check("model version: health.engine.MODEL_VERSION equals MODEL_VERSION in index.js",
          js_version is not None and MODEL_VERSION == js_version,
          f"python {MODEL_VERSION!r}, index.js {js_version!r}")
    check("model version: the golden fixture was generated from the same model version",
          golden()["modelVersion"] == MODEL_VERSION, f"fixture {golden()['modelVersion']!r}")
    check("model version: simulate() stamps it on every result",
          sim("drink_water_1L")["meta"]["modelVersion"] == MODEL_VERSION)


def test_mulberry32_matches_javascript_reference():
    for seed_s, expected in golden()["mulberry32"].items():
        rng = mulberry32(int(seed_s))
        got = [rng() for _ in expected]
        check(f"mulberry32({seed_s}): first {len(expected)} outputs bit-identical to JS",
              got == expected, f"python {got}\n        javascript {expected}")
    # Math.imul / ToUint32 semantics the generator depends on (values from the ECMAScript spec).
    check("mulberry32: Math.imul emulation is signed 32-bit",
          imul(0xFFFFFFFF, 5) == -5 and imul(-5, 12) == -60 and imul(0x7FFFFFFF, 2) == -2
          and imul(0x10000, 0x10000) == 0, "")
    a, b = mulberry32(2**32 + 1), mulberry32(1)
    c, d = mulberry32(-1), mulberry32(4294967295)
    check("mulberry32: seed goes through ToUint32 (2**32+1 == 1, -1 == 4294967295)",
          [a() for _ in range(3)] == [b() for _ in range(3)]
          and [c() for _ in range(3)] == [d() for _ in range(3)])


def test_steady_state_constants_match_javascript_reference():
    g = golden()["steadyState"]
    p = default_params()
    C = constants(p)
    w = Worst()
    keys_ok = set(C) == set(g["constants"])
    for k, e in g["constants"].items():
        if isinstance(e, bool):
            check(f"constants: {k} is {e} as in JS", C.get(k) is e, repr(C.get(k)))
        else:
            w.add(f"constants.{k}", C[k], e)
    y0 = initial_state(p)
    for i, e in enumerate(g["initialState"]):
        w.add(f"initialState[{STATE_KEYS[i]}]", y0[i], e)
    d0 = derived(y0, p)
    for k, e in g["derived0"].items():
        w.add(f"derived(y0).{k}", d0[k], e)
    f0 = fluxes(y0, p, baseline_inputs(p))
    for k, e in g["fluxes0"].items():
        w.add(f"fluxes(y0).{k}", f0[k], e)
    check("M0: constants() has exactly the JS keys (minus the JS-only _snap)", keys_ok,
          f"python-only {sorted(set(C) - set(g['constants']))}, "
          f"js-only {sorted(set(g['constants']) - set(C))}")
    check("M0-M10: constants, initial_state, derived(y0), fluxes(y0) match JS", w.ok, w.summary())
    note(w.summary())
    check("derived/fluxes: key order is the JS key order",
          list(d0) == list(DERIVED_KEYS) == list(g["derived0"]) and list(f0) == list(g["fluxes0"]))
    # Cache semantics (JS: WeakMap per params object + snapshot check).
    check("cache: constants(p) is memoised (same object for unchanged params)",
          constants(p) is C)
    p["GFR_0"] = 130
    C2 = constants(p)
    check("cache: an in-place edit of p is seen (no stale constants)",
          C2["GFR0_Lh"] == 130 * 60 / 1000 and C["GFR0_Lh"] == 125 * 60 / 1000,
          f"{C2['GFR0_Lh']} vs {C['GFR0_Lh']}")
    try:
        C2["GFR0_Lh"] = 0.0  # type: ignore[index]
        mutable = True
    except TypeError:
        mutable = False
    check("cache: cached constants are read-only (a caller cannot poison them)", not mutable)


def test_divergence_checkpoints_match_javascript_reference():
    """docs/health/03 §4.2: when a trajectory diverges, these localise it -- rhs() at the
    initial state (baseline and drinking inputs) and one RK4 step of the augmented state."""
    g = golden()["checkpoints"]
    p = default_params()
    C = constants(p)
    y0 = initial_state(p)
    n, nl = len(STATE_KEYS), len(LEDGER_KEYS)
    w = Worst()
    for label, inputs, exp in (("baseline", baseline_inputs(p), g["rhsBaseline"]),
                               ("drinking", None, g["rhsDrinking"])):
        if inputs is None:
            inputs = SCENARIOS[exp["scenario"]].inputs(exp["inputsAt"], p)
            d = first_difference(inputs, exp["inputs"])
            check("checkpoint: drink_water_1L inputs at t = 1.05 h equal JS", d is None, d or "")
        led = [0.0] * nl
        dy = rhs(0.0, y0, p, inputs, None, led, C)
        for i, e in enumerate(exp["dy"]):
            w.add(f"rhs[{label}].d{STATE_KEYS[i]}", dy[i], e)
        for i, e in enumerate(exp["ledger"]):
            w.add(f"rhs[{label}].ledger.{LEDGER_KEYS[i]}", led[i], e)
    step = g["rk4Step"]
    sc = SCENARIOS[step["scenario"]]
    inputs = sc.inputs(step["inputsAt"], p)
    ledb = [0.0] * nl

    def deriv(t: float, z: list[float], inp: Any, dz: list[float]) -> None:
        rhs(t, z, p, inp, dz, ledb, C)
        dz[n:n + nl] = ledb

    z = list(y0) + [0.0] * nl
    rk4_step(deriv, step["t"], z, step["h"], inputs, workspace(n + nl))
    keys = [*STATE_KEYS, *LEDGER_KEYS]
    for i, e in enumerate(step["z"]):
        w.add(f"rk4Step.{keys[i]}", z[i], e)
    check("checkpoint: rhs(y0) with baseline and drinking inputs, and one RK4 step, match JS",
          w.ok, w.summary())
    note(w.summary())


# Solver and port coverage the registered scenarios cannot give (fixture section
# solverCoverage, BUG-20261003-132/-133/-135): potassium and sweat fluxes, output thinning,
# breakpoints inside output intervals, unsorted breakpoints. These runs are built HERE from
# the fixture's own description (never added to the shipped SCENARIOS registry).
COVERAGE_RUNS = ("baseline_K_icf_x1.02", "sweat_potassium", "unsorted_breakpoints",
                 "drink_water_1L_outEvery_7", "drink_water_1L_outEvery_2.5",
                 "drink_water_1L_dt_0.1", "salt_load_10g_dt_0.1")
_COVERAGE: dict[str, dict[str, Any]] = {}


def spec_scenario(spec: Mapping[str, Any]) -> Any:
    """The fixture's data-described scenario (tools/health_golden.mjs specScenario): the
    baseline diet, then for each schedule entry with from <= t < to, `set` assigns and
    `scale` multiplies. Breakpoints are kept in the given (possibly unsorted) order."""
    schedule = [(e["from"], e["to"], dict(e.get("set", {})), dict(e.get("scale", {})))
                for e in spec["schedule"]]

    def inputs(t: float, p: Mapping[str, Any]) -> dict[str, float]:
        u = baseline_inputs(p)
        for lo, hi, assign, scale in schedule:
            if t >= lo and t < hi:
                for k, v in assign.items():
                    u[k] = v
                for k, f in scale.items():
                    u[k] = u[k] * f
        return u

    return dataclasses.replace(
        SCENARIOS["baseline"], id=spec["id"], title=spec["id"], description="self-test only",
        t_end=spec["tEnd"], dt=spec["dt"], out_every=spec["outEvery"],
        breakpoints=tuple(spec["breakpoints"]), events=(), overrides=(), inputs=inputs,
        validation=None, label=None)


def coverage_run(name: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run one solverCoverage case as the generator did (memoised at default params)."""
    if params is None and name in _COVERAGE:
        return _COVERAGE[name]
    run = golden()["solverCoverage"][name]["run"]
    p = dict(params if params is not None else default_params())
    if "spec" in run:
        r = simulate(params=p, scenario=spec_scenario(run["spec"]))
    elif "y0Scale" in run:
        y0 = initial_state(p)
        for k, f in run["y0Scale"].items():
            y0[IDX[k]] *= f
        r = simulate(params=p, scenario=run["scenario"], t_end=run["tEnd"], y0=y0)
    else:
        r = simulate(params=p, scenario=run["scenario"], dt=run.get("dt"),
                     out_every=run.get("outEvery"))
    if params is None:
        _COVERAGE[name] = r
    return r


def test_default_params_trajectories_match_javascript_reference():
    gt = golden()["trajectories"]
    check("trajectories: fixture covers every registered scenario (HREQ-V-08), including the "
          "required baseline, drink_water_1L, salt_load_10g, no_water_24h",
          set(gt) == set(SCENARIOS), str(list(gt)))
    total = Worst()
    runs = []
    for sid, g in gt.items():
        t0 = time.perf_counter()
        runs.append((sid, g, sim(sid, g["dt"], g["outEvery"])))
        if g["steps"] > 10_000:
            note(f"{sid}: {g['steps']} RK4 steps took {time.perf_counter() - t0:.2f} s")
    for sid, g in golden()["stiffCorner"]["runs"].items():
        t0 = time.perf_counter()
        runs.append((f"{sid} at the stiff corner", g, stiff_corner_run(sid)))
        note(f"{sid} at the stiff corner: {g['steps']} RK4 steps took "
             f"{time.perf_counter() - t0:.2f} s")
    chronic = gt["chronic_high_salt_30d"]
    check("trajectory chronic_high_salt_30d: exactly 41,664 RK4 steps, recorded on <= 1000 points",
          chronic["steps"] == 41_664 and sim("chronic_high_salt_30d", chronic["dt"],
                                             chronic["outEvery"])["meta"]["steps"] == 41_664
          and chronic["length"] <= 1000, f"{chronic['steps']} steps, {chronic['length']} points")
    check("trajectory: the stiff corner covers chronic_high_salt_30d and drink_water_3L_fast",
          set(golden()["stiffCorner"]["runs"]) == {"chronic_high_salt_30d", "drink_water_3L_fast"})
    for sid, g, r in runs:
        T = len(r["t"])
        check(f"trajectory {sid}: grid length {g['length']} and {g['steps']} RK4 steps as in JS",
              T == g["length"] and r["meta"]["steps"] == g["steps"],
              f"python length {T}, steps {r['meta']['steps']}")
        check(f"trajectory {sid}: maxStep {g['maxStep']!r} as in JS",
              r["meta"]["maxStep"] == g["maxStep"], repr(r["meta"]["maxStep"]))
        check(f"trajectory {sid}: compares the required indices [0, 1, 10, 100, last]",
              g["requiredIndices"] == [0, 1, 10, 100, T - 1]
              and set(g["requiredIndices"]) <= set(g["indices"]))
        w = compare_trajectory(r, g, sid)
        check(f"trajectory {sid}: every state, derived and ledger value matches JS "
              f"(rel {REL_TOL:g} / abs {ABS_TOL:g})", w.ok, w.summary())
        note(w.summary())
        total.n += w.n
        total.identical += w.identical
    note(f"all trajectories: {total.n} values, {total.identical} bit-identical")

    # Solver and port coverage (fixture section solverCoverage).
    gc = golden()["solverCoverage"]
    check("solver coverage: the fixture holds the K-perturbed baseline, the sweat/potassium "
          "run, outEvery 7 and 2.5, dt 0.1 runs and the unsorted-breakpoint scenario",
          list(gc) == list(COVERAGE_RUNS), str(list(gc)))
    for name in COVERAGE_RUNS:
        if name not in gc:
            continue
        g, r = gc[name], coverage_run(name)
        T = len(r["t"])
        check(f"coverage {name}: grid length {g['length']}, {g['steps']} RK4 steps and maxStep "
              "as in JS", T == g["length"] and r["meta"]["steps"] == g["steps"]
              and r["meta"]["maxStep"] == g["maxStep"]
              and g["requiredIndices"] == [0, 1, 10, 100, T - 1],
              f"python length {T}, steps {r['meta']['steps']}, maxStep {r['meta']['maxStep']!r}")
        w = compare_trajectory(r, g, name)
        check(f"coverage {name}: every state, derived and ledger value matches JS", w.ok,
              w.summary())
        note(w.summary())
    # What each case exists for, asserted on the run itself so a fixture that stopped
    # exercising it cannot pass quietly.
    k0 = default_params()["K_icf_0"]
    sk = coverage_run("sweat_potassium")
    kb = coverage_run("baseline_K_icf_x1.02")
    base_k = default_params()["kIn_base_mmold"] / 24
    check("coverage: potassium moves -- K_icf leaves K_icf_0 and urinary K departs from intake "
          "in the sweat/potassium run and the K-perturbed baseline (every registered scenario "
          "holds them exactly at baseline)",
          max(abs(v - k0) for v in sk["states"]["K_icf"]) > 1e-3 * k0
          and max(abs(v - base_k) for v in kb["derived"]["K_excr"]) > 1e-2 * base_k)
    p0 = default_params()
    sk_sc = spec_scenario(gc["sweat_potassium"]["run"]["spec"])
    u_sweat, u_k = sk_sc.inputs(3.0, p0), sk_sc.inputs(1.5, p0)
    f_sweat = fluxes(initial_state(p0), p0, u_sweat)
    check("coverage: the sweat/potassium run sweats 0.8 L/h with its sodium over [2, 4) h and "
          "takes 6x the K intake over [1, 3) h",
          f_sweat["sweat"] == 0.8 and f_sweat["sweat_na"] == 0.8 * p0["sweat_na_mmolL"] > 0
          and u_k["kIn_mmolh"] == base_k * 6 and u_k["sweat_Lh"] == 0,
          f"{u_sweat}, {u_k}, sweat {f_sweat['sweat']}, sweat_na {f_sweat['sweat_na']}")
    check("coverage: drink_water_1L recorded every 7th point has 104 points (the final point "
          "is always recorded), every 2.5th 241 (JavaScript Math.round(2.5) = 3, not "
          "Python's round(2.5) = 2)",
          len(coverage_run("drink_water_1L_outEvery_7")["t"]) == 104
          and len(coverage_run("drink_water_1L_outEvery_2.5")["t"]) == 241,
          f"{len(coverage_run('drink_water_1L_outEvery_7')['t'])}, "
          f"{len(coverage_run('drink_water_1L_outEvery_2.5')['t'])}")
    spec = gc.get("unsorted_breakpoints", {}).get("run", {}).get("spec", {})
    bps = spec.get("breakpoints", [])
    check("coverage: the unsorted-breakpoint scenario really is unsorted, with breakpoints off "
          "the 0.1 h grid", bool(bps) and bps != sorted(bps)
          and all(abs(b * 10 - round(b * 10)) > 1e-6 for b in bps), str(bps))


def _apply_table_edits(table: dict[str, Any], edits: Mapping[str, Any]) -> dict[str, Any]:
    for name, fields in edits.items():
        table[name].update(fields)
    return table


def _compare_samples(label: str, got: Sequence[Mapping[str, Any]],
                     exp: Sequence[Mapping[str, Any]]) -> None:
    check(f"{label}: {len(exp)} samples as in JS", len(got) == len(exp), f"{len(got)}")
    w = Worst()
    order_ok = True
    for i, (a, e) in enumerate(zip(got, exp, strict=False)):
        order_ok &= list(a) == list(e)
        for k in e:
            w.add(f"sample[{i}].{k}", a.get(k, math.nan), e[k], SAMPLE_REL_TOL, 0.0)
    check(f"{label}: parameter keys in table order", order_ok)
    check(f"{label}: every sampled value matches JS to {SAMPLE_REL_TOL:g} relative",
          w.ok, w.summary())
    note(w.summary())


def test_draw_samples_matches_javascript_reference():
    g = golden()["drawSamples"]
    d = draw_samples(n=g["n"], seed=g["seed"])
    check(f"drawSamples(n={g['n']}, seed={g['seed']}): rejected = {g['rejected']} as in JS",
          d["rejected"] == g["rejected"], str(d["rejected"]))
    _compare_samples(f"drawSamples(n={g['n']}, seed={g['seed']})", d["samples"], g["samples"])
    # A table where some draws are infeasible: the same draws must be rejected and counted.
    gr = golden()["rejection"]
    table = _apply_table_edits(param_table(), gr["tableEdits"])
    d2 = draw_samples(n=gr["n"], seed=gr["seed"], table=table)
    check(f"drawSamples with an infeasible region: rejects {gr['rejected']} draws, as JS does",
          d2["rejected"] == gr["rejected"] and gr["rejected"] > 0, str(d2["rejected"]))
    _compare_samples("drawSamples with an infeasible region", d2["samples"], gr["samples"])
    k_entry = param_table()["k_excr_gain"]
    check("sampling: k_excr_gain (range 20-100, hi/lo exactly 5) is sampled UNIFORMLY, as mc.js "
          "does (hi/lo > 5 is false) -- its params.json note saying 'Log-uniform' is wrong (PRM)",
          sampling_mode(k_entry) == "uniform" and k_entry["range"][1] / k_entry["range"][0] == 5,
          sampling_mode(k_entry))
    margins = golden()["rejectionMargins"]
    check("fixture: every Monte Carlo section was generated > 1e-6 from any rejection boundary "
          "(HREQ-V-11), so no accept/reject decision rests on the last bit",
          bool(margins) and all(m["minRelativeMargin"] > m["refuseBelow"] == 1e-6
                                for m in margins.values()),
          str({k: m["minRelativeMargin"] for k, m in margins.items()}))
    note("smallest margin to a rejection boundary: " + ", ".join(
        f"{k} {m['minRelativeMargin']:.3g} ({m['nearestBoundary']})" for k, m in margins.items()))


def test_monte_carlo_bands_match_javascript_reference():
    g = golden()["simulateMC"]
    t0 = time.perf_counter()
    r = simulate_mc(scenario=g["scenario"], n=g["n"], seed=g["seed"], keys=g["keys"])
    note(f"simulate_mc({g['scenario']}, n={g['n']}) took {time.perf_counter() - t0:.2f} s")
    check("simulateMC: grid length and rejected count as in JS",
          len(r["t"]) == g["length"] and r["rejected"] == g["rejected"],
          f"length {len(r['t'])}, rejected {r['rejected']}")
    check("simulateMC: result carries the disclaimer", r["disclaimer"] == DISCLAIMER)
    w = Worst()
    for i, k in enumerate(g["indices"]):
        w.add(f"t[{k}]", r["t"][k], g["t"][i])
    for band in ("q05", "q50", "q95", "dq05", "dq50", "dq95"):
        for key in g["keys"]:
            for i, k in enumerate(g["indices"]):
                w.add(f"{band}.{key}[{k}]", r[band][key][k], g[band][key][i])
    check(f"simulateMC({g['scenario']}, n={g['n']}, seed={g['seed']}): q05/q50/q95 and Δ-bands "
          "match JS at t indices " + str(g["indices"]), w.ok, w.summary())
    note(w.summary())


def test_salt_load_metrics_match_javascript_reference():
    g = golden()["saltLoadMetrics"]
    m = salt_load_metrics(sim(g["scenario"], g["dt"]))
    w = Worst()
    for k, e in g["metrics"].items():
        w.add(k, m[k], e)
    check("saltLoadMetrics: same metric keys as JS (plus the Python meta)",
          [k for k in m if k != "meta"] == list(g["metrics"]), str(list(m)))
    check("saltLoadMetrics(salt_load_10g, dt=1/30) matches JS", w.ok, w.summary())
    note(", ".join(f"{k}={v:.6g}" for k, v in m.items() if k != "meta"))


def test_scenario_records_match_javascript_reference():
    g = golden()["scenarios"]
    check("scenarios: same ids in the same order as JS SCENARIOS",
          list(SCENARIOS) == g["scenarioOrder"], f"{list(SCENARIOS)} vs {g['scenarioOrder']}")
    check("scenarios: MMOL_NA_PER_G_NACL identical", MMOL_NA_PER_G_NACL == g["MMOL_NA_PER_G_NACL"])
    p = default_params()

    py_only: list[str] = []

    def js_view(validation: Mapping[str, Any] | None) -> Any:
        """The validation record minus the Python-only annotations (id, role, calibrates)."""
        if validation is None:
            return None
        out = dict(validation)
        out["expects"] = []
        for e in validation.get("expects", []):
            extra = [k for k in e if k in PY_ONLY_EXPECTATION_KEYS]
            py_only.extend(f"{e['metric']}.{k}" for k in extra)
            out["expects"].append({k: v for k, v in e.items() if k not in PY_ONLY_EXPECTATION_KEYS})
        return out

    def record(sc: Any, with_inputs: bool = True) -> dict[str, Any]:
        rec = {"id": sc.id, "title": sc.title, "description": sc.description,
               "tEnd": sc.t_end, "dt": sc.dt, "outEvery": sc.out_every,
               "breakpoints": list(sc.breakpoints), "events": [dict(e) for e in sc.events],
               "overrides": [dict(o) for o in sc.overrides], "validation": js_view(sc.validation),
               "label": sc.label}
        return rec | ({"inputs": [[t, sc.inputs(t, p)] for t, _ in probes]} if with_inputs else {})

    for sid, exp in g["scenarios"].items():
        probes = exp["inputs"]
        sc = SCENARIOS.get(sid)
        if sc is None:
            check(f"scenario {sid}: exists in the port", False, "missing")
            continue
        exp_main = {k: v for k, v in exp.items() if k != "sweep"}
        d = first_difference(record(sc), exp_main, sid)
        check(f"scenario {sid}: id, title, description, tEnd, dt, events, overrides, "
              "validation contract and input schedule are verbatim JS (Python-only role "
              "annotations aside)", d is None, d or "")
        if "sweep" in exp:
            sw = sc.sweep
            es = exp["sweep"]
            ok = (sw is not None and sw.variable == es["variable"] and sw.unit == es["unit"]
                  and list(sw.values) == es["values"] and list(sw.metrics) == es["metrics"])
            check(f"scenario {sid}: sweep variable/unit/values/metrics as in JS", ok)
            if sw is not None:
                for g_val, made in zip(sw.values, es["made"], strict=True):
                    d = first_difference(record(sw.make(g_val), False), made, f"sweep.make({g_val})")
                    if d:
                        break
                check(f"scenario {sid}: sweep.make(g) builds the JS scenario for all "
                      f"{len(sw.values)} doses", d is None, d or "")
    check("scenarios: the only Python-only annotations are one explicit id per expectation "
          "(HREQ-E-13) and the chronic calibration and structural roles (docs/health/03 §5.1)",
          sorted(py_only) == sorted(
              [f"{e['metric']}.id" for sc in SCENARIOS.values()
               for e in (sc.validation or {}).get("expects") or []]
              + ["ΔMAP at day 30 per +100 mmol/day Na.role",
                 "ΔMAP at day 30 per +100 mmol/day Na.calibrates",
                 "MAP time course.role", "MAP time course.calibrates",
                 "Na excretion ≈ intake by day 30.role"]), str(py_only))
    made = [make_water_load(1), make_water_load(0.5, 5, 2, 6), make_water_load(1.5, 30),
            make_salt_load(2.5), make_salt_load(6, 0.25, 10, 3, 24), make_salt_load(0)]
    for sc, exp in zip(made, g["made"], strict=True):
        probes = exp["inputs"]
        d = first_difference(record(sc), exp, sc.id)
        check(f"make_*_load: {exp['id']} is the JS makeWaterLoad/makeSaltLoad scenario",
              d is None, d or "")


def test_influence_screen_matches_javascript_reference():
    g = golden()["computeInfluence"]
    o = g["opts"]
    t0 = time.perf_counter()
    r = compute_influence(scenario=o["scenario"], t_end=o["tEnd"], dt=o["dt"])
    note(f"compute_influence({o}) took {time.perf_counter() - t0:.2f} s")
    w = Worst()
    for key, eff in g["effects"].items():
        for name, e in eff.items():
            w.add(f"effects.{key}.{name}", r["effects"][key][name], e)
    check("computeInfluence: same quantity keys as JS", list(r["effects"]) == list(g["effects"]))
    check("computeInfluence: every effect matches JS", w.ok, w.summary())
    note(w.summary())
    d = first_difference(r["params"], g["params"])
    check("computeInfluence: the parameter lists (>1 % effect, strongest first) equal JS",
          d is None, d or "")
    js_meta = {k: v for k, v in r["meta"].items() if k not in ("disclaimer", "validation_status")}
    d = first_difference(js_meta, g["meta"])
    check("computeInfluence: meta (infeasible, nParams, modelVersion) equals JS", d is None,
          str(d))
    global _INFLUENCE
    _INFLUENCE = r


def test_golden_comparison_detects_a_perturbed_model_constant():
    """Acceptance criterion 1 (docs/health/00 §12): the equivalence test must FAIL when a
    model constant is perturbed. Each parameter is raised by 1e-6 relative and the golden
    trajectories are re-compared; every parameter must be caught except the ones that are
    provably inert in the golden scenarios, listed with the reason."""
    # k_excr_gain and sweat_na_mmolL were inert here until the fixture gained the
    # K-perturbed baseline and the sweat/potassium run (BUG-20261003-132); they must now
    # be caught like every other model parameter.
    inert = {
        "na_normal_low": "classification threshold: only the Monte Carlo rejection rule reads it",
        "na_normal_high": "classification threshold: only the Monte Carlo rejection rule reads it",
    }
    gt = golden()["trajectories"]
    gc = golden()["solverCoverage"]
    # cheapest first; the two coverage runs are the only ones where K and sweat fluxes move
    order = ["drink_water_1L", "sweat_potassium", "baseline_K_icf_x1.02", "no_water_24h",
             "salt_load_10g"]
    p0 = default_params()
    undetected = []
    for name, v in p0.items():
        caught = False
        for sid in order:
            p = dict(p0)
            p[name] = v * (1 + 1e-6)
            if sid in gc:
                r, g = coverage_run(sid, p), gc[sid]
            else:
                r, g = simulate(params=p, scenario=sid, dt=gt[sid]["dt"]), gt[sid]
            if not compare_trajectory(r, g, sid).ok:
                caught = True
                break
        if not caught:
            undetected.append(name)
    check("golden: a 1e-6 relative change to any parameter fails the trajectory comparison, "
          "except the documented inert ones",
          set(undetected) == set(inert),
          f"undetected {sorted(undetected)}; documented inert {sorted(inert)}")
    for k, why in inert.items():
        note(f"inert in the golden trajectories: {k} -- {why}")
    # The classification thresholds are held fixed in sampling, so they ride in every sample:
    # the drawSamples golden catches them instead.
    tab = param_table()
    tab["na_normal_low"]["value"] = tab["na_normal_low"]["value"] * (1 + 1e-6)
    s = draw_samples(n=1, seed=1, table=tab)["samples"][0]
    e = golden()["drawSamples"]["samples"][0]
    check("golden: a perturbed classification threshold fails the drawSamples comparison",
          err_ratio(s["na_normal_low"], e["na_normal_low"], SAMPLE_REL_TOL, 0.0) > 1)

# The quantile series of fixture section nonFinite. JSON writes -0 as 0, so the self-test
# carries the literal (with its -0) and checks it equals the fixture's copy.
QB_SERIES = [[1.0, math.nan, 3.0], [math.inf, 2.0, -0.0], [-math.inf, math.nan, 0.0],
             [5.0, 4.0, math.inf], [2.0, 3.0, math.inf]]
QB_QS = [0.05, 0.5, 0.95, 0, 1]


def test_non_finite_semantics_match_javascript_reference():
    """The port emulates JavaScript where Python's maths differs on non-finite values
    (BUG-20261003-134): Float64Array.sort puts NaN last; Math.max(0, NaN) is NaN, where
    Python's max(0.0, nan) is 0.0; Math.pow(1, NaN) is NaN, where C99 pow says 1. The fixture
    pins all three to the reference; a NaN must sit at the same position on both sides
    (err_ratio: NaN against NaN is equal, NaN against a number is infinitely wrong)."""
    g = golden()["nonFinite"]
    qb = g["quantileBands"]
    check("non-finite: the self-test's quantile series and qs are the fixture's (-0 aside)",
          first_difference(QB_SERIES, qb["series"]) is None and QB_QS == qb["qs"],
          str(first_difference(QB_SERIES, qb["series"])))
    got = quantile_bands(QB_SERIES, QB_QS)
    w = Worst()
    for j, q in enumerate(QB_QS):
        for k, e in enumerate(qb["bands"][j]):
            w.add(f"quantileBands q={q}[{k}]", got[j][k], e)
    check("non-finite: quantile_bands with NaN and +-Infinity in the samples equals JS "
          "quantileBands (NaN sorted last; NaN positions identical)", w.ok, w.summary())
    note(w.summary())
    p0 = default_params()
    for label, desc in (("adhNaN", "a state with ADH = NaN (Math.max(0, NaN) = NaN)"),
                        ("pow1NaN", "aldo_vol_exp = gfr_map_exp = NaN at vr = MAP/MAP_0 = 1 "
                                    "(Math.pow(1, NaN) = NaN)")):
        e = g[label]
        p = p0 | e["overrides"]
        y = list(e["y"])
        C = constants(p)
        inputs = baseline_inputs(p)
        led = [0.0] * len(LEDGER_KEYS)
        dy = rhs(0.0, y, p, inputs, None, led, C)
        f = fluxes(y, p, inputs, C)
        d = derived(y, p, C)
        w = Worst()
        for i, v in enumerate(e["dy"]):
            w.add(f"{label} rhs.d{STATE_KEYS[i]}", dy[i], v)
        for i, v in enumerate(e["ledger"]):
            w.add(f"{label} ledger.{LEDGER_KEYS[i]}", led[i], v)
        for k, v in e["fluxes"].items():
            w.add(f"{label} fluxes.{k}", f[k], v)
        for k, v in e["derived"].items():
            w.add(f"{label} derived.{k}", d[k], v)
        n_nan = sum(1 for sec in ("dy", "ledger") for v in e[sec] if v != v) + sum(
            1 for sec in ("fluxes", "derived") for v in e[sec].values() if v != v)
        check(f"non-finite: rhs, ledger, fluxes and derived at {desc} equal JS, NaN for NaN",
              w.ok and n_nan > 0, w.summary() + f"; {n_nan} NaN values in the fixture")
        note(f"{label}: {w.summary()}; {n_nan} NaN values compared by position")


# ---------------------------------------------------------------------------
# Model invariants (independent of the JavaScript).
# ---------------------------------------------------------------------------
def test_baseline_is_a_steady_state():
    r = sim("baseline")
    drift, key, k = max_relative_state_drift(r)
    check(f"baseline: max |Δstate| / scale over 24 h < {STEADY_STATE_DRIFT_MAX:g}",
          drift < STEADY_STATE_DRIFT_MAX, f"{drift:.3g} at {key}[{k}]")
    note(f"max relative drift {drift:.3g} ({key}, t index {k}); scale = |y(0)|, or 1 if y(0) = 0")
    p = default_params()
    y0 = initial_state(p)
    dy = rhs(0.0, y0, p, baseline_inputs(p))
    worst = max(abs(dy[i]) / (abs(y0[i]) or 1.0) for i in range(len(y0)))
    check("baseline: rhs(initial_state) is zero to round-off (< 1e-12 relative per hour)",
          worst < 1e-12, f"{worst:.3g}")
    check("baseline: ledger integrates the baseline intake exactly over 24 h",
          abs(r["ledger"]["na_in"][-1] - p["naIn_base_mmold"]) <= 1e-9 * p["naIn_base_mmold"]
          and abs(r["ledger"]["na_out"][-1] - p["naIn_base_mmold"]) <= 1e-9 * p["naIn_base_mmold"],
          f"na_in {r['ledger']['na_in'][-1]!r}, na_out {r['ledger']['na_out'][-1]!r}")
    # The steady state is analytic for ANY feasible parameter set, not just the defaults.
    samples = draw_samples(n=3, seed=11)["samples"]
    worst_s = max(max_relative_state_drift(simulate(params=s, scenario="baseline", t_end=6))[0]
                  for s in samples)
    check("baseline: three Monte Carlo parameter sets also sit at their steady state (6 h)",
          worst_s < STEADY_STATE_DRIFT_MAX, f"{worst_s:.3g}")
    note("3 samples x 6 h in the default suite; the n = 256 x 24 h gate of 03 §3.1 is "
         "test_robust_steady_state_drift_over_the_reporting_set (--robust)")
    corner = max_relative_state_drift(simulate(params=stiff_corner_params(), scenario="baseline"))
    check("baseline: the stiff parameter corner (docs/health/03 §3.5) sits at its steady state "
          "over 24 h", corner[0] < STEADY_STATE_DRIFT_MAX, f"{corner[0]:.3g} at {corner[1]}")


def test_water_and_sodium_mass_balance_closes():
    """The model's linear invariants, at every output time of every shipped scenario and of
    the stiff-corner run (docs/health/03 §3.2, HREQ-V-03):
        water       V_ecf + V_icf + V_gut_water − (water_in − water_out)
        sodium      Na_ecf + Na_gut − (na_in − na_out)
        potassium   K_icf − (k_in − k_out)
        cell solute osm_icf_solute − 2·K_icf
    Each must stay at its t = 0 value to 1e-9 relative to that value. RK4 preserves linear
    invariants up to round-off, so a larger residual is a bookkeeping bug (a flux in a
    balance but not in the ledger), not numerics. The largest residuals are printed."""
    runs = [(sid, sim(sid)) for sid in SCENARIOS]
    runs += [(f"{sid} at the stiff corner", stiff_corner_run(sid))
             for sid in golden()["stiffCorner"]["runs"]]
    # The only runs in which the potassium and cell-solute invariants are not trivially
    # constant (K and sweat fluxes are zero in every registered scenario), plus the
    # off-grid breakpoint runs (BUG-20261003-132, -135).
    runs += [(f"coverage {name}", coverage_run(name)) for name in COVERAGE_RUNS]
    for label, r in runs:
        s, L = r["states"], r["ledger"]

        def invariants(k: int, s: Mapping[str, list[float]] = s,
                       L: Mapping[str, list[float]] = L) -> dict[str, float]:
            return {
                "water": s["V_ecf"][k] + s["V_icf"][k] + s["V_gut_water"][k]
                - (L["water_in"][k] - L["water_out"][k]),
                "sodium": s["Na_ecf"][k] + s["Na_gut"][k] - (L["na_in"][k] - L["na_out"][k]),
                "potassium": s["K_icf"][k] - (L["k_in"][k] - L["k_out"][k]),
                "cell solute": s["osm_icf_solute"][k] - 2 * s["K_icf"][k],
            }

        i0 = invariants(0)
        rel = dict.fromkeys(i0, 0.0)
        for k in range(len(r["t"])):
            for name, v in invariants(k).items():
                d = abs(v - i0[name]) / abs(i0[name])
                if not (d <= rel[name]):
                    rel[name] = d
        for name, d in rel.items():
            check(f"mass balance {label}: {name} invariant constant to {MASS_BALANCE_TOL:g} "
                  "relative", d <= MASS_BALANCE_TOL, f"{d:.3g} (value at t=0: {i0[name]:.6g})")
        note(f"{label}: " + ", ".join(f"{n} {d:.2g}" for n, d in rel.items()))


# ---------------------------------------------------------------------------
# Numerical gates of docs/health/03 §3.3-§3.6 (HREQ-V-04 .. V-07). Each gate is shown to
# fail on the defect it exists for (the breakpoint trap and the instability trap).
# ---------------------------------------------------------------------------
#: HREQ-V-05 (03 §3.4, chosen): halving the step twice changes no state by more than this
#: fraction of its peak magnitude; ADH and Thirst, whose targets pass through max(0, ·) and
#: a [0, 1] clamp the solver does not locate, are held to the looser bound. Measured
#: 2026-10-03 (default params): drink_water_1L ANP 7.2e-7, ADH 2.0e-5, Thirst 3.2e-4;
#: salt_load_10g ANP 1.1e-7, ADH 2.6e-8, Thirst 9.2e-8 -- 14x and 3x inside the gates.
CONVERGENCE_TOL = 1e-5
CONVERGENCE_TOL_KINKED = 1e-3
KINKED_STATES = ("ADH", "Thirst")
PRODUCTION_STEP_FACTOR = 0.25      # index.js simulate: maxStep = min(dt, 0.25 · tau_min)
FINE_STEP_FACTOR = 0.0625          # a quarter of it: the step halved twice
#: HREQ-V-04 (03 §3.3, chosen): an off-grid bolus is delivered to 1e-12 relative.
BOLUS_EXACTNESS_TOL = 1e-12
#: 03 §3.3 / §9: the off-grid bolus (1 L over 7.3 min from 1.0037 h) and the instability
#: trap's step factor (capped by dt at 0.25 h = 3.3 · tau_min on chronic_high_salt_30d).
BOLUS = {"liters": 1, "dur_min": 7.3, "at": 1.0037, "t_end": 3}
INSTABILITY_STEP_FACTOR = 3.5


def with_step_factor(factor: float, fn: Callable[[], Any]) -> Any:
    """Run fn() with api.MAX_STEP_FRACTION_OF_TAU_MIN set to `factor`, then restore it."""
    old = api_module.MAX_STEP_FRACTION_OF_TAU_MIN
    api_module.MAX_STEP_FRACTION_OF_TAU_MIN = factor
    try:
        return fn()
    finally:
        api_module.MAX_STEP_FRACTION_OF_TAU_MIN = old


def convergence_errors(params: Mapping[str, Any], scenario: str,
                       out_every: int | None = None) -> tuple[dict[str, float], int, int]:
    """Peak-scaled max |x_coarse - x_fine| per state, production step factor vs a quarter
    of it (03 §3.4), and the two RK4 step counts."""
    a = with_step_factor(PRODUCTION_STEP_FACTOR,
                         lambda: simulate(params=params, scenario=scenario, out_every=out_every))
    b = with_step_factor(FINE_STEP_FACTOR,
                         lambda: simulate(params=params, scenario=scenario, out_every=out_every))
    errs = {}
    for k in STATE_KEYS:
        x, y = a["states"][k], b["states"][k]
        peak = max(abs(v) for v in y) or 1.0
        errs[k] = max(abs(u - v) for u, v in zip(x, y, strict=True)) / peak
    return errs, a["meta"]["steps"], b["meta"]["steps"]


def check_convergence(label: str, errs: Mapping[str, float], coarse: int, fine: int) -> None:
    smooth = {k: v for k, v in errs.items() if k not in KINKED_STATES}
    ks = max(smooth, key=lambda k: smooth[k])
    kk = max(KINKED_STATES, key=lambda k: errs[k])
    check(f"convergence {label}: halving the step twice ({coarse} -> {fine} RK4 steps) changes "
          f"no state by more than {CONVERGENCE_TOL:g} of its peak ({CONVERGENCE_TOL_KINKED:g} for "
          "ADH and Thirst) (HREQ-V-05)",
          smooth[ks] <= CONVERGENCE_TOL and errs[kk] <= CONVERGENCE_TOL_KINKED
          and fine >= 2 * coarse,
          f"worst smooth {ks} {smooth[ks]:.3g}, worst kinked {kk} {errs[kk]:.3g}, "
          f"steps {coarse} -> {fine}")
    note(f"{label}: worst smooth {ks} {smooth[ks]:.2g}, ADH {errs['ADH']:.2g}, Thirst "
         f"{errs['Thirst']:.2g}; steps {coarse} -> {fine}")


def test_halving_the_step_twice_converges():
    """HREQ-V-05 on the two scenarios with the sharpest transients (03 §3.4). Every other
    scenario, and the stiff corner (HREQ-V-06), run under --robust."""
    check("convergence: the production step factor is 0.25, as in index.js",
          api_module.MAX_STEP_FRACTION_OF_TAU_MIN == PRODUCTION_STEP_FACTOR)
    for sid in ("drink_water_1L", "salt_load_10g"):
        check_convergence(sid, *convergence_errors(default_params(), sid))


@robust
def test_robust_convergence_on_every_scenario_and_the_stiff_corner():
    """HREQ-V-05 and V-06: every registered scenario at default parameters and at the stiff
    corner of 03 §3.5 (the 30-day runs dominate)."""
    for label, params in (("default", default_params()), ("stiff corner", stiff_corner_params())):
        for sid in SCENARIOS:
            t0 = time.perf_counter()
            check_convergence(f"{sid} ({label})", *convergence_errors(params, sid))
            note(f"  took {time.perf_counter() - t0:.2f} s")


@robust
def test_robust_steady_state_drift_over_the_reporting_set():
    """03 §3.1 / HREQ-V-02: the closed-form steady state holds for every accepted sample of
    the reporting set (n = 256, seed 1), over the full 24 h of baseline."""
    t0 = time.perf_counter()
    samples = draw_samples(n=256, seed=1)["samples"]
    worst, where = 0.0, ""
    for i, s in enumerate(samples):
        d, key, k = max_relative_state_drift(simulate(params=s, scenario="baseline", t_end=24))
        if not (d <= worst):
            worst, where = d, f"sample {i}, {key}[{k}]"
    check(f"baseline: all 256 accepted samples of the reporting set sit at their steady state "
          f"over 24 h (max |Δstate| / scale < {STEADY_STATE_DRIFT_MAX:g})",
          len(samples) == 256 and worst < STEADY_STATE_DRIFT_MAX, f"{worst:.3g} at {where}")
    note(f"n = 256 x 24 h: worst drift {worst:.3g} at {where}; took "
         f"{time.perf_counter() - t0:.2f} s")


def _bolus_delivered(r: Mapping[str, Any], p: Mapping[str, Any], t_end: float) -> float:
    """Bolus volume in the ledger: total intake minus the continuous diet and metabolic water."""
    C = constants(p)
    return r["ledger"]["water_in"][-1] - (C["waterIn_h"] + C["metab_h"]) * t_end


def test_off_grid_bolus_is_exact_and_the_breakpoint_trap_fails():
    """HREQ-V-04 (03 §3.3): no step exceeds min(dt, 0.25·tau_min) or straddles a breakpoint;
    an off-grid bolus is delivered to 1e-12 relative; and with its breakpoints removed the
    same bolus MUST fail that test (the breakpoint trap)."""
    p = default_params()
    sc = make_water_load(BOLUS["liters"], BOLUS["dur_min"], BOLUS["at"], BOLUS["t_end"])
    steps: list[tuple[float, float]] = []
    real_step = solver_module.rk4_step

    def recording_step(deriv: Any, t: float, z: list[float], h: float, inputs: Any,
                       ws: Any) -> None:
        steps.append((t, h))
        real_step(deriv, t, z, h, inputs, ws)

    solver_module.rk4_step = recording_step
    try:
        r = simulate(params=p, scenario=sc)
    finally:
        solver_module.rk4_step = real_step
    max_step = r["meta"]["maxStep"]
    dt, t_end = r["meta"]["dt"], sc.t_end
    bps = sorted(sc.breakpoints)
    n_grid = math.ceil(t_end / dt - 1e-9)
    cuts = sorted({*(k * dt for k in range(n_grid)), t_end, *(b for b in bps if 0 < b < t_end)})
    expected_steps = sum(max(1, math.ceil((b - a) / max_step - 1e-12))
                         for a, b in zip(cuts, cuts[1:], strict=False) if b > a)
    straddles = [(t, h, b) for t, h in steps for b in bps if t < b - 1e-12 and t + h > b + 1e-12]
    largest = max(h for _, h in steps) if steps else math.nan
    check("step control: every RK4 step <= maxStep = min(dt, 0.25·tau_min), none straddles a "
          "breakpoint, and the step count is the sum over segments of ceil(length / maxStep)",
          len(steps) == r["meta"]["steps"] == expected_steps
          and largest <= max_step * (1 + 1e-12) and not straddles,
          f"{len(steps)} steps recorded, meta {r['meta']['steps']}, expected {expected_steps}; "
          f"largest {largest!r} vs maxStep {max_step!r}; straddles {straddles[:3]}")
    got = _bolus_delivered(r, p, t_end)
    err = abs(got - BOLUS["liters"]) / BOLUS["liters"]
    check(f"bolus: 1 L over 7.3 min from 1.0037 h (off the 1/60 h grid) is delivered to "
          f"{BOLUS_EXACTNESS_TOL:g} relative (HREQ-V-04)", err <= BOLUS_EXACTNESS_TOL,
          f"delivered {got!r} L, relative error {err:.3g}")
    trap = simulate(params=p, scenario=dataclasses.replace(sc, breakpoints=()))
    got_t = _bolus_delivered(trap, p, t_end)
    err_t = abs(got_t - BOLUS["liters"]) / BOLUS["liters"]
    check("breakpoint trap: the same bolus with its breakpoints removed FAILS the exactness "
          "gate, misplacing 9-10 % of the bolus (03 §3.3 reports 9.6 %)",
          err_t > BOLUS_EXACTNESS_TOL and 0.09 < err_t < 0.10,
          f"delivered {got_t!r} L, misplaced {100 * err_t:.3g} %")
    note(f"bolus error with breakpoints {err:.2g}; without them {100 * err_t:.3g} % misplaced; "
         f"{len(steps)} steps, maxStep {max_step:.6g} h")


def test_simulate_refuses_a_non_finite_trajectory():
    """HREQ-V-07 (03 §3.6): a trajectory holding a NaN or infinite value fails its run.
    The instability trap (03 §3.3: step factor 3.5 on chronic_high_salt_30d, maxStep capped
    by dt at 0.25 h = 3.3·tau_min) must raise NonFiniteTrajectoryError; a production run
    must not. index.js returns the NaN trajectory: a deliberate, documented port deviation."""
    p = default_params()
    tau = constants(p)["tau_min"]
    raised: NonFiniteTrajectoryError | None = None
    try:
        with_step_factor(INSTABILITY_STEP_FACTOR,
                         lambda: simulate(params=p, scenario="chronic_high_salt_30d"))
    except NonFiniteTrajectoryError as exc:
        raised = exc
    check("instability trap: step factor 3.5 (maxStep 0.25 h = 3.3·tau_min) on "
          "chronic_high_salt_30d raises NonFiniteTrajectoryError instead of returning NaN",
          raised is not None and min(0.25, INSTABILITY_STEP_FACTOR * tau) == 0.25
          and 0.25 / tau > 3.3, str(raised))
    if raised is not None:
        ctx = raised.as_dict()
        check("instability trap: the error is S3/NUM, a NumericalError, and names the scenario, "
              "the first non-finite time (within 48 h), the key and the model version",
              ctx["severity"] == "S3" and ctx["class"] == "NUM"
              and isinstance(raised, HealthError)
              and type(raised).__mro__[1].__name__ == "NumericalError"
              and ctx["scenario"] == "chronic_high_salt_30d" and ctx["t"] <= 48
              and ctx["key"] in (*STATE_KEYS, *LEDGER_KEYS, *DERIVED_KEYS)
              and ctx["model_version"] == MODEL_VERSION and ctx["non_finite_values"] > 0
              and "HREQ-V-07" in ctx["message"], str(ctx))
        note(f"instability trap: {raised.message}")
    check("instability trap: the step factor is restored afterwards",
          api_module.MAX_STEP_FRACTION_OF_TAU_MIN == PRODUCTION_STEP_FACTOR)
    try:
        ok = simulate(params=p, scenario="chronic_high_salt_30d", t_end=48)
        finite = all(math.isfinite(v) for sec in ("states", "derived", "ledger")
                     for vals in ok[sec].values() for v in vals)
        normal = None
    except NonFiniteTrajectoryError as exc:
        finite, normal = False, str(exc)
    check("instability trap: the same 48 h at the production step factor runs, all values "
          "finite, no exception", normal is None and finite, str(normal))
    # The detector itself: one NaN deep in a derived series of an otherwise finite run.
    r = sim("drink_water_1L")
    der = {k: list(v) for k, v in r["derived"].items()}
    der["U_osm"][300] = math.nan
    try:
        api_module._refuse_non_finite(r["t"], r["states"], r["ledger"], der, "drink_water_1L")
        found = None
    except NonFiniteTrajectoryError as exc:
        found = exc.as_dict()
    check("non-finite gate: a single NaN in one derived series is found, with its key and time",
          found is not None and found["key"] == "U_osm" and found["section"] == "derived"
          and found["t"] == r["t"][300] and found["non_finite_values"] == 1, str(found))


def test_scenario_expectations_harness_evaluates_every_quantitative_expectation():
    kinds: dict[str, int] = {}
    for sc in SCENARIOS.values():
        for e in (sc.validation or {}).get("expects") or []:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    check("harness: V1 registers 24 expectations, by kind as docs/health/01 §7.2 states",
          kinds == V1_EXPECTATION_KINDS, str(kinds))
    check("harness: every registered kind is either countable or reported with a reason",
          set(kinds) <= set(COUNTABLE_KINDS) | set(NOT_CHECKED_KINDS), str(set(kinds)))
    total_rows = 0
    for sid, sc in SCENARIOS.items():
        r = sim(sid)
        expects = (sc.validation or {}).get("expects") or []
        rows = evaluate_expectations(sid, r)
        total_rows += len(rows)
        check(f"harness {sid}: one row per registered expectation ({len(expects)}), none skipped",
              len(rows) == len(expects) and [x["metric"] for x in rows] == [e["metric"] for e in expects],
              f"{len(rows)} rows for {len(expects)} expectations")
        bad_status = [x for x in rows if x["status"] not in ("pass", "fail", "not_checked")]
        silent = [x for x in rows if x["status"] == "not_checked" and not x["reason"]]
        check(f"harness {sid}: every row has status pass|fail|not_checked, and a reason when "
              "not checked", not bad_status and not silent, f"{bad_status or silent}")
        ranged_unscored = [x["metric"] for x, e in zip(rows, expects, strict=True)
                           if isinstance(e.get("range"), list) and e.get("kind") not in NOT_CHECKED_KINDS
                           and x["status"] == "not_checked"]
        check(f"harness {sid}: every expectation with a numeric range is computed (no harness gap)",
              not ranged_unscored, f"not computed: {ranged_unscored}")
        wrongly_scored = [x["metric"] for x in rows
                          if x["kind"] in NOT_CHECKED_KINDS and x["status"] != "not_checked"]
        check(f"harness {sid}: qualitative/unverified/known-divergence/design-target rows are "
              "never scored as a pass", not wrongly_scored, str(wrongly_scored))
        for x in rows:
            actual = "—" if x["actual"] is None else f"{x['actual']:.6g}"
            note(f"[{x['status']:>11}] {sid}: {x['metric']} = {actual}  ({x['reason']})")
        if sid in ("drink_water_1L", "salt_load_10g"):
            for x in rows:
                if x["kind"] != "quantitative":
                    continue
                finding = LITERATURE_FAILS.get((sid, x["metric"]))
                if finding is None:
                    check(f"expectation {sid}: '{x['metric']}' passes with default params "
                          f"(range {x['range']})", x["status"] == "pass",
                          f"actual {x['actual']!r}: {x['reason']}; evidence {x['evidence']}")
                    continue
                js = finding_record(sid, x["metric"])
                check(f"expectation {sid}: '{x['metric']}' is REPORTED as a counted FAIL "
                      f"(range {x['range']}; measured {x['actual']!r} h, JavaScript reference "
                      f"{js['actual']!r} h) -- a literature finding, not hidden",
                      x["status"] == "fail" and x["counted"] is True and "outside" in x["reason"]
                      and err_ratio(x["actual"], js["actual"]) <= 1, str(x))
    check("harness: one row for each of the 24 registered expectations", total_rows == 24,
          str(total_rows))
    global _HARNESS_SUMMARY
    _HARNESS_SUMMARY = summarize([x for sid in SCENARIOS
                                  for x in evaluate_expectations(sid, sim(sid))])
    num = evaluate_expectations("baseline", sim("baseline"))[0]
    check("harness: the numerical drift row is reported (value shown), never counted as a pass",
          num["status"] == "not_checked" and num["countable"] is False
          and num["actual"] is not None and num["actual"] < STEADY_STATE_DRIFT_MAX, str(num))
    # Refusals: a result for another scenario, and a run too short to compute a metric.
    try:
        evaluate_expectations("salt_load_10g", sim("drink_water_1L"))
        refused = False
    except ValueError:
        refused = True
    check("harness: refuses to score a result against another scenario's expectations", refused)
    short = simulate(scenario="salt_load_10g", t_end=12)
    rows = evaluate_expectations("salt_load_10g", short)
    r48 = next(x for x in rows if x["metric"] == "fraction excreted by 48 h")
    check("harness: a run too short for a metric is reported not_checked with the run length, "
          "never a substitute value",
          r48["status"] == "not_checked" and r48["actual"] is None
          and r48["reason"] == "run too short: t_end 12 h < scenario 72 h", str(r48))


def test_failing_expectation_is_pinned_to_the_reference_with_its_band():
    """The one V1 quantitative expectation the model fails with default parameters is a
    property of the MODEL, not of the port: the JavaScript reference, measured by the same
    definition, gives the same value; the Monte Carlo band and in-range share agree too."""
    for (sid, metric), why in LITERATURE_FAILS.items():
        js = finding_record(sid, metric)
        r = sim(sid)
        sc = SCENARIOS[sid]
        start = sc.events[0]["start"]
        row = next(x for x in evaluate_expectations(sid, r) if x["metric"] == metric)
        check(f"finding {sid}: harness value equals the JavaScript reference measured by the "
              "same definition", err_ratio(row["actual"], js["actual"]) <= 1,
              f"python {row['actual']!r}, javascript {js['actual']!r}")
        na, t = r["derived"]["Na_plasma"], r["t"]
        d6 = na[nearest_index(t, start + 6)] - na[0]
        check(f"finding {sid}: |ΔNa| is still >= 0.5 mmol/L 6 h after drinking starts, so ANY "
              "reading of 'recovered within 6 h' fails", abs(d6) >= 0.5
              and err_ratio(d6, js["dNa_6h_after_start"]) <= 1, f"{d6!r}")
        variants = recovery_time_variants(r, start)
        w = Worst()
        for k, e in js["variants"].items():
            w.add(f"variants.{k}", variants[k], e)
        lo, hi = row["range"]
        check(f"finding {sid}: both readings -- first time after the nadir |ΔNa| < 0.5, and the "
              "time after which it stays below -- equal the JavaScript reference and both fail",
              w.ok and variants["first_after_nadir_from_start"] == row["actual"]
              and all(not (lo <= variants[k] <= hi) for k in
                      ("first_after_nadir_from_start", "stays_below_from_start",
                       "first_after_nadir_from_nadir")), w.summary() + f"; {variants}")
        note(f"{sid}: readings (h): first after nadir, from start "
             f"{variants['first_after_nadir_from_start']:.4g}; stays below, from start "
             f"{variants['stays_below_from_start']:.4g}; first after nadir, from the nadir "
             f"{variants['first_after_nadir_from_nadir']:.4g}; JS at dt 1/30 and 0.1: "
             + ", ".join(f"{v['first_after_nadir_from_start']:.4g}"
                         for v in js["variantsByDt"].values())
             + f"; JS Monte Carlo median trajectory (n=256): "
             f"{js['mcMedianTrajectory256']['first_after_nadir_from_start']:.4g}")
        g8 = js["mc"]["8"]
        t0 = time.perf_counter()
        mc = monte_carlo_runs(sid, n=g8["n"], seed=g8["seed"])
        # n = 8 is the golden equivalence check, not a published band (HREQ-U-08).
        row_mc = next(x for x in evaluate_expectations(sid, r, mc=mc, allow_small_n=True)
                      if x["metric"] == metric)
        check(f"finding {sid}: a band at n = {g8['n']} is shown with its n and a band_reason, "
              "never as a published band (HREQ-U-08)",
              row_mc["n"] == g8["n"] and row_mc["band"] is not None
              and f"n = {g8['n']} < 256" in (row_mc["band_reason"] or ""), str(row_mc))
        w = Worst()
        for q in ("q05", "q50", "q95"):
            w.add(f"band.{q}", row_mc["band"][q], g8["band"][q])
        w.add("in_range_share", row_mc["in_range_share"], g8["in_range_share"])
        check(f"finding {sid}: the harness's Monte Carlo band and in-range share (n = {g8['n']}, "
              f"seed {g8['seed']}) equal the JavaScript reference's",
              w.ok and row_mc["n"] == g8["n"] and row_mc["rejected"] == g8["rejected"], w.summary())
        g256 = js["mc"]["256"]
        note(f"{sid}: {metric}: default {row['actual']:.4g} h vs range {row['range']}; band at "
             f"n = {g256['n']} (JS reference): q05 {g256['band']['q05']:.3g}, q50 "
             f"{g256['band']['q50']:.3g}, q95 {g256['band']['q95']:.3g} h; in-range share "
             f"{g256['in_range_share']:.3f} ({why}); n = 8 band here took "
             f"{time.perf_counter() - t0:.2f} s")


def test_calibration_and_structural_rows_are_never_counted_as_validation():
    """docs/health/03 §5.2 rules 4-5 and its planted registry fixtures."""
    # The shipped chronic rows: one calibration, one structural, neither counted.
    rows = evaluate_expectations("chronic_high_salt_30d", sim("chronic_high_salt_30d"))
    cal = next(x for x in rows if x["role"] == "calibration")
    design = next(x for x in rows if x["metric"] == "MAP time course")
    struct = next(x for x in rows if x["role"] == "structural")
    check("roles: chronic ΔMAP row is a calibration target of map_vol_exp, pn_gain, aldo_vol_exp",
          cal["metric"] == "ΔMAP at day 30 per +100 mmol/day Na"
          and cal["calibrates"] == ["map_vol_exp", "pn_gain", "aldo_vol_exp"]
          and "map_vol_exp" in cal["reason"], str(cal))
    check("roles: the D-2 'MAP time course' design target is the calibration target of "
          "map_auto_tau_h (docs/health/03 §12), not counted, its note naming D-2",
          design["role"] == "calibration" and design["calibrates"] == ["map_auto_tau_h"]
          and design["kind"] == "design-target" and design["status"] == "not_checked"
          and not design["counted"] and "map_auto_tau_h" in design["reason"]
          and "Decision D-2" in next(e for e in SCENARIOS["chronic_high_salt_30d"].validation[
              "expects"] if e["metric"] == "MAP time course")["note"], str(design))
    check("roles: chronic Na-balance row is structural",
          struct["metric"] == "Na excretion ≈ intake by day 30", str(struct))
    check("roles: calibration and structural rows are evaluated (pass/fail) but never counted",
          cal["status"] in ("pass", "fail") and struct["status"] in ("pass", "fail")
          and not cal["counted"] and not struct["counted"], f"{cal['status']}, {struct['status']}")
    summ = summarize(rows)
    check("roles: summarize() puts them in their own columns, not in the validation totals",
          summ["counted_pass"] + summ["counted_fail"] == 0
          and summ["calibration"]["n"] == 2 and summ["structural"]["n"] == 1
          and summ["calibration"]["statuses"]["pass"] + summ["calibration"]["statuses"]["fail"] == 1
          and summ["calibration"]["statuses"]["not_checked"] == 1
          and summ["structural"]["statuses"]["pass"] + summ["structural"]["statuses"]["fail"] == 1,
          str(summ))
    check("roles: summarize() names the 4 calibrated parameters of 03 §12 (for the CLI status "
          "line)", len(summ["calibrated_parameters"]) == 4
          and summ["calibrated_parameters"] == ["aldo_vol_exp", "map_auto_tau_h", "map_vol_exp",
                                                 "pn_gain"], str(summ["calibrated_parameters"]))
    every = [x for sid in SCENARIOS for x in evaluate_expectations(sid, sim(sid))]
    total = summarize(every)
    check("roles: V1 has 12 countable rows; 1 calibration + 1 structural leave 10 counted, all "
          "co-developed (docs/health/03 §5.2 rule 6)",
          sum(x["countable"] for x in every) == 12
          and total["counted_pass"] + total["counted_fail"] == 10
          and set(total["counted_by_registration"]) == {"co-developed"}, str(total))
    check("roles: V1 calibrates 4 parameters (aldo_vol_exp, map_auto_tau_h, map_vol_exp, "
          "pn_gain) against 10 independent counted rows (HREQ-M-12)",
          len(total["calibrated_parameters"]) == 4
          and total["independent_vs_calibrated"] == {"independent_counted": 10,
                                                     "calibrated_parameters": 4},
          str(total["independent_vs_calibrated"]))
    note(f"V1 totals: counted pass {total['counted_pass']}, counted fail {total['counted_fail']}, "
         f"not_checked {total['not_checked']}, calibration {total['calibration']}, structural "
         f"{total['structural']}, missing extractors {total['missing_extractors']}")
    check("every row carries model_version and the disclaimer (HREQ-M-01)",
          all(x["model_version"] == MODEL_VERSION and x["disclaimer"] == DISCLAIMER for x in every))

    # Planted registries on drink_water_1L (same id, so its result and extractors apply).
    base = SCENARIOS["drink_water_1L"]
    r = sim("drink_water_1L")
    base_expects = list(base.validation["expects"])
    # Planted copies drop the shipped explicit id, so they take a positional id.
    peak = {k: v for k, v in next(e for e in base_expects
                                  if e["metric"] == "peak urine flow").items() if k != "id"}

    def planted(*extra: dict[str, Any]) -> Any:
        return dataclasses.replace(base, validation={**base.validation,
                                                     "expects": base_expects + list(extra)})

    before = summarize(evaluate_expectations(base, r))
    cal_rows = evaluate_expectations(planted({**peak, "role": "calibration",
                                              "calibrates": ["adh_ec50"]}), r)
    after = summarize(cal_rows)
    check("planted: a PASSING calibration row does not move the validation pass count",
          cal_rows[-1]["status"] == "pass" and not cal_rows[-1]["counted"]
          and after["counted_pass"] == before["counted_pass"]
          and after["calibration"]["statuses"]["pass"] == before["calibration"]["statuses"]["pass"] + 1
          and after["calibrated_parameters"] == ["adh_ec50"],
          f"before {before['counted_pass']}, after {after['counted_pass']}")
    st_rows = evaluate_expectations(planted({**peak, "role": "structural"}), r)
    check("planted: a passing structural row does not move the validation pass count",
          summarize(st_rows)["counted_pass"] == before["counted_pass"]
          and summarize(st_rows)["structural"]["n"] == 1)
    gap = evaluate_expectations(planted({"metric": "planted metric with no extractor",
                                         "target": "x", "range": [0, 1],
                                         "kind": "quantitative"}), r)
    check("planted: a quantitative row with no extractor is not_checked with a reason, the row "
          "count is unchanged, and summarize() lists it by id",
          len(gap) == len(base_expects) + 1 and gap[-1]["status"] == "not_checked"
          and gap[-1]["reason"].startswith("NO METRIC IMPLEMENTATION")
          and summarize(gap)["missing_extractors"] == [gap[-1]["id"]], str(gap[-1]))
    key = ("drink_water_1L", "planted metric whose extractor returns NaN")
    METRICS[key] = lambda sc, rr: math.nan
    try:
        nan_rows = evaluate_expectations(planted({"metric": key[1], "target": "x",
                                                  "range": [0, 1], "kind": "quantitative"}), r)
    finally:
        del METRICS[key]
    check("planted: an extractor that returns NaN is a counted FAIL (a model that cannot "
          "answer has failed, not abstained)",
          nan_rows[-1]["status"] == "fail" and nan_rows[-1]["counted"], str(nan_rows[-1]))
    q_rows = evaluate_expectations(planted({"metric": "planted direction", "target": "rise",
                                            "kind": "qualitative"}), r)
    check("planted: a qualitative row is not_checked, with a reason, and not counted",
          q_rows[-1]["status"] == "not_checked" and q_rows[-1]["reason"]
          and not q_rows[-1]["counted"])
    try:
        evaluate_expectations(planted({**peak, "id": "dup"}, {**peak, "id": "dup"}), r)
        refused = False
    except ConfigurationError:
        refused = True
    check("planted: a registry with a duplicated expectation id is refused", refused)


def test_every_extractor_value_is_pinned_and_independently_recomputed():
    """docs/health/03 §5 (BUG-20261003-117): a status alone cannot see an extractor that
    reads the wrong window, unit or baseline -- the value moves and stays inside its range.

    (a) PINNED holds every extractor's default-parameter value (the run sim() makes: the
        scenario's dt, the 30-day scenario recorded every 3rd point), compared at REL_TOL.
        The literals were printed with repr() from this engine (model 1.0.1) and were
        cross-checked by (b) before they were written down; a model change that moves
        one must re-print it and explain the difference (golden-fixture rule).
    (b) Each metric is recomputed here from the result's own series, without the engine's
        ledger or the extractor's helpers: urine and Na excretion are trapezoid integrals of
        `urine_flow` (L/h) and `Na_excr` (mmol/h) over the stated window from the event
        start, minus window x the series' own value at t = 0 where the metric says
        "above baseline"; point readings come from the series and a grid search of our own.
    """
    PINNED = {
        "baseline/01": 9.719173059288608e-16,
        "drink_water_1L/01": -3.076296774764103,
        "drink_water_1L/02": 7.050000000000001,
        "drink_water_1L/03": 1.4666666666666668,
        "drink_water_1L/04": 0.5721408405333197,
        "drink_water_1L/05": 62.75778406888058,
        "drink_water_1L/06": 0.6897013984124625,
        "salt_load_10g/01": 2.1381547762933337,
        "salt_load_10g/03": 0.29551422866221755,
        "salt_load_10g/04": 0.9234361809558178,
        "chronic_high_salt_30d/01": 2.205914093919021,
        "chronic_high_salt_30d/03": 1.0002079475438233,
        "no_water_24h/01": 11.0079117304781,
    }
    # Tolerances of (b). Point readings use the same arithmetic as the extractor: REL_TOL.
    # Integrals: the trapezoid on the output grid vs the solver's RK4 ledger is an O(dt^2)
    # quadrature difference, measured at <= 2.6e-6 relative (water fraction, 1/60 h grid);
    # 1e-4 relative leaves margin and is still ~100x tighter than the smallest extractor
    # mutation the review planted (a shifted window or a missing baseline moves >= 1 %).
    # ΔMAP uses Na_excr(0) x 24 as the baseline intake (steady state), not the parameter.
    INTEGRAL_REL = 1e-4
    STEADY_REL = 1e-6
    rows = {x["id"]: x for sid in SCENARIOS for x in evaluate_expectations(sid, sim(sid))}
    scored = {i: x["actual"] for i, x in rows.items() if x["actual"] is not None}
    check("pins: every row with a computed value is pinned and every pin has a row "
          f"({len(PINNED)} extractors)",
          set(scored) == set(PINNED)
          and {(i.split("/")[0], rows[i]["metric"]) for i in PINNED} == set(METRICS),
          f"unpinned {sorted(set(scored) - set(PINNED))}, "
          f"pinned without a value {sorted(set(PINNED) - set(scored))}")
    w = Worst()
    for i, v in PINNED.items():
        w.add(i, scored.get(i, math.nan), v)
    check("pins: every extractor's default-parameter value equals its pinned literal "
          f"(REL_TOL {REL_TOL:g})", w.ok, w.summary())

    def k_at(t: Sequence[float], x: float) -> int:
        return min(range(len(t)), key=lambda k: (abs(t[k] - x), k))

    def trapz(t: Sequence[float], y: Sequence[float], a: float, b: float) -> float:
        ia, ib = k_at(t, a), k_at(t, b)
        return sum((t[k + 1] - t[k]) * (y[k] + y[k + 1]) / 2 for k in range(ia, ib))

    indep: dict[str, tuple[float, float]] = {}     # id -> (independent value, rel tolerance)
    r = sim("baseline")
    drift = 0.0
    for key in STATE_KEYS:
        s = r["states"][key]
        drift = max([drift] + [abs(v - s[0]) / (abs(s[0]) or 1.0) for v in s])
    indep["baseline/01"] = (drift, REL_TOL)

    r = sim("drink_water_1L")
    t, d = r["t"], r["derived"]
    ev = SCENARIOS["drink_water_1L"].events[0]
    na, uf = d["Na_plasma"], d["urine_flow"]
    k_nadir = na.index(min(na))
    indep["drink_water_1L/01"] = (min(na) - na[0], REL_TOL)
    indep["drink_water_1L/02"] = (next((t[k] - ev["start"] for k in range(k_nadir + 1, len(t))
                                        if abs(na[k] - na[0]) < 0.5), math.inf), REL_TOL)
    indep["drink_water_1L/03"] = (t[uf.index(max(uf))] - ev["start"], REL_TOL)
    indep["drink_water_1L/04"] = (max(uf), REL_TOL)
    indep["drink_water_1L/05"] = (min(d["U_osm"]), REL_TOL)
    indep["drink_water_1L/06"] = ((trapz(t, uf, ev["start"], ev["start"] + 3) - 3 * uf[0])
                                  / ev["water_L"], INTEGRAL_REL)

    r = sim("salt_load_10g")
    t, d = r["t"], r["derived"]
    ev = SCENARIOS["salt_load_10g"].events[0]
    na, ne = d["Na_plasma"], d["Na_excr"]
    load = ev["salt_g"] * 1000 / 58.44              # mmol Na in the NaCl load
    indep["salt_load_10g/01"] = (max(na) - na[0], REL_TOL)
    for rid, h in (("salt_load_10g/03", 8), ("salt_load_10g/04", 48)):
        indep[rid] = ((trapz(t, ne, ev["start"], ev["start"] + h) - h * ne[0]) / load,
                      INTEGRAL_REL)

    r = sim("chronic_high_salt_30d")
    t, ne, mp = r["t"], r["derived"]["Na_excr"], r["states"]["MAP"]
    ov = SCENARIOS["chronic_high_salt_30d"].overrides[0]
    t30 = ov["start"] + 30 * 24
    indep["chronic_high_salt_30d/01"] = (
        (mp[k_at(t, t30)] - mp[k_at(t, ov["start"])]) * 100
        / (ov["naIn_mmolh"] * 24 - ne[0] * 24), STEADY_REL)
    indep["chronic_high_salt_30d/03"] = (trapz(t, ne, t30 - 24, t30) / (ov["naIn_mmolh"] * 24),
                                         INTEGRAL_REL)

    r = sim("no_water_24h")
    t, osm = r["t"], r["derived"]["osm_plasma"]
    ov = SCENARIOS["no_water_24h"].overrides[0]
    indep["no_water_24h/01"] = (osm[k_at(t, ov["end"])] - osm[0], REL_TOL)

    check("independent: every pinned extractor has an independent recomputation",
          set(indep) == set(PINNED), str(sorted(set(PINNED) ^ set(indep))))

    # Whole-run extractors read the WHOLE run: on V1 data every extremum falls early, so an
    # extractor that looked only at the first part of the run would pass the pins. Plant
    # each extremum at the last output point of a full-length copy of the result.
    whole = []
    for sid, metric, series, sign in (
            ("drink_water_1L", "Na_plasma nadir − baseline", "Na_plasma", -1),
            ("drink_water_1L", "peak urine flow", "urine_flow", 1),
            ("drink_water_1L", "minimum urine osmolality", "U_osm", -1),
            ("salt_load_10g", "Na_plasma peak − baseline", "Na_plasma", 1)):
        r = sim(sid)
        y = list(r["derived"][series])
        y[-1] = (max(y) if sign > 0 else min(y)) + sign * 1.0
        planted = {**r, "derived": {**r["derived"], series: y}}
        row = next(x for x in evaluate_expectations(sid, planted) if x["metric"] == metric)
        want = y[-1] - (y[0] if "baseline" in metric else 0.0)
        whole.append((sid, metric, row["actual"], want))
    check("whole-run extractors: an extremum planted at the LAST output point is the value "
          "(nadir, peak flow, minimum U_osm, Na peak)",
          all(err_ratio(a, w) <= 1 for _, _, a, w in whole), str(whole))
    for rid, (v, tol) in indep.items():
        got = scored.get(rid, math.nan)
        ratio = err_ratio(got, v, rel=tol)
        check(f"independent: {rid} '{rows[rid]['metric']}' = {got!r} equals the recomputation "
              f"{v!r} to {tol:g} relative", ratio <= 1, f"{ratio:.3g} x tolerance")


def test_harness_refuses_registry_defects_and_scores_boundaries():
    """docs/health/03 §5.2 (BUG-20261003-120, -123, -124, -125): a registry defect is a
    ConfigurationError, never a quiet not_checked row; bounds are inclusive; ±inf is a
    counted fail; a non-number from an extractor is never coerced."""
    from health.engine.validate import RUN_TOO_SHORT, MetricUnavailable

    base = SCENARIOS["drink_water_1L"]
    r = sim("drink_water_1L")
    shipped = list(base.validation["expects"])
    E = [{k: v for k, v in e.items() if k != "id"} for e in shipped]   # positional ids
    peak, recovery = E[3], E[1]

    def planted(rows: Sequence[Mapping[str, Any]]) -> Any:
        return dataclasses.replace(base, validation={**base.validation, "expects": list(rows)})

    def outcome(rows: Sequence[Mapping[str, Any]]) -> str:
        try:
            got = evaluate_expectations(planted(rows), r)
        except ConfigurationError as exc:
            return f"ConfigurationError: {exc.message}"
        return f"accepted: last row {got[-1]['status']} ({got[-1]['reason']})"

    def refused(label: str, rows: Sequence[Mapping[str, Any]]) -> None:
        got = outcome(rows)
        check(f"planted: {label} is refused (ConfigurationError), never a quiet row",
              got.startswith("ConfigurationError"), got)

    check("ids: every shipped expectation has an explicit id <scenario_id>/<NN> (1-based, in "
          "registration order), unique over the registry",
          all(e.get("id") == f"{sid}/{i + 1:02d}" for sid, sc in SCENARIOS.items()
              for i, e in enumerate((sc.validation or {}).get("expects") or [])),
          str([e.get("id") for sc in SCENARIOS.values()
               for e in (sc.validation or {}).get("expects") or []]))
    # H3: kinds and ranges.
    refused("kind 'quantitive' on a copy of the recovery row", shipped + [{**recovery,
                                                                         "kind": "quantitive"}])
    refused("kind typo ON the recovery row (the 9 pass / 0 fail summary)",
            [shipped[0], {**shipped[1], "kind": "quantitive"}] + shipped[2:])
    refused("a row with no kind", E + [{k: v for k, v in peak.items() if k != "kind"}])
    refused("kind 'Quantitative' (case)", E + [{**peak, "kind": "Quantitative"}])
    refused("a quantitative row with no range", E + [{k: v for k, v in peak.items()
                                                      if k != "range"}])
    refused("a semi-quantitative row with no range",
            E + [{k: v for k, v in peak.items() if k != "range"} | {"kind": "semi-quantitative"}])
    refused("a calibration row naming no parameter", E + [{**peak, "role": "calibration"}])
    # H7: malformed ranges.
    refused("a reversed range [0.9, 0.35]", E + [{**peak, "range": [0.9, 0.35]}])
    refused("a NaN range [nan, nan]", E + [{**peak, "range": [math.nan, math.nan]}])
    refused("a half-NaN range [nan, 0.9]", E + [{**peak, "range": [math.nan, 0.9]}])
    refused("a range of strings", E + [{**peak, "range": ["0.35", "0.9"]}])
    refused("a range of three numbers", E + [{**peak, "range": [0.35, 0.9, 1]}])
    got = outcome(E + [{**peak, "range": [-math.inf, math.inf]}])
    check("planted: an intentionally unbounded range [-inf, inf] is accepted and scored",
          got.startswith("accepted: last row pass"), got)
    # H6: duplicate FINAL ids, explicit or positional.
    refused("an explicit id colliding with a positional default (drink_water_1L/01)",
            E + [{**peak, "id": "drink_water_1L/01"}])
    refused("an explicit id colliding with a shipped explicit id",
            shipped + [{**peak, "id": "drink_water_1L/04"}])

    # H8 and H3: planted extractors on range [0.35, 0.9].
    key = ("drink_water_1L", "planted boundary metric")
    row = {"metric": key[1], "target": "x", "range": [0.35, 0.9], "kind": "quantitative"}
    try:
        for value, want in ((0.35, "pass"), (0.9, "pass"), (math.nextafter(0.9, 1.0), "fail"),
                            (math.nextafter(0.35, 0.0), "fail"), (math.inf, "fail"),
                            (-math.inf, "fail"), (1, "fail")):
            METRICS[key] = lambda sc, rr, v=value: v
            last = evaluate_expectations(planted(E + [row]), r)[-1]
            check(f"planted: an extractor returning {value!r} on [0.35, 0.9] is a counted "
                  f"{want.upper()} (bounds inclusive; ±inf fails)",
                  last["status"] == want and last["counted"] is True, str(last))
        for value in ("0.5", None, True):
            METRICS[key] = lambda sc, rr, v=value: v
            refused(f"an extractor returning {value!r} (never coerced with float())", E + [row])
        METRICS[key] = lambda sc, rr: (_ for _ in ()).throw(
            MetricUnavailable("planted: no event", cause="scenario_has_no_event"))
        refused("an extractor unavailable for a reason other than run length", E + [row])
        METRICS[key] = lambda sc, rr: (_ for _ in ()).throw(
            MetricUnavailable("planted: run ends at 1 h", cause=RUN_TOO_SHORT))
        rows = evaluate_expectations(planted(E + [row]), r)
        summ = summarize(rows)
        check("planted: an extractor unavailable because the run is too short gives a "
              "not_checked row with the reason, listed by summarize() next to missing_extractors",
              rows[-1]["status"] == "not_checked" and not rows[-1]["counted"]
              and rows[-1]["reason"] == "unavailable: planted: run ends at 1 h"
              and summ["not_checked_countable"] == [{"id": rows[-1]["id"],
                                                     "reason": rows[-1]["reason"]}],
              str(summ["not_checked_countable"]))
    finally:
        del METRICS[key]


def test_harness_refuses_unfit_results_and_monte_carlo_sets():
    """BUG-20261003-118 (Monte Carlo set), -121 (truncated run), -122 (non-default result):
    the harness scores only what it was asked to score, and stamps what it scored."""
    from health.engine.validate import RUN_TOO_SHORT, MetricUnavailable, params_digest

    r = sim("drink_water_1L")

    def raises(label: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            got = "accepted"
        except ConfigurationError as exc:
            got = f"ConfigurationError: {exc.message}"
        check(f"{label} is refused (ConfigurationError)", got.startswith("ConfigurationError"),
              got)

    # H4: a truncated run is never scored with substitute values.
    short = simulate(scenario="drink_water_1L", t_end=1.8)
    rows = evaluate_expectations("drink_water_1L", short)
    summ = summarize(rows)
    check("truncated run (t_end 1.8 h of 12 h): every row is not_checked with the run length, "
          "nothing is counted, and summarize() lists all 6 countable rows with that reason",
          all(x["status"] == "not_checked" and x["actual"] is None and not x["counted"]
              and "run too short: t_end 1.8 h < scenario 12 h" in x["reason"] for x in rows)
          and summ["counted_pass"] + summ["counted_fail"] == 0
          and len(summ["not_checked_countable"]) == 6,
          str([(x["id"], x["status"], x["reason"]) for x in rows]))
    causes = []
    for (sid, metric), fn in METRICS.items():
        if sid != "drink_water_1L":
            continue
        try:
            fn(SCENARIOS[sid], short)
            causes.append((metric, "value"))
        except MetricUnavailable as exc:
            causes.append((metric, exc.cause))
    check("truncated run: every drink_water_1L extractor called directly -- peak, nadir, "
          "minimum and recovery included -- refuses with cause run_too_short",
          len(causes) == 6 and all(c == RUN_TOO_SHORT for _, c in causes), str(causes))
    na = list(r["derived"]["Na_plasma"])
    k_nadir = na.index(min(na))
    stuck = {**r, "derived": {**r["derived"],
                              "Na_plasma": na[:k_nadir] + [na[k_nadir]] * (len(na) - k_nadir)}}
    rec = evaluate_expectations("drink_water_1L", stuck)[1]
    check("full-length run that never recovers: the recovery row is +inf and a counted FAIL",
          rec["actual"] == math.inf and rec["status"] == "fail" and rec["counted"], str(rec))

    # H5: non-default parameters or dt are refused unless params_override; meta stamps both.
    meta = evaluate_expectations("drink_water_1L", r)[0]["meta"]
    check("row meta stamps the params digest, dt and t_end of the scored result",
          meta["params_digest"] == params_digest(default_params()) and meta["dt"] == 1 / 60
          and meta["t_end"] == 12 and meta["params_override"] is False, str(meta))
    p = draw_samples(n=1, seed=3)["samples"][0]
    other = simulate(scenario="drink_water_1L", params=p)
    raises("a result with non-default parameters, without params_override,",
           lambda: evaluate_expectations("drink_water_1L", other))
    coarse = simulate(scenario="drink_water_1L", dt=0.5)
    raises("a result at dt 0.5 h (scenario dt 1/60 h), without params_override,",
           lambda: evaluate_expectations("drink_water_1L", coarse))
    m_other = evaluate_expectations("drink_water_1L", other, params_override=True)[0]["meta"]
    m_coarse = evaluate_expectations("drink_water_1L", coarse, params_override=True)[0]["meta"]
    check("params_override=True scores it and the row meta says so (digest, dt)",
          m_other["params_override"] is True and m_other["params_digest"] == params_digest(p)
          and m_other["params_digest"] != meta["params_digest"]
          and m_coarse["params_override"] is True and m_coarse["dt"] == 0.5,
          f"{m_other} {m_coarse}")

    # H1: a Monte Carlo set must be this scenario's, on this grid, of size n >= 256.
    mc2 = monte_carlo_runs("drink_water_1L", n=2, seed=1)
    raises("a Monte Carlo set for another scenario",
           lambda: evaluate_expectations("drink_water_1L", r, allow_small_n=True,
                                         mc=monte_carlo_runs("drink_water_3L_fast", n=2, seed=1)))
    raises("a Monte Carlo set with another t_end",
           lambda: evaluate_expectations("drink_water_1L", r, allow_small_n=True,
                                         mc=monte_carlo_runs("drink_water_1L", n=2, seed=1,
                                                             t_end=6)))
    raises("a Monte Carlo set at another dt",
           lambda: evaluate_expectations("drink_water_1L", r, allow_small_n=True,
                                         mc=monte_carlo_runs("drink_water_1L", n=2, seed=1,
                                                             dt=0.1)))
    raises("a Monte Carlo set whose run count is not its n",
           lambda: evaluate_expectations("drink_water_1L", r, allow_small_n=True,
                                         mc={**mc2, "results": mc2["results"][:1]}))
    raises("a Monte Carlo set of n = 2 without allow_small_n (HREQ-U-08)",
           lambda: evaluate_expectations("drink_water_1L", r, mc=mc2))
    small = evaluate_expectations("drink_water_1L", r, mc=mc2, allow_small_n=True)
    row = small[0]
    check("allow_small_n=True: the n = 2 band is shown with its n and a band_reason",
          row["band"] is not None and row["n"] == 2 and row["seed"] == 1
          and "n = 2 < 256" in row["band_reason"], str(row))
    check("summarize(): a counted row whose band is at n < 256 still counts as unbanded "
          "(all 6 counted drink_water_1L rows)",
          summarize(small)["unbanded_counted"] == 6, str(summarize(small)["unbanded_counted"]))


def test_every_row_publishes_its_band_fields_and_versions():
    """HREQ-V-15 / HREQ-U-08 / HREQ-M-12 (BUG-20261003-119, -126, -130): every row carries
    its band fields (None and a band_reason when there is no band), the registry version and
    the model version; summarize() counts unbanded counted rows and never hides them."""
    from health.engine.validate import registry_version

    every = [x for sid in SCENARIOS for x in evaluate_expectations(sid, sim(sid))]
    fields = {"band": None, "in_range_share": None, "n": 0, "seed": None, "rejected": None,
              "band_reason": "no Monte Carlo supplied"}
    check("no Monte Carlo: every row carries band, in_range_share, n, seed, rejected and "
          "band_reason (None / 0 / 'no Monte Carlo supplied')",
          all({k: x.get(k, "MISSING") for k in fields} == fields for x in every),
          str([x["id"] for x in every if any(x.get(k, "MISSING") != v
                                             for k, v in fields.items())]))
    total = summarize(every)
    check("summarize(): all 10 counted rows are unbanded without Monte Carlo",
          total["unbanded_counted"] == 10 == total["counted_pass"] + total["counted_fail"],
          str(total["unbanded_counted"]))
    version = registry_version()
    check("every row's meta and the summary carry the registry version (sha256 of the "
          "registered expectation records) and the model version",
          len(version) == 64 and total["registry_version"] == version
          and all(x["meta"].get("registry_version") == version
                  and x["meta"].get("modelVersion") == MODEL_VERSION for x in every), version)
    base = SCENARIOS["drink_water_1L"]
    edited = dataclasses.replace(base, validation={
        **base.validation, "expects": [{**base.validation["expects"][0], "target": "edited"}]
        + list(base.validation["expects"][1:])})
    r = sim("drink_water_1L")
    ed_rows = evaluate_expectations(edited, r)
    check("a registry edit (one target text) changes the registry version",
          ed_rows[0]["meta"]["registry_version"] != version
          and ed_rows[0]["meta"]["registry_version"] == registry_version(edited))
    try:
        summarize(every + ed_rows)
        mixed = "accepted"
    except ConfigurationError as exc:
        mixed = f"ConfigurationError: {exc.message}"
    check("summarize() refuses rows from two registry versions",
          mixed.startswith("ConfigurationError"), mixed)
    mc256 = {"results": [r] * 256, "n": 256, "seed": 0, "rejected": 0}
    banded = evaluate_expectations("drink_water_1L", r, mc=mc256)
    s256 = summarize(banded)
    check("a Monte Carlo set of n = 256 gives every counted row a published band "
          "(band_reason None) and summarize() then counts 0 unbanded",
          all(x["band"] is not None and x["band_reason"] is None for x in banded if x["counted"])
          and s256["unbanded_counted"] == 0 and s256["counted_pass"] + s256["counted_fail"] == 6,
          str(s256["unbanded_counted"]))
    by_id = {x["id"]: x for x in every}
    div, unv = by_id["chronic_high_salt_30d/04"], by_id["salt_load_sweep/02"]
    check("known-divergence and unverified rows state WHAT diverges / is unverified (their "
          "note, else their target) in the reason",
          "Heer 2000 found plasma volume +315 mL" in div["reason"]
          and "Audit F-02/F-03" in unv["reason"], f"{div['reason']} | {unv['reason']}")


_SWEEP: dict[str, Any] | None = None
_HARNESS_SUMMARY: dict[str, Any] | None = None
_INFLUENCE: dict[str, Any] | None = None


def test_salt_dose_sweep_is_monotonic():
    global _SWEEP
    g = golden()["simulateSweep"]
    t0 = time.perf_counter()
    _SWEEP = simulate_sweep(n=g["n"], seed=g["seed"], values=g["values"])
    note(f"simulate_sweep(values={g['values']}, n={g['n']}, seed={g['seed']}) "
         f"took {time.perf_counter() - t0:.2f} s")
    for metric in ("peak_strain_index", "peak_dNa"):
        for band in ("q05", "q50", "q95"):
            v = _SWEEP["metrics"][metric][band]
            check(f"sweep: {metric} {band} is non-decreasing over doses {g['values']} g",
                  all(v[i] <= v[i + 1] for i in range(len(v) - 1)), str(v))
        note(f"{metric} q50 = {[round(x, 6) for x in _SWEEP['metrics'][metric]['q50']]}")
    w = Worst()
    for metric, bands in g["metrics"].items():
        for band, vals in bands.items():
            for i, e in enumerate(vals):
                w.add(f"{metric}.{band}[{g['values'][i]} g]", _SWEEP["metrics"][metric][band][i], e)
    check("sweep: every metric band matches JS simulateSweep", w.ok and _SWEEP["rejected"] == g["rejected"],
          w.summary() + f"; rejected {_SWEEP['rejected']} vs {g['rejected']}")
    note(w.summary())


def test_infeasible_params_are_refused_not_silently_accepted():
    cases = {
        "required urine osmolality above U_osm_max (U_osm_max = 500)": {"U_osm_max": 500},
        "non-positive steady-state urine flow (water intake 0.5 L/day)": {"waterIn_base_Ld": 0.5},
        "degenerate concentrating range (U_osm_min = U_osm_max)": {"U_osm_min": 1200},
        # docs/health/03 §9 fixture: inside every range but water intake 1.3 L/day.
        "required urine osmolality 1,229 mOsm/kg above U_osm_max 1,200 (water intake "
        "1.3 L/day, 03 §9)": {"waterIn_base_Ld": 1.3},
    }
    for label, edits in cases.items():
        p = default_params() | edits
        check(f"infeasible: constants() flags {label}", constants(p)["feasible"] is False)
        for what, fn in (("initial_state", lambda p=p: initial_state(p)),
                         ("simulate", lambda p=p: simulate(params=p, scenario="baseline", t_end=1))):
            try:
                fn()
                raised = None
            except InfeasibleParametersError as exc:
                raised = str(exc)
                taxonomy = isinstance(exc, HealthError) and exc.severity.value == "S3"
            check(f"infeasible: {what} raises health.errors.InfeasibleParametersError for {label}",
                  raised is not None and "infeasible params" in raised and taxonomy, str(raised))
    gr = golden()["rejection"]
    d = draw_samples(n=gr["n"], seed=gr["seed"],
                     table=_apply_table_edits(param_table(), gr["tableEdits"]))
    ok = all(constants(s)["feasible"]
             and s["na_normal_low"] <= constants(s)["Na_ss"] <= s["na_normal_high"]
             for s in d["samples"])
    check("infeasible: draw_samples counts its rejections and returns only feasible, "
          "normonatremic samples", d["rejected"] > 0 and ok, f"rejected {d['rejected']}")
    tab = param_table()
    tab["U_osm_max"].update({"value": 300, "range": [300, 300], "mc": False})
    try:
        draw_samples(n=2, seed=1, table=tab)
        gave_up = None
    except InfeasibleParametersError as exc:
        gave_up = str(exc)
    check("infeasible: draw_samples raises InfeasibleParametersError when no feasible sample "
          "exists, after 50·n draws",
          gave_up is not None and "0/2 feasible samples after 100 draws" in gave_up, str(gave_up))

    # docs/health/03 §9 fixtures, as numbers (HREQ-V-25).
    u13 = constants(default_params() | {"waterIn_base_Ld": 1.3})
    check("§9 fixture: water intake 1.3 L/day requires urine at 1228.57 mOsm/kg (> U_osm_max "
          "1200), so it is infeasible",
          round(u13["U_ss"], 2) == 1228.57 and u13["feasible"] is False, f"{u13['U_ss']!r}")
    p296 = default_params() | {"adh_threshold": 296}
    c296 = constants(p296)
    try:
        y296 = initial_state(p296)
        det_ok = abs(y296[STATE_KEYS.index("Na_ecf")] / y296[STATE_KEYS.index("V_ecf")]
                     - c296["Na_ss"]) < 1e-9
    except InfeasibleParametersError:
        det_ok = False
    check("§9 fixture: adh_threshold = 296 mOsm/kg is FEASIBLE with baseline sodium 145.61 "
          "mmol/L, above na_normal_high 145 (the deterministic path accepts it)",
          c296["feasible"] is True and round(c296["Na_ss"], 2) == 145.61
          and c296["Na_ss"] > p296["na_normal_high"] and det_ok, f"{c296['Na_ss']!r}")

    def frozen_table(**values: float) -> dict[str, Any]:
        """Every entry held fixed: each draw is exactly the default set with `values`."""
        tab = param_table()
        for e in tab.values():
            e["mc"] = False
        for k, v in values.items():
            tab[k]["value"] = v
        return tab

    try:
        draw_samples(n=2, seed=1, table=frozen_table(adh_threshold=296))
        refused = None
    except InfeasibleParametersError as exc:
        refused = exc
    check("§9 fixture: draw_samples REJECTS adh_threshold = 296 by its normonatremia rule "
          "(every one of 100 draws rejected and counted, then it raises)",
          refused is not None and refused.context.get("rejected") == 100
          and "0/2 feasible samples after 100 draws" in refused.message, str(refused))
    control = draw_samples(n=2, seed=1, table=frozen_table())
    check("§9 fixture: the same frozen table at the default adh_threshold (281) is accepted "
          "with no rejection, so the rejection above is the normonatremia rule's",
          control["rejected"] == 0 and len(control["samples"]) == 2, str(control["rejected"]))


def test_results_carry_the_disclaimer_and_reproducibility_fields():
    r = sim("drink_water_1L")
    m = r["meta"]
    check("simulate: meta.disclaimer is the V1 disclaimer (HREQ-S-01)", m["disclaimer"] == DISCLAIMER)
    check("simulate: result records model version, params, scenario and dt (HREQ-P-03)",
          m["modelVersion"] == MODEL_VERSION and r["scenario"] == "drink_water_1L"
          and r["params"] == default_params() and m["dt"] == 1 / 60)
    check("simulate: result keys are the JS keys",
          set(r) == {"t", "Y", "states", "derived", "ledger", "params", "scenario", "meta"}
          and list(r["states"]) == list(STATE_KEYS) and list(r["derived"]) == list(DERIVED_KEYS)
          and list(r["ledger"]) == list(LEDGER_KEYS))
    check("simulate: Y[i] is states[STATE_KEYS[i]]",
          all(r["Y"][i] is r["states"][k] for i, k in enumerate(STATE_KEYS)))
    sweep = _SWEEP if _SWEEP is not None else {"disclaimer": DISCLAIMER}
    check("simulate_sweep: result carries the disclaimer", sweep["disclaimer"] == DISCLAIMER)
    check("nearest_index: first index on a tie, as JS",
          nearest_index([0.0, 1.0, 2.0], 0.5) == 0 and nearest_index([0.0, 1.0, 2.0], 1.6) == 2)


@needs("yaml")
def test_config_agrees_with_engine_and_reference():
    """config/health/base.yaml holds every tolerance, seed and threshold (HREQ-N-06); the
    engine mirrors the JavaScript's literal constants. The three must agree: a bump in one
    place and not the others is a CFG defect (docs/health/05 §3)."""
    import yaml

    cfg = yaml.safe_load(HEALTH_CONFIG.read_text(encoding="utf-8"))
    js = (REFERENCE_ENGINE / "index.js").read_text(encoding="utf-8")
    m = re.search(r"export const MODEL_VERSION = '([^']+)'", js)
    js_version = m.group(1) if m else None
    m = re.search(r"export const DISCLAIMER = '([^']+)'", js)
    js_disclaimer = m.group(1) if m else None
    check("config: model version agrees in base.yaml, the Python engine, index.js and the fixture",
          cfg["model"]["version"] == MODEL_VERSION == js_version == golden()["modelVersion"],
          f"yaml {cfg['model']['version']!r}, python {MODEL_VERSION!r}, js {js_version!r}")
    check("config: disclaimer agrees in base.yaml, the Python engine and index.js",
          cfg["model"]["disclaimer"] == DISCLAIMER == js_disclaimer)
    check("config: validation status agrees in base.yaml and the Python engine",
          cfg["model"].get("validation_status") == VALIDATION_STATUS,
          repr(cfg["model"].get("validation_status")))
    rp = cfg["model"]["reference_person"]
    check("config: reference person agrees with REFERENCE_PERSON",
          (rp["name"], rp["mass_kg"], rp["bsa_m2"]) == (REFERENCE_PERSON["name"],
                                                       REFERENCE_PERSON["mass_kg"],
                                                       REFERENCE_PERSON["bsa_m2"]), str(rp))
    eq = cfg["equivalence"]
    check("config: equivalence tolerances are the ones this suite applies",
          (eq["relative_tolerance"], eq["absolute_tolerance"]) == (REL_TOL, ABS_TOL), str(eq))
    check("config: golden fixture and generator paths are the ones this suite reads",
          ROOT / eq["golden_fixture"] == GOLDEN_PATH and eq["generator"] == "tools/health_golden.mjs"
          and (ROOT / eq["generator"]).exists(), str(eq))
    nm = cfg["numerics"]
    check("config: numerical gates (drift, mass balance, step bound) agree",
          (nm["steady_state_drift_max"], nm["mass_balance_tolerance"],
           nm["max_step_fraction_of_tau_min"])
          == (STEADY_STATE_DRIFT_MAX, MASS_BALANCE_TOL, MAX_STEP_FRACTION_OF_TAU_MIN), str(nm))
    mc = cfg["monte_carlo"]
    sig = inspect.signature(simulate_mc).parameters
    check("config: Monte Carlo defaults, quantiles, sampling rule and retry budget agree",
          mc["default_n"] == sig["n"].default and mc["default_seed"] == sig["seed"].default
          and mc["quantiles"] == [0.05, 0.5, 0.95] and mc["log_uniform_ratio"] == LOG_UNIFORM_RATIO
          and mc["max_tries_per_sample"] == MAX_TRIES_PER_SAMPLE, str(mc))
    check("config: the small test n is the n the golden fixture was generated with",
          mc["test_n"] == golden()["simulateMC"]["n"] == golden()["drawSamples"]["n"]
          and mc["test_n_sweep"] == golden()["simulateSweep"]["n"], str(mc))
    inf = cfg["influence"]
    check("config: sensitivity-screen defaults agree with INFLUENCE_DEFAULTS",
          (inf["scenario"], inf["t_end_h"], inf["dt_h"], inf["relative_perturbation"],
           inf["threshold"]) == (INFLUENCE_DEFAULTS["scenario"], INFLUENCE_DEFAULTS["tEnd"],
                                 INFLUENCE_DEFAULTS["dt"], INFLUENCE_DEFAULTS["rel"],
                                 INFLUENCE_DEFAULTS["threshold"]), str(inf))
    ex = cfg["expectations"]
    check("config: countable and reported expectation kinds agree with the harness",
          tuple(ex["countable_kinds"]) == COUNTABLE_KINDS
          and set(ex["reported_kinds"]) == set(NOT_CHECKED_KINDS), str(ex))
    label = SCENARIOS["chronic_high_salt_30d"].label
    check("config: the chronic scenario's trajectory label literal in scenarios.py equals "
          "display.trajectory_label (one copy of each mandated label, HREQ-S-03)",
          label is not None and label == cfg["display"]["trajectory_label"],
          f"scenarios.py {label!r}, base.yaml {cfg['display'].get('trajectory_label')!r}")


def test_every_public_result_carries_disclaimer_and_validation_status():
    """HREQ-S-01 as amended: every result object carries meta.disclaimer, byte-equal to the
    JavaScript DISCLAIMER, and meta.validation_status = "Not clinically validated."."""
    js = (REFERENCE_ENGINE / "index.js").read_text(encoding="utf-8")
    m = re.search(r"export const DISCLAIMER = '([^']+)'", js)
    js_disclaimer = m.group(1) if m else None
    check("disclaimer: health.engine.DISCLAIMER is byte-equal to DISCLAIMER in index.js",
          js_disclaimer is not None
          and DISCLAIMER.encode("utf-8") == js_disclaimer.encode("utf-8"), repr(js_disclaimer))
    check('validation status: VALIDATION_STATUS is "Not clinically validated."',
          VALIDATION_STATUS == "Not clinically validated.", repr(VALIDATION_STATUS))
    sweep = _SWEEP if _SWEEP is not None else simulate_sweep(n=1, seed=1, values=[0])
    infl = _INFLUENCE if _INFLUENCE is not None else compute_influence(
        scenario="drink_water_1L", t_end=0.5, dt=0.1)
    rows = [x for sid in SCENARIOS for x in evaluate_expectations(sid, sim(sid))]
    results: list[tuple[str, Mapping[str, Any]]] = [
        ("simulate", sim("drink_water_1L")),
        ("simulate_mc", simulate_mc(scenario="drink_water_1L", n=2, seed=1, t_end=1,
                                    keys=["Na_plasma"])),
        ("simulate_sweep", sweep),
        ("compute_influence", infl),
        ("draw_samples", draw_samples(n=2, seed=1)),
        ("salt_load_metrics", salt_load_metrics(sim("salt_load_10g"))),
        ("param_summary", param_summary()),
        ("monte_carlo_runs", monte_carlo_runs("drink_water_1L", n=1, seed=1, t_end=1)),
        ("summarize", summarize(rows)),
    ] + [(f"expectation row {x['id']}", x) for x in rows]
    missing = [name for name, r in results
               if not isinstance(r.get("meta"), Mapping)
               or r["meta"].get("disclaimer") != DISCLAIMER
               or r["meta"].get("validation_status") != VALIDATION_STATUS
               or r["meta"].get("modelVersion") != MODEL_VERSION]
    check(f"every public result ({len(results)} objects: simulate, simulate_mc, simulate_sweep, "
          "compute_influence, draw_samples, salt_load_metrics, param_summary, monte_carlo_runs, "
          "summarize and all 24 expectation rows) carries meta.disclaimer, "
          "meta.validation_status and meta.modelVersion (HREQ-M-01)", not missing,
          f"missing on: {missing}")
    check("the JavaScript-shaped top-level disclaimer of simulate_mc / simulate_sweep is kept",
          results[1][1]["disclaimer"] == DISCLAIMER and sweep["disclaimer"] == DISCLAIMER)


# ---------------------------------------------------------------------------
# Runner (shape of tests/selftest.py).
# ---------------------------------------------------------------------------
def discover() -> list[tuple[str, Callable[[], None]]]:
    """Every `test_*` function in this module, in definition order (no hand-kept list)."""
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    tests.sort(key=lambda nf: inspect.getsourcelines(nf[1])[1])
    return tests


def exit_code(n_failed: int, n_skipped: int, *, strict: bool) -> int:
    """Exit 0 only when nothing FAILED -- and, under `--no-skips`, nothing was skipped."""
    if n_failed:
        return 1
    if strict and n_skipped:
        return 2
    return 0


def robust_status(ran: bool, n_robust: int) -> str:
    """The summary line's statement of whether the slow gates ran (03 §3.1, §3.4)."""
    if ran:
        return f"robust gates ran ({n_robust} test functions, --robust)"
    return f"robust gates NOT run ({n_robust} test functions need --robust)"


def summary_lines(n_tests: int, n_passed: int, n_failed: int,
                  skipped: list[tuple[str, tuple[str, ...]]], robust_note: str = "") -> list[str]:
    """The end-of-run report. Skipped tests are LISTED by name above the counts."""
    lines: list[str] = []
    if skipped:
        lines.append(f"SKIPPED ({len(skipped)}) -- not run here; `--no-skips` runs them for real:")
        for name, mods in skipped:
            lines.append(f"  {name}  (needs: {', '.join(mods)})")
        lines.append("")
    mods = sorted({m for _, ms in skipped for m in ms})
    tail = f", {len(skipped)} skipped (needs: {', '.join(mods)})" if skipped else ", 0 skipped"
    lines.append(f"{n_tests} test functions, {n_passed} passed, {n_failed} failed{tail}"
                 + (f"; {robust_note}" if robust_note else ""))
    return lines


def run_suite(tests: list[tuple[str, Callable[[], None]]], *, strict: bool = False,
              robust_note: str = "") -> int:
    """Run `tests` in order, print the report, return the process exit code."""
    t_start = time.perf_counter()
    for name, fn in tests:
        missing = missing_modules(fn)
        print(f"\n--- {name} ---")
        if missing:
            SKIPPED.append((name, missing))
            print(f"SKIP  {name} -- needs {', '.join(missing)}, not importable here")
            continue
        t0 = time.perf_counter()
        try:
            fn()
        except Exception:  # noqa: BLE001 - a crashing test is recorded as a FAIL, not lost
            check(f"{name}: ran to completion without an exception", False,
                  traceback.format_exc().strip().replace("\n", "\n        "))
        print(f"      ({time.perf_counter() - t0:.2f} s)")
    print("\n" + "=" * 72)
    if _HARNESS_SUMMARY is not None:   # docs/health/03 §5.2 rules 3 and 6
        h = _HARNESS_SUMMARY
        missing = h["missing_extractors"]
        print(f"MISSING EXTRACTORS ({len(missing)})" + (": " + ", ".join(missing) if missing else ""))
        print(f"expectations (default params): {h['rows']} rows; counted {h['counted_pass']} pass, "
              f"{h['counted_fail']} fail; {h['not_checked']} not_checked; calibration "
              f"{h['calibration']['n']} {h['calibration']['statuses']} (calibrates "
              f"{', '.join(h['calibrated_parameters'])}); structural {h['structural']['n']} "
              f"{h['structural']['statuses']}; independent {h['independent_vs_calibrated']} "
              f"· unbanded counted {h['unbanded_counted']} · countable not_checked "
              f"{h['not_checked_countable']}")
    for line in summary_lines(len(tests), len(PASS), len(FAIL), SKIPPED, robust_note):
        print(line)
    print(f"wall time {time.perf_counter() - t_start:.1f} s")
    if FAIL:
        print("\nFAILURES:")
        for f in FAIL:
            print("  " + f)
    if strict and SKIPPED:
        print("\n--no-skips: a skipped test is a test that did not run. Install the "
              f"missing module(s) -- {', '.join(sorted({m for _, ms in SKIPPED for m in ms}))} "
              "-- and run again.")
    print("=" * 72)
    return exit_code(len(FAIL), len(SKIPPED), strict=strict)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-skips", action="store_true",
                    help="fail if ANY test was skipped for a missing module "
                         "(the mode gates.py and the post-Install CI step use)")
    ap.add_argument("--robust", action="store_true",
                    help="also run the slow gates: convergence on every scenario and the stiff "
                         "corner, and the n = 256 x 24 h steady-state drift (docs/health/03 "
                         "§3.1, §3.4)")
    a = ap.parse_args(argv)
    global ROBUST
    ROBUST = a.robust
    print("=" * 72)
    print(f"NAVANAX HEALTH ENGINE SELF-TEST  (model {MODEL_VERSION}; stdlib only; golden "
          f"{GOLDEN_PATH.relative_to(ROOT)})" + ("  [--no-skips]" if a.no_skips else "")
          + ("  [--robust]" if a.robust else ""))
    print(DISCLAIMER)
    print("=" * 72)
    every = discover()
    n_robust = sum(1 for _, fn in every if getattr(fn, "robust", False))
    tests = [(n, fn) for n, fn in every if ROBUST or not getattr(fn, "robust", False)]
    return run_suite(tests, strict=a.no_skips, robust_note=robust_status(ROBUST, n_robust))


if __name__ == "__main__":
    raise SystemExit(main())
