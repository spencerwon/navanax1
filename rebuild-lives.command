#!/bin/bash
# Navanax -- REBUILD THE ORDER-LIFETIME TABLE (deliberate, slow, safe to cancel).
#
# The dashboard no longer does this at startup (BUG-20260914-079): on a store
# this size it took the whole page down. Run it when you can leave the machine
# alone -- overnight is ideal. It reads `events` and rewrites `order_lives`.
# It NEVER reads or writes the landing zone. Cancelling it changes nothing:
# the rewrite is one transaction, so an interrupted run leaves every row as it
# was, and the dashboard keeps working on the older lifetime counts.
#
# While it runs the dashboard must be STOPPED -- only one writer at a time.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
UIDN=$(id -u)
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import zstandard,yaml' 2>/dev/null && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "No python with the dependencies found."; read -r -p "Enter to close. "; exit 1; }

echo "============================================================"
echo "  NAVANAX -- rebuild order lifetimes"
echo "============================================================"
echo "  This can take a long time on a large store and the dashboard"
echo "  will be stopped for the duration. Ctrl+C is safe at any point."
echo
read -r -p "  Type yes to go ahead: " GO
[ "$GO" != "yes" ] && { echo "  Nothing done."; read -r -p "Enter to close. "; exit 0; }

echo "  Stopping the dashboard (the recorder keeps running)..."
launchctl bootout "gui/$UIDN/com.navanax.dashboard" 2>/dev/null && echo "    unloaded." || echo "    was not loaded."
sleep 3

echo "  Rebuilding. Progress is printed by the fold itself."
START=$(date +%s)
NAVANAX_REFOLD_LIVES=1 "$PY" - <<'PYEOF'
import logging, time, sys
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "src"))
from navanax.normalize import Normalizer, lives_method_state
t0 = time.time()
n = Normalizer(Path.cwd())
st = lives_method_state(n.conn)
print(f"  lifetime rows: {st.get('rows')}, method versions {st.get('min')}..{st.get('max')}")
print(f"  finished in {time.time()-t0:,.0f}s")
PYEOF
echo "  elapsed: $(( $(date +%s) - START ))s"

echo "  Starting the dashboard again..."
PL="$HOME/Library/LaunchAgents/com.navanax.dashboard.plist"
[ -f "$PL" ] && { launchctl enable "gui/$UIDN/com.navanax.dashboard" 2>/dev/null; launchctl bootstrap "gui/$UIDN" "$PL" 2>/dev/null; echo "    started."; } || echo "    no plist; run autostart-install.command."
echo
read -r -p "Press Enter to close. "
