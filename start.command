#!/bin/bash
# Navanax START — double-click this file to begin recording the market.
#
# Everything below is readable. Nothing is hidden. It does three things:
#   1. installs the project's dependencies into your Python (first run only takes
#      a minute; later runs are seconds)
#   2. proves the compressor can read back what it writes -- and REFUSES to start
#      if it cannot, because the data this records cannot be re-fetched
#   3. starts the stream consumer and keeps this window open while it runs
#
# To STOP recording: click this window and press  Ctrl + C  (one time). It shuts
# down cleanly, writes its last frame, and the next start records the downtime
# as a gap -- that is BUG-20260909-009 working as intended, not an error.
#
# The window stays open after exit so you can read what happened.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1

echo "============================================================"
echo "  NAVANAX -- start recording"
echo "  folder: $(pwd)"
echo "============================================================"
echo

# ---------------------------------------------------------------- 1. python
PY=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$candidate"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "[FAIL] No Python 3.10+ found. Install from https://www.python.org/downloads/"
  echo; read -r -p "Press Enter to close. "; exit 1
fi
echo "[1/3] Using $($PY -V) at $(command -v "$PY")"

if [ ! -f .env ]; then
  echo
  echo "[FAIL] No .env file here. Copy .env.example to .env and put your OpenSea"
  echo "       API key in it (TextEdit: Format -> Make Plain Text before saving)."
  echo; read -r -p "Press Enter to close. "; exit 1
fi

# ---------------------------------------------------------------- 2. deps
echo "[2/3] Installing dependencies (websockets, zstandard, PyYAML...)."
echo "      First time: about a minute. After that: seconds."
"$PY" -m pip install -q -e ".[dev]" 2>&1 | grep -v "already satisfied" | tail -5
if ! "$PY" -c "import zstandard, websockets, yaml" 2>/dev/null; then
  echo
  echo "[FAIL] Dependencies did not install. Scroll up for the reason."
  echo "       Most common: no internet, or pip pointing at a different Python."
  echo; read -r -p "Press Enter to close. "; exit 1
fi
echo "      Installed. zstandard $("$PY" -c 'import zstandard;print(zstandard.__version__)')"
echo

# ---------------------------------------------------------------- 3. run
echo "[3/3] Starting the stream consumer."
echo "      The first thing it does is prove the compressor round-trips on THIS"
echo "      machine. If that fails it refuses to start -- read the message."
echo
echo "      Healthy looks like:   subscribed: collection:argonauts"
echo "      Expired key looks like: NOT SUBSCRIBED TO ANYTHING  (free keys last 7 days)"
echo
echo "      Stop with Ctrl + C.  Leave this window open while recording."
echo "------------------------------------------------------------"
echo
"$PY" -m navanax.cli ingest
STATUS=$?
echo
echo "------------------------------------------------------------"
case $STATUS in
  0) echo "Stopped cleanly. Last frame written. Downtime from now until the next";
     echo "start will be recorded as a gap." ;;
  2) echo "Exited: configuration problem (key or watchlist). See above." ;;
  3) echo "REFUSED TO START: the compressor failed its round-trip check. See above." ;;
  4) echo "REFUSED TO START: another copy is already recording to this folder." ;;
  *) echo "Exited with code $STATUS. Scroll up for the reason." ;;
esac
echo
read -r -p "Press Enter to close this window. "
