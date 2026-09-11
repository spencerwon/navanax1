#!/bin/bash
# Navanax UPDATE — double-click to put the code Claude has merged onto this
# computer and restart the background jobs so they actually run it.
#
# WHAT IT DOES, in order: pull main (fast-forward only), reinstall the package
# ONLY if pyproject.toml changed, re-write the launchd job files ONLY if the
# generator that produces them changed, stop and restart the three com.navanax
# jobs, then read the dashboard's own health endpoint and print what it says.
#
# WHAT IT NEVER TOUCHES: data/. Not the landing zone, not the analytical store,
# not the logs. An update changes code; code is replaceable and the record is
# not (docs/07 §4). If an update ever needs the store rebuilt, that is
# rebuild-store.command's job and it is a separate, deliberate double-click.
#
# IT REFUSES, having changed nothing, in three cases:
#
#   1. There are edits here that are not committed. Pulling on top of them
#      either fails halfway or buries them; this names the files instead.
#
#   2. This checkout is not on main. It does NOT switch for you: a Claude
#      session may have this copy parked on its own branch mid-review, and
#      switching underneath it throws that work away. It prints one sentence
#      for you to send Claude, who knows which branch it is and why.
#
#   3. main and this copy have diverged, so the pull would not be a
#      fast-forward. That means someone committed here. Merging it blind is
#      how a local fix gets silently reverted; Claude sorts it out.
#
# This is the deploy step, and it is the only one. Viewing is
# open-dashboard.command; it starts nothing and is safe to double-click at any
# time.
#
# One thing that looks unsafe and is not: the pull can replace THIS file while
# bash is part-way through reading it. git writes the new content to a temp file
# and renames it into place, so the running shell keeps reading the old inode
# through its open descriptor and finishes the version it started. The next
# double-click gets the new one.

set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1

AGENTS="$HOME/Library/LaunchAgents"
LABELS="com.navanax.recorder com.navanax.dashboard com.navanax.traits"
HEALTH_URL="http://127.0.0.1:8765/api/health"

fail() { echo; echo "$@"; echo; read -r -p "Press Enter to close. "; exit 1; }

echo "============================================================"
echo "  NAVANAX -- update this computer"
echo "  project: $PWD"
echo "============================================================"
echo

command -v git >/dev/null 2>&1 \
  || fail "[REFUSED] git is not installed on this machine, so there is nothing
          to pull from. Nothing was changed."
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || fail "[REFUSED] This folder is not a git working copy, so there is no
          branch to update. Nothing was changed."

# ------------------------------------------------------------ 1. clean tree
# --porcelain respects .gitignore, so data/ never appears here: the record is
# not tracked and cannot make this refuse.
echo "[1/7] Checking for uncommitted edits."
DIRTY="$(git status --porcelain 2>/dev/null)"
if [ -n "$DIRTY" ]; then
  echo
  echo "[REFUSED] These files in this folder have changes that are not committed:"
  echo
  printf '%s\n' "$DIRTY" | sed 's/^/          /'
  echo
  echo "          An update pulls new code over the files here. Doing that on top"
  echo "          of uncommitted edits either stops halfway or buries them, so"
  echo "          this stops instead. Nothing was changed."
  echo
  echo "          Send the list above to Claude and say the update refused."
  echo
  read -r -p "Press Enter to close. "
  exit 1
fi
echo "      Clean."

# ------------------------------------------------------------ 2. on main
echo "[2/7] Checking which branch this copy is on."
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [ "$BRANCH" != "main" ]; then
  echo
  echo "[REFUSED] This copy is on branch '$BRANCH', not main."
  echo
  echo "          This file will not switch branches for you. A Claude session"
  echo "          may have parked this copy on that branch in the middle of"
  echo "          something, and switching underneath it loses that work."
  echo
  echo "          Send Claude exactly this sentence:"
  echo
  echo "              the live checkout is on branch $BRANCH; switch it to main"
  echo
  echo "          Nothing was changed."
  echo
  read -r -p "Press Enter to close. "
  exit 1
fi
echo "      On main."

# ------------------------------------------------------------ 3. pull
echo "[3/7] Pulling main."
OLD="$(git rev-parse HEAD)"
PULL_OUT="$(git pull --ff-only 2>&1)"; PULL_RC=$?
printf '%s\n' "$PULL_OUT" | sed 's/^/      /'
if [ "$PULL_RC" -ne 0 ]; then
  echo
  echo "[REFUSED] The pull did not go through, so nothing here changed."
  echo
  echo "          --ff-only means this only ever fast-forwards: it moves this"
  echo "          copy along main, and never merges or rewrites. If it refused,"
  echo "          either this copy has commits of its own that main does not"
  echo "          have, or the network is down."
  echo
  echo "          Send the lines above to Claude. Do not merge by hand."
  echo
  read -r -p "Press Enter to close. "
  exit 1
fi
NEW="$(git rev-parse HEAD)"
if [ "$OLD" = "$NEW" ]; then
  echo "      Already up to date -- no new commits."
else
  echo "      New commits ($(git rev-parse --short "$OLD")..$(git rev-parse --short "$NEW")):"
  git log --oneline "$OLD..$NEW" | sed 's/^/        /'
fi
CHANGED="$(git diff --name-only "$OLD" "$NEW" 2>/dev/null)"

# ------------------------------------------------------------ 4. dependencies
# Only when the package definition itself moved. `pip install -e` on every
# update is slow, needs the network, and can fail for reasons that have nothing
# to do with the commits you just pulled -- an editable install already points
# at src/, so new Python code is live the moment it lands on disk.
echo "[4/7] Dependencies."
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -z "$PY" ] && fail "[FAILED] No Python 3.10+ found. The code is updated on disk, but
          nothing here could be reinstalled or restarted."
if printf '%s\n' "$CHANGED" | grep -qx 'pyproject.toml'; then
  echo "      pyproject.toml changed -- reinstalling the package."
  "$PY" -m pip install -q -e . \
    || fail "[FAILED] pip could not reinstall the package. The new code is on disk
          but its dependencies may not be. Send this to Claude; the jobs were
          NOT restarted."
  echo "      Reinstalled."
else
  echo "      pyproject.toml unchanged -- nothing to reinstall."
fi

# ------------------------------------------------------------ 5. job files
# The plists name an absolute interpreter and an absolute project path, so they
# do not go stale when ordinary code changes. They go stale only when the
# generator changes -- and a plist that is never re-rendered is how a fixed
# ThrottleInterval sits in the repo and not on the machine.
echo "[5/7] launchd job files."
if printf '%s\n' "$CHANGED" | grep -qx 'tools/launchd.py'; then
  if [ -d "$AGENTS" ]; then
    for L in $LABELS; do
      "$PY" tools/launchd.py render "$L" --root "$PWD" --python "$PY" \
            --out "$AGENTS/$L.plist" >/dev/null \
        || fail "[FAILED] Could not re-write $AGENTS/$L.plist. The jobs were NOT
          restarted, so the ones already loaded keep running the old files."
      echo "      re-wrote  $AGENTS/$L.plist"
    done
  else
    echo "      tools/launchd.py changed, but $AGENTS does not exist --"
    echo "      the background jobs were never installed here. Nothing to re-write."
  fi
else
  echo "      Generator unchanged -- the installed job files are still correct."
fi

# ------------------------------------------------------------ 6. restart
# bootout then bootstrap, per job. bootout sends SIGTERM, which is what lets the
# recorder flush its final frame (docs/04 §8.8); bootstrap loads the plist
# fresh, so a re-rendered file takes effect. launchctl is an Apple component:
# every call to it is guarded, and on any other machine this step says so and
# the update still counts as done.
echo "[6/7] Restarting the background jobs."
if command -v launchctl >/dev/null 2>&1; then
  DOMAIN="gui/$(id -u)"
  for L in $LABELS; do
    if [ ! -f "$AGENTS/$L.plist" ]; then
      echo "      $L: no job file installed -- run autostart-install.command."
      continue
    fi
    launchctl bootout "$DOMAIN/$L" >/dev/null 2>&1
    launchctl enable "$DOMAIN/$L" >/dev/null 2>&1
    if launchctl bootstrap "$DOMAIN" "$AGENTS/$L.plist" >/dev/null 2>&1; then
      echo "      $L: restarted."
    else
      echo "      $L: PROBLEM -- macOS refused to load it:"
      launchctl bootstrap "$DOMAIN" "$AGENTS/$L.plist" 2>&1 | sed 's/^/              /'
    fi
  done
  echo "      Waiting 5 s for them to come up."
  sleep 5
  for L in $LABELS; do
    if launchctl print "$DOMAIN/$L" >/dev/null 2>&1; then
      S="$(launchctl print "$DOMAIN/$L" 2>/dev/null | sed -n 's/.*state = \(.*\)/\1/p' | head -1)"
      P="$(launchctl print "$DOMAIN/$L" 2>/dev/null | sed -n 's/^[[:space:]]*pid = \([0-9]*\).*/\1/p' | head -1)"
      echo "      $L  state ${S:-known to macOS}${P:+  (process $P)}"
    else
      echo "      $L  NOT LOADED"
    fi
  done
else
  echo "      launchctl is not available on this machine (it is an Apple"
  echo "      component, so this is not a Mac). The three background jobs were"
  echo "      NOT restarted. The new code is on disk and the update is done."
fi

# ------------------------------------------------------------ 7. health
# Ask the dashboard, not the process table. "The job is running" and "the store
# is readable" are different claims, and the second is the one that goes wrong
# (BUG-20260910-067). quick_check is the store's own page-level verdict;
# store_writer says which process holds the fold-writer lock.
echo "[7/7] Dashboard health."
"$PY" - "$HEALTH_URL" <<'PYEOF'
import json
import sys
import urllib.error
import urllib.request

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=10) as r:
        health = json.loads(r.read().decode("utf-8"))
except (urllib.error.URLError, OSError, ValueError) as exc:
    print(f"      Could not read {url}")
    print(f"      ({type(exc).__name__}: {exc})")
    print("      The dashboard is not answering. It may still be starting, or")
    print("      folding a large store. Try open-dashboard.command in a minute;")
    print("      if it is still silent, run autostart-status.command.")
else:
    def show(label, value):
        text = json.dumps(value, default=str) if not isinstance(value, str) else value
        print(f"      {label}: {text}")
    show("quick_check", health.get("quick_check", "(absent from /api/health)"))
    show("store_writer", health.get("store_writer", "(absent from /api/health)"))
PYEOF

echo
echo "============================================================"
echo "  UPDATE DONE"
echo "============================================================"
echo
echo "  Nothing under data/ was read, written or moved by this file."
echo
echo "  Double-click open-dashboard.command to view."
echo
read -r -p "Press Enter to close. "
