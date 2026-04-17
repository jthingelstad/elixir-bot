# Elixir Releases

This file tracks shipped features and capabilities in reverse chronological order.

---

## v4.8 — Trophy Hall

**Date:** 2026-04-16

Awards become first-class. Until now, War Champ, Iron King, and friends were recomputed from `war_participation` at announcement time and lived only in Discord posts and Elixir's conversational memory. v4.8 adds a durable `awards` table, seven award types across season and weekly scopes, and a `trophy_case` on every member — rendered inline on the POAP KINGS roster and published as its own `elixirAwards.json` for the new `/members/trophy/<season>` page.

### New Award Catalog

- **War Champ** — top-3 fame for the season (gold / silver / bronze). Granted on season close.
- **Iron King** — 4/4 decks on every battle day of every battle week. Pass/fail.
- **Donation Champ** — top-3 donation totals for the season.
- **Donation Champ Weekly** — top-3 for each CR week. Piggybacks on the existing `weekly_donation_leader` detector, so the weekly podium now persists to the trophy case automatically.
- **War Participant** — any fame > 0 in any race of the season. Granted mid-season the first heartbeat after a member contributes.
- **Perfect Week** — 4/4 decks every battle day of a single week. Earnable up to 4× per season.
- **Rookie MVP** — top-3 fame among members whose `clan_memberships.joined_at` falls inside the season window.

### Schema & Grants

- `awards` table (migration 30) keys on `(award_type, season_id, section_index, member_id)` — one row per member per scope — with rank stored as data. All grants are idempotent via `INSERT OR IGNORE` so detectors are safe to run every heartbeat.
- `storage/awards.py` hosts the grant queries — Iron King and Perfect Week use `war_participant_snapshots` (final `decks_used_today` per battle day); Donation Champ sums the MAX weekly `donations_week` across the season window; Rookie MVP joins to `clan_memberships.joined_at` inside the season bounds.
- Season detection: a season is "closed" once a newer `season_id` appears in `war_races`, so `detect_season_awards` back-fills on its own without a timing-sensitive trigger.

### Signal & Memory

- New `award_earned` signal type routed to `clan-events` alongside `war_champ_standings` and `weekly_donation_leader`. Dedup key `award_earned::<type>::<season>::<scope>::<tag>::r<rank>`.
- `_award_earned_fact` mapper in `agent/memory_tasks.py` stores every grant as a public `clan_memories` row tagged `<award_type>`, `award`, `season_<N>` — so future conversations can ask "who won Iron King last season?" and get a durable answer.

### POAP KINGS Site

- Each member object in `elixirRoster.json` and `elixirMembers.json` now carries a `trophy_case` array — same row shape as the underlying awards table, ordered newest-season-first. No icon keys, medal labels, or display strings; the site derives rendering from `award_type` + `rank`.
- New top-level `elixirAwards.json` — all seasons, all awards, grouped by `season_id` with `season_start` / `season_end` dates — feeds the `/members/trophy/<season>` page.

### Files

- `storage/awards.py` (new) — grant queries, insert helper, trophy-case reads.
- `heartbeat/_awards.py` (new) — `detect_season_awards`, `detect_weekly_awards`, `detect_weekly_donation_awards`, `detect_war_participant_awards`.
- `db/_migrations.py` — migration 30.
- `heartbeat/__init__.py`, `heartbeat/_pipeline.py` — wire detectors into both tick and the storage-backed war path.
- `runtime/channel_subagents.py` — route `award_earned` to clan-events + durable memory.
- `agent/memory_tasks.py` — award-earned fact mapper.
- `modules/poap_kings/site.py` — `build_trophy_case`, `build_awards_data`, new `awards` content type.
- `runtime/jobs/_site.py` — publish `elixirAwards.json` alongside `roster` and `clan`.

### Tests

- 622 tests passing (was 612). New `tests/test_awards.py` covers idempotent grants, Iron King's all-battle-days rule, season-close detection (no grants mid-season, all ranks on close), weekly donation persistence from signal payload, and the trophy-case site payloads.

---

## v4.7 — Elixir Counting

**Date:** 2026-04-15

The quiz module pivots from card trivia to tactical literacy. Every question now tests a real in-game decision — trade math, cycle cost, cost discipline — instead of "what rarity is this card." Correct answers ship with a short LLM-written explanation in Elixir's voice that ends with why the answer matters in play, and every multi-card question includes a side-by-side strip of the actual card icons.

### Retired Questions

- **Rarity**, **card type**, **evo/hero mode**, and **champion identification** are gone. They were trivia — obvious from the icon or irrelevant to play. Reading cost off a card is a one-time thing; knowing how to trade against one is forever.

### New Question Types

- **Positive trade.** Given a curated scenario — "You Fireball a Musketeer and an Ice Spirit" — is the trade +2 / +1 / Even / -1 / -2? Seeds live in `modules/card_training/trade_scenarios.py` with ~20 canonical Clash Royale situations across Fireball-value, small-spell, big-spell, even, and negative trades.
- **Cycle total.** Sum the elixir cost of a 4-card rotation. Teaches what a cheap vs. heavy deck actually costs.
- **Cycle back.** Given a rotation, how much elixir to cycle back to a specific card? This is the exact math every player does before committing a win-condition push.

### Cost Comparison Upgraded

- `generate_cost_comparison_question` now filters to four cards of the same `card_type` within a 3-elixir cost band — comparing four spells or four troops of similar cost, not a troop to a cheap spell. The question tests discrimination instead of the obvious.

### LLM-Backed Explanations

- Each generator produces the mechanics (math, correct option, choices) deterministically, then hands a compact context to a new `event:quiz_explain` workflow that writes a 1–2 sentence tactical narration in Elixir's voice. Routes to Haiku; ~$0.30/month at 5 questions/day.
- Every explanation closes with "why it matters in play" — never trivia, never filler.
- Deterministic templated fallback kicks in if the LLM call fails, so the quiz never breaks.

### Visual: Card Icon Strips

- Multi-card questions (cycle_total, cycle_back, positive_trade, cost_comparison) now render a composite PNG strip of the actual card icons with labels underneath, attached to the question embed. Built with Pillow at generation time; graceful placeholder tiles when an icon fails to fetch.
- Cost comparison question strip respects the A/B/C/D button order so the visuals line up with the labels.

### Fast-Start Defect Flight

This release shipped six patches in an hour of live testing as real defects surfaced:

- **#15** null-cost support cards crashed the cost-comparison sort.
- **#16** `/quiz start` timed out Discord's 3-second interaction window because 5 LLM calls fired serially. Now deferred and answered via `followup.send`.
- **#17** Haiku wrapped JSON in `\`\`\`json ... \`\`\`` fences, leaking the wrapper into user-visible text. Reused the existing `_parse_json_response` helper.
- **#18** the question text spelled out each card's cost (`"Valkyrie (4), Clone (3)"`) which collapsed cost-literacy questions into grade-school arithmetic. Question text now shows names only; cost math lives in the explanation.

### Files

- `modules/card_training/questions.py` — 2 retired → 5 retired, 2 new, 3 new, 1 upgraded.
- `modules/card_training/trade_scenarios.py` (new) — the curated seed list.
- `modules/card_training/explanations.py` (new) — LLM-backed explanation helper with fallback.
- `modules/card_training/images.py` (new) — card-icon strip composer.
- `agent/workflows.py` — new `explain_quiz_answer` workflow routed to the lightweight model.
- `agent/prompts.py` — new `_quiz_explain_system` prompt.

### Tests

- 575 tests passing (was 571). New tests cover type+cost-range filter, correct math for all three new generators, fallback path when LLM is absent or raises, and a regression test for the null-cost crash.

---

## v4.6 — Clan Keep

**Date:** 2026-04-15

Elixir can now act on what it sees. v4.5 gave the awareness loop perception — one agent turn that reads the full situation and decides what to say. v4.6 gives it hands and a calendar: write tools to flag members and queue leadership follow-ups, a revisit scheduler so the agent can tell its future self "check on this later," and a weekly synthesis job that writes canonical arc memories and retires stale ones. The persona finally matches the implementation.

### Awareness Write Surface (#8)

- The `awareness` workflow now carries three write tools: `save_clan_memory`, `flag_member_watch(member_tag, reason, expires_at)`, and `record_leadership_followup(topic, recommendation)`.
- All three persist as leadership-scoped memories — `flag_member_watch` writes tag `watch-list`, `record_leadership_followup` writes tag `followup`. `save_clan_memory` from awareness uses `source_type=elixir_inference` with `confidence<1.0` (vs `leader_note/1.0` from clanops).
- Per-tick write budget capped at 3 calls; enforced in `agent/chat.py`'s tool loop. The 4th call returns a structured `awareness_write_budget_reached` error.
- Write counts logged in `awareness_ticks` via migration 27 (`write_calls_issued`, `write_calls_succeeded`, `write_calls_denied`).
- `update_member` stays clanops-only — member metadata mutations are a leadership action, not an awareness observation.

### Self-Scheduled Revisits (#9)

- New `schedule_revisit(signal_key, at, rationale)` tool lets the awareness agent schedule a reminder for a later tick. Stored in a new `revisits` table (migration 28) with `UNIQUE(signal_key, due_at)` for idempotent scheduling.
- `build_situation` surfaces due revisits under a `due_revisits` top-level key. `situation_is_quiet` wakes the agent when revisits are due even with zero raw signals.
- Covered revisits are marked `revisited_at` after each tick so they don't re-surface.

### Weekly Memory Synthesis (#10)

- New `memory-synthesis` activity runs Sunday 22:00 Chicago. An LLM turn receives the week's memories, posts from leadership/war/clan channels, live clan state, and prior synthesis arcs, then returns a structured plan.
- Arc memories persist with `source_type=elixir_synthesis`, `confidence=1.0`, scoped to leadership by default. Stale memory IDs are expired via `clan_memories.expires_at`. Contradictions between stored memory and live state are flagged in the digest.
- Migration 29 widens the `clan_memories.source_type` CHECK to include `elixir_synthesis` via a full table rewrite (FTS + triggers + indices rebuilt).
- `MEMORY_SYNTHESIS_DRY_RUN=true` logs the plan without persisting — safe for first-run validation.
- Digest and contradiction list post to `#leader-lounge`.

### Feature Flag Cleanup (#11)

- `ELIXIR_AWARENESS_LOOP` env flag and the legacy per-signal router retired. The awareness loop is now the only path. `_observation_signal_batches`, `_merge_day_transition_batches`, and the conftest leak-guard removed.

### Emoji Fix

- Agent prompts now enumerate the 19 real custom emoji names and permit standard Unicode shortcodes.
- `_resolve_custom_emoji` strips hallucinated shortcodes (e.g. `:poap:`, `:poap_kings:`) via the `emoji` CLDR package while preserving valid Unicode shortcodes (`:dragon:`, `:trophy:`).

### Signal Dedup Fix

- `detect_arena_changes` and `_detect_war_rollovers_for_pair` now propagate `signal_log_type` so `_mark_delivered_signals` writes the specific dedup key. Fixes repeated arena-change posts (6x Vijay) and a latent war-rollover re-fire risk.

### Operational

- Startup message in `#leader-lounge` now shows Release, Build, and Host on one line.
- Test suite: 543 → 571 passing.

---

## v4.5 — Coherent

**Date:** 2026-04-14

Elixir's proactive posting flipped from "one LLM call per detected signal" to "one agent turn per heartbeat that sees the full situation and decides what to say." The agent now investigates before posting, collapses related signals into single coherent posts, and is allowed to choose silence when nothing material has changed. Time and standing context attach to every post by default, not just to checkpoint triggers.

### Unified Awareness Loop

- New `runtime/situation.py` assembler builds one `Situation` payload per tick: time/phase, clan standing, all signals grouped by lane, hard-post-floor list, channel memory, and roster vitals.
- New `awareness` workflow with the full read toolset (including `cr_api`) and an 8-round tool budget. The agent investigates before posting — streak posts cite specific opponents, member-join posts name the new player's deck and trophy count, war recaps name the contributors who carried the week.
- Coherent timing: when 5 war signals (battle-day complete, week rollover, war complete, next practice phase active, etc.) all hit one tick, the agent emits one sequenced post instead of 5 separate ones racing each other.
- Genuine silence: stale signals get caught at the agent layer (e.g. a `battle_hot_streak` signal whose live battle log shows the streak has since broken) and skipped with a logged reason.
- Hard-post-floor fallback: signals like `member_join`, `war_battle_rank_change`, and `capability_unlock` are guaranteed to produce a post — if the agent omits one, the legacy per-signal path delivers it.

### Channel Reorganization — `#trophy-road`

- New `#trophy-road` channel (id `1493787763538133204`) carries volatile non-war battle activity: hot streaks, trophy pushes, Path of Legends promotions, and future Classic/Grand Challenge / Global Tournament / Ultimate Champion finishes.
- `#player-progress` narrowed to durable milestones — arena unlocks, level-ups, card unlocks, badges, achievements. The mixing problem is gone.
- Routing in `plan_signal_outcomes` updated to split `BATTLE_MODE_SIGNAL_TYPES` from `PROGRESSION_SIGNAL_TYPES`. Mixed batches split between lanes.

### Time-Aware Posts in Every Lane

- New `build_situation_time()` helper lifts hours-remaining, day index, phase, and colosseum awareness out of war-checkpoint scope.
- The `_build_outcome_context` envelope now carries a `TIME / PHASE` block on every channel post — river-race posts can reference "9 hours left in Practice Day 2" without waiting for a 6h checkpoint to fire.

### `channel_update` Gets Real Reach

- The proactive `channel_update` workflow moved from `READ_TOOLS_NO_EXTERNAL` to the full `READ_TOOLS` set (now includes `cr_api`) with rounds bumped from 3 to 6. The system prompt now directs the model to investigate before posting.
- Streak posts and rank-change posts can resolve specific opponents instead of restating the signal dict.

### Tests & Eval

- 18 new tests in `tests/test_awareness_loop.py` covering lane classification, situation assembly, fast-path skip, lane validation, and hard-post-floor fallback.
- New replay harness (`scripts/replay_awareness.py`) replays real signals from the local DB through the awareness loop and validates lane discipline + hard-floor coverage. Used to evaluate quality before shipping.
- Test suite: 518 → 536 passing.

### Rollout

- Cutover gated by `ELIXIR_AWARENESS_LOOP=true` env flag for one war cycle, with the legacy per-signal router kept as a fallback. The flag and the legacy router were retired after the cutover validation window; the awareness loop is now the only path.

---

## v4.4 — Omnipresent

**Date:** 2026-04-13

Elixir's horizon expanded from "our clan" to "any clan, any player, any tournament on the live Clash Royale API." A single unified `cr_api` tool bridges the LLM to external lookups by tag, existing local tools now expose the tags the LLM needs to chain into scouting, and the scheduled Clan Wars Intel Report was rewired through the normal LLM+tool plumbing instead of bespoke orchestration.

### Unified `cr_api` Tool

- New LLM tool with 8 aspects: `player`, `player_battles`, `player_chests`, `clan`, `clan_members`, `clan_war`, `clan_war_log`, `tournament`.
- Ask about any tag — "how strong is clan #QVJJL829", "scout player #P8JVG92U and show me their recent battles", "pull up top members of #G22GQVQR" — and Elixir fetches the answer live.
- Aspect chaining works: `player` → `player_battles` → `lookup_cards` produces a full scouting report with opponent decks identified by name and elixir cost.
- Strict tag validation (`_normalize_cr_tag`) rejects malformed tags with a clean envelope error instead of a 404 from the API.
- Our-clan tags on clan aspects are rejected with a pointer to the richer local tools (`get_clan_health`, `get_clan_roster`).

### Tag Exposure (LLM Chaining)

- `get_member_recent_form` now emits `player_tag` so follow-up scouts can chain.
- `get_member_war_status` now emits `player_tag`.
- `get_member_recent_losses` now emits an `opponent_tags` aggregate so "who's been beating me" can chain into `cr_api(aspect='player')` to scout the opponent.
- Before: the LLM knew *who* beat you but couldn't look them up. Now it can.

### Clan Wars Intel Report — LLM-Driven

- The scheduled Intel Report job (`#river-race`) no longer runs hardcoded orchestration. The LLM drives the fan-out across the four competing clans using `cr_api` and a new `get_clan_intel_report` tool that wraps the existing threat-scoring helpers.
- New `intel_report` workflow with a 15-round tool budget and a narrow toolset — the threat scoring logic was kept, the orchestration and narrative code around it was deleted.
- Same output quality, one consistent code path for conversational scouting and scheduled scouting.

### Guardrails

- Per-turn cap of 5 external lookups per LLM conversation (`EXTERNAL_LOOKUP_CAP`) prevents runaway chains.
- In-module TTL cache (60–600s per endpoint) keeps conversational scouting cheap on the CR API.
- External lookups are excluded from low-context workflows (`observe`, `channel_update`, `reception`, `roster_bios`) where they have no business firing.

### Tests & Dev

- 24 new tests covering tag normalization, cache TTL, dispatch guards, per-aspect whitelist filters, envelope budget, and cap constants.
- New unified eval harness (`scripts/eval_all_requests.py`) runs regular, deck, and cr_api-tag buckets through the real pipeline in a single command.
- Cleaned up the `scripts/` directory and added a README documenting every operational and eval utility.

---

## v4.3 — Deck Review

**Date:** 2026-04-12

Elixir gained a dedicated deck-review workflow that grounds advice in each player's own battle history rather than generic meta talk. It handles regular Trophy Road decks, the four-deck River Race / Clan Wars war pool (which the Clash Royale API doesn't expose directly), and a build-from-scratch suggest mode that's especially useful for clan members who haven't played war yet because they can't figure out how to assemble four non-overlapping decks.

### Personalized Deck Review (`#ask-elixir`)

- Asking "review my deck" / "improve my deck" / "what should I change" now routes to a specialized workflow instead of generic Q&A.
- Advice is grounded in the player's actual recent losses — Elixir cites specific opponent cards (e.g. "Mega Knight has been in 6 of your last 9 losses") instead of repeating meta knowledge.
- All suggestions are validated against the player's collection and card levels — no recommending a card they don't own at competitive level.

### War Deck Review (`review my war decks`)

- Reconstructs the player's four river-race war decks from battle history, since the Clash Royale API doesn't expose them directly.
- Duel battles reveal three decks per battle (one per round); river-race PvP battles reveal one each.
- Returns confidence (`high` / `medium` / `low`) and asks for confirmation when the reconstruction is uncertain.
- Enforces the no-overlap rule on every swap suggestion: a card moved into one deck must come out of wherever it currently lives across the other three.

### Build From Scratch (`build me a deck`, `build my war decks`)

- "Build me a deck" → suggests 1–2 candidate decks with per-card reasoning, drawn from the player's collection and shaped by what's been beating them.
- "Build my war decks" / "I want to start playing war" → builds four full war decks (32 unique cards) with distinct roles per deck. A post-response validator confirms the no-overlap and ownership constraints, asking the LLM to revise (up to 2 attempts) on violations.

### New War Player Onboarding

- Asking "review my war decks" with no war activity yet triggers a warm offer: Elixir acknowledges the player hasn't played war, explains that building four non-overlapping decks is the most common blocker, and offers to put together a starter kit.
- The reply prompt routes seamlessly into the four-deck builder.

### Data Foundation

- New `opponent_deck_json` column on `member_battle_facts` captures opponent decks on every battle ingest going forward, plus a one-time backfill of all 11K+ historical battles from raw API payloads.
- New `losses` include on `get_member` and new `war_decks` aspect on `get_member_war_detail` cleanly extend the existing tool surface (no new top-level tools added).

### Structural

- New `deck_review` LLM workflow registered alongside `interactive` / `clanops` / `observation`, with a higher 10-round budget for the longer war-mode chains.
- New deck-request classifier separates "show my deck" (fast static report, unchanged) from "review my deck" (LLM workflow), eliminating a long-standing routing bug where review intent silently fell through to the display report.
- 12 new tests covering opponent capture, losses aggregation, war-deck reconstruction status logic, no-overlap regression, and the war-suggest validator.

---

## v4.2 — Race Command

**Date:** 2026-04-11

Elixir's River Race intelligence and internal architecture both got sharper in this release. The LLM tool layer was consolidated from 51 tools down to 15 domain-aligned tools, and the #river-race channel now carries real situational awareness of the competitive field and the clan's historic win streak.

### Tool Layer Consolidation

- Collapsed 51 single-purpose LLM tools into 15 domain-aligned tools with aspect-based routing (e.g. `get_war_season(aspect="standings")` instead of separate `get_war_champ_standings`).
- Reduces prompt overhead and gives the LLM cleaner, more predictable tool interfaces.

### River Race — Competing Clan Awareness

- The #river-race subagent now references competing clans by name with fame-gap framing — who's closest, who's falling behind, and snarky commentary when a rival barely shows up.
- Race standings data was already passed to the LLM but previously ignored; the prompt now actively instructs Elixir to use it.

### River Race — Win Streak Memory

- Introduced unscoped "clan identity" durable memories that load for the river-race subagent regardless of which war week is active.
- A race win streak memory is auto-updated on each `war_week_complete` signal by counting consecutive 1st-place finishes in the `war_races` table.
- POAP KINGS' unbroken 1st-place streak (Season 129 Week 2 to present) is now part of Elixir's River Race voice.

### River Race — Day Transition Consolidation

- When a battle day ends and a new one starts simultaneously, the two signals (`*_complete` + `*_started`) are now merged into a single batch, producing one cohesive message instead of two back-to-back posts.
- Applies to all day transition types: battle-to-battle, practice-to-battle, practice-to-practice.

### Structural

- Split large modules and tightened exception handling across the codebase.
- Added API retry logic for transient Clash Royale API failures.
- Fixed 3 pre-existing test failures from stale patch targets.
- Tightened promotion content: non-ASCII escaping in JSON output, more concise copy.

---

## v4.1 — Card Quiz

**Date:** 2026-04-07

Elixir gained a card catalog foundation and a new interactive quiz experience in `#card-quiz`. Members can now test and sharpen their Clash Royale card knowledge directly inside Discord.

### Card Catalog

- Added a synced card catalog from the Clash Royale `/cards` API (121 standard cards, daily refresh at 4 AM CT).
- New `lookup_cards` LLM tool gives Elixir accurate card data (elixir cost, rarity, type, Evo/Hero capability) so it stops guessing when members ask about card stats or tradeoffs in `#ask-elixir`.
- Card catalog syncs at startup and daily via the activity scheduler.

### Card Quiz (`#card-quiz`)

- `/elixir quiz start` — start an interactive quiz session (1-10 questions, ephemeral to the member).
- `/elixir quiz stats` — view personal accuracy and daily streak.
- `/elixir quiz leaderboard` — daily streak rankings.
- A daily quiz question is posted automatically each morning at 10 AM CT.

**Question types (v1):**
- What is the elixir cost of this card?
- Which of these cards costs the most/least elixir?
- What rarity is this card?
- Is this card a troop, spell, or building?
- Does this card support Evo, Hero, both, or neither?
- Which of these cards is a Champion?

All questions are generated from real card catalog data with card images. Daily questions track streaks for consecutive correct answers.

### Structural

- Renamed `integrations/` to `modules/` — both `poap_kings` and `card_training` now live under a unified feature module directory.
