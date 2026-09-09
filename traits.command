#!/bin/bash
# Navanax TRAITS — double-click to load every Argonaut's traits into the store.
#
# Two passes: (1) the collection's token list from OpenSea, ~47 governed reads
# (about 40% of one hour's budget) -- (2) each token's metadata from where it
# is hosted, NOT metered by OpenSea. Takes 10-20 minutes for 9,212 tokens.
# Stop any time with Ctrl + C; run again to resume where it stopped.
# The dashboard's trait filters light up as the data arrives.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "No Python 3.10+ found."; read -r -p "Enter to close. "; exit 1; }
echo "============================================================"
echo "  NAVANAX -- traits onboarding"
echo "============================================================"
"$PY" -m navanax.cli traits
echo; read -r -p "Done. Press Enter to close. "
