#!/bin/bash
# Navanax OPEN DASHBOARD — the button. Double-click to VIEW the dashboard.
#
# This only opens the page in your browser. It NEVER starts a dashboard:
# on 2026-09-10 a second dashboard started by hand beside the background one
# corrupted the analytical store (BUG-20260910-067). The background job
# (autostart-install.command) is the one that runs; this file just looks.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
URL="http://127.0.0.1:8765/"
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
done
LISTENING=no
if [ -n "$PY" ]; then
  "$PY" - <<'PYEOF' && LISTENING=yes
import socket, sys
s = socket.socket(); s.settimeout(1.5)
try:
    s.connect(("127.0.0.1", 8765)); sys.exit(0)
except OSError:
    sys.exit(1)
finally:
    s.close()
PYEOF
fi
if [ "$LISTENING" = yes ]; then
  echo "Dashboard is running. Opening $URL"
  command -v open >/dev/null 2>&1 && open "$URL"
  exit 0
fi
echo "============================================================"
echo "  The dashboard is NOT running (nothing answers on port 8765)."
echo "============================================================"
if command -v launchctl >/dev/null 2>&1; then
  if launchctl print "gui/$(id -u)/com.navanax.dashboard" >/dev/null 2>&1; then
    echo "  The background job com.navanax.dashboard IS loaded; it may be"
    echo "  starting, restarting, or rebuilding the store. Last log lines:"
  else
    echo "  The background job com.navanax.dashboard is NOT loaded."
    echo "  Double-click autostart-install.command to start it."
  fi
fi
[ -f data/logs/dashboard.log ] && tail -5 data/logs/dashboard.log | sed 's/^/    /'
echo
echo "  This file never starts a dashboard itself (BUG-067). Wait a moment"
echo "  and double-click it again, or run autostart-status.command."
read -r -p "Press Enter to close. "
