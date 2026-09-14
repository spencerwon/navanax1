#!/bin/bash
# Navanax AUTOSTART UNINSTALL — double-click to turn automatic recording off.
#
# It stops the three background jobs and deletes exactly the three files that
# autostart-install.command created in ~/Library/LaunchAgents/. Nothing else.
#
# IT DELETES NO DATA. The landing zone, the databases and the logs are all left
# exactly as they are. This only removes the automation.
#
# After this, recording happens only while start.command is open, the way it
# did before.

set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
AGENTS="$HOME/Library/LaunchAgents"
# Fallback only. The real list comes from tools/launchd.py below, plus whatever is
# actually loaded -- a hardcoded list here could not stop a job it had never heard
# of, and com.navanax.recorder-b is exactly that job.
LABELS="com.navanax.recorder com.navanax.dashboard com.navanax.traits"

echo "============================================================"
echo "  NAVANAX -- turn OFF automatic recording"
echo "============================================================"
echo

OS="$(uname -s 2>/dev/null || echo unknown)"
if [ "$OS" != "Darwin" ]; then
  echo "[REFUSED] macOS only (launchd is an Apple component). This machine: $OS"
  echo
  read -r -p "Press Enter to close. "; exit 1
fi

DOMAIN="gui/$(id -u)"

# Every label this generator knows, UNION everything of ours currently loaded.
# Two sources because either alone leaves something behind: the generator does not
# know a job from a future version, and `launchctl list` does not show a job whose
# plist is installed but never loaded.
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && { PY="$(command -v "$c")"; break; }
done
if [ -n "$PY" ]; then
  KNOWN="$("$PY" tools/launchd.py all-labels 2>/dev/null | tr '\n' ' ')"
  [ -n "${KNOWN// /}" ] && LABELS="$KNOWN"
fi
LOADED="$(launchctl list 2>/dev/null | awk '{print $3}' | grep '^com\.navanax\.' | tr '\n' ' ')"
for L in $LOADED; do
  case " $LABELS " in
    *" $L "*) ;;
    *) LABELS="$LABELS $L"; echo "  note: $L is loaded and is not one this version installs -- "
       echo "        stopping and removing it too, because this is the uninstaller";;
  esac
done

REMOVED=0
for L in $LABELS; do
  WAS_LOADED=no
  launchctl print "$DOMAIN/$L" >/dev/null 2>&1 && WAS_LOADED=yes
  # bootout sends SIGTERM first, so the recorder flushes its final frame and the
  # landing-zone manifest closes cleanly. It is a stop, not a kill.
  launchctl bootout "$DOMAIN/$L" >/dev/null 2>&1
  launchctl unload -w "$AGENTS/$L.plist" >/dev/null 2>&1
  if [ "$WAS_LOADED" = yes ]; then
    echo "  stopped  $L"
  else
    echo "  not running  $L"
  fi
  if [ -f "$AGENTS/$L.plist" ]; then
    rm -f "$AGENTS/$L.plist" && { echo "  deleted  $AGENTS/$L.plist"; REMOVED=$((REMOVED+1)); }
  else
    echo "  no file  $AGENTS/$L.plist (already gone)"
  fi
done

# The opt-in keep-awake job is a separate switch with its own uninstaller, so
# this one leaves it alone -- but say so, or it looks like it was missed.
if [ -f "$AGENTS/com.navanax.keepawake.plist" ]; then
  echo
  echo "  NOTE: the optional keep-awake job is still installed. It is separate;"
  echo "        remove it with keepawake-uninstall.command if you want it gone."
fi

cat <<EOF

------------------------------------------------------------
  Done. $REMOVED job file(s) removed.

  Recording is NOT happening right now. To record, double-click
  start.command and leave that window open, as before.

  Nothing was deleted from data/ -- the landing zone, the
  databases and the logs are untouched.

  The stop above was a clean stop: the recorder flushed its last
  frame. The time from now until you next start recording will be
  recorded as a gap on the next start. That is correct.
------------------------------------------------------------

EOF
read -r -p "Press Enter to close this window. "
