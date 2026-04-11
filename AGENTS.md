# Elixir Bot

Discord bot for the POAP KINGS Clash Royale clan (#J2RGCRVG). Uses discord.py plus Anthropic Claude model routing:
- chat workflows default to `claude-sonnet-4-6`
- site/content workflows default to `claude-sonnet-4-6`
- promotion workflows default to `claude-sonnet-4-6`
- interactive/reception workflows default to `claude-haiku-4-5-20251001`
- observation workflows default to `claude-haiku-4-5-20251001`

`AGENTS.md` is the single source of truth for repository-specific instructions and architecture notes.

## Project Structure

- `elixir.py` — Main bot: Discord events, APScheduler, channel routing
- `elixir_agent.py` — Stable public LLM entrypoint; routes observation, channel replies, and site generation through the `agent/` package
- `cr_api.py` — Clash Royale API client (clan roster, war status, river race log)
- `heartbeat.py` — API-driven signal detection for clan, war, and progression events
- `db/` — SQLite data store package: identity, memory, current state, analytics, war, and raw payload capture
- `cr_knowledge.py` — Static Clash Royale + POAP KINGS game knowledge
- `prompts.py` — Loads and caches external prompt/config files from `prompts/`
- `prompts/subagents/` — Channel-named subagent prompt files
- `modules/poap_kings/` — POAP KINGS-specific site integration and GitHub publishing
- `modules/card_training/` — Elixir University card training quiz: question generation, Discord views, and quiz storage
- `scripts/review_agent_feedback.py` — Review recent LLM/channel failures and `#ask-elixir` feedback from SQLite for debugging and prompt/tool routing analysis
- `runtime/activities.py` — Canonical registry for recurring automated activities
- `runtime/channel_subagents.py` — Signal outcome planning, channel-targeted delivery, and subagent memory context
- `runtime/channel_router.py` — Discord message routing for interactive channels
- `storage/`, `agent/`, `runtime/` — Domain-first implementation packages for persistence, LLM behavior, and Discord runtime; root modules remain the stable public API surface

## Environment

- Python with venv (`source venv/bin/activate`)
- Requires `.env` with: DISCORD_TOKEN, CLAUDE_API_KEY, CR_API_KEY
- Non-secret config (channel IDs, clan tag) lives in `prompts/DISCORD.md` and `prompts/CLAN.md`
- Local start: `venv/bin/python elixir.py`
- Production process management uses `launchd`; see `SETUP.md`

## Running Tests

```bash
venv/bin/python -m pytest tests/ -v
```

Tests use in-memory SQLite and mocked external services (no API keys needed).

## Cleanup

```bash
venv/bin/python scripts/clean.py
venv/bin/python scripts/clean.py --db
```

- default: remove cache directories like `__pycache__` and `.pytest_cache`
- `--db`: also remove local runtime files like `elixir.db` and `elixir.pid`

## Database

SQLite at `elixir.db` (auto-created, gitignored). The project now uses the baseline schema defined in `_migration_0()` in `db/__init__.py`. The key tables are:

- Identity + metadata: `members`, `member_metadata`, `member_aliases`, `discord_users`, `discord_links`
- Conversation memory: `conversation_threads`, `messages`, `memory_facts`, `memory_episodes`, `channel_state`
- Prompt failure review: `prompt_failures`
- Clan/member state: `clan_memberships`, `member_current_state`, `member_state_snapshots`, `member_daily_metrics`
- Player analytics: `player_profile_snapshots`, `member_card_collection_snapshots`, `member_deck_snapshots`, `member_card_usage_snapshots`, `member_battle_facts`, `member_recent_form`
- War: `war_current_state`, `war_day_status`, `war_races`, `war_participation`
- Raw ingest + signals: `raw_api_payloads`, `signal_log`, `cake_day_announcements`

All `db` module functions accept an optional `conn` parameter — pass one in tests, omit in production.

### Migrations

Schema is managed by `_MIGRATIONS` list in `db/__init__.py` using `PRAGMA user_version`. To add a schema change:

1. Write a `_migration_N(conn)` function
2. Append it to `_MIGRATIONS`
3. Keep migrations additive unless you are intentionally resetting the database as a breaking change

Migrations run automatically in `get_connection()`. This repo currently treats the baseline schema as the clean foundation; additive migrations are fine, but breaking resets are acceptable when the model changes materially.

Current migrations:
- `_migration_0` — baseline schema
- `_migration_1` — prompt failure logging table for channel/reception LLM failures
- `_migration_2` — generated roster profile fields in `member_metadata`
- `_migration_3` — promote trusted join dates into metadata
- `_migration_4` — rename `member_metadata.joined_at_override` to `joined_at`
- `_migration_17` — signal detector cursors for forward-only war processing

## Site Content System

Elixir is the single authority for all dynamic data on poapkings.com. All Elixir-owned files use the `elixir-` prefix:

- `elixirClan.json` — Dynamic clan stats (memberCount, scores, donations, etc.)
- `elixirRoster.json` — Full roster with member data + bios + intro
- `elixirHome.json` — Home page message of the day
- `elixirMembers.json` — Members page weekly recap payload
- `elixirPromote.json` — Promotional messages (5 channels)

Filenames are camelCase (not hyphenated) because 11ty uses the filename stem as the data key — `elixirClan.json` becomes `elixirClan` in templates. The POAP KINGS integration publishes these files directly to GitHub paths in the site repo; local sibling-repo writes are now a legacy/dev-only path. `site.json` contains only static site config (url, joinUrl, discordUrl, tagline, clanTag).

Website publish visibility lives in `#poapkings-com`. Elixir posts there for real GitHub-backed publish outcomes:
- `site-content`
- `weekly-recap` site sync
- `promotion-content`
- manual non-preview runs that publish to GitHub

Success posts should include the commit SHA and direct GitHub commit URL. No-change publishes stay quiet.

## Subagent Architecture

Elixir now uses channel-named subagents: one Elixir identity, many focused lanes.

Core rule: one signal is not one post. A signal can fan out into multiple channel outcomes, each generated for the destination channel only.

Current primary subagents:
- `reception` — onboarding and verification
- `general` — mention-driven general Q&A
- `ask-elixir` — open-channel clan conversation
- `war-talk` — mention-driven tactical war Q&A
- `leader-lounge` — private leadership and clan operations
- `river-race` — war coordination and battle-day urgency
- `player-progress` — milestone and progression celebrations
- `clan-events` — joins, promotions, anniversaries, and clan recognitions
- `announcements` — weekly recap and clan-wide Elixir system updates
- `promote-the-clan` — recruiting copy for Discord and the website
- `poapkings-com` — website publish visibility only

## Recurring Activities

The canonical source of truth for scheduled automated work is `runtime/activities.py`, not scattered scheduler calls or prose docs.

Each activity declares:
- owner subagent
- purpose
- schedule
- executor function
- delivery targets
- whether manual triggering is allowed

Current recurring activities:
- **Every 30 minutes with up to 900s jitter, 24/7** — `clan-awareness` via `_clan_awareness_tick()`
  Processes non-war clan signals and routes outcomes into channels like `#clan-events` and `#leader-lounge`.
- **Every hour at :00 Chicago, no jitter** — `war-poll` via `_war_poll_tick()`
  Polls live River Race state and stores the hourly war snapshot pipeline.
- **Every hour at :05 Chicago, no jitter** — `war-awareness` via `_war_awareness_tick()`
  Reads stored war data, processes war-only signals, and owns scheduled River Race coordination across `#river-race` and optional leadership notes.
- **Every 30 minutes with up to 900s jitter** — `player-progression` via `_player_intel_refresh()`
  Refreshes stored player profile and battle intelligence, then emits progression signals to `#player-progress`.
- **Daily at 12:00 PM Chicago with up to 30 minutes jitter** — `daily-clan-insight` via `_ask_elixir_daily_insight()`
  Posts one short data-driven hidden fact in `#ask-elixir` when the data supports a genuinely interesting insight.
- **Friday 7:00 PM Chicago** — `leadership-review` via `_clanops_weekly_review()`
  Posts the weekly leadership review in `#leader-lounge`.
- **Monday 9:00 AM Chicago** — `weekly-recap` via `_weekly_clan_recap()`
  Posts the public weekly clan recap and syncs the members-page payload to the website.
- **6:00 PM Chicago** — `site-content` via `_site_content_cycle()`
  Refreshes daily POAP KINGS site content and publishes roster, clan, and home payloads to GitHub.
- **Friday 9:00 AM Chicago** — `promotion-content` via `_promotion_content_cycle()`
  Generates recruiting content for `#promote-the-clan` and the website promotion payload.

## Architecture: Prompts vs Code

Principle: **Prompts define what Elixir says and why. Code defines when, where, and how.**

### Prompt files (`prompts/`)

- `SOUL.md` — Elixir's persistent identity, stance, and non-human sense of self.
- `PURPOSE.md` — Elixir's mission, responsibilities, and guardrails.
- `GAME.md` — Clash Royale mechanics (game-generic, rarely changes).
- `CLAN.md` — Clan-specific identity, rules, history, and configurable thresholds (inactivity, promotion criteria, donation highlights, clan lore).
- `DISCORD.md` — Declarative Discord channel contract: IDs, subagents, workflows, reply policies, memory scope, and durable-memory flags.
- `subagents/*.md` — Channel-named behavior prompts for each subagent.

### What stays in code

Activity scheduling, channel routing, signal detection, outcome fan-out, delivery dedupe, tool execution, JSON response contracts, memory enforcement, nickname matching, LLM parameters, and Elixir data normalization.

## Memory Model

Elixir uses two memory layers:
- conversational memory in identity/message storage (`discord_user`, `member`, `channel`)
- durable scoped memory in contextual memory (`public`, `leadership`, `system_internal`)

Important rules:
- channel subagents read destination-channel conversational context, not a global blended chat history
- public subagents read public durable memory only
- `leader-lounge` can read public plus leadership durable memory
- `reception` should stay focused on onboarding context and avoid unrelated clan-event noise
- one source signal can create multiple channel outcomes, but durable memory records must stay scope-safe and must not let leadership copy overwrite public memory
- outcome delivery state is tracked separately from signal detection so retries can target only failed destinations

## Agent Loop Guardrails (Current)

- Tool policy is enforced in code per workflow (not prompt-only):
  - `observation` -> read tools only
  - `channel_update` -> read tools only
  - `channel_update_leadership` -> read tools only
  - `interactive` -> read tools only
  - `clanops` -> read + write tools
  - `reception` -> no tools
  - `roster_bios` -> read tools only
- Write tools are gated by workflow policy and `CLANOPS_WRITE_TOOLS_ENABLED` (default enabled for `clanops` only).
- Tool outputs are wrapped in a compact envelope (`ok`, `error`, `truncated`, `meta`, `data`) and truncated for context budget safety.
- Leader/member factual answers should prefer structured query tools over clipped roster context. Resolve members by name/Discord handle before using tag-based tools when needed.
- Strict JSON workflow contracts are validated in code with one repair retry:
  - `observation`: requires `event_type`, `summary`, `content` (or `null`)
  - `channel_update` / `channel_update_leadership` / `interactive` / `clanops`: require `event_type`, `summary`, `content`
  - `clanops` `channel_share` responses also require `share_content`
  - `reception`: requires `event_type=reception_response` and `content`
  - `roster_bios`: requires `intro` and `members` map
- Loop telemetry is logged per request: workflow, tool rounds, tools called, denied tools, validation failures, prompt/completion size estimates, and completion latencies.
- Channel/reception failures are also persisted in `prompt_failures` with the cleaned question text, workflow, failure type/stage, Discord metadata, result preview/raw JSON, and the last LLM error/model snapshot.
- Reply behavior is enforced in code from channel config:
  - `mention_only` for channels like `#general`, `#war-talk`, and `#leader-lounge`
  - `open_channel` for `#ask-elixir`
  - `disabled` for notification-only channels like `#poapkings-com`, `#river-race`, and `#announcements`

### Agent Feedback Review

Use the stored prompt-failure log and `#ask-elixir` feedback records when a Discord request fails, falls back, returns unusable output, or gets a thumbs-down:

```bash
venv/bin/python scripts/review_agent_feedback.py --limit 20
venv/bin/python scripts/review_agent_feedback.py --workflow clanops --json
```

- text mode is for quick local triage
- `--json` is the format to hand to Codex or Claude for “what failed and what should we change?” review

## Context Budgeting (Current)

- Roster context is clipped in `_clan_context()` to avoid prompt bloat.
- Defaults:
  - chat workflows use `MAX_CONTEXT_MEMBERS_DEFAULT` (30)
  - site generation uses `MAX_CONTEXT_MEMBERS_FULL` (50)
- When clipping occurs, context includes an omitted-members summary line.

## Heartbeat Tick Contract (Current)

- `heartbeat.tick()` returns a `HeartbeatTickResult` bundle:
  - `signals`
  - `clan`
  - `war`
- `elixir._clan_awareness_tick()` consumes this bundle and does not re-fetch clan/war in the same cycle.
- `elixir._war_awareness_tick()` uses the same bundle shape but requests war-only signal detection.

## System Signals

One-time capability or upgrade announcements should use the queued `system_signals` path, not an ad hoc Discord post.

- Define startup-seeded system signals in `runtime/system_signals.py`
- Add one entry to `STARTUP_SYSTEM_SIGNALS` with:
  - stable `signal_key`
  - `signal_type` such as `capability_unlock`
  - `payload` fields the channel-update workflow can talk about, including `audience` when the update is meant for the clan
- Startup queues these signals idempotently via `queue_startup_system_signals()`
- Clan-awareness surfaces pending signals through the normal channel-update routing flow and marks them announced after a successful post
- Elixir also posts a separate startup check-in to the leadership workflow with the running build hash and a short Clash Royale-flavored line

This keeps feature announcements discoverable: future changes should usually mean “edit one list” instead of remembering startup-hook details.

## Query Layer (Current)

Elixir’s core member/leader questions should be answered from structured query helpers and tools, not prompt reconstruction. The LLM has 15 domain-aligned tools organized into four groups:

- **Member domain**: `resolve_member`, `get_member` (include: profile, form, war, trend, deck, cards, history, memories, chests), `get_member_war_detail` (aspect: summary, attendance, battles, missed_days, vs_clan_avg)
- **River Race domain**: `get_river_race` (live race state + competing clan standings), `get_war_season` (aspect: summary, standings, win_rates, boat_battles, score_trend, season_comparison, trending, perfect_attendance, no_participation), `get_war_member_standings` (metric: fame, win_rate, attendance)
- **Clan domain**: `get_clan_roster` (aspect: list, summary, recent_joins, longest_tenure, role_changes, max_cards), `get_clan_health` (aspect: at_risk, hot_streaks, losing_streaks, trophy_drops, promotion_candidates), `get_clan_trends`
- **Utility**: `lookup_cards`, `get_player_details`, `update_member`, `save_clan_memory`

War tools include `war_player_type` (regular/occasional/rare/never) per member. Leadership evaluations include CR account age. Sensitive aspects (at_risk, promotion_candidates) are gated to leadership workflows at execution time.

### Mostly LLM

Almost every message Elixir sends is LLM-generated. Events, scheduled activities, website publish notices, and channel replies pass context to the LLM, which crafts the message using Elixir's identity from `SOUL.md` + `PURPOSE.md`, channel contract from `DISCORD.md`, and lane behavior from `subagents/*.md`.

Exception: preauthored system-signal announcements may be written directly in code and delivered without LLM rewriting when deterministic wording matters.

### Portability

A new clan forks elixir-bot and primarily rewrites `CLAN.md` and `DISCORD.md`, plus any subagent prompts that reflect their own server culture. `SOUL.md`, `PURPOSE.md`, and `GAME.md` should stay mostly portable.

### Future work

- startup linting for subagent config, reply policy, and activity registry consistency outside the bot runtime

## Key Conventions

- All times in America/Chicago timezone
- Clan tag: J2RGCRVG (POAP KINGS)
- POAP KINGS site content publishes to `src/_data/elixir*.json` in the configured GitHub site repo
