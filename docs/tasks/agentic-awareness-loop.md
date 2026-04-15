# Vision: A Unified Agentic Awareness Loop for Elixir

## Context

Elixir's current architecture is deterministic detection plus per-signal prose generation. A heartbeat tick runs every 30 minutes (`heartbeat/__init__.py:89`), emits signals, and a router (`runtime/channel_subagents.py:250 plan_signal_outcomes`) assigns each signal to a channel. Each planned outcome then becomes its own LLM call through `generate_channel_update()` with a pre-enriched context envelope built in `runtime/jobs/_signals.py:238 _build_outcome_context`.

That design worked to ship a reliable feed, but it has three compounding problems:

1. **Shallow commentary.** The `channel_update` workflow uses `READ_TOOLS_NO_EXTERNAL` (`agent/tool_policy.py:11`). It cannot call the new `cr_api` tool added in v4.4 (`agent/tool_defs.py:432`). When Elixir posts on a hot streak, she has no way to look at *who* the player was beating — the post is restricted to what the signal dict already contains. Tool rounds are capped at 3, and the model is asked to "write a post," not to investigate.
2. **Time blindness.** Battle-day hours-remaining, day index, and phase are computed (`heartbeat/_war.py:506`) only inside specific checkpoint signals — 12h/6h/3h triggers fired by the hourly war awareness cron (`runtime/jobs/_core.py:357 _war_awareness_tick`). The agent never gets ambient situational awareness like "it is Battle Day 2, 14h left, we're rank 3, +180 fame behind leader" to reason freely. It just gets discrete tripwires.
3. **Channel semantics are blurred.** `#player-progress` currently carries durable milestones (level-ups, card unlocks, badges) *and* volatile battle-mode activity (`battle_hot_streak`, `battle_trophy_push`, `path_of_legend_promotion`). `#river-race` is correctly isolated to war. Trophy Road / Path of Legends / ladder activity has no home of its own. The `_classify_battle()` classifier (`storage/player.py:420`) already distinguishes war / ranked / ladder / special_event / friendly / other — we just don't route on it.

The fix is not more signals. It is **one awareness loop that sees the world end-to-end and decides what to say**, with real CR API reach and real time awareness, posting into channels that reflect *kinds of activity* rather than signal families.

---

## Current-state map (what exists to build on)

- **Heartbeat detection** — `heartbeat/__init__.py:tick` produces typed signal dicts. Pure, idempotent, cursor-based (`heartbeat/_pipeline.py:38`). Keep this.
- **Tool plumbing** — `agent/chat.py:_chat_with_tools` already supports multi-turn tool loops up to 15 rounds (`intel_report` uses 15). `cr_api` bridge is capped at 5 external calls per turn. This is production-grade; we are under-using it.
- **World-state building blocks** — compact war context (`_build_compact_war_context`), race standings (`_extract_race_standings_summary`), insight layer (`_build_river_race_insight_layer`), player insight (`_build_player_insight_context`) already exist in `runtime/jobs/_signals.py`. They are scattered across outcome-context branches; they want to be a single "situation" assembler.
- **Classifier** — `storage/player.py:_battle_mode_group` already emits `war`, `ranked`, `ladder`, `special_event`, `friendly`, `other`. This is the seed of channel-lane routing.
- **Durable memory** — subagents already have channel-scoped durable memory, which lets an agentic loop remember "I covered this angle last tick."

---

## Target state — the Awareness Tick

Replace `plan_signal_outcomes()` + N parallel `_deliver_signal_outcome()` LLM calls with **one agent turn per heartbeat tick** (with optional per-channel post fan-out as tool-driven sub-steps).

### One situation object, built once per tick

At the end of each heartbeat tick, assemble a single `Situation` payload and hand it to the awareness agent:

- **Time awareness**: `phase` (battle/practice/offseason), `day_number` / `day_total`, `hours_remaining_in_day`, `hours_until_war_reset`, `is_final_battle_day`, `is_colosseum_week`. Already computed — lift out of checkpoint scope.
- **Standing**: clan rank, fame, deficit to leader, pace_status, engagement %, untouched count. Already computed in `_build_river_race_insight_layer`.
- **Since-last-tick**: all signals accumulated since the last awareness run, grouped by lane (war / battle-mode / milestone / clan-event / leadership). Raw signals, not pre-enriched per-channel.
- **Channel memory**: per-channel last N posts with timestamps + summaries, so the agent knows what it has already said. Already available.
- **Roster vitals**: compact 20-row table of most-active members this week (fame, battles, trophies delta). Used as scouting anchor, not posted verbatim.

### The agent's job is not "write a post" — it is "decide"

The loop prompt inverts today's framing. Instead of *"Here is signal X for channel Y — write it"*, it is *"Here is the clan's current situation. What, if anything, is worth saying right now, and where?"*

The agent returns a structured plan: zero-or-more posts, each with `channel`, `tone`, `leads_with`, and a draft body. A post is allowed to cite tool-use evidence. Silence is an allowed, expected outcome.

Give it the full read-tool set **plus** `cr_api`, with ~8 tool rounds. That lets it:

- Resolve streak-player opponents via `cr_api(aspect="player_battles")` → `cr_api(aspect="player")` and comment on who they actually beat.
- Scout an inbound war opponent with `cr_api(aspect="clan_war")` without needing the separate scheduled `intel_report`.
- Check its own memory (`get_member → memories`) before reposting an angle it covered 3 hours ago.
- Read race standings via `get_river_race` / `get_war_member_standings` when the signal dict isn't enough.

### Why this beats per-signal generation

- **Deeper posts.** A hot-streak post can open with "8-in-a-row, and three of those were against 7K+ trophy opponents on 3.2-elixir cycle decks" instead of "8-in-a-row, nice." The ceiling moves from "what we pre-enriched" to "what the model can find."
- **Coherent timing.** When three things land in the same tick (war day transition + a card unlock + a leave), the agent sequences them rather than firing three independent posts in race-condition order. It also *can choose to hold* a minor milestone if the war narrative is more important this tick.
- **Genuine silence.** When nothing material has changed, the agent says nothing. Today's router forces a post per routable signal.
- **Fewer prompts to maintain.** Per-channel prompt forks collapse into one loop prompt + channel lane guidance. The "river-race is about momentum, player-progress is about celebration" rules become voice notes in a single persona file, not hard-coded context branches.

---

## Channel reorganization

Today's model mixes two orthogonal axes: **signal source** (battle vs. milestone vs. roster) and **channel**. Split on the first axis and let the agent pick tone within a channel.

| Channel | Scope | Signals |
|---|---|---|
| **#river-race** (keep) | Clan Wars only | `war_*`, race checkpoints, rank changes, day transitions, week/season complete |
| **#trophy-road** (new) | Battle-mode activity outside war | `battle_hot_streak`, `battle_trophy_push`, `path_of_legend_promotion`, future Classic/Grand Challenge + Global Tournament finishes, future Ultimate Champion |
| **#player-progress** (narrowed) | Durable player milestones | `arena_change`, `player_level_up`, `career_wins_milestone`, `new_card_unlocked`, `new_champion_unlocked`, `card_level_milestone`, `badge_earned`, `badge_level_milestone`, `achievement_star_milestone` |
| **#clan-events** (keep) | Roster / community | `member_join`, `member_leave`, `elder_promotion`, birthdays, anniversaries, tournament signals |
| **#leader-lounge** (keep) | Leadership-only | ops notes, rank swings, at-risk, kicks — read/write tools |
| **#announcements** (keep) | System / weekly | capability unlocks, weekly recap |

**Rationale for the split.** Durable milestones are *celebratory and infrequent* ("Sarah hit King Level 50"). Trophy Road / Path of Legends / ladder activity is *narrative and volatile* ("three-person push tonight, Alex is 140 trophies off his season best"). Mixing them makes both read worse. Everything in `#trophy-road` is discardable tomorrow; everything in `#player-progress` belongs in your long-term story. The existing `_classify_battle` / `_battle_mode_group` classifier is the natural router.

**What about new battle types?** Once #trophy-road exists, adding Classic Challenge finishes, Global Tournament placements, or evolution unlocks is additive — not a re-architecture.

---

## Tradeoffs

- **Cost.** Sonnet tool-loop every 30 min vs. today's "only if there are signals to post" per-signal calls. Mitigation: tick fast-path — if the situation diff is empty (no new signals, no time-to-meaningful-boundary, no new posts warranted) the agent call is skipped by a cheap pre-check, same way `_clan_awareness_tick` early-returns today.
- **Determinism and testability.** Signal-to-post is no longer mechanical. Mitigation: (a) hard-post floors — certain signal types (`war_battle_rank_change`, `member_join`, `capability_unlock`) are guaranteed to produce a post, the agent chooses framing not existence; (b) snapshot tests on the situation object, evaluator tests on the post plan.
- **CR API rate limiting.** Multi-tool fan-out across channels per tick could pressure the API. Mitigation: existing 5-call-per-turn cap + existing TTL cache on cr_api; budget is adequate. Per-player CR lookups on streaks are naturally bounded to 1–2 per tick.
- **Channel concurrency.** One agent posting to 3 channels in a tick needs ordered, non-overlapping Discord writes. Mitigation: existing signal delivery already handles this — the awareness agent emits a post plan, the delivery layer still posts sequentially with the same idempotency guarantees.
- **Model confusion / off-lane posts.** Giving the model N channels + free choice risks wrong-channel posts. Mitigation: channel lane rules live in the prompt, and the post plan's `channel` field is validated against an allowlist that follows signal-family hints (e.g., a draft that leads with war content cannot ship to `#trophy-road`).
- **Retention of existing sub-workflows.** `weekly_digest`, `intel_report`, `clanops`, `deck_review`, interactive Q&A — these stay as-is. The awareness loop replaces only the *proactive channel_update* path.

---

## Incremental path (four shippable phases)

1. **Surface time awareness.** Extract `hours_remaining_in_day`, `hours_until_war_reset`, day/phase, colosseum flag into a reusable `situation.time` block and expose to existing `channel_update` context. Already mostly computed in `heartbeat/_war.py`. Low risk, immediate uplift for river-race posts.
2. **Give `channel_update` real tools.** Move `channel_update` from `READ_TOOLS_NO_EXTERNAL` to `READ_TOOLS` (include `cr_api`), bump rounds from 3 to 6. Re-prompt to say "investigate before you post." This alone unlocks deeper streak commentary without touching the loop structure. (`agent/tool_policy.py:46–48, 60–63`.)
3. **Channel reorg.** Introduce `#trophy-road`. Extend `PROGRESSION_SIGNAL_TYPES` routing to split battle-mode signals (`battle_hot_streak`, `battle_trophy_push`, `path_of_legend_promotion`) onto the new channel. Narrow `#player-progress` to durable milestones. Update `CHANNEL_SUBAGENT_CONFIG` and `plan_signal_outcomes`.
4. **Awareness tick replaces the router.** Build the `Situation` assembler, swap `plan_signal_outcomes` + per-outcome `generate_channel_update` for a single `run_awareness_tick()`. Keep the hard-post floors. Retire per-channel outcome-context builders. Per-tick post plan drives Discord delivery.

Phases 1–3 each ship value independently. Phase 4 is the architectural payoff and depends on the prior three.

---

## Critical files

- `heartbeat/__init__.py:89` — heartbeat tick; where situation assembly attaches.
- `heartbeat/_war.py:470–518` — time-remaining, engagement %, pace; lift into `situation.time` / `situation.standing`.
- `runtime/jobs/_signals.py:238–326` — today's per-channel context builders; source material for a unified situation assembler.
- `runtime/channel_subagents.py:11–131, 250–315` — signal-family constants and the routing planner; target of the channel reorg and of the awareness-tick replacement.
- `agent/tool_policy.py:45–74` — toolset and round caps per workflow; edited in phases 2 and 4.
- `agent/tool_defs.py:432` — `cr_api` tool; the capability unlocked for channel posts.
- `storage/player.py:420–448` — `_classify_battle` / `_battle_mode_group`; the seed for the #trophy-road lane.
- `prompts/DISCORD.md` and `prompts/subagents/*.md` — channel persona definitions; updated for the new channel and the awareness-loop voice.

---

## Verification signals (how we'll know it's working)

- Streak posts cite specific opponents, trophies beaten, or archetypes faced — content that was previously impossible.
- River-race posts reference hours-remaining narratively ("six hours left and we're 180 fame behind") without waiting for a checkpoint trigger.
- Quiet ticks produce zero posts across all channels when nothing warrants attention — verifiable from Discord timestamps.
- `#trophy-road` carries only battle-mode activity; `#player-progress` carries only durable milestones. No overlap over a full week.
- One awareness-tick LLM call per heartbeat (plus any sub-tool calls) in logs, instead of N per-signal channel_update calls.
