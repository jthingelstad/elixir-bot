# Plan: Elixir Event Core Rewrite

Status: Design plan. Original tracking issue: #95. Boundary refinement: #97.

Captured: 2026-06-21.

This plan supersedes the earlier "stream as observation substrate" framing in
`docs/tasks/elixir-stream-redesign-direction.md` and nests the implemented
stream work into a fuller event-sourcing architecture. The prior work was a
necessary step: battles and selected detector signals now land in
`game_event_stream`, and Situation can read stream summaries.

The next target is larger: rewrite Elixir's core data substrate so the event log
is the authoritative record of observed state changes, derived observations,
leadership recommendations, and case decisions. This is a core data rewrite, not
a bot rewrite. Runtime surfaces, prompt workflows, and external side-effect
layers should consume projections from the Event Core; they should not define
the Event Core's schema.

## Purpose

Elixir should be able to ask and answer questions like:

- What changed for this player over the last 7, 28, 56, or 90 days?
- Which players got the same achievement or badge this week?
- Which cards are moving across the clan?
- Which members are active in Ranked / Path of Legends, 2v2, special events,
  Trophy Road, and River Race?
- Which state changes caused a streak, a promotion candidate, a demotion
  candidate, or a kick review?
- Which leadership recommendations were generated, refreshed, suppressed, or
  resolved, and what evidence caused them?

The current model can answer some of this from a mix of fact tables, signal
events, rollups, decision cases, leader-action side tables, and awareness
telemetry. That mix is useful, but not coherent. The same fact can be
represented in several places, and many ordinary state changes are not events at
all unless a detector decides they are notable.

The event-sourcing target is:

```
External snapshots / commands
  -> observed domain events
  -> projections and aggregators
  -> derived events
  -> leadership recommendation events
  -> case projections and decision events
```

In this model, "signals" are no longer the primary unit of awareness. They become
derived stream events or recommendation events emitted by aggregators.

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
- **Recommendation event**: a leadership-private derived event that says Elixir
  recommends or suppresses action for a member, with evidence links and policy
  version.
- **Case projection**: the current lifecycle state of a recommendation or
  concern, derived from recommendation and case-decision events.

## Core Boundary

The Event Core is a data model. It should not contain presentation logic.

Do not put these concepts in the authoritative event stream:

- external surface routing identifiers
- rendering component identifiers, layouts, action states, or final copy
- outbound side-effect receipt identifiers or formatting
- external publishing formatting details
- presentation-specific routing policy

Those belong in downstream read models that consume Event Core projections. The
Event Core may record that a leadership recommendation was generated,
suppressed, accepted, rejected, deferred, or resolved. It should not record the
form used to surface that recommendation.

## Architectural Gap

Today `game_event_stream` is mostly:

- one row per observed battle (`event_class='battle'`)
- one row per selected detector signal (`event_class='signal'`)

That is not yet event sourcing. It is a hybrid observation ledger.

Missing pieces:

1. Ordinary player state changes are not always events.
   A card can move from level 12 to 13 without an event because Elixir only emits
   `card_level_milestone` when a configured threshold is crossed.

2. Signals combine detection, derivation, and actionability policy.
   A streak is currently a signal. In the target model it is a derived event,
   caused by battle events, that may or may not feed a recommendation, case, or
   other downstream policy decision.

3. Projection ownership is unclear.
   Tables like `member_current_state`, card snapshots, war tables, daily metrics,
   decision cases, and leader-action side tables are currently partial ledgers.
   In the target model, they are projections or downstream side-effect records.

4. Leadership recommendations are a side system.
   Promotion, demotion, and kick recommendations are currently produced by a
   recompute-first scan. They should instead be generated by stream consumers and
   inserted into the same event store as leadership-private recommendation
   events.

5. Filters and cohort views are bolted on after the fact.
   The stream needs first-class dimensions for player, card, badge, achievement,
   battle mode, war season, local date, scope, and source.

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

Some existing tables may remain as source snapshots during migration. The end
state should make their role explicit: either upstream sample archive or
projection from events.

External side-effect tables are outside the Event Core. They may consume Event
Core projections, but they are not authoritative sources for player, roster,
recommendation, or case state.

### 4. Aggregators

Aggregators consume events/projections and emit derived events.

Examples:

| Aggregator | Consumes | Emits |
|---|---|---|
| battle streak detector | `battle_played` | `battle_hot_streak_detected`, `battle_slump_detected` |
| ranked pulse detector | ranked `battle_played`, `ranked_league_changed` | `ranked_activity_surge_detected`, `ranked_climb_detected` |
| cohort badge detector | `badge_earned`, `badge_level_changed` | `cohort_badge_wave_detected` |
| card movement detector | `card_unlocked`, `card_level_changed`, `card_evolution_changed` | `card_upgrade_wave_detected`, `new_champion_wave_detected` |
| roster health detector | roster/player/war events | `inactive_member_risk_detected` |
| leadership recommendation generator | roster/player/war/ranked/case events | `promotion_candidate_detected`, `demotion_candidate_detected`, `kick_candidate_detected`, `leadership_recommendation_suppressed` |
| war momentum detector | war period/member events | `war_momentum_shift_detected`, `war_recovery_needed_detected` |

The key rule: aggregators do not directly post. They emit events.

Leadership recommendation generators are aggregators. They consume the same event
stream as every other generator, emit leadership-scoped derived events, and link
each recommendation to its evidence events. They are not a separate side system.

### 5. Situation Builder

Situation becomes a stream query and projection assembly layer.

It should read:

- recent base events by scope/window
- recent derived events by priority
- cohort clusters by dimension
- due cases
- due revisits represented as case reminders
- recent recommendation and case events
- projection snapshots for compact context

It should not depend on ephemeral signal batches as its main model. A current
tick can still pass newly emitted events into Situation, but the agent should
understand them as "new event positions since last run," not as special signal
objects.

### 6. Leadership Recommendations and Cases

Leadership recommendations are part of the Event Core.

Target flow:

```
base and derived events
  -> promotion_candidate_detected | demotion_candidate_detected | kick_candidate_detected
  -> leadership_recommendation_refreshed | leadership_recommendation_suppressed
  -> decision_case_opened | decision_case_refreshed
  -> decision_case_deferred | decision_case_accepted | decision_case_rejected | decision_case_resolved
  -> recommendation_outcome_observed
```

The case table is a projection of these events. It answers "what is currently
open, due, deferred, resolved, or measured?" The event store answers "what
happened, what policy generated it, and what evidence caused it?"

Recommendation events should carry:

- `player_tag`
- `recommendation_type`: promotion, demotion, kick, watch, no_action
- `reason_codes`: inactivity, low war participation, elder readiness,
  sustained contribution, role mismatch, etc.
- `policy_version`
- `confidence` or `severity`
- `scope='leadership'`
- `caused_by_event_ids_json`

External consumers can project these cases into whatever surface they own. That
projection must not leak surface-specific concepts back into the Event Core.

## Event Model

Minimum event columns:

| Column | Purpose |
|---|---|
| `event_id` | local row id |
| `global_position` | ordered application sequence |
| `event_key` | deterministic idempotency key |
| `event_type` | domain event type |
| `event_family` | player, card, badge, achievement, battle, war, roster, recommendation, case, system |
| `event_class` | base, derived, recommendation, case, system |
| `schema_version` | payload version |
| `source_system` | player_intel, clan_awareness, war_awareness, recommendation_generator, case_manager, manual, system |
| `source_detector` | emitter/aggregator name |
| `occurred_at` | when the underlying thing happened, if known |
| `observed_at` | when Elixir observed it |
| `recorded_at` | when Elixir appended it |
| `local_date` | America/Chicago date for day-level filters |
| `scope` | public, leadership, system_internal |
| `subject_type` | member, clan, card, badge, achievement, battle_mode, war, case, recommendation, system |
| `subject_key` | primary subject key |
| `actor_type` | optional actor, usually member or elixir |
| `actor_key` | player tag, stable human identifier, or Elixir component |
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
- local date + event family
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
- `inactive_member_risk_detected`
- `war_momentum_shift_detected`
- `war_recovery_needed_detected`
- `clan_record_detected`
- `season_award_granted`

### Leadership Recommendation Events

- `promotion_candidate_detected`
- `demotion_candidate_detected`
- `kick_candidate_detected`
- `leadership_recommendation_refreshed`
- `leadership_recommendation_suppressed`
- `leadership_recommendation_expired`
- `recommendation_outcome_observed`

### Case Decision Events

- `decision_case_opened`
- `decision_case_refreshed`
- `decision_case_deferred`
- `decision_case_accepted`
- `decision_case_rejected`
- `decision_case_resolved`

### System Observation Events

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
- Elixir generated, suppressed, or resolved leadership recommendations about
  player

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
- war awards and leadership recommendations caused by war evidence

### Leadership Recommendation and Case Audit View

Filter:

- `event_family IN ('recommendation', 'case')`
- optional player/card/badge/mode dimensions

Answers:

- what Elixir saw
- why Elixir recommended promotion, demotion, kick, watch, or no action
- why Elixir suppressed a recommendation
- which event cluster caused a case to open or refresh
- how the case was accepted, rejected, deferred, resolved, or measured

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

If adopted, the likely fit is a bounded Event Core package rather than a full
rewrite of bot runtime modules.

## Migration Plan

### Phase 0: Inventory and Invariants

Goal: freeze the conceptual model before schema changes.

Tasks:

- Inventory current fact tables, signal types, recommendation/case tables,
  side-effect ledgers, and runtime jobs.
- Mark every table as one of:
  - upstream sample archive
  - event store
  - projection
  - derived state
  - external side-effect outside the Event Core
  - compatibility table
- Define event naming rules.
- Define dimension keys and canonical IDs for players, cards, badges,
  achievements, battle modes, war seasons, recommendations, and cases.
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

- can append base, derived, recommendation, case, and system events
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

Do not trigger side effects from base events directly. Existing awareness paths
remain compatibility consumers until derived-event and recommendation phases are
ready.

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

- leadership recommendation generators can point to event evidence
- war Situation can be rebuilt from war projections derived from events
- API schema sentinel is an event before any external consumer decides how to
  handle it

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

### Phase 6: Leadership Recommendations and Cases

Goal: promotion, demotion, and kick recommendations become Event Core outputs,
and decision cases become projections/state machines driven by those events.

Tasks:

- Generate leadership-scoped recommendation events from base and derived events.
- Open/refresh cases from recommendation events.
- Emit `decision_case_*` events for lifecycle changes.
- Emit `leadership_recommendation_suppressed` when evidence does not clear the
  action threshold.
- Remove recompute-first leadership recommendation behavior once parity is proven.

Exit criteria:

- a promotion/demotion/kick recommendation can trace to base events and derived
  events
- deferred cases resurface because their case state says they are due
- leader decision history is replayable/auditable

### Phase 7: Recommendation Policy and Suppression

Goal: recommendation policy decisions are evented and auditable without encoding
side-effect behavior.

Tasks:

- Version leadership recommendation policies.
- Record recommendation threshold decisions with evidence and reason codes.
- Emit `leadership_recommendation_suppressed` for no-action decisions worth
  auditing.
- Keep external side effects outside the Event Core.

Exit criteria:

- every recommendation has a policy version and evidence events
- every audited suppression has reason codes
- no presentation-specific fields are required to explain a recommendation

### Phase 8: Situation V2

Goal: Situation is built from event positions, projections, recommendation
events, and cases.

Tasks:

- Replace signal-batch-first Situation fields with stream-native fields:
  - `new_events_since_last_awareness`
  - `event_clusters`
  - `derived_events_by_priority`
  - `recommendation_events`
  - `due_cases`
  - `projection_snapshots`
- Keep payload compaction strict.
- Keep battle-grain rows out of prompts except as aggregates or drilldowns.
- Add scope-specific filters before prompt assembly.

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
- `signal_outcomes` stays an external side-effect compatibility ledger outside
  the Event Core until side-effect handling is redesigned.
- signal dicts become transient DTOs or disappear.
- direct side-effect paths consume projections rather than detector signals.
- old project tables remain dormant or are dropped via dedicated FK-safe
  migration if no longer referenced.

Exit criteria:

- no production path relies on signal grain as the authoritative observation
- all leadership recommendations trace to base events, derived events, and case
  events
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
- recommendation suppression is recorded

Operational checks:

- event append rate by family
- projection lag by consumer
- failed consumer count
- event payload size distribution
- scope leakage audit
- recommendation coverage audit
- DB growth and retention behavior

Production rollout:

1. shadow write events
2. compare projections against existing tables
3. enable derived-event aggregators in shadow
4. compare derived events against current signals
5. enable recommendation generators in shadow
6. switch Situation to stream-native read path
7. retire old paths only after several clean weekly cycles

## Guardrails

- No raw battle flood in prompts.
- No leadership data in public Situation.
- No presentation-specific fields in the Event Core.
- No destructive schema migration until projections can replay.
- No derived event without causal evidence.
- No recommendation event without scope, policy version, and evidence.
- No backfill event that pretends to have real-time precision it does not have.
- No dependency adoption that forces a bot-wide rewrite before the model is
  proven.

## Open Questions

1. Should the existing `game_event_stream` be evolved, or should a new
   `domain_events` table become the event store with a compatibility view?
2. How long should full-fidelity non-battle base events be retained?
3. Which event families should survive indefinitely via rollups?
4. Should Elixir store upstream raw snapshot hashes for every profile sample?
5. Should leadership recommendation generators run synchronously during ingest
   or by scheduled consumers?
6. Should the Python `eventsourcing` library own the event store after the first
   native phase, or remain only an architectural reference?

## Definition of Done

This migration is complete when:

- every meaningful observed player, Clash Royale card, badge, achievement,
  roster, battle, ranked, war, leadership recommendation, case, and system
  change is represented as an event or as a projection from events
- existing current-state tables are documented as projections or upstream sample
  archives
- derived observations such as streaks are event-store consumers, not special
  signal objects
- promotion, demotion, and kick recommendations are generated by Event Core
  consumers, not a side system
- Situation is assembled from stream positions, event clusters, projections,
  recommendations, and cases
- Elixir can filter and aggregate by player, event type, card, achievement,
  badge, battle mode, war season, scope, recommendation type, and case state
