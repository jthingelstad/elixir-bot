#!/usr/bin/env bash
#
# Shared git preflight for AGENT-TEAM objectives. Run from the repo root at the start
# of every run. Prints the working-tree state and a verdict; exits non-zero when
# the tree is in a state an automated run should NOT act on. An automated agent
# should stop and report on a non-zero exit rather than pull/merge/rebase/stash.

set -euo pipefail

command -v git >/dev/null 2>&1 || { echo "git not found"; exit 2; }

if ! git fetch origin --prune >/dev/null 2>&1; then
  echo "  ✗ git fetch origin failed — synchronization is unknown; stop and report."
  exit 1
fi

if ! branch="$(git symbolic-ref --quiet --short HEAD)"; then
  echo "  ✗ detached HEAD — stop and report."
  exit 1
fi
echo "==> Preflight on branch: $branch"
git status --short --branch | sed 's/^/  /'

verdict=0

if [ "$branch" != "main" ]; then
  echo "  ✗ branch must be main — stop and report."
  verdict=1
fi

# Dirty worktree?
if [ -n "$(git status --porcelain)" ]; then
  echo "  ✗ worktree is DIRTY — stop and report (do not act on unexpected local changes)."
  verdict=1
fi

# Compare to upstream if one is set.
if upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
  if [ "$upstream" != "origin/main" ]; then
    echo "  ✗ upstream must be origin/main, not $upstream — stop and report."
    verdict=1
  else
    ahead="$(git rev-list --count '@{u}..HEAD')"
    behind="$(git rev-list --count 'HEAD..@{u}')"
    if [ "$behind" -gt 0 ] && [ "$ahead" -gt 0 ]; then
      echo "  ✗ DIVERGED from $upstream ($ahead ahead, $behind behind) — stop and report."
      verdict=1
    elif [ "$behind" -gt 0 ]; then
      echo "  ✗ BEHIND $upstream by $behind — stop and report (do not pull from an automated run)."
      verdict=1
    elif [ "$ahead" -gt 0 ]; then
      echo "  ✗ AHEAD of $upstream by $ahead — stop and report; pre-existing commits are read-only."
      verdict=1
    fi
  fi
else
  echo "  ✗ no upstream configured for $branch — stop and report."
  verdict=1
fi

if [ "$verdict" -eq 0 ]; then
  echo "  ✓ clean main exactly synchronized with origin/main — safe to work."
fi
exit "$verdict"
