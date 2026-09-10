#!/bin/bash
# Navanax PULL -- double-click to bring this Mac up to date with GitHub.
#
# Fast-forward only. That means: if GitHub is simply ahead of you, your copy
# catches up. If your copy and GitHub have BOTH changed, this stops and tells
# you, rather than trying to combine them. Combining two histories is a
# judgment call, and a bad one loses work silently.
#
# It never merges, never rebases, and never resolves a conflict.
# Pulling is safe -- it needs no sign-off and no approval. Only pushing does.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"

echo "============================================================"
echo "  NAVANAX -- pull (fast-forward only)"
echo "============================================================"
echo "  Branch: ${BRANCH:-(none)}"
echo

if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  echo "  This checkout is not on a branch, so there is nothing to"
  echo "  fast-forward. Bring it to the tech-lead."
  echo
  read -r -p "Press Enter to close this window. "
  exit 1
fi

DIRTY="$(git status --porcelain)"
if [ -n "$DIRTY" ]; then
  echo "  There are uncommitted changes here:"
  echo
  echo "$DIRTY" | sed 's/^/    /'
  echo
  echo "  Pulling on top of them could bury them, so this stops."
  echo "  Commit them or set them aside first."
  echo
  read -r -p "Press Enter to close this window. "
  exit 1
fi

echo "  Fetching from GitHub..."
if ! git fetch --prune origin; then
  echo
  echo "  Could not reach GitHub. Usually that is the network, or"
  echo "  GitHub Desktop being signed out. Try again in a moment."
  echo
  read -r -p "Press Enter to close this window. "
  exit 1
fi

UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"
if [ -z "$UPSTREAM" ]; then
  echo
  echo "  Branch '$BRANCH' does not exist on GitHub yet, so there is"
  echo "  nothing to pull. That is normal for a brand-new branch --"
  echo "  push.command will create it."
  echo
  read -r -p "Press Enter to close this window. "
  exit 0
fi

BEHIND="$(git rev-list --count "HEAD..$UPSTREAM" 2>/dev/null || echo 0)"
AHEAD="$(git rev-list --count "$UPSTREAM..HEAD" 2>/dev/null || echo 0)"

echo
echo "  Comparing with $UPSTREAM:"
echo "    $BEHIND commit(s) on GitHub that you do not have"
echo "    $AHEAD commit(s) here that GitHub does not have"
echo

if [ "$BEHIND" = "0" ]; then
  echo "  Already up to date. Nothing to do."
  echo
  read -r -p "Press Enter to close this window. "
  exit 0
fi

if [ "$AHEAD" != "0" ]; then
  echo "------------------------------------------------------------"
  echo "  THE TWO HISTORIES HAVE DIVERGED. Nothing was changed."
  echo
  echo "  Both sides have commits the other does not. Combining them"
  echo "  is a decision, not a button -- so this stops here."
  echo
  echo "  Only on GitHub:"
  git log --oneline "HEAD..$UPSTREAM" | sed 's/^/    /'
  echo
  echo "  Only here:"
  git log --oneline "$UPSTREAM..HEAD" | sed 's/^/    /'
  echo
  echo "  Bring this to the tech-lead. Do not merge or rebase it"
  echo "  yourself, and do not let anything force-push over it --"
  echo "  one of those two lists would disappear."
  echo "------------------------------------------------------------"
  echo
  read -r -p "Press Enter to close this window. "
  exit 1
fi

echo "  Fast-forwarding to $UPSTREAM..."
echo
if git merge --ff-only "$UPSTREAM"; then
  echo
  echo "------------------------------------------------------------"
  echo "  Up to date. Now at:"
  git log --oneline -1 | sed 's/^/    /'
  echo "------------------------------------------------------------"
else
  echo
  echo "------------------------------------------------------------"
  echo "  The fast-forward was refused. Git's reason is above."
  echo "  Nothing was merged and nothing was lost. Bring it to the"
  echo "  tech-lead."
  echo "------------------------------------------------------------"
fi
echo
read -r -p "Press Enter to close this window. "
