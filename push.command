#!/bin/bash
# Navanax PUSH -- double-click to send the current branch to GitHub.
#
# It checks first and pushes second, and it will not push unless you type PUSH.
#
# Why this is a click and not something an agent does for you: the Linux VM the
# agents run in cannot reach GitHub at all -- the corporate proxy refuses the
# connection (403 after CONNECT). Your Mac can, because GitHub Desktop already
# holds your credentials. And you approve every pull request, so the last step
# being yours is the point, not an inconvenience.
#
# This NEVER merges, NEVER pushes to main, and NEVER uses --force.
# After it runs you still have to open the pull request and approve it yourself.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "No Python 3.10+ found."; read -r -p "Enter to close. "; exit 1; }

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"

echo "============================================================"
echo "  NAVANAX -- push a branch to GitHub"
echo "============================================================"
echo "  Branch: ${BRANCH:-(none)}"
echo
echo "  Running every check before anything is sent..."
echo

if ! "$PY" tools/pushgate.py; then
  echo
  echo "------------------------------------------------------------"
  echo "  NOTHING WAS PUSHED."
  echo
  echo "  The checks above say this branch is not ready. Each failure"
  echo "  line has an arrow (->) under it saying what fixes it. If a"
  echo "  sign-off is missing, that is a review that has not happened"
  echo "  yet -- bring it to the tech-lead rather than editing the file."
  echo "------------------------------------------------------------"
  read -r -p "Press Enter to close this window. "
  exit 1
fi

echo
echo "------------------------------------------------------------"
echo "  All checks passed. Nothing has been sent yet."
echo
echo "  Read the summary above. If it is the work you expect to see,"
echo "  type   PUSH   and press Enter. Anything else cancels."
echo "------------------------------------------------------------"
CONFIRM=""
read -r -p "  Type PUSH to send it: " CONFIRM
if [ "$CONFIRM" != "PUSH" ]; then
  echo
  echo "  Cancelled. Nothing was pushed."
  read -r -p "  Press Enter to close this window. "
  exit 1
fi

echo
echo "  Pushing $BRANCH to origin..."
echo
git push --set-upstream origin "$BRANCH" 2>&1
PUSH_OK=$?

echo
if [ "$PUSH_OK" -eq 0 ]; then
  URL="$(git remote get-url origin 2>/dev/null | sed -e 's#git@github.com:#https://github.com/#' -e 's#\.git$##')"
  echo "------------------------------------------------------------"
  echo "  PUSHED. The branch is now on GitHub:"
  echo "    $URL/tree/$BRANCH"
  echo
  echo "  YOUR NEXT STEP -- nobody else can do this part:"
  echo "    1. Open  $URL/compare/$BRANCH?expand=1"
  echo "    2. Read the description. It should say what changed, what"
  echo "       was verified, and what is NOT fixed."
  echo "    3. If you are satisfied, approve and merge it yourself."
  echo
  echo "  Nothing merged. Nothing touched main. No agent can approve"
  echo "  this for you, and none is allowed to ask you to hurry."
  echo "------------------------------------------------------------"
else
  echo "------------------------------------------------------------"
  echo "  THE PUSH FAILED. Git's own words are above."
  echo
  echo "  In plain language, it is almost always one of two things:"
  echo
  echo "    * Credentials -- GitHub is not recognising this Mac."
  echo "      Open GitHub Desktop, make sure you are signed in, and"
  echo "      try this again."
  echo
  echo "    * The branch moved on GitHub since you last fetched, so"
  echo "      your copy and the remote's copy have diverged. Double-"
  echo "      click pull.command first. If that also refuses, the two"
  echo "      histories genuinely conflict -- bring it to the"
  echo "      tech-lead. Do NOT force it; --force would delete work"
  echo "      that is on the remote and not on this Mac."
  echo "------------------------------------------------------------"
fi
echo
read -r -p "Press Enter to close this window. "
