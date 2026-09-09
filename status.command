#!/bin/bash
# Navanax STATUS — double-click to see how recording is going. Read-only.
#
# Shows: what is on the watchlist, open gaps, gaps awaiting backfill, and then
# runs the landing-zone integrity audit (every file re-read and re-counted).
# Costs no API budget. Safe to run while recording is happening.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "No Python 3.10+ found."; read -r -p "Enter to close. "; exit 1; }

echo "============================================================"
echo "  NAVANAX -- status"
echo "============================================================"
"$PY" -m navanax.cli status
echo
echo "------------------------------------------------------------"
echo "  Landing-zone integrity audit (re-reads every file)"
echo "------------------------------------------------------------"
"$PY" -m navanax.cli verify
echo
echo "  Files on disk:"
du -sh data/landing 2>/dev/null || echo "  (nothing recorded yet)"
echo
read -r -p "Press Enter to close this window. "
