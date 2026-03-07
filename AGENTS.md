# Elixir Bot

Discord bot for the POAP KINGS Clash Royale clan (#J2RGCRVG). Uses discord.py + OpenAI GPT-4o.

`AGENTS.md` is the single source of truth for repository-specific instructions and architecture notes.

## Project Structure

- `elixir.py` — Main bot: Discord events, APScheduler, channel routing
- `elixir_agent.py` — LLM engine: observation + channel replies + site content generation via GPT-4o
- `cr_api.py` — Clash Royale API client (clan roster, war status, river race log)
- `heartbeat.py` — Hourly signal detection (milestones, joins/leaves, war transitions)
- `db.py` — SQLite V2 data store: identity, memory, current state, analytics, war, and raw payload capture
- `cr_knowledge.py` — Static Clash Royale + POAP KINGS game knowledge
- `prompts.py` — Loads and caches external prompt/config files from `prompts/`
- `site_content.py` — JSON content management for poapkings.com (write, validate, commit/push)
- `_*` extracted modules — Internal implementation splits for large files (`_db_*.py`, `_agent_*.py`, `_elixir_*.py`); root modules remain the stable public API surface

## Environment

- Python with venv (`source venv/bin/activate`)
- Requires `.env` with: DISCORD_TOKEN, OPENAI_API_KEY, CR_API_KEY
- Non-secret config (channel IDs, clan tag) lives in `prompts/DISCORD.md` and `prompts/CLAN.md`
- Start with `./run.sh`

## Running Tests

```bash
venv/bin/python -m pytest tests/ -v
```

Tests use in-memory SQLite and mocked external services (no API keys needed).

## Database

SQLite at `elixir.db` (auto-created, gitignored). The project now uses the V2 schema defined in `_migration_0()` in `db.py`. The key tables are:

- Identity + metadata: `members`, `member_metadata`, `member_aliases`, `discord_users`, `discord_links`
- Conversation memory: `conversation_threads`, `messages`, `memory_facts`, `memory_episodes`, `channel_state`
- Clan/member state: `clan_memberships`, `member_current_state`, `member_state_snapshots`, `member_daily_metrics`
- Player analytics: `player_profile_snapshots`, `member_card_collection_snapshots`, `member_deck_snapshots`, `member_card_usage_snapshots`, `member_battle_facts`, `member_recent_form`
- War: `war_current_state`, `war_day_status`, `war_races`, `war_participation`
- Raw ingest + signals: `raw_api_payloads`, `signal_log`, `cake_day_announcements`

All `db.py` functions accept an optional `conn` parameter — pass one in tests, omit in production.

### Migrations

Schema is managed by `_MIGRATIONS` list in `db.py` using `PRAGMA user_version`. To add a schema change:

1. Write a `_migration_N(conn)` function
2. Append it to `_MIGRATIONS`
3. Keep migrations additive unless you are intentionally resetting the database as a breaking change

Migrations run automatically in `get_connection()`. This repo currently treats V2 as the clean baseline; additive migrations are fine, but breaking resets are acceptable when the model changes materially.

Current migrations:
- `_migration_0` — V2 baseline schema

## Site Content System

Elixir is the single authority for all dynamic data on poapkings.com. All Elixir-owned files use the `elixir-` prefix:

- `elixirClan.json` — Dynamic clan stats (memberCount, scores, donations, etc.)
- `elixirRoster.json` — Full roster with member data + bios + intro
- `elixirHome.json` — Home page message of the day
- `elixirMembers.json` — Members page message of the day
- `elixirPromote.json` — Promotional messages (5 channels)

Filenames are camelCase (not hyphenated) because 11ty uses the filename stem as the data key — `elixirClan.json` becomes `elixirClan` in templates. JSON schemas live in `poapkings.com/src/_data/schemas/`. `site.json` contains only static site config (url, joinUrl, discordUrl, tagline, clanTag).

### Scheduled Jobs

- **8:00 AM Chicago** — `_site_data_refresh()`: Fetch CR API, build roster + clan data, commit/push
- **8:00 PM Chicago** — `_site_content_cycle()`: Refresh data, generate LLM bios/messages, commit/push
- **Every 6 hours by default** — `_player_intel_refresh()`: Refresh a stale subset of active members' player profiles and battle logs into the V2 analytics tables, and emit progression signals like level-ups and card milestones

## Architecture: Prompts vs Code

Principle: **Prompts define what Elixir says and why. Code defines when, where, and how.**

### Prompt files (`prompts/`)

- `PURPOSE.md` — Elixir's identity, voice, personality. Portable across any clan.
- `GAME.md` — Clash Royale mechanics (game-generic, rarely changes).
- `CLAN.md` — Clan-specific identity, rules, history, and configurable thresholds (trophy milestones, inactivity, promotion criteria, donation highlights, clan lore).
- `DISCORD.md` — Discord server structure, channel behaviors, config IDs.

### What stays in code

Channel routing, JSON response format contracts, tool definitions + execution, signal detection logic (reads thresholds from CLAN.md), conversation memory, scheduling, nickname matching, LLM parameters, and V2 data normalization.

## Agent Loop Guardrails (Current)

- Tool policy is enforced in code per workflow (not prompt-only):
  - `observation` -> read tools only
  - `interactive` -> read tools only
  - `clanops` -> read + write tools
  - `reception` -> no tools
  - `roster_bios` -> read tools only
- Write tools are gated by workflow policy and `CLANOPS_WRITE_TOOLS_ENABLED` (default enabled for `clanops` only).
- Tool outputs are wrapped in a compact envelope (`ok`, `error`, `truncated`, `meta`, `data`) and truncated for context budget safety.
- Leader/member factual answers should prefer V2 tools over clipped roster context. Resolve members by name/Discord handle before using tag-based tools when needed.
- Strict JSON workflow contracts are validated in code with one repair retry:
  - `observation`: requires `event_type`, `summary`, `content` (or `null`)
  - `interactive` / `clanops`: require `event_type`, `summary`, `content`; `channel_share` also requires `share_content`
  - `reception`: requires `event_type=reception_response` and `content`
  - `roster_bios`: requires `intro` and `members` map
- Loop telemetry is logged per request: workflow, tool rounds, tools called, denied tools, validation failures, prompt/completion size estimates, and completion latencies.

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
- `elixir._heartbeat_tick()` consumes this bundle and does not re-fetch clan/war in the same cycle.

## V2 Query Layer (Current)

Elixir’s core member/leader questions should be answered from V2 query helpers and tools, not prompt reconstruction. Important read paths include:

- member resolution: `resolve_member`
- roster summaries: `get_clan_roster_summary`, `list_clan_members`, `list_longest_tenure_members`, `list_recent_joins`
- member intelligence: `get_member_profile`, `get_member_recent_form`, `get_member_current_deck`, `get_member_signature_cards`
- war intelligence: `get_current_war_status`, `get_war_deck_status_today`, `get_member_war_status`, `get_war_season_summary`, `get_members_without_war_participation`, `compare_member_war_to_clan_average`
- trend/support signals: `get_trophy_drops`, `get_members_on_losing_streak`, `get_trending_war_contributors`, `get_members_at_risk`

### No templates — all LLM

Every message Elixir sends is LLM-generated. No hardcoded message templates. Events (joins, leaves, nickname matches, role grants) pass context to the LLM, which crafts the message using its voice from PURPOSE.md and channel context from DISCORD.md.

### Portability

A new clan forks elixir-bot and only rewrites CLAN.md (their clan name, tag, rules, thresholds) and DISCORD.md (their server layout, channel IDs). PURPOSE.md and GAME.md stay mostly the same.

### Future work

- channel-role config should eventually support hot reload or startup lint tooling outside the bot runtime

## Key Conventions

- All times in America/Chicago timezone
- Clan tag: J2RGCRVG (POAP KINGS)
- Site content goes to `../poapkings.com/src/_data/elixir*.json`
