#!/usr/bin/env bash
#
# Shared git preflight for AGENT-TEAM roles. Run from the repo root at the start
# of every run. Prints the working-tree state and a verdict; exits non-zero when
# the tree is in a state an automated run should NOT act on (dirty / behind /
# diverged). An automated agent should stop and report (file an issue) on a
# non-zero exit rather than pull/merge/rebase/stash.
#
# This file is byte-identical across every project's AGENT-TEAM/scripts/.

set -euo pipefail

command -v git >/dev/null 2>&1 || { echo "git not found"; exit 2; }

git fetch origin --prune >/dev/null 2>&1 || echo "  (warning: git fetch failed — offline?)"

branch="$(git rev-parse --abbrev-ref HEAD)"
echo "==> Preflight on branch: $branch"
git status --short --branch | sed 's/^/  /'

verdict=0

# Dirty worktree?
if [ -n "$(git status --porcelain)" ]; then
  echo "  ✗ worktree is DIRTY — stop and report (do not act on unexpected local changes)."
  verdict=1
fi

# Compare to upstream if one is set.
if upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
  ahead="$(git rev-list --count '@{u}..HEAD')"
  behind="$(git rev-list --count 'HEAD..@{u}')"
  if [ "$behind" -gt 0 ] && [ "$ahead" -gt 0 ]; then
    echo "  ✗ DIVERGED from $upstream ($ahead ahead, $behind behind) — stop and report."
    verdict=1
  elif [ "$behind" -gt 0 ]; then
    echo "  ✗ BEHIND $upstream by $behind — stop and report (do not pull from an automated run)."
    verdict=1
  elif [ "$ahead" -gt 0 ]; then
    echo "  ! AHEAD of $upstream by $ahead — you may only push if your role is expected to publish these existing commits."
  fi
else
  echo "  (no upstream configured for $branch)"
fi

if [ "$verdict" -eq 0 ]; then
  echo "  ✓ clean and in sync — safe to work."
fi
exit "$verdict"
