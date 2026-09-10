"""One command, every gate. `python3 tools/gates.py [--corpus LANDING DB]`

Runs, in order, and stops at the first failure with a non-zero exit:
  1. tests/selftest.py            (discovery-based; count printed)
  2. ruff check src tests tools
  3. tools/buglog.py --check       (every fixed bug names a test that exists)
  4. tools/secrets_check.py
  5. optional: a real-corpus fold  (--corpus <landing root> <analytics.sqlite>)
     re-folds the analytical store from the landing zone into a SCRATCH COPY
     and prints the before/after counts the tech-lead requires in every PR
     description (docs/proposals/TECHLEAD_2026-09-09_factcheck.md, R1).
     The landing zone is read only; the operator's store is never touched.

Agents run this before claiming green. The orchestrator runs it on the
operator's machine with --corpus before any PR is opened.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(label: str, cmd: list[str], *, optional: bool = False) -> str:
    print(f"\n=== {label}: {' '.join(cmd)}")
    t0 = time.time()
    if shutil.which(cmd[0]) is None:
        if optional:
            print(f"--- {label}: SKIPPED ({cmd[0]} not installed here; CI runs it)")
            return ""
        raise SystemExit(f"gate failed: {label} ({cmd[0]} not installed)")
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = (p.stdout + p.stderr)
    tail = "\n".join(out.strip().splitlines()[-6:])
    print(tail)
    print(f"--- {label}: {'OK' if p.returncode == 0 else 'FAILED'} in {time.time() - t0:.1f}s")
    if p.returncode != 0:
        raise SystemExit(f"gate failed: {label}")
    return out


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
        out = run("selftest", [py, "tests/selftest.py"])
        line = next((ln for ln in out.splitlines() if "test functions" in ln), "")
        print(f"    {line.strip()}")
    run("ruff", ["ruff", "check", "src", "tests", "tools"], optional=True)   # lint; CI is the hard gate
    run("bug ledger", [py, "tools/buglog.py", "--check"])
    run("secrets", [py, "tools/secrets_check.py"])
    if a.corpus:
        corpus_fold(Path(a.corpus[0]), Path(a.corpus[1]))
    print("\nALL GATES GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
