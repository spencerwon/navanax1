#!/bin/bash
# Navanax DASHBOARD (manual run) — NOT the viewer. To VIEW the dashboard,
# double-click open-dashboard.command or "Navanax Dashboard.webloc".
#
# This starts a dashboard PROCESS by hand, for a machine WITHOUT the
# background job. If the background job is running it refuses (exit 2) before
# touching the store -- two dashboards on one store is how the store got
# corrupted on 2026-09-10 (BUG-20260910-067). Reads what the recorder has
# written (never the landing zone), folds new frames every few seconds, serves
# http://127.0.0.1:8765 to THIS computer only (REQ-N-13). Ctrl + C stops it.

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
