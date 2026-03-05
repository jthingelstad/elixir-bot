# Elixir Bot — Setup & Operations

Operations guide for agents running the bot in production.

## Install

```bash
cd elixir-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Verify the install:

```bash
pytest tests/ -v
```

All 130 tests use in-memory SQLite and mocked services — no API keys or network needed. If tests pass, the install is good.

## Configure

Create a `.env` file in the project root.

**Required** (secrets):

```env
DISCORD_TOKEN=your_discord_bot_token
OPENAI_API_KEY=your_openai_api_key
CR_API_KEY=your_clash_royale_api_key
```

**Optional** (defaults shown):

```env
POAPKINGS_REPO_PATH=../poapkings.com               # Sibling repo for journal entries
HEARTBEAT_START_HOUR=7                              # Start hour (Chicago time)
HEARTBEAT_END_HOUR=22                               # End hour (Chicago time)
EDITORIAL_HOUR=20                                   # Daily editorial hour (Chicago time)
ELIXIR_DB_PATH=./elixir.db                          # SQLite database path
```

Non-secret config (Discord channel IDs, guild ID, clan tag) lives in prompt files checked into the repo:

- `prompts/DISCORD.md` — Channel behaviors and Discord IDs (`## Config` section)
- `prompts/CLAN.md` — Clan tag, rules, thresholds

## Process Management (launchd)

The bot runs as a **launchd** service, not manually via `python elixir.py`. The plist points at `run.sh`, which activates the venv and execs `python elixir.py`.

### Starting and stopping

```bash
# Stop the bot
launchctl unload ~/Library/LaunchAgents/com.poapkings.elixir.plist

# Start the bot
launchctl load ~/Library/LaunchAgents/com.poapkings.elixir.plist
```

### Verify it's running (single process)

```bash
pgrep -f "python elixir.py"
```

This should return **exactly one** PID. Zero means it's not running. More than one means duplicates — unload, kill all, then load again:

```bash
launchctl unload ~/Library/LaunchAgents/com.poapkings.elixir.plist
pkill -f "python elixir.py"
launchctl load ~/Library/LaunchAgents/com.poapkings.elixir.plist
```

## Deployment (Updating)

Canonical update procedure:

```bash
# 1. Stop
launchctl unload ~/Library/LaunchAgents/com.poapkings.elixir.plist

# 2. Update
cd ~/Projects/elixir-bot
git pull

# 3. Reinstall dependencies (safe to always run; fast if nothing changed)
source venv/bin/activate
pip install -r requirements.txt

# 4. Start
launchctl load ~/Library/LaunchAgents/com.poapkings.elixir.plist

# 5. Verify
pgrep -f "python elixir.py"
```

Database migrations run automatically on startup — no manual schema changes needed.

## Logs

The bot logs to **stdout/stderr** only. launchd captures this — check the plist for `StandardOutPath` / `StandardErrorPath` to find the log file location.

### What normal looks like

```
Elixir online as Elixir#1234 🧪
Scheduler started — hourly heartbeat ...
Heartbeat: 3 signals detected, consulting LLM
Posted observation: ...
```

### What errors look like

```
Heartbeat error: ...
Heartbeat: failed to fetch clan data: ...
leader-lounge error: ...
```

### Checking heartbeat health

```bash
# Did the heartbeat fire recently?
grep "Heartbeat:" /path/to/log | tail -5

# Was anything posted?
grep "Posted observation:" /path/to/log | tail -5

# Any errors?
grep "ERROR" /path/to/log | tail -10
```

### Manual heartbeat test

This runs one heartbeat cycle against the live API (requires `.env` with valid keys):

```bash
cd ~/Projects/elixir-bot
source venv/bin/activate
python -c "import heartbeat; signals = heartbeat.tick(); print(f'{len(signals)} signals')"
```

This is safe — it reads from the API and writes to the local DB, but does **not** post to Discord.

## What's Stateful

### `elixir.db` (SQLite)

Lives at `ELIXIR_DB_PATH` (default: `./elixir.db` in the project root). Contains real clan data:

- Member snapshots (90-day retention)
- War results and participation (180-day retention)
- Leader conversation memory (30-day retention)
- Join dates and birthdays (permanent)
- Cake day announcement dedup (7-day retention)

**Safe to delete** if you want a clean slate — the bot will recreate it on startup. But you lose all history. The file is gitignored.

### `elixir.log`

Only exists if launchd is configured to write stdout to a file. The bot doesn't rotate it. If it grows large, it's safe to truncate (`> elixir.log`) or delete while the bot is stopped.

### `../poapkings.com/src/_data/elixir.json`

Append-only journal. The bot writes entries here and git pushes. This is in a separate repo.
