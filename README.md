# Elixir 🧪 — POAP KINGS Discord Bot

LLM-powered clan management bot for **POAP KINGS**, a Clash Royale clan (#J2RGCRVG). Uses discord.py + OpenAI GPT-4o with function calling for intelligent, context-aware responses.

## What Elixir Does

- **Hourly heartbeat**: Fetches clan data, detects changes (milestones, joins/leaves, war status, deck usage), and posts about what matters — skips when nothing interesting happened
- **Leader Q&A**: Leaders @mention Elixir in #leader-lounge for promotion advice, war analysis, player lookups, and more — with conversation memory
- **War tracking**: Monitors deck usage on battle days (Thu-Sun), tracks War Champ standings across seasons, and celebrates perfect participation
- **Member history**: SQLite-backed snapshots track trophy progression, donations, arena changes, and role promotions over time

## Channels

| Channel | Behavior |
|---------|----------|
| **#elixir** | Broadcast only. Elixir posts observations here but never responds. |
| **#leader-lounge** | Interactive. Leaders @Elixir with questions and get responses with conversation memory. |

## Quick Start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in your tokens
python elixir.py
```

See [SETUP.md](SETUP.md) for full setup instructions, environment variables, architecture details, and database schema.

## Running Tests

```bash
pytest tests/ -v
```

69 tests — all use in-memory SQLite and mocked external services. No API keys needed.

## Project Structure

| File | Purpose |
|------|---------|
| `elixir.py` | Main bot: Discord events, APScheduler heartbeat, channel routing |
| `elixir_agent.py` | LLM engine: GPT-4o with function calling for observations + leader Q&A |
| `heartbeat.py` | Hourly signal detection: milestones, war decks, joins/leaves, donations |
| `db.py` | SQLite history store: member snapshots, war results, conversations, War Champ |
| `cr_api.py` | Clash Royale API client: clan roster, war status, river race log, player profiles |
| `cr_knowledge.py` | Static game + clan knowledge injected into LLM system prompt |
| `journal.py` | Append-only JSON log committed to sibling poapkings.com repo |

## Key Features

- **Signal-based posting** — Only calls the LLM when there are real things to talk about (trophy milestones, war completions, new members, etc.)
- **Agentic tool use** — LLM can query member history, war stats, promotion candidates, War Champ standings, and player profiles on demand
- **Conversation memory** — Remembers prior leader Q&A exchanges (30-day retention)
- **War Champ tracking** — Aggregates fame per member across a 4-5 week season; weekly rankings shared to the clan
- **Perfect participation** — Tracks members who use all 4 decks every battle day all season
- **Deck usage monitoring** — On battle days, thanks players who used their decks and nudges those who haven't
- **Self-managing database** — Change-only snapshots, automatic data expiration (90/180/30 day retention)
- **Clan composition awareness** — Understands the target ratio of leaders, elders, and members

## Clan Knowledge

Elixir knows POAP KINGS rules: war schedule (Thu-Sun battle days, 4 decks/day), season format (e.g., "130-1"), War Champ rewards, perfect participation rewards, elder promotion criteria, clan composition targets, and donation expectations. See `cr_knowledge.py`.
