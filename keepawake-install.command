#!/bin/bash
# Navanax KEEP AWAKE (optional) — double-click to stop your Mac going to sleep
# on its own WHILE IT IS PLUGGED IN, so recording is not interrupted.
#
# READ THIS BEFORE YOU RUN IT. It changes your Mac's behaviour, not just this
# project's, so it is a separate switch from autostart-install.command.
#
# WHAT IT DOES
#   Runs Apple's own /usr/bin/caffeinate with two flags:
#       -i   prevent the system going idle-to-sleep
#       -s   ...but ONLY while the Mac is on AC power
#   No project code runs here. caffeinate is a small Apple utility that has
#   shipped with macOS for years; all it does is hold a "please stay awake"
#   assertion, the same one a video player holds while a film is playing.
#
# THE TRADE-OFF, honestly
#   ON POWER    the Mac stays awake indefinitely and keeps recording. The
#               display can still sleep; the machine underneath does not.
#               A laptop that never sleeps runs warmer and its fan may run.
#   ON BATTERY  because of -s, this does nothing at all. The Mac sleeps
#               normally and recording stops. That is deliberate: without -s
#               you could flatten the battery overnight and lose more than you
#               gained.
#   CLOSING THE LID  still sleeps the Mac, on power or not. macOS does not let
#               any program override the lid, and neither this nor launchd nor
#               anything else in this project can change that. If you want
#               recording overnight, leave the lid OPEN and the charger in.
#
# It is fully reversible: keepawake-uninstall.command removes it and your Mac
# goes straight back to its normal sleep behaviour, no restart needed.

set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
PROJECT="$(pwd)"
AGENTS="$HOME/Library/LaunchAgents"
L="com.navanax.keepawake"

echo "============================================================"
echo "  NAVANAX -- optional keep-awake"
echo "============================================================"
echo

OS="$(uname -s 2>/dev/null || echo unknown)"
if [ "$OS" != "Darwin" ]; then
  echo "[REFUSED] macOS only (caffeinate and launchd are Apple components)."
  echo "          This machine: $OS. Nothing was changed."
  echo; read -r -p "Press Enter to close. "; exit 1
fi
if [ ! -x /usr/bin/caffeinate ]; then
  echo "[REFUSED] /usr/bin/caffeinate is not present on this Mac. Nothing was changed."
  echo; read -r -p "Press Enter to close. "; exit 1
fi

echo "  This will keep the Mac awake while it is PLUGGED IN."
echo "  On battery it does nothing. Closing the lid still sleeps it."
echo
read -r -p "  Type  yes  to install it, anything else to cancel: " ANSWER
if [ "$ANSWER" != "yes" ]; then
  echo
  echo "  Cancelled. Nothing was changed."
  echo; read -r -p "Press Enter to close. "; exit 0
fi

PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$(command -v "$c")"; break; }
done
if [ -z "$PY" ]; then
  echo; echo "[REFUSED] No Python 3.10+ found (needed only to write the job file)."
  echo; read -r -p "Press Enter to close. "; exit 1
fi

mkdir -p "$AGENTS" data/logs || { echo "[FAILED] could not create $AGENTS"; read -r -p "Enter to close. "; exit 1; }
"$PY" tools/launchd.py render "$L" --root "$PROJECT" --python "$PY" --out "$AGENTS/$L.plist" >/dev/null \
  || { echo "[FAILED] could not write $AGENTS/$L.plist"; read -r -p "Enter to close. "; exit 1; }
echo
echo "  wrote  $AGENTS/$L.plist"

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$L" >/dev/null 2>&1
launchctl unload -w "$AGENTS/$L.plist" >/dev/null 2>&1
if launchctl bootstrap "$DOMAIN" "$AGENTS/$L.plist" >/dev/null 2>&1 \
   || launchctl load -w "$AGENTS/$L.plist" >/dev/null 2>&1; then
  echo "  loaded $L"
else
  echo "  PROBLEM: macOS refused to load it:"
  launchctl bootstrap "$DOMAIN" "$AGENTS/$L.plist" 2>&1 | sed 's/^/      /'
fi

sleep 2
echo
launchctl print "$DOMAIN/$L" 2>/dev/null | sed -n 's/.*state = \(.*\)/  state: \1/p' | head -1
echo
echo "------------------------------------------------------------"
echo "  Installed. While the charger is in, the Mac will not fall"
echo "  asleep on its own. On battery, nothing changes. The lid"
echo "  still sleeps it either way."
echo
echo "  Undo at any time: keepawake-uninstall.command"
echo "  Check it:         autostart-status.command"
echo "------------------------------------------------------------"
echo
read -r -p "Press Enter to close this window. "
