# Elixir v5.1 — Runtime Engine

> **Status:** Implemented; the pre-cut evidence in §1 is retained as historical
> design input and the production amendment in §2 is authoritative.
> **Owner:** Jamie · **Last reviewed:** 2026-07-15
>
> The driver `architecture.md` omits (§17.3): what runs each tick, in what order,
> with what cursor and delivery guarantees. Grounded in the current live loop
> (`event_core/live/tick.py`, `service.py`, `discord_consumer.py`,
> `runtime/activities.py`) — the proven semantics carry; the framework does not.

## 1. What existed before the clean break (historical)

- `v5-reactive-tick` runs every **30 min** (`HEARTBEAT_INTERVAL_MINUTES`,
  `runtime/activities.py:36–47`, `max_instances=1, coalesce=True`). One tick =
  `apply_payloads → advance followers → IntentConsumer.run`
  (`event_core/live/tick.py:10–19`).
- `fetch_payloads` (`tick.py:22–41`) fetches **the full roster** — every member's
  profile *and* battlelog every tick (~100+ calls/30 min) — plus clan and
  `currentriverrace`.
- A **second poller coexists**: `player-progression` (`_player_intel_refresh`)
  refreshes the v4 read model in batches of **5** every **30 min** with 2.0 s
  request spacing (`runtime/jobs/_intel.py:53–57`). This is the "round-robin"
  `architecture.md` §15 describes. Two pollers hitting the same endpoints for two
  data models is Part I's duplication in live form — v5.1 replaces **both** with
  one scheduler.
- Delivery is at-least-once with a **6 h staleness drop**
  (`MAX_INTENT_AGE_HOURS = 6`, `discord_consumer.py:36`): on a failed post the
  consumer **stops without advancing its position** and retries next tick
  (`:66–101`); stale backlog is dropped with reason `stale_backlog`.

## 2. The v5.1 tick

One engine tick, default **every 10 minutes** (configurable; the poll scheduler,
not the tick rate, controls API spend). Single process, single writer; APScheduler
`max_instances=1, coalesce=True` carried.

```
tick(now):
  1. POLL      — spend the per-tick API budget per the §4 scheduler.
                 Every successful response → api_observation_receipts (append-only)
                 → deduplicated raw_api_payloads content (L1). Nothing else touches
                 the API.
                 engine.observations admits and canonically envelopes the decoded
                 response against its endpoint shape and requested entity identity.
                 Rejection leaves baselines/projections/events and success freshness
                 unchanged; counters + contract incidents explain the silence.
  2. APPLY     — engine.materialize applies all admitted observations in one
                 generation transaction. materialization_inputs records each
                 admitted receipt/hash. Interactive refreshes create their own
                 generation through the same application service.
                 The transaction includes the following three logical sublayers:
       INGEST    battle mirror: new battles → battle_events (dedup-keyed inserts).
                 War keys are resolved from the battle's OWN battle_time against
                 the season/section calendar (war_weeks + the live race baseline),
                 NOT the tick-time clock — the battlelog returns battles hours old,
                 and a late-mirrored battle from a previous war day must land in
                 that day, not this one. A duplicate row enriches previously-missing
                 war context rather than freezing a thinner first observation.
       EMIT      for each polled (entity, aspect): diff against state_baselines,
                 emit player_events / clan_events / war_events, then write the new
                 baseline in the same transaction. First sight emits nothing (§8).
                 On the first tick of each America/Chicago day, the calendar
                 emitter also runs here (birthdays/anniversaries — events.md §4;
                 clock-driven, no API call, date-embedded dedup keys).
       PROJECT   refresh what the polls touched: player_current_state,
                 player_side_mode_progress, player_card_collection, player_recent_form (subjects with new
                 battles), rollups (upsert today's rows), war_weeks /
                 war_week_clans / war_participation / war_attendance_days,
                 member_management inputs, and successful-poll freshness.
  3. MANAGE    — still inside the generation transaction, evaluate source freshness
                 and run Layer-1 evaluators + Layer-2 candidacy machines only for
                 members whose evidence is ready. Stale/missing evidence writes a
                 held judgment and cannot surface as actionable. kick_state transitions fire the Q1 reactive
                 path (a leader action through the existing policy gate).
                 State updates only — the weekly grain (week_anchor,
                 promote_qualifying_weeks) rolls in weekly-leadership-review (§3),
                 never mid-week.
  4. COMMIT    — persist the final materialization_runs status with all streams,
                 projections, readiness, and management writes. Apply/manage failure
                 rolls all semantic writes back and leaves a failed run record.
```

**Current production amendment (2026-07-15):** `run_tick` exposes only the data
path above. It accepts no compose/send callback and cannot enable the retired poster.
The unified awareness loop is the sole proactive owner: it reads the emitted
streams and projections by durable per-stream cursors, makes one whole-situation
editorial decision, validates the complete plan in deterministic code, and
posts with hard-floor coverage. The retired scorer/intent consumer is isolated
behind `engine.legacy_proactive` and can be reached only by an explicit offline
`legacy_proactive=True` rehearsal. This amendment supersedes the proactive
runtime described in `recognition.md` without deleting that historical algorithm.

Stateful stream consumers track positions in `stream_cursors` (`consumer_key`, e.g.
`emit:player:cards`, `recognize:battle`, `project:form`). **Cursor semantics,
per stream:** `cursor_int` = the last processed insertion id — `event_id` for
`player_events` / `clan_events` / `war_events`, and the implicit SQLite `rowid`
for `battle_events` (its PK is a TEXT dedup key; the table is *not*
`WITHOUT ROWID`, precisely so insertion order gives consumers a monotonic
cursor — never order by `battle_time`, which arrives out of order across
players). A failed materialization does not advance any of its coupled outputs;
the transaction rolls back and management is skipped. Independent consumers
still rely on idempotent keys for safe replay. A consumer that fails on the same position **3 ticks
running** skips it, records the poison event to `prompt_failures`
(`failure_type='engine_poison'`), and moves on — one bad payload must not stall
the stream (new guard; today a bad notification can wedge a follower).

**The war clock** (§16.2) is computed from the `riverrace` baseline — a pure
function, no table — and feeds emission, the awareness read, and (only when
explicitly enabled) the legacy war recognizer/composer.
**Day-boundary anchoring (carried learning, pre-v5.1 issue #20; restored
2026-07-04):** CR's reset hour skews off the nominal 10:00Z and drifts per
season — the clock anchors each 24h period on the `war_day_opened` event's
`observed_at` (`engine/clock.py:period_anchor_from_events`); the fixed hour
is only the no-anchor fallback.

## 3. Scheduled activities (the non-tick remainder)

The activity registry (`runtime/activities.py`) stays the single source of truth.
Engine-owned entries after the cut:

| Activity | Schedule | Replaces |
|---|---|---|
| `engine-tick` | every 10 min | `v5-reactive-tick` (30 min) + `player-progression`'s polling role |
| `weekly-leadership-review` | weekly, **Monday 7:00 AM America/Chicago** (after the CR donation/war reset; ratified by Jamie, 2026-07-03) | Q1's batch half — one review post to `#leader-actions` from `member_management` state; promote/demote candidacies surface here. **Owns the weekly grain roll:** before composing, it advances `week_anchor` to the new week and rolls the hysteresis counters (`promote_qualifying_weeks` up/down per the Layer-2 rules). The engine tick (§2 step 5) updates evaluator *states* continuously; only this activity advances the week — so a mid-week flap can't mint a qualifying week. |
| `war-attendance-snapshot` | daily during war days, end of battle day | **finalizes** `war_attendance_days`: the tick (step 4) upserts the day's rows live from participation diffs; this activity closes the day — fills final `decks_used`/`fame_delta` and stamps the row's last `observed_at`. Evaluator inputs (Iron King, war-reliable) read **finalized days only**, so a mid-day read never counts a half-played day against anyone |
| `db-maintenance` | daily | carried; purge targets updated to `schema.md` §1 retention |
| `tournament-watch` | dynamic — leader-started/stopped (`start_tournament_watch` / `stop_tournament_watch`, `runtime/jobs/_tournament.py`); resumes on restart if active | carried as-is (Q7-style port-and-repoint); its lifecycle moments claim the ledger per events.md §5 |
| `api-sentinel`, `daily-clan-insight`, `weekly-recap`, `promotion-content`, `clan-wars-intel`, `weekly-discord-invite-relay` | carried unchanged | reads repoint per `schema.md` §9 |

**Retired activities:** `award-detection` (Q5 — awards now consume
`season_closed` + accrue `war_participant` in step 4; no daily scan),
`war-poll` (the scheduler owns riverrace polling), `leadership-action-scan`'s
*scan* role (Q1 reactive path replaces it) — its **outcome-refresh and feedback
synthesis** duties (`_leadership_action_scan`, `runtime/jobs/_core.py:1129–1160`)
move into the weekly review activity and a small daily `action-outcome-refresh`.

## 4. Adaptive polling (§15) — the budget scheduler

Replaces both existing pollers. State lives in a small `poll_state` table
(added to `schema.md` §7.6):

```sql
CREATE TABLE poll_state (
    player_tag TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE,
    temperature TEXT NOT NULL DEFAULT 'cold' CHECK (temperature IN ('hot','warm','cold')),
    heat INTEGER NOT NULL DEFAULT 0,            -- decaying counter, not just an enum
    last_battlelog_poll TEXT, last_profile_poll TEXT,
    last_battle_seen TEXT, last_roster_delta TEXT,
    updated_at TEXT NOT NULL
);
```

`last_battlelog_poll` and `last_profile_poll` mean **last admitted successful
observation**, not last attempt. A transport failure or rejected payload stays
due according to the previous success time. A legitimate empty battlelog is an
admitted success.

**Temperature (per player):**

- Battlelog poll finds new battles → `heat = 3` (**hot**).
- Clan-poll delta (trophies/donations moved since last clan poll) → `heat = max(heat, 2)` (**warm**) — the cheap
  roster-wide heartbeat: one clan call flags movers without per-player spend.
- Each tick without evidence of activity: `heat -= 1` (floor 0 = **cold**).

**Per-endpoint cadence:**

| Endpoint | hot | warm | cold | Fairness floor |
|---|---|---|---|---|
| clan (1 call, whole roster) | every tick | every tick | every tick | — |
| currentriverrace | every tick on war/colosseum days; hourly on training days (clock-gated) | | | — |
| player battlelog | every tick | ≤ 30 min | ≤ 2 h | **6 h** — every member polled at least this often |
| player profile | ≤ 1 h (and **immediately** after a battlelog arena-up candidate, §11 confirmation) | ≤ 4 h | ≤ 12 h | **24 h** |

**Budget:** `POLL_BUDGET_PER_TICK` (default **40** calls) covers the
**per-player** endpoints (battlelog, profile), spent hottest-first
(priority = heat, then longest-overdue); the fairness floor promotes starved
players to the front regardless of temperature. The clan and `currentriverrace`
calls (≤3/tick) are fixed overhead **outside** the budget — they are the cheap
roster-wide heartbeat the whole design leans on and must never be crowded out.
`riverracelog` (finalized weeks) is not on a cadence at all: it is fetched
**once per `week_finished` / `season_closed`** to stitch final standings
(§16.7) — a handful of calls per month. Request spacing carries the existing
2.0 s (`_intel.py:57`). New members (no `poll_state` row) are seeded warm so
first baselines land quickly — and first-sight emits nothing (§8), so the seed
poll is silent.

Envelope check: worst-case *demand* (50 members all hot) is ~62 per-player
calls per 10-min tick, which the budget **clips to 40** — so the hard ceiling
is 40 × 144 + overhead ≈ **~6.2 k calls/day**, moderately above today's ~4.9 k
(full-roster every 30 min); under clipping, the fairness floor still bounds
every member's staleness. The *typical* day, with a handful of hot players,
runs an order of magnitude lower while hot coverage improves from 30 min to
10 min. The battlelog depth ceiling (mode 30, Q8 appendix) means a hot player
producing >30 battles between polls loses history — 10-min hot polling makes
that practically unreachable.

## 5. Delivery contracts

### 5.1 Production awareness delivery

Awareness validates the complete post plan before any send. A rejected plan
gets exactly one no-tools, wording-only repair; routing, signal coverage, relay
decisions, and factual numbers are immutable. If validation still fails, if a
send fails, or if a hard-post signal is uncovered, the awareness tick fails and
its cursor does not advance. The same evidence resurfaces next loop. Before the
first Discord call, every post is inserted into
`awareness_delivery_intents`. Each post advances independently from `pending` to
`sending` to `fulfilled`, and fulfilled posts are also recorded in
`awareness_posts` for channel-memory dedup context. An explicit send failure
returns only that post to `pending`; a retry skips intents already fulfilled. A
crash that leaves `sending` without a receipt fails closed during a 15-minute
lease, then reclaims the intent to `pending` for an at-least-once retry. This
prefers a rare duplicate over permanently wedging every later awareness post.
Awareness builds its inputs in one SQLite read
transaction and exposes the exact `data_generation` it saw. Thought persistence
and consumed stream checkpoints commit together. There is no template fallback.

### 5.2 Legacy intent consumer — migration shadow (§17.3)

Offline-only carried semantics (`communication_intents`, `schema.md` §7.2):

1. Select `pending`/`failed` intents, oldest first.
2. `expires_at` passed (**6 h** from raise, carried from
   `MAX_INTENT_AGE_HOURS`) → mark `expired`, reason logged. Drop stale — a
   6-hour-old celebration reads as bot lag, not delight.
3. Compose (recognition.md §7) **then** send **then** mark `fulfilled` — never
   fulfil-before-send (carried from `make_agent_poster`,
   `event_core/live/runtime.py:334–341`).
4. Send fails → `status='failed'`, `attempts += 1`, `last_error` recorded, **stop
   consuming** this tick (carried: preserves lane ordering; `discord_consumer.py:66–101`).
   Next tick retries from the oldest failed intent.
5. Every delivered message records intent id + recognition key in its metadata
   (carried shape, `runtime.py:342–360`) so `#ask-elixir` feedback and
   prompt-failure review can trace any post to its evidence.

Duplicate-protection at the boundary: the ledger guarantees one *intent* per
moment; at-least-once delivery means a crash between send and mark can still
double-post once. Accepted for the retained shadow contract — noted so
nobody "fixes" it into fulfil-before-send, which silently loses posts instead.

## 6. Startup

1. `db.get_connection()` verifies the clean-break v5.1 spine, applies ordered
   post-cut migrations from `db/schema.py`, and validates the current schema
   contract. Missing awareness cursors bridge from the last successful thought;
   a truly fresh awareness consumer starts at zero so no unreviewed stream row
   can be silently skipped.
2. Queue startup system signals idempotently (Q7 carry;
   `runtime/system_signals.py`).
3. Post the build-hash check-in to the `#elixir-log` webhook (carried, AGENTS.md).
4. First tick proceeds normally; baselines missing (fresh cut) populate silently
   via first-sight.

## 7. Telemetry

Carried requirements (AGENTS.md agent-loop guardrails): per-tick log line with
counts per materialization step (polled/ingested/emitted/projected/managed/
failed), per-workflow LLM telemetry to `llm_calls`, failures to
`prompt_failures`. The tick writes one `runtime_job_status` row per step group;
awareness persists each thought, outcome, tool trace, cursor checkpoint, and
successful post so silence and retry state remain explainable.

## 8. Decisions this doc makes — ✅ all ratified (Jamie, 2026-07-03)

| Decision | Why |
|---|---|
| Tick interval 10 min (was 30) | Hot-player latency; budget-bounded so spend doesn't scale with rate |
| `poll_state` as its own table (not `stream_cursors` metadata) | It's per-player domain state with its own columns, not a consumer position |
| Poison-event skip after 3 attempts | Today a bad payload can wedge a follower; idempotency makes skipping safe |
| Weekly review lands **Monday 7:00 AM America/Chicago** | Aligns with CR weekly reset; in leadership's feed before the workday |
| `POLL_BUDGET_PER_TICK = 40` default | Keeps worst case near today's spend; tune against observed rate limits |
