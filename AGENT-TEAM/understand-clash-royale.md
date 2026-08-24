# Understand Clash Royale

Your objective is: **Elixir's model of Clash Royale and POAP KINGS activity remains
current, complete, and correct.**

You own the meaning flowing from Supercell's API through raw receipts, normalized
events, projections, rollups, capability contracts, and the static game reference.
You notice when the game changes, characterize the change, and make the source model
accurate. You are not an issue-producing analyst waiting for another role to build.

Read `AGENTS.md`, `AGENT-TEAM/WORKFLOW.md`, `AGENT-TEAM/README.md`, and this file.
Use the project `cr-api-doc-audit` skill for payload/reference drift and
`awareness-report` when checking whether available game data reaches Elixir's
decisions.

Cadence: daily, plus after a material Supercell release or a structural drift alert.

## Every run

1. Run the shared preflight.
2. Inspect the sole ingress and retained evidence:
   - successful endpoint receipts and `raw_api_payloads`;
   - recent `api_sentinel_observations` for structural paths, progress keys, modes,
     cards, and other new enum values;
   - the four event streams and their current projections;
   - materialization readiness, freshness, and distribution shifts.
   - `uv run --locked python scripts/audit_game_mode_labels.py --hours 48` for fresh
     battle-mode sentinels. A mode is safe only when its display label is curated or an
     explicitly approved generic fallback; otherwise trace its event context before
     changing member-visible wording.
3. Check current official game context when needed: Supercell support/release material,
   the official API contract, and reputable current Clash Royale references. A field's
   presence is not proof of its meaning; connect events, modes, badges, and progress
   only when the payload or authoritative semantics establishes the relationship.
4. Characterize each candidate with counts, first/last observation, affected entities,
   raw examples, and downstream consumers. Distinguish new from merely rare.
5. Ask what useful data Elixir captures but does not yet interpret, especially special
   events and shifts in where the clan is actually playing. At least monthly, perform a
   small captured-but-unused data inventory: trace each credible candidate from receipt
   through events, projections, and capability consumers, then either prove it is not
   useful or correct the missing source representation. Do not create an idea backlog.
6. Inspect open `objective:game` issues for multi-run context.
7. For every active natural-acceptance watch, run its named read-only check and close
   it on the stated evidence. For label watches use
   `scripts/check_natural_label_acceptance.py` with the deployment timestamp, exact
   label, and expiry; an expired no-mention watch is a healthy no-op, not permission to
   manufacture a post.

## Action

When the evidence establishes a source defect or missing internal representation,
acquire the `game` checkout lease and correct the ingress, materialization, capability,
tests, or reference documentation in the same run. Rehearse schema changes on a copy,
never the live database. Run the full gates before committing and pushing.

Ask Jamie before a change alters what members see or creates a new product behavior.
Present the smallest useful version as one yes/no decision, not a Data-to-Product-to-
Build issue chain.

## Success

- structural API and game changes are understood within a day;
- raw data, normalized events, projections, and capabilities agree;
- special events and new modes are recognized from actual participation;
- factual source defects are fixed before they become confident bad advice;
- steady data produces a concise healthy no-op, not a daily pile of findings.
