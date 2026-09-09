#!/bin/bash
# Navanax AUTOSTART STATUS — double-click to see whether the background jobs are
# alive and what they last said. Read-only: it changes nothing, starts nothing,
# stops nothing, and costs no OpenSea API budget.
#
# Three things to look at, in order of importance:
#   1. "running (process N)" next to com.navanax.recorder
#   2. "last exit" -- 0 means it stopped cleanly, anything else is a fault code
#   3. the gap count at the bottom: gaps are the honest record of downtime

set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
AGENTS="$HOME/Library/LaunchAgents"
LABELS="com.navanax.recorder com.navanax.dashboard com.navanax.traits com.navanax.keepawake"

echo "============================================================"
echo "  NAVANAX -- background job status"
echo "  $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "============================================================"

OS="$(uname -s 2>/dev/null || echo unknown)"
if [ "$OS" != "Darwin" ]; then
  echo "[REFUSED] macOS only (launchd is an Apple component). This machine: $OS"
  echo; read -r -p "Press Enter to close. "; exit 1
fi

DOMAIN="gui/$(id -u)"

for L in $LABELS; do
  echo
  echo "------------------------------------------------------------"
  if [ ! -f "$AGENTS/$L.plist" ]; then
    case "$L" in
      com.navanax.keepawake) echo "  $L: not installed (optional -- keepawake-install.command)";;
      *) echo "  $L: NOT INSTALLED -- run autostart-install.command";;
    esac
    continue
  fi
  OUT="$(launchctl print "$DOMAIN/$L" 2>&1)"
  if ! printf '%s' "$OUT" | grep -q "state = "; then
    echo "  $L: installed but NOT LOADED into macOS."
    echo "     Run autostart-install.command again."
    continue
  fi
  STATE="$(printf '%s\n' "$OUT" | sed -n 's/.*state = \(.*\)/\1/p' | head -1)"
  PID="$(printf '%s\n' "$OUT" | sed -n 's/^[[:space:]]*pid = \([0-9]*\).*/\1/p' | head -1)"
  LAST="$(printf '%s\n' "$OUT" | sed -n 's/^[[:space:]]*last exit code = \(.*\)/\1/p' | head -1)"
  RUNS="$(printf '%s\n' "$OUT" | sed -n 's/^[[:space:]]*runs = \([0-9]*\).*/\1/p' | head -1)"
  echo "  $L"
  echo "     state       ${STATE:-unknown}${PID:+  (process $PID)}"
  echo "     last exit   ${LAST:-none recorded}"
  [ -n "$RUNS" ] && echo "     times run   $RUNS"
done

echo
echo "============================================================"
echo "  What each exit code means"
echo "     0  stopped cleanly (or has not stopped yet)"
echo "     2  configuration -- missing API key, or empty watchlist"
echo "     3  refused: the compressor failed its check on this machine"
echo "     4  refused: something else already holds the recording lock"
echo "        (usually a start.command window you left open)"
echo "     5  crashed during the run -- read the log below"
echo "============================================================"

for N in recorder dashboard traits keepawake; do
  F="data/logs/$N.log"
  echo
  echo "--- last 5 lines of $F ---"
  if [ -f "$F" ]; then
    tail -5 "$F" | sed 's/^/    /'
  else
    echo "    (no log yet -- that job has not run)"
  fi
done

# The fast health read. `status` queries only the small operational database:
# it does not re-read the landing zone, so it is instant and safe to run while
# recording is happening. (Note for the curious: --shallow is a flag on
# `verify`, not on `status`. The deep landing-zone audit lives in
# status.command; this file deliberately does not run it.)
echo
echo "============================================================"
echo "  Recording health (fast read -- no files re-read, no API used)"
echo "============================================================"
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
if [ -n "$PY" ]; then
  "$PY" -m navanax.cli status 2>&1 | sed 's/^/  /'
else
  echo "  (no Python 3.10+ found -- cannot read the gap count)"
fi
echo
echo "  'open gaps' above should be 0 while the recorder is running."
echo "  A non-zero count is not damage: it is the system telling you"
echo "  honestly which windows it was not watching."
echo
echo "  For the full landing-zone integrity audit, use status.command."
echo
read -r -p "Press Enter to close this window. "
