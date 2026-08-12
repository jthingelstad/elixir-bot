#!/usr/bin/env bash
# Read-only audit of the objective-owned exception ledger.

set -euo pipefail

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found"; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq not found"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated"; exit 1; }

objectives=("objective:run" "objective:game" "objective:agent")
allowed='["objective:run","objective:game","objective:agent"]'
retired='["proposal","approved","ready","needs-design","wip","needs-deploy","meta","needs-data","needs-quality","needs-eval"]'
issues="$(gh issue list --state open --limit 1000 --json number,title,labels,updatedAt)"
verdict=0

echo "Objective queue — $(gh repo view --json nameWithOwner --jq .nameWithOwner)"
for objective in "${objectives[@]}"; do
  printf '\n==> %s\n' "$objective"
  printf '%s' "$issues" | jq -r --arg objective "$objective" '
    .[] | select([.labels[].name] | index($objective))
    | "  #\(.number)  [\([.labels[].name] | join(","))]  \(.title)  (\(.updatedAt[0:10]))"'
done

printf '\n==> Decisions waiting on Jamie\n'
printf '%s' "$issues" | jq -r '
  .[] | select([.labels[].name] | index("decision")) | "  #\(.number)  \(.title)"'

printf '\n==> Missing, conflicting, or unknown objective ownership\n'
bad="$(printf '%s' "$issues" | jq -r --argjson allowed "$allowed" '
  .[] | [.labels[].name | select(startswith("objective:"))] as $owners
  | select(($owners | length) != 1 or ($allowed | index($owners[0]) | not))
  | "  #\(.number)  owners=\($owners | if length == 0 then "none" else join(",") end)  \(.title)"')"
if [ -n "$bad" ]; then printf '%s\n' "$bad"; verdict=1; fi

printf '\n==> Retired workflow controls on open issues\n'
old="$(printf '%s' "$issues" | jq -r --argjson retired "$retired" '
  .[] | [.labels[].name | select((. as $name | $retired | index($name)) != null or startswith("dispatch:") or startswith("legacy:"))] as $controls
  | select($controls | length > 0)
  | "  #\(.number)  [\($controls | join(","))]  \(.title)"')"
if [ -n "$old" ]; then printf '%s\n' "$old"; verdict=1; fi

printf '\n==> Stale objective issues (no update in 14 days)\n'
cutoff="$(date -u -v-14d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '-14 days' +%Y-%m-%dT%H:%M:%SZ)"
printf '%s' "$issues" | jq -r --arg cutoff "$cutoff" '
  .[] | select(.updatedAt < $cutoff) | select([.labels[].name | startswith("objective:")] | any)
  | "  #\(.number)  \(.title)  (updated \(.updatedAt[0:10]))"'

if [ "$verdict" -eq 0 ]; then
  echo "  ✓ every open issue has one known objective owner and no retired controls"
fi
exit "$verdict"
