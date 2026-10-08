"""Fail when an exception that must never be swallowed is caught without a re-raise.

    python3 tools/swallow_check.py --names DataIntegrityError src
    PYTHONPATH=src python3 tools/swallow_check.py --family health.errors:SurfaceIntegrityError src/health

A handler "swallows" when its own body (not a function defined inside it) never executes a
`raise`. The check reads the syntax tree, so it is not fooled by a `raise` later in the same
function, by a tuple (`except (KeyError, MissingDisclaimerError):`), or by a qualified name
(`except errors.MissingDisclaimerError:`). The regular expression the CI steps used until
2026-10-03 read a handler's body as running to the next column-0 line, which made any later
`raise` in the function hide the swallow -- it flagged nothing on a planted swallow
(BUG-20261003-168, BUG-20261003-169). `--family module:Class` imports `module` and takes
`Class` and every subclass it defines, so a class added later is covered without editing CI.

Exit 1 with one `::error::` line per swallowing handler; exit 0 and `ok (N files)` otherwise.
Standard library only.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import pathlib
import sys


def _names_one(node: ast.expr | None, names: set[str]) -> bool:
    if isinstance(node, ast.Tuple):
        return any(_names_one(e, names) for e in node.elts)
    return (isinstance(node, ast.Name) and node.id in names) or \
        (isinstance(node, ast.Attribute) and node.attr in names)


def _raises(body: list[ast.stmt]) -> bool:
    """A `raise` in the handler's own body (not inside a nested function or class)."""
    todo: list[ast.AST] = list(body)
    while todo:
        n = todo.pop()
        if isinstance(n, ast.Raise):
            return True
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(n))
    return False


def swallowed(source: str, names: set[str]) -> list[int]:
    """Line numbers of every `except` clause catching one of `names` (alone, in a tuple, or
    qualified) whose own body never raises."""
    return sorted(n.lineno for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.ExceptHandler) and _names_one(n.type, names)
                  and not _raises(n.body))


def family(spec: str) -> set[str]:
    """`module:Class` -> the class name and every subclass defined in that module."""
    mod_name, _, cls_name = spec.partition(":")
    mod = importlib.import_module(mod_name)
    base = getattr(mod, cls_name)
    return {name for name, obj in inspect.getmembers(mod, inspect.isclass)
            if issubclass(obj, base) and obj.__module__ == mod.__name__}


def scan(root: pathlib.Path, names: set[str]) -> tuple[list[str], int]:
    """(problems as `path:line`, files scanned) over every .py file under root."""
    bad: list[str] = []
    scanned = 0
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        scanned += 1
        try:
            lines = swallowed(p.read_text(encoding="utf-8"), names)
        except SyntaxError as exc:                       # a file CI cannot parse is a finding
            bad.append(f"{p}: cannot parse ({exc})")
            continue
        bad += [f"{p}:{n}" for n in lines]
    return bad, scanned


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=pathlib.Path, help="directory to scan for .py files")
    ap.add_argument("--names", default="", help="comma-separated exception class names")
    ap.add_argument("--family", action="append", default=[],
                    help="module:Class -- that class and every subclass the module defines")
    a = ap.parse_args(argv)
    names = {n.strip() for n in a.names.split(",") if n.strip()}
    for spec in a.family:
        names |= family(spec)
    if not names:
        print("::error::swallow_check: no exception names given (--names or --family)")
        return 2
    bad, scanned = scan(a.root, names)
    if bad:
        print(f"::error::{', '.join(sorted(names))} caught without re-raise (S1):", *bad,
              sep="\n  ")
        return 1
    print(f"ok ({scanned} files, {len(names)} exception names)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
