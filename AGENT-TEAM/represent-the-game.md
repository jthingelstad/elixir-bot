# Represent the Game

Your objective is: **Elixir's own model of Clash Royale is current, complete, and
accurate — from Supercell's payloads through receipts, events, projections,
rollups, and capabilities.**

External game intelligence — product direction, releases, CRL, the competitive
meta, community sentiment — is **not yours**. It belongs to the domain objective
`Understand Clash Royale` in `../AGENT-TEAM/understand-clash-royale.md`, whose
findings land in `~/Projects/clash-royale/cr-agent-api-docs`. You consume that
outlook; you do not reproduce it. If it is stale or missing, say so and continue
with the evidence you have rather than browsing in its place.

Read `AGENTS.md`, `AGENT-TEAM/WORKFLOW.md`, `AGENT-TEAM/README.md`, and this file.
Use `.claude/skills/cr-api-doc-audit` for payload/reference drift and
`awareness-report` when checking whether available game data reaches Elixir's
decisions. A database audit alone does fulfil this objective; discovering the
outside world does not.

Cadence: daily, plus after a drift alert or a domain outlook naming a change that
Elixir must be able to represent.

## Every run

1. Run the shared preflight. Read `Current state` and the domain game outlook. The
   outlook tells you what Elixir *should* be able to represent soon; it is an input,
   not a finding of yours.
2. Inspect the sole ingress and retained evidence:
   - successful endpoint receipts and `raw_api_payloads`;
   - recent `api_sentinel_observations` for structural paths, progress keys, modes,
     cards, and other new enum values;
   - the four event streams and their current projections;
   - materialization readiness, freshness, and distribution shifts;
   - `uv run --locked python scripts/audit_game_mode_labels.py --hours 48` for fresh
     battle-mode sentinels. A mode is safe only when its display label is curated or
     an explicitly approved generic fallback; otherwise trace its event context
     before changing member-visible wording.
3. Characterize each candidate with counts, first and last observation, affected
   entities, raw examples, and downstream consumers. Distinguish new from merely
   rare. A field's presence is not proof of meaning: connect events, modes, badges,
   and progress only when evidence establishes the link.
4. Take the domain outlook's named developments one at a time and answer a single
   question for each: **can Elixir already represent and retrieve this fact?** State
   whether it can, needs a source correction, or lacks evidence. An external
   announcement can be real and useful while Elixir has no representation for it —
   that is a finding, not a contradiction. Missing clan participation is never a
   reason to discard it.
5. Ask what useful data Elixir captures but does not yet interpret, especially
   special events and shifts in where the clan is actually playing. At least
   monthly, perform a small captured-but-unused data inventory: trace each credible
   candidate from receipt through events, projections, and capability consumers,
   then either prove it is not useful or correct the missing source representation.
   Do not create an idea backlog.
6. Inspect open `objective:game` issues for multi-run context.
7. For every active natural-acceptance watch, run its named read-only check and
   close it on the stated evidence. For label watches use
   `scripts/check_natural_label_acceptance.py` with the deployment timestamp, exact
   label, and expiry; an expired no-mention watch is a healthy no-op, not permission
   to manufacture a post.

## Boundaries

Do not scrape, retain identities or raw comments, or change member-facing behavior
from an audit. Clan-specific interpretation stays here; anything true for *any* CR
caller belongs in the shared reference, which the domain objective owns — send it
there rather than writing a second copy.

## Action

When the evidence establishes a source defect or missing internal representation,
acquire the `game` checkout lease and correct the ingress, materialization,
capability, tests, or in-repo documentation in the same run. Rehearse schema
changes on a copy, never the live database. Run the full gates before committing
and pushing.

Ask Jamie before a change alters what members see or creates a new product
behavior. Present the smallest useful version as one yes/no decision.

## Success

- structural API changes are represented within a day of being observed;
- raw data, normalized events, projections, and capabilities agree;
- special events and new modes are recognized from actual participation;
- every development the domain outlook names has a stated representation verdict;
- factual source defects are fixed before they become confident bad advice;
- steady data produces a concise healthy no-op, not a daily pile of findings.

Use the common closeout contract. Lead `Evidence` with the representation verdicts,
then any internal finding. Report code changes when needed, but do not make commits
or database counts the measure of success.
