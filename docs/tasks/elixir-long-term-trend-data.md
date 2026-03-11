# Elixir Long-Term Trend Data

## Overview

Build a durable long-term trend data layer for Elixir so the bot can:

1. generate trustworthy player and clan charts
2. answer longer-term performance questions with real time-series data
3. distinguish short-term noise from real trends
4. reason about performance, activity, resets, and churn with better context

Elixir already stores:

- current member state
- per-member daily state snapshots
- player profile snapshots
- raw battle facts

This task formalizes those into a chart-ready and prompt-ready trend subsystem with explicit daily rollups, clan-level rollups, and completeness tracking.

Canonical reporting day for this subsystem: **America/Chicago**

Raw event timestamps should still remain in UTC where they already exist.

---

## Objectives

Implement a trend data subsystem that can:

1. store daily player snapshots keyed by Chicago calendar day
2. store daily clan snapshots keyed by Chicago calendar day
3. aggregate daily battle activity by player and by clan
4. group battle activity by stable mode families and preserve raw mode detail
5. track wins, losses, draws, trophy change, and activity volume over time
6. expose completeness/confidence signals so charts and Elixir do not overclaim
7. support later chart generation and LLM trend analysis without reprocessing raw history every time

---

## Design Principles

### 1. Canonical day semantics must be consistent

All trend tables introduced by this task should use a canonical reporting date derived from **America/Chicago**.

Requirements:

- trend rows must be keyed by Chicago day, not UTC day
- raw timestamps should still be stored in UTC when available
- all rollups must document that the reporting day is Chicago-local
- do not store per-member localized day buckets in the primary schema

Rationale:

- clan-level reporting must stay comparable
- member-localized days create ambiguous rollups and difficult charts
- localized views can be derived later if member timezones ever become available

### 2. Snapshot metrics and activity metrics must be separated

These are different types of data:

- snapshot metrics: member trophies, clan score, member count
- flow/activity metrics: battles, wins, losses, trophy change over a day

Requirements:

- snapshot data and activity rollups must live in separate tables
- clan and member rollups must also remain separate

### 3. Completeness must be explicit

Battle activity data is only trustworthy if Elixir knows whether battle ingestion likely captured the day well enough.

Requirements:

- daily battle rollups must include completeness metadata
- battle completeness should compare ingested battle facts against available profile counters where possible
- charts and LLM summaries should be able to exclude or hedge incomplete days

### 4. Official and unofficial clan metrics may coexist if labeled clearly

Examples:

- official-ish or API-native metrics:
  - `clan_score`
  - `clan_war_trophies`
  - `required_trophies`
  - member count
- useful derived metrics:
  - `total_member_trophies`
  - `avg_member_trophies`
  - `top_member_trophies`

Requirements:

- derived metrics are allowed
- derived metrics must be stored with clear naming and documented semantics

### 5. Churn must be modeled directly

Clan membership changes materially affect aggregate charts and LLM interpretation.

Requirements:

- daily clan trend data must include joins, leaves, and net membership change
- Elixir should be able to separate performance shifts from roster churn

---

## Existing Foundation

The current repo already provides useful building blocks:

- `member_daily_metrics` in [db/__init__.py](/Users/jamie/Projects/elixir-bot/db/__init__.py#L608)
- daily member snapshot writes in [storage/roster.py](/Users/jamie/Projects/elixir-bot/storage/roster.py#L103)
- raw battle fact storage in [db/__init__.py](/Users/jamie/Projects/elixir-bot/db/__init__.py#L679)
- battle ingest and mode classification in [storage/player.py](/Users/jamie/Projects/elixir-bot/storage/player.py#L182)

This task should extend that foundation instead of replacing it.

---

## Scope of Work

Implement a long-term trend subsystem with:

- Chicago-day utilities
- expanded daily member metrics semantics
- new daily clan metrics
- new daily member battle rollups
- new daily clan battle rollups
- completeness accounting
- query helpers for trend analysis and chart inputs
- tests
- implementation notes

This task is design plus implementation planning, with the expectation that code work follows in phases.

---

## Data Model Requirements

### 1. Keep and standardize `member_daily_metrics`

Existing table:

- `member_daily_metrics`

Requirements:

- treat this as the canonical daily player snapshot table
- ensure its `metric_date` is derived from Chicago-local day
- document clearly that it is a Chicago-day snapshot

Expected stored fields already include:

- `member_id`
- `metric_date`
- `exp_level`
- `trophies`
- `best_trophies`
- `clan_rank`
- `donations_week`
- `donations_received_week`
- `last_seen_api`

Potential additive fields to consider later:

- `pol_league`
- `pol_trophies`
- `battle_count_total`
- `wins_total`
- `losses_total`

These should be additive only if they can be populated reliably from player profile snapshots.

### 2. Add `clan_daily_metrics`

Create a new table for daily clan-wide snapshot metrics.

Suggested fields:

- `metric_date` — Chicago date, primary key with clan tag
- `clan_tag`
- `clan_name`
- `member_count`
- `open_slots`
- `clan_score`
- `clan_war_trophies`
- `required_trophies`
- `total_member_trophies`
- `avg_member_trophies`
- `top_member_trophies`
- `weekly_donations_total`
- `joins_today`
- `leaves_today`
- `net_member_change`
- `observed_at_utc`
- `raw_json`

Rules:

- snapshot once per reporting day
- update in place if re-run on the same day
- derived values should be computed from the current active roster and clan API snapshot

### 3. Add `member_daily_battle_rollups`

Create a player-level daily battle aggregate table.

Primary key:

- `member_id`
- `battle_date`
- `mode_group`
- `game_mode_id` nullable

Suggested fields:

- `member_id`
- `battle_date` — Chicago date
- `mode_group`
- `game_mode_id`
- `game_mode_name`
- `battles`
- `wins`
- `losses`
- `draws`
- `crowns_for`
- `crowns_against`
- `trophy_change_total`
- `first_battle_at_utc`
- `last_battle_at_utc`
- `captured_battles`
- `expected_battle_delta` nullable
- `completeness_ratio` nullable
- `is_complete`
- `last_aggregated_at`

Rules:

- roll up from `member_battle_facts`
- preserve both stable grouping and raw mode detail
- if `game_mode_id` is unavailable, still roll up by `mode_group`

### 4. Add `clan_daily_battle_rollups`

Create a clan-level daily battle aggregate table derived from member daily battle rollups.

Primary key:

- `battle_date`
- `mode_group`
- `game_mode_id` nullable

Suggested fields:

- `battle_date`
- `clan_tag`
- `mode_group`
- `game_mode_id`
- `game_mode_name`
- `members_active`
- `battles`
- `wins`
- `losses`
- `draws`
- `crowns_for`
- `crowns_against`
- `trophy_change_total`
- `captured_battles`
- `expected_battle_delta` nullable
- `completeness_ratio` nullable
- `is_complete`
- `last_aggregated_at`

### 5. Mode grouping contract

Daily battle rollups should store a stable mode family in addition to raw mode metadata.

Suggested mode groups:

- `ladder`
- `ranked`
- `war`
- `special_event`
- `friendly`
- `other`

Requirements:

- grouping logic must be centralized in code
- the mapping must be deterministic
- raw `game_mode_id` and `game_mode_name` should still be preserved

---

## Completeness Requirements

Completeness is a first-class feature, not a nice-to-have.

### Member-level completeness

For each member/day bucket:

- compare new `member_battle_facts` rows captured for the day
- compare with `battleCount` deltas from the nearest `player_profile_snapshots` before/after that period when possible

Suggested derived values:

- `captured_battles`
- `expected_battle_delta`
- `completeness_ratio`
- `is_complete`

Rules:

- if expected count is unknown, leave ratio null and mark completeness conservatively
- if captured count is materially below expected delta, mark incomplete
- if expected count matches captured count closely enough, mark complete

### Clan-level completeness

Clan completeness should be derived from the member-level rollups for that day and mode group.

Possible aggregation rules:

- complete if all contributing member rollups are complete
- or complete if weighted clan completeness ratio exceeds a threshold

The implementation should document the exact rule and keep it deterministic.

---

## Reset and Season-Awareness Requirements

The system must support trend interpretation in the presence of ladder and season resets.

Requirements:

- raw daily values should still be stored honestly
- query helpers should support current-season views and fixed-window views separately
- chart and analysis helpers should be able to annotate reset boundaries

Examples:

- current trophies can drop because of reset, not bad performance
- Path of Legend league/trophy changes should be interpreted within season context

This task does not require full chart rendering, but it must leave the data model ready for reset-aware chart queries.

---

## Churn Requirements

Daily clan trend data should explicitly account for roster movement.

At minimum:

- `member_count`
- `joins_today`
- `leaves_today`
- `net_member_change`

Future-friendly additions to consider:

- `member_days_active`
- `new_member_count_7d`
- `departed_member_count_7d`

These are important for both chart interpretation and Elixir sentiment.

---

## Query Layer Requirements

Add V2 query helpers for:

### Player trends

- daily trophy history for a member
- daily battle activity for a member by mode group
- daily win/loss history for a member
- moving-window summaries like 7d and 30d

### Clan trends

- daily member count history
- daily clan score history
- daily total member trophies history
- daily clan battle activity by mode group
- daily clan win/loss summaries by mode group

### Comparison and sentiment helpers

- compare last 7 days vs previous 7 days
- compare current season vs previous season where applicable
- identify improving, flat, and declining members or clan periods
- detect whether a shift is likely driven by churn vs performance

These helpers should be designed for both charts and LLM prompts.

---

## Write Path Requirements

### 1. Daily member snapshot path

Update the existing member daily snapshot write path to derive `metric_date` from Chicago time rather than UTC string slicing.

### 2. Daily clan snapshot path

Add a write path that captures one daily clan snapshot during the normal clan refresh flow.

Candidate places:

- heartbeat-driven clan refresh
- site-content refresh
- a dedicated daily trend snapshot routine

The write path must be idempotent for a given Chicago day.

### 3. Daily battle rollup path

Rollups should be built from `member_battle_facts`, not directly from live API payloads.

Recommended write strategy:

- insert/update battle facts first
- identify affected `(member, Chicago day)` buckets
- recompute only affected rollups
- then recompute affected clan daily rollups

This keeps writes incremental and deterministic.

---

## Suggested Modules / Deliverables

Adapt to existing structure, but aim for:

- `storage/trends.py`
  - daily clan snapshot writes
  - member daily battle rollups
  - clan daily battle rollups
  - trend query helpers
- additive migrations in `db/__init__.py`
- helper utilities for Chicago-day calculation
- tests in:
  - `tests/test_db_v2.py`
  - new focused trend test file if needed
- `IMPLEMENTATION_NOTES.md` updates if the implementation introduces operational tradeoffs

---

## Migration Requirements

Add additive migrations for:

1. `clan_daily_metrics`
2. `member_daily_battle_rollups`
3. `clan_daily_battle_rollups`
4. required indexes for date, member, clan tag, mode group, and completeness queries

Potential indexes:

- `clan_daily_metrics(metric_date DESC)`
- `member_daily_battle_rollups(member_id, battle_date DESC, mode_group)`
- `clan_daily_battle_rollups(battle_date DESC, mode_group)`
- optional `game_mode_id` indexes if raw mode charts are expected

Do not reset or replace existing V2 tables for this work.

---

## Implementation Phases

### Phase 1: Time semantics and clan daily snapshots

Deliver:

- Chicago-day utility helper
- `member_daily_metrics` write path corrected to Chicago-day semantics
- `clan_daily_metrics` table and write path
- tests for day bucketing and idempotent daily writes

### Phase 2: Member daily battle rollups

Deliver:

- `member_daily_battle_rollups` table
- mode-group classification helper
- rollup computation from `member_battle_facts`
- wins/losses/draws/trophy-change aggregates
- completeness fields
- tests

### Phase 3: Clan daily battle rollups

Deliver:

- `clan_daily_battle_rollups` table
- clan aggregation from member daily rollups
- completeness aggregation rule
- tests

### Phase 4: Query helpers and prompt-ready summaries

Deliver:

- member trend queries
- clan trend queries
- simple comparison helpers for 7d and 30d views
- prompt-ready trend summary packaging
- tests

### Phase 5: Operational rollout and notes

Deliver:

- implementation notes
- any needed backfill or one-off rebuild command
- manual verification checklist

---

## Backfill Strategy

Backfill is useful but should be treated separately from the core schema/workflow work.

Recommended approach:

1. ship schema and live write paths first
2. add a rebuild/backfill command that derives rollups from existing `member_battle_facts` and historical member snapshots
3. mark historical completeness conservatively where profile-counter comparison is unavailable

Important:

- backfill should be idempotent
- backfill should not be required for the live system to function going forward

---

## Testing Requirements

Write tests covering at minimum:

### Chicago-day semantics

- timestamps around UTC midnight bucket into the correct Chicago date
- daily upserts do not create duplicate rows for the same Chicago day

### Clan daily metrics

- member count and open slots are correct
- derived trophy totals and averages are correct
- joins/leaves/net change are correct

### Member daily battle rollups

- battles are grouped by Chicago day correctly
- wins/losses/draws aggregate correctly
- trophy change totals aggregate correctly
- mode grouping is deterministic

### Clan daily battle rollups

- clan totals equal the sum of member daily rollups
- completeness aggregation behaves deterministically

### Query helpers

- member history queries return date-ordered rows
- clan history queries return date-ordered rows
- comparison helpers distinguish current vs previous windows correctly

---

## Acceptance Criteria

The task is complete only if all of the following are true:

1. Long-term trend tables use Chicago-day semantics consistently.
2. Existing member daily metrics are written using Chicago-day logic.
3. A new `clan_daily_metrics` table exists and is populated idempotently.
4. A new `member_daily_battle_rollups` table exists and tracks battles, wins, losses, draws, and trophy change by day and mode.
5. A new `clan_daily_battle_rollups` table exists and aggregates member daily battle rollups.
6. Rollups preserve both stable `mode_group` and raw mode identity where available.
7. Completeness metadata exists so incomplete battle-capture days can be identified.
8. Clan daily trend data includes member count and churn metrics.
9. Query helpers exist for member and clan trend history.
10. Tests exist for time semantics, rollups, completeness, and query behavior.

---

## Rollout Guidance

Recommended order:

1. add Chicago-day helper and standardize member daily writes
2. add `clan_daily_metrics`
3. add member battle rollups
4. add clan battle rollups
5. add query helpers
6. add optional backfill tooling

Do not start with charts.

First make the trend layer trustworthy, queryable, and testable. Charts and richer LLM use should come after the data semantics are stable.

