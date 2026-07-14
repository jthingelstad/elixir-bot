# Elixir Bot

Discord bot for the POAP KINGS Clash Royale clan (#J2RGCRVG). Uses discord.py plus Anthropic Claude model routing:
- chat workflows default to `claude-sonnet-4-6`
- promotion/content workflows default to `claude-sonnet-4-6`
- interactive/reception workflows default to `claude-haiku-4-5-20251001`
- observation workflows default to `claude-haiku-4-5-20251001`

`AGENTS.md` is the single source of truth for repository-specific instructions and architecture notes.

## v5.1 Migration Note (2026-07-03/04)

The engine was re-architected in a clean break: the three layered engine
generations (v4 event store, v4 signal/delivery, v5 Event Core on the
`eventsourcing` framework) were **deleted**, replaced by the single `engine/`
package, and the database was rebuilt as `elixir-v51.db` with tags as the
natural key everywhere (no synthetic member ids). The spec of record is
`docs/reference/v5.1/` (architecture, schema, events, recognition, runtime,
management, migration — plus the decision record). The pre-cut database is
preserved forever as the read-only cold archive `elixir-v5-archive-2026H2.db`.
Rollback before close-out = old git ref + copy the archive back + relaunch.

## Project Structure

- `elixir.py` — Main bot: Discord events, APScheduler, channel routing
- `elixir_agent.py` — Stable public LLM entrypoint; routes observation, channel replies, and content generation through the `agent/` package
- `cr_api.py` — Clash Royale API client (clan roster, war status, river race log). The **only** API ingress; every response is appended to `raw_api_payloads` under its true endpoint name
- `engine/` — The v5.1 engine (spec: `docs/reference/v5.1/`): `tick.py` (the five-step production data path plus explicit legacy recognizer shadow), `clock.py` (war clock), `ingest.py` (battle mirror), `baselines.py` + `emitters/` (state-diff event emission), `recognition/` (retained deterministic scorer/ledger shadow), `delivery.py` (retained at-least-once legacy intent consumer), `management.py` (clan-management state machines), `polling.py` (adaptive budget scheduler), `projections.py` (read models), `offline.py` (rehearsal/replay engine — no API, no Discord)
- `db/` — SQLite access package: connection discipline, identity helpers, and the storage facade
- `cr_knowledge.py` — Static Clash Royale + POAP KINGS game knowledge
- `prompts.py` — Loads and caches external prompt/config files from `prompts/`
- `prompts/lanes/` — Discord destination-lane behavior prompts
- `prompts/agents/` — Executable workflow prompts that are not tied to one Discord destination
- `scripts/review_agent_feedback.py` — Review recent LLM/channel failures and `#ask-elixir` feedback from SQLite for debugging and prompt/tool routing analysis
- `scripts/migrate_v51/` — The v5.1 migration toolkit: `schema_v51.py` (the baseline schema source of truth), `transforms.py` (archive→new transforms), `parity_checks.py`, `rehearsal.py`
- `runtime/activities.py` — Canonical registry for recurring automated activities
- `runtime/clan_chat_copy.py` — Dedicated Clash Royale in-game clan chat copy generation, validation, and fallback guardrails
- `runtime/channel_router.py` — Discord message routing for interactive channels
- `storage/`, `agent/`, `runtime/` — Domain-first implementation packages for persistence, LLM behavior, and Discord runtime; root modules remain the stable public API surface
- Facade discipline: `elixir_agent.py` is an explicit static facade over `agent/` (its import list is the public API; submodules may only reach it via function-level imports). `elixir` is a sys.modules alias for `runtime.app`, whose explicit import blocks declare the runtime surface that tests and `runtime.activities` address by name. No dynamic re-export machinery — if a name should be public, add it to the explicit lists.

## The Engine (v5.1)

One data flow, spec'd in `docs/reference/v5.1/`:

- **One ingress:** `cr_api` → `raw_api_payloads` (14-day rolling analysis buffer, never the system of record).
- **Four event streams:** `battle_events` (native — battles mirror in with exact timestamps; war keys resolved from the battle's own time), `player_events`, `clan_events`, `war_events` (emitted — each poll diffs against its `state_baselines` row; first sight emits nothing; dedup keys make re-processing safe).
- **One proactive owner:** the unified awareness loop reads the event streams and current projections, decides worthiness and framing in one turn, and posts with hard-floor coverage. The ported deterministic recognizers/delivery consumer remain explicit migration-shadow seams only; production `run_tick` does not run them.
- **Composition policy:** the awareness workflow owns voice and routing. Deterministic code validates the complete plan before any send (including member pronouns and unranked-war claims), permits one wording-only repair, then fails closed so the event resurfaces next loop. There is no member-facing template fallback.
- **Delivery:** awareness sends first, then records fulfilled `awareness:post` intents as channel memory/dedup context. A send or hard-floor miss fails the tick without advancing the awareness cursor. Legacy pending/failed intents retain their at-least-once consumer for migration/shadow use.
- **Clan management:** `engine/management.py` per `docs/reference/v5.1/management.md` — Layer-1 evaluators (sustained donor / war-reliable / battle-active, 3-of-4-week hysteresis) feed promote/demote/kick candidacy machines. Kick-risk is reactive (fires a leader action through the policy gate mid-tick); promote/demote surface in the Monday 7:00 CT weekly review, which is also the only place the weekly counters roll. Engagement is measured from battles — `lastSeen`/logins are deliberately ignored.
- **Adaptive polling:** `poll_state` temperatures (battles → hot; clan-poll deltas → warm; decay to cold) drive a budget of 40 per-player calls/tick, hottest first, with fairness floors (battlelog ≤6 h, profile ≤24 h for everyone). The clan and riverrace calls are cheap fixed overhead outside the budget.

### Engine Tick Contract

`engine.tick.run_tick(conn, api=…)` runs the production five-step data path
(poll → ingest → emit → project → manage) with per-step guards — a failing step
logs, records its error in the counters, and the tick continues. The emitted
streams and projections are the awareness loop's read. `deliver=False` is the
default and production contract; `deliver=True` explicitly exercises the
retired recognize → legacy-intent-deliver path for migration tests only. The
offline engine follows the same awareness-only default, with
`legacy_proactive=True` as its explicit shadow seam. Counters land in
`runtime_job_status` (`engine_tick` row) every tick.

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
- Tests use temp-file/in-memory SQLite and mocked external services (no API keys needed). The suite runs green in ~8 s.
- `tests/conftest.py` builds the v5.1 schema from `scripts/migrate_v51/schema_v51.py` (plus the archive's DDL export for carried tables) into a session template, copied per test.
- Test fixtures handle DB connection lifecycle — use `pytest.fixture` instead of manual try/finally.
- The pre-commit hook runs `ruff check .` then the full suite (both match CI); `git commit --no-verify` bypasses in an emergency.

### Reality-based testing (the three levers beyond the suite)

Unit tests target one delta with minimal dicts; these three run the engine against reality and catch what hand-built fixtures can't. Run the first two before deploying engine changes:

1. **Replay gate** — `./venv/bin/python scripts/replay_gate.py`. Snapshots the live DB, clears baselines, and replays the real raw-payload window twice through the awareness-only offline engine. Pass 1 inventories historical drift (current code may derive events an older deployment missed); pass 2 is the hard gate and must add exactly zero events, battles, legacy claims, or legacy intents under the same code. Ends with the current-data-relative season-close rehearsal + global invariants. All gates must PASS.
2. **Time-travel simulator** — `./venv/bin/python scripts/simulate.py`. A deterministic synthetic war week (skewed 09:37Z reset, a join, a leave, a level-up, war battles, section rollover) through the production `run_tick` path at ~2 s/simulated-week. It proves event correctness, drift anchoring, poll fairness, zero legacy claims/intents, and that the awareness read sees hard-post stream events.
3. **Real-payload fixtures** — `tests/fixtures/cr/*.json`, loaded via `load_cr_fixture` (tests/conftest.py) and asserted by `tests/test_cr_fixture_shapes.py`. When Supercell drifts a payload shape, these fail with a clear diff. Refresh stale fixtures by re-exporting from `raw_api_payloads` — never hand-edit them.

`assert_db_invariants` (tests/conftest.py) is the shared floor under all of it — an autouse sweep after every test, plus a gate inside both scripts: unique open memberships, one ledger claim per key, FTS mirror in sync, no space-format timestamps, known lanes/statuses only.

### Confidence layer (where failures go; how to know Elixir is healthy)

The bugs that keep biting are seam/first-use failures that fail *silently*. Three
tools make them visible:

1. **Incident ledger** — every best-effort/swallowing `except` records to
   `runtime_incidents` (`storage/incidents.py:record_incident`) before it passes.
   An external agent finds all open failures in one query:
   `sqlite3 elixir-v51.db "SELECT at, component, summary, detail FROM runtime_incidents WHERE resolved_at IS NULL ORDER BY at DESC LIMIT 50"`.
   Also on Observatory `/incidents`, and the daily `engine-health` job names them
   to #elixir-log. Resolve one with `UPDATE runtime_incidents SET resolved_at = ...`.
2. **Entrypoint smoke** (`tests/test_entrypoints_smoke.py`) — static + dynamic
   check that every function's names resolve and every compose/card/tool
   entrypoint is invocable. Catches the NameError/lazy-import class at test time.
3. **`scripts/confidence_report.py`** — one command (`--json`, non-zero exit on
   findings) that unifies open incidents + smoke/integration test status + the
   latest post-quality scorecard. "Is Elixir healthy?" in one answer. Run it
   before/after any change; it's what the unattended `confidence-monitor` routine
   executes.

### Review discipline

A green suite is necessary, not sufficient. Before deploying a substantive change, do a **cold adversarial review** of the diff — read it as a skeptic hunting for what breaks, not as the author confirming what works. After deploying, do a **live behavioral audit**: watch what the running system actually does (Observatory, tick counters, posted messages) rather than what the code says it should do. The 2026-07-04 end-to-end review is the reference case: the suite was green, yet the live audit found a season-breaking gap (the awards consumer was never built — two work streams each assumed the other owned it) and the cold review found ten more real defects (delivery commit ordering, per-lane fail-stop, timestamp-format mismatches, CSRF host matching). The `engine-health` daily activity (`runtime/health.py`) institutionalizes the live audit's checks, but it covers only known failure classes — new changes need fresh adversarial eyes. Never mark a cross-stream feature done without verifying the consumer end-to-end.

## Cleanup

```bash
venv/bin/python scripts/clean.py
venv/bin/python scripts/clean.py --db
```

- default: remove cache directories like `__pycache__` and `.pytest_cache`
- `--db`: also remove legacy local runtime files (e.g. a stray `elixir.db`, `elixir.pid`) — it never touches `elixir-v51.db` or the archives

## Database

SQLite at `elixir-v51.db` (overridable via `ELIXIR_DB_PATH`; gitignored).
Three database files exist, with distinct roles:

- **`elixir-v51.db`** — the operational engine DB: 49 engine tables + 4 conversation tables (53 designed). The baseline schema source of truth is `scripts/migrate_v51/schema_v51.py`; `db.get_connection()` **refuses** databases that do not carry the v5.1 spine — this build never rebuilds or migrates a database in place.
- Durable memory lives IN the engine DB since 2026-07-04 (the v5.1 memory pass, `docs/reference/v5.1/memory.md`): `memories` + `memory_tags` + `memory_log` + `memories_fts`, accessed through the `memory_store` seam. The old `elixir-v5-memory.db` is archived (`elixir-v5-memory-archive-2026H2.db`, read-only); `ELIXIR_V5_MEMORY_DB` is retired. **One database for all runtime activity.**
- **`elixir-v5-archive-2026H2.db`** — the pre-cut cold archive. Read-only (chmod 444), never written; open with `file:…?immutable=1`. Everything historical lives here.

The engine DB follows the layered retention model (`docs/reference/v5.1/schema.md`):

- L1 raw response log: `raw_api_payloads` (14 d)
- L2 current-state baselines: `state_baselines` (diff substrate; not a read model)
- L3 event streams: `battle_events` (180 d), `player_events` (180 d), `clan_events` (365 d), `war_events` (365 d)
- L4 rollups (durable): `player_daily_metrics`, `player_daily_battle_rollups`, `clan_daily_metrics`, `clan_daily_battle_rollups`
- L5 identity & tenure (durable): `players`, `player_metadata`, `player_aliases`, `clans`, `discord_users`, `discord_links`, `clan_memberships` — **the CR tag is the key everywhere**; "is X a member" = has an open `clan_memberships` row
- L6 projections (disposable, rebuilt from streams): `player_current_state`, `player_card_collection`, `player_recent_form`, `member_management`
- Proactive history + legacy shadow: `communication_intents` records fulfilled awareness posts for channel memory and still houses any legacy queue rows; `recognition_ledger` retains durable award claims and the disabled deterministic recognizer's history. Neither is disposable.
- Clan management: `leader_action_recommendations`, `decision_cases`, `revisits`
- Bounded war stream: `war_seasons` (durable), `war_weeks`, `war_week_clans`, `war_participation`, `war_attendance_days`
- Awards (durable): `awards` — `war_champ` is a ranked podium (season points); `iron_king` is PARTICIPATION (4/4 decks every battle day — unranked, any number earn it, never crown one); `rookie_mvp` = members in their FIRST war season; `free_pass` rotates to the highest-ranked War Champ who did NOT win it last month (`engine/emitters/war.py:close_season`). The LIVE in-progress races are computed on demand via `storage.awards.get_award_races` (top-10, points, tie-aware) and surfaced in the awareness read as `award_races`; `war_champ_lead_change` / `rookie_mvp_lead_change` events emit on a leader change.
- Engine control: `stream_cursors` (durable), `poll_state`, `runtime_job_status`
- Ops singletons + tournaments star + the conversation set (`conversation_threads`, `messages`, `memory_facts`, `memory_episodes`)

All `db` module functions accept an optional `conn` parameter — pass one in tests, omit in production.

### Schema changes

There is no in-place migration runner anymore. The v5.1 baseline in
`scripts/migrate_v51/schema_v51.py` is the schema source of truth; a future
schema change means extending that module and applying it deliberately
(`db/_migrations.py` is the retired pre-v5.1 history, kept for reference
only — nothing imports it). Backups: `scripts/backup_db.py` covers the
operational DB only — single database since the memory pass (the archive needs no backup — it never
changes).

## Website Note

Elixir no longer publishes to poapkings.com — site publishing was removed
entirely on 2026-06-21 (the website has its own standalone update script).
The `poapkings-com` lane / `#website-updates` channel remains only as a
legacy visibility surface. Don't add site-publish behavior back into the bot.

## Agents And Lanes

Elixir has one identity and several executable workflows. Discord destinations are **lanes**, not independent agents.

Core rule: one signal is not one post. The awareness loop reads the whole current situation, decides which moments deserve communication, and may combine several events into one post while proving coverage for every hard-post signal.

Current primary lanes:
- `reception` — onboarding and verification (`#welcome`)
- `general` — mention-driven general Q&A (`#clan-chat`)
- `ask-elixir` — open-channel clan conversation and Clash Royale screenshot help
- `leader-lounge` — private leadership and clan operations (`#leaders`)
- `arena-relay` — crisp leader action cards and leader-posted Clash Royale screenshot observation readouts (`#actions`; also the fail-closed destination for unknown intent prefixes)
- `river-race` — River Race scoreboard, recap, and major war-momentum updates
- `member-highlights` — curated player milestones and non-war battle pushes (`#player-highlights`)
- `clan-events` — joins, promotions, anniversaries, and clan recognitions (`#clan-events`)
- `announcements` — weekly recap and clan-wide Elixir system updates (`#announcements`)
- `promote-the-clan` — recruiting copy (`#recruiting`)
- `poapkings-com` — legacy website-visibility lane (see Website Note)

Current executable agents/workflows:
- `awareness` — the sole proactive voice workflow: it reads current streams, projections, history, and channel memory, then returns one structured post plan. Deterministic copy policy permits one wording-only repair and otherwise fails closed before Discord.
- `interactive` — public read-only conversation in member-facing lanes.
- `clanops` — private leadership conversation with gated write tools.
- `reception` — constrained onboarding and identity-verification replies.
- `memory_synthesis` — weekly memory hygiene and canonical arc synthesis.
- `content` workflows — recruiting, weekly recap, and other publishable content.
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

- **The engine heartbeat is `engine-tick`** (`_engine_tick`, every 10 minutes, `max_instances=1`): one `engine.tick.run_tick` pass — poll → ingest → emit → project → manage → recognize → deliver — plus leader-action card posting and the clan-chat relay tradition. It replaced the deleted `v5-reactive-tick`, `war-poll`, `player-progression`, and `award-detection` activities (awards now grant on the war stream's `season_closed` event; polling is the adaptive scheduler's job).
- **Clan management:** `weekly-leadership-review` (Mon 7:00 CT — rolls the weekly hysteresis grain, surfaces promote/demote candidacies as leader actions, posts one review) and `action-outcome-refresh` (daily 9:30 CT — leader-action outcome evaluation + feedback-synthesis re-queue). The old `leadership-action-scan` is **gone**; its scan/creation role lives in the engine's reactive kick path.
- **War:** `war-attendance-snapshot` (daily 4:15 CT — finalizes `war_attendance_days` just before the ~09:15 UTC war-day boundary; evaluators read finalized days only).
- **Scheduled posts / reports:** `daily-clan-insight` (`#ask-elixir` hidden fact), `weekly-recap` (public recap), `weekly-discord-invite-relay`, `promotion-content` (`#recruiting`), `clan-wars-intel`.
- **Maintenance / ops:** `api-sentinel` (CR-API drift notes to `#leaders`), `memory-synthesis` (weekly memory hygiene), `card-catalog-sync`, `db-maintenance`, `db-backup` (daily 3:37 CT iCloud snapshot), `engine-health` (daily 8:23 CT read-only audit — `runtime/health.py`; posts to `#elixir-log` only when a check fails).
- **Tournaments:** the watch is leader-started/stopped (`runtime/jobs/_tournament.py`), a dynamic job that resumes on restart — not a registry entry.

## Architecture: Prompts vs Code

Principle: **Prompts define what Elixir says and why. Code defines when, where, and how.**

### Prompt files (`prompts/`)

- `SOUL.md` — Elixir's persistent identity, stance, and non-human sense of self.
- `PURPOSE.md` — Elixir's mission, responsibilities, and guardrails.
- `GAME.md` — Clash Royale mechanics (game-generic, rarely changes).
- `CLAN.md` — Clan-specific identity, rules, history, and configurable thresholds (inactivity, the ratified clan-management constants, donation highlights, clan lore).
- `DISCORD.md` — Declarative Discord channel contract: IDs, lanes, workflows, reply policies, memory scope, and durable-memory flags. The engine resolves lane→channel from this file at runtime (no hard-coded channel ids).
- `lanes/*.md` — Destination-lane behavior prompts.
- `agents/*.md` — Executable workflow prompts for awareness, memory synthesis, routing, and specialist agents.

### What stays in code

Activity scheduling, channel routing, stream emission, hard-post floors, copy-policy invariants, outcome fan-out, delivery bookkeeping, tool execution, JSON response contracts, memory enforcement, nickname matching, LLM parameters, Elixir data normalization, and in-game clan chat copy guardrails. The awareness model makes editorial worthiness judgments; code still owns factual and delivery invariants.

## Memory Model

Elixir uses two memory layers:
- conversational memory in identity/message storage (`discord_user`, `member`, `channel`)
- durable scoped memory (`public`, `leadership`) in the engine DB's `memories` tables, ranked at retrieval (match + confidence + recency) with FTS search

Important rules:
- channel lanes read destination-channel conversational context, not a global blended chat history
- public lanes read public durable memory only
- `leader-lounge` can read public plus leadership durable memory
- `reception` should stay focused on onboarding context and avoid unrelated clan-event noise
- one source signal can create multiple channel outcomes, but durable memory records must stay scope-safe and must not let leadership copy overwrite public memory
- successful awareness posts are recorded as fulfilled intents for channel memory; failed awareness ticks do not advance their read cursor, so uncovered signals resurface with already-landed posts available for dedup

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
- `#actions` is normally action-board style with disabled general replies, but `runtime/channel_router.py` special-cases leader-posted Clash Royale screenshots as observation evidence and replies with a concise `arena_relay_screenshot_observation` readout.

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
- **River Race domain**: `get_river_race` (live race state + competing clan standings, read off the war clock), `get_war_season` (aspect: summary, standings, win_rates, boat_battles, score_trend, season_comparison, trending, perfect_attendance, no_participation), `get_clan_intel_report`
- **Clan domain**: `get_clan_roster` (aspect: list, summary, recent_joins, longest_tenure, role_changes, max_cards, trends), `get_clan_health` (aspect: at_risk, hot_streaks, losing_streaks, trophy_drops, promotion_candidates — at_risk and promotion_candidates read the `member_management` projection, so tools and the leader-action pipeline can never disagree), `get_clan_game_modes` (aspect: summary, ranked, side_modes, events)
- **Card + awards domain**: `lookup_cards`, `get_member_card_profile`, `lookup_member_cards`, `get_awards`
- **Elixir state + utility**: `get_elixir_state` (aspects: recent stream events / event windows / game modes, plus decision cases, communication intents, recognition state, season window), `cr_api` (live Clash Royale API bridge for any external player/clan/tournament), `update_member`, `save_clan_memory`, `flag_member_watch`, `record_leadership_followup`, `schedule_revisit`

War tools include `war_player_type` (regular/occasional/rare/never) per member. Leadership evaluations include CR account age. Sensitive aspects (at_risk, promotion_candidates) are gated to leadership workflows at execution time.

### Mostly LLM

Almost every message Elixir sends is LLM-generated. Events, scheduled activities, and channel replies pass context to the LLM, which crafts the message using Elixir's identity from `SOUL.md` + `PURPOSE.md`, channel contract from `DISCORD.md`, lane behavior from `lanes/*.md`, and workflow-specific guidance from `agents/*.md` where applicable.

Exceptions: preauthored system-signal announcements may be written directly in code. Awareness posts have no deterministic member-facing fallback: invalid copy gets one bounded LLM wording repair, then the tick fails closed and retries from evidence on the next loop.

### Portability

A new clan forks elixir-bot and primarily rewrites `CLAN.md` and `DISCORD.md`, plus any lane prompts that reflect their own server culture. `SOUL.md`, `PURPOSE.md`, `GAME.md`, and most agent prompts should stay mostly portable. The clan-management policy constants live in `CLAN.md`; their meaning lives in `docs/reference/v5.1/management.md`.

### Future work

- startup linting for lane config, reply policy, and activity registry consistency outside the bot runtime
- the intra-package aggregators (`db/__init__.py`, `storage/war.py`, `agent/tools.py`) still use the dynamic `__export_public` copy loop. Converting them to the explicit-facade pattern requires giving each aggregated submodule a real `__all__` first — without that, a static conversion either enshrines junk names (`datetime`, `Optional`) or risks dropping a name that whole-module `db` mocks in tests would never catch.
- lift the non-strict file-level xfail marks in `tests/` file-by-file as the deferred-pass semantics settle (see the Phase 8 notes in `docs/reference/v5.1/`).

## Work Tracking

- **GitHub issues** are the canonical queue for discrete, trackable work. Use
  `gh issue list` / `gh issue create` / `gh issue view`. Claude in any session
  can read and write issues on `jthingelstad/elixir-bot`.
- Use labels to cluster arcs: `persona` for work that closes the gap between
  Elixir's articulated persona (`prompts/SOUL.md`, `prompts/PURPOSE.md`) and
  the implementation; `v5.1` for the engine re-architecture arc. Add a
  tracking issue when an arc has 3+ child issues.
- **`docs/tasks/*.md`** is for *active* long-form design docs — the *why*
  behind an in-flight arc, not the unit-of-work ledger. When a design doc
  exists, link it from the tracking issue. When an arc ships, move its doc to
  `docs/archive/`; docs describing a stable, ongoing system live in
  `docs/reference/`. See `docs/README.md` for the layout.
- Default: create an issue before starting non-trivial work. Commit directly
  to `main` — PRs are not required. Reference the issue number in commit
  messages (e.g. `Closes #12`) so GitHub auto-closes on push.

## Key Conventions

- All times in America/Chicago timezone (engine-internal timestamps are UTC; suffixless timestamps are UTC by convention)
- Clan tag: J2RGCRVG (POAP KINGS)
- CR tags are identity: if the CR API identifies it with a tag, the tag is the key; internal-only entities keep synthetic ids
