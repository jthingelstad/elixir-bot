# Understand Clash Royale

Your objective is: **Elixir understands where Clash Royale is going, what is
happening now, what is coming next, and what it means for POAP KINGS.**

Actively learn from the web: product direction, new cards and modes, balance and
progression changes, CRL and other tournaments, the competitive meta, and player
reactions. Discover developments before they appear in the clan's API payloads.
Keep a forward view as well as an accurate account of today's game.

You also own the meaning flowing from Supercell's API through raw receipts,
events, projections, rollups, capabilities, and the game reference. Use that
evidence to connect the wider game to the clan and correct source defects. A
database audit alone does not fulfill this objective.

Read `AGENTS.md`, `AGENT-TEAM/WORKFLOW.md`, `AGENT-TEAM/README.md`, and this file.
Use the project `cr-api-doc-audit` skill for payload/reference drift and
`awareness-report` when checking whether available game data reaches Elixir's
decisions.

Cadence: daily, plus after a material release, competition milestone, or drift alert.

## Every run

1. Run the shared preflight and read the previous game outlook and upcoming dates
   in automation memory. A checkout mutation gate does not prevent web research.
2. **Browse current external sources every run, before the detailed database audit.**
   Run `uv run --locked python AGENT-TEAM/scripts/external_game_pulse.py` for the
   reviewed source manifest and local meta context, then actually search and open
   current web pages. The helper does not browse; a fresh local snapshot does not
   establish that the external game is understood.
   - Read official news, release notes, and announced event calendars. Look for new
     modes, cards, Heroes/Evolutions, balance, progression, rewards, and experiments.
   - Check CRL's current rulebook and official event/broadcast channels: qualifiers,
     results, qualification stakes, World Finals, dates, and how to watch. Include
     other significant tournaments and in-game leagues. Never wait for clan
     participation to establish that an announced competition matters.
   - Read a current competitive aggregate or specialist analysis (RoyaleAPI or
     SQURS), including its date, game mode, population, and sample where available.
     Look for shifts in decks, card use, strategy, participation, and game direction.
     A preview article is reporting, not measured meta evidence.
   - Take a small manual sample of current community/creator discussion. Treat
     recurring excitement, confusion, and frustration as questions to investigate;
     a few comments do not establish population sentiment.
   Open primary sources behind secondary reports. Check publication/update dates
   separately from event dates and distinguish announced, live, completed, and
   unconfirmed. Follow revised schedules rather than repeating cached snippets.
   If a source is inaccessible, use another credible route and state the gap.
3. Synthesize a short game outlook: **what changed, what is next, why it matters,
   and what remains uncertain**. Keep the next 30 days plus the next major CRL
   milestone in `Current state`, with source links, dates/timezones, last checked
   time, and a next review date. Recheck an approaching event each run and replace
   passed dates with verified results or remove them. Preserve this outlook when
   recording an engineering fix; an old "pulse reviewed" timestamp is insufficient.
   Separate official facts, specialist reporting, dated aggregate evidence, and
   community hypotheses. No change after a real web check is a valid result.
4. Inspect the sole ingress and retained evidence, guided by the external outlook:
   - successful endpoint receipts and `raw_api_payloads`;
   - recent `api_sentinel_observations` for structural paths, progress keys, modes,
     cards, and other new enum values;
   - the four event streams and their current projections;
   - materialization readiness, freshness, and distribution shifts.
   - `uv run --locked python scripts/audit_game_mode_labels.py --hours 48` for fresh
     battle-mode sentinels. A mode is safe only when its display label is curated or an
     explicitly approved generic fallback; otherwise trace its event context before
     changing member-visible wording.
5. Connect credible external developments to the existing game reference and
   capability consumers. State whether Elixir can already represent and retrieve
   the fact, needs a source correction, or lacks evidence. External announcements
   can be useful without an API representation; missing clan participation is not
   a reason to discard them. A field's presence is not proof of meaning: connect
   events, modes, badges, and progress only when evidence establishes the link.
   Do not scrape, retain identities or raw comments, refresh the meta database
   automatically, or change member-facing behavior from this pulse.
6. Characterize internal data candidates with counts, first/last observation, affected entities,
   raw examples, and downstream consumers. Distinguish new from merely rare.
7. Ask what useful data Elixir captures but does not yet interpret, especially special
   events and shifts in where the clan is actually playing. At least monthly, perform a
   small captured-but-unused data inventory: trace each credible candidate from receipt
   through events, projections, and capability consumers, then either prove it is not
   useful or correct the missing source representation. Do not create an idea backlog.
8. Inspect open `objective:game` issues for multi-run context.
9. For every active natural-acceptance watch, run its named read-only check and close
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

- each run can explain the current external game outlook from freshly opened sources;
- upcoming CRL milestones, tournaments, modes, and releases are known before they arrive;
- meaningful market and player signals are interpreted with clear evidence and uncertainty;
- structural API and game changes are understood within a day;
- raw data, normalized events, projections, and capabilities agree;
- special events and new modes are recognized from actual participation;
- factual source defects are fixed before they become confident bad advice;
- steady data produces a concise healthy no-op, not a daily pile of findings.

Use the common closeout contract. Lead `Evidence` with the meaningful external
development and next dated event, then any internal finding. Report code changes
when needed, but do not make commits or database counts the measure of success.
