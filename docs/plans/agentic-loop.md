# Agentic Loop v2 — event-driven wakes, one chassis, a learning loop

Status: **Phases 0 and 1 shipped and live** (2026-08-04). Umbrella document;
each phase gets its own ready-to-build doc as it comes up. Supersedes the fixed
4×/day awareness cadence as the target architecture. The scoped-composer
experiment (2026-08-04, seven replayed hard-posts, Haiku/Sonnet vs the brain's
actual posts) is the evidence base: a ~25–40K-token scoped turn with tools
matches brain quality at 4–20× lower cost; the brain spends ~300K tokens/tick.

## Where this stands

| Phase | State | Flag |
|---|---|---|
| 0 — shadow wakes + baseline | **shipped, live** (`4eaab798`) | `ELIXIR_WAKE_POLICY=1`, `ELIXIR_WAKE_SHADOW=1` |
| 1 — chassis + join responder | **shipped, LIVE, gate MET 2026-08-05** (`276011fb`, enabled `f1d6c2fa`) | `ELIXIR_WAKE_RESPONDER=1` |
| 2 — roster wakes, brain 4×→2× | not started | — |
| 3 — war wakes, brain →1× | not started | — |
| 4 — leader-feedback reflection | not started | — |
| 5 — dossiers + follow-ups | not started | needs `_apply_v36` |
| 6 — adoption + tuning | not started | — |

**Phase 2 is unblocked.** Phase 1's gate was met on 2026-08-05 by rehearsal
rather than by waiting for five organic joins (see Phase 1 below for why and
what was proven). Nothing else blocks Phase 2 starting.

**A gate lesson worth carrying forward:** "≥5 real joins" was an exit criterion
the team could not influence — it depended on strangers deciding to join a clan.
When a gate is waiting on the world rather than on work, look for a rehearsal
that isolates the untested wiring instead of parking the phase. Two isolations
made it safe here: a database copy and a redirected Discord channel.

Wake-policy decisions already ratified (both from Phase 0 findings): badges
split at the emitter into `badge_earned` (digest) / `legendary_badge_earned`
(immediate); ranked promotions split three ways at league 4 —
`pol_promotion` (digest) / `champion_league_reached` (immediate) /
`ultimate_champion_reached` (immediate). Jamie's governing principle, stated
2026-08-04: *use normalization judiciously to make the data shape work for
Elixir* — when routing needs a distinction, split the type at the emitter or
stamp a resolved field into the payload floor, never a predicate each reader
re-invents.

### Built so far — the map

| Piece | Where |
|---|---|
| Wake policy (data) | `engine/event_contracts.py` — `wake`, `wake_model` per contract |
| Wake evaluator | `runtime/awareness/wake.py` |
| Shadow report | `scripts/wake_shadow_report.py` (`--simulate` replays history) |
| Chassis | `agent/chassis.py` — `Attention` / `Scope` / `Budget` / `run_turn` |
| Delivery validator | `agent/post_validation.py` |
| Posting tools | `agent/tool_defs.SURFACE_TOOLS` + executors in `agent/tool_exec.py` |
| Scoped responder | `runtime/awareness/respond.py` |
| Job prompts | `prompts/jobs/*.md` via `prompts.job_prompt()` |
| Episodes / observations | `wake_episodes`, `wake_observations` (telemetry DB) |
| Tier predicates | `engine/normalize.badge_tier`, `.ranked_league_tier` |

### Bugs this work has found in existing code

Worth keeping, because they are the argument for the next phase's care:

- `MAX_ROUNDS_BY_WORKFLOW` was built only from specs declaring a
  `response_schema`, silently forcing every other spec to 3 rounds.
- `write_tools_allowed` was a declarative-only field that granted nothing; the
  gate hardcoded two workflow names.
- Surface tools declared for the write gate leaked into `ALL_TOOLS` (clanops's
  surface).
- `storage/telemetry.py` used `json.dumps` without importing `json`, hidden by
  the module's fail-soft `except`.
- Reaching Ultimate Champion emitted two events for the same player at the same
  timestamp.

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

**Status: BUILT 2026-08-04, live shadow running, awaiting Jamie's exit-gate
review.** Backfill simulation over the last 20 days is already in (numbers
below); a week of live shadow confirms it.

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

Shipped as: `engine/event_contracts.py` (wake fields + assignments),
`runtime/awareness/wake.py`, `storage/telemetry.record_wake_observation` +
`wake_observations` table (telemetry DB, no core migration),
`scripts/wake_shadow_report.py` (`--simulate` replays history so the gate does
not have to wait a week), `tests/test_wake_evaluator.py` (15 tests).

**Backfill findings, 20 days to 2026-08-04** (`--simulate --days 20`):

| Measure | Result |
|---|---|
| Wakes/day | 5.6 (budget is 20 — ample headroom) |
| Split | 89 batch/Haiku, 20 immediate/Haiku, 3 immediate/Sonnet |
| Median latency saved | 56 min (mean 90, **max 338**) |
| Hard-post misses | **0** — every guaranteed post matched an intent |
| Projected wake cost | $0.15/day vs $2.00/day scheduled brain |

**The finding, and what it changed.** `badge_earned` would have woken 51×
while the brain posted about badges only 6 times. Investigating rather than
demoting revealed the real problem: **one event type covered two populations.**
Of 102 badge events, ~40 were "Card Mastery: <card>" grind and 4 were one-off
Legendary badges — and the brain's six posts were all Legendaries. A wake policy
keyed on event type alone had to choose between waking 40× for grind or making
the rare ones wait.

Jamie's call (2026-08-04) was to **split the type at the emitter** rather than
add a payload predicate to the registry — keeping wake policy pure data, and
putting the distinction where the tier is already known. The general principle
he stated: *use normalization judiciously to make the data shape work for
Elixir.*

Shipped: `legendary_badge_earned` as its own contract (immediate),
`badge_earned` demoted to digest, `badge_tier` stamped into the payload at the
emitter (and added to the payload floor) so `runtime/awareness/read.py` stops
re-deriving `level is None`. `normalize.badge_tier()` is now the single
predicate. Ranked got the same treatment using types that already existed:
`ultimate_champion_reached` immediate, `pol_promotion` digest.

Re-simulated after the split:

| Measure | Before split | After |
|---|---|---|
| Wakes/day | 5.6 | **2.6** |
| Wakes with no matching post | 52 | **1** |
| Median latency saved | 56 min | 63 min |

The one caveat: historical rows all carry the old type, so the simulation
cannot show `legendary_badge_earned` firing. Applying the new predicate to
history, 4 of those 102 badges would wake immediately and 98 would not.

**Ranked promotions, analysed the same way (2026-08-04).** Unlike badges (two
populations, a clean binary) this is a GRADIENT, and the clan's interest tracks
it: promotions into leagues 1-3 reached a post 20% of the time, into 4-6 60%,
into 7 100%. League 4 is where the game renames the tier to "Champion", so the
split uses the game's own boundary. Jamie chose league 4. Shipped:
`pol_promotion` (Master tiers) → digest, new `champion_league_reached` (4-6) →
immediate, `ultimate_champion_reached` (7) → immediate.

The analysis also found a duplicate: reaching Ultimate Champion emitted BOTH
`ultimate_champion_reached` and a `pol_promotion` for the same player at the
identical timestamp. The emitter now fires only the former.

Applied to the 27 historical promotions: 15 stop waking, 10 wake (~0.5/day),
2 duplicates removed.

Exit gate: Jamie reviews the assignments and volumes.
Kill switch: `ELIXIR_WAKE_POLICY=0` (default ON for shadow, it posts nothing).
Size: an evening. No LLM calls, no schema change, no behavior change.

---

### Phase 1 — The chassis, born as the join responder

**Status: BUILT 2026-08-04, shipped OFF (`ELIXIR_WAKE_RESPONDER=0`).** Two real
joins replayed end to end against production data, delivery stubbed.

Shipped: `agent/chassis.py` (Attention/Scope/Budget, one system-assembly
recipe, auto-injected lessons, staging), `agent/post_validation.py`,
`post_to_discord` + `post_to_clan_chat` in `agent/tool_defs.SURFACE_TOOLS`
(never in `_SHARED_TOOL_NAMES` or `ALL_TOOLS`), executors in
`agent/tool_exec.py`, `runtime/awareness/respond.py`, `prompts/jobs/welcome.md`,
`prompts.job_prompt()`, `wake_episodes` in the telemetry DB, and 25 tests.

**Measured:** both welcomes handled on the Haiku tier at **~$0.044 each**
including validator retries, versus ~$0.50 for a brain tick. The returning
member was detected from the precomputed stint history and the post came back
nearly word for word identical to what the brain had written.

The validator bounced twice (a literal `\n`, a `:shortcode:` in clan chat) and
the model fixed both in-loop — the bounce-and-fix contract works.

Three bugs the live run caught, all fixed:
- `MAX_ROUNDS_BY_WORKFLOW` was built only from specs declaring a
  `response_schema`, so a spec declaring 6 rounds silently ran at 3 — the turn
  spent them on a tool call plus a bounce and returned a *weekly recap* for a
  join. Also silently affected `awareness_triage` and `release_notes`.
- Surface tools declared in `TOOL_DEFINITIONS` leaked into `ALL_TOOLS`, which
  is clanops's surface.
- The job file's "years played is the weakest fact" guidance was a paragraph,
  and the model led with account age while holding the deck. Rewritten as a
  prohibition; the next run dropped it entirely and named the deck's cards.

**Exit gate: MET 2026-08-05 (Jamie).** The original bar was ≥5 real joins, which
would have parked the phase for weeks — new members join rarely. Jamie's call:
prove the wiring by rehearsal instead. Three real joins (blackberry, Ram,
Gabriel) were replayed through the **live delivery path**, isolated two ways —
`ELIXIR_DB_PATH` on a copy, every Discord send redirected to `#thinking`.
Verified afterwards: zero intents, zero `awareness_posts`, zero leader-action
rows in the production database.

All three composed, validated, created a durable intent, sent, recorded a
receipt and produced an in-game sibling — `fulfilled`, `attempts=1`, message id
present, no stuck `sending` rows.

Two results only a live run could produce:

- **The escalation ladder fired for real.** On Gabriel, Haiku returned prose
  instead of calling the posting tool and produced no post; the responder
  escalated to Sonnet, which delivered. That path had unit tests but had never
  executed against a live model. Reliability observed: Haiku carried 2 of 3.
- **Cost, measured:** $0.29 for three welcomes — **~$0.10 each** including the
  failed attempt and the escalation. Use that as the planning number rather
  than the $0.044 from the stubbed replay. Still ~1/5 of a brain tick.

Quality reviewed and accepted by Jamie. The welcome led with deck archetype
rather than trophies-and-arena (the prohibited opener), and the seed's stint
history caught that Ram was on his *third* stint — something the brain's own
post had missed.

Still unproven, both well-worn paths every brain post already uses:
`_engine_send` marshalling from the worker thread to the bot's event loop, and
the clan-chat relay raising a real `#actions` card rather than the rehearsal's
stand-in. The first organic join exercises both, with the daily brain as
backstop.


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

**Entry condition:** Phase 1's exit gate is met — ≥5 real joins welcomed by the
chassis and reviewed. Do not start on a hunch that it works; the welcomes are
the evidence.

Build, in this order:

1. **Job files first, one per event type**, because a job file is the only
   per-purpose prose and writing it is where the thinking happens:
   `farewell.md`, `role_change.md`, `podium.md`, `milestone_batch.md`. Mine the
   real posts for the lessons the way `welcome.md` did — `awareness.md` lines
   ~145-160 carry the departure and role-change rules, and the leader-context
   note (`member_left_verified` payload carries `leader_context`) must reach the
   farewell. Kicks are NEVER narrated.
2. **Register them** in `respond.JOB_BY_EVENT_TYPE`. This is the whole wiring —
   if anything else needs to change per event type, stop (design rule 2).
3. **Surfaces per job.** A farewell is announcements + clan chat (notable
   departures only); a role change is announcements; `pol_season_podium` is
   announcements. `respond.respond()` currently hardcodes
   `lanes=("announcements",)` and both surfaces — that must become a per-job
   declaration, which is the first real test of whether the Attention
   abstraction holds.
4. **Batch-class job** (`milestone_batch.md`) for the coalesced
   arena/legendary-badge/champion-league wake. This is the first wake carrying
   MIXED event types in one turn, so `job_for()`'s one-job rule needs a
   many-types-one-job mapping rather than a per-type map.
5. **Cadence 4× → 2×/day** in `runtime/activities.py`, and delete
   `runtime/awareness/trigger.py` (subsumed by the evaluator; its cursor key
   `awareness:join_trigger` can stay as an orphan row or be cleaned).
6. **Divergence watch** — a nightly check that no two fulfilled intents within
   24h cover overlapping signal keys, or re-tell the same member's story. This
   is the v4 regression canary and the one genuinely new safety mechanism the
   phase needs.

Known traps, all paid for already:
- Emoji: prefer the `elixir_` custom set (Jamie); `:crossed_swords:` is the
  honest Unicode exception.
- A departure post must not fire on a raw `member_left` — only
  `member_left_verified`.
- The responder must keep using the caller's `deliver_fn`; a second delivery
  path is the v4 failure.

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
