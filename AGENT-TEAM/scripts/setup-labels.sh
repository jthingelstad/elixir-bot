#!/usr/bin/env bash
#
# Set up and clean the GitHub labels that drive the AGENT-TEAM workflow.
#
# Run from the repo root:   AGENT-TEAM/scripts/setup-labels.sh
# Re-runnable (idempotent) — safe to run again after editing this file.
#
# Requires: the GitHub CLI (`gh`), authenticated. The repo is inferred from the
# current directory's git remote.
#
# STRUCTURE: the "CORE" block below is identical across every project (it is the
# shared taxonomy in AGENT-TEAM/WORKFLOW.md). Only edit the "PROJECT EXTENSIONS"
# block for this repo's domain labels + one-time migrations. Keep CORE in sync
# across projects.

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

# ─────────────────────────────────────────────────────────────────────────────
# CORE  — identical across every AGENT-TEAM project. See AGENT-TEAM/WORKFLOW.md.
# ─────────────────────────────────────────────────────────────────────────────
echo "==> CORE: product discovery + approval gate"
upsert "proposal"     "D4C5F9" "New direction awaiting Jamie's approval — do NOT build yet"
upsert "approved"     "0E8A16" "Approved by Jamie — cleared to build"
upsert "ready"        "C2E0C6" "Triaged and actionable now — Build Manager picks these first"
upsert "needs-design" "BFBFBF" "Not actionable until the approach is settled"
upsert "blocked"      "000000" "Waiting on an external dependency"

echo "==> CORE: work-type labels"
upsert "bug"         "D73A4A" "Reproducible defect — Build Manager fixes"
upsert "regression"  "B60205" "Worked before, now broken (high priority) — Build Manager"
upsert "enhancement" "A2EEEF" "New feature or capability — Build Manager builds"
upsert "eval"        "5319E7" "Missing measurement — Evaluator"
upsert "operations"  "D93F0B" "Production health, deploys, runtime — Operations Manager"
upsert "meta"        "5319E7" "Change to the team itself (role/workflow/labels/scripts) — Manager files, Jamie approves"

echo "==> CORE: concurrency + provenance"
upsert "wip"          "FBCA04" "Claimed — an agent is working this now; others skip it (released if the agent stops)"
upsert "needs-deploy" "E36209" "Committed but NOT live until deployed (e.g. a DB migration) — Operations Manager deploys promptly & atomically"
upsert "generated"    "FEF2C0" "Filed by an automated agent (not a human)"

# Kept as-is on purpose (useful GitHub defaults):
#   documentation, duplicate, invalid, question, wontfix   (wontfix declines proposals)
#   dependencies, github_actions, python                   (Dependabot auto-applies these)

# ─────────────────────────────────────────────────────────────────────────────
# PROJECT EXTENSIONS  — elixir-bot only. Domain work-type labels + migrations.
# ─────────────────────────────────────────────────────────────────────────────
echo "==> PROJECT: elixir-bot domain labels"
upsert "prompt"   "BFD4F2" "Prompt or persona-text change — Build Manager (+ Evaluator)"
upsert "persona"  "C5DEF5" "Gap vs SOUL.md / PURPOSE.md — Product/Quality -> Build Manager"
upsert "data"     "1D76DB" "New/changed data pattern or data-quality finding — Data Analyst -> Product Manager"

echo "==> PROJECT: elixir-bot label migrations"
rename "cost-reliability" "reliability" "E99695" "Stability, runtime, cost, observability — Operations Manager"
rename "routing-quality"  "quality"     "FBCA04" "Recommendation quality: accuracy, noise, routing, delivery — Quality Manager"
remove "backlog-hygiene"; remove "codex"; remove "coaching"; remove "data-health"
remove "elixir-improvement"; remove "signal-gap"; remove "needs-human-triage"
remove "good first issue"; remove "help wanted"

echo
echo "==> Done. Current labels:"
gh label list --limit 300
