# Agentic Loop v2 — event-driven wakes, one chassis, a learning loop

Status: **Phases 0, 1 and 2 shipped and live** (2026-08-05). Umbrella document;
each phase gets its own ready-to-build doc as it comes up. Supersedes the fixed
4×/day awareness cadence as the target architecture. The scoped-composer
experiment (2026-08-04, seven replayed hard-posts, Haiku/Sonnet vs the brain's
actual posts) is the evidence base: a ~25–40K-token scoped turn with tools
matches brain quality at 4–20× lower cost; the brain spends ~300K tokens/tick.

> ## ⚠️ Read this before costing any further phase
>
> **The economics this plan was written against have changed, and the change
> argues against its own remaining cost case.** Awareness was 51% of LLM spend
> when Phase 0 was drafted. Measured 2026-08-05 it is **35%**, and Phase 2's
> cadence cut takes roughly $37/month more off it. The rest of the bill is
> member-facing work the plan never touched: interactive ~15%, deck_review ~16%.
>
> So **Phase 3's value is no longer primarily cost.** It buys $10–15/month net —
> it removes brain ticks but adds Sonnet war wakes — against a total that was
> $201/month. Justify it on **latency and quality** (a season close narrated in
> minutes rather than at the next cron) or not at all. Anyone opening this plan
> to "save money" should read the cost table in the Phase 3 section first.
>
> **A hard daily spend ceiling now exists** (`agent/spend_budget.py`,
> `ELIXIR_DAILY_SPEND_USD=3.20`) for awareness and Ask Elixir only. Scheduled
> jobs do not charge or read it. `wake_response`, `wake_response_chat`,
> `clan_chat_copy`, `reception` and `intent_router` are `ESSENTIAL` and never
> gated. Scheduled awareness is budgeted; the third-rung run after every scoped
> responder failed an uncovered floor uses the explicit `required_work()`
> context. **Any new workflow that carries a hard post MUST stay outside
> `BUDGETED` or use that explicit floor context, or the ceiling can silence a
> floor.** See [[elixir-daily-spend-ceiling]].

## Where this stands

| Phase | State | Flag |
|---|---|---|
| 0 — shadow wakes + baseline | **shipped, live** (`4eaab798`) | `ELIXIR_WAKE_POLICY=1`, `ELIXIR_WAKE_SHADOW=1` |
| 1 — chassis + join responder | **shipped, LIVE, gate MET 2026-08-05** (`276011fb`, enabled `f1d6c2fa`) | `ELIXIR_WAKE_RESPONDER=1` |
| 2 — roster wakes, brain 4×→2× | **shipped, LIVE 2026-08-05, gate MET 2026-08-19** | `ELIXIR_WAKE_RESPONDER=1` |
| 3 — war wakes, brain →1× | **shipped, LIVE 2026-08-19** | `ELIXIR_WAKE_RESPONDER=1` |
| 4 — leader-feedback reflection | **shipped, LIVE 2026-08-19** | `ELIXIR_REFLECTION=1` |
| 5 — dossiers + follow-ups | **complete in code 2026-08-20; natural acceptance watching** (`_apply_v38`) | `ELIXIR_DOSSIERS` kill switch, `ELIXIR_REFLECTION` |
| 6 — adoption + tuning | not started | — |

**Phase 2 shipped 2026-08-05** and is **verified on a real member**: Escanor
joined at 14:57:53Z and the welcome was delivered at 14:58:07Z — **14 seconds**,
against a historical median of 2.0 hours. Exactly one intent covered it (two
would be the v4 failure), and the divergence canary reports clean.

Nine jobs now, the brain runs once a day, and `trigger.py` is gone. What the
pre-build analysis changed about the plan is recorded in the Phase 2 section
below — three of its six steps were wrong about where the work was.

**Phase 2's exit gate is MET, closed by Jamie 2026-08-19.** Two weeks measured
2026-08-05 to 2026-08-19, almost all of it unattended:

| Gate criterion | Result |
|---|---|
| Zero divergence flags | **0 overlaps** across 58 fulfilled intents / 14 days |
| Zero floor misses | **0** |
| Post quality | reviewed and accepted; two defects found and fixed (below) |
| Cost | responder **$0.112/episode**; awareness stack **$0.79/day** against a ~$1.40 target |

41 wake episodes, **40 delivered (97.6%)**. Jobs exercised on live events:
`milestone_batch` 27, `welcome` 9, `farewell` 3, `role_change` 2. The farewell
carried the leader's note through to the post — the `leader_context` path the
phase was built for — and the role changes cited real war evidence.

**`podium` never fired, and could not have.** `pol_season_podium` is monthly; it
last fired 2026-08-03, two days BEFORE Phase 2 shipped, and the next is ~2026-09-03.
Jamie's call on 2026-08-19 was to close the gate rather than hold a shipped phase
open for a calendar: the job is registered, its prompt is written, and it was
rehearsed — waiting three more weeks buys one observation and blocks everything
behind it. **It is the one job still unproven in production; treat its first live
firing as a thing to watch, not as a thing that is known to work.** This is the
same lesson the phase already recorded once about "≥5 real joins": when a gate is
waiting on the world rather than on work, stop waiting.

**What the gate found that the criteria did not ask for** — all four fixed and
deployed 2026-08-19 (`bad0255a`, `8a57ee33`):

- **The escalation ladder fired 10 times and left no evidence.** Haiku carried
  31/41 (76%); every one of the 10 escalations had the identical signature — no
  post, no validator rejection, `stop_reason=end_turn`, output nowhere near the
  2,000-token ceiling. The turn ended early rather than the tier being too weak.
  Only the winning tier's episode was stored, so none of it was diagnosable from
  the episodes; it came out of log lines. A won episode now carries the rungs
  that failed, and a tier that ends without posting gets one nudge at the same
  price before the stronger model is paid for.
- **The canary could be skipped by an unrelated job.** It ran 13 of 14 days. It
  rides inside `action-outcome-refresh`, whose early `return` on failure took it
  down on 2026-08-03 (`database is locked`). A check that did not run is
  indistinguishable from a clean one — the single thing this gate cannot afford.
- **The literal `\n` was renting a round.** 22 bounces across 41 wakes, 56% of
  episodes paying an extra model round, the model rewriting it correctly every
  time. Now repaired and counted onto the episode.
- **The welcome converged on a form.** Nine welcomes, nine different decks, one
  three-sentence skeleton — the trophies-and-arena opener `welcome.md` already
  bans, relocated to sentence two. `recent_posts` was reaching the model the
  whole time; the job file now asks for a different structure, not just
  different facts.

**Also measured, and it is what re-costs Phase 3:** the awareness stack runs at
**$0.79/day** (brain $0.48 + responder $0.31). The plan's post-Phase-2 target was
~$1.40/day and its **post-Phase-3** target was $1.00-1.20/day. Phase 2 alone beat
the Phase 3 number by 25-35%. See the Phase 3 section.

**The brain's own floors are still unwatched.** `divergence.floor_misses` reads
wake episodes only, so it covers the responder and nothing else. Two brain runs
failed in 29 during the window (llm_api_error 2026-08-12; a copy-policy repair
failure 2026-08-17). Fail-closed worked both times and the next run covered the
signal — but the 2026-08-17 failure delayed a `week_finished` hard post by
**16h 24m** with nothing raising a flag. The normal figure is ~4.5h.

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
| Chassis | `agent/chassis.py` — `Attention` / `Scope` / `run_turn`; spend policy stays in the workflow registry |
| Delivery validator | `agent/post_validation.py` |
| Posting tools | `agent/tool_defs.SURFACE_TOOLS` + executors in `agent/tool_exec.py` |
| Explicit silence | `choose_silence`, offered only by a silence-allowed `Attention` |
| Scoped responder | `runtime/awareness/respond.py` |
| **Job registry (surfaces + event types)** | `runtime/awareness/respond.JOBS` — the per-event-type behaviour, as DATA |
| **Divergence canary** | `runtime/awareness/divergence.py` → daily #leaders report |
| **Spend ceiling** | `agent/spend_budget.py` — `BUDGETED` names awareness / Ask Elixir; jobs are absent |
| **Workflow policy** | `agent/workflow_registry.py` — model, tools, writes, rounds, tokens, effort, timeout |
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
   > **Extended 2026-08-05.** This rule was written about the *wake* budget. A
   > second budget now exists — the daily **spend ceiling** — and it can refuse a
   > model call outright. It applies only to `agent.spend_budget.BUDGETED`.
   > Floor-carrying responder workflows stay outside that set; the normally
   > budgeted awareness brain crosses it only inside the explicit
   > `required_work()` third-rung context. Tests pin both paths.
7. **A number that can change what Elixir does lives in the clan DB.**
   `elixir-telemetry.db` is admin history and must stay safe to delete. The wake
   budget violated this until 2026-08-05 (it counted fired wakes from
   `wake_observations` and could hold a wake on the result); the spend ceiling
   was built to the rule from the start. Writes to telemetry are fine; a *read
   that decides* is not.
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

Shipped: `agent/chassis.py` (Attention/Scope, one system-assembly
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
- `agent/chassis.py`: `Attention` / `Scope` dataclasses; workflow spend lives in
  the canonical registry row;
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

**SHIPPED 2026-08-05.** What the analysis changed, because three of the six
steps above were wrong about where the work was:

- **Step 2 was already done.** `JOB_BY_EVENT_TYPE` is `event_type -> job`, which
  is *already* many-to-one: four milestone types pointing at one job collapse to
  a single name and `job_for`'s one-job rule fires unchanged. Step 4's "needs a
  many-types-one-job mapping" was zero lines.
- **The real blocker was two levels up, in the evaluator.** Wakes grouped by
  `(class, model)`, not by job. With five jobs registered, a join and a verified
  departure — both `(immediate, lightweight)` — would land in ONE wake mapping to
  `{welcome, farewell}`, be refused as ambiguous, and fall to the brain silently,
  on exactly the busy ticks that matter most. Replaying real history: 34 events
  that would have been one refused wake are now three clean ones. The key is
  `(class, model, job)` — class stays because it carries timing, so an immediate
  legendary badge is not held behind an arena climb's batch window.
- **Payloads never reached the responder at all.** `pending_events` selected
  everything except `payload_json`, so `respond`'s payload branch was live only
  in tests. Every Phase 2 job needs it — the farewell cannot see the leader's
  note, the role change cannot tell a promotion from a demotion, the podium has
  no finishers. One column, and the whole phase depended on it.
- **Job files are hyphenated.** `job_prompt` maps `_` to `-`, so `role_change`
  reads `role-change.md`. The plan spelled them with underscores, which would
  have raised inside `assemble_system` on a live member event.
- **`role_changed` carried no name.** Joins and leaves always have, so this was
  the one clan event whose payload could not say who it was about. Stamped at the
  emitter (normalize at the source) rather than worked around in the job.
- **Surfaces came from measurement, not choice.** 31 days of delivered intents:
  role changes 7/7 announcements and 0/7 clan chat; milestones 28/28 `#elixir`;
  joins 10/10 with a clan-chat sibling; farewells 3 of 7 — which is "notable
  departures only" expressed as evidence. Milestones' apparent clan-chat siblings
  belonged to co-covered joins; `arena_changed` never once posted alone.
- **The trigger's third rung had to be replaced before it could be deleted.**
  `trigger.py` fired the brain out of band for joins; deleting it while halving
  the cron would have left a failed responder waiting up to 12 hours. The
  replacement keys on an uncovered *floor* rather than an event type, so it
  generalises to every hard post and adds no per-event-type knowledge.
- **Nothing recorded a floor miss.** `episode` was set only on the successful
  tier, so a wake that failed every tier left one log line — invisible to the
  query this gate is about. It now names the signals it could not cover.

Baseline this replaces (31 days, measured before the change): verified
departures reached members in a median 50 min, role changes 55 min, the ranked
podium 170 min, and reaching Ultimate Champion took **361 min** — six hours to
say out loud. Two hard posts were missed outright: gtr0925's departure
(2026-07-15) and a promotion to Elder (2026-07-05). Added load is ~1.9 scoped
turns/day.

Exit gate: **MET 2026-08-19** — two weeks, zero floor misses
(`runtime/awareness/divergence.py`), zero divergence flags, post quality accepted,
awareness spend $0.79/day against the expected ~$1.40. Full result and the four
defects the window surfaced are in the status section at the top of this file.

> One correction worth carrying: **the daily report only POSTS to #leaders when
> it finds something.** A clean day is a log line. "Watch #leaders for the daily
> divergence report" was written as if silence meant nothing had run, when in
> fact silence was the pass condition — and for two weeks Jamie was watching an
> empty channel with no way to tell the two apart. If a check is someone's
> evidence, it has to report that it ran, not only that it failed.
Kill switch: per-class — a wake class flips back to digest with one registry
edit; cadence revert is one line.
Size: 2–3 evenings.
Fallback deletion date: end of phase — trigger.py and the 4× schedule do not
survive into Phase 3.

---

### Phase 3 — War narrative wakes; brain to 1×/day

**Goal:** the big moments (week close, season close, league change) arrive as
Sonnet wakes within minutes; the full brain becomes the daily judgment layer.

> **Re-justify before building this. The cost case has evaporated.**
>
> | | share of spend | note |
> |---|---|---|
> | awareness | 35% | was 51% when this plan was written; Phase 2 halved its cadence |
> | interactive | 15% | member conversation — Jamie's stated do-not-cut line |
> | deck_review | 16% | capped at 6 rounds 2026-08-05; 62% of it is cache writes |
> | everything else | 34% | |
>
> Phase 3 removes brain ticks and **adds** Sonnet war wakes: **$10–15/month net**
> against a $201/month total. That is not a cost project. Build it for **latency
> and quality** — a season close narrated in minutes instead of at the next cron,
> and the cross-stream batching that makes the season-close triple land as one
> post — or do not build it.
>
> Two things that did not exist when this section was written and now gate it:
> - **`week_finished`, `season_closed`, `clan_league_changed` and
>   `tournament_finished` are hard posts on the `chat` tier.** Whatever workflow
>   composes them MUST stay outside `agent.spend_budget.BUDGETED` or use the
>   explicit `required_work()` floor context, or the daily spend ceiling can
>   silence a season close. `wake_response_chat` is already outside; anything
>   new must make the choice deliberately.
> - **The war wakes share `(immediate, chat)` with `pol_season_podium`.** The
>   grouping key is `(class, model, job)`, so a podium landing in the same tick as
>   a week close now separates cleanly — but only because the podium has a job.
>   Give each war type a job or they will collide with each other.

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

**SHIPPED 2026-08-19.** What the pre-build analysis changed, continuing this
plan's habit of being wrong about where the work is:

- **The two job files were the bug.** `war_week.md` + `war_season.md` would have
  split the season boundary into two jobs — and wakes group by (class, model,
  job), so that is two wakes and two posts narrating one moment, the exact
  divergence this architecture exists to prevent. Measured at
  2026-08-03T11:17:22Z, `week_finished`, `season_closed` and
  `clan_league_changed` fire in the SAME instant. One job (`war_close`) makes the
  plan's own "must land as ONE post" structural rather than hoped-for.
- **The contracts were already right.** All four war types were already
  `immediate`/`chat`; they simply had no job, so `job_for` returned None and they
  fell to the brain. Phase 3's core was a registry entry, not a wake-policy
  change — the same discovery Phase 2 made about step 2.
- **`tournament_finished` and `clan_birthday` were hard posts with no job**, and
  nothing in the plan mentioned them. Both had to be registered before the
  cadence could be cut, because a floor with no job waits for the brain.
  `tests/test_wake_jobs_phase3.py::test_every_hard_post_has_a_job` is now the
  join between the jobs and the cron: it fails if anyone adds a hard post without
  a job, which is the precondition for `AWARENESS_LOOP_HOURS_DEFAULT = "9"`.
- **Which brain slot to cut was measurable.** Over the Phase 2 window the 21:05
  CT run was silent 11 of 15 times for 5 posts; the 09:05 CT run posted on all
  14. The quiet slot went.
- **The seed's traps were real and none were in the payload.** Standings carry
  clan TAGS with no names; the week label needs `section_index + 1` off the
  dedup key, not the payload; and war-league tiers run ASCENDING inside a band
  (Silver II is above Silver I), the opposite of the ranked ladder. All three are
  resolved in `_war_facts` before the model sees them.

Not built: the boundary-day escalation tool (a war wake asking for the full
brain). The failure path it would improve already exists — an uncovered floor
runs the brain out of band — and adding a surface tool touches three tool-name
sets for a case that has not yet occurred. It stays on the plan.

Exit gate: one full war week + one season boundary handled by wakes, quality
reviewed side-by-side against the brain era. Awareness spend at target
(~$1.00–1.20/day). **Next natural checkpoints: a week close ~2026-08-24 and the
season boundary ~2026-09-03**, which is also `podium`'s first live firing.
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
  AND gated silences with reasons), reactions/notes, linked member-authored
  conversations, and current lessons; emits referential editorial lessons
  (upsert, capped at 12 injected). Weekly
  Opus synthesis now consumes the nightly notes.
- Lessons already flow chassis-wide via `assemble_context` — no extra wiring;
  the brain keeps its existing `_editorial_guidance` injection.

**SHIPPED 2026-08-19.** What the build changed about the plan:

- **Reactions are feeders, not lessons, and the tag is what enforces it.** The
  chassis selects injected guidance by the `editorial` tag and takes 12. A raw
  reaction written into that set would evict a real lesson in order to tell every
  future turn that somebody once pressed a thumbs-up. Reactions carry
  `editorial-feeder` instead; only the nightly pass promotes a conclusion.
- **The caps and references are in code, after the model answers.** Three lessons
  a night, a 0.5 confidence floor, and a hard requirement that every
  `evidence_refs` value exist in that night's evidence index. Plausible free text
  and invented IDs are dropped, not downgraded.
- **Lessons dedupe on their evidence, not their wording.** The 24h window
  overlaps at the boundary, so the same reaction re-read tomorrow would otherwise
  become a second copy of the same rule in different words.
- **A quiet day makes no model call at all.** No intents, reactions, or linked conversations means
  there is nothing to reflect on, and paying to be told so is not a learning loop.
- **The workflow is toolless on purpose.** Everything it may reason about is
  handed to it; a tool would let it go and find a fact to justify a lesson it had
  already decided to write.

Not built: leadership free-text replies routed through the leader-note
interpreter. Reactions are the higher-signal, lower-ambiguity half and they
carry the exit gate; threading replies through note interpretation is a second
attribution problem worth doing on its own evidence.

Exit gate: two weeks of lessons reviewed — are they true, specific, and
traceable? At least one demonstrable behavior change from a reaction. **First
lessons possible 2026-08-19 02:40 CT** (the job is a no-op until a leader reacts
to something, so the gate starts when the first reaction lands).
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

**SHIPPED 2026-08-19** as `_apply_v38` (`member_dossiers`, `scheduled_followups`),
rehearsed on a copy of the 1.7GB production database before deploy: 0.00s, no row
counts changed, `integrity_check` ok, `foreign_key_check` clean.

**Completed structurally 2026-08-20:** linked Ask Elixir/deck-review messages now
enter reflection with stable `message:<id>` references; dossier persistence
requires a matching member-authored reference. Interactive and deck-review turns
may call one bounded `schedule_followup`. Due follow-ups can produce a Discord
post, a durable clan-chat-only relay, or explicit successful silence, and the
wake cursor advances only for a consumed outcome. A cross-layer test covers that
conversation → dossier/follow-up → due event → silence → cursor slice.

- **No backfill, deliberately.** Generating fifty dossiers from statistics on
  migration day would manufacture exactly the confident-sounding fiction this is
  meant to replace. A dossier is earned by observation; empty is correct.
- **Follow-ups reuse the wake path rather than becoming a second scheduler.**
  A due intention is emitted as an ordinary `followup_due` clan event by the
  engine tick, so it inherits cursors, grouping, escalation and the brain
  backstop for free. `fired` is set at EMISSION — leaving the row pending too
  would give one intention two retry mechanisms, which is how a gentle check-in
  becomes the same question asked four times.
- **`followup_due` is NOT a hard post.** A check-in is a kindness, not an
  obligation, and a floor would block the cursor until someone is asked how their
  phone is. Silence succeeds only through `choose_silence`; an empty model turn
  remains a failed attempt and escalates.
- **Clan chat can stand alone.** The staged row uses lane `clan_chat` in the same
  delivery outbox. Unlike the best-effort sibling relay after Discord already
  landed, a clan-chat-only intent stays pending unless its relay card succeeds.
- **Dossier injection is on by default with a kill switch.** `ELIXIR_REFLECTION`
  gates writing; `ELIXIR_DOSSIERS=0` disables member-facing injection. Exact
  conversation references now protect the capture side of that boundary.
- **`schedule_followup` reaches every relevant conversation with a tight cap.**
  Awareness/clanops retain their existing write surface; `interactive` and
  `deck_review` receive only this write tool, at one successful call per turn.

Exit gate: the structural path is covered and focused tests pass. Natural
acceptance remains: spot-check the first referential dossiers for accuracy/tone
and the first organic follow-up for timing and voice; do not manufacture either.
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
