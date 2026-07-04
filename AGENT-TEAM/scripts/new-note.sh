#!/usr/bin/env bash
#
# Scaffold a per-run note in AGENT-TEAM/notes/ (gitignored scratch). Roles call
# this at the end of a run to record what they did, so the team and the weekly
# Manager can see recent activity without committing per-run noise.
#
#   AGENT-TEAM/scripts/new-note.sh <role> <slug>
#   e.g.  AGENT-TEAM/scripts/new-note.sh build-manager fix-reminder-dedupe
#
# Prints the path it created (open it and fill it in).
#
# This file is byte-identical across every project's AGENT-TEAM/scripts/.

set -euo pipefail

role="${1:-role}"
slug="${2:-run}"
# Slugify the role/slug loosely (lowercase, spaces/underscores → dashes).
norm() { printf '%s' "$1" | tr '[:upper:] _' '[:lower:]--' | tr -cd '[:alnum:]-'; }
role="$(norm "$role")"
slug="$(norm "$slug")"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
notes_dir="$(dirname "$script_dir")/notes"
mkdir -p "$notes_dir"

date_iso="$(date +%Y-%m-%d)"
stamp="$(date +%Y-%m-%dT%H:%M:%S%z)"
path="$notes_dir/${date_iso}-${role}-${slug}.md"

if [ -e "$path" ]; then
  echo "$path"   # already exists — don't clobber
  exit 0
fi

cat > "$path" <<EOF
# ${role} — ${slug}

Run: ${stamp}

## Did
<one focused thing this run>

## Evidence
<data / logs / files / issue comments you relied on>

## Issues touched
<#N filed / claimed / closed — and label changes>

## Handoff / next
<what another role or the next run should pick up, or "none">
EOF

echo "$path"
