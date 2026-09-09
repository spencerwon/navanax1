#!/bin/bash
# Navanax DASHBOARD — double-click to open the live view in your browser.
#
# Reads what the recorder has written (it never touches the landing zone),
# folds new frames into a queryable store every few seconds, and serves a page
# at http://127.0.0.1:8765 -- reachable from THIS computer only (REQ-N-13).
# Safe to run alongside start.command. Stop with Ctrl + C or close this window.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "No Python 3.10+ found."; read -r -p "Enter to close. "; exit 1; }
if ! "$PY" -c "import zstandard, yaml" 2>/dev/null; then
  echo "Dependencies missing -- run start.command once first (it installs them)."
  read -r -p "Enter to close. "; exit 1
fi
echo "============================================================"
echo "  NAVANAX -- dashboard"
echo "  This window must stay open while you use the page."
echo "============================================================"
"$PY" -m navanax.cli dashboard
echo; read -r -p "Dashboard stopped. Press Enter to close. "
