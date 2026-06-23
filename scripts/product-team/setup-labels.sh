#!/usr/bin/env bash
#
# Set up and clean the GitHub labels that drive the product-team workflow.
#
# Run once from the repo root:   scripts/product-team/setup-labels.sh
# Re-runnable (idempotent) — safe to run again after editing this file.
#
# Requires: the GitHub CLI (`gh`), authenticated. The repo is inferred from
# the current directory's git remote, so run it inside the elixir-bot checkout.
#
# See README.md in this directory for what each label means and who works it.

set -euo pipefail

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found — install from https://cli.github.com/"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated — run: gh auth login"; exit 1; }

# Snapshot existing labels once so we can branch on what's already there.
existing="$(gh label list --limit 300 --json name --jq '.[].name')"
has() { printf '%s\n' "$existing" | grep -Fxq "$1"; }

upsert() { # name color description  — create or update in place
  gh label create "$1" --color "$2" --description "$3" --force >/dev/null
  echo "  upsert  $1"
}
rename() { # old new color description — rename if old exists, else upsert new
  if has "$1"; then
    gh label edit "$1" --name "$2" --color "$3" --description "$4" >/dev/null
    echo "  rename  $1 -> $2"
  else
    upsert "$2" "$3" "$4"
  fi
}
remove() { # name — delete if present
  if has "$1"; then
    gh label delete "$1" --yes >/dev/null
    echo "  delete  $1"
  fi
}

echo "==> Removing messy / redundant labels"
remove "backlog-hygiene"     # vague auto-triage; superseded by the workflow below
remove "codex"               # tool-name tag, no workflow meaning
remove "coaching"            # domain tag, not a workflow stage
remove "data-health"         # domain tag, not a workflow stage
remove "elixir-improvement"  # meaningless — all work is an Elixir improvement
remove "signal-gap"          # domain tag; file as proposal/quality instead
remove "needs-human-triage"  # replaced by proposal -> approved gate
remove "good first issue"    # irrelevant to a solo/automated repo
remove "help wanted"         # irrelevant to a solo/automated repo

echo "==> Renaming / repurposing existing labels"
rename "cost-reliability" "reliability" "E99695" "Stability, runtime, cost, observability — Operations Manager"
rename "routing-quality"  "quality"     "FBCA04" "Recommendation quality: accuracy, noise, routing, delivery — Quality Manager"

echo "==> Lane / work-type labels"
upsert "operations"  "D93F0B" "Production health, deploys, runtime — Operations Manager"
upsert "bug"         "D73A4A" "Reproducible defect — Build Manager fixes"
upsert "regression"  "B60205" "Worked before, now broken (high priority) — Build Manager"
upsert "enhancement" "A2EEEF" "New feature or capability — Build Manager builds"
upsert "prompt"      "BFD4F2" "Prompt or persona-text change — Build Manager (+ Evaluator)"
upsert "persona"     "C5DEF5" "Gap vs SOUL.md / PURPOSE.md — Product/Quality -> Build Manager"
upsert "eval"        "5319E7" "Missing measurement — Evaluator"
upsert "data"        "1D76DB" "New/changed data pattern or data-quality finding — Data Analyst -> Product Manager"

echo "==> Product discovery + approval gate"
upsert "proposal"     "D4C5F9" "Product Manager idea awaiting approval — do NOT build yet"
upsert "approved"     "0E8A16" "Approved by Jamie — cleared to build"
upsert "ready"        "C2E0C6" "Triaged and actionable now — Build Manager picks these first"
upsert "needs-design" "BFBFBF" "Not actionable until the approach is settled"
upsert "blocked"      "000000" "Waiting on an external dependency"

echo "==> Concurrency / state"
upsert "wip"         "FBCA04" "Claimed — an agent is working this now; others skip it (released if the agent stops)"

echo "==> Provenance"
upsert "generated"   "FEF2C0" "Filed by an automated product-team agent (not a human)"

# Kept as-is on purpose:
#   documentation                   — useful default
#   duplicate, invalid, question, wontfix — useful triage outcomes (wontfix declines proposals)
#   dependencies, github_actions, python  — Dependabot auto-applies these

echo
echo "==> Done. Current labels:"
gh label list --limit 300
