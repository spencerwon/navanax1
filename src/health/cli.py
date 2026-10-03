"""Health subsystem command line (stdlib only).

    python -m health.cli kb-check   [--root DIR] [--params FILE]
    python -m health.cli kb-summary [--root DIR] [--params FILE] [--json]
    python -m health.cli status     [--root DIR] [--params FILE]

kb-check exits 1 when any `error` finding is reported (or the KB cannot be
loaded at all) and 0 otherwise; warnings and info never fail it. --root points
at a directory holding the five KB files (default: the packaged data).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from health.kb import KBLoadError, load_kb
from health.kb.check import SEVERITIES, Finding, check_kb, counts_by_severity
from health.kb.report import format_share, format_summary, summary

DISCLAIMER = "Educational model — not medical advice."


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
    return f"KB files: {kb_dir}\nparams:   {paths.get('params') or '(none found)'}"


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
        return "unavailable"
    return str(MODEL_VERSION)


def _status_header() -> None:
    print(f"health {_version()}")
    print(f"model {_model_version()}")


def cmd_status(kb: dict[str, Any], _args: argparse.Namespace) -> int:
    """HREQ-P-07 order: package version, model version, KB version and curator, counts,
    parameter grade distribution and the >= B share, then the disclaimer on its own line."""
    s = summary(kb)
    c = s["counts"]
    counts = counts_by_severity(check_kb(kb))
    _status_header()
    print(f"KB {s['kbVersion']}  generated {s['generated']}  curator {s['curator']}")
    print(f"entities {c['entities']}  relations {c['relations']}  evidence {c['evidence']}  "
          f"verification records {c['verificationRecords']}  quantities {c['quantities']}")
    grades = s["paramsByGrade"]
    print(f"engine params by grade ({c['params']}): "
          + ", ".join(f"{g} {n}" for g, n in grades.items()))
    print(f"engine params graded >= B: {format_share(s['paramsGradedAtLeastB'])}  "
          f"E-assumption: {grades.get('E-assumption', 0)}")
    print(f"KB contract: {counts['error']} error(s), {counts['warn']} warning(s) "
          "(python -m health.cli kb-check for detail)")
    print(_modules_line())
    print(DISCLAIMER)
    return 0


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
             "status": "package and KB version, counts, grade share, disclaimer"}
    for name, text in helps.items():
        p = sub.add_parser(name, help=text, description=text)
        p.add_argument("--root", default=None,
                       help="directory holding the KB JSON files (default: packaged data)")
        p.add_argument("--params", default=None,
                       help="engine params.json to verify mirrors against (default: ROOT/"
                            "params.json, else the packaged engine/params.json)")
        if name == "kb-summary":
            p.add_argument("--json", action="store_true", help="print the summary as JSON")
    return ap


def _ensure_printable() -> None:
    """Never crash on the disclaimer's dash under a non-UTF-8 stdout."""
    for stream in (sys.stdout, sys.stderr):
        try:
            DISCLAIMER.encode(stream.encoding or "ascii")
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
            print(DISCLAIMER)
        return 1
    return COMMANDS[args.command](kb, args)


if __name__ == "__main__":
    raise SystemExit(main())
