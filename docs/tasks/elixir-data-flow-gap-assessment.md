# Elixir Data-Flow Gap Assessment

Status: Analysis only. This is a point-in-time review of how data actually
moves through Elixir today (2026-06-20), one day after the internal data
subsystem pivot (`docs/tasks/internal-data-subsystem-pivot.md`) was landed
across 8 phases. It is the precursor to a redesign, not the redesign.

Method: live production DB forensics on `elixir.db` (snapshot 2026-06-20
11:50) plus five independent code traces, one per stage. Evidence is cited as
`file:line`.

## Executive verdict

The pivot was genuinely implemented — the new tables exist, are populated, and
are partly wired into the prompt. This is **not** a stubbed feature. But it was
built **alongside** the old ledgers rather than **subordinating** them, and it
was built at **signal grain** rather than the **battle grain** the owner
intended. The result is a system that has a "durable spine" on paper but in
practice still reasons from sparse detector signals and materialized snapshot
blobs, while the same concern is represented in three-to-four tables at every
stage with no authoritative owner. That redundancy — not missing code — is the
"fragmented" feeling.

## End-to-end data-flow map (as built today)

```
                         CLASH ROYALE API
                               │
        ┌──────────────────────┼───────────────────────┐
        │ heartbeat.tick       │ get_player_battle_log  │ war ingest
        ▼                      ▼                        ▼
  roster / member_     member_battle_facts (12,141)   war_* tables
  current_state        + daily/clan rollups (6,640)   (145k snapshots)
        │                      │                        │
        │   DETECTORS (heartbeat/_roster, _war, _awards, player snapshot)
        │                      │                        │
        └─────────► signal dicts ◄──────────────────────┘
                        │
                        │  record_signal_events()  ← ONLY detector signals
                        │  (delivery.py:2388 path #1; system.py:50)
                        ▼
              game_event_stream (29 rows, ~20h old)   ◄── GRAIN GAP:
                        │                                   0 of 12,141 battles
        ┌───────────────┼───────────────┐                   reach the stream
        ▼               ▼               ▼
  elixir_projects   decision_cases   project_event_links
  (4 singletons,    (20, lifecycle   (3 rows)
   assessed each    works; NO          ▲
   tick; own        project_id)        │ loose re-query, not FK
   nothing) ────────┘                  │
        │                              │
        ▼   build_situation() → recent_events + projects + cases blocks
   AWARENESS TICK (run_awareness_tick) ── situation dumped as JSON to Claude
        │
        ├── posts ──► communication_intents (32) ──► Discord  ──┐
        │                    │ (awareness + arena-relay only)   │ ALSO writes
        │                    └──────────────────────────────────┤ signal_outcomes
        ├── silence ─► (quiet tick) ──► NO skip intent           │ (2,009) — the
        │                                                        │ legacy ledger
        └── leader concerns ──► punted to "decision-case workflow"
                                         │
                                         ▼
                       _leadership_action_scan (RECOMPUTE-first)
                       get_members_at_risk / get_promotion_candidates
                                         │ cases used only as a filter;
                                         │ overdue deferred cases DISMISSED
                                         ▼
                       leader_action_recommendations (48; 20 linked to a case)
                                         │ decide_leader_action → case status
                                         ▼
                                   #leader-actions cards
```

Three places hold "observations": fact tables, `game_event_stream`, and
project `state_json`. Four places hold "a concern about a member":
`decision_cases`, `leader_action_recommendations`, `memories`
(`watch-list`/`followup` tags), `revisits`. Two places hold "what we said":
`communication_intents`, `signal_outcomes`.

## Stage-by-stage assessment

### Stage 1 — API → state tables  (MATURE; one structural gap)

What works: ingestion is healthy and battle-accurate. `snapshot_player_battlelog`
(`storage/player.py:1279`) writes one idempotent row per battle into
`member_battle_facts` (UNIQUE on member_id, battle_time, battle_type,
opponent_tag, crowns_for, crowns_against), then recomputes member/clan daily
rollups (`storage/player.py:1366-1367`). War and roster ingest via
`heartbeat.tick`.

Gaps:
- **[High] Grain: 0 of 12,141 battles are projected into the stream.**
  `snapshot_player_battlelog` writes facts, rollups, and a few aggregate
  *pulse-signals* (`battle_hot_streak`, `battle_trophy_push`) but never calls
  `record_game_event`. The dense observation log the owner wants does not exist.
- **[Med] Ingestion lag.** `PLAYER_INTEL_BATCH_SIZE=5` every 30 min
  (`runtime/jobs/_intel.py:49-90`) means a ~50-member roster refreshes battle
  facts only every ~5 hours. Even a battle-grain stream would trail live play.

### Stage 2 — signals → stream  (WIRED; thin by construction, not by bug)

What works: `record_signal_events` (`storage/event_stream.py:231`) records
**before** delivery via the canonical awareness path
(`runtime/signals/delivery.py:2388`, `_deliver_signal_group_via_awareness`),
used by clan_awareness, war_awareness, award_detection, player_intel, tournament,
and system signals. `event_key` is deterministic + `INSERT OR IGNORE` → idempotent.
No code filter drops signal types; the "missing" types are simply weekly/rare and
haven't fired in the 20h since recording went live.

Gaps:
- **[High] No backfill + young recording.** The stream is ~20h old (29 rows) and
  weeks of prior `signal_log` history were never replayed, so any "look back over
  time" is empty today even at signal grain.
- **[Med] Bypass blind spots.** Tournament close/recap
  (`runtime/jobs/_tournament.py:315,391`) and the Clan Wars Intel Report
  (`runtime/jobs/_intel.py:252`) post to Discord with **no** event recorded.
- **[Low] Scope can diverge from delivery lane.** `_scope_from_signal`
  (`storage/event_stream.py:96`) keys off audience metadata + a hardcoded 3-type
  leadership set; leadership-lane war-ops/donation signals are stored `public`.
- **[Low] Plan drift.** The pivot doc claims player-progression and system
  signals "remain transition paths"; code refutes both — they record. Docs are
  stale vs code.

### Stage 3 — LLM review of streaming data, filtered for context  (REAL for projects/cases; COSMETIC for the stream itself)

What works: `runtime/situation.py` builds `recent_events` (7/28/56/90-day window
summaries), `projects`, and `decision_cases` blocks, and `run_awareness_tick`
(`agent/workflows.py:447-454`) serializes the whole Situation dict as JSON into
the Claude prompt. `prompts/agents/awareness.md:18` correctly frames
`recent_events` as context, not an obligation. `weekly-recap` consumes the
`war_season` project snapshot meaningfully (`runtime/helpers/_reports.py:725-748`).

Gaps:
- **[High] The stream block is near-empty and payload-stripped.** `_compact_event`
  (`situation.py:342-356`) drops `payload_json`, so the model sees only event
  type/subject/time, and with ~29 events `recent_events` is essentially a count
  table. The *substance* in the prompt comes from `projects.war_season.state_json`
  (rich) and `decision_cases` counts — not from the stream. So "Elixir reviews the
  stream" is true mechanically but hollow in practice.
- **[Med] Permissive scope filter.** The main clan/war tick builds one Situation
  with `scope_filter="public+leadership"` (`situation.py:479-485`,
  `delivery.py:2404`); only `player_intel` is hard-scoped public-only. Leadership
  event metadata is in-context on turns that may also emit public posts;
  containment is behavioral (prompt discipline), not structural.
- **[Low] Other workflows under-consume.** memory-synthesis and daily-insight pull
  windows only as anti-repetition guardrails (`runtime/jobs/_memory.py:323`,
  `runtime/jobs/_core.py:215-243`); war-recap/season-awards/tournament/intel are
  deliberately signal-only.

### Stage 4 — observations shared with the clan  (PARTIAL; silence not audited, dual ledgers)

What works: `communication_intents` are created **before** delivery for awareness
posts (`create_awareness_post_intent`) and arena-relay sidecars, consumed end to
end (`messages.intent_id` set, `mark_communication_intent_delivered`). The
"why did Elixir post this?" trace exists (`get_communication_trace_for_message`,
`storage/communication_intents.py:851`).

Gaps:
- **[High] Silence is not auditable.** Quiet ticks (`delivery.py:2406-2414`) and
  reason-less empty plans create **no** skip intent — the plan's core exit
  criterion fails; deliberate silence survives only in `awareness_ticks`.
- **[High] Intents cover only 2 lanes.** System-signal, tournament, war-recap,
  season-awards, and generic channel updates write `signal_outcomes` with no
  intent (`delivery.py:1750-1782`). Hence 32 intents vs 2,009 outcomes.
- **[High] Dual-write, no reconciliation.** Awareness deliveries write BOTH
  ledgers (`delivery.py:2271` + `:2289`) with only a soft `intent_id` back-pointer
  and no consistency check, view, or job.
- **[Med] Case/project linkage is heuristic and mostly empty.** `_infer_case_id`/
  `_infer_project_id` are best-effort; only 8/32 and 6/32 populated — the
  post→case/project hop of the trace is broken for ~75%+.

### Stage 5 — leader action requests  (PARTIAL; recompute-first, cases not the spine)

What works: when the scan posts a member-review card it carries `case_id`
(`runtime/jobs/_core.py:1071-1081`), and `decide_leader_action` syncs the decision
back to the case (`storage/leader_actions.py:981-1014`). The write-back half is
solid.

Gaps:
- **[High] The scan is recompute-first, not case-first.**
  `_post_candidate_leader_action_recommendations` (`_core.py:1084`) drives off
  `get_members_at_risk`/`get_promotion_candidates` and uses due cases only as a
  filter (`:1184-1188`). Phase 6's core task is unimplemented.
- **[High] Overdue deferred cases are stranded — and auto-dismissed.** When
  `due_at` passes but the detector no longer flags the member,
  `_dismiss_stale_deferred_case` (`_core.py:1186`) closes the case. The north-star
  ("re-surface when due, without a fresh signal") is inverted. 4 cases are overdue
  now and would not fire as cards.
- **[Med] Cards are not a clean projection of cases (20/48).** Relay action types
  have no case mapping (`storage/decision_cases.py:469-473`).
- **[Med] `decision_cases` has no `project_id`.** Project↔case is a loose
  recomputed query (`storage/projects.py:540-559`), unlike intents which carry the
  FK. The "projects own cases" model is unrealized.
- **[Low] Awareness write-tools default to memories, not cases.**
  `flag_member_watch`/`record_leadership_followup` write a memory and upsert a case
  only if the LLM passes `case_type` (`agent/tool_exec.py:1083-1092,1180-1199`);
  `schedule_revisit` writes the separate `revisits` table.

## Root-cause threads (why it feels fragmented)

1. **Wrong grain.** The stream records detector *summaries*, not *battles*. The
   "long stream of data" was never created; Elixir still reasons from sparse
   signals plus snapshot blobs. (Stages 1–2.)

2. **A new layer added beside the old ones, not over them.** The same thing is
   stored 2–4 times at every stage: observations (facts / event_stream / project
   state); concerns (decision_cases / leader_action_recommendations / memories /
   revisits); delivery (communication_intents / signal_outcomes). No table is
   authoritative, so each subsystem reads its own copy. (Stages 4–5.)

3. **Detector-first, not state-first.** The leader scan and most posting recompute
   from raw facts each run and treat cases/projects/intents as side-projections
   that are then re-queried — so the "durable spine" sits *downstream* of the
   detector and can even be overwritten or dismissed by it. (Stages 3, 5.)

4. **Projects are dashboards, not aggregates.** Four singleton rows, three of which
   (clan_development, onboarding, recruitment) never start or complete, owning
   nothing via FK. The "mission" abstraction is cosmetic; only `war_season` is
   genuinely project-shaped. (Stage 3/5.)

5. **Silence and non-awareness lanes are invisible to the new ledger.** Quiet
   ticks and most channels never create intents, so the communication ledger is a
   minority record, not the decision log it was meant to be. (Stage 4.)

## Naming / design verdict on cases & projects

- **Projects: misnamed and under-designed.** 3 of 4 are standing *domains*, not
  bounded projects; all 4 are write-mostly *assessments* that own nothing. Either
  rename to domains/assessments and drop the mission pretense, or make them real
  aggregates (lifecycle + FK ownership of cases and events).
- **Cases: well-named, architecturally orphaned.** The lifecycle is real, but the
  concern it represents is duplicated across leader cards, memories, and revisits,
  it isn't linked to a project, and the detector — not the case — drives leader
  action. "Case" should be the single home for a member concern; today it is one
  of four.

## Where the leverage is (for the redesign, not yet built)

These are the smallest moves that would most reduce fragmentation; sequencing and
design to be decided in the redesign step.

1. **Project battle facts into the stream** at battle grain (the owner's intent):
   a ~15-line writer in `snapshot_player_battlelog` keyed on the existing dedupe
   tuple, plus a battle `scope`/rollup tier so window summaries and Situation
   aren't swamped (~30–40k rows at 90 days).
2. **Pick one authoritative table per stage** and make the others projections:
   event_stream for observations, decision_cases for concerns,
   communication_intents for delivery — then retire/rename the duplicates
   (memories→narrative only, revisits→case reminders, signal_outcomes→delivery
   transport under intents).
3. **Make the leader scan case-first** (read due/open cases; stop dismissing
   overdue deferrals) so the north-star inactivity loop actually closes.
4. **Resolve the projects question** (domains vs aggregates) and add `project_id`
   to cases if they stay.
5. **Record skip intents** for quiet ticks so silence is auditable.
6. **Backfill** the stream from `signal_log`/facts so history exists.
