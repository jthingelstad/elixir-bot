#!/usr/bin/env bash
# Read-only audit of the small exception ledger used by objective owners.

set -euo pipefail

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated"; exit 1; }

section() { printf '\n==> %s\n' "$1"; }
list() { gh issue list --state open --limit 100 "$@" \
  --json number,title,labels,updatedAt \
  --jq '.[] | "  #\(.number)  [\([.labels[].name] | join(","))]  \(.title)  (updated \(.updatedAt[0:10]))"'; }

echo "Objective issue audit — $(gh repo view --json nameWithOwner --jq .nameWithOwner)"

section "Needs Jamie decision"
list --label decision

section "Run Elixir"
list --label objective:run

section "Understand Clash Royale"
list --label objective:game

section "Improve Elixir"
list --label objective:agent

section "Open issues with no objective owner"
gh issue list --state open --limit 100 --json number,title,labels,updatedAt \
  --jq '.[] | select((.labels|map(.name)) as $l | (["objective:run","objective:game","objective:agent"] | map(. as $x | $l | index($x)) | any) | not) | "  #\(.number)  [\([.labels[].name]|join(","))]  \(.title)"'

section "Stale objective issues (no update in 14 days)"
cutoff="$(date -u -v-14d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '-14 days' +%Y-%m-%dT%H:%M:%SZ)"
gh issue list --state open --limit 100 --json number,title,labels,updatedAt \
  --jq '.[] | select(.updatedAt < "'"$cutoff"'") | select((.labels|map(.name)|map(startswith("objective:"))|any)) | "  #\(.number)  \(.title)  (updated \(.updatedAt[0:10]))"'

echo
echo "==> Done."
