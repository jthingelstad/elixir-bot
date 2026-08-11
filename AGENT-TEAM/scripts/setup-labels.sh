#!/usr/bin/env bash
# Install the compact objective-owner label set and retire routing labels.

set -euo pipefail

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated"; exit 1; }

existing="$(gh label list --limit 300 --json name --jq '.[].name')"
has() { printf '%s\n' "$existing" | grep -Fxq "$1"; }
upsert() {
  gh label create "$1" --color "$2" --description "$3" --force >/dev/null
  echo "  upsert  $1"
}
remove() {
  if has "$1"; then
    gh label delete "$1" --yes >/dev/null
    echo "  delete  $1"
  fi
}

echo "==> Objective ownership"
upsert "objective:run"   "D93F0B" "Owned end-to-end by Run Elixir"
upsert "objective:game"  "1D76DB" "Owned end-to-end by Understand Clash Royale"
upsert "objective:agent" "FBCA04" "Owned end-to-end by Improve Elixir"
upsert "decision"        "D4C5F9" "Jamie must answer before this objective continues"

echo "==> Descriptive work types"
upsert "bug"         "D73A4A" "Reproducible defect"
upsert "regression"  "B60205" "Worked before and is now broken"
upsert "enhancement" "A2EEEF" "New capability or material improvement"
upsert "eval"        "5319E7" "Measurement or regression-coverage work"
upsert "operations"  "D93F0B" "Production health, runtime, or deploy concern"
upsert "reliability" "E99695" "Reliability, observability, cost, or recovery concern"
upsert "data"        "1D76DB" "Clash Royale data, schema, or interpretation finding"
upsert "quality"     "FBCA04" "Agent accuracy, relevance, timing, or noise finding"
upsert "prompt"      "BFD4F2" "Prompt or workflow-language change"
upsert "persona"     "C5DEF5" "Gap against SOUL.md or PURPOSE.md"
upsert "blocked"     "000000" "Waiting on an external dependency"

echo "==> Retire the former issue-routing workflow"
for label in \
  proposal approved ready needs-design wip needs-deploy generated meta \
  needs-data needs-quality needs-eval \
  dispatch:operations dispatch:build dispatch:evaluator dispatch:data \
  dispatch:quality dispatch:product dispatch:manager; do
  remove "$label"
done

echo
echo "==> Done. Current labels:"
gh label list --limit 300
