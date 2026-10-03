"""Health subsystem command line (stdlib only).

    python -m health.cli kb-check   [--root DIR] [--params FILE]
    python -m health.cli kb-summary [--root DIR] [--params FILE] [--json]
    python -m health.cli status     [--root DIR] [--params FILE] [--fast]

kb-check exits 1 when any `error` finding is reported (or the KB cannot be
loaded at all) and 0 otherwise; warnings and info never fail it. status exits on
the same rule -- 1 when the KB cannot be loaded or breaks its contract (it lists
the error findings), 0 otherwise -- and whatever happens it ends with the
disclaimer and then the validation status, each on its own line (HREQ-P-07,
HREQ-S-01). --root points at a directory holding the five KB files (default: the
packaged data). status --fast skips the expectation harness run (~3-8 s).

status honours the module flags (HREQ-X-01, docs/health/07 "Flags and what reads them"):
with engine-v1 disabled in config/health/modules.yaml it prints
`model unavailable (engine-v1 disabled in config/health/modules.yaml)` and
`expectations: skipped (engine-v1 disabled)` and never imports the engine; a registry
that cannot be read fails closed the same way. kb-check and kb-summary read no flag: the
knowledge base contract holds whatever the surfaces show.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any

from health.kb import KBLoadError, load_kb
from health.kb.check import SEVERITIES, Finding, check_kb, counts_by_severity
from health.kb.report import UNAVAILABLE, assumption_count, format_share, format_summary, summary

DISCLAIMER = "Educational model — not medical advice."
VALIDATION_STATUS = "Not clinically validated."
"""config/health/base.yaml model.disclaimer and model.validation_status (the self-test
holds these copies, the engine's and the config's to one text). Constants, not a config
read: the surface must print them even when nothing else can be loaded."""
STATUS_MAX_ERRORS = 20
ENGINE_MODULE = "engine-v1"
"""The registry id whose flag governs the model and expectations lines of status."""


def _version() -> str:
    try:
        from health import __version__
    except ImportError:
        return "unknown"
    return str(__version__)


def _print_findings(findings: list[Finding]) -> None:
    for sev in SEVERITIES:
        group = [f for f in findings if f.severity == sev]
        print(f"{sev.upper()} ({len(group)})")
        if not group:
            print("  (none)")
        width = max((len(f.code) for f in group), default=0)
        for f in group:
            print(f"  {f.code:<{width}}  {f.where}")
            print(f"  {'':<{width}}    {f.message}")
        print()


def _sources(kb: dict[str, Any]) -> str:
    paths = kb.get("paths", {})
    kb_dir = paths.get("entities", "?").rsplit("/", 1)[0].rsplit("\\", 1)[0]
    return (f"KB files: {kb_dir}\nparams:   {paths.get('params') or '(none found)'}\n"
            f"params.data.js: {paths.get('params_js') or '(none found)'}")


def cmd_kb_check(kb: dict[str, Any], _args: argparse.Namespace) -> int:
    findings = check_kb(kb)
    counts = counts_by_severity(findings)
    print(_sources(kb))
    print()
    _print_findings(findings)
    print(format_summary(summary(kb)))
    print()
    verdict = "FAIL" if counts["error"] else "PASS"
    print(f"kb-check: {verdict} -- {counts['error']} error(s), {counts['warn']} warning(s), "
          f"{counts['info']} info")
    return 1 if counts["error"] else 0


def cmd_kb_summary(kb: dict[str, Any], args: argparse.Namespace) -> int:
    s = summary(kb)
    if args.json:
        print(json.dumps(s, indent=2, ensure_ascii=False))
    else:
        print(format_summary(s))
    return 0


def _model_version() -> str:
    """The engine's model version, or "unavailable" -- never a guess (HREQ-N-01)."""
    try:
        from health.engine import MODEL_VERSION
    except Exception:  # noqa: BLE001 - any engine import failure means "unavailable"
        return UNAVAILABLE
    return str(MODEL_VERSION)


def _module_gate(module_id: str) -> tuple[str, str] | None:
    """None when `module_id` is enabled in the registry; otherwise (short, long) -- the
    reason a surface prints in place of the module's output. HREQ-X-01: the flag governs
    the surface. Fails closed: a registry that cannot be read shows the module as
    unavailable, never as on."""
    try:
        from health.registry import display_path, module_state, registry_path
        state = module_state(module_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable registry fails closed, never open
        return ("module registry unreadable",
                f"module registry unreadable: {type(exc).__name__}: {exc}")
    if state == "enabled":
        return None
    word = "disabled" if state == "disabled" else "not registered"
    return f"{module_id} {word}", f"{module_id} {word} in {display_path(registry_path())}"


def _status_header() -> None:
    print(f"health {_version()}")
    gate = _module_gate(ENGINE_MODULE)
    print(f"model {_model_version()}" if gate is None else f"model {UNAVAILABLE} ({gate[1]})")


def _expectations(fast: bool) -> str:
    """expectations_line() behind the engine-v1 flag: a disabled engine is not run."""
    gate = _module_gate(ENGINE_MODULE)
    if gate is not None:
        return f"expectations: skipped ({gate[0]})"
    return expectations_line(fast)


def _footer() -> None:
    """The last two lines of every status run, whatever happened before them."""
    print(DISCLAIMER)
    print(VALIDATION_STATUS)


def expectations_line(fast: bool = False) -> str:
    """HREQ-D-06: the expectation harness over every registered scenario at default
    parameters (config expectations.evaluate_on), summarised by validate.summarize().
    "unavailable" when the engine cannot be imported or the run fails -- never zeros.
    Also printed, never hidden: independent counted rows vs calibrated parameters
    (HREQ-M-12), countable rows left not_checked (with ids), how many counted rows have a
    published band (n >= 256, HREQ-U-08 / V-15) and the registry version (12 hex digits)."""
    if fast:
        return "expectations: skipped (--fast)"
    try:
        from health.engine import SCENARIOS, simulate
        from health.engine.validate import evaluate_expectations, summarize
    except Exception as exc:  # noqa: BLE001 - any engine import failure means "unavailable"
        print(f"status: expectations unavailable: the engine cannot be imported "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return f"expectations: {UNAVAILABLE}"
    try:
        rows: list[dict[str, Any]] = []
        for sid in SCENARIOS:
            rows.extend(evaluate_expectations(sid, simulate(scenario=sid)))
        s = summarize(rows)
    except Exception as exc:  # noqa: BLE001 - a failed run is shown as unavailable, not as 0
        print(f"status: expectations unavailable: the harness run failed "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return f"expectations: {UNAVAILABLE}"
    kinds = Counter(r.get("kind") for r in rows)
    cal = list(s["calibrated_parameters"])
    counted = s["counted_pass"] + s["counted_fail"]
    banded = counted - s["unbanded_counted"]
    no_mc = all(not r.get("n") for r in rows)
    why = ("no Monte Carlo in this run; " if no_mc else "") + "n ≥ 256 required for a published band"
    ivc = s["independent_vs_calibrated"]
    unscored = s["not_checked_countable"]
    return (f"expectations {s['rows']}: counted pass {s['counted_pass']} · "
            f"counted fail {s['counted_fail']} · not_checked {s['not_checked']} · "
            f"calibration {s['calibration']['n']} · structural {s['structural']['n']} · "
            f"known-divergence {kinds['known-divergence']} · unverified {kinds['unverified']} · "
            f"calibrated params {len(cal)}" + (f" ({', '.join(cal)})" if cal else "")
            + f" · independent {ivc['independent_counted']} vs calibrated "
            f"{ivc['calibrated_parameters']} (HREQ-M-12)"
            + f" · countable not_checked {len(unscored)}"
            + (f" ({', '.join(x['id'] for x in unscored)})" if unscored else "")
            + f" · bands {banded} of {counted} counted"
            + (f" ({why})" if s["unbanded_counted"] else "")
            + f" · registry {str(s['registry_version'])[:12]}")


def _status_body(kb: dict[str, Any], args: argparse.Namespace) -> int:
    s = summary(kb)
    c = s["counts"]
    findings = check_kb(kb)
    counts = counts_by_severity(findings)
    print(f"KB {s['kbVersion']}  generated {s['generated']}  curator {s['curator']}")
    print(f"entities {c['entities']}  relations {c['relations']}  evidence {c['evidence']}  "
          f"verification records {c['verificationRecords']}  quantities {c['quantities']}")
    grades = s["paramsByGrade"]
    if grades:
        print(f"engine params by grade ({c['params']}): "
              + ", ".join(f"{g} {n}" for g, n in grades.items()))
    else:
        print(f"engine params by grade: {UNAVAILABLE}")
    print(f"engine params graded >= B: {format_share(s['paramsGradedAtLeastB'])}  "
          f"E-assumption: {assumption_count(s)}")
    if s.get("paramsUnavailable"):
        print(f"status: engine parameter grade share and E-assumption count unavailable: "
              f"{s['paramsUnavailable']}", file=sys.stderr)
    print(f"KB contract: {counts['error']} error(s), {counts['warn']} warning(s) "
          "(python -m health.cli kb-check for detail)")
    errors = [f for f in findings if f.severity == "error"]
    for f in errors[:STATUS_MAX_ERRORS]:
        print(f"  error  {f.code}  {f.where}: {f.message}")
    if len(errors) > STATUS_MAX_ERRORS:
        print(f"  ... and {len(errors) - STATUS_MAX_ERRORS} more error(s)")
    print(_expectations(args.fast))
    print(_modules_line())
    return 1 if errors else 0


def cmd_status(kb: dict[str, Any], args: argparse.Namespace) -> int:
    """HREQ-P-07 order: package version, model version, KB version and curator, counts,
    parameter grade distribution and the >= B share, the KB contract (and its errors),
    the expectation counts (HREQ-D-06), the modules, then the disclaimer and the
    validation status, each on its own line, LAST -- even if a line above fails."""
    _status_header()
    try:
        code = _status_body(kb, args)
    except Exception as exc:  # noqa: BLE001 - the footer must still be printed
        print(f"status: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"status: {UNAVAILABLE} (internal error, see stderr)")
        code = 1
    _footer()
    return code


def _modules_line() -> str:
    """HREQ-X-01: every registered module with its flag, so a disabled one is visible.
    A missing or unreadable registry is said plainly (CFG), never shown as 'no modules'."""
    try:
        from health.registry import format_modules, load_modules
        return format_modules(load_modules())
    except FileNotFoundError as exc:
        return f"modules: registry not found ({exc.filename}) -- CFG defect, docs/health/05 §3"
    except Exception as exc:  # noqa: BLE001 - the status line must still print the disclaimer
        return f"modules: registry unreadable ({type(exc).__name__}: {exc})"


COMMANDS = {"kb-check": cmd_kb_check, "kb-summary": cmd_kb_summary, "status": cmd_status}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m health.cli",
                                 description="Navanax health subsystem: knowledge-base checks "
                                             "and status. " + DISCLAIMER)
    sub = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")
    helps = {"kb-check": "check the KB against its contract; exit 1 on any error finding",
             "kb-summary": "counts by type, scale, relation, source and grade",
             "status": "versions, KB counts, grade share, contract, expectations, modules, "
                       "disclaimer"}
    for name, text in helps.items():
        p = sub.add_parser(name, help=text, description=text)
        p.add_argument("--root", default=None,
                       help="directory holding the KB JSON files (default: packaged data)")
        p.add_argument("--params", default=None,
                       help="engine params.json to verify mirrors against (default: ROOT/"
                            "params.json, else the packaged engine/params.json)")
        if name == "kb-summary":
            p.add_argument("--json", action="store_true", help="print the summary as JSON")
        if name == "status":
            p.add_argument("--fast", action="store_true",
                           help="skip the expectation harness run (prints 'skipped (--fast)')")
    return ap


def _ensure_printable() -> None:
    """Never crash on the disclaimer's dash under a non-UTF-8 stdout."""
    for stream in (sys.stdout, sys.stderr):
        try:
            (DISCLAIMER + " · ").encode(stream.encoding or "ascii")
        except (UnicodeEncodeError, LookupError):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _ensure_printable()
    args = build_parser().parse_args(argv)
    try:
        kb = load_kb(args.root, params_path=args.params)
    except KBLoadError as exc:
        print(f"{args.command}: cannot load the knowledge base: {exc}", file=sys.stderr)
        if args.command == "status":       # a surface that fails still carries the disclaimer
            _status_header()
            print("KB unavailable")
            print(_expectations(args.fast))
            print(_modules_line())
            _footer()
        return 1
    return COMMANDS[args.command](kb, args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:              # stdout closed early (`| head`): exit quietly
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(1) from None
