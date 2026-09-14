#!/bin/bash
# Navanax PROBE — can one API key hold two stream connections at once?
#
# WHAT IT COSTS: zero REST reads. The stream is unmetered. The only thing it
# spends is ten minutes of your time and two extra connections on your key.
#
# WHY IT EXISTS: the plan for a redundant second stream (PR-10) rests entirely
# on the assumption that OpenSea permits two simultaneous connections per key.
# Nobody has checked. Ten minutes settles it, and the same ten minutes also
# measures the thing that actually justifies a second stream -- how many events
# ONE connection quietly drops.
#
# LEAVE THE RECORDER RUNNING. Do not stop it to make room for this. These are
# two EXTRA connections, so your key will be carrying at least three at once.
# If the recorder's own log shows a disconnect during the next ten minutes,
# that is not bad luck -- that is the answer, and it means the account will not
# hold what PR-10 wants. This probe reads that log afterwards and tells you.
#
# WHAT IT WRITES: one dated entry in docs/measurements/, plus a scratch file of
# raw frames under data/probes/ for later inspection. It does NOT write to
# data/landing/ (the irreplaceable record), does not open data/ops.db, and does
# not open data/analytics.sqlite. Nothing it captures is ever folded into the
# store -- the scratch file has no checksums and no sequence numbers, so it is
# not evidence of anything except what this probe saw.
#
# Stop early with Ctrl + C. It still writes what it saw, as an entry marked
# PARTIAL with the seconds it actually ran -- a short run is evidence with a
# small n, and throwing it away was never the honest option.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "No Python 3.10+ found."; read -r -p "Enter to close. "; exit 1; }

if [ ! -f .env ]; then
  echo "[FAIL] No .env file here. Copy .env.example to .env and put your OpenSea key in it."
  read -r -p "Press Enter to close. "; exit 1
fi

SECONDS_ARG="${1:-600}"

echo "============================================================"
echo "  NAVANAX -- PR-0.2 probe: two stream connections, one key"
echo "============================================================"
echo
echo "  Runs for ${SECONDS_ARG} seconds. Spends ZERO REST reads."
echo "  Leave the recorder running -- that is part of the test."
echo "  (To run it for a different length: drag this file into Terminal and add"
echo "   a number of seconds after it, e.g.  probe-two-sockets.command 120)"
echo
read -r -p "  Press Enter to start, or close this window to cancel. "
echo
PYTHONPATH=src "$PY" tools/probe_two_sockets.py --seconds "$SECONDS_ARG"
STATUS=$?
echo
echo "------------------------------------------------------------"
case $STATUS in
  0) echo "Done. The answer is in docs/measurements/2026-09-11_two_sockets.md" ;;
  2) echo "Configuration: no usable OPENSEA_API_KEY in .env, or the websockets package";
     echo "is not installed here. Double-click setup.command, then try again." ;;
  130) echo "Stopped early with Ctrl+C. What it had seen up to that point WAS written to";
       echo "docs/measurements/2026-09-11_two_sockets.md, as an entry marked PARTIAL with";
       echo "the number of seconds it actually ran. Read it as a lower bound, not as a";
       echo "finished measurement." ;;
  *) echo "Exited with code $STATUS. Scroll up for the reason." ;;
esac
echo
read -r -p "Press Enter to close this window. "
