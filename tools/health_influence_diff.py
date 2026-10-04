#!/usr/bin/env python3
"""Influence-union diff: the review record for every model change (HREQ-V-19).

Computes the current influence union (computeInfluenceAll: every registered scenario at its
own horizon, +10 % and -10 %, HREQ-U-12) and prints, per quantity, the feeders added and
removed against a baseline union, then the same per scenario. Exit 0 when no feeder list
changed (order is ignored: a re-ranking is not a new claim), 1 on any added or removed
feeder, 2 on a usage error or a baseline without an influenceAll section.

Baseline: the influenceAll section of the golden fixture -- by default the committed
tests/fixtures/health/golden_v1.json, or that file as it is at a git revision (--base REF,
e.g. origin/main), or any fixture path (--golden PATH). Run it BEFORE regenerating the fixture
(against the committed one) or after (against --base origin/main); either way the output goes
into the pull request: a parameter that newly feeds a displayed quantity is a claim the rigor
review must accept (docs/health/03 section 7.1).

    python3 tools/health_influence_diff.py                       # Python port, every scenario
    python3 tools/health_influence_diff.py --node                # JavaScript reference (faster)
    python3 tools/health_influence_diff.py --base origin/main    # against main's fixture
    python3 tools/health_influence_diff.py --scenarios drink_water_1L   # one scenario's lists

Cost: 805 simulations. The pure-Python port takes minutes (the 30-day scenario dominates);
--node runs the reference in about 25 s. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "health" / "golden_v1.json"
GOLDEN_REL = "tests/fixtures/health/golden_v1.json"
ENGINE_JS = ROOT / "reference" / "metabolic-map-v1" / "engine" / "index.js"
NODE_CANDIDATES = ("/opt/node22/bin/node", "node")

#: Printed as the last two lines (HREQ-S-01); byte-equal to health.engine's constants.
DISCLAIMER = "Educational model — not medical advice."
VALIDATION_STATUS = "Not clinically validated."


def _decode(obj: dict[str, Any]) -> Any:
    """The fixture writes NaN/±Infinity as {"$float": "..."}."""
    if len(obj) == 1 and "$float" in obj:
        return float(obj["$float"])
    return obj


def load_baseline(golden: str | None, base: str | None) -> dict[str, Any]:
    """The whole baseline fixture (dict). Raises SystemExit(2) when it cannot be read."""
    if base:
        r = subprocess.run(["git", "-C", str(ROOT), "show", f"{base}:{GOLDEN_REL}"],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise SystemExit(f"health_influence_diff: cannot read {GOLDEN_REL} at {base}: "
                             f"{r.stderr.strip()}")
        text = r.stdout
    else:
        path = Path(golden) if golden else GOLDEN
        if not path.is_file():
            raise SystemExit(f"health_influence_diff: no fixture at {path}")
        text = path.read_text(encoding="utf-8")
    return json.loads(text, object_hook=_decode)


def compute_python(scenarios: Sequence[str] | None) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    from health.engine import compute_influence_all
    return compute_influence_all(scenarios=scenarios)


def compute_node(scenarios: Sequence[str] | None, node: str) -> dict[str, Any]:
    script = (
        "const E = await import(process.argv[1]);"
        "const ids = process.argv[2] ? process.argv[2].split(',') : undefined;"
        "const r = E.computeInfluenceAll({ scenarios: ids });"
        "const rep = (_k, v) => (typeof v === 'number' && !Number.isFinite(v)"
        " ? { $float: String(v) } : v);"
        "process.stdout.write(JSON.stringify({ meta: r.meta, feeders: r.feeders, union: r.union,"
        " runs: Object.fromEntries(Object.entries(r.runs).map(([s, d]) => [s,"
        " Object.fromEntries(Object.entries(d).map(([k, x]) => [k, { params: x.params }]))])) }, rep));")
    r = subprocess.run([node, "--input-type=module", "-e", script, ENGINE_JS.as_uri(),
                        ",".join(scenarios or [])], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise SystemExit(f"health_influence_diff: the reference failed: {r.stderr.strip()[-600:]}")
    return json.loads(r.stdout, object_hook=_decode)


def union_of(feeders: Mapping[str, Mapping[str, Sequence[str]]],
             scenarios: Sequence[str]) -> dict[str, set[str]]:
    """OR of the per-scenario feeder lists of `scenarios` (influence_union's rule)."""
    out: dict[str, set[str]] = {}
    for sid in scenarios:
        for key, names in feeders[sid].items():
            out.setdefault(key, set()).update(names)
    return out


def diff_lists(old: Mapping[str, set[str]], new: Mapping[str, set[str]]
               ) -> dict[str, tuple[list[str], list[str]]]:
    """{key: (added, removed)} for every key whose feeder SET changed (a key present on one
    side only counts all its feeders as added or removed)."""
    out: dict[str, tuple[list[str], list[str]]] = {}
    for key in list(old) + [k for k in new if k not in old]:
        a, b = set(old.get(key, ())), set(new.get(key, ()))
        if a != b:
            out[key] = (sorted(b - a), sorted(a - b))
    return out


def report(baseline: Mapping[str, Any], current: Mapping[str, Any], scenarios: Sequence[str],
           write: Callable[[str], None]) -> int:
    """Print the review record; return the number of changed (scope, quantity) lists."""
    old_all = baseline["influenceAll"]
    old_union = union_of(old_all["feeders"], scenarios)
    new_union = union_of(current["feeders"], scenarios)
    changes = 0
    d = diff_lists(old_union, new_union)
    scope = "union" if list(scenarios) == list(old_all["meta"]["scenarios"]) else \
        f"union over {', '.join(scenarios)}"
    write(f"== {scope}: {len(d)} of {len(new_union)} quantities changed")
    for key, (added, removed) in d.items():
        write(f"  {key}: " + "; ".join(x for x in (
            "added " + ", ".join(added) if added else "",
            "removed " + ", ".join(removed) if removed else "") if x))
    changes += len(d)
    for sid in scenarios:
        ds = diff_lists({k: set(v) for k, v in old_all["feeders"][sid].items()},
                        {k: set(v) for k, v in current["feeders"][sid].items()})
        if not ds:
            continue
        write(f"== {sid}: {len(ds)} quantities changed")
        for key, (added, removed) in ds.items():
            write(f"  {key}: " + "; ".join(x for x in (
                "added " + ", ".join(added) if added else "",
                "removed " + ", ".join(removed) if removed else "") if x))
        changes += len(ds)
    return changes


def run(argv: Sequence[str] | None = None, *, current: Mapping[str, Any] | None = None,
        write: Callable[[str], None] = print) -> int:
    """The tool; `current` lets a test pass a computed result instead of recomputing."""
    ap = argparse.ArgumentParser(prog="health_influence_diff", description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--golden", help="baseline fixture path (default: the committed fixture)")
    src.add_argument("--base", help="baseline: the committed fixture at this git revision")
    ap.add_argument("--scenarios", help="comma-separated scenario ids (default: every scenario "
                    "in the baseline)")
    ap.add_argument("--node", nargs="?", const="", default=None,
                    help="compute with the JavaScript reference (optional path to node)")
    a = ap.parse_args(argv)
    try:
        baseline = load_baseline(a.golden, a.base)
    except SystemExit as e:
        write(str(e))
        return 2
    if "influenceAll" not in baseline:
        write("health_influence_diff: the baseline fixture has no influenceAll section (written "
              "since model 1.1.0); nothing to diff against")
        return 2
    known = list(baseline["influenceAll"]["meta"]["scenarios"])
    ids = [s for s in a.scenarios.split(",") if s] if a.scenarios else known
    unknown = [s for s in ids if s not in known]
    if unknown:
        write(f"health_influence_diff: not in the baseline's union: {', '.join(unknown)}")
        return 2
    t0 = time.perf_counter()
    if current is None:
        if a.node is not None:
            node = a.node or next((c for c in NODE_CANDIDATES
                                   if (os.path.isfile(c) and os.access(c, os.X_OK))
                                   or shutil.which(c)), None)
            if not node:
                write("health_influence_diff: --node given but no Node binary found")
                return 2
            current = compute_node(None if ids == known else ids, shutil.which(node) or node)
            engine = f"JavaScript reference ({node})"
        else:
            current = compute_python(None if ids == known else ids)
            engine = "Python port"
    else:
        engine = "supplied result"
    missing = [s for s in ids if s not in current["feeders"]]
    if missing:
        write(f"health_influence_diff: the current result lacks scenarios: {', '.join(missing)}")
        return 2
    meta = current["meta"]
    write(f"influence diff (HREQ-V-19): baseline model {baseline.get('modelVersion')} "
          f"({a.base + ':' + GOLDEN_REL if a.base else a.golden or GOLDEN_REL}) -> current "
          f"model {meta.get('modelVersion')} ({engine}, {meta.get('runs')} runs, "
          f"{time.perf_counter() - t0:.1f} s); scenarios: {', '.join(ids)}; "
          f"+/-{meta.get('rel')}, threshold {meta.get('threshold')}")
    changes = report(baseline, current, ids, write)
    write("no feeder list changed" if not changes else
          f"{changes} feeder list(s) changed: each added feeder of a displayed quantity is a "
          "claim the rigor review must accept (docs/health/03 section 7.1)")
    write(DISCLAIMER)
    write(VALIDATION_STATUS)
    return 1 if changes else 0


if __name__ == "__main__":
    sys.exit(run())
