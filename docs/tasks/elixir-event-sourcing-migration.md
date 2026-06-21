# Plan: Elixir Event-Sourcing Migration

Status: Design plan. Tracking issue: #95.

Captured: 2026-06-21.

This plan supersedes the earlier "stream as observation substrate" framing in
`docs/tasks/elixir-stream-redesign-direction.md` and nests the implemented
stream work into a fuller event-sourcing architecture. The prior work was a
necessary step: battles and selected detector signals now land in
`game_event_stream`, and Situation can read stream summaries. The next target is
larger: make Elixir's event log the authoritative record of observed state
changes, derived observations, Elixir decisions, and communication history.

## Purpose

Elixir should be able to ask and answer questions like:

- What changed for this player over the last 7, 28, 56, or 90 days?
- Which players got the same achievement or badge this week?
- Which cards are moving across the clan?
- Which members are active in Ranked / Path of Legends, 2v2, special events,
  Trophy Road, and River Race?
- Which state changes caused a streak, a promotion candidate, a demotion
  candidate, a kick review, or a Discord post?
- What did Elixir choose not to say, and why?

The current model can answer some of this from a mix of fact tables, signal
events, rollups, messages, leader-action tables, and awareness telemetry. That
mix is useful, but not coherent. The same fact can be represented in several
places, and many ordinary state changes are not events at all unless a detector
decides they are notable.

The event-sourcing target is:

```
External snapshots / commands
  -> observed domain events
  -> projections and aggregators
  -> derived events
  -> commentary candidates and intents
  -> delivery events
```

In this model, "signals" are no longer the primary unit of awareness. They become
derived stream events or commentary candidates emitted by aggregators.

## Core Decision

Elixir's event log is the authoritative internal history of Elixir's observed
world.

The Clash Royale API remains the authoritative upstream source for current game
state. It is not Elixir's event store. It is an external snapshot source that
Elixir samples, diffs, and converts into observed events.

Plain-language distinction:

- **CR API snapshot**: what Supercell currently says is true.
- **Observed event**: what Elixir observed changed, when it observed it, and what
  upstream evidence caused that observation.
- **Projection**: Elixir's query-optimized view derived from events.
- **Derived event**: an event emitted by an aggregator after reading other
  events or projections.
- **Intent**: Elixir's planned communication or deliberate silence.
- **Delivery event**: what happened when Elixir attempted a side effect.

## Architectural Gap

Today `game_event_stream` is mostly:

- one row per observed battle (`event_class='battle'`)
- one row per selected detector signal (`event_class='signal'`)

That is not yet event sourcing. It is a hybrid observation ledger.

Missing pieces:

1. Ordinary player state changes are not always events.
   A card can move from level 12 to 13 without an event because Elixir only emits
   `card_level_milestone` when a configured threshold is crossed.

2. Signals combine detection, derivation, and commentary eligibility.
   A streak is currently a signal. In the target model it is a derived event,
   caused by battle events, that may or may not become a commentary candidate.

3. Projection ownership is unclear.
   Tables like `member_current_state`, card snapshots, war tables, daily metrics,
   decision cases, leader-action cards, and messages are currently partial
   ledgers. In the target model, they are projections or downstream records.

4. Silence is not fully evented.
   Elixir can skip a post or decide something is quiet, but that decision is not
   consistently represented as an intent/event with causal links.

5. Filters and cohort views are bolted on after the fact.
   The stream needs first-class dimensions for player, card, badge, achievement,
   battle mode, war season, lane, audience, and source.

## Target Architecture

### 1. Event Store

Append-only durable event log. This can evolve from `game_event_stream` or move
to a new table such as `domain_events`. The important change is conceptual:
events become the write model, not a sidecar.

Required properties:

- Append-only: no updating payloads in place.
- Globally ordered: every event has a monotonically increasing global position.
- Idempotent: repeated ingest of the same upstream observation produces the same
  event key and no duplicate event.
- Causally linked: derived events point to the base events that caused them.
- Scoped: public, leadership, and system-internal data are separated structurally.
- Versioned: event schemas have versions and can be upcast.
- Queryable by dimensions without unpacking every payload.

### 2. Snapshot Ingest

Snapshot ingest reads external state and emits base events.

For example:

```
CR player profile snapshot
  -> player_profile_observed
  -> player_name_changed
  -> player_trophies_changed
  -> player_best_trophies_changed
  -> ranked_league_changed
  -> card_level_changed
  -> badge_level_changed
  -> achievement_stars_changed
```

Battle-log ingest emits:

```
battle_played
```

Roster ingest emits:

```
member_joined
member_left
member_role_changed
member_donations_changed
member_war_preference_changed
```

War ingest emits:

```
war_season_observed
war_period_started
war_period_completed
war_rank_changed
war_member_decks_used_changed
war_member_fame_changed
```

### 3. Projections

Projection tables are deterministic read models derived from the event log.

Current tables that should become projections:

| Projection | Purpose |
|---|---|
| `members`, `member_current_state` | latest roster identity and current member state |
| `player_profile_snapshots` or successor | latest and historical sampled profile views |
| card collection tables | current card levels/evolutions plus history |
| badge / achievement views | current badge and achievement state |
| `member_battle_facts` | battle-detail read model and battle query accelerator |
| `member_daily_metrics` | daily player summaries |
| war tables | war-season and war-period read models |
| `event_rollups` | durable summaries beyond the high-fidelity retention window |
| `decision_cases` | current unresolved concern state |
| `communication_intents` | current communication decision state |
| `messages` | delivered communication read model |

Some existing tables may remain as source snapshots during migration. The end
state should make their role explicit: either upstream sample archive or
projection from events.

### 4. Aggregators

Aggregators consume events/projections and emit derived events.

Examples:

| Aggregator | Consumes | Emits |
|---|---|---|
| battle streak detector | `battle_played` | `battle_hot_streak_detected`, `battle_slump_detected` |
| ranked pulse detector | ranked `battle_played`, `ranked_league_changed` | `ranked_activity_surge_detected`, `ranked_climb_detected` |
| cohort badge detector | `badge_earned`, `badge_level_changed` | `cohort_badge_wave_detected` |
| card movement detector | `card_unlocked`, `card_level_changed`, `card_evolution_changed` | `card_upgrade_wave_detected`, `new_champion_wave_detected` |
| roster health detector | roster/player/war events | `inactive_member_risk_detected`, `promotion_candidate_detected`, `demotion_candidate_detected` |
| war momentum detector | war period/member events | `war_momentum_shift_detected`, `war_recovery_needed_detected` |
| commentary planner | derived events and projections | `commentary_candidate_created`, `commentary_suppressed` |

The key rule: aggregators do not directly post. They emit events.

### 5. Situation Builder

Situation becomes a stream query and projection assembly layer.

It should read:

- recent base events by lane/audience/window
- recent derived events by priority
- cohort clusters by dimension
- due cases
- due revisits represented as case reminders or intent reminders
- recent commentary intents and delivery outcomes
- projection snapshots for compact context

It should not depend on ephemeral signal batches as its main model. A current
tick can still pass newly emitted events into Situation, but the agent should
understand them as "new event positions since last run," not as special signal
objects.

### 6. Commentary and Delivery

Communication should also be evented.

Target flow:

```
derived event or event cluster
  -> commentary_candidate_created
  -> communication_intent_created
  -> communication_intent_selected | communication_intent_suppressed
  -> delivery_attempted
  -> delivery_succeeded | delivery_failed
  -> message_recorded
```

This makes silence auditable. If Elixir sees 10 players earn the same badge and
chooses not to post, that decision should be queryable.

## Event Model

Minimum event columns:

| Column | Purpose |
|---|---|
| `event_id` | local row id |
| `global_position` | ordered application sequence |
| `event_key` | deterministic idempotency key |
| `event_type` | domain event type |
| `event_family` | player, card, badge, achievement, battle, war, roster, commentary, delivery, system |
| `event_class` | base, derived, intent, delivery, system |
| `schema_version` | payload version |
| `source_system` | player_intel, clan_awareness, war_awareness, manual, system, discord, site |
| `source_detector` | emitter/aggregator name |
| `occurred_at` | when the underlying thing happened, if known |
| `observed_at` | when Elixir observed it |
| `recorded_at` | when Elixir appended it |
| `scope` | public, leadership, system_internal |
| `subject_type` | member, clan, card, badge, achievement, battle_mode, war, case, intent, system |
| `subject_key` | primary subject key |
| `actor_type` | optional actor, usually member or elixir |
| `actor_key` | player tag, Discord user, Elixir component |
| `clan_tag` | clan dimension |
| `player_tag` | denormalized member dimension |
| `card_key` | denormalized card dimension |
| `badge_key` | denormalized badge dimension |
| `achievement_key` | denormalized achievement dimension |
| `game_mode` | ladder, ranked, two_v_two, special_event, tournament, friendly, war, other |
| `battle_type` | Clash Royale battle type when relevant |
| `season_id` | war/ranked/game season dimension |
| `war_week` | war week/section dimension |
| `correlation_id` | groups events from one ingest run or decision chain |
| `causation_id` | immediate parent event |
| `caused_by_event_ids_json` | derived-event evidence list |
| `payload_json` | compact event payload |
| `payload_hash` | integrity/dedupe support |

Indexes should support:

- global position scans
- event type + time
- scope + time
- player + time
- card + time
- badge + time
- achievement + time
- game mode + time
- season/week + time
- event family/class + time
- correlation/causation lookup

## Event Taxonomy

### Player and Roster Base Events

- `player_profile_observed`
- `player_name_changed`
- `player_experience_changed`
- `player_level_changed`
- `player_trophies_changed`
- `player_best_trophies_changed`
- `player_wins_changed`
- `player_losses_changed`
- `player_battle_count_changed`
- `player_donations_changed`
- `player_war_day_wins_changed`
- `player_challenge_best_changed`
- `member_joined`
- `member_left`
- `member_role_changed`
- `member_active_again_observed`
- `member_inactivity_observed`

### Ranked / Path of Legends Base Events

- `ranked_season_result_observed`
- `ranked_league_changed`
- `ranked_trophies_changed`
- `ranked_global_rank_changed`
- `ultimate_champion_status_changed`

### Card Base Events

- `card_collection_observed`
- `card_unlocked`
- `card_level_changed`
- `card_level_milestone_crossed`
- `card_evolution_changed`
- `card_current_deck_changed`
- `card_usage_observed`

`card_level_milestone_crossed` can be either a base event emitted during diffing
or a derived event from `card_level_changed`. Prefer derived if we want every
ordinary card change recorded first.

### Badge and Achievement Base Events

- `badge_collection_observed`
- `badge_earned`
- `badge_level_changed`
- `achievement_observed`
- `achievement_stars_changed`
- `achievement_completed`

### Battle Base Events

- `battle_played`
- `battle_deck_observed`
- `battle_opponent_observed`

`battle_played` remains high-volume and must not be inserted raw into prompts.
It feeds projections, rollups, and aggregators.

### War Base Events

- `war_state_observed`
- `war_season_started`
- `war_season_completed`
- `war_week_started`
- `war_week_completed`
- `war_period_started`
- `war_period_completed`
- `war_rank_changed`
- `war_fame_changed`
- `war_member_participation_changed`
- `war_member_decks_used_changed`
- `war_rival_activity_changed`

### Derived Events

- `battle_hot_streak_detected`
- `battle_slump_detected`
- `battle_trophy_push_detected`
- `ranked_activity_surge_detected`
- `ranked_climb_detected`
- `cohort_badge_wave_detected`
- `cohort_achievement_wave_detected`
- `card_upgrade_wave_detected`
- `new_card_unlock_wave_detected`
- `promotion_candidate_detected`
- `demotion_candidate_detected`
- `inactive_member_risk_detected`
- `war_momentum_shift_detected`
- `war_recovery_needed_detected`
- `clan_record_detected`
- `season_award_granted`

### Elixir Operational Events

- `decision_case_opened`
- `decision_case_refreshed`
- `decision_case_deferred`
- `decision_case_resolved`
- `leader_action_card_created`
- `leader_action_decided`
- `commentary_candidate_created`
- `commentary_suppressed`
- `communication_intent_created`
- `communication_intent_selected`
- `communication_intent_suppressed`
- `delivery_attempted`
- `delivery_succeeded`
- `delivery_failed`
- `message_recorded`
- `site_publish_attempted`
- `site_publish_succeeded`
- `site_publish_failed`
- `runtime_job_started`
- `runtime_job_succeeded`
- `runtime_job_failed`
- `api_schema_shape_observed`
- `api_schema_sentinel_emitted`

## Filtered Views and Aggregates

The event model should make these views straightforward.

### Player Timeline

Filter:

- `player_tag = ?`
- `event_family IN (...)`
- time window

Answers:

- player changed name
- player climbed in ranked
- player upgraded cards
- player earned badges
- player battled in each mode
- Elixir opened/resolved cases about player
- Elixir posted or suppressed commentary about player

### Achievement / Badge Cohort View

Filter:

- `achievement_key = ?` or `badge_key = ?`
- event type `achievement_stars_changed`, `achievement_completed`,
  `badge_earned`, `badge_level_changed`
- time window

Answers:

- 10 players got the same achievement
- 4 players leveled the same mastery badge
- a seasonal event badge is spreading through the clan

### Card Movement View

Filter:

- `card_key = ?`
- event type `card_unlocked`, `card_level_changed`,
  `card_evolution_changed`

Answers:

- which members unlocked a new champion
- which cards are becoming level 16
- which evolutions are appearing
- whether card movement correlates with battle-mode success

### Battle-Mode View

Filter:

- `game_mode = ranked | ladder | two_v_two | special_event | war`
- event type `battle_played` plus derived battle events

Answers:

- who is grinding Path of Legends this week
- which players are driving 2v2 activity
- whether a special event has unusual participation
- whether ranked activity is broad or isolated to one player

### War Season View

Filter:

- `season_id = ?`
- war event families

Answers:

- week-by-week rank/fame trajectory
- member participation deltas
- rival movement
- war awards and leader actions caused by war evidence

### Commentary Audit View

Filter:

- `event_class IN ('intent', 'delivery')`
- optional player/card/badge/mode dimensions

Answers:

- what Elixir saw
- why Elixir posted
- why Elixir stayed quiet
- which event cluster caused a post
- whether delivery succeeded

## Relationship to the `eventsourcing` Library

The event-sourcing architecture should align with the concepts in the Python
`eventsourcing` project:

- aggregate sequences
- application-wide notification log
- projections/materialized views
- process applications
- tracking records for reliable consumers
- versioned/upcastable events

Reference:

- https://eventsourcing.readthedocs.io/en/stable/

Do not start by forcing the whole bot into the library. Elixir already has a
large SQLite schema and many runtime paths. The better sequence is:

1. adopt the event-sourcing model and event taxonomy in Elixir-native tables
2. implement one or two consumers with explicit tracking
3. evaluate whether the library should own the event store / notification log /
   process-application mechanics

If adopted, the likely fit is a bounded event-store package rather than a full
rewrite of bot runtime modules.

## Migration Plan

### Phase 0: Inventory and Invariants

Goal: freeze the conceptual model before schema changes.

Tasks:

- Inventory current fact tables, signal types, delivery ledgers, and runtime jobs.
- Mark every table as one of:
  - upstream sample archive
  - event store
  - projection
  - derived state
  - delivery side effect
  - compatibility table
- Define event naming rules.
- Define dimension keys and canonical IDs for players, cards, badges,
  achievements, battle modes, war seasons, cases, and intents.
- Define scope rules for public/leadership/system events.
- Define retention policy for base events and rollups.

Exit criteria:

- event taxonomy reviewed
- table ownership matrix written
- no schema/code migration started without ownership labels

### Phase 1: Event Store Schema V2

Goal: make the event log capable of being the durable backbone.

Tasks:

- Add missing columns or create `domain_events`.
- Add global sequence/position.
- Add schema version.
- Add denormalized dimension columns.
- Add causation/correlation fields.
- Add consumer tracking table.
- Add event schema registry in code.
- Add append helper that enforces:
  - deterministic `event_key`
  - append-only writes
  - scope validation
  - schema version validation
  - compact payload size limits

Exit criteria:

- can append base, derived, intent, and delivery events
- can scan by global position
- can query by player/card/badge/achievement/game mode
- existing `game_event_stream` readers still work or have a compatibility view

### Phase 2: Base Events From Player Profile Diffs

Goal: ordinary player state changes become events.

Tasks:

- Refactor `snapshot_player_profile()` so its diff engine emits base events.
- Emit every meaningful profile/card/badge/achievement/ranked delta, not only
  post-worthy milestones.
- Keep current signal outputs as compatibility derived events during the phase.
- Update projections from emitted events.
- Record source snapshot hash and correlation ID per ingest run.

Important rule:

Do not post from base events directly. Posting still goes through existing
awareness until derived-event/commentary phases are ready.

Exit criteria:

- card level 12 -> 13 is queryable as `card_level_changed`
- card level 15 -> 16 is queryable as `card_level_changed` and can derive
  `card_level_milestone_crossed`
- badge and achievement changes are queryable even when not posted
- Path of Legends league changes are first-class base events

### Phase 3: Base Events From Roster, War, Awards, Manual Observations

Goal: all primary non-battle observations enter the event store.

Tasks:

- Emit roster membership and role events.
- Emit donation and activity deltas where useful.
- Emit war season/week/period/rank/participation events.
- Emit award-granted events.
- Emit manual screenshot/clan-voyage observations as events.
- Emit API sentinel findings as events before notification.

Exit criteria:

- leader-action scan can point to event evidence
- war Situation can be rebuilt from war projections derived from events
- API schema sentinel is both an event and a communication candidate

### Phase 4: Projection Writers

Goal: make current tables deterministic projections of events.

Tasks:

- Build consumer tracking table:
  - `consumer_name`
  - `last_global_position`
  - `updated_at`
  - status/error fields
- Move projection updates behind replayable handlers.
- Start with low-risk projections:
  - per-player current state
  - per-card current state
  - per-badge current state
  - per-mode battle summary
- Add replay tooling against a DB copy.
- Keep current write paths in shadow until replayed projections match existing
  tables within defined tolerances.

Exit criteria:

- projections can be rebuilt from event log on a copy
- projection consumers are idempotent
- projection lag is visible in admin/status output

### Phase 5: Derived Event Aggregators

Goal: replace signal-first detection with stream aggregators.

Tasks:

- Implement aggregators as consumers with tracking.
- Port current signal detectors into derived-event emitters:
  - battle hot streak
  - battle trophy push
  - Path of Legends promotion/demotion/global rank
  - card milestones
  - badge milestones
  - achievement milestones
  - promotion/demotion/kick candidates
  - war momentum and war recovery signals
- Add new aggregators:
  - ranked activity surge
  - cohort badge wave
  - cohort achievement wave
  - card upgrade wave
  - event-mode participation surge

Exit criteria:

- current signal outcomes have derived-event equivalents
- derived events include `caused_by_event_ids_json`
- aggregators can be replayed without duplicate derived events
- old signal functions are compatibility wrappers, not the primary model

### Phase 6: Cases and Leader Actions as Evented Projections

Goal: decision cases become projections/state machines driven by events.

Tasks:

- Open/refresh cases from derived events.
- Emit `decision_case_*` events for lifecycle changes.
- Make leader-action cards projections of cases.
- Emit `leader_action_card_created` and `leader_action_decided`.
- Remove recompute-first leader-action behavior once parity is proven.

Exit criteria:

- a promotion/demotion/kick card can trace to base events and derived events
- deferred cases resurface because their case state says they are due
- leader decision history is replayable/auditable

### Phase 7: Commentary Candidates and Intent Events

Goal: commentary planning becomes evented and silence is auditable.

Tasks:

- Convert derived events into `commentary_candidate_created`.
- Add priority/novelty/audience policy.
- Emit `commentary_suppressed` when Elixir intentionally stays quiet.
- Create `communication_intent_created` from selected candidates.
- Store coverage links from intent to candidate/events.
- Keep Discord delivery side effects only after intent is durable.

Exit criteria:

- every proactive post has an intent event
- every deliberate skip has a suppression event
- quiet ticks can be audited by event position/time window

### Phase 8: Situation V2

Goal: Situation is built from event positions, projections, cases, and intents.

Tasks:

- Replace signal-batch-first Situation fields with stream-native fields:
  - `new_events_since_last_awareness`
  - `event_clusters`
  - `derived_events_by_priority`
  - `commentary_candidates`
  - `due_cases`
  - `recent_intents`
  - `projection_snapshots`
- Keep payload compaction strict.
- Keep battle-grain rows out of prompts except as aggregates or drilldowns.
- Add lane/audience-specific filters before prompt assembly.

Exit criteria:

- Elixir can notice a cohort story without a bespoke signal
- Elixir can reason about ranked, cards, badges, achievements, and war through
  the same event model
- public Situation cannot include leadership-only events by construction

### Phase 9: Backfill and Replay

Goal: make history useful without corrupting operational truth.

Backfill sources:

- `member_battle_facts`
- player profile snapshots
- card collection snapshots
- member daily metrics
- war tables
- `signal_log`
- `signal_outcomes`
- `communication_intents`
- `messages`
- leader action recommendations
- decision cases

Rules:

- Backfilled events use `source_system='backfill'`.
- Backfilled `observed_at` is original observation time when known.
- Backfilled `recorded_at` is backfill time.
- Never infer more precision than the source supports.
- Keep backfill scripts idempotent.
- Run on a DB copy first.

Exit criteria:

- at least 90 days of core player/battle/war/card/badge history is available
  where source tables support it
- backfilled projections match current tables within documented tolerances

### Phase 10: Decommission Compatibility Layers

Goal: remove duplicate ledgers only after parity.

Candidates:

- `signal_log` becomes compatibility-only or is replaced by evented completion
  markers.
- `signal_outcomes` collapses under communication-intent/delivery events.
- signal dicts become transient DTOs or disappear.
- direct delivery paths are retired or wrapped in intent events.
- old project tables remain dormant or are dropped via dedicated FK-safe
  migration if no longer referenced.

Exit criteria:

- no production path relies on signal grain as the authoritative observation
- all proactive communications trace to event/candidate/intent/delivery chain
- admin/debug tooling reads event store and projections

## Verification Strategy

Unit tests:

- event key determinism
- schema validation
- scope validation
- upcasting
- projection replay
- aggregator replay
- consumer tracking

Integration tests:

- player profile diff emits expected base events
- card upgrade emits ordinary change plus milestone-derived event
- badge cohort emits wave event
- ranked battles emit ranked aggregate event
- leader-action case traces to base events
- communication suppression is recorded

Operational checks:

- event append rate by family
- projection lag by consumer
- failed consumer count
- event payload size distribution
- scope leakage audit
- commentary coverage audit
- DB growth and retention behavior

Production rollout:

1. shadow write events
2. compare projections against existing tables
3. enable derived-event aggregators in shadow
4. compare derived events against current signals
5. enable commentary candidates in shadow
6. switch Situation to stream-native read path
7. retire old paths only after several clean weekly cycles

## Guardrails

- No raw battle flood in prompts.
- No leadership data in public Situation.
- No Discord/site side effect before durable intent.
- No destructive schema migration until projections can replay.
- No derived event without causal evidence.
- No commentary candidate without audience/scope.
- No backfill event that pretends to have real-time precision it does not have.
- No dependency adoption that forces a bot-wide rewrite before the model is
  proven.

## Open Questions

1. Should the existing `game_event_stream` be evolved, or should a new
   `domain_events` table become the event store with a compatibility view?
2. How long should full-fidelity non-battle base events be retained?
3. Which event families should survive indefinitely via rollups?
4. Should Elixir store upstream raw snapshot hashes for every profile sample?
5. Should commentary candidates be generated synchronously during ingest or by a
   scheduled consumer?
6. Should the Python `eventsourcing` library own the event store after the first
   native phase, or remain only an architectural reference?

## Definition of Done

This migration is complete when:

- every meaningful observed player, card, badge, achievement, roster, battle,
  ranked, war, case, intent, delivery, and system change is represented as an
  event or as a projection from events
- existing current-state tables are documented as projections or upstream sample
  archives
- derived observations such as streaks are event-store consumers, not special
  signal objects
- Situation is assembled from stream positions, event clusters, projections,
  cases, and intents
- Elixir can filter and aggregate by player, event type, card, achievement,
  badge, battle mode, war season, audience, and commentary lane
- every proactive post and every deliberate silence has an auditable event chain
