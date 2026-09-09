#!/bin/bash
# Navanax setup — double-click this file, or run:  bash ~/Documents/navanax1/setup.command
#
# Everything below is readable. Nothing is hidden. It does four things:
#   1. clears a stale git lock file (explained on screen before it happens)
#   2. installs one Python library
#   3. runs the preflight check against OpenSea using the key in .env
#   4. prints exactly what to do next
#
# It does NOT delete anything else, does NOT touch files outside this project,
# and does NOT send your API key anywhere except OpenSea.

# Resolve the project folder from this script's own location, so it works
# whether double-clicked, run by path, or run from anywhere.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
echo "======================================================================"
echo "  NAVANAX SETUP"
echo "  Working in: $(pwd)"
echo "======================================================================"
echo

# ---------------------------------------------------------------- 1. lock file
LOCK=".git/index.lock"
if [ -f "$LOCK" ]; then
  echo "[1/4] Stale git lock file found."
  echo "      A git command of mine crashed and left behind a marker file that"
  echo "      makes git think it's still running. Removing exactly this one file:"
  echo "        $(pwd)/$LOCK   ($(wc -c < "$LOCK" | tr -d ' ') bytes)"
  rm -f "$LOCK" && echo "      Removed. Git will work again." || echo "      Could not remove it — not fatal, git just stays stuck."
else
  echo "[1/4] No stale git lock. Nothing to remove."
fi
echo

# ---------------------------------------------------------------- 2. library
echo "[2/4] Installing the 'websockets' library (needed to hold the live feed)..."
if python3 -c "import websockets" 2>/dev/null; then
  echo "      Already installed."
else
  pip3 install --quiet --user websockets 2>&1 | tail -2
  python3 -c "import websockets" 2>/dev/null \
    && echo "      Installed." \
    || echo "      Install failed. Preflight will still run its API checks, just not the live-feed test."
fi
echo

# ---------------------------------------------------------------- 3. preflight
echo "[3/4] Running preflight against OpenSea."
echo "      Reads your key from .env (BUG-20260909-001 fixed - it genuinely does now)."
echo "      Costs 3 of your 600 hourly API requests."
echo "      Watches the live feed for 2 minutes, so this takes ~2.5 min total."
echo
python3 tools/preflight.py --slug argonauts --stream-seconds 120
STATUS=$?
echo

# ---------------------------------------------------------------- 4. next
echo "======================================================================"
if [ $STATUS -eq 0 ]; then
  echo "  PREFLIGHT PASSED."
  echo
  echo "  Copy everything printed above and paste it back into the chat."
  echo "  It contains no key material — safe to share."
  echo
  echo "  Then to actually start collecting data, run:"
  echo "      cd ~/Documents/navanax1 && python3 -m navanax.cli ingest"
  echo "  Leave that running. Ctrl-C stops it cleanly."
else
  echo "  PREFLIGHT FAILED (exit code $STATUS)."
  echo "  Copy everything above into the chat — the error text says what broke."
  echo "  Nothing was damaged; preflight only reads."
fi
echo "======================================================================"
echo
echo "Press return to close."
read -r _
