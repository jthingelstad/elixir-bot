# Elixir Bot

Discord bot for the POAP KINGS Clash Royale clan (#J2RGCRVG). Uses discord.py + OpenAI GPT-4o.

## Project Structure

- `elixir.py` — Main bot: Discord events, APScheduler, channel routing
- `elixir_agent.py` — LLM engine: observation + leader Q&A via GPT-4o
- `cr_api.py` — Clash Royale API client (clan roster, war status, river race log)
- `heartbeat.py` — Hourly signal detection (milestones, joins/leaves, war transitions)
- `db.py` — SQLite history store (member snapshots, war results)
- `cr_knowledge.py` — Static Clash Royale + POAP KINGS game knowledge
- `journal.py` — Append-only JSON log committed to sibling poapkings.com repo

## Channels

- **#elixir** — Broadcast only. Elixir posts here but never responds to messages.
- **#leader-lounge** — Interactive. Leaders @Elixir with questions.

## Environment

- Python with venv (`source venv/bin/activate`)
- Requires `.env` with: DISCORD_TOKEN, OPENAI_API_KEY, CR_API_KEY
- Non-secret config (channel IDs, clan tag) lives in `prompts/DISCORD.md` and `prompts/CLAN.md`
- Start with `./run.sh`

## Running Tests

```
pytest tests/ -v
```

Tests use in-memory SQLite and mocked external services (no API keys needed).

## Key Conventions

- All times in America/Chicago timezone
- Clan tag: J2RGCRVG (POAP KINGS)
- SQLite DB: `elixir.db` (auto-created on first run)
- Journal entries go to `../poapkings.com/src/_data/elixir.json`
