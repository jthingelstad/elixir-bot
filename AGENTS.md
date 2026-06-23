# Elixir Bot

Discord bot for the POAP KINGS Clash Royale clan (#J2RGCRVG). Uses discord.py plus Anthropic Claude model routing:
- chat workflows default to `claude-sonnet-4-6`
- site/content workflows default to `claude-sonnet-4-6`
- promotion workflows default to `claude-sonnet-4-6`
- interactive/reception workflows default to `claude-haiku-4-5-20251001`
- observation workflows default to `claude-haiku-4-5-20251001`

`AGENTS.md` is the single source of truth for repository-specific instructions and architecture notes.

## Project Structure

- `elixir.py` — Main bot: Discord events, APScheduler, channel routing
- `elixir_agent.py` — Stable public LLM entrypoint; routes observation, channel replies, and site generation through the `agent/` package
- `cr_api.py` — Clash Royale API client (clan roster, war status, river race log)
- `heartbeat.py` — API-driven signal detection for clan, war, and progression events
- `db/` — SQLite data store package: identity, memory, current state, analytics, war, and raw payload capture
- `cr_knowledge.py` — Static Clash Royale + POAP KINGS game knowledge
- `prompts.py` — Loads and caches external prompt/config files from `prompts/`
- `prompts/lanes/` — Discord destination-lane behavior prompts
- `prompts/agents/` — Executable workflow prompts that are not tied to one Discord destination
- `modules/poap_kings/` — POAP KINGS-specific site integration and GitHub publishing
- `scripts/review_agent_feedback.py` — Review recent LLM/channel failures and `#ask-elixir` feedback from SQLite for debugging and prompt/tool routing analysis
- `runtime/activities.py` — Canonical registry for recurring automated activities
- `runtime/clan_chat_copy.py` — Dedicated Clash Royale in-game clan chat copy generation, validation, and fallback guardrails
- `event_core/` — Event-sourced v5 reactive engine: ingest payloads → detections / recommendations / decision-cases → communication intents → confirmed Discord delivery. This is the proactive path; it replaced the v4 signal/awareness loop (`runtime/signals/`, `signal_lanes.py`, `situation.py`), which has been deleted.
- `runtime/channel_router.py` — Discord message routing for interactive channels
- `storage/`, `agent/`, `runtime/` — Domain-first implementation packages for persistence, LLM behavior, and Discord runtime; root modules remain the stable public API surface
- Facade discipline: `elixir_agent.py` is an explicit static facade over `agent/` (its import list is the public API; submodules may only reach it via function-level imports). `elixir` is a sys.modules alias for `runtime.app`, whose explicit import blocks declare the runtime surface that tests and `runtime.activities` address by name. No dynamic re-export machinery — if a name should be public, add it to the explicit lists.

## Environment

- Python 3.14 via Homebrew; project venv at `venv/` (gitignored)
- Requires `.env` with: DISCORD_TOKEN, CLAUDE_API_KEY, CR_API_KEY
- Non-secret config (channel IDs, clan tag) lives in `prompts/DISCORD.md` and `prompts/CLAN.md`
- Local start: `venv/bin/python elixir.py`
- Production process management uses `launchd`; see `SETUP.md`

### Venv setup (one-time)

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

If the venv is missing or broken, recreate it with the commands above.

`requirements.lock` is a `pip freeze` snapshot of the known-good production
venv — use `pip install -r requirements.lock` to reproduce it exactly, and
regenerate it after any deliberate dependency upgrade.

## Running Tests

```bash
./venv/bin/pytest tests/ -v
```

- **Always use `./venv/bin/pytest`** — do not use bare `pytest` or `python3 -m pytest`. The Homebrew `pytest` binary runs in its own isolated env and cannot import project dependencies.
- `pyproject.toml` configures `pythonpath = ["."]` so all project imports resolve without install.
- Tests use in-memory SQLite and mocked external services (no API keys needed).
- Test fixtures handle DB connection lifecycle — use `pytest.fixture` instead of manual try/finally.

## Cleanup

```bash
venv/bin/python scripts/clean.py
venv/bin/python scripts/clean.py --db
```

- default: remove cache directories like `__pycache__` and `.pytest_cache`
- `--db`: also remove local runtime files like `elixir.db` and `elixir.pid`

## Database

SQLite at `elixir-v5.db` (default `_DEFAULT_DB_PATH` in `db/__init__.py`; auto-created, gitignored; overridable via `ELIXIR_DB_PATH`). The v5 event-sourcing stores and the legacy domain tables now live in this one unified file — `db.DB_PATH` and `event_core.config.PROJECTIONS_DB` resolve to the same database. The project uses the baseline schema in `_migration_0()` in `db/__init__.py`. The key tables are:

- Identity + metadata: `members`, `member_metadata`, `member_aliases`, `discord_users`, `discord_links`
- Conversation memory: `conversation_threads`, `messages`, `memory_facts`, `memory_episodes`, `channel_state`
- Prompt failure review: `prompt_failures`
- Clan/member state: `clan_memberships`, `member_current_state`, `member_state_snapshots`, `member_daily_metrics`
- Player analytics: `player_profile_snapshots`, `member_card_collection_snapshots`, `member_deck_snapshots`, `member_card_usage_snapshots`, `member_battle_facts`, `member_recent_form`
- War: `war_current_state`, `war_day_status`, `war_races`, `war_participation`
- Manual activity capture: `clan_voyages`, `clan_voyage_entries`, `arena_relay_screenshot_observations`
- Event Core (v5): `detections`, `battle_telemetry`, `communication_intents`, `decision_cases`, `elixir_projects`, `signal_detector_cursors` — the reactive engine's projections and read models
- Raw ingest + support: `raw_api_payloads`, `signal_log`, `cake_day_announcements`

The legacy v4 `game_event_stream` and `event_rollups` tables are retired — no code reads or writes them (their storage modules were deleted in the v4 teardown), and they remain on disk only as dormant historical data pending an eventual drop migration.

All `db` module functions accept an optional `conn` parameter — pass one in tests, omit in production.

### Migrations

Schema is managed by `_MIGRATIONS` list in `db/__init__.py` using `PRAGMA user_version`. To add a schema change:

1. Write a `_migration_N(conn)` function
2. Append it to `_MIGRATIONS`
3. Keep migrations additive unless you are intentionally resetting the database as a breaking change

Migrations run automatically in `get_connection()`. This repo currently treats the baseline schema as the clean foundation; additive migrations are fine, but breaking resets are acceptable when the model changes materially.

The `_MIGRATIONS` list in `db/_migrations.py` is the canonical history — read it directly rather than maintaining a duplicate enumeration here.

## Site Content System

Elixir is the single authority for all dynamic data on poapkings.com. All Elixir-owned files use the `elixir-` prefix:

- `elixirClan.json` — Dynamic clan stats (memberCount, scores, donations, etc.)
- `elixirRoster.json` — Full roster with member data + bios + intro
- `elixirHome.json` — Home page message of the day
- `elixirMembers.json` — Members page weekly recap payload
- `elixirPromote.json` — Promotional messages (5 channels)

Filenames are camelCase (not hyphenated) because 11ty uses the filename stem as the data key — `elixirClan.json` becomes `elixirClan` in templates. The POAP KINGS integration publishes these files directly to GitHub paths in the site repo; local sibling-repo writes are now a legacy/dev-only path. `site.json` contains only static site config (url, joinUrl, discordUrl, tagline, clanTag).

Website publish visibility lives in `#website-updates`. Elixir posts there for real GitHub-backed publish outcomes:
- `site-content`
- `weekly-recap` site sync
- `promotion-content`
- manual non-preview runs that publish to GitHub

Success posts should include the commit SHA and direct GitHub commit URL. No-change publishes stay quiet.

## Agents And Lanes

Elixir has one identity and several executable workflows. Discord destinations are **lanes**, not independent agents.

Core rule: one signal is not one post. A signal enters the event/project/case/intent pipeline; Elixir then decides which lane, if any, should receive a communication.

Current primary lanes:
- `reception` — onboarding and verification (`#welcome`)
- `general` — mention-driven general Q&A (`#clan-chat`)
- `ask-elixir` — open-channel clan conversation and Clash Royale screenshot help
- `leader-lounge` — private leadership and clan operations (`#leaders`)
- `arena-relay` — crisp leader action cards and leader-posted Clash Royale screenshot observation readouts
- `river-race` — River Race scoreboard, recap, and major war-momentum updates
- `member-highlights` — curated player milestones and non-war battle pushes (`#player-highlights`)
- `clan-events` — joins, promotions, anniversaries, and clan recognitions (`#clan-events`)
- `announcements` — weekly recap and clan-wide Elixir system updates (`#announcements`)
- `promote-the-clan` — recruiting copy for Discord and the website (`#recruiting`)
- `poapkings-com` — website publish visibility only (`#website-updates`)

Current executable agents/workflows:
- `awareness` — the compose workflow the v5 reactive engine (`_v5_reactive_tick`) drives: given a detection's context plus projects, cases, recent events, and channel memory, it decides whether to raise a communication intent and in what voice.
- `interactive` — public read-only conversation in member-facing lanes.
- `clanops` — private leadership conversation with gated write tools.
- `reception` — constrained onboarding and identity-verification replies.
- `memory_synthesis` — weekly memory hygiene and canonical arc synthesis.
- `content` workflows — website, recruiting, weekly recap, and other publishable content.
- specialist workflows such as `deck_review`, `tournament_update`, `clan_chat_copy`, and `intent_router`.

## Recurring Activities

The canonical source of truth for scheduled automated work is `runtime/activities.py`, not scattered scheduler calls or prose docs.

Each activity declares:
- owner lane
- purpose
- schedule
- executor function
- delivery targets
- whether manual triggering is allowed

Read the exact, current list (keys, schedules, executors, enabled state) from `runtime/activities.py` — don't trust a hand-maintained copy here, which drifts. The shape today:

- The proactive heartbeat is **`v5-reactive-tick`** (`_v5_reactive_tick`). It runs the Event Core engine — ingest → detections → recommendations/cases → communication intents → confirmed Discord delivery — and **replaced the deleted v4 `clan-awareness` / `war-awareness` ticks**. Leadership recommendations now flow through it too (`leadership-action-scan` is retained but disabled).
- **Ingest / refresh:** `war-poll` (hourly River Race snapshot), `player-progression` (`_player_intel_refresh`, player profile + battle intel + `#player-highlights`), `card-catalog-sync`.
- **Scheduled posts / reports:** `daily-clan-insight` (`#ask-elixir` hidden fact), `weekly-recap` (public recap + website sync), `weekly-discord-invite-relay`, `promotion-content` (`#recruiting` + website), `clan-wars-intel`, `award-detection`.
- **Maintenance / ops:** `api-sentinel` (CR-API drift notes to `#leaders`), `memory-synthesis` (weekly memory hygiene), `db-maintenance`.

## Architecture: Prompts vs Code

Principle: **Prompts define what Elixir says and why. Code defines when, where, and how.**

### Prompt files (`prompts/`)

- `SOUL.md` — Elixir's persistent identity, stance, and non-human sense of self.
- `PURPOSE.md` — Elixir's mission, responsibilities, and guardrails.
- `GAME.md` — Clash Royale mechanics (game-generic, rarely changes).
- `CLAN.md` — Clan-specific identity, rules, history, and configurable thresholds (inactivity, promotion criteria, donation highlights, clan lore).
- `DISCORD.md` — Declarative Discord channel contract: IDs, lanes, workflows, reply policies, memory scope, and durable-memory flags.
- `lanes/*.md` — Destination-lane behavior prompts.
- `agents/*.md` — Executable workflow prompts for awareness, memory synthesis, routing, and specialist agents.

### What stays in code

Activity scheduling, channel routing, signal detection, outcome fan-out, delivery dedupe, tool execution, JSON response contracts, memory enforcement, nickname matching, LLM parameters, Elixir data normalization, and in-game clan chat copy guardrails.

## Memory Model

Elixir uses two memory layers:
- conversational memory in identity/message storage (`discord_user`, `member`, `channel`)
- durable scoped memory in contextual memory (`public`, `leadership`, `system_internal`)

Important rules:
- channel lanes read destination-channel conversational context, not a global blended chat history
- public lanes read public durable memory only
- `leader-lounge` can read public plus leadership durable memory
- `reception` should stay focused on onboarding context and avoid unrelated clan-event noise
- one source signal can create multiple channel outcomes, but durable memory records must stay scope-safe and must not let leadership copy overwrite public memory
- outcome delivery state is tracked separately from signal detection so retries can target only failed destinations

## Agent Loop Guardrails (Current)

- Tool policy is enforced in code per workflow (not prompt-only):
  - `observation` -> read tools only
  - `channel_update` -> read tools only
  - `channel_update_leadership` -> read tools only
  - `interactive` -> read tools only
  - `clanops` -> read + write tools
  - `reception` -> no tools
  - `roster_bios` -> read tools only
- Write tools are gated by workflow policy and `CLANOPS_WRITE_TOOLS_ENABLED` (default enabled for `clanops` only).
- Tool outputs are wrapped in a compact envelope (`ok`, `error`, `truncated`, `meta`, `data`) and truncated for context budget safety.
- Leader/member factual answers should prefer structured query tools over clipped roster context. Resolve members by name/Discord handle before using tag-based tools when needed.
- Strict JSON workflow contracts are validated in code with one repair retry:
  - `observation`: requires `event_type`, `summary`, `content` (or `null`)
  - `channel_update` / `channel_update_leadership` / `interactive` / `clanops`: require `event_type`, `summary`, `content`
  - `clanops` `channel_share` responses also require `share_content`
  - `reception`: requires `event_type=reception_response` and `content`
  - `roster_bios`: requires `intro` and `members` map
- Loop telemetry is logged per request: workflow, tool rounds, tools called, denied tools, validation failures, prompt/completion size estimates, and completion latencies.
- Channel/reception failures are also persisted in `prompt_failures` with the cleaned question text, workflow, failure type/stage, Discord metadata, result preview/raw JSON, and the last LLM error/model snapshot.
- Reply behavior is enforced in code from channel config:
  - `mention_only` for channels like `#clan-chat` and `#leaders`
  - `open_channel` for `#ask-elixir`
  - `disabled` for notification-only channels like `#website-updates`, `#river-race`, and `#announcements`
- `#leader-actions` is normally action-board style with disabled general replies, but `runtime/channel_router.py` special-cases leader-posted Clash Royale screenshots as observation evidence and replies with a concise `arena_relay_screenshot_observation` readout.

### Agent Feedback Review

Use the stored prompt-failure log and `#ask-elixir` feedback records when a Discord request fails, falls back, returns unusable output, or gets a thumbs-down:

```bash
venv/bin/python scripts/review_agent_feedback.py --limit 20
venv/bin/python scripts/review_agent_feedback.py --workflow clanops --json
```

- text mode is for quick local triage
- `--json` is the format to hand to Codex or Claude for “what failed and what should we change?” review

## Context Budgeting (Current)

- Roster context is clipped in `_clan_context()` to avoid prompt bloat.
- Defaults:
  - chat workflows use `MAX_CONTEXT_MEMBERS_DEFAULT` (30)
  - site generation uses `MAX_CONTEXT_MEMBERS_FULL` (50)
- When clipping occurs, context includes an omitted-members summary line.

## Heartbeat Tick Contract (Current)

- `heartbeat.tick()` returns a `HeartbeatTickResult` bundle:
  - `signals`
  - `clan`
  - `war`
- The Event Core engine (`event_core/`), driven by `_v5_reactive_tick`, is the consumer of clan/war observations on the proactive path. The v4 `_clan_awareness_tick` / `_war_awareness_tick` consumers that previously read this bundle have been deleted.

## System Signals

One-time capability or upgrade announcements should use the queued `system_signals` path, not an ad hoc Discord post.

- Define startup-seeded system signals in `runtime/system_signals.py`
- Add one entry to `STARTUP_SYSTEM_SIGNALS` with:
  - stable `signal_key`
  - `signal_type` such as `capability_unlock`
  - `payload` fields the channel-update workflow can talk about, including `audience` when the update is meant for the clan
- Startup queues these signals idempotently via `queue_startup_system_signals()`
- Pending system signals are published by `runtime/system_status_post.py` (`_post_system_signal_updates`) — a direct post to the target lane that marks each announced after a successful send. The `api-sentinel` activity drives this for CR-API drift notes
- Elixir also posts a separate startup check-in to the #elixir-log webhook with the running build hash and a short Clash Royale-flavored line

This keeps feature announcements discoverable: future changes should usually mean “edit one list” instead of remembering startup-hook details.

## Query Layer (Current)

Elixir’s core member/leader questions should be answered from structured query helpers and tools, not prompt reconstruction. The LLM has a set of domain-aligned tools (defined in `agent/tool_defs.py`) organized into five groups:

- **Member domain**: `resolve_member`, `get_member` (include: profile, form, battles, war, trend, deck, losses, history, memories, chests, awards), `get_member_war_detail` (aspect: summary, attendance, battles, missed_days, vs_clan_avg, war_decks)
- **River Race domain**: `get_river_race` (live race state + competing clan standings), `get_war_season` (aspect: summary, standings, win_rates, boat_battles, score_trend, season_comparison, trending, perfect_attendance, no_participation), `get_clan_intel_report`
- **Clan domain**: `get_clan_roster` (aspect: list, summary, recent_joins, longest_tenure, role_changes, max_cards, trends), `get_clan_health` (aspect: at_risk, hot_streaks, losing_streaks, trophy_drops, promotion_candidates), `get_clan_game_modes` (aspect: summary, ranked, side_modes, events), `get_clan_voyage`
- **Card + awards domain**: `lookup_cards`, `get_member_card_profile`, `lookup_member_cards`, `get_awards`
- **Elixir state + utility**: `get_elixir_state` (aspects: recent detections / event windows / game modes, plus projects, decision cases, communication intents, season window), `cr_api` (live Clash Royale API bridge for any external player/clan/tournament), `update_member`, `save_clan_memory`, `flag_member_watch`, `record_leadership_followup`, `schedule_revisit`

War tools include `war_player_type` (regular/occasional/rare/never) per member. Leadership evaluations include CR account age. Sensitive aspects (at_risk, promotion_candidates) are gated to leadership workflows at execution time.

### Mostly LLM

Almost every message Elixir sends is LLM-generated. Events, scheduled activities, website publish notices, and channel replies pass context to the LLM, which crafts the message using Elixir's identity from `SOUL.md` + `PURPOSE.md`, channel contract from `DISCORD.md`, lane behavior from `lanes/*.md`, and workflow-specific guidance from `agents/*.md` where applicable.

Exception: preauthored system-signal announcements may be written directly in code and delivered without LLM rewriting when deterministic wording matters.

### Portability

A new clan forks elixir-bot and primarily rewrites `CLAN.md` and `DISCORD.md`, plus any lane prompts that reflect their own server culture. `SOUL.md`, `PURPOSE.md`, `GAME.md`, and most agent prompts should stay mostly portable.

### Future work

- startup linting for lane config, reply policy, and activity registry consistency outside the bot runtime
- the intra-package aggregators (`db/__init__.py`, `storage/war.py`, `agent/tools.py`) still use the dynamic `__export_public` copy loop. Converting them to the explicit-facade pattern requires giving each aggregated submodule a real `__all__` first — without that, a static conversion either enshrines junk names (`datetime`, `Optional`) or risks dropping a name that whole-module `db` mocks in tests would never catch.

## Work Tracking

- **GitHub issues** are the canonical queue for discrete, trackable work. Use
  `gh issue list` / `gh issue create` / `gh issue view`. Claude in any session
  can read and write issues on `jthingelstad/elixir-bot`.
- Use labels to cluster arcs: `persona` for work that closes the gap between
  Elixir's articulated persona (`prompts/SOUL.md`, `prompts/PURPOSE.md`) and
  the implementation. Add a tracking issue when an arc has 3+ child issues.
- **`docs/tasks/*.md`** is for *active* long-form design docs — the *why*
  behind an in-flight arc, not the unit-of-work ledger. When a design doc
  exists, link it from the tracking issue. When an arc ships, move its doc to
  `docs/archive/`; docs describing a stable, ongoing system live in
  `docs/reference/`. See `docs/README.md` for the layout.
- Default: create an issue before starting non-trivial work. Commit directly
  to `main` — PRs are not required. Reference the issue number in commit
  messages (e.g. `Closes #12`) so GitHub auto-closes on push.

## Key Conventions

- All times in America/Chicago timezone
- Clan tag: J2RGCRVG (POAP KINGS)
- POAP KINGS site content publishes to `src/_data/elixir*.json` in the configured GitHub site repo
