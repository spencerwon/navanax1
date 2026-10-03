"""Standard-library-only self-test for the health error hierarchy (src/health/errors.py).

Runnable with a bare `python3 tests/health_errors_selftest.py` -- no pytest, nothing to
install, well under two seconds. It is the `errors` module's own test (docs/health/07):
the class hierarchy of docs/health/05 §6, the severity and halt behaviour each class
declares, the context every raise carries, and the rule CI also enforces with a grep --
`SurfaceIntegrityError` is never caught and swallowed -- run here against src/health and
against a planted violation, so the rule is proven to fire.

Conventions are those of tests/selftest.py: module-level `test_*` functions in definition
order, `check()` records each assertion, `--no-skips` refuses to exit 0 if anything was
skipped (nothing here can be: there is no third-party import).
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import health.errors as errors_module  # noqa: E402
from health.errors import (  # noqa: E402
    CalibrationAsValidationError,
    ConfigurationError,
    ErrorClass,
    EvidenceIntegrityError,
    ExpectationSkippedError,
    HealthError,
    InfeasibleParametersError,
    InPlaceEditError,
    MisquotedSourceError,
    MissingDisclaimerError,
    MissingUncertaintyError,
    NumericalError,
    OperationalError,
    PersistenceError,
    ProvenanceError,
    ReferenceDivergenceError,
    Severity,
    SilentWrongnessError,
    StepControlError,
    SurfaceIntegrityError,
    UngradedValueError,
    UnitMismatchError,
    UnlabelledIndexError,
    UnresolvedEvidenceError,
)

PASS: list[str] = []
FAIL: list[str] = []
SKIPPED: list[tuple[str, tuple[str, ...]]] = []

#: The pattern of the CI step "SurfaceIntegrityError is never swallowed (health)". It reads
#: a handler's body as running to the next column-0 line, so a swallowing handler followed,
#: in the same function, by any `raise` passes it; `swallowed()` below reads the syntax tree.
CI_SWALLOW_RE = re.compile(r"except\s+SurfaceIntegrityError[^\n]*:\n((?:\s+.*\n)+?)(?=\S|\Z)")


def check(name: str, cond: bool, detail: str = "") -> None:
    suffix = f" -- {detail}" if detail and not cond else ""
    PASS.append(name) if cond else FAIL.append(f"{name}{suffix}")
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"\n        {detail}" if detail and not cond else ""))


def error_classes() -> list[type[HealthError]]:
    """Every HealthError subclass health.errors defines, the base included -- so a class
    added later is held to the same rules without this file naming it."""
    return [obj for _, obj in inspect.getmembers(errors_module, inspect.isclass)
            if issubclass(obj, HealthError) and obj.__module__ == errors_module.__name__]


def _surface_names() -> set[str]:
    return {c.__name__ for c in error_classes() if issubclass(c, SurfaceIntegrityError)}


def _names_a_surface_error(node: ast.expr | None, names: set[str]) -> bool:
    if isinstance(node, ast.Tuple):
        return any(_names_a_surface_error(e, names) for e in node.elts)
    return (isinstance(node, ast.Name) and node.id in names) or \
        (isinstance(node, ast.Attribute) and node.attr in names)


def _raises(body: list[ast.stmt]) -> bool:
    """A `raise` in the handler's own body (not in a function defined inside it)."""
    todo: list[ast.AST] = list(body)
    while todo:
        n = todo.pop()
        if isinstance(n, ast.Raise):
            return True
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(n))
    return False


def _swallow_tool():
    """tools/swallow_check.py, the check both CI steps run (loaded by path: tools/ is not a
    package)."""
    import importlib.util
    path = ROOT / "tools" / "swallow_check.py"
    spec = importlib.util.spec_from_file_location("swallow_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def swallowed(source: str) -> list[int]:
    """Line numbers of every `except` clause catching SurfaceIntegrityError or a subclass
    (alone, in a tuple, or as `errors.X`) whose own body never raises -- the same reading
    the suite keeps in `_names_a_surface_error`/`_raises`, cross-checked against the tool."""
    names = _surface_names()
    here = sorted(n.lineno for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.ExceptHandler) and _names_a_surface_error(n.type, names)
                  and not _raises(n.body))
    tool = _swallow_tool().swallowed(source, names)
    if tool != here:
        raise AssertionError(f"tools/swallow_check.py disagrees with the suite: {tool} vs {here}")
    return here


# ---------------------------------------------------------------------------
def test_hierarchy_is_the_documented_one() -> None:
    """docs/health/05 §6: each named class sits under the family root that decides its
    severity, and the family roots sit directly under HealthError."""
    families = {
        SurfaceIntegrityError: (MissingDisclaimerError, UnlabelledIndexError),
        EvidenceIntegrityError: (UnresolvedEvidenceError, MisquotedSourceError,
                                 ReferenceDivergenceError),
        SilentWrongnessError: (MissingUncertaintyError, CalibrationAsValidationError,
                               UnitMismatchError, ExpectationSkippedError),
        ProvenanceError: (UngradedValueError, InPlaceEditError),
        NumericalError: (InfeasibleParametersError, StepControlError),
        OperationalError: (PersistenceError,),
    }
    for root, members in families.items():
        check(f"hierarchy: {root.__name__} is a direct HealthError subclass",
              HealthError in root.__bases__, str(root.__mro__))
        wrong = [c.__name__ for c in members if not issubclass(c, root)]
        check(f"hierarchy: {', '.join(c.__name__ for c in members)} are {root.__name__}s",
              not wrong, str(wrong))
    check("hierarchy: ConfigurationError is a HealthError (S3 unless it silently changed a "
          "result)", issubclass(ConfigurationError, HealthError)
          and ConfigurationError.severity is Severity.S3)
    check("hierarchy: no family root is a subclass of another family root",
          not [(a.__name__, b.__name__) for a in families for b in families
               if a is not b and issubclass(a, b)])


def test_every_error_class_declares_its_halt_behaviour() -> None:
    """Severity in the type system (05 §6): every class -- including any added later --
    declares a Severity, an ErrorClass and both halt flags; S0a and only S0a withholds the
    value; S0b and only S0b halts release; the base is S3 and halts nothing."""
    classes = error_classes()
    check(f"halt flags: health.errors defines at least the 23 classes of 2026-10-03 "
          f"({len(classes)} found)", len(classes) >= 23, str([c.__name__ for c in classes]))
    for c in classes:
        sev, cls = c.severity, c.error_class
        check(f"{c.__name__}: severity is a Severity ({getattr(sev, 'value', sev)}) and the "
              f"class an ErrorClass ({getattr(cls, 'value', cls)})",
              isinstance(sev, Severity) and isinstance(cls, ErrorClass))
        check(f"{c.__name__}: withholds_value and halts_release are booleans",
              isinstance(c.withholds_value, bool) and isinstance(c.halts_release, bool))
        check(f"{c.__name__}: withholds the value exactly when it is S0a "
              f"(severity {getattr(sev, 'value', sev)}, withholds {c.withholds_value})",
              c.withholds_value == (sev is Severity.S0A))
        check(f"{c.__name__}: halts release exactly when it is S0b "
              f"(severity {getattr(sev, 'value', sev)}, halts {c.halts_release})",
              c.halts_release == (sev is Severity.S0B))
    check("base: HealthError is S3, withholds nothing, halts nothing",
          HealthError.severity is Severity.S3 and not HealthError.withholds_value
          and not HealthError.halts_release)
    check("S0a: every SurfaceIntegrityError subclass withholds the value",
          all(c.withholds_value for c in classes if issubclass(c, SurfaceIntegrityError)))
    check("S0b: every EvidenceIntegrityError subclass halts release",
          all(c.halts_release for c in classes if issubclass(c, EvidenceIntegrityError)))
    check("S1: every SilentWrongnessError subclass is S1",
          all(c.severity is Severity.S1 for c in classes if issubclass(c, SilentWrongnessError)))
    check("S2: every ProvenanceError subclass is S2",
          all(c.severity is Severity.S2 for c in classes if issubclass(c, ProvenanceError)))
    check("ETH: the missing-disclaimer and unlabelled-index errors are ethics findings",
          MissingDisclaimerError.error_class is ErrorClass.ETH
          and UnlabelledIndexError.error_class is ErrorClass.ETH)
    check("NUM: a Python/JavaScript divergence is a numerics finding that halts release",
          ReferenceDivergenceError.error_class is ErrorClass.NUM
          and ReferenceDivergenceError.halts_release)


def test_severity_and_class_values_are_the_taxonomy() -> None:
    """docs/health/05 §2 and §3: the values a ledger entry and a raise share."""
    check("Severity values are S0a S0b S1 S2 S3 S4, in that order",
          [s.value for s in Severity] == ["S0a", "S0b", "S1", "S2", "S3", "S4"])
    want = {"MDL", "PRM", "EVD", "NUM", "KBI", "VAL", "PRS", "ETH", "CFG", "INF", "SEC"}
    check("ErrorClass holds the eleven health classes",
          {c.value for c in ErrorClass} == want, str(sorted(c.value for c in ErrorClass)))
    check("Severity and ErrorClass are str enums (a value compares equal to its text)",
          Severity.S0A == "S0a" and ErrorClass.CFG == "CFG")


def test_every_raise_carries_its_context() -> None:
    """05 §6.1: which parameter, which scenario, which evidence id, which model version --
    in the exception, in as_dict(), and in the message a log shows."""
    exc = ReferenceDivergenceError("trajectories differ", expected=1.0, received=1.1,
                                   parameter="adh_slope", scenario="baseline",
                                   evidence_id="ev:x", model_version="9.9.9", step=7)
    d = exc.as_dict()
    check("as_dict: type, severity, class and both halt flags",
          d["type"] == "ReferenceDivergenceError" and d["severity"] == "S0b"
          and d["class"] == "NUM" and d["halts_release"] is True
          and d["withholds_value"] is False, str(d))
    check("as_dict: every context field, extra keywords included",
          (d["parameter"], d["scenario"], d["evidence_id"], d["model_version"], d["step"],
           d["expected"], d["received"]) == ("adh_slope", "baseline", "ev:x", "9.9.9", 7,
                                             1.0, 1.1), str(d))
    s = str(exc)
    check("str: severity/class first, then parameter, scenario, evidence, values, model",
          s.startswith("[S0b/NUM] trajectories differ") and "parameter=adh_slope" in s
          and "scenario=baseline" in s and "evidence=ev:x" in s
          and "expected=1.0 received=1.1" in s and s.endswith("model=9.9.9"), s)
    check("str: an exception with no context is just its tag and message",
          str(SurfaceIntegrityError("no disclaimer")) == "[S0a/PRS] no disclaimer")
    try:
        raise MissingDisclaimerError("status rendered no disclaimer")
    except SurfaceIntegrityError as caught:   # re-raised below: never swallowed
        check("catching: a MissingDisclaimerError is caught as a SurfaceIntegrityError and "
              "as a HealthError", isinstance(caught, HealthError) and caught.withholds_value)
        try:
            raise caught
        except HealthError:
            pass


def test_surface_integrity_error_is_never_swallowed(tmp: Path) -> None:
    """05 §6.1: a surface that cannot render the disclaimer renders "unavailable"; it never
    catches SurfaceIntegrityError and shows the number anyway. The rule over src/health
    (the CI grep's pattern), and proof that it fires on a planted swallow."""
    bad: list[str] = []
    scanned = 0
    for p in sorted((ROOT / "src" / "health").rglob("*.py")):
        scanned += 1
        bad += [f"{p.relative_to(ROOT)}:{n}" for n in swallowed(p.read_text(encoding="utf-8"))]
    check(f"never swallowed: no `except SurfaceIntegrityError` without a re-raise in "
          f"src/health ({scanned} files)", scanned > 0 and not bad, str(bad))
    planted = (
        "def show(value):\n"                                  # 1
        "    try:\n"
        "        render(value)\n"
        "    except SurfaceIntegrityError:\n"                 # 4: swallowed
        "        return value\n"
        "\n"
        "def show_ok(value):\n"
        "    try:\n"
        "        render(value)\n"
        "    except SurfaceIntegrityError as exc:\n"          # 10: re-raised
        "        log(exc)\n"
        "        raise\n"
        "\n"
        "def show_masked(value):\n"
        "    try:\n"
        "        render(value)\n"
        "    except (KeyError, errors.MissingDisclaimerError):\n"   # 17: swallowed
        "        value = None\n"
        "\n"
        "    if value is None:\n"
        "        raise ValueError('later, unrelated')\n"
        "    return value\n")
    check("never swallowed: planted handlers that return or drop the value are caught at "
          "their lines (4, 17) -- a subclass in a tuple included; the one that re-raises "
          "(10) is not", swallowed(planted) == [4, 17], str(swallowed(planted)))
    regex_hits = [planted[:m.start()].count("\n") + 1 for m in CI_SWALLOW_RE.finditer(planted)
                  if "raise" not in m.group(1)]
    check("never swallowed: the CI steps' old regular expression misses the planted swallows "
          "(it is kept here only as the record of BUG-20261003-168/169)", regex_hits == [],
          str(regex_hits))
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    check("never swallowed: both CI steps run tools/swallow_check.py (the syntax-tree check), "
          "and no step uses the regular expression", ci.count("tools/swallow_check.py") >= 2
          and "re.finditer(r\"except" not in ci, str(ci.count("tools/swallow_check.py")))
    tool = _swallow_tool()
    check("never swallowed: the tool fires on the same plant for DataIntegrityError-style "
          "names given with --names", tool.swallowed(planted.replace("SurfaceIntegrityError",
          "DataIntegrityError"), {"DataIntegrityError"}) == [4], "")
    planted_dir = tmp / "swallow-plant" / "pkg"
    planted_dir.mkdir(parents=True)
    (planted_dir / "view.py").write_text(planted, encoding="utf-8")
    code = tool.main(["--names", "SurfaceIntegrityError,MissingDisclaimerError",
                      str(planted_dir.parent)])
    check("never swallowed: the tool's command line exits 1 on the planted file", code == 1,
          str(code))
    check("never swallowed: the tool's --family health.errors:SurfaceIntegrityError covers every "
          "surface class", tool.family("health.errors:SurfaceIntegrityError") == _surface_names(),
          str(tool.family("health.errors:SurfaceIntegrityError") ^ _surface_names()))


# ---------------------------------------------------------------------------
# Runner (same conventions as tests/selftest.py)
# ---------------------------------------------------------------------------
def discover() -> list[tuple[str, object]]:
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    tests.sort(key=lambda nf: inspect.getsourcelines(nf[1])[1])
    return tests


def main(argv: list[str] | None = None) -> int:
    import argparse
    import shutil

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-skips", action="store_true",
                    help="fail if ANY test was skipped (none can be: stdlib only)")
    a = ap.parse_args(argv)
    print("=" * 72)
    print("HEALTH ERRORS SELF-TEST  (stdlib only)" + ("  [--no-skips]" if a.no_skips else ""))
    print("=" * 72)
    t0 = time.perf_counter()
    tests = discover()
    tmp = Path(tempfile.mkdtemp(prefix="health-errors-selftest-"))
    try:
        for name, fn in tests:
            print(f"\n--- {name} ---")
            try:
                if inspect.signature(fn).parameters:
                    fn(tmp)
                else:
                    fn()
            except Exception as exc:  # noqa: BLE001 - a crashing test is a FAIL, not an abort
                check(f"{name} ran to completion", False, repr(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    wall = time.perf_counter() - t0
    check(f"the suite runs in under 2 s ({wall:.2f} s)", wall < 2.0)
    print("\n" + "=" * 72)
    print(f"{len(tests)} test functions, {len(PASS)} passed, {len(FAIL)} failed, "
          f"{len(SKIPPED)} skipped")
    print(f"wall time {wall:.2f} s")
    if FAIL:
        print("\nFAILURES:")
        for f in FAIL:
            print("  " + f)
    print("=" * 72)
    if FAIL:
        return 1
    return 2 if a.no_skips and SKIPPED else 0


if __name__ == "__main__":
    raise SystemExit(main())
