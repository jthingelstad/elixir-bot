# Plan: Elixir Event Core (v5) — Event-Sourced, Reactive Architecture

Status: Design plan. Original tracking issue: #95. Boundary refinement: #97.

Captured: 2026-06-21. Revised: 2026-06-21 (library adoption, reactive v5 framing,
two-context aggregate model, UTC-only, single-campaign migration).

This plan supersedes the "stream as observation substrate" framing in
`docs/tasks/elixir-stream-redesign-direction.md`. The prior work was a necessary
step — battles and selected detector signals now land in `game_event_stream`, and
Situation can read stream summaries. This plan nests that work into a fuller
event-sourcing architecture and changes the runtime model along with the data
model.

The target is not "add an event log next to the current system." It is to make
the event log the **authoritative internal record** of everything Elixir
observes, infers, and decides, and to make Elixir **react to events** instead of
waking on a schedule and guessing what mattered. This is a core rewrite of
Elixir's data and control substrate. It is done once, as a whole, on an isolated
copy, and cut over atomically. There is no second consumer of this system, so
there is **no backward-compatibility layer**.

---

## 1. The v5 Thesis: from scheduled awareness to event-driven action

Today Elixir wakes on a timer, assembles a large "Situation" blob of everything
it might care about this tick, and asks an agent to find something worth saying.
The schedule drives the bot; the data is a passive lookup.

v5 inverts this. **Events drive the bot.** When ingest observes that a player did
something, that observation becomes an event. When an aggregator notices a
pattern (a hot streak, a badge wave, a promotion-worthy contribution), it emits a
derived event. The *arrival of a noteworthy event* is what triggers Elixir to
act — to post about a player's achievement, to open a leadership case, to flag a
risk. Elixir becomes reactive to what the clan is **doing**, not to the clock.

Two trigger sources remain, in deliberate proportion:

- **Reactive (primary).** A process application follows the event log. When a
  derived or recommendation event of a noteworthy class lands, that event — with
  its own evidence links — is the trigger. This is the engine.
- **Cadence (secondary, small).** A short list of genuinely periodic reflections
  ("comment on clan activity in the last 24 hours") stays time-triggered, but
  these **query projections**; they do not assemble signal batches. This is the
  exception, not the model.

Consequences for the old design:

- **"Situation" as a pre-computed per-tick blob is retired.** It was an artifact
  of schedule-first operation. In v5 the triggering event already knows what
  mattered. What survives is a thin, on-demand **context query service** over
  projections, called *by a trigger* when Elixir decides to act and needs
  surrounding detail to write well. There is no "Situation V2."
- Signals stop being the primary unit of awareness. They become derived events
  emitted by aggregators.

---

## 2. Core Decisions (firm)

These are settled. They close several previously-open questions.

1. **The event log is Elixir's authoritative internal history.** The Clash Royale
   API is the authoritative *upstream* source of current game state. It is not
   Elixir's event store. It is an external snapshot source Elixir samples, diffs,
   and converts into observed events.

2. **Adopt the Python `eventsourcing` library with its SQLite backend.** We are
   committing to the full model and to growing use of it; the library gives us the
   exact machinery this design leans on (see §4). We build native domain
   aggregates *on* the library, not a hand-rolled event store.

3. **Two bounded contexts** (see §5): the **Observed World** (game-domain
   observation aggregates, written only by ingest) and **Elixir's Mind** (Elixir's
   inferences and decisions, written only by stream followers). Member and
   WarSeason live in the World; Recommendation and DecisionCase live in the Mind.
   The Mind reads the World; the World never reads the Mind.

4. **UTC everywhere in the data layer.** Events, payloads, and projections store
   time in UTC (CR API time is already Zulu). No `America/Chicago` anywhere in the
   event core. Timezone conversion happens only at presentation/rollup read time,
   in exactly one isolated place (see §7).

5. **Single-campaign rewrite, no backward compatibility.** The whole model is
   built on an isolated copy of the repo and database, validated by replay and a
   one-time parity check, then cut over atomically. No compatibility consumers, no
   shim functions, no dual-running. Backward compatibility is dead weight when
   there is one user and the upstream truth (CR API) is always re-derivable.

---

## 3. Core Boundary

The Event Core is a data and reasoning model. It contains no presentation logic.

Never put in the event store:

- external surface routing identifiers
- rendering component identifiers, layouts, action states, or final copy
- outbound side-effect receipt identifiers or formatting
- external publishing formatting details
- presentation-specific routing policy

Those belong in downstream side-effect surfaces that **consume** Event Core
projections. The core may record that Elixir *decided to communicate* something
(a communication-intent event, see §8) or that a recommendation was generated,
suppressed, accepted, rejected, deferred, or resolved. It must not record the
form used to surface any of it.

Directional dependency, enforced by construction:

```
CR API (upstream truth)
  -> Observed World aggregates        (ingest writes)
  -> Elixir's Mind aggregates         (followers write, reading the World)
  -> side-effect surfaces             (read projections of the Mind; write nothing back)
```

---

## 4. Architecture on the `eventsourcing` library

The library is aggregate-centric: an **aggregate** is a consistency boundary with
its own event-sequenced history keyed by a stable ID, reconstructed by replaying
its events. This is a different shape from the old "one wide event table" plan,
and the difference matters (see §4.2).

### 4.1 What the library gives us

The three properties that become load-bearing once compatibility shims are gone
are provided directly:

- **Gap-free global ordering** → the **notification log**
  (`app.notification_log.select(start=n, limit=m)`). This *is* the
  `global_position` the old plan was going to hand-roll, correct under SQLite
  locking instead of a hand-built sequence table.
- **Reliable consumers** → **process applications** (`Follower`) maintain
  **tracking records** of how far they have read. Exactly-once consumption,
  replayable. This replaces `signal_detector_cursors` and the planned consumer
  table.
- **Schema evolution** → built-in **upcasting** of versioned events.

SQLite environment (per the library tutorial, part 3):

- `PERSISTENCE_MODULE = 'eventsourcing.sqlite'`
- `SQLITE_DBNAME = '/path/to/elixir-events.db'` — the event store gets its **own
  file**, separate from the projection/read-model DB (today's `elixir.db`). Keep
  the library's opaque write store apart from queryable projections.
- `SQLITE_LOCK_TIMEOUT` — optional, default 5s.
- Tests use the in-memory form `'file::memory:?mode=memory&cache=shared'`.

Stored events are neither compressed nor encrypted by default; leadership-scoped
payloads that need protection must be handled explicitly (see Guardrails).

### 4.2 The reshape: the store is opaque; dimensions live in projections

The library stores events as serialized payloads **per aggregate sequence**. You
**cannot** run `WHERE card_key = ? across all members` against the event store.
This kills the old "Event Model" section's wide, 30-column, denormalized
`domain_events` table with dimensional indexes. That table does not exist in this
design.

Instead, the old table's two jobs split cleanly along CQRS lines:

- **Write model** = the library's event store (opaque, per-aggregate, append-only,
  globally ordered via the notification log).
- **Read model** = purpose-built **projection tables** in `elixir.db`, maintained
  by follower process applications. Every "filtered view" the old plan wanted
  (player timeline, card movement, badge cohort, battle-mode, war season,
  recommendation/case audit) becomes a projection with exactly the columns and
  indexes that view needs.

This is more correct CQRS, not a compromise. It also means the existing
`storage/event_stream.py` (the 612-line hand-rolled append helper) and the
`game_event_stream` table are **replaced**, not evolved.

---

## 5. The Aggregate Model

The central design question — answered with the CR API surface in hand — is *what
are the aggregates?* The answer falls into two bounded contexts.

### 5.1 Bounded context A — the Observed World (written only by ingest)

These aggregates record **observations of external game reality**. Their events
are of the form "we observed X changed, when, and from what upstream evidence."
They are written exclusively by snapshot ingest. They never contain inference or
decision.

| Aggregate | Key | Lifecycle | Notes |
|---|---|---|---|
| **Player** | `player_tag` | durable, slow-changing | Primary anchor. Profile/card/badge/achievement/ranked progression. Carries embedded *foreign*-season snapshots (league stats, PoL results) that are projections of the season aggregates, not Player's own lifecycle. Keep lean — do **not** replay battles to reconstruct it (see §5.3). |
| **Clan** | `clan_tag` | durable, slow-changing | Second anchor. Clan-wide state plus **roster membership lifecycle** (joined/left/role-changed) as Clan events — roster is a Clan invariant. |
| **RiverRace** (weekly war) | `(clan_tag, riverRaceSeasonId, sectionIndex)` | training → warDay → colosseum → `finishTime` | The strongest lifecycle in the API. The **week** is the aggregate boundary, not the multi-week "season." `currentriverrace` is the in-flight view; the `riverracelog` entry is the terminal state. ⚠ `riverRaceSeasonId` is a **sequential integer** — a different keyspace from the league season's `YYYY-MM`. |
| **LeagueSeason** (ranked/PoL) | `seasonId` = `YYYY-MM` | monthly start → end | The canonical ranked/league cycle. Player `leagueStatistics` and PoL season results are projections referencing this key. |
| **Tournament** (player-created) | `tournament_tag` | inPreparation → inProgress → ended | Bounded lifecycle with a terminal state and `created/started/endedTime`. Capture final `membersList` on the `ended` transition. Linked battle facts rotate out of battle logs fast — capture promptly. |
| **SpecialEvent** | `eventTag` | appeared → disappeared (**inferred** from polling) | `/events` exposes no start/end timestamps, only presence. Model an appear/disappear lifecycle inferred from polling; play-facts arrive via `Battle.eventTag`. |
| **GlobalTournament** (optional) | `tournament_tag` | `startTime` → `endTime` | Supercell-run; usually absent in sampling. Low priority — include only if/when populated. |

**Facts, not aggregates** (high-volume, immutable, externally authored — they feed
projections, they are not consistency boundaries):

- **Battle** — the core fact stream. Dedup key = `battleTime` + sorted
  `(team[0].tag, opponent[0].tag)`. **Game mode is a field on the battle**
  (`gameMode.id`/`name`, `type`), not an aggregate. ⚠ battle logs are a rolling
  ~30-battle window — facts are lost if not captured promptly.
- **Rankings / leaderboards / upcoming chests** — point-in-time snapshots. Ingest
  as snapshot facts attached to the relevant location/season/mode key.

**Reference data** (slowly-changing global catalogs — versioned static snapshots,
not per-entity aggregates):

- **Card catalog** (`/cards`), **Locations** (`/locations`).

### 5.2 Bounded context B — Elixir's Mind (written only by followers)

This is your DecisionCase/Recommendation insight generalized. These aggregates are
**not part of the game**. They are Elixir's own inferences and decisions. They are
written exclusively by process applications that follow the World's notification
log, and every event they emit carries `caused_by` links pointing back at the
World events that justified it. They are true behavioral aggregates with
invariants and command-driven state machines — unlike World aggregates, which only
record observations.

| Aggregate | Key | Lifecycle | Notes |
|---|---|---|---|
| **Detection** | detection id | detected → (refreshed) → expired/superseded | Derived observations: hot streak, slump, trophy push, ranked surge, cohort badge/achievement wave, card-upgrade wave, inactive-member risk, war momentum shift, clan record. Replaces "signals." Each carries `caused_by_event_ids`. |
| **Recommendation** | recommendation id | detected → refreshed → suppressed/expired → outcome observed | Promotion / demotion / kick / watch / no-action. Real invariants (cannot refresh an expired recommendation). Carries `recommendation_type`, `reason_codes`, `policy_version`, `confidence`/`severity`, `scope='leadership'`, evidence links. |
| **DecisionCase** | case id | opened → refreshed → deferred → accepted/rejected/resolved | The leadership decision state machine. Real invariants (cannot accept a resolved case). Deferred cases resurface because case state says they are due. |

The split answers the user's instinct directly: **Member/Clan/RiverRace/LeagueSeason
are the Observed World** (game reality Elixir watches); **Detection/Recommendation/
DecisionCase are Elixir's Mind** (what Elixir concludes and decides). Side-effect
surfaces (Discord posts) read the Mind and write nothing back.

This also resolves writer-concurrency cleanly: **ingest is the single writer to
the World; each follower owns its own Mind aggregates.** No aggregate has two
writers, so optimistic-version contention is avoided by construction.

### 5.3 Two design decisions this model forces (gating — see §12)

1. **Where do battles live in a library that requires aggregates?** Everything in
   the notification log must be an aggregate event, but a Player aggregate that
   replays thousands of `BattleObserved` events on every load is wrong.
   - *Recommended:* a per-ingest-run **`BattlesObserved` summary event** (on
     Player or a thin `ClanActivity` aggregate) that references a batch, while raw
     battle rows land directly in a **battle-fact projection table** in `elixir.db`
     where they are queryable by mode/time. The log stays lean; the high-volume
     data stays in SQL.
   - *Alternative:* a dedicated per-player `BattleLog` aggregate, never
     reconstructed, consumed forward-only by followers (lean Player aggregate, but
     a high-volume sequence in the library store).
   - Decide in Phase 0. This is the one place library purity has a real cost.

2. **Where do derived events live?** Derived events are Mind events, so they live
   on **Detection / Recommendation / DecisionCase aggregates**, not back on the
   Player. The unified "Player Timeline" view (observations + inferences about one
   player) is reassembled as a **projection** that merges World events for
   `player_tag` with Mind events whose subject is that player. This keeps every
   aggregate single-writer and uses the projection layer we are building anyway.

### 5.4 Why game modes are NOT aggregates

Game *modes* (Trophy Road / Path of Legends / 2v2 / Touchdown / CHAOS / special
events) have no stable identity and no start→end lifecycle of their own. A mode is
a **category of battle facts**, expressed as `gameMode.id`/`type` on the Battle
fact and as a **dimension** on battle projections. Modeling each mode as an
aggregate would invent identity where the API has none.

What *does* deserve a period aggregate is anything with a key and a lifecycle:
River Race week, league season, tournaments, special events. That is the line:
**mode = dimension; bounded cycle = aggregate.**

---

## 6. Mapping current tables to the new model

| Current table(s) | Becomes |
|---|---|
| `game_event_stream` (12.5k battle + 54 signal rows), `storage/event_stream.py` | **Removed.** Replaced by the library event store + projections. |
| `members`, `member_current_state`, `clan_memberships`, `member_metadata`, `member_aliases` | **Projections** of Player + Clan aggregates. |
| `player_profile_snapshots`, `member_state_snapshots`, `member_card_collection_snapshots`, `member_card_usage_snapshots`, `member_deck_snapshots` | **Upstream sample archive** (ingest source) and/or **projections**; role made explicit per table. |
| `member_battle_facts`, `tournament_battles`, `member_recent_form` | **Battle-fact projection(s)** (see §5.3). |
| `member_daily_metrics`, `clan_daily_metrics`, `member_daily_battle_rollups`, `clan_daily_battle_rollups`, `event_rollups` | **Rollup projections** — the one place TZ enters (see §7). |
| `war_current_state`, `war_day_status`, `war_period_clan_status`, `war_races`, `war_participation`, `war_participant_snapshots`, `clan_voyages`, `clan_voyage_entries` | **RiverRace** aggregate + war projections. |
| `tournaments`, `tournament_participants` | **Tournament** aggregate + projections. |
| `card_catalog`, `game_mode_contexts` | **Reference data** (versioned static snapshots). |
| `signal_log`, `signal_outcomes`, `signal_detector_cursors`, `signal_keys.py` | **Removed / replaced** by Detection aggregates + library tracking records. |
| `decision_cases`, `leader_action_recommendations`, `revisits` | **DecisionCase** + **Recommendation** aggregates (the Mind). |
| `communication_intents`, `communication_intent_event_links` | Communication-intent events (Mind→surface boundary, see §8) + a side-effect surface read model. |
| `raw_api_payloads`, `arena_relay_screenshot_observations`, `api_sentinel_observations` | **Backfill/ingest sources** (see §10) + observation events. |
| `awareness_ticks` | Reframed/retired — the schedule-first awareness loop is replaced by reactive triggers + a small cadence set (§8). |

Memory/embedding tables (`clan_memories*`, `memory_*`), Discord plumbing
(`discord_*`, `messages`, `channel_state`, `conversation_threads`), `llm_calls`,
and improvement/project tracking are **outside** the Event Core. They may consume
projections; they are not part of this model.

---

## 7. Projections and read models (UTC storage, TZ only at read)

Projection tables are deterministic read models, each maintained by a follower
process application with its own tracking record. They are rebuildable from the
event log on a copy.

Storage is **UTC**. `local_date` is **removed from the data layer entirely** — it
was presentation logic smuggled into storage, in violation of §3.

The single unavoidable timezone boundary is **daily rollups** (`member_daily_*`,
`clan_daily_*`). A human "today" is a Chicago day. The rule:

- Daily-rollup projections compute the Chicago day boundary **at projection/read
  time, from UTC events**, as an explicit, parameterized presentation choice.
- The timezone (`America/Chicago`) appears in exactly **one** isolated module used
  by rollup projections. No event, no payload, no World/Mind aggregate ever knows
  what Chicago is.

Initial projection set (built in roughly this order of risk):

- per-player current state; per-card current state; per-badge/achievement state
- per-mode battle summary (the battle-fact projection)
- player timeline (World + Mind merge by `player_tag`)
- cohort views (badge/achievement/card by dimension)
- war-season views (RiverRace projections)
- recommendation/case audit view (Mind aggregates)
- daily rollups (TZ-aware, isolated)

---

## 8. Aggregators and the reactive trigger path

Aggregators are **process applications** (`Follower`) that consume the
notification log with tracking and emit derived (Mind) events. The key rule
holds: **aggregators do not post; they emit events.**

Examples (consume → emit):

| Aggregator | Consumes | Emits |
|---|---|---|
| battle streak detector | battle-fact events | `battle_hot_streak_detected`, `battle_slump_detected` |
| ranked pulse detector | ranked battles, `ranked_league_changed` | `ranked_activity_surge_detected`, `ranked_climb_detected` |
| cohort badge detector | `badge_earned`, `badge_level_changed` | `cohort_badge_wave_detected` |
| card movement detector | `card_unlocked`, `card_level_changed`, `card_evolution_changed` | `card_upgrade_wave_detected`, `new_champion_wave_detected` |
| roster health detector | roster/player/war events | `inactive_member_risk_detected` |
| war momentum detector | RiverRace period/member events | `war_momentum_shift_detected`, `war_recovery_needed_detected` |
| recommendation generator | roster/player/war/ranked/case events | `promotion_candidate_detected`, `demotion_candidate_detected`, `kick_candidate_detected`, `leadership_recommendation_suppressed` |

The **reactive trigger** is itself a follower — a **communication policy** process
application. When a noteworthy Detection or Recommendation event lands, the policy
decides whether it warrants action and emits a **communication-intent** event. The
intent is the Mind→surface boundary: the *decision to communicate* is in the core
(scoped public/leadership); the *post, its copy, formatting, and delivery* are the
side-effect surface, outside the core. Existing `communication_intents` is that
surface's read model.

Cadence reflections ("24h clan summary") are a small, separate scheduled follower
that queries projections — not a return of schedule-first awareness.

---

## 9. Event taxonomy

Organized by context and aggregate. (Largely preserved from the prior plan; types
are unchanged in spirit, re-homed onto aggregates.)

### Observed World — Player aggregate

`player_profile_observed`, `player_name_changed`, `player_experience_changed`,
`player_level_changed`, `player_trophies_changed`, `player_best_trophies_changed`,
`player_wins_changed`, `player_losses_changed`, `player_battle_count_changed`,
`player_donations_changed`, `player_war_day_wins_changed`,
`player_challenge_best_changed`.

Ranked / Path of Legends: `ranked_season_result_observed`,
`ranked_league_changed`, `ranked_trophies_changed`, `ranked_global_rank_changed`,
`ultimate_champion_status_changed`.

Cards: `card_collection_observed`, `card_unlocked`, `card_level_changed`,
`card_level_milestone_crossed` (prefer derived), `card_evolution_changed`,
`card_current_deck_changed`, `card_usage_observed`.

Badges/achievements: `badge_collection_observed`, `badge_earned`,
`badge_level_changed`, `achievement_observed`, `achievement_stars_changed`,
`achievement_completed`.

### Observed World — Clan aggregate (roster)

`member_joined`, `member_left`, `member_role_changed`,
`member_active_again_observed`, `member_inactivity_observed`,
`member_donations_changed`.

### Observed World — Battle facts

`battle_played` (high-volume; never injected raw into prompts — feeds projections,
rollups, aggregators), `battle_deck_observed`, `battle_opponent_observed`. See
§5.3 for how these enter the log.

### Observed World — RiverRace aggregate

`war_state_observed`, `war_season_started`, `war_season_completed`,
`war_week_started`, `war_week_completed`, `war_period_started`,
`war_period_completed`, `war_rank_changed`, `war_fame_changed`,
`war_member_participation_changed`, `war_member_decks_used_changed`,
`war_rival_activity_changed`.

### Observed World — LeagueSeason / Tournament / SpecialEvent

`league_season_started`, `league_season_completed`, `tournament_observed`,
`tournament_started`, `tournament_ended`, `special_event_appeared`,
`special_event_disappeared`.

### Elixir's Mind — Detection aggregate

`battle_hot_streak_detected`, `battle_slump_detected`,
`battle_trophy_push_detected`, `ranked_activity_surge_detected`,
`ranked_climb_detected`, `cohort_badge_wave_detected`,
`cohort_achievement_wave_detected`, `card_upgrade_wave_detected`,
`new_card_unlock_wave_detected`, `inactive_member_risk_detected`,
`war_momentum_shift_detected`, `war_recovery_needed_detected`,
`clan_record_detected`, `season_award_granted`.

### Elixir's Mind — Recommendation aggregate

`promotion_candidate_detected`, `demotion_candidate_detected`,
`kick_candidate_detected`, `leadership_recommendation_refreshed`,
`leadership_recommendation_suppressed`, `leadership_recommendation_expired`,
`recommendation_outcome_observed`.

### Elixir's Mind — DecisionCase aggregate

`decision_case_opened`, `decision_case_refreshed`, `decision_case_deferred`,
`decision_case_accepted`, `decision_case_rejected`, `decision_case_resolved`.

### Mind→surface boundary — communication intent

`communication_intent_raised`, `communication_intent_fulfilled`,
`communication_intent_dropped`. (Decision only; no copy/formatting/receipt.)

### System observation

`runtime_job_started`, `runtime_job_succeeded`, `runtime_job_failed`,
`api_schema_shape_observed`, `api_schema_sentinel_emitted`.

---

## 10. Backfill as fixture (the big payoff)

Backfill is not a late nicety. It is the **test fixture and validation oracle**
for the entire rewrite, because Elixir has been **archiving raw API responses**.

The mechanism: backfill replays archived snapshots through the **same aggregate
command methods that live ingest uses**. `Player.observe_profile(snapshot)` emits
the identical events whether the snapshot came from `raw_api_payloads` history or
a fresh poll. There is **one ingest code path**; backfill is just that path fed
historical input. This means the rewrite is exercised against years of real data
before it ever goes live.

Backfill sources:

- `raw_api_payloads` (the archive — primary, highest fidelity)
- `member_battle_facts`, player profile snapshots, card collection snapshots
- war tables, member daily metrics
- `signal_log` / `signal_outcomes`, `leader_action_recommendations`,
  `decision_cases` (for Mind-context history where it can be reconstructed)

Rules:

- Backfilled events stamp `occurred_at`/`observed_at` from the archived
  observation time; `recorded_at` is backfill time. Never infer more precision
  than the source supports.
- Backfill is idempotent (deterministic event keys / aggregate commands).
- Backfill runs on the isolated copy first.

Because raw payloads are archived, projections of history are not guesswork — they
are derived from the same upstream truth live ingest sees.

---

## 11. The migration campaign (one pass, not a phased rollout)

This is a **dependency graph executed as a single campaign on an isolated copy**,
not a release schedule. The ordering exists because each layer is the substrate
the next is tested against — a foundation bug is far cheaper to catch with zero
consumers than with eight. The boundaries are **review checkpoints**, not
shadow-rollout milestones. Nothing ships half-on; it is whole when it merges.

Isolation: a git worktree + a **copy of `elixir.db`** and a fresh
`elixir-events.db`. The running bot is untouched until cutover.

```
[0] Phase 0 — settle the gating decisions (§12). No schema work until done.
      |
[1] Event Core on the library — Application + aggregate skeletons
      (Player, Clan, RiverRace, LeagueSeason, Tournament, SpecialEvent;
       Detection, Recommendation, DecisionCase). Append helpers enforce
       deterministic commands, scope, schema version.
      |
[2] Ingest path — snapshot diff -> aggregate command methods emitting World events.
      |
[3] Backfill -> replay raw_api_payloads through the SAME ingest path.   <-- fixture
      |
[4] Projection followers + replay harness. Rebuild read models from the log.
      |
[5] Aggregators (all of them) as followers emitting Detection events.
      |
[6] Recommendation + DecisionCase followers (the Mind state machines).
      |
[7] Reactive trigger — communication-policy follower -> communication intents.
      Cadence reflections (small) over projections.
      |
[8] Cutover — point the live bot at the new core; delete old tables/paths.
```

No compatibility layer at any step. No old/new dual-run. The CR API is the
self-healing oracle for current state after cutover; only history depends on
backfill, which is validated once (§12).

---

## 12. Verification strategy

The old plan's oracle was dual-running ("compare new projections to old tables
forever"). With no compat and an atomic cutover, the oracle changes:

- **Replay determinism (continuous).** Rebuild any projection from the event log
  twice → byte-identical. Projections are pure functions of events. This is the
  primary, permanent correctness guarantee.
- **One-time parity at cutover (then discard).** Rebuild backfilled projections
  and compare to the current production tables **once**. Divergence is the bug
  list. Expect a real share of it to be **old-code bugs**, not replay bugs —
  telling them apart is the work; budget for it. After cutover the old tables are
  dropped.
- **API re-poll self-heal.** Current-state projections re-derive on the next poll,
  so only *history* is unrecoverable — and history comes from backfill validated
  above.

Unit tests: aggregate command determinism, scope validation, event upcasting,
projection replay, aggregator replay, follower tracking, Recommendation/Case state
machine invariants.

Integration tests: profile diff emits expected Player events; card upgrade emits
ordinary change + milestone-derived event; badge cohort emits wave event; ranked
battles produce ranked aggregate events; recommendation traces to base + derived
events; suppression is recorded; deferred case resurfaces when due.

Operational checks: notification-log append rate by aggregate/family; projection
lag by follower; failed-follower count; payload size distribution; scope-leakage
audit; recommendation coverage audit; DB growth and retention.

---

## 13. Guardrails

- No raw battle flood in prompts (battles are facts behind projections/rollups).
- No leadership data in public-scoped output, by construction.
- No presentation-specific fields in the Event Core.
- No timezone in the data layer; `America/Chicago` exists only in the one rollup
  presentation module.
- No derived/Mind event without causal evidence (`caused_by_event_ids`).
- No recommendation event without scope, policy version, and evidence.
- No backfill event that claims real-time precision it does not have.
- No World aggregate written by anything but ingest; no Mind aggregate written by
  anything but its owning follower (single-writer invariant).
- Leadership-scoped payloads are not protected by the library's default SQLite
  storage — handle sensitive fields explicitly if needed.

---

## 14. Phase 0 decisions that gate everything

No schema or aggregate code until these are written down:

1. **Aggregate boundaries** — confirm the §5 set; settle Player-vs-Clan ownership
   of membership; decide whether RiverRace is keyed at week or season grain
   (recommended: week).
2. **Battle model (§5.3 #1)** — `BattlesObserved` summary event + battle-fact
   projection (recommended) vs. dedicated `BattleLog` aggregate.
3. **Derived-event home (§5.3 #2)** — confirm Mind aggregates + timeline-by-
   projection.
4. **Command idempotency** — deterministic rules for *change* events under the
   aggregate model, including value oscillation (12→13→12) and missed-poll jumps
   (12→14 across a gap) — you only see snapshots, so define exactly what each
   emits.
5. **Key-format hazards** — RiverRace `seasonId` (sequential int) vs LeagueSeason
   `YYYY-MM` are different keyspaces; Player `progress` keys are opaque side-mode
   labels and must **not** key aggregates.
6. **Retention** — produce an actual events/day and 90-day DB-growth estimate
   (you emit "every meaningful delta" for ~50 members on top of ~350–450
   battles/day); let the number drive base-event retention vs rollup survival.
7. **Scope rules** — public / leadership / system_internal, and how leadership
   payloads are protected given default SQLite storage.

---

## 15. Open questions

Most prior open questions are now decided (library: yes; new store: yes, separate
file; sync vs scheduled generation: reactive followers). Remaining:

1. Retention horizon for full-fidelity non-battle base events vs rollup-only
   history (input to Phase 0 #6).
2. Whether `GlobalTournament` is worth modeling given it is usually empty in
   sampling.
3. Whether the cadence reflection set should shrink further over time as reactive
   coverage proves out.

---

## 16. Definition of Done

v5 is complete when:

- every meaningful observed player, card, badge, achievement, roster, battle,
  ranked, war, tournament, and special-event change is an event on a World
  aggregate (or a battle/snapshot fact) and is queryable via projections;
- every Detection, Recommendation, and DecisionCase is a Mind aggregate, written
  only by followers, each event carrying causal evidence back into the World;
- derived observations (streaks, waves) are stream consumers, not signal objects;
- promotion/demotion/kick recommendations are generated by followers and trace to
  base + derived events;
- **Elixir acts reactively** — a noteworthy event triggers a communication intent
  without a schedule, with a small cadence set as the only time-driven exception;
- the data layer is UTC-only, with timezone confined to one rollup presentation
  module;
- projections are rebuildable from the event log on a copy and verified by replay
  determinism;
- the old `game_event_stream` / signal / side-table system is gone, not wrapped.
```