# Elixir Bot

Discord bot for the POAP KINGS Clash Royale clan (#J2RGCRVG). Uses discord.py + OpenAI GPT-4o.

## Project Structure

- `elixir.py` — Main bot: Discord events, APScheduler, channel routing
- `elixir_agent.py` — LLM engine: observation + leader Q&A via GPT-4o
- `cr_api.py` — Clash Royale API client (clan roster, war status, river race log)
- `heartbeat.py` — Hourly signal detection (milestones, joins/leaves, war transitions)
- `db.py` — SQLite history store; versioned migrations via `PRAGMA user_version`
- `cr_knowledge.py` — Static Clash Royale + POAP KINGS game knowledge
- `prompts.py` — Loads and caches external prompt/config files from `prompts/`
- `journal.py` — Append-only JSON log committed to sibling poapkings.com repo

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

## Key Conventions

- All times in America/Chicago timezone
- Clan tag: J2RGCRVG (POAP KINGS)
- Journal entries go to `../poapkings.com/src/_data/elixir.json`
