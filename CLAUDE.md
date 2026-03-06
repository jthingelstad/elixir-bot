# Elixir Bot

Discord bot for the POAP KINGS Clash Royale clan (#J2RGCRVG). Uses discord.py + OpenAI GPT-4o.

## Project Structure

- `elixir.py` — Main bot: Discord events, APScheduler, channel routing
- `elixir_agent.py` — LLM engine: observation + leader Q&A + site content generation via GPT-4o
- `cr_api.py` — Clash Royale API client (clan roster, war status, river race log)
- `heartbeat.py` — Hourly signal detection (milestones, joins/leaves, war transitions)
- `db.py` — SQLite history store; versioned migrations via `PRAGMA user_version`
- `cr_knowledge.py` — Static Clash Royale + POAP KINGS game knowledge
- `prompts.py` — Loads and caches external prompt/config files from `prompts/`
- `site_content.py` — JSON content management for poapkings.com (write, validate, commit/push)

## Environment

- Python with venv (`source venv/bin/activate`)
- Requires `.env` with: DISCORD_TOKEN, OPENAI_API_KEY, CR_API_KEY
- Non-secret config (channel IDs, clan tag) lives in `prompts/DISCORD.md` and `prompts/CLAN.md`
- Start with `./run.sh`

## Running Tests

```bash
pytest tests/ -v
```

Tests use in-memory SQLite and mocked external services (no API keys needed).

## Database

SQLite at `elixir.db` (auto-created, gitignored). Schema is defined in `_migration_0()` in `db.py`. Tables: `member_snapshots`, `war_results`, `war_participation`, `leader_conversations`, `member_dates`, `cake_day_announcements`. All `db.py` functions accept an optional `conn` parameter — pass one in tests, omit in production.

### Migrations

Schema is managed by `_MIGRATIONS` list in `db.py` using `PRAGMA user_version`. To add a schema change:

1. Write a `_migration_N(conn)` function
2. Append it to `_MIGRATIONS`
3. Use `_add_column_if_not_exists()` for adding columns

Migrations run automatically in `get_connection()`. Existing data is always preserved.

Current migrations:
- `_migration_0` — Initial schema (all tables)
- `_migration_1` — Add `profile_url`, `poap_address`, `note` columns to `member_dates`

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

## Architecture: Prompts vs Code

Principle: **Prompts define what Elixir says and why. Code defines when, where, and how.**

### Prompt files (`prompts/`)

- `PURPOSE.md` — Elixir's identity, voice, personality. Portable across any clan.
- `GAME.md` — Clash Royale mechanics (game-generic, rarely changes).
- `CLAN.md` — Clan-specific identity, rules, history, and configurable thresholds (trophy milestones, inactivity, promotion criteria, donation highlights, clan lore).
- `DISCORD.md` — Discord server structure, channel behaviors, config IDs.

### What stays in code

Channel routing, JSON response format contracts, tool definitions + execution, signal detection logic (reads thresholds from CLAN.md), conversation memory, scheduling, nickname matching, LLM parameters.

### No templates — all LLM

Every message Elixir sends is LLM-generated. No hardcoded message templates. Events (joins, leaves, nickname matches, role grants) pass context to the LLM, which crafts the message using its voice from PURPOSE.md and channel context from DISCORD.md.

### Portability

A new clan forks elixir-bot and only rewrites CLAN.md (their clan name, tag, rules, thresholds) and DISCORD.md (their server layout, channel IDs). PURPOSE.md and GAME.md stay mostly the same.

### Future work

- `leader_share` is currently specific to #leader-lounge → #elixir. Should become a general-purpose "post to broadcast" tool available in any interactive channel.

## Key Conventions

- All times in America/Chicago timezone
- Clan tag: J2RGCRVG (POAP KINGS)
- Site content goes to `../poapkings.com/src/_data/elixir*.json`
