"""One command, every gate. `python3 tools/gates.py [--corpus LANDING DB]`

Runs, in order, and stops at the first failure with a non-zero exit:
  - tests/selftest.py --no-skips   (discovery-based; count printed. Strict: a test
                                    skipped for a missing dependency FAILS here,
                                    because this machine has the dependencies)
  - the health self-tests          (HEALTH_SUITES, below)
  - ruff check src tests tools
  - tools/buglog.py --check        (every fixed bug names a test that exists)
  - tools/secrets_check.py
  - the other health gates         (below)
  - optional: a real-corpus fold   (--corpus <landing root> <analytics.sqlite>)
     re-folds the analytical store from the landing zone into a SCRATCH COPY
     and prints the before/after counts the tech-lead requires in every PR
     description (docs/proposals/TECHLEAD_2026-09-09_factcheck.md, R1).
     The landing zone is read only; the operator's store is never touched.

The health gates, one line per module so its removal recipe (docs/health/07) deletes its
own line here with its step below:
  HEALTH_SUITES                   one row per module with a self-test, run --no-skips
  health module registry          python -m health.registry --check (process)
  health kb-check                 python -m health.cli kb-check (cli)
  health golden fixture           the golden fixture is what the JavaScript reference writes today (engine-v1)
`health.cli status` is not a gate: it is a surface, run by hand.

The last line is `ALL GATES GREEN` only when every gate ran and passed; a gate that could
not run here (its tool is not installed) turns it into `GATES GREEN, N SKIPPED: ...` --
a skipped gate is never a silent green.

Agents run this before claiming green. The orchestrator runs it on the
operator's machine with --corpus before any PR is opened.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_CANDIDATES = ("/opt/node22/bin/node",)   # then `node` on PATH

# Health subsystem suites (docs/health/03 §2): one row per module that has a self-test.
# A module's removal recipe deletes its row (docs/health/07).
HEALTH_SUITES: tuple[tuple[str, str], ...] = (
    ("health selftest", "tests/health_selftest.py"),                # engine-v1
    ("health kb selftest", "tests/health_kb_selftest.py"),          # kb-v1
    ("health errors selftest", "tests/health_errors_selftest.py"),  # errors
    ("health app selftest", "tests/health_app_selftest.py"),        # desktop-app
)

SKIPPED: list[str] = []   # gates that could not run here; the verdict line names them


def run(label: str, cmd: list[str], *, optional: bool = False,
        env_extra: dict[str, str] | None = None) -> str:
    """Run one gate from the repository root; SystemExit on failure. `env_extra` is added
    to this process's environment (a step never builds its own, so removing one leaves no
    import behind)."""
    print(f"\n=== {label}: {' '.join(cmd)}")
    t0 = time.time()
    if shutil.which(cmd[0]) is None:
        if optional:
            print(f"--- {label}: SKIPPED ({cmd[0]} not installed here; CI runs it)")
            SKIPPED.append(f"{label} ({cmd[0]} not installed)")
            return ""
        raise SystemExit(f"gate failed: {label} ({cmd[0]} not installed)")
    env = {**os.environ, **env_extra} if env_extra else None
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    out = (p.stdout + p.stderr)
    tail = "\n".join(out.strip().splitlines()[-6:])
    print(tail)
    print(f"--- {label}: {'OK' if p.returncode == 0 else 'FAILED'} in {time.time() - t0:.1f}s")
    if p.returncode != 0:
        raise SystemExit(f"gate failed: {label}")
    return out


def node_binary() -> str | None:
    """/opt/node22/bin/node, else `node` on PATH, else None."""
    for cand in (*NODE_CANDIDATES, shutil.which("node")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def health_golden_check() -> None:
    """engine-v1 (docs/health/03 Appendix B V-08, HREQ-X-05): the committed golden fixture
    is byte-for-byte what tools/health_golden.mjs writes from the JavaScript reference
    today. Without Node the gate is SKIPPED, loudly, and counted in the verdict."""
    node = node_binary()
    if node is None:
        print("\n=== health golden fixture: tools/health_golden.mjs --check")
        print("--- health golden fixture: SKIPPED (no Node binary): golden --check not run")
        SKIPPED.append("health golden fixture (no Node binary: golden --check not run)")
        return
    run("health golden fixture", [node, "tools/health_golden.mjs", "--check"])


def verdict() -> str:
    """`ALL GATES GREEN` only when nothing was skipped; otherwise the skips, by name."""
    if not SKIPPED:
        return "ALL GATES GREEN"
    return (f"GATES GREEN, {len(SKIPPED)} SKIPPED: " + "; ".join(SKIPPED)
            + " -- a skipped gate is not a passed gate")


def corpus_fold(landing: Path, db: Path) -> None:
    """Re-fold a COPY of the store from the landing zone and print the counts."""
    sys.path.insert(0, str(ROOT / "src"))
    import sqlite3
    import tempfile

    from navanax.normalize import Normalizer, refresh_order_lives
    scratch = Path(tempfile.gettempdir()) / (db.stem + ".gates-scratch.sqlite")   # local disk, never next to the live store
    scratch_lock = scratch.with_name(scratch.name + ".lock")   # the fold-writer lock, BUG-20260910-067
    for p in (scratch, scratch.with_suffix(".sqlite-wal"), scratch.with_suffix(".sqlite-shm"),
              scratch_lock):
        if p.exists():
            p.unlink()
    # The live store may be mid-write (WAL) and, read over a mount, can look
    # malformed. Read it immutable+read-only for the "before" counts only; if
    # even that fails, fold from the landing zone into a FRESH scratch store
    # and say the comparison is unavailable. Never copy a live WAL database.
    before: dict = {}
    if db.exists():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
            for t in ("events", "order_lives", "order_criteria", "tokens", "traits"):
                try:
                    before[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.OperationalError:
                    before[t] = "n/a"
                except sqlite3.DatabaseError as exc:
                    before[t] = f"unreadable ({exc})"
            c.close()
        except sqlite3.DatabaseError as exc:
            before = {"unreadable": f"{type(exc).__name__}: {exc} (live store mid-write; before-counts unavailable)"}
    print(f"\n=== corpus fold (scratch copy; landing read-only): before {before}")
    t0 = time.time()
    n = Normalizer(landing, scratch)
    n.reset_for_refold()
    stats = n.sync()
    end_ts = n.conn.execute("SELECT MAX(valid_ts) FROM events").fetchone()[0]
    refresh_order_lives(n.conn, now_ts=end_ts)
    q = lambda s: n.conn.execute(s).fetchone()[0]  # noqa: E731
    after = {t: q(f"SELECT COUNT(*) FROM {t}") for t in ("events", "order_lives", "order_criteria")}
    exits = n.conn.execute("SELECT exit_reason, COUNT(*) FROM order_lives GROUP BY 1 ORDER BY 2 DESC").fetchall()
    unparsed = q("SELECT COUNT(*) FROM unparsed")
    print(f"sync: {stats}")
    if stats.get("files_failed") or stats.get("files_short"):
        raise SystemExit(f"gate failed: corpus fold could not fully read {stats.get('files_failed', 0)} file(s) "
                         f"and found {stats.get('files_short', 0)} short: {stats.get('last_error')} "
                         "-- run this on a machine with the landing zone's codec installed (the Mac: gates.command)")
    print(f"after: {after}  unparsed: {unparsed}  exits(as of window end): {exits}")
    print(f"--- corpus fold: OK in {time.time() - t0:.1f}s")
    if isinstance(before.get("events"), int) and after["events"] != before["events"]:
        print(f"!!! event count changed on re-fold: {before['events']} -> {after['events']} -- a parser change or a bug; say which in the PR")
    n.close()
    scratch.unlink(missing_ok=True)
    scratch_lock.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", nargs=2, metavar=("LANDING", "DB"))
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    py = sys.executable
    if not a.skip_tests:
        # BUG-20260911-078: --no-skips. A test that needs PyYAML is SKIPPED (never
        # passed) on a machine that lacks it, so CI's pre-install step can be green.
        # This machine HAS the dependencies, so a skip here means a test stopped
        # running and the developer would otherwise never find out.
        out = run("selftest", [py, "tests/selftest.py", "--no-skips"])
        line = next((ln for ln in out.splitlines() if "test functions" in ln), "")
        print(f"    {line.strip()}")
        # Health subsystem (docs/health/03 §2): the engine port against the JavaScript
        # golden fixture, every knowledge-base rule against a planted violation, and the
        # error hierarchy's halt rules.
        for label, script in HEALTH_SUITES:
            out = run(label, [py, script, "--no-skips"])
            line = next((ln for ln in out.splitlines() if "test functions" in ln), "")
            print(f"    {line.strip()}")
    run("ruff", ["ruff", "check", "src", "tests", "tools"], optional=True)   # lint; CI is the hard gate
    run("bug ledger", [py, "tools/buglog.py", "--check"])
    run("secrets", [py, "tools/secrets_check.py"])
    # HREQ-X-01/X-02: the module registry against the repository (process). PYTHONPATH=src
    # so the health gates run on a checkout that has not been pip-installed, as CI's do.
    run("health module registry", [py, "-m", "health.registry", "--check"],
        env_extra={"PYTHONPATH": str(ROOT / "src")})
    # HREQ-D-03: the knowledge base conforms to its contract (cli).
    run("health kb-check", [py, "-m", "health.cli", "kb-check"],
        env_extra={"PYTHONPATH": str(ROOT / "src")})
    health_golden_check()
    if a.corpus:
        corpus_fold(Path(a.corpus[0]), Path(a.corpus[1]))
    print(f"\n{verdict()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
