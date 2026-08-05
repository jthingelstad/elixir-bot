# Agentic Loop v2 — event-driven wakes, one chassis, a learning loop

Status: **plan ratified pending Jamie review** (2026-08-04). Umbrella document;
each phase gets its own ready-to-build doc as it comes up. Supersedes the fixed
4×/day awareness cadence as the target architecture. The scoped-composer
experiment (2026-08-04, seven replayed hard-posts, Haiku/Sonnet vs the brain's
actual posts) is the evidence base: a ~25–40K-token scoped turn with tools
matches brain quality at 4–20× lower cost; the brain spends ~300K tokens/tick.

## The destination

```
every 10 min   SENSE       engine tick (unchanged, $0)
               ATTEND      deterministic wake evaluator: immediate / batch / digest / never
minutes        RESPOND     scoped turn on the chassis: tools, post-as-tool-call, validator
daily          DELIBERATE  existing brain, 1×/day: digest signals, trends, backstop sweep
nightly        REFLECT     posts + leader reactions → lessons, dossiers, tuning proposals
weekly         CONSOLIDATE existing Opus memory synthesis, fed by reflection
```

One execution chassis (`run_turn(Attention)`) serves the high-traffic composing
surfaces. Cross-cutting concerns — voice assembly, memory injection, tool
availability, validation, delivery, episode accounting — live exactly once.

## Design rules (from the downsides review, binding)

1. **The chassis earns adoption; it is never a big-bang migration.** Target is
   NOT "workflow count → 0". Genuinely weird workflows (screenshot readout,
   leader-note interpretation, memory synthesis) stay specialists permanently.
   A zoo workflow converts only when we are touching it anyway, gated by a
   golden-output diff.
2. **No per-event-type code paths in the responder.** Wake behavior differences
   are registry data. The day `respond.py` grows an `if event_type ==` branch,
   we are rebuilding v4's `delivery.py` and must stop.
3. **Floors are never budget-gated.** Hard-post coverage reconciliation runs on
   every responder turn; an uncovered floor fails the turn, cursors hold, the
   daily deliberation inherits. Same guarantee as today, relocated.
4. **Learning proposes; Jamie ratifies.** Lessons are capped, evidence-linked,
   visible, and removable. Wake-policy changes ship as approval cards, never
   silently.
5. **Every phase has a kill switch env flag and a fallback-deletion date.** The
   half-migrated middle is the worst state; we do not camp there.
6. **Member data (dossiers, reactions) lives in the DB, never in git** — the
   repo is public.

## Phases

Each phase is independently shippable and independently killable. Do not start
a phase until the previous phase's exit gate is reviewed.

---

### Phase 0 — Shadow wakes + baseline (measure before changing)

**Goal:** know exactly what the wake architecture would have done, before it
does anything.

Build:
- `wake` + `wake_model` fields on `EventContract` (`engine/event_contracts.py`)
  with initial assignments (immediate: joins/leaves/roles/tournament + war
  boundaries/podium/birthday; batch 60 min: legendary badges, arena, pol
  promotions; digest: the rest; never: system noise).
- `runtime/awareness/wake.py`: the evaluator, generalized from
  `trigger.py` (pending-events-past-cursor, per-class high-water marks in
  `stream_cursors`, min-lead suppression, daily wake budget cap). **Log-only**:
  every engine tick it records what it would have fired and when.
- A small report: shadow-wake latency vs. actual post latency per event class;
  wakes/day distribution; projected cost.

Exit gate: ~1 week of shadow data. Jamie reviews the report — do the wake
assignments and volumes look right?
Kill switch: `ELIXIR_WAKE_POLICY=0` (default ON for shadow, it posts nothing).
Size: an evening. No LLM calls, no schema change, no behavior change.

---

### Phase 1 — The chassis, born as the join responder

**Goal:** the smallest real chassis, serving exactly one wake type
(`member_joined`), replacing the join trigger's full-brain run. Joins are the
proven case (trigger.py exists because of them) and the cheapest quality
review (Jamie sees every welcome).

Build:
- `agent/chassis.py`: `Attention` / `Scope` / `Budget` dataclasses;
  `assemble_system` (identity + knowledge **incl. GAME.md** + policy + job file
  + surface guidance — one recipe); `assemble_context` (seed + editorial
  lessons + recent posts for in-scope surfaces); the tool loop against one
  registry where surface tools are enabled by `attention.surfaces`.
- Write tools: `post_to_discord`, `post_to_clan_chat` — executor validates
  deterministically (literal `\n` escapes, wrapping quotes, unknown emoji,
  length caps; clan-chat: 200-char sentence-aware clip + Supercell-filter rules
  moved from prompt to code), writes the outbox intent with `covers_json`,
  delivers via the existing path. One retry on validation bounce, then model
  escalation (Haiku → Sonnet), then leave for the daily brain.
- Floor reconciliation after the turn (`policy.py` logic, new call site).
- Episode record per turn (trigger, context digest, tool trace, deliveries,
  cost, outcome) — v1 storage: `communication_intents`-adjacent JSON in the
  existing telemetry DB, no core-schema migration.
- `prompts/jobs/welcome.md` — the first job file. Scoped seed carries
  precomputed labels (stint history for welcome-back detection is in the
  profile tool already).
- Golden tests: replay the experiment's welcome cases through the chassis.

Exit gate: ≥5 real joins welcomed by the chassis. Jamie compares against brain
welcomes; cost per welcome measured. In-game sibling parity verified (the
2026-07-04 single-pipeline rule).
Kill switch: `ELIXIR_WAKE_RESPONDER=0` → joins fall back to the join trigger
(which stays intact through this phase).
Size: a weekend. No schema migration.

---

### Phase 2 — Roster wakes; brain to 2×/day

**Goal:** the chassis covers all Haiku-tier hard-posts; the scheduled brain
halves.

Build:
- Wake classes go live for `member_left_verified`, `role_changed`,
  `tournament_finished`, `pol_season_podium`, `clan_birthday`, plus the batch
  class (legendary badges / arena / pol promotions, 60-min coalesce).
- Job files: `farewell.md`, `role_change.md`, `podium.md`, `milestone_batch.md`.
- Escalation ladder live end-to-end; `trigger.py` deleted (subsumed).
- Scheduled awareness cadence 4× → 2×/day (`runtime/activities.py`).
- Divergence watch: a nightly check that no two intents in 24h cover
  overlapping signals or re-tell the same member's story (the v4 regression
  canary).

Exit gate: two weeks, zero floor misses (reconciliation log), zero divergence
flags, Jamie satisfied with post quality. Cost report: expected ~$1.40/day
total awareness spend at this stage.
Kill switch: per-class — a wake class flips back to digest with one registry
edit; cadence revert is one line.
Size: 2–3 evenings.
Fallback deletion date: end of phase — trigger.py and the 4× schedule do not
survive into Phase 3.

---

### Phase 3 — War narrative wakes; brain to 1×/day

**Goal:** the big moments (week close, season close, league change) arrive as
Sonnet wakes within minutes; the full brain becomes the daily judgment layer.

Build:
- `week_finished` / `season_closed` / `clan_league_changed` as immediate Sonnet
  wakes with cross-stream batching (the season-close triple must land as ONE
  post — the experiment's batch case).
- Scoped seed precomputes the traps: human week label, league-direction
  semantics, streak counts — models must narrate them, not derive them.
- `prompts/jobs/war_week.md`, `war_season.md`.
- Daily deliberation consumes digest-class signals; gate unchanged in front of
  it. Boundary-day escalation: a war wake may request the full brain when it
  judges the moment bigger than its scope (a tool, not a heuristic).

Exit gate: one full war week + one season boundary handled by wakes, quality
reviewed side-by-side against the brain era. Awareness spend at target
(~$1.00–1.20/day).
Kill switch: war classes → digest (registry edit) restores brain-composed war
posts at the daily cadence.
Size: 2 evenings.

---

### Phase 4 — The learning loop v1 (leader feedback → lessons)

**Goal:** Elixir gets better from Jamie's reactions, nightly, with no deploy.

Build:
- Reaction listener: any leadership emoji reaction on an Elixir message maps to
  its delivery intent via `discord_message_id`; stored as editorial feeder rows
  (existing `engine/editor.py` lane, no new table).
- Leadership free-text replies to an Elixir post route through the existing
  leader-note interpretation machinery, attributed to the same intent.
- `runtime/jobs/_reflection.py` (nightly, Sonnet): reads 24h of intents (posts
  AND gated silences with reasons), reactions/notes, current lessons; emits
  evidence-linked editorial lessons (upsert, capped at 12 injected). Weekly
  Opus synthesis now consumes the nightly notes.
- Lessons already flow chassis-wide via `assemble_context` — no extra wiring;
  the brain keeps its existing `_editorial_guidance` injection.

Exit gate: two weeks of lessons reviewed — are they true, specific, and
traceable? At least one demonstrable behavior change from a reaction.
Kill switch: `ELIXIR_REFLECTION=0`; individual lessons removable by leader
note; a poisoned lane empties with one delete.
Size: a weekend.
Explicit non-goal: cooldown constants stay until Phase 6 proves lessons cover
them.

---

### Phase 5 — Memory of people + carried intentions

**Goal:** Elixir knows its ~50 members as people and can carry an intention
forward in time.

Build:
- Member dossiers: one row per member (DB — **schema migration `_apply_v36`**,
  so this phase is a deploy with the full migration discipline: verify on a
  copy with `ELIXIR_DB_PATH` first). ~500 tokens each: episodic notes ("phone
  broke, said he'd be back"), preferences, notable history. Written ONLY by the
  nightly reflection; injected by `assemble_context` for every member in
  `scope`. Injection-safety: dossier text is model-authored — same
  display-name normalization rules apply.
- `schedule_followup(when, why, member_tag?)` tool + table (same migration) +
  `followup_due` wake class through the standard evaluator/budget/floor path.
  First uses: post-advice check-ins in #ask-elixir, quiet-joiner check, "ask
  canavar how the phone is" class of intentions.

Exit gate: dossier spot-check (accuracy + tone — would Jamie be comfortable if
a member saw their own dossier?); first follow-ups fire and read naturally.
Kill switch: dossier injection and followup wakes are independent flags.
Size: a weekend + the migration care.

---

### Phase 6 — Adoption, tuning, and retirement

**Goal:** the chassis serves the high-value surfaces; the system starts tuning
its own attention under approval.

Build (convert-on-touch, each with a golden-output diff gate):
- `interactive` (#ask-elixir) onto the chassis first — the biggest value line
  inherits dossiers + lessons + episodes in one move.
- `deck_review`, weekly recap (+email), daily deliberation as touched.
- Declare the permanent-specialist list in `workflow_registry.py` docstring.
- Wake-policy tuning cards from reflection ("badge wakes 0/9 → digest?") via
  the existing #actions card machinery, one Done/Decline per card.
- Cooldown retirement: each hand-tuned constant either justified-and-kept or
  replaced by a lesson, one at a time.
- Budget governor (optional, last): daily cognition budget with reserved floor
  allocation; only if the wake budget cap has proven insufficient.

Exit gate: rolling; each conversion stands alone.
Size: ongoing, opportunistic — never a dedicated migration push.

---

## What we are NOT building (standing non-goals)

- No multi-agent staff, no per-channel composers — one author per wake, ever
  (v4's lesson).
- No vector store; retrieval is keyed (member tag, event type, week).
- No autonomous self-modification — reflection proposes, Jamie ratifies.
- No mass-engagement dependency — the learning loop is designed for one
  attentive leader's signal.
- No new posting paths outside the outbox/validator.

## Cost trajectory

| Milestone | Awareness $/day | Latency (wake-worthy events) |
|---|---|---|
| Today | ~2.20 | up to 6h (joins ~10 min) |
| After Phase 2 | ~1.40 | ≤10 min roster, 3h war |
| After Phase 3 | ~1.00–1.20 | ≤10 min everything |
| After Phase 4–5 | +~0.20 reflection | — plus learning + dossiers |

Savings are earmarked for #ask-elixir depth (richer tools, longer answers,
Opus for hard analytical questions), not for pocketing.

## How we work through this

One phase per working session (roughly). Each phase: build → gates → deploy →
observe its exit-gate window → Jamie reviews → next. Phases 0+1 can land in the
same session. The per-phase ready-to-build docs get written as each phase
starts, battle-intelligence style, and this umbrella tracks status.
