# Elixir Bot

Elixir is an LLM-powered Discord bot for the POAP KINGS Clash Royale clan (`#J2RGCRVG`).

It is not a generic chat bot and not a single-feed narrator. Elixir is a channel-native clan agent with:
- channel-named subagents like `river-race`, `clan-events`, `leader-lounge`, and `ask-elixir`
- a central recurring activity registry for scheduled work
- scoped memory for public vs leadership context
- GitHub-backed publishing for poapkings.com

[AGENTS.md](AGENTS.md) is the repository source of truth for architecture and operating notes. This README is the best high-level introduction to the project.

Useful companion docs:
- [AGENTS.md](AGENTS.md)
- [SETUP.md](SETUP.md)
- [VERSIONS.md](VERSIONS.md)

## What Elixir Does

Elixir currently handles four main kinds of work:

1. Discord conversation
   Elixir answers questions in the right channel with the right lane behavior. `#ask-elixir` is open conversation, `#general` and `#war-talk` are mention-driven, and `#leader-lounge` is private clan operations.

2. Signal-driven clan updates
   Elixir detects roster, war, and progression signals, then fans one source event into one or more destination-specific outcomes. A new member join can become:
   - a public welcome in `#clan-events`
   - a factual leadership note in `#leader-lounge`
   - a short relay-ready welcome in `#arena-relay`

3. Scheduled recurring activities
   Elixir runs recurring activities like `clan-awareness`, `war-poll`, `war-awareness`, `player-progression`, `weekly-recap`, `promotion-content`, and the daily `#ask-elixir` hidden-fact post.

4. POAP KINGS website publishing
   Elixir generates and publishes structured data for poapkings.com, pushes it to GitHub, and reports publish outcomes in `#poapkings-com`.

## Current Channel Model

Elixir now uses channel-named subagents instead of one overloaded public stream.

Primary public/proactive lanes:
- `#river-race` for River Race coordination and battle-day urgency
- `#player-progress` for player milestones and progression
- `#clan-events` for joins, promotions, anniversaries, and clan recognitions
- `#announcements` for the weekly recap and important clan-wide Elixir updates
- `#arena-relay` for 160-character Clan Chat relay copy
- `#promote-the-clan` for recruiting copy members can reuse
- `#poapkings-com` for website publish visibility

Primary interactive lanes:
- `#ask-elixir` for open conversation with Elixir
- `#general` for mention-driven general questions
- `#war-talk` for mention-driven tactical war questions
- `#reception` for onboarding and identity verification
- `#leader-lounge` for leadership and clan operations

Legacy:
- `#elixir` remains in Discord for now, but it is retired from automated posting.

The live channel contract lives in [prompts/DISCORD.md](prompts/DISCORD.md).

## Recurring Activities

Recurring automated work is defined in [runtime/activities.py](runtime/activities.py). This is the canonical schedule registry.

Current activities:
- `clan-awareness`
  Every 30 minutes with up to 15 minutes of jitter, 24/7. Processes non-war clan signals and routes outcomes to subagents like `clan-events` and `leader-lounge`.
- `war-poll`
  Every hour at `:00` CT with no jitter. Owns scheduled live war ingest and persists the River Race snapshot pipeline.
- `war-awareness`
  Every hour at `:05` CT with no jitter. Reads stored war data, then owns scheduled River Race coordination and war-only signal handling.
- `player-progression`
  Every 30 minutes with up to 15 minutes of jitter. Refreshes player profiles and battle logs, then emits progression milestones.
- `daily-clan-insight`
  Daily in `#ask-elixir` at 12:00 PM CT with up to 30 minutes of jitter. Posts one short hidden fact when the data supports a genuinely interesting insight.
- `leadership-review`
  Weekly post in `#leader-lounge`.
- `weekly-recap`
  Weekly public recap in `#announcements`, plus members-page sync for the website.
- `site-content`
  Daily POAP KINGS website sync for clan, roster, and home payloads.
- `promotion-content`
  Weekly recruiting content for both `#promote-the-clan` and the website.

## Quick Start

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file with the required secrets:

```env
DISCORD_TOKEN=your_discord_bot_token
OPENAI_API_KEY=your_openai_api_key
CR_API_KEY=your_clash_royale_api_key
```

Start the bot locally:

```bash
venv/bin/python elixir.py
```

See [SETUP.md](SETUP.md) for production setup, `launchd`, optional site publishing config, and operational guidance.

## Running Tests

```bash
venv/bin/python -m pytest tests/ -v
```

Tests use in-memory SQLite and mocked external services. No API keys are needed for the test suite.

## Project Structure

Core entrypoints:
- [elixir.py](elixir.py)
  Main bot runtime entrypoint.
- [elixir_agent.py](elixir_agent.py)
  Stable public LLM entrypoint for replies, updates, and site generation.
- [heartbeat.py](heartbeat.py)
  API-driven signal detection for clan, war, and progression events.
- [cr_api.py](cr_api.py)
  Clash Royale API client.

Prompt and behavior stack:
- [prompts/SOUL.md](prompts/SOUL.md)
  Elixir's persistent identity and stance.
- [prompts/PURPOSE.md](prompts/PURPOSE.md)
  Mission and guardrails.
- [prompts/GAME.md](prompts/GAME.md)
  Clash Royale mechanics and stable game knowledge.
- [prompts/CLAN.md](prompts/CLAN.md)
  POAP KINGS-specific rules, thresholds, and clan identity.
- [prompts/DISCORD.md](prompts/DISCORD.md)
  Declarative channel contract.
- [prompts/subagents/](prompts/subagents/)
  Channel-named behavior prompts.

Runtime architecture:
- [runtime/activities.py](runtime/activities.py)
  Canonical recurring activity registry.
- [runtime/channel_router.py](runtime/channel_router.py)
  Discord message routing and reply-policy enforcement.
- [runtime/channel_subagents.py](runtime/channel_subagents.py)
  Multi-outcome signal planning, delivery, and channel-safe memory context.
- [runtime/jobs.py](runtime/jobs.py)
  Scheduled activity executors.
- [runtime/admin.py](runtime/admin.py)
  Admin command dispatch and manual activity execution.

Persistence and intelligence:
- [db/](db/)
  SQLite schema and query helpers.
- [storage/](storage/)
  Identity, memory, analytics, and message persistence.
- [agent/](agent/)
  LLM prompt composition, workflow contracts, tool policy, and chat loop.

Integrations:
- [integrations/poap_kings/](integrations/poap_kings/)
  POAP KINGS website integration and GitHub publishing.

## Prompt Model

Principle: prompts define what Elixir says and why. Code defines when, where, and how.

The current prompt stack is:
- `SOUL.md`
  Who Elixir is.
- `PURPOSE.md`
  What Elixir is for.
- `GAME.md`
  Stable Clash Royale knowledge.
- `CLAN.md`
  POAP KINGS-specific reality.
- `DISCORD.md`
  Server and channel contract.
- `subagents/*.md`
  Destination-lane behavior.

This split matters. It keeps one consistent Elixir identity across very different channels without collapsing everything into one noisy system prompt.

## Memory Model

Elixir uses two memory layers.

Conversational memory:
- user, member, and channel conversation state
- recent channel history
- message summaries and episodes

Durable scoped memory:
- `public`
- `leadership`
- `system_internal`

Important rules:
- public subagents only read public durable memory
- `leader-lounge` can read public plus leadership durable memory
- `reception` should stay focused on onboarding context
- multi-outcome signals share a source identity, but public and leadership durable memories stay separated so private copy cannot overwrite public memory

## POAP KINGS Website Integration

Elixir owns the dynamic site data written to poapkings.com:
- `elixirClan.json`
- `elixirRoster.json`
- `elixirHome.json`
- `elixirMembers.json`
- `elixirPromote.json`

GitHub-backed site publishing lives in [integrations/poap_kings/site.py](integrations/poap_kings/site.py).

When a real publish happens, Elixir reports it in `#poapkings-com` with:
- success or failure
- commit SHA
- direct GitHub commit URL
- repo and branch
- changed content types when useful

No-change publishes stay quiet.

## Admin and Operations

Elixir's operator surface now lives entirely in Discord `#leader-lounge`.

Use:
- private slash commands under `/elixir ...`
- public room commands with `@Elixir do ...`

Examples:

```text
/elixir system status
/elixir activity show activity:clan-awareness
/elixir integration poap-kings publish target:data preview:true
@Elixir do member set Ditika join-date 2026-03-07
@Elixir do signal show recent --limit 5
```

The command model is object-first and grouped around:
- `system`
- `clan`
- `member`
- `memory`
- `signal`
- `activity`
- `integration`

Useful operational docs:
- [SETUP.md](SETUP.md)
  Installation, launchd, deploy/update flow, and logs.
- [AGENTS.md](AGENTS.md)
  Deeper architecture and repository conventions.

## Reviewing Prompt Failures

Elixir stores failed or unusable Discord prompt attempts in the local `prompt_failures` table.

Review the latest failures with:

```bash
venv/bin/python scripts/review_prompt_failures.py --limit 20
venv/bin/python scripts/review_prompt_failures.py --workflow clanops --json
```

Use `--json` when you want to hand the failure set to another model for diagnosis.

## Cleanup

```bash
venv/bin/python scripts/clean.py
venv/bin/python scripts/clean.py --db
```

Default cleanup removes caches like `__pycache__` and `.pytest_cache`.

`--db` also removes local runtime files like `elixir.db` and `elixir.pid`.

## Portability

The project is designed so a new clan can fork it and mostly rewrite:
- [prompts/CLAN.md](prompts/CLAN.md)
- [prompts/DISCORD.md](prompts/DISCORD.md)
- selected files in [prompts/subagents/](prompts/subagents/)

The shared Elixir identity in `SOUL.md`, `PURPOSE.md`, and the game knowledge in `GAME.md` should remain broadly portable.
