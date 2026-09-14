#!/bin/bash
# Navanax PROBE — how many events does OpenSea really return for limit=200?
#
# WHAT IT COSTS: exactly ONE REST read, out of the 120 you get per hour. Not
# two, not four -- retries are switched off, so even a failure costs one. If the
# budget governor says fewer than 5 reads are available right now, this refuses
# and spends nothing.
#
# WHY IT EXISTS: two of our own documents say the events endpoint returns "up to
# 200 per page", and neither of them checked. Every estimate of how much history
# a REST read buys is scaled off that number. One read settles it.
#
# WHAT IT WRITES: one dated entry in docs/measurements/, and the usual one-line
# spend record in data/ops.db. It never writes to data/landing/ (the raw record)
# and never opens data/analytics.sqlite.
#
# SAFE TO RUN while the recorder is going. It shares the same REST budget, so
# don't run it in the same minute as traits.command.

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

echo "============================================================"
echo "  NAVANAX -- PR-0.1 probe: the events endpoint's page size"
echo "============================================================"
echo
echo "  This spends ONE OpenSea REST read and writes one line of markdown."
echo "  It does not touch the recorded market data."
echo
read -r -p "  Press Enter to spend it, or close this window to cancel. "
echo
PYTHONPATH=src "$PY" tools/probe_events_page.py
STATUS=$?
echo
echo "------------------------------------------------------------"
case $STATUS in
  0) echo "Measured. The answer is in docs/measurements/2026-09-11_events_page_size.md" ;;
  1) echo "The request came back non-200. The read was spent, the reason is above, and";
     echo "the entry was still written -- a failed measurement is also a measurement." ;;
  2) echo "Configuration: no usable OPENSEA_API_KEY in .env. Nothing was spent." ;;
  5) echo "The request failed before it produced an answer. See the reason above." ;;
  6) echo "REFUSED: too little REST budget left. Nothing was spent. Try again in an hour." ;;
  *) echo "Exited with code $STATUS. Scroll up for the reason." ;;
esac
echo
read -r -p "Press Enter to close this window. "
