#!/bin/bash
# Navanax KEEP AWAKE — REMOVE. Double-click to give your Mac its normal sleep
# behaviour back. Takes effect immediately; no restart, no logout.
#
# This removes ONLY the keep-awake job. The recorder, dashboard and trait jobs
# are untouched and keep running. No data is deleted.

set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
AGENTS="$HOME/Library/LaunchAgents"
L="com.navanax.keepawake"

echo "============================================================"
echo "  NAVANAX -- remove the optional keep-awake"
echo "============================================================"
echo

OS="$(uname -s 2>/dev/null || echo unknown)"
if [ "$OS" != "Darwin" ]; then
  echo "[REFUSED] macOS only. This machine: $OS. Nothing was changed."
  echo; read -r -p "Press Enter to close. "; exit 1
fi

DOMAIN="gui/$(id -u)"
WAS_LOADED=no
launchctl print "$DOMAIN/$L" >/dev/null 2>&1 && WAS_LOADED=yes
launchctl bootout "$DOMAIN/$L" >/dev/null 2>&1
launchctl unload -w "$AGENTS/$L.plist" >/dev/null 2>&1
[ "$WAS_LOADED" = yes ] && echo "  stopped  $L" || echo "  not running  $L"

if [ -f "$AGENTS/$L.plist" ]; then
  rm -f "$AGENTS/$L.plist" && echo "  deleted  $AGENTS/$L.plist"
else
  echo "  no file  $AGENTS/$L.plist (already gone)"
fi

cat <<'EOF'

------------------------------------------------------------
  Done. Your Mac now sleeps exactly as macOS normally would.

  Recording will stop whenever it sleeps. The gap is recorded
  honestly on the next wake, but the events inside it are gone
  and cancellations in that window cannot be backfilled.

  The recorder, dashboard and trait jobs are untouched.
------------------------------------------------------------

EOF
read -r -p "Press Enter to close this window. "
