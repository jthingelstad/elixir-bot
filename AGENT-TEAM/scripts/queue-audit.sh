#!/usr/bin/env bash
#
# Queue audit for the AGENT-TEAM workflow — surfaces work slipping through the
# cracks. Read-only (gh queries only). Primarily the Manager's helper, but any
# role can run it. The repo is inferred from the current directory's git remote.
#
# This file is byte-identical across every project's AGENT-TEAM/scripts/.
#
# Requires: the GitHub CLI (`gh`), authenticated.

set -euo pipefail

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found — install from https://cli.github.com/"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated — run: gh auth login"; exit 1; }

# Issues with no update in the last N days count as "aging".
STALE_DAYS="${STALE_DAYS:-4}"
WIP_STALE_HOURS="${WIP_STALE_HOURS:-24}"

section() { printf '\n==> %s\n' "$1"; }
# List issues matching a gh search, showing number / age / labels / title.
list() { gh issue list --state open --limit 100 "$@" \
  --json number,title,labels,updatedAt \
  --jq '.[] | "  #\(.number)  [\([.labels[].name] | join(","))]  \(.title)  (updated \(.updatedAt[0:10]))"' \
  || true; }

echo "Queue audit — $(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || echo 'this repo')"

section "Proposals awaiting Jamie's decision (approve → approved+ready, or wontfix)"
list --label proposal

section "Ready/approved but UNCLAIMED (no wip) — should be getting picked up"
gh issue list --state open --limit 100 --json number,title,labels,updatedAt \
  --jq '.[] | select((.labels|map(.name)) as $l | (($l|index("ready")) or ($l|index("approved"))) and (($l|index("wip"))|not)) | "  #\(.number)  [\([.labels[].name]|join(","))]  \(.title)"' || true

section "Stale wip (claimed but no update in >${WIP_STALE_HOURS}h — candidate for the stale-claim rule)"
cutoff="$(date -u -v-"${WIP_STALE_HOURS}"H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "-${WIP_STALE_HOURS} hours" +%Y-%m-%dT%H:%M:%SZ)"
gh issue list --state open --label wip --limit 100 --json number,title,labels,updatedAt \
  --jq --arg cutoff "$cutoff" '.[] | select(.updatedAt < $cutoff) | "  #\(.number)  \(.title)  (updated \(.updatedAt[0:16]))"' || true

section "Aging & unworked (open >${STALE_DAYS}d, not wip/blocked/needs-design)"
cutoff_d="$(date -u -v-"${STALE_DAYS}"d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "-${STALE_DAYS} days" +%Y-%m-%dT%H:%M:%SZ)"
gh issue list --state open --limit 100 --json number,title,labels,updatedAt \
  --jq --arg cutoff "$cutoff_d" '.[] | select(.updatedAt < $cutoff) | select((.labels|map(.name)) as $l | (($l|index("wip"))|not) and (($l|index("blocked"))|not) and (($l|index("needs-design"))|not)) | "  #\(.number)  [\([.labels[].name]|join(","))]  \(.title)"' || true

section "Stale needs-design / blocked (revisit or close)"
list --label needs-design
list --label blocked

section "Unlabeled open issues (need triage)"
gh issue list --state open --limit 100 --json number,title,labels \
  --jq '.[] | select((.labels|length)==0) | "  #\(.number)  \(.title)"' || true

echo
echo "==> Done."
