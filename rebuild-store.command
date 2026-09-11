#!/bin/bash
# Navanax REBUILD STORE — double-click when the dashboard says the analytical
# store is malformed.
#
# WHAT THIS DOES: moves data/analytics.sqlite aside under a dated name. That is
# all. It stops nothing, kills nothing, and deletes nothing.
#
# WHY THAT IS ENOUGH: the analytical store is DERIVED. Every row in it is a fold
# of the landing zone (data/landing), which is append-only and is not touched by
# this script or by anything the dashboard does. Move the store aside and the
# dashboard folds a fresh one from the same frames — you lose no history, and no
# number changes except that the wrong ones go away.
#
#   * If the dashboard is RUNNING, it notices within a minute and rebuilds by
#     itself. There is nothing to restart and no window to reopen.
#   * If it is NOT running, start it as usual (dashboard.command, or it comes
#     back at your next login) and it rebuilds on the way up.
#
# WHAT IT REFUSES TO DO: move the store while a live process holds the fold
# writer lock on it. That process has the file open and is writing to it; moving
# it out from under a writer is how you turn one corrupt store into two
# (BUG-20260910-067). It names the pid so you can quit that dashboard first.
#
# The old file is kept as analytics.sqlite.corrupt-<date>. Nothing here deletes
# it. When you are sure the rebuilt store is fine, drag it to the Trash yourself
# — deciding that is your call, not a script's.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
# Double-clicking passes no arguments, so this is the project this file sits in --
# which is what you want. An explicit path is accepted for the self-test, and for
# pointing at a second project directory.
PROJECT="${1:-$PWD}"
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "No Python 3.10+ found."; read -r -p "Enter to close. "; exit 1; }

echo "============================================================"
echo "  NAVANAX — REBUILD THE ANALYTICAL STORE"
echo "============================================================"
echo

PYTHONPATH="$PWD/src" "$PY" - "$PROJECT" "$PWD/src" <<'PYEOF'
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
import yaml  # noqa: E402

from navanax.normalize import store_writer_info, writer_lock_held  # noqa: E402

cfg = yaml.safe_load((root / "config" / "base.yaml").read_text())
db = root / cfg["analytical"]["path"]

if not db.exists():
    print(f"There is no store at {db}.")
    print("Nothing to move aside — the dashboard will fold a fresh one when it starts.")
    raise SystemExit(0)

# The fold-writer lock. A live holder means a process has this file open and is
# writing to it; moving it now is how one corrupt store becomes two.
#
# ASK THE LOCK, not the lock file. "Is the pid in the file still alive?" gives
# false refusals in both directions -- a process that closed its store is still
# named in the file, and a pid can be reused -- and a false refusal here means
# the Operator cannot recover a corrupt store at all.
info = store_writer_info(db)
held = writer_lock_held(db)
if held or (held is None and info.get("alive") and info.get("pid")):
    who = f"process {info['pid']}" if info.get("pid") else "another process"
    print(f"REFUSING: {who} holds the fold-writer lock on this store")
    print(f"          (since {info.get('since')}, lock file {info.get('lock')}).")
    if held is None:
        print("          (This volume does not support file locking, so that is read from")
        print("           the lock file rather than from the lock itself.)")
    print()
    print("          That is a running dashboard, and it has the file open. Moving it")
    print("          out from under a live writer is how one corrupt store becomes two.")
    print()
    if info.get("pid"):
        print(f"          Quit that dashboard first — close its window, or run:  kill {info['pid']}")
    else:
        print("          Quit the dashboard that owns it first.")
    print("          — then double-click this file again. Nothing has been changed.")
    raise SystemExit(3)

stamp = datetime.now(timezone.utc).date().isoformat()
dest = db.with_name(db.name + f".corrupt-{stamp}")
n = 2
while dest.exists():                      # never overwrite an earlier one
    dest = db.with_name(db.name + f".corrupt-{stamp}-{n}")
    n += 1

size = sum(Path(str(db) + s).stat().st_size for s in ("", "-wal", "-shm")
           if Path(str(db) + s).exists())
moved = []
for suffix in ("", "-wal", "-shm"):
    src = Path(str(db) + suffix)
    if src.exists():
        target = Path(str(dest) + suffix)
        src.rename(target)                # a rename, never a delete
        moved.append(target.name)

print(f"Moved aside ({size / 1e6:.1f} MB):")
for m in moved:
    print(f"    {m}")
print()
print("The landing zone was NOT touched. Not one byte of it, by this script or by")
print("the dashboard — every frame that was ever recorded is still there, and the")
print("rebuild re-folds all of them.")
print()
print("WHAT HAPPENS NOW")
print("  * If the dashboard is running, it picks this up within a minute and")
print("    rebuilds by itself. Reload http://127.0.0.1:8765/ in a minute.")
print("  * If it is not running, start it (dashboard.command) and it rebuilds")
print("    on the way up.")
print()
print("A big store takes a few minutes to re-fold. The Health tab shows the fold")
print("running, and the number of events climbing back.")
print()
print(f"The old file is kept as {dest.name}. Nothing deletes it — when you are")
print("sure the rebuilt store is good, drag it to the Trash yourself.")
PYEOF

rc=$?
echo
if [ $rc -eq 0 ]; then
  echo "============================================================"
  echo "  DONE"
  echo "============================================================"
else
  echo "============================================================"
  echo "  NOTHING WAS CHANGED"
  echo "============================================================"
fi
echo
read -r -p "Press Enter to close. "
exit $rc
