# Event Core v5 — Architecture Boundary Decision

**Status:** Decided 2026-06-21. Supersedes the "full cutover" framing in
`event-core-v5-cutover-runbook.md` Part B.

## TL;DR

Event sourcing is the right tool for the **inference + reactive-communication**
core (the "Mind"), and that part is live and working. It is the *wrong* tool for
the **rich operational/analytics read model** (decks, league stats, path-of-legend,
progress, war intra-day detail). Forcing everything through the event store — the
"full cutover" — would re-event bulky snapshot data only to reproject it into the
same shape, with no inference benefit, a history discontinuity, and ~50 reader
rewrites. **We are not doing that.**

The Part A consolidation already delivered the real goal: one operational DB
(`elixir-v5.db`), with the event store and durable memory as separate files, and
`elixir.db` retired. The v4 tables now live as a plain operational read model
*inside* that one DB. That is the intended end state.

## What earns its keep (keep, it's load-bearing)

- **Event store** (`elixir-v5-events.db`) — the audit/inference substrate.
- **The Mind** — `detections` / recommendations / `CommunicationIntent`, driving
  the reactive, agent-voiced posting pipeline. This is *new capability* v4 never
  had; it is the v5 thesis and it works in production.
- **`battle_telemetry`** — retention-managed telemetry the battle detectors scan.
- **`detections` projection** — read by `CohortWaveDetector`.
- **Durable memory** (`elixir-v5-memory.db`) — `clan_memories*`, its own store.

## What is redundant (maintained but unread)

The Observed-World **current-state projections** are written by `advance()` every
reactive tick but have **no live readers**:
`player_current_profile`, `member_current_state_proj`, `player_current_collections`,
`clan_daily_metrics_proj`, `war_current_state_proj`, `war_participation_proj`,
`roster_lifecycle`.

Confirmed 2026-06-21:
- No module outside `event_core/` reads any of these (grep across
  `runtime/ agent/ storage/ heartbeat/ modules/`).
- `event_core/read/tools.py` (the v5 read API: `get_player_current`, …) has **zero**
  live consumers.
- The live agent's tool surface (`agent/tool_exec.py`) reads v4 `storage/*`
  exclusively (7 storage refs, 0 event_core refs).

They were built to *prove* exact parity vs legacy during the migration. They did
their job. They are now scalar subsets that duplicate the v4 tables without
replacing them — the dual-write overhead (CPU + storage + two code paths) is the
real architectural smell, not the event store itself.

## Why "full cutover" is the wrong goal

The v5 event model captures **only scalar** profile + roster fields
(`PROFILE_SCALAR_FIELDS`, `ROSTER_FIELDS` in `event_core/domain/player.py`). It does
**not** event: `current_deck`/support, favourite card, `league_statistics`,
path-of-legend results, `progress_json`, `previous_clan_rank`, `source`/`raw_json`,
nor war intra-day participants, deck composition, or rich battle detail. The live
agent read tools, card/war analytics, and tournament enrichment all need that rich
data, and it lives only in the v4 snapshot tables.

To drop those v4 tables we would have to: add new event capture for all of the
above → reproject → backfill (history capped at the 14-day raw-payload window, so a
discontinuity) → rewrite ~50 read sites including a pervasive `member_id`→`player_tag`
key change → parity-validate each → retire writers. That is a multi-week ingest-model
rebuild whose output is largely the same snapshot tables we started with. Poor
cost/benefit; not pursued.

## The boundary (the rule going forward)

- **Event-source** the facts that drive inference, plus the entire Mind. New
  observations that *trigger Elixir to act* belong in the event stream.
- **Keep a conventional materialized read model** (the v4 `storage/*` tables) for
  rich operational/analytics queries and the agent's read tools. It does not need
  to be event-sourced. It lives in the same `elixir-v5.db`.
- When adding a new signal Elixir should *react* to → add an event + detector.
  When adding data Elixir only needs to *read/display* → extend the operational
  read model directly. Don't route read-only data through the event store.

## Follow-ups (optional, not required for a coherent end state)

1. **Retire the redundant current-state projections** to remove the dual-write:
   stop running them in `event_core/live/engine.advance()` and drop the seven
   tables. Keep `detections` + `battle_telemetry` (those are consumed). This is the
   highest-value cleanup — it removes overhead without touching any reader.
   Defer the `*_proj` parity harness to an offline validation script if still wanted.
2. **Drop the genuinely-dead awareness tables** (`signal_detector_cursors`,
   `signal_outcomes`, `awareness_ticks`) — written only by the disabled awareness
   jobs; first guard the few admin/status readers against their absence.
3. Leave all other v4 operational tables in place — they are the read model.

Neither follow-up is needed for correctness; the system is coherent as-is. They
only reduce overhead.
