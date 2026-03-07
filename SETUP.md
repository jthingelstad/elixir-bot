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

All 121 tests use in-memory SQLite and mocked services — no API keys or network needed. If tests pass, the install is good.

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
POAPKINGS_REPO_PATH=../poapkings.com               # Sibling repo for site content
HEARTBEAT_START_HOUR=7                              # Start hour (Chicago time)
HEARTBEAT_END_HOUR=22                               # End hour (Chicago time)
SITE_CONTENT_HOUR=20                                # Evening content cycle hour (Chicago time)
ELIXIR_DB_PATH=./elixir.db                          # SQLite database path
```

Non-secret config (Discord channel IDs, guild ID, clan tag) lives in prompt files checked into the repo:

- `prompts/DISCORD.md` — Channel behaviors, per-channel routing, singleton channel roles, and Discord IDs
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
Elixir online as Elixir#1234
Scheduler started — hourly heartbeat ...
Heartbeat: 3 signals detected, consulting LLM
Posted observation: ...
```

### What errors look like

```
Heartbeat error: ...
Heartbeat: failed to fetch clan data: ...
leader-lounge error: ...
prompt_failure id=42 workflow=clanops type=agent_none stage=respond_in_channel ...
```

Prompt failures are intentionally loud in the log now. Each `prompt_failure` line corresponds to a persisted row in SQLite with the prompt text, failure metadata, result preview, and last OpenAI error/model snapshot.

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
python -c "import heartbeat; result = heartbeat.tick(); print(f'{len(result.signals)} signals')"
```

This is safe — it reads from the API and writes to the local DB, but does **not** post to Discord.

## Reviewing Prompt Failures

Use this when Elixir says it hit an error, falls back with a weak response, or silently fails to produce a clean answer.

```bash
cd ~/Projects/elixir-bot
source venv/bin/activate
venv/bin/python scripts/review_prompt_failures.py --limit 20
venv/bin/python scripts/review_prompt_failures.py --workflow clanops --json
```

- default output is human-readable triage
- `--workflow` narrows to `clanops`, `interactive`, or `reception`
- `--json` is the format to paste into Codex or Claude for review

The backing data lives in the `prompt_failures` table inside `elixir.db`.

## Cleanup

Remove transient local cruft:

```bash
venv/bin/python scripts/clean.py
```

Remove caches plus local runtime files like `elixir.db` and `elixir.pid`:

```bash
venv/bin/python scripts/clean.py --db
```

## What's Stateful

### `elixir.db` (SQLite)

Lives at `ELIXIR_DB_PATH` (default: `./elixir.db` in the project root). Contains real clan data in the V2 schema:

- Member identity, metadata, and clan membership history
- Current member state, daily metrics, and player analytics
- War state, war participation, and battle facts
- Conversation memory and channel state
- Prompt failure review records for failed/fallback LLM requests
- Raw API payload capture and operational signal tables

**Safe to delete** if you want a clean slate — the bot will recreate it on startup. But you lose all history. The file is gitignored.

### `elixir.log`

Only exists if launchd is configured to write stdout to a file. The bot doesn't rotate it. If it grows large, it's safe to truncate (`> elixir.log`) or delete while the bot is stopped.

### `../poapkings.com/src/_data/elixir*.json`

Dynamic site content files (elixirClan.json, elixirRoster.json, etc.). The bot writes and pushes these. This is in a separate repo.
