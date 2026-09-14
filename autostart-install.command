#!/bin/bash
# Navanax AUTOSTART INSTALL — double-click this once. After it finishes, the
# recorder starts by itself every time you log in, restarts itself if it
# crashes, and you never have to keep a Terminal window open again.
#
# WHAT THIS CHANGES ON YOUR MAC, exactly and completely:
#   - it creates three small text files in  ~/Library/LaunchAgents/
#   - it tells macOS's own job manager ("launchd") to read them
#   - it makes a folder  data/logs/  inside this project
# That is the entire footprint. No admin password. No system settings touched.
# No login items. Nothing installed outside your own user account. Undo it any
# time by double-clicking autostart-uninstall.command, which deletes exactly
# those three files.
#
# It does NOT change how recording works, what is recorded, or where data goes.

set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
PROJECT="$(pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOCK="data/landing/.ingest.lock"     # matches config/base.yaml: landing.root
LABELS="com.navanax.recorder com.navanax.dashboard com.navanax.traits"

echo "============================================================"
echo "  NAVANAX -- turn on automatic recording"
echo "  project: $PROJECT"
echo "============================================================"
echo

fail() { echo; echo "$@"; echo; read -r -p "Press Enter to close. "; exit 1; }

# ------------------------------------------------------------ 1. this is a Mac
# launchd is Apple's. There is no version of this script for anything else, and
# quietly doing nothing on the wrong machine is worse than saying so.
OS="$(uname -s 2>/dev/null || echo unknown)"
if [ "$OS" != "Darwin" ]; then
  fail "[REFUSED] This installer only works on macOS (it uses launchd, an Apple
          component). This machine reports: $OS
          Nothing was changed."
fi
echo "[1/6] macOS confirmed."

# ------------------------------------------------------------ 2. .env
# Installing a recorder that cannot authenticate would give you three jobs that
# restart every ten seconds forever and record nothing. Check first.
if [ ! -f .env ]; then
  fail "[REFUSED] There is no .env file in this folder, so the recorder has no
          OpenSea API key and could not record anything.
          Fix: copy .env.example to .env and put your key in it
          (TextEdit: Format -> Make Plain Text before saving).
          Then double-click this file again. Nothing was changed."
fi
echo "[2/6] .env found."

# ------------------------------------------------------------ 3. python
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -z "$PY" ] && fail "[REFUSED] No Python 3.10+ found. Run start.command once first.
          Nothing was changed."
if ! "$PY" -c "import zstandard, websockets, yaml" 2>/dev/null; then
  fail "[REFUSED] The project's libraries are not installed for $PY.
          Run start.command once first -- it installs them -- then come back.
          Nothing was changed."
fi
# PR-10: the redundant recorder (connection B) is installed ONLY when
# stream.redundant.enabled is true in config/base.yaml. tools/launchd.py reads
# that flag; if it cannot be read for any reason we keep the three-job list
# above, because a job for a connection that refuses to start would restart
# every ten seconds forever and record nothing.
LABELS_FROM_CONFIG="$("$PY" tools/launchd.py labels --root "$PROJECT" 2>/dev/null | tr '\n' ' ')"
[ -n "$LABELS_FROM_CONFIG" ] && LABELS="$LABELS_FROM_CONFIG"

# Turning the redundant flag back OFF leaves com.navanax.recorder-b installed and
# loaded. launchd keeps starting it, `ingest --redundant` refuses because the flag
# is off, KeepAlive restarts it ten seconds later, and the loop runs forever in a
# log nobody is reading. Nothing is damaged -- the refusal happens before anything
# is recorded -- but a job that cannot succeed should not be left running.
LOADED="$(launchctl list 2>/dev/null | awk '{print $3}' | grep '^com\.navanax\.' | tr '\n' ' ')"
STALE="$("$PY" tools/launchd.py stale --root "$PROJECT" $LOADED 2>/dev/null | tr '\n' ' ')"
UNKNOWN="$("$PY" tools/launchd.py stale --root "$PROJECT" --unknown $LOADED 2>/dev/null | tr '\n' ' ')"
if [ -n "${STALE// /}" ]; then
  echo "      Removing background jobs this configuration no longer wants:"
  for L in $STALE; do
    launchctl bootout "gui/$(id -u)/$L" >/dev/null 2>&1
    rm -f "$HOME/Library/LaunchAgents/$L.plist"
    echo "        removed $L (stopped, and its job file deleted)"
  done
fi
if [ -n "${UNKNOWN// /}" ]; then
  echo "      NOTE: these navanax jobs are loaded but this version does not know them:"
  for L in $UNKNOWN; do echo "        $L"; done
  echo "      They were LEFT ALONE -- they are more likely to belong to a newer"
  echo "      version of this project than to be rubbish. Nothing was stopped."
fi
echo "[3/6] Using $("$PY" -V) at $PY"
echo "      (the full path is written into the job files, so the jobs do not"
echo "       depend on your Terminal settings -- background jobs never read them)"

# ------------------------------------------------------ 4. stop a manual recorder
# THE ONE THING THAT COULD GO WRONG IF WE SKIPPED THIS:
# Two recorders writing to one landing zone silently lose manifest records --
# including gap records, which nothing can detect afterwards (that is why
# _single_instance exists). The project already guards against it: the second
# process REFUSES to start, because the first holds an exclusive lock on
# data/landing/.ingest.lock. So the worst case here is a job that refuses and
# retries, never a corrupted record. We still stop the manual one properly,
# because a clean stop flushes its final frame and an abrupt one does not.
echo
echo "[4/6] Checking for a recorder you started by hand..."
# If a PREVIOUS install of these jobs is loaded, unload it first. Otherwise its
# recorder holds the lock, and stopping it by pid is pointless -- launchd would
# just restart it ten seconds later, mid-install.
if launchctl print "gui/$(id -u)/com.navanax.recorder" >/dev/null 2>&1; then
  echo "      A previous background recorder is loaded. Stopping it first"
  echo "      (this is a reinstall, not a second copy)."
  launchctl bootout "gui/$(id -u)/com.navanax.recorder" >/dev/null 2>&1
  sleep 2
fi
MANUAL_PID=""
if [ -f "$LOCK" ]; then
  MANUAL_PID="$(sed -n 's/^pid=\([0-9][0-9]*\).*/\1/p' "$LOCK" 2>/dev/null | head -1)"
fi
# Safety: a pid in a stale lock file may since have been REUSED by some
# completely unrelated program of yours. Confirm the process really is a navanax
# recorder before signalling it. Nothing else on your Mac is ever touched.
IS_NAVANAX=no
if [ -n "$MANUAL_PID" ] && kill -0 "$MANUAL_PID" 2>/dev/null; then
  CMD="$(ps -p "$MANUAL_PID" -o command= 2>/dev/null)"
  case "$CMD" in
    *navanax.cli*ingest*) IS_NAVANAX=yes ;;   # the recorder's exact command line, nothing looser
    *) echo "      The lock file names process $MANUAL_PID, but that process is:"
       echo "        ${CMD:-(unreadable)}"
       echo "      That is NOT a navanax recorder -- the lock file is stale and the"
       echo "      number has been reused. Leaving that process completely alone." ;;
  esac
fi
if [ "$IS_NAVANAX" = yes ]; then
  echo "      Found one running (process $MANUAL_PID). Asking it to stop cleanly."
  echo "      A clean stop writes its last frame; the gap from now until the new"
  echo "      job starts is recorded as a gap, which is correct behaviour."
  kill -TERM "$MANUAL_PID" 2>/dev/null
  for _ in $(seq 1 30); do
    kill -0 "$MANUAL_PID" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$MANUAL_PID" 2>/dev/null; then
    fail "[STOPPED] Process $MANUAL_PID did not exit within 30 seconds.
          Nothing has been installed. Close its Terminal window (or press
          Ctrl+C in it), wait for it to say it stopped, and run this again.
          Refusing to continue: installing now would leave two recorders
          contending for one landing zone."
  fi
  echo "      Stopped cleanly. Its Terminal window may still be open -- that is"
  echo "      just a window now; you can close it."
else
  echo "      No hand-started recorder is running. Nothing to stop."
fi

# ------------------------------------------------------------ 5. write the jobs
echo
echo "[5/6] Writing the three job files."
mkdir -p "$AGENTS" data/logs || fail "[FAILED] Could not create $AGENTS or data/logs."
for L in $LABELS; do
  "$PY" tools/launchd.py render "$L" --root "$PROJECT" --python "$PY" \
        --out "$AGENTS/$L.plist" >/dev/null \
    || fail "[FAILED] Could not write $AGENTS/$L.plist -- nothing else was changed."
  echo "      $AGENTS/$L.plist"
done

# ------------------------------------------------------------ 6. load them
echo
echo "[6/6] Handing the jobs to macOS."
UIDNUM="$(id -u)"
DOMAIN="gui/$UIDNUM"
PROBLEMS=0
for L in $LABELS; do
  # Remove any previous copy first, or 'bootstrap' fails with "service already
  # loaded" and you would end up with the OLD job still running.
  launchctl bootout "$DOMAIN/$L" >/dev/null 2>&1
  # 'enable' clears a disabled flag a previous 'unload -w' may have left, so
  # bootstrap is not refused for a reason that has nothing to do with the job.
  launchctl enable "$DOMAIN/$L" >/dev/null 2>&1
  if launchctl bootstrap "$DOMAIN" "$AGENTS/$L.plist" >/dev/null 2>&1; then
    echo "      loaded  $L"
  elif launchctl load -w "$AGENTS/$L.plist" >/dev/null 2>&1; then
    # Older macOS (pre-10.11 syntax). Same effect, deprecated spelling.
    echo "      loaded  $L  (via the older 'load' command)"
  else
    echo "      PROBLEM $L -- macOS refused to load it. Details:"
    launchctl bootstrap "$DOMAIN" "$AGENTS/$L.plist" 2>&1 | sed 's/^/              /'
    PROBLEMS=$((PROBLEMS+1))
  fi
done

sleep 3
echo
echo "------------------------------------------------------------"
echo "  Current state"
echo "------------------------------------------------------------"
for L in $LABELS; do
  if launchctl print "$DOMAIN/$L" >/dev/null 2>&1; then
    S="$(launchctl print "$DOMAIN/$L" 2>/dev/null | sed -n 's/.*state = \(.*\)/\1/p' | head -1)"
    P="$(launchctl print "$DOMAIN/$L" 2>/dev/null | sed -n 's/^[[:space:]]*pid = \([0-9]*\)/\1/p' | head -1)"
    echo "  $L: ${S:-known to macOS}${P:+  (process $P)}"
  else
    echo "  $L: NOT LOADED -- see the message above"
  fi
done

if [ "$PROBLEMS" -gt 0 ]; then
  echo
  echo "============================================================"
  echo "  $PROBLEMS job(s) did NOT load -- see PROBLEM above."
  echo "  The jobs that loaded are running; the failed one is not."
  echo "  Send the lines above to Claude. Nothing else was changed."
  echo "============================================================"
  echo; read -r -p "Press Enter to close. "; exit 1
fi

cat <<EOF

============================================================
  WHAT IS NOW TRUE
============================================================

Three background jobs exist. macOS runs them; nothing you have
open needs to stay open.

  com.navanax.recorder   the stream recorder. Starts when you log
                         in, and if it ever crashes macOS restarts
                         it about 10 seconds later. Every restart
                         records the time it was down as a GAP, so
                         a hole in the record is always visible as
                         a hole and never as a quiet market.

  com.navanax.dashboard  serves the page at http://127.0.0.1:8765
                         Reachable from this computer only. Also
                         restarts itself, but no sooner than 30 seconds
                         after it stops: if something else is already
                         using port 8765 it says so in dashboard.log
                         and waits, instead of retrying in a tight loop.
                         Just open that address in Safari or Chrome
                         whenever you want it -- no window to keep open,
                         no button to press.

  com.navanax.traits     the trait finder. Runs every day at 3:30 AM
                         -- NOT right now. The first run spends up to
                         ~97 of your 120 hourly OpenSea reads, and
                         that is your call, not this installer's:
                         double-click traits.command when you want it
                         now, or let 3:30 AM do it. Later runs are
                         cheap: a finished token list is not
                         re-fetched, only failed tokens retry.

  Logs (plain text, open them in TextEdit):
      $PROJECT/data/logs/recorder.log
      $PROJECT/data/logs/dashboard.log
      $PROJECT/data/logs/traits.log
  Each restart writes a banner line, so you can tell one long
  healthy run from a loop of short crashed ones.

  CLOSING THIS WINDOW CHANGES NOTHING. The jobs are not children
  of this window. Close it whenever you like.

  To see how it is going:  double-click autostart-status.command
  To turn all of this off: double-click autostart-uninstall.command
  start.command still works afterwards -- but do not run it while
  the recorder job is loaded: it will correctly refuse, because the
  background job already holds the recording lock.

============================================================
  THE ONE THING THIS CANNOT DO
============================================================

  When your Mac SLEEPS, recording stops.

  launchd cannot prevent sleep -- no job scheduler on macOS can.
  A closed lid, or the screen sleeping on battery, stops the
  recorder as surely as quitting it. The gap is recorded honestly
  when it wakes, but the events in that window are gone, and
  cancellations and order invalidations in that window can never
  be backfilled from anywhere.

  If you want the Mac to stay awake while it is plugged in, there
  is a separate, optional, one-click switch that runs Apple's own
  /usr/bin/caffeinate: keepawake-install.command (it explains the
  battery and lid trade-off before it does anything). It is
  deliberately NOT part of this install, because changing when
  your Mac sleeps is your decision, not the installer's.

EOF
read -r -p "Press Enter to close this window. "
