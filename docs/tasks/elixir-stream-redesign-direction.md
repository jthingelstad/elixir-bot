# Elixir Stream Redesign — Confirmed Direction

Captured 2026-06-20, following `docs/tasks/elixir-data-flow-gap-assessment.md`.
This is the north star the phased work builds toward. Decisions here were made
with the clan owner and supersede the generic "projects" framing in
`docs/tasks/internal-data-subsystem-pivot.md`.

## Core decisions

### 1. The event stream is the observation substrate — at BATTLE grain, across ALL game modes

- Every battle is projected into `game_event_stream`, one event per battle,
  idempotent. This covers **all modes**: Trophy Road (ladder), **Path of Legends
  (ranked)**, River Race (war), **2v2 (TeamVsTeam)**, special events, tournaments.
- `game_mode` — from the existing canonical `storage/game_modes.classify_battle_mode()`
  — is a **first-class, queryable dimension** so Elixir can make *mode-specific*
  observations. Today Elixir is effectively Trophy-Road-only and ignores PoL, 2v2,
  and events even though the data is already in `member_battle_facts`
  (PoL ≈ 1,368 battles, 2v2 ≈ 1,072, events ≈ 3,744 in the current DB). This is the
  core fix.
- **Merge Tactics** and other non-battlelog side modes are not in the battlelog;
  they surface via player `progress` keys (`classify_progress_key`) and are tracked
  separately, not as battle events.
- Battle events are **telemetry-grade** (~350–450/day, ~30–40k rows at 90-day
  retention). They are **not** injected raw into prompts — they feed rollups and
  on-demand queries. The existing low-volume signal events remain the prompt-facing
  layer.

### 2. "Projects" is retired — the war season is window + rollups + awards

The generic `elixir_projects` abstraction was confusing and redundant. The war
season decomposes into three things that already exist or are already specced:

- **Season window** (a concrete fact): `season_id` + its ~5-week section bounds,
  which Elixir explicitly knows, so it reasons *across the whole season* rather than
  a single day/week/snapshot.
- **Season / period performance**: `event_rollups` (`war_cycle`, per-mode like
  `ranked_season`, `member_90d`). Already specced, currently **0 rows**. This becomes
  the "clan performance across the season" layer.
- **Season recognition**: the existing `awards` system (season/section-scoped,
  idempotent, derived from facts). Awards and rollups are siblings — both are
  period-scoped aggregations of the stream; awards are the *discrete honors*, rollups
  are the *continuous picture*.

`elixir_projects` and its four singletons are dropped:
`clan_development` / `onboarding` / `recruitment` content becomes open
`decision_cases` + simple queries; `war_season` becomes the season window +
`war_cycle` rollup.

### 3. `decision_cases` is the single home for a "concern"

(Carried from the gap assessment.) Memories = narrative annotation; revisits = case
reminders; leader-action cards = case projections; the leader scan becomes
case-first. Not part of Phase 1, but the model the later phases target.

## Phase plan (revised)

- **Phase 1 — Battle-grain stream + game modes (shadow). [IN PROGRESS]**
  - Additive migration: `game_event_stream` gains `game_mode` + `event_class`
    columns and supporting indexes.
  - `snapshot_player_battlelog` projects each inserted battle into the stream
    (`event_class='battle'`, classified `game_mode`, `subject_type='member'`,
    `season_id`, `occurred_at=battle_time`, compact payload).
  - Backfill the existing `member_battle_facts` rows (idempotent via the existing
    battle dedupe tuple).
  - Guard: existing signal-grain consumers (Situation `recent_events`,
    `summarize_events_by_window`) default to `event_class='signal'` so battle
    telemetry does not bloat prompts — **no posting-behavior change in this phase.**
  - Tests: idempotent insertion, mode classification on events, backfill, and
    Situation unchanged.

- **Phase 2 — Season window + per-mode consumption. [SHIPPED 2a]** A concrete
  season-window helper (`get_season_window`: bounds + week-by-week rank/fame
  trajectory from war tables) and a live per-mode battle summary
  (`summarize_battle_modes`: Trophy Road / Path of Legends / 2v2 / events / war /
  tournaments with W-L, win rate, and top members) — both computed from the stream
  and wired into Situation (`mode_pulse`, `season_window`) and the awareness prompt,
  so Elixir can comment on Path of Legends grinds, 2v2 streaks, and whole-season
  arcs. Consumption is computed live (always fresh) rather than pre-stored.
  - **Follow-on (2b):** durable `event_rollups` persistence (history beyond the
    90-day stream, per-mode + member-level) and recap/daily-insight consumption.

- **Phase 3 — Cases as the concern spine; case-first leader scan. [SHIPPED 3a]**
  The leader-action scan now posts from due decision cases — the candidate
  recompute is just the sensor that opens/refreshes cases. A deferred case
  re-surfaces when its `due_at` passes even without a fresh detector flag
  (fixing the inverted north-star, where overdue deferrals were silently
  auto-dismissed); an open case the detector no longer flags is left in Situation
  rather than carded with stale evidence. Card dedupe (168h) prevents re-post spam.
  - **[SHIPPED 3b]** `record_leadership_followup` now always opens a durable
    decision case (a member-review type when `case_type` is set — card-eligible —
    otherwise a generic topic-keyed `leadership_followup` case) with the memory as
    its annotation. `flag_member_watch` stays memory-only unless `case_type` makes
    it action-oriented. A leadership concern now has a single home.
- **Phase 4 — Delivery on `communication_intents` (incl. skip intents).**
- **Phase 5 — Retire `elixir_projects`, dead paths; docs.**

## Guardrails

- Migrations stay additive; no destruction of existing runtime tables until the new
  model has run in shadow.
- Battle events must never bloat awareness prompts (`event_class` guard).
- Backfill is idempotent (`event_key` derived from the existing battle dedupe tuple),
  so it is safe to validate on a DB copy and then re-run on the live DB after more
  battles have accumulated.
