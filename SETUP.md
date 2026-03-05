# Elixir Bot — Setup Guide

## Overview

Elixir is a Discord bot for the **POAP KINGS** Clash Royale clan (#J2RGCRVG). It uses discord.py for Discord integration and OpenAI GPT-4o for intelligence. It posts automated observations to #elixir and answers leader questions in #leader-lounge.

## Prerequisites

- Python 3.10+
- A Discord bot token (with `message_content` intent enabled)
- An OpenAI API key (GPT-4o access)
- A Clash Royale API key (from [developer.clashroyale.com](https://developer.clashroyale.com))
- The `poapkings.com` repo cloned as a sibling directory (for journal entries)

## Installation

```bash
# Clone the repo
git clone <repo-url> elixir-bot
cd elixir-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# For running tests
pip install pytest
```

### Dependencies (requirements.txt)

| Package | Purpose |
|---------|---------|
| `discord.py>=2.0` | Discord bot framework |
| `openai` | GPT-4o API client for LLM intelligence |
| `requests` | Clash Royale API HTTP calls |
| `python-dotenv` | Load `.env` file |
| `apscheduler` | Hourly heartbeat scheduler |
| `pytz` | Chicago timezone handling |

## Environment Variables

Create a `.env` file in the project root:

```env
# Required — secrets only
DISCORD_TOKEN=your_discord_bot_token
OPENAI_API_KEY=your_openai_api_key
CR_API_KEY=your_clash_royale_api_key

# Optional (defaults shown)
POAPKINGS_REPO_PATH=../poapkings.com               # Path to poapkings.com repo
HEARTBEAT_START_HOUR=7                              # Start hour (Chicago time)
HEARTBEAT_END_HOUR=22                               # End hour (Chicago time)
EDITORIAL_HOUR=20                                   # Daily editorial hour (Chicago time)
ELIXIR_DB_PATH=./elixir.db                          # SQLite database path
```

Non-secret configuration (Discord channel IDs, guild ID, clan tag) lives in prompt files:
- `prompts/DISCORD.md` — Channel behaviors and Discord IDs (`## Config` section)
- `prompts/CLAN.md` — Clan tag, rules, thresholds

## Running

```bash
source venv/bin/activate
python elixir.py
```

Or use `./run.sh` if present.

## Running Tests

```bash
source venv/bin/activate
pytest tests/ -v
```

Tests use in-memory SQLite and mock all external services (no API keys, Discord, or network needed). Currently 69 tests.

## Project Architecture

### Files

| File | Purpose |
|------|---------|
| `elixir.py` | Main bot entry point. Discord events, APScheduler hourly heartbeat, channel routing. |
| `elixir_agent.py` | LLM engine. GPT-4o with function calling (tool use). Handles observations and leader Q&A. |
| `heartbeat.py` | Hourly signal detection. Cheap deterministic checks before calling the LLM. |
| `db.py` | SQLite history store. Member snapshots, war results, conversation memory, War Champ tracking. |
| `cr_api.py` | Clash Royale API client. Clan roster, war status, river race log, player profiles. |
| `cr_knowledge.py` | Static game knowledge constants (war schedule, thresholds). Loads configurable values from prompt files. |
| `journal.py` | Append-only JSON log. Writes entries to `poapkings.com` repo and git pushes. |
| `announcements.py` | Legacy, unused. |

### How It Works

1. **Heartbeat** (`heartbeat.py`) runs every hour during active hours (7am-10pm Chicago):
   - Fetches live clan + war data from Clash Royale API
   - Snapshots member data to SQLite (change-only — skips if nothing changed)
   - Purges expired data (90 days for snapshots, 180 days for war, 30 days for conversations)
   - Runs signal detectors: joins/leaves, trophy milestones, arena changes, role changes, war day transitions, war deck usage, war completion, donation leaders (end of day only), inactivity
   - If signals found, passes them to the LLM to craft a Discord post
   - Join/leave signals get formatted posts directly (no LLM needed)

2. **Leader Q&A** (`elixir_agent.py`) when a leader @mentions Elixir in #leader-lounge:
   - Loads conversation history for that leader (SQLite-backed memory)
   - Saves the question, calls GPT-4o with tools, saves the response
   - LLM can call tools on demand: member history, war results, player details, War Champ standings, promotion candidates, perfect war participation

3. **Journal** (`journal.py`) appends observation entries to `poapkings.com/src/_data/elixir.json` and git pushes.

### Discord Channels

- **#elixir** — Broadcast only. Elixir posts observations here but never responds.
- **#leader-lounge** — Interactive. Leaders @Elixir with questions and get responses with conversation memory.

### Database (SQLite)

Auto-created at `elixir.db` on first run. Self-managing with automatic data expiration.

**Tables:**
- `member_snapshots` — Hourly roster snapshots (change-only). 90-day retention.
- `war_results` — River Race results per season/week. 180-day retention.
- `war_participation` — Per-member war fame/decks per race. Linked to war_results.
- `leader_conversations` — Q&A memory per leader. 30-day retention, 20 turns max.

### LLM Tools (Function Calling)

The LLM can call these tools during observations or leader Q&A:
- `get_member_history` — Trophy/donation trends over time
- `get_war_results` — Recent war outcomes
- `get_member_war_stats` — A member's war participation record
- `get_promotion_candidates` — Members who meet elder criteria
- `get_player_details` — Detailed player stats from CR API
- `get_war_champ_standings` — Season fame leaderboard
- `get_perfect_war_participants` — Members with 100% war attendance

### Clash Royale / Clan Knowledge

Key rules encoded in `cr_knowledge.py`:
- **War schedule**: Battle days Thu-Sun. Training Mon-Wed. 4 decks per day.
- **Seasons**: Identified as SEASON-WEEK (e.g., "130-1"). Currently season 130.
- **War Champ**: Top fame contributor each season wins a free Pass Royale.
- **Perfect participation**: Using all 4 decks every battle day all season = free Pass Royale.
- **Clan composition**: ~1 leader, 2-3 elders per 10 members.
- **Donations**: Highlighted once per day (end of day). Consistency drives elder promotion.
- **Elder promotion**: Consistent donations (50+/week), war participation, active in last 7 days, 2+ weeks tenure.

### Key Conventions

- All times in **America/Chicago** timezone
- Clan tag: `J2RGCRVG` (POAP KINGS)
- Member snapshot file: `member_snapshot.json` (tag-to-name map for join/leave detection)
- SQLite DB: `elixir.db` (gitignored, auto-created)
- Journal entries: `../poapkings.com/src/_data/elixir.json`
- `.env`, `venv/`, `__pycache__/`, `*.log`, `elixir.db` are all gitignored
