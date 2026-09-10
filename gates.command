#!/bin/bash
# Navanax GATES — double-click to run every check the tech-lead requires before
# a pull request, INCLUDING the real-corpus fold that only this Mac can run
# (it needs the zstd codec the recorder writes with).
#
# What it does: tests, lint (if installed), bug-ledger check, secrets check,
# then re-folds the analytical store FROM THE LANDING ZONE into a scratch copy
# in your temp folder and prints the counts. It READS the landing zone and
# your store; it never writes to either. The report is saved to
#   data/logs/gates-<date>.txt
# so it can be read back by the reviewer without you copying anything.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "No Python 3.10+ found."; read -r -p "Enter to close. "; exit 1; }
mkdir -p data/logs
OUT="data/logs/gates-$(date +%Y%m%d-%H%M%S).txt"
echo "============================================================"
echo "  NAVANAX -- gates + real-corpus fold"
echo "  report: $OUT"
echo "============================================================"
LANDING="$("$PY" -c 'import yaml;print(yaml.safe_load(open("config/base.yaml"))["landing"]["root"])' 2>/dev/null || echo data/landing)"
DB="$("$PY" -c 'import yaml;print(yaml.safe_load(open("config/base.yaml"))["analytical"]["path"])' 2>/dev/null || echo data/analytics.sqlite)"
# A review worktree (a second checkout beside the real project, e.g.
# navanax1-tester) has no data of its own: read the sibling project's.
if [ ! -d "$LANDING" ] && [ -d "../navanax1/$LANDING" ]; then
  echo "no landing zone here; reading the sibling project's: ../navanax1/$LANDING"
  LANDING="../navanax1/$LANDING"; DB="../navanax1/$DB"
fi
PYTHONPATH=src "$PY" tools/gates.py --corpus "$LANDING" "$DB" 2>&1 | tee "$OUT"
echo; echo "Report saved to $OUT"; read -r -p "Press Enter to close. "
