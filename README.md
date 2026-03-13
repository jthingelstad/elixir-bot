# Elixir - POAP KINGS Discord Bot

LLM-powered clan management bot for **POAP KINGS**, a Clash Royale clan (#J2RGCRVG). Uses discord.py plus OpenAI model routing for intelligent, context-aware responses.

## What Elixir Does

- **Heartbeat cadence**: Fetches clan data every 47 minutes with jitter, detects changes (milestones, joins/leaves, war status, deck usage), and posts about what matters — skips when nothing interesting happened
- **Scheduled player-intel refresh**: Keeps a stale subset of active members' profiles and battle logs warm between the daily POAP KINGS site sync runs, and surfaces progression moments like level-ups, badge/achievement unlocks, and card milestone upgrades
- **Channel-aware operations**: Elixir answers member questions in `interactive` channels, participates proactively in `clanops`, and routes announcements/onboarding by prompt-defined channel roles
- **War tracking**: Monitors deck usage on battle days (Thu-Sun), tracks War Champ standings across seasons, and celebrates perfect participation
- **V2 clan intelligence**: SQLite-backed identity, roster, war, battle-log, and conversation-memory tables support deterministic member and leader answers

## Quick Start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# create .env with DISCORD_TOKEN, OPENAI_API_KEY, CR_API_KEY
venv/bin/python elixir.py
```

See [SETUP.md](SETUP.md) for full setup, configuration, and operations guide.

## Running Tests

```bash
venv/bin/python -m pytest tests/ -v
```

Focused tests use in-memory SQLite and mocked external services. No API keys needed.

## Cleanup

```bash
venv/bin/python scripts/clean.py
venv/bin/python scripts/clean.py --db
```

- default: remove cache directories like `__pycache__` and `.pytest_cache`
- `--db`: also remove local runtime files like `elixir.db` and `elixir.pid`

## Reviewing Prompt Failures

When a Discord prompt fails, falls back, or returns unusable output, Elixir stores a row in the local `prompt_failures` table with:

- cleaned prompt text
- workflow, failure type, and failure stage
- Discord channel/user/message IDs
- result preview or raw JSON
- last OpenAI model, error, and call timestamp

Review the latest failures with:

```bash
venv/bin/python scripts/review_prompt_failures.py --limit 20
venv/bin/python scripts/review_prompt_failures.py --workflow clanops --json
```

Use `--json` when you want to hand the failure set to another model for diagnosis and routing/prompt recommendations.

## V2 Validation Scripts

```bash
venv/bin/python scripts/smoke_test_v2.py --sample-limit 5
venv/bin/python scripts/eval_question_corpus_v2.py --mode fixture
```

- `smoke_test_v2.py` hits the live Clash Royale API using your local `.env`
- `eval_question_corpus_v2.py` runs representative leader/member questions in either `fixture` or `live` mode

## Admin Member Metadata

Use `elixir_do` for member metadata updates instead of CSV import/export:

```bash
venv/bin/python scripts/elixir_do.py set-join-date "Ditika" 2026-03-07
venv/bin/python scripts/elixir_do.py clear-join-date "Ditika"
venv/bin/python scripts/elixir_do.py set-birthday "King Levy" 2 14
venv/bin/python scripts/elixir_do.py set-profile-url "King Thing" https://example.com
venv/bin/python scripts/elixir_do.py set-note "King Thing" "Founder and systems builder"
```

These same commands are also available in `#clanops` with the `do` prefix.

## Project Structure

| File | Purpose |
|------|---------|
| `elixir.py` | Main bot: Discord events, APScheduler heartbeat, channel routing |
| `elixir_agent.py` | Stable public LLM entrypoint; routes observation, channel replies, and site generation through the `agent/` package |
| `heartbeat.py` | Hourly signal detection: milestones, war decks, joins/leaves, donations |
| `db/` | SQLite V2 store package: identity, memory, current state, player analytics, war, raw payloads |
| `cr_api.py` | Clash Royale API client: clan roster, war status, river race log, player profiles |
| `cr_knowledge.py` | Static game + clan knowledge injected into LLM system prompt |
| `prompts.py` | Loads and caches external prompt/config files from `prompts/` |
| `integrations/poap_kings/` | POAP KINGS-specific site integration and GitHub publishing |
| `storage/`, `agent/`, `runtime/` | Domain-first implementation packages for persistence, LLM behavior, and Discord runtime; public APIs stay at the root modules |

## Key Features

- **Signal-based posting** — Only calls the LLM when there are real things to talk about (trophy milestones, war completions, new members, etc.)
- **Agentic tool use** — LLM can resolve members, query roster summaries, current decks, recent form, war participation, and player profiles on demand
- **Clanops analytics** — Can answer at-risk member questions, recent-join performance, trending war contributors, and member-vs-clan war comparisons from the V2 query layer
- **War-battle analytics** — Can answer war-battle win/loss records, attendance rates, and highest war-battle win rates from stored battle facts
- **Role-change tracking** — Can report recent promotions and demotions from role snapshots instead of inferring from prompt context
- **Conversation memory** — Stores Discord/user/channel conversation history and subject-level memory in V2 tables
- **Memory-backed responses** — Leader, reception, and channel-observation flows now load durable user/member/channel memory in addition to raw recent turns
- **War Champ tracking** — Aggregates fame per member across a 4-5 week season; weekly rankings shared to the clan
- **Perfect participation** — Tracks members who use all 4 decks every battle day all season
- **Deck usage monitoring** — On battle days, thanks players who used their decks and nudges those who haven't
- **Cake days** — Tracks and announces clan birthday, member join anniversaries, and member birthdays
- **Self-managing database** — Raw ingest + normalized V2 tables, automatic data expiration, and a clean V2 baseline schema
- **Clan composition awareness** — Understands the target ratio of leaders, elders, and members

## Prompts & Configuration

Elixir's personality, knowledge, and channel behavior are defined in markdown files under `prompts/`, not hardcoded in Python. This makes it easy to tune the bot without touching code.

| File | What it controls |
|------|-----------------|
| `prompts/PURPOSE.md` | Elixir's personality, voice, and tone |
| `prompts/GAME.md` | Clash Royale game knowledge (war schedule, seasons, arenas) |
| `prompts/CLAN.md` | POAP KINGS rules, clan tag, promotion criteria, composition targets |
| `prompts/DISCORD.md` | Channel behaviors, Discord IDs, guild config |

These files are loaded by `prompts.py` and injected into the LLM system prompt. Static constants (trophy milestones, thresholds) live in `cr_knowledge.py`.

## Data Model

Elixir now uses a V2 SQLite schema centered on:

- `members`, `member_metadata`, `member_aliases`, `discord_users`, `discord_links`
- `conversation_threads`, `messages`, `memory_facts`, `memory_episodes`, `channel_state`
- `prompt_failures`
- `member_current_state`, `member_state_snapshots`, `member_daily_metrics`
- `player_profile_snapshots`, `member_deck_snapshots`, `member_card_usage_snapshots`, `member_battle_facts`, `member_recent_form`
- `war_current_state`, `war_day_status`, `war_races`, `war_participation`
- `raw_api_payloads`

See [docs/data-model-v2.md](docs/data-model-v2.md) for the full design.

Important bootstrap behavior:
- after a fresh database reset, current roster members do not get fake join dates
- `joined_date` stays unknown until Elixir observes a real join event over time or a leader supplies an override
