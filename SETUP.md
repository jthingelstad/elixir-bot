# Elixir Bot — Setup & Operations

Operations guide for running Elixir locally or in production.

This document focuses on:
- install and configuration
- `launchd` process management
- deploy/update flow
- logs and health checks
- safe cleanup and stateful files

For architecture details, use [AGENTS.md](AGENTS.md). For a high-level product overview, use [README.md](README.md).

## Install

```bash
cd ~/Projects/elixir-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Verify the install:

```bash
venv/bin/python -m pytest tests/ -v
```

The test suite uses in-memory SQLite and mocked external services, so you do not need API keys to validate the install.

## Configure

Create a `.env` file in the project root.

Required secrets:

```env
DISCORD_TOKEN=your_discord_bot_token
OPENAI_API_KEY=your_openai_api_key
CR_API_KEY=your_clash_royale_api_key
```

Optional site publishing settings:

```env
POAP_KINGS_SITE_ENABLED=1
POAP_KINGS_SITE_REPO=jthingelstad/poapkings.com
POAP_KINGS_SITE_BRANCH=main
POAP_KINGS_SITE_TOKEN=github_pat_xxx
```

Optional runtime tuning:

```env
ELIXIR_DB_PATH=./elixir.db
HEARTBEAT_INTERVAL_MINUTES=30
HEARTBEAT_JITTER_SECONDS=900
WAR_POLL_MINUTE=0
WAR_AWARENESS_MINUTE=5
PLAYER_INTEL_REFRESH_MINUTES=30
PLAYER_INTEL_REFRESH_JITTER_SECONDS=900
SITE_CONTENT_HOUR=18
CLANOPS_WEEKLY_REVIEW_DAY=fri
CLANOPS_WEEKLY_REVIEW_HOUR=19
WEEKLY_RECAP_DAY=mon
WEEKLY_RECAP_HOUR=9
PROMOTION_CONTENT_DAY=fri
PROMOTION_CONTENT_HOUR=9
ASK_ELIXIR_DAILY_INSIGHT_HOUR=12
ASK_ELIXIR_DAILY_INSIGHT_MINUTE=0
ASK_ELIXIR_DAILY_INSIGHT_JITTER_SECONDS=1800
```

Non-secret config lives in prompt files checked into the repo:
- [prompts/DISCORD.md](prompts/DISCORD.md)
  Discord IDs, subagents, workflows, reply policy, and memory scope.
- [prompts/CLAN.md](prompts/CLAN.md)
  Clan tag, rules, thresholds, and clan-specific identity.

## Local Run

Start Elixir locally:

```bash
source venv/bin/activate
venv/bin/python elixir.py
```

On startup, Elixir should:
- connect to Discord
- register scheduled activities from the activity registry
- post a startup check-in to the leadership workflow with the running build hash
- seed startup system signals if needed

## Process Management (`launchd`)

Production uses `launchd`. The plist at `~/Library/LaunchAgents/com.poapkings.elixir.plist` should run the venv Python binary directly.

`launchd` is the process owner. Do not manage production with `nohup`, `pkill`, or a background shell process.

### Start

```bash
launchctl load ~/Library/LaunchAgents/com.poapkings.elixir.plist
```

### Stop

```bash
launchctl unload ~/Library/LaunchAgents/com.poapkings.elixir.plist
```

### Verify it is running

```bash
launchctl list | grep com.poapkings.elixir
```

Typical interpretation:
- `-` or `0` in the exit-status column usually means healthy
- a negative value like `-15` means the last run was terminated

### Preferred helper

There is also a local helper script:

```bash
bash scripts/admin.sh status
bash scripts/admin.sh start
bash scripts/admin.sh stop
bash scripts/admin.sh restart
```

Use whichever path your deployment prefers, but `launchd` remains the underlying source of truth.

## Deployment / Updating

If you have the upgrade helper available, use it:

```bash
bash scripts/upgrade.sh
```

Manual deploy flow:

```bash
launchctl unload ~/Library/LaunchAgents/com.poapkings.elixir.plist
cd ~/Projects/elixir-bot
git pull
source venv/bin/activate
pip install -r requirements.txt
launchctl load ~/Library/LaunchAgents/com.poapkings.elixir.plist
launchctl list | grep com.poapkings.elixir
```

Database migrations run automatically at startup.

## Scheduled Activities

The canonical schedule source is [runtime/activities.py](runtime/activities.py).

Current recurring activity set:
- `clan-awareness`
  Every 30 minutes with up to 15 minutes of jitter, 24/7.
- `war-poll`
  Every hour at `:00` CT with no jitter.
- `war-awareness`
  Every hour at `:05` CT with no jitter.
- `player-progression`
  Every 30 minutes with up to 15 minutes of jitter.
- `daily-clan-insight`
  Daily at 12:00 PM CT with up to 30 minutes of jitter.
- `leadership-review`
  Weekly.
- `weekly-recap`
  Weekly.
- `site-content`
  Daily.
- `promotion-content`
  Weekly.

Use the live admin surface when you want the actual schedule rendered from the registry instead of trusting this file:

```text
/elixir system schedule
@Elixir do system schedule
```

Or in Discord:
- `/elixir system schedule`
- `@Elixir do system schedule`

## Health Checks

Useful live checks:

```text
/elixir system status
@Elixir do system status
@Elixir do signal show recent --limit 5
@Elixir do activity list
```

Useful Discord checks:
- `/elixir system status`
- `/elixir system schedule`
- `/elixir system storage`
- `/elixir system storage view:clan`
- `/elixir system storage view:war`
- `/elixir system storage view:memory`
- `/elixir clan war`
- `/elixir clan status`

Manual non-posting previews are available for many admin jobs:

```text
/elixir activity run activity:site-content preview:true
/elixir activity run activity:weekly-recap preview:true
/elixir activity run activity:promotion-content preview:true
@Elixir do activity run site-content --preview
```

Preview mode suppresses Discord sends and GitHub site pushes, but still runs the job logic.

## Logs

Elixir logs to stdout/stderr. In production, `launchd` captures those logs according to the plist's `StandardOutPath` and `StandardErrorPath`.

### What healthy startup looks like

Typical healthy startup lines include:

```text
Elixir online as ...
Scheduler started — clan-awareness — Every 30 minutes with up to 900s jitter., war-poll — Every hour at :00 CT., war-awareness — Every hour at :05 CT. ...
```

You should also see:
- a startup message in the leadership workflow
- activity registration from the scheduler summary

### What to look for in logs

General errors:

```bash
grep "ERROR" /path/to/elixir.log | tail -20
```

Prompt failures:

```bash
grep "prompt_failure" /path/to/elixir.log | tail -20
```

POAP KINGS website publishes:

```bash
grep "POAP KINGS" /path/to/elixir.log | tail -20
```

If GitHub-backed publishes are working, Elixir should also post operational visibility into `#poapkings-com`.

### Prompt failure review

When Discord prompt generation fails or falls back:

```bash
venv/bin/python scripts/review_prompt_failures.py --limit 20
venv/bin/python scripts/review_prompt_failures.py --workflow clanops --json
```

The backing data lives in the `prompt_failures` table in `elixir.db`.

## Manual Runtime Checks

Safe local heartbeat inspection:

```bash
source venv/bin/activate
venv/bin/python -c "import heartbeat; result = heartbeat.tick(); print(f'{len(result.signals)} signals')"
```

This reads from the Clash Royale API and writes to the local DB, but does not post to Discord.

If you want to run a recurring activity directly through the supported admin surface, use:

```text
/elixir activity run activity:clan-awareness preview:true
/elixir activity run activity:war-awareness preview:true
/elixir activity run activity:player-progression preview:true
@Elixir do activity run clan-awareness --preview
```

## Member Metadata Operations

Use the admin surface instead of direct database edits:

```text
/elixir member set member:Ditika field:join-date value:2026-03-07
/elixir member clear member:Ditika field:join-date
/elixir member set member:"King Levy" field:birthday value:02-14
/elixir member set member:"King Thing" field:profile-url value:https://example.com
/elixir member set member:"King Levy" field:poap-address value:0xabc123...
/elixir member set member:"King Thing" field:note value:"Founder and systems builder"
```

These commands are also exposed in Discord leadership/admin flows.

## Stateful Files and Data

### `elixir.db`

Default path: `./elixir.db`, unless overridden by `ELIXIR_DB_PATH`.

It contains:
- member identity and metadata
- Discord links
- conversation and channel memory
- prompt failures
- current clan/member state
- player analytics and battle facts
- war state and participation
- raw ingest and signal logs

It is safe to delete if you want a clean local reset, but you will lose history and memory.

### Log files

Only exist if your `launchd` plist writes stdout/stderr to a file.

It is safe to rotate, truncate, or delete those logs while the service is stopped.

### POAP KINGS website repo

The dynamic site data is published to the configured GitHub repo under:
- `src/_data/elixirClan.json`
- `src/_data/elixirRoster.json`
- `src/_data/elixirHome.json`
- `src/_data/elixirMembers.json`
- `src/_data/elixirPromote.json`

The runtime publish path is GitHub API-based. Local sibling-repo writes are legacy/dev-only behavior.

## Cleanup

Remove transient local files:

```bash
venv/bin/python scripts/clean.py
```

Remove caches plus local runtime state like `elixir.db` and `elixir.pid`:

```bash
venv/bin/python scripts/clean.py --db
```

## Common Drift To Watch For

The following are common sources of stale docs or stale assumptions:
- activity cadence changed in `runtime/activities.py`
- channel contract changed in `prompts/DISCORD.md`
- reply policy changed for a channel like `#ask-elixir`
- a new operational channel like `#poapkings-com` was added
- startup behavior changed in `runtime/app.py`

When in doubt, trust code over prose:
- scheduler truth: [runtime/activities.py](runtime/activities.py)
- channel truth: [prompts/DISCORD.md](prompts/DISCORD.md)
- admin command truth: [runtime/admin.py](runtime/admin.py)
