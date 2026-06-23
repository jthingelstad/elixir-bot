# Plan: Elixir Internal Data Subsystem Pivot

Status: Phases 1-8 have been implemented. The canonical internal data
subsystem is now `game_event_stream` -> `elixir_projects` /
`decision_cases` -> `communication_intents`, with `event_rollups` preserving
long-term summaries beyond the 90-day operational stream.

Follow-up cleanup: awareness-loop hard-post-floor misses now record failed
`coverage_gap` communication intents instead of falling back to legacy
per-signal delivery. Direct `_deliver_signal_group()` callers remain as
transition paths for player progression, tournament, and startup/system signal
jobs.

## Purpose

Elixir has outgrown the original "detector emits signal, router picks channel,
LLM writes post" architecture. The current codebase now has richer data,
an awareness loop, leader action cards, memories, revisits, war snapshots, and
player analytics. Those pieces are valuable, but they do not yet share one
durable model of what Elixir has observed, what Elixir is monitoring, what
Elixir recommends, and what Elixir chose to say.

This plan defines a staged pivot for Elixir's internal data subsystem:

1. Append normalized game observations into a SQLite event stream.
2. Build durable project/mission state on top of that stream.
3. Represent actionable concerns as decision cases.
4. Persist communication intents before Discord/site delivery.
5. Consolidate proactive workflows so each channel is a projection of the same
   underlying Elixir state, not an independent decisioning lane.

The goal is not just fewer missed leader actions. The goal is a more coherent
Elixir: one agentic system with memory of the clan's recent stream, active
missions, unresolved cases, and prior communications.

## Current Problem

The current system has several partial ledgers:

- Authoritative fact tables: roster, member state, battle facts, war snapshots,
  awards, and analytics.
- Signal dicts emitted by detectors during heartbeat or scheduled jobs.
- `signal_outcomes`, which tracks delivery status per signal/channel/intent.
- `awareness_ticks`, which records per-tick awareness-loop observability.
- `revisits`, which records time-bound reminders for the awareness loop.
- `leader_action_recommendations`, which tracks posted action cards and leader
  decisions.
- Contextual memories, which store durable observations but should not be the
  authoritative operational ledger.

These tables are useful, but none of them is "Elixir's observation stream" or
"Elixir's current operating plan." As a result, two different subsystems can see
the same facts and reach related but disconnected conclusions. For example,
inactivity can surface as a #leaders status note while #leader-actions depends on
a later independent scan that recomputes candidates instead of consuming the same
case state.

## Target Mental Model

Use four durable layers:

1. **Facts**: Current and historical game facts from the Clash Royale API.
2. **Event stream**: Append-only semantic events derived from facts and signals.
3. **Projects and cases**: Durable interpretations Elixir is managing over time.
4. **Intents and delivery**: What Elixir decided to communicate, then whether it
   landed.

Plain-language definitions:

- **Fact**: Data directly observed from an API, snapshot, or human entry.
- **Event**: A normalized "something happened" record, suitable for 90-day
  operational stream queries and compact long-term rollups.
- **Project**: A durable mission Elixir is running, such as the current war
  season, recruitment, onboarding, or clan development.
- **Case**: A specific unresolved decision or recommendation, often inside a
  project.
- **Intent**: A planned communication or deliberate skip.
- **Delivery**: The Discord/site/runtime result of an intent.
- **Memory**: Narrative or contextual knowledge. Memory may summarize projects
  and cases, but it should not be the authoritative state machine.

## Target Tables

These are schema sketches. Exact column names can be adjusted during
implementation, but the lifecycle boundaries should stay intact.

### `game_event_stream`

Append-mostly stream of semantic events that Elixir can review over time.

```sql
CREATE TABLE game_event_stream (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  source_system TEXT NOT NULL,        -- heartbeat, war_awareness, player_intel, awards, manual
  source_detector TEXT,
  source_signal_key TEXT,
  source_signal_type TEXT,
  observed_at TEXT NOT NULL,
  occurred_at TEXT,
  scope TEXT NOT NULL DEFAULT 'public',
  subject_type TEXT,                  -- member, clan, war, recruitment, system
  subject_key TEXT,                   -- player tag, clan tag, war season key, etc.
  season_id TEXT,
  war_week TEXT,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

Key rules:

- `event_key` is deterministic and idempotent.
- Events are not channel outcomes and are not post requests.
- Sensitive leadership events use `scope='leadership'`.
- The operational stream should retain enough detail for 90-day queries, so
  Elixir can compare the current 28-day war cycle against the prior cycle and
  still see longer roster/recruitment patterns.
- Query helpers should support tiered windows:
  - 7 days: recent pulse and prompt-friendly immediate context.
  - 28 days: one full River Race cycle.
  - 56 days: current cycle compared with the prior cycle.
  - 90 days: broader trend and analytics horizon.
- Raw payload is kept compact and structured; large API responses stay in the
  existing raw payload/fact tables.

### `event_rollups`

Compact historical stream records that can persist indefinitely after the
full-fidelity operational stream ages out.

```sql
CREATE TABLE event_rollups (
  rollup_id INTEGER PRIMARY KEY AUTOINCREMENT,
  rollup_key TEXT NOT NULL UNIQUE,
  rollup_type TEXT NOT NULL,          -- member_90d, war_cycle, project_summary, case_history
  scope TEXT NOT NULL DEFAULT 'public',
  subject_type TEXT,
  subject_key TEXT,
  project_key TEXT,
  season_id TEXT,
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  source_event_count INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

Rollups are not a replacement for facts or cases. They are a compact historical
view of the event stream for comparisons that need to survive past the 90-day
operational window. Examples: war-over-war participation trend, member
inactivity history, recruitment funnel summaries, and completed case history.

### `elixir_projects`

Durable mission objects that Elixir can manage over time.

```sql
CREATE TABLE elixir_projects (
  project_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_key TEXT NOT NULL UNIQUE,
  project_type TEXT NOT NULL,         -- war_season, recruitment, clan_development, onboarding
  title TEXT NOT NULL,
  status TEXT NOT NULL,               -- active, paused, completed, archived
  scope TEXT NOT NULL DEFAULT 'public',
  starts_at TEXT,
  ends_at TEXT,
  objective_json TEXT,
  current_state_json TEXT,
  last_assessed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

Initial project types:

- `war_season`: One active project per River Race season.
- `clan_development`: Long-running roster health, promotions, inactivity, and
  role balance.
- `recruitment`: Recruiting content, open slots, join funnel, and promotion.
- `onboarding`: New member welcome, Discord linking, early engagement.

### `project_event_links`

Join table from projects to event stream rows.

```sql
CREATE TABLE project_event_links (
  project_id INTEGER NOT NULL,
  event_id INTEGER NOT NULL,
  role TEXT NOT NULL DEFAULT 'evidence', -- evidence, milestone, risk, decision_input
  created_at TEXT NOT NULL,
  PRIMARY KEY (project_id, event_id, role)
);
```

This lets the war-season project collect war rank changes, week completions,
participation risks, and noteworthy player contributions without copying the
event payload into project state.

### `decision_cases`

Durable unresolved concerns or recommendations.

```sql
CREATE TABLE decision_cases (
  case_id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_key TEXT NOT NULL UNIQUE,
  project_id INTEGER,
  case_type TEXT NOT NULL,            -- inactivity_review, promotion_review, war_recovery, onboarding_gap
  status TEXT NOT NULL,               -- watching, recommended, deferred, accepted, rejected, resolved, expired
  severity TEXT NOT NULL DEFAULT 'normal',
  scope TEXT NOT NULL DEFAULT 'leadership',
  subject_type TEXT,
  subject_key TEXT,
  title TEXT NOT NULL,
  recommendation TEXT,
  rationale TEXT,
  next_action TEXT,
  due_at TEXT,
  deferred_until TEXT,
  opened_at TEXT NOT NULL,
  decided_at TEXT,
  resolved_at TEXT,
  evidence_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

Cases are where deferrals should live. A leader action card is a UI projection of
a case, not the case itself.

### `communication_intents`

The decision ledger for what Elixir chose to say or intentionally skip.

```sql
CREATE TABLE communication_intents (
  intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_key TEXT NOT NULL UNIQUE,
  project_id INTEGER,
  case_id INTEGER,
  target_channel_key TEXT,
  audience_scope TEXT NOT NULL,
  intent_type TEXT NOT NULL,          -- post, action_card, skip, revisit, site_update
  status TEXT NOT NULL,               -- planned, skipped, delivering, delivered, failed, superseded
  reason TEXT,
  skipped_reason TEXT,
  content_json TEXT,
  covers_event_ids_json TEXT,
  covers_signal_keys_json TEXT,
  planned_at TEXT NOT NULL,
  delivered_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

This table should eventually replace most routine use of `signal_outcomes` for
proactive decisioning. `signal_outcomes` can remain as a compatibility/delivery
table while the transition is underway.

## Implementation Phases

Each phase should be small enough for a coding agent to complete and verify
without changing unrelated behavior.

### Phase 0: Baseline Inventory and Guardrails

Goal: Prepare for a data-subsystem pivot without changing runtime behavior.

Tasks:

- Convert this document into a tracking issue or link it from an existing
  tracking issue.
- Update `docs/tasks/signal-inventory.md` so it matches the current detectors,
  including fields that are important for stable event identity.
- Define an event identity policy:
  - Prefer explicit `signal_key`.
  - Else use `signal_log_type`.
  - Else derive from event type, subject, season/week/day, occurred date, and a
    compact payload hash.
- Add tests that snapshot current signal delivery behavior for representative
  flows: inactivity, member join, war rank change, war week complete, promotion
  candidate, player highlight.
- Document which workflows are allowed to write events, projects, cases, and
  intents.
- Document the retention model:
  - full-fidelity compact stream rows are operational for 90 days
  - durable history beyond 90 days lives in facts, projects, cases, memories, or
    rollups

Exit criteria:

- No production behavior changes.
- Current proactive flows have test coverage around their existing side effects.
- Coding agents have a stable vocabulary for event, project, case, intent, and
  delivery.

### Phase 1: Event Stream Foundation

Goal: Add `game_event_stream` in shadow mode and ingest current signals into it
without changing posting behavior.

Tasks:

- Add an additive SQLite migration for `game_event_stream` and indexes:
  - `(observed_at DESC)`
  - `(event_type, observed_at DESC)`
  - `(subject_type, subject_key, observed_at DESC)`
  - `(season_id, war_week, observed_at DESC)`
  - `(scope, observed_at DESC)`
- Add `storage/event_stream.py` with helpers:
  - `event_key_for_signal(signal, source_system, source_detector)`
  - `record_game_event(...)`
  - `record_signal_events(signals, source_system, source_detector)`
  - `list_recent_events(...)`
  - `list_subject_events(...)`
  - `summarize_events_by_window(..., windows=(7, 28, 56, 90))`
- Call `record_signal_events(...)` before delivery in:
  - clan awareness
  - war awareness
  - award detection
  - any player progression/intel signal emission path that bypasses
    `_deliver_signal_group_via_awareness`
  - system signals
- Store the resulting `event_id`/`event_key` back into the in-memory signal dict
  where possible as `event_key`.
- Keep Discord delivery, awareness, and leader actions unchanged.

Exit criteria:

- Re-running the same signal batch does not create duplicate events.
- Tests prove idempotent insertion and scope handling.
- A local query can show "last 90 days of Elixir events" from SQLite, with
  focused helpers for 7/28/56/90-day windows.

### Phase 2: Stream-Aware Situation

Goal: Let the awareness loop see a compact recent event window in addition to
current tick signals.

Tasks:

- Extend `runtime/situation.py` with a compact `recent_events` block:
  - a 7-day recent pulse
  - 28-day current war-cycle context
  - 56-day war-over-war comparison context
  - 90-day broader trend context
  - capped row count
  - grouped by lane/type/scope
  - leadership-scoped rows only included for leadership-aware workflows
- Add summarization helpers so the prompt receives compact window summaries, not
  unbounded raw event payloads. Raw 90-day event rows should remain queryable by
  tools/admin scripts, not stuffed wholesale into the awareness prompt.
- Keep `signals_by_lane` as the current-tick trigger source.
- Update `prompts/agents/awareness.md` so the agent treats `recent_events` as
  history/context, not as an obligation to post.
- Record `event_keys` in `awareness_ticks.signal_outcomes_json` or a successor
  field so audits can connect a tick to the stream.

Exit criteria:

- Quiet ticks remain cheap and quiet.
- Awareness can see recent history for pattern recognition.
- Tests verify that leadership-only events do not leak into public channel
  situations.

### Phase 3: War Season Project

Goal: Make the current River Race season a durable project rather than a story
reconstructed from scattered snapshots on every tick.

Tasks:

- Add migrations for `elixir_projects` and `project_event_links`.
- Add `storage/projects.py` with helpers:
  - `ensure_project(project_type, project_key, ...)`
  - `get_active_project(project_type)`
  - `update_project_state(project_id, state_patch)`
  - `link_project_event(project_id, event_id, role)`
  - `project_snapshot(project_key)`
- Create or update the active `war_season` project during war poll or war
  awareness.
- Link war events to the active war-season project.
- Store compact project state:
  - season id
  - current week/day/phase
  - latest rank and point gap
  - rival clans
  - participation health
  - active risks
  - recent war communications
  - prior-cycle comparison where the 56/90-day event windows support it
- Add the active war-season project snapshot to `Situation`.

Exit criteria:

- A CLI or storage test can show the active war-season project without invoking
  the LLM.
- War awareness posts can be grounded in project state plus current signals.
- No delivery behavior changes are required in this phase.

### Phase 4: Decision Cases

Goal: Represent operational recommendations as durable cases with lifecycle and
evidence.

Tasks:

- Add the `decision_cases` table and storage helpers:
  - `upsert_case(...)`
  - `transition_case(case_key, status, ...)`
  - `list_open_cases(...)`
  - `list_due_cases(...)`
  - `case_snapshot(case_key)`
- Start with four case types:
  - `inactivity_review`
  - `promotion_review`
  - `demotion_review`
  - `war_recovery`
- Convert awareness write tools:
  - `flag_member_watch` should upsert a `watching` case when appropriate.
  - `record_leadership_followup` should upsert a `recommended` case when it is
    action-oriented.
  - `schedule_revisit` can remain a reminder, but should link to a case when one
    exists.
- Link `leader_action_recommendations` to `decision_cases` with a nullable
  `case_id` column.
- Move inactivity deferral semantics onto the case:
  - deferral date
  - reason/note
  - next due time
  - current evidence
- Add open/due cases to `Situation`.

Exit criteria:

- An inactive member review has one durable case across #leaders notes,
  #leader-actions cards, deferrals, and resolution.
- A deferred case reappears when due without requiring a new unrelated signal.
- Leader-action cards can be generated from cases.

### Phase 5: Communication Intents

Goal: Persist Elixir's decision to communicate before delivery happens.

Tasks:

- Add `communication_intents` and storage helpers:
  - `create_intent(...)`
  - `mark_intent_delivering(...)`
  - `mark_intent_delivered(...)`
  - `mark_intent_failed(...)`
  - `mark_intent_skipped(...)`
  - `list_recent_intents(...)`
- When `run_awareness_tick()` returns posts or a skip reason, persist intents
  before posting to Discord.
- Link intents to:
  - current tick signal keys
  - event stream rows
  - project id
  - case id, where applicable
- Make delivery consume intents.
- Keep writing `messages` and `signal_outcomes` while compatibility is needed.
- Add a clear "why did Elixir post this?" query path from Discord message back
  to intent, case/project, event stream, and original signal.

Exit criteria:

- Every proactive post has a persisted intent.
- Deliberate silence can be audited as a `skip` intent when a meaningful signal
  or due case was considered.
- Failed delivery does not erase the underlying decision.

### Phase 6: Consolidate Proactive Workflows

Goal: Stop parallel subsystems from making independent decisions from the same
facts.

Tasks:

- Change `_leadership_action_scan()` so it reads due/open cases rather than
  recomputing all candidates independently.
- Change arena-relay sidecars so they are generated from communication intents
  or cases, not from a second call to `plan_signal_outcomes()`.
- Make weekly recap, daily insight, and memory synthesis read projects/events
  where useful, rather than reconstructing the story solely from raw facts and
  recent messages.
- Reduce `plan_signal_outcomes()` to fallback/compatibility behavior.
  Awareness no longer invokes this path for hard-post-floor recovery; remaining
  direct callers should be migrated deliberately.
- Document which scheduled activities are "observers" and which are
  "communicators":
  - observers write facts/events/projects/cases
  - communicators create intents/deliveries

Exit criteria:

- #leaders and #leader-actions are projections of the same cases.
- War posts and leader war notes share the same war-season project state.
- There is one routine proactive decision path for clan/war/player signals.

### Phase 7: Admin and Agent Tools

Goal: Make Elixir's current internal state inspectable by humans and LLM tools.

Tasks:

- Add read tools:
  - `get_event_stream`
  - `get_projects`
  - `get_project`
  - `get_decision_cases`
  - `get_communication_intents`
- Add a local script, for example `scripts/elixir_state.py`, with views:
  - recent events
  - 7/28/56/90-day event summaries
  - active projects
  - open cases
  - due cases
  - recent intents and delivery failures
- Add tests for leadership scope filtering.
- Update leader-facing prompt guidance so Elixir can answer:
  - "What are you monitoring?"
  - "What recommendations are open?"
  - "Why did you post that?"
  - "What would you do next?"

Exit criteria:

- The current list of monitored recommendations is visible without reading raw
  Discord history.
- Elixir can answer leadership questions from structured state.

### Phase 8: Rollups, Backfill, Cleanup, and Retention

Goal: Make the new subsystem sustainable.

Implementation status:

- `event_rollups` stores `member_90d`, `war_cycle`, `project_summary`, and
  `case_history` summaries.
- `get_event_rollups` and `scripts/elixir_state.py rollups` expose long-term
  summaries.
- Weekly database maintenance writes rollups before pruning
  `game_event_stream` rows older than 90 days.
- Historical backfill is intentionally conservative: authoritative fact tables,
  projects, cases, messages, and awards remain the source for old history; the
  event stream is not bulk-reconstructed from raw Discord history unless a
  future analytics need justifies that one-off backfill.

Tasks:

- Decide whether to backfill stream events from:
  - `signal_log`
  - `signal_outcomes`
  - `messages.raw_json`
  - war snapshots
  - awards
- Add the `event_rollups` table and rollup writer if it has not already shipped
  with an earlier phase.
- Define first rollup types:
  - `war_cycle`
  - `member_90d`
  - `project_summary`
  - `case_history`
- Add `get_event_rollups` as a read tool and add long-term rollup views to
  `scripts/elixir_state.py`.
- Add retention policy:
  - `game_event_stream` keeps full-fidelity compact events for 90 days.
  - events that are durable beyond 90 days are promoted into facts, projects,
    cases, memories, or `event_rollups`.
  - `event_rollups` can persist indefinitely when the summary is still useful.
  - oversized payloads should stay in raw/fact tables with references
  - old delivered intents can be summarized if needed
- Add database maintenance checks for event-stream growth and 90-day pruning,
  with pruning blocked until any required rollups have been written.
- Update docs:
  - `AGENTS.md`
  - `docs/tasks/signal-inventory.md`
  - `docs/tasks/agentic-awareness-loop.md`
  - this plan, marking completed phases
- Retire compatibility branches once all scheduled flows use events, projects,
  cases, and intents.

Exit criteria:

- New model is documented as the canonical internal data subsystem.
- Legacy paths are either removed or clearly marked compatibility-only.
- Database growth is understood and bounded.

## Suggested Issue Breakdown

Use one tracking issue for the pivot, then one child issue per phase:

1. Add event-stream table and shadow ingestion.
2. Add stream-aware Situation context.
3. Add durable war-season project state.
4. Add decision cases and link leader action cards.
5. Add communication intents and delivery linkage.
6. Consolidate leadership-action scan and arena sidecars onto cases/intents.
7. Add admin/tool visibility for events, projects, cases, and intents.
8. Add rollups, retention, backfill, docs, and cleanup.

Each issue should include:

- files expected to change
- migration number, if any
- tests to add/update
- runtime behavior expected to remain unchanged or intentionally change
- rollback/compatibility notes

## Ordering Rationale

The event stream comes first because it is useful in shadow mode and has the
lowest behavior risk. Projects come before cases because a war-season project is
the clearest durable mission and gives cases a parent context. Cases come before
communication intents because recommendations and deferrals need durable state
before #leaders and #leader-actions can become projections of the same object.
Communication intents come before delivery consolidation because they create the
audit trail needed to safely retire legacy per-signal routing.

## Risks and Mitigations

- **Duplicate posts**: Keep existing signal completion and delivery dedupe until
  communication intents fully replace them.
- **Model context bloat**: Situation should receive compact event summaries, not
  raw unbounded stream rows.
- **Leadership data leakage**: Every event, project, case, and intent needs a
  scope. Public situations must exclude leadership-scoped rows.
- **Schema churn**: Keep migrations additive. Avoid resetting existing runtime
  tables until the new subsystem has run in shadow mode.
- **Ambiguous ownership**: Scheduled jobs should be classified as observers or
  communicators. Mixed jobs should be split over time.
- **Memory confusion**: Memory can summarize and contextualize, but cases and
  projects are the operational truth.

## Verification Strategy

Minimum tests across the pivot:

- Migration tests for each new table.
- Idempotent event insertion for repeated signal batches.
- Scope filtering for leadership-only events/cases/intents.
- Situation builder includes recent event summaries without leaking private
  rows.
- War-season project is created/updated from war awareness data.
- Inactivity review case survives deferral and reopens when due.
- Leader-action card links to the same case as a #leaders recommendation.
- Awareness posts create intents before delivery.
- Failed delivery leaves the intent available for retry/audit.

Runtime checks:

- A 90-day event stream query produces a useful clan activity timeline, with
  7/28/56/90-day summaries available for prompt context and analytics.
- Active war-season project can be inspected without LLM reconstruction.
- Open cases list matches what leaders see on #leader-actions.
- "Why did Elixir post this?" can be traced from message to intent to case or
  project to event stream.

## Initial North-Star Example

For an inactivity review:

1. `detect_inactivity()` emits an `inactive_members` signal.
2. `game_event_stream` records one event per affected member, plus an optional
   aggregate event.
3. Clan development project links those events as roster-health evidence.
4. `decision_cases` opens or updates one `inactivity_review` case per member.
5. Awareness sees the current signal, recent stream, open cases, and prior
   deferrals.
6. It creates:
   - a #leaders intent if leaders need a status note
   - a #leader-actions action-card intent if a decision is due
   - or a skip intent if the case is already open and not due
7. Delivery posts from intents.
8. A leader deferral updates the case, not just the card.
9. When the deferral expires, the due case re-enters Situation even without a
   fresh detector signal.

This is the behavioral shape the whole pivot should produce.
