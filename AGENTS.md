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
- `cr_api.py` — Clash Royale API client (clan roster, war status, river race log). The **only** API ingress; every successful response appends an `api_observation_receipts` row under its true endpoint, while identical response bodies share one `raw_api_payloads` content row
- `engine/` — The v5.1 data engine (spec: `docs/reference/v5.1/`): `tick.py` (production orchestrator), `observations.py` (admission + canonical envelopes), `materialize.py` (the shared observation application path used by production, interactive refresh, and replay), `readiness.py` (source freshness + durable materialization generations), `event_contracts.py` (event vocabulary/routing), `clock.py`, `ingest.py`, `baselines.py` + `emitters/`, `change_sets.py`, `management.py`, `polling.py`, `projections.py`, and `offline.py`. The deterministic `recognition/` + `delivery.py` proactive stack was retired entirely in #207 — the awareness loop is the sole proactive owner. `offline.py` remains as the API-free replay harness `scripts/replay_gate.py` drives.
- `capabilities/` — Canonical domain answers shared by agent tools, awareness, scheduled reports, memory synthesis, and admin surfaces. Consumers may compact or present these facts differently, but do not recalculate them. Versioned contracts currently cover `game_modes.py`, `war.py`, `members.py`, `management.py`, and `awards.py`; management answers are explicitly leadership-scoped, while the other contracts are audience-neutral facts.
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
- Facade discipline: `elixir_agent.py`, `agent/tools.py`, and `storage/war.py` are explicit static facades. `db` uses an explicit name→source registry with lazy resolution to avoid its storage import cycle; duplicate declarations fail at import instead of being chosen by order. `elixir` is a sys.modules alias for `runtime.app`, whose explicit import blocks declare the runtime surface that tests and `runtime.activities` address by name. No namespace-copy re-export machinery — if a name should be public, add it to the explicit lists.

## The Engine (v5.1)

One data flow, spec'd in `docs/reference/v5.1/`:

- **One ingress:** `cr_api` → append-only `api_observation_receipts` → hash-deduplicated `raw_api_payloads` content (14-day rolling analysis buffer, never the system of record). Admission decisions attach to receipts; `materialization_inputs` link admitted receipts/content hashes to the generation that applied them.
- **Four event streams:** `battle_events` (native — battles mirror in with exact timestamps; war keys resolved from the battle's own time), `player_events`, `clan_events`, `war_events` (emitted — each poll diffs against its `state_baselines` row; first sight emits nothing; dedup keys make re-processing safe). `engine/event_contracts.py` is the single vocabulary for event ownership, payload floors, time semantics, awareness lanes, and hard-post policy.
- **One proactive owner:** the unified awareness loop reads the event streams and current projections, decides worthiness and framing in one turn, and posts with hard-floor coverage. The ported deterministic recognizers/delivery consumer remain an explicit offline comparison seam only; production `run_tick` does not import or run them.
- **Composition policy:** the awareness workflow owns voice and routing. Deterministic code validates the complete plan before any send (including member pronouns and unranked-war claims), permits one wording-only repair, then fails closed so the event resurfaces next loop. There is no member-facing template fallback.
- **Delivery:** awareness validates hard-post coverage before any send, persists every planned post to `awareness_delivery_intents`, then advances each intent `pending → sending → fulfilled`. Explicit send failures return only that intent to pending; the next turn skips already-fulfilled posts. `awareness_posts` remains the delivered channel-memory ledger. A failed turn does not advance event cursors. A crash while an intent is `sending` fails closed during its 15-minute lease, then returns to pending for an at-least-once retry instead of wedging the outbox forever. The retired consumer creates `communication_intents` only as a connection-local TEMP table in explicit offline legacy rehearsals.
- **Clan management:** `engine/management.py` per `docs/reference/v5.1/management.md` — Layer-1 evaluators (sustained donor / war-reliable / battle-active, 3-of-4-week hysteresis) feed promote/demote/kick candidacy machines. Kick-risk is reactive (fires a leader action through the policy gate mid-tick); promote/demote surface in the Monday 7:00 CT weekly review, which is also the only place the weekly counters roll. Engagement is measured from battles — `lastSeen`/logins are deliberately ignored. Every verdict carries `judgment_status` (`ready` / `held` / `unknown`), its evidence timestamp/reason, and the `materialization_id` that produced it; stale evidence fails closed and is excluded from actionable capability reads.
- **Adaptive polling:** `poll_state` temperatures (battles → hot; clan-poll deltas → warm; decay to cold) drive a budget of 40 per-player calls/tick, hottest first, with fairness floors (battlelog ≤6 h, profile ≤24 h for everyone). The clan and riverrace calls are cheap fixed overhead outside the budget.

### Engine Tick Contract

`engine.tick.run_tick(conn, api=…)` runs poll → one atomic generation. Poll
admission produces canonical `Observation` envelopes. Atomic apply uses
`engine.materialize` to advance battle rows, emitted deltas/baselines,
projections, rollups, successful-poll freshness, readiness, and management
together; if apply or management fails, all semantic writes roll back. Every
admitted input lands in `materialization_inputs`, and the final
`materialization_runs` state commits with the generation. Interactive profile
and battle-log refreshes create `interactive` generations through the same
application service; offline replay uses it too. Awareness and primary
capabilities read one SQLite snapshot and expose its `data_generation`. The production
entrypoint has no compose/send arguments and cannot post proactively; neither can
the offline engine, whose `legacy_proactive` adapter seam was removed with the
deterministic recognizer in #207. Awareness is the sole proactive owner. Emitter change sets must satisfy their event/table postconditions
before a baseline advances. Counters land in
`runtime_job_status` (`engine_tick` row) every tick.

## Environment

- Python 3.14 managed by uv; project environment at `.venv/` (gitignored)
- Requires `.env` with: DISCORD_TOKEN, CLAUDE_API_KEY, CR_API_KEY
- Non-secret config (channel IDs, clan tag) lives in `prompts/DISCORD.md` and `prompts/CLAN.md`
- Local start: `uv run --locked python elixir.py`
- Production process management uses `launchd`; see `SETUP.md`

### Feature flags — dark-launch, then graduate

Member-facing or behaviour-changing features ship behind an `ELIXIR_*` env flag
(OFF by default) so they can be validated in prod before they're trusted. The
current awareness gate remains a genuine kill-switch while it is still watched.

**A flag is scaffolding, not furniture.** Every dark-launch flag carries an
implicit graduation step: once the feature is trusted, **remove the flag** — make
the behaviour the default and delete the `os.getenv` gate, its `.env` line, and
the flag's mentions in docstrings/tests. Do NOT leave a validated feature gated
indefinitely; that is how `.env` and the code accrete dead toggles.

Rules of thumb:
- A flag that is fully ON in prod and no longer being toggled is a graduation
  candidate — collapse it into the live behavior.
- When you retire a flag, grep the whole repo (`.env`, code, tests, RELEASES.md)
  and remove every reference; a retired flag left in `.env` reads as live config.
- A flag read nowhere in code is dead — delete it from `.env` on sight.
- Keep flags only for genuine kill-switches (a risky, still-watched behaviour) or
  true deploy config (paths, ports, model ids, secrets) — those are not flags.

### Environment setup (one-time)

```bash
uv sync --locked
```

If `.venv` is missing or broken, `uv sync --locked` recreates it from
`pyproject.toml` and `uv.lock`. Production uses `uv sync --locked --no-dev`.

## Running Tests

```bash
uv run --locked pytest tests/ -v
```

- **Always use `uv run --locked pytest`** — do not use bare `pytest` or `python3 -m pytest`.
- `pyproject.toml` configures `pythonpath = ["."]` so all project imports resolve without install.
- Tests use temp-file/in-memory SQLite and mocked external services (no API keys needed). The suite runs green in ~8 s.
- `tests/conftest.py` builds the v5.1 schema from `scripts/migrate_v51/schema_v51.py` (plus the archive's DDL export for carried tables) into a session template, copied per test.
- Test fixtures handle DB connection lifecycle — use `pytest.fixture` instead of manual try/finally.
- The pre-commit hook mirrors CI in fail-fast order: dependency lock, docs, exception policy, `ruff check`, `ruff format --check`, capability-contract mypy, then the full suite with the 80% capability-coverage floor. `git commit --no-verify` bypasses in an emergency.

### Reality-based testing (the three levers beyond the suite)

Unit tests target one delta with minimal dicts; these three run the engine against reality and catch what hand-built fixtures can't. Run the first two before deploying engine changes:

1. **Replay gate** — `uv run --locked python scripts/replay_gate.py`. Snapshots the live DB, clears baselines, and replays the real raw-payload window twice through the awareness-only offline engine. Pass 1 inventories historical drift (current code may derive events an older deployment missed); pass 2 is the hard gate and must add exactly zero events, battles, or legacy claims under the same code. Ends with the current-data-relative season-close rehearsal + global invariants. All gates must PASS.
2. **Time-travel simulator** — `uv run --locked python scripts/simulate.py`. A deterministic synthetic war week (skewed 09:37Z reset, a join, a leave, a level-up, war battles, section rollover) through the production `run_tick` path at ~2 s/simulated-week. It proves event correctness, drift anchoring, poll fairness, zero legacy claims, absence of the retired delivery queue, and that the awareness read sees hard-post stream events.
3. **Real-payload fixtures** — `tests/fixtures/cr/*.json`, loaded via `load_cr_fixture` (tests/conftest.py) and asserted by `tests/test_cr_fixture_shapes.py`. When Supercell drifts a payload shape, these fail with a clear diff. Refresh stale fixtures by re-exporting from `raw_api_payloads` — never hand-edit them.

`assert_db_invariants` (tests/conftest.py) is the shared floor under all of it — an autouse sweep after every test, plus a gate inside both scripts: unique open memberships, one ledger claim per key, FTS mirror in sync, canonical timestamps, and projection consistency.

### Confidence layer (where failures go; how to know Elixir is healthy)

The bugs that keep biting are seam/first-use failures that fail *silently*. Three
tools make them visible:

1. **The error log** — `logs/elixir-error.log` (ERROR+ with tracebacks, written
   by `runtime/logging_setup.py`, rotated at 2 MB × 5). Abandoned runtime work
   and cross-table consistency failures log there on their module's own logger
   with a stable `<component> failed: k=v` prefix. Expected parsing, user/tool
   errors, and optional enrichment use bounded fallbacks or lower levels instead
   of flooding it; see `docs/reference/error-handling.md`. It is small enough
   (~6 lines/day) to read whole, which is the point.
   **Elixir does not monitor itself.** A `runtime_incidents` ledger and a daily
   `engine-health` job tried, and the ledger recorded 0 rows in 25 days while
   the log held 159 real errors — so the check reported "all clear" through
   every failure. Both were retired 2026-07-28 (schema v20). Detection is an
   operator job: **AGENT-TEAM/error-watch.md**, owned by the Operations Manager.
2. **Entrypoint smoke** (`tests/test_entrypoints_smoke.py`) — static + dynamic
   check that every function's names resolve and every compose/card/tool
   entrypoint is invocable. Catches the NameError/lazy-import class at test time.
3. **`scripts/confidence_report.py`** — one command (`--json`, non-zero exit on
   findings) that unifies grouped errors from the error log + smoke/integration
   test status + the latest post-quality scorecard, plus the `liveness` silence
   alarm (an error log cannot report a failure that produced no error, and the
   worst outages were quiet). "Is Elixir healthy?" in one answer. Run it
   before/after any change; the external Operations and Quality Manager routines
   execute it. The scorecard samples the active awareness and assistant-message
   paths read-only. Agents turn confirmed findings into GitHub issues; the report
   never creates a second work queue or silently changes production memory.

### Review discipline

A green suite is necessary, not sufficient. Before deploying a substantive change, do a **cold adversarial review** of the diff — read it as a skeptic hunting for what breaks, not as the author confirming what works. After deploying, do a **live behavioral audit**: watch what the running system actually does (Observatory, tick counters, posted messages) rather than what the code says it should do. The 2026-07-04 end-to-end review is the reference case: the suite was green, yet the live audit found a season-breaking gap (the awards consumer was never built — two work streams each assumed the other owned it) and the cold review found ten more real defects (delivery commit ordering, per-lane fail-stop, timestamp-format mismatches, CSRF host matching). An `engine-health` daily activity once tried to institutionalize the live audit's checks in-product; it was retired 2026-07-28 because a check that only covers known failure classes, run by the system it is checking, manufactures false calm (it read a ledger that never recorded a row). The watching lives outside the runtime now — `AGENT-TEAM/error-watch.md` — and new changes still need fresh adversarial eyes. Never mark a cross-stream feature done without verifying the consumer end-to-end.

## Cleanup

```bash
uv run --locked python scripts/clean.py
uv run --locked python scripts/clean.py --db
```

- default: remove cache directories like `__pycache__` and `.pytest_cache`
- `--db`: also remove legacy local runtime files (e.g. a stray `elixir.db`, `elixir.pid`) — it never touches `elixir-v51.db` or the archives

## Database

SQLite at `elixir-v51.db` (overridable via `ELIXIR_DB_PATH`; gitignored).
Three database files exist, with distinct roles:

- **`elixir-v51.db`** — the operational engine DB. The clean-break baseline source is `scripts/migrate_v51/schema_v51.py`; ordered post-cut evolution lives in `db/schema.py`. `db.get_connection()` refuses databases without the v5.1 spine, migrates compatible v5.1 databases forward, and is the sole initializer.
- Durable memory lives IN the engine DB since 2026-07-04 (the v5.1 memory pass, `docs/reference/v5.1/memory.md`): `memories` + `memory_tags` + `memories_fts`, accessed through the `memory_store` seam. `inference` rows carry a 90-day default TTL and are reclaimed by db-maintenance; curated kinds (`leader_note`, `synthesis`, `system`) never expire by default (#215). The old `elixir-v5-memory.db` is archived (`elixir-v5-memory-archive-2026H2.db`, read-only); `ELIXIR_V5_MEMORY_DB` is retired. **One database for all runtime activity.**
- **`elixir-v5-archive-2026H2.db`** — the pre-cut cold archive. Read-only (chmod 444), never written; open with `file:…?immutable=1`. **Not present on this workstation** — `tests/conftest.py` and `schema_v51.build()` both treat it as optional and fall back to the frozen `carried_ddl.sql`, which is why nothing has failed. Do not assume it is reachable; verify before planning any recovery around it.

**Historical recovery actually comes from the rolling backups** in `$ELIXIR_BACKUP_DIR` (see `scripts/backup_db.py`). Each nightly `.db.gz` froze the short-retention `raw_api_payloads` window as it stood on its own date, so their UNION reaches much further back than any single snapshot — the live DB holds ~2 weeks of payloads, the backups months. `scripts/backfill_battle_fields.py` is the current worked example: it reads the live database plus every backup through the current extractor.

The engine DB follows the layered retention model (`docs/reference/v5.1/schema.md`):

- L1 API provenance: append-only `api_observation_receipts` plus deduplicated `raw_api_payloads` content (14 d)
- L2 current-state baselines: `state_baselines` (diff substrate; not a read model)
- L3 event streams: `battle_events` (180 d), `player_events` (180 d), `clan_events` (365 d), `war_events` (365 d)
- L4 rollups (durable): `player_daily_metrics`, `player_daily_battle_rollups`, `clan_daily_metrics`
- L5 identity & tenure (durable): `players`, `player_metadata`, `player_aliases`, `clans`, `discord_users`, `discord_links`, `clan_memberships` — **the CR tag is the key everywhere**; "is X a member" = has an open `clan_memberships` row
- L6 projections (disposable, rebuilt from streams): `player_current_state`, `player_card_collection`, `player_recent_form`, `member_management`
- Awareness control: per-stream positions in `stream_cursors`, plus `awareness_thoughts`, `awareness_delivery_intents`, and `awareness_posts`. Standing concerns live in `memories` as `Watch:` / `Hold:` titles — the separate `watches` table was never written and was dropped in #211
- Award idempotency: `UNIQUE(award_type, season_id, section_index, player_tag)` on `awards`. The `recognition_ledger` that once mirrored it held intentless claims nothing read, and went with the recognizer in #207
- Clan management: `leader_action_recommendations`, `revisits`. The legacy `decision_cases` table and nullable leader-action link were removed by schema v21 after #216 made leader actions authoritative.
- Bounded war stream: `war_seasons` (durable), `war_weeks`, `war_week_clans`, `war_participation`, `war_attendance_days`
- Awards (durable): `awards` — `war_champ` is a ranked podium (season points); `iron_king` is PARTICIPATION (4/4 decks every battle day — unranked, any number earn it, never crown one); `rookie_mvp` = members in their FIRST war season; `free_pass` rotates to the highest-ranked War Champ who did NOT win it last month (`engine/emitters/war.py:close_season`). The LIVE in-progress races are computed on demand via `storage.awards.get_award_races` (top-10, points, tie-aware) and surfaced in the awareness read as `award_races`; `war_champ_lead_change` / `rookie_mvp_lead_change` events emit on a leader change.
- Engine control: `stream_cursors` (durable), `poll_state`, `runtime_job_status`, `materialization_runs`, `materialization_inputs`
- Ops singletons + tournaments star + the conversation set (`conversation_threads`, `messages`, `memory_facts`, `memory_episodes`)

All `db` module functions accept an optional `conn` parameter — pass one in tests, omit in production.

### Schema changes

The clean-break v5.1 baseline in `scripts/migrate_v51/schema_v51.py` is
migration 0. All post-cut forward changes are ordered and versioned in
`db/schema.py`; `db.get_connection()` applies them before returning. Runtime
and domain modules may validate required columns but must never issue `CREATE`
or `ALTER`. The retired pre-v5.1 migration history lives in Git and the cold
archive rather than executable runtime code. The committed fresh-schema fingerprint test changes with every
intentional schema change. Backups: `scripts/backup_db.py` covers the
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

- **The engine heartbeat is `engine-tick`** (`_engine_tick`, every 10 minutes, `max_instances=1`): one `engine.tick.run_tick` pass — poll → atomic observation apply → readiness-gated manage — plus leader-action card posting. It replaced the deleted `v5-reactive-tick`, `war-poll`, `player-progression`, and `award-detection` activities (awards now grant on the war stream's `season_closed` event; polling is the adaptive scheduler's job). The separate awareness activity consumes events and owns proactive posting.
- **Clan management:** `weekly-leadership-review` (Mon 7:00 CT — rolls the weekly hysteresis grain, surfaces promote/demote candidacies as leader actions, posts one review) and `action-outcome-refresh` (daily 9:30 CT — leader-action outcome evaluation + feedback-synthesis re-queue). The old `leadership-action-scan` is **gone**; its scan/creation role lives in the engine's reactive kick path.
- **War:** `war-attendance-snapshot` (daily 4:15 CT — finalizes `war_attendance_days` just before the ~09:15 UTC war-day boundary; evaluators read finalized days only).
- **Scheduled posts / reports:** `daily-clan-insight` (`#ask-elixir` hidden fact), `weekly-recap` (public recap), `weekly-discord-invite-relay`, `promotion-content` (`#recruiting`), `clan-wars-intel`.
- **Maintenance / ops:** `api-sentinel` (CR-API drift notes to `#leaders`), `memory-synthesis` (weekly memory hygiene), `card-catalog-sync`, `db-maintenance`, `db-backup` (daily 3:37 CT iCloud snapshot). `engine-health` was retired 2026-07-28 — production-problem detection is an operator/AGENT-TEAM job (`AGENT-TEAM/error-watch.md`), not an internal function of the clan bot.
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
- awareness reads are generation-stamped snapshots; validated post plans persist before send, successful posts fulfill their own intents and feed channel memory, and failed ticks do not advance event cursors, so retries send only unfulfilled work

## Agent Loop Guardrails (Current)

- Tool policy is enforced in code per workflow (not prompt-only):
  - `observation` -> read tools only
  - `channel_update` -> read tools only
  - `channel_update_leadership` -> read tools only
  - `interactive` -> read tools only
  - `clanops` -> read + write tools
  - `reception` -> no tools
  - `roster_bios` -> read tools only
- Write tools are gated by workflow policy: `clanops` can use read + write tools;
  every other interactive workflow remains read-only.
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
uv run --locked python scripts/review_agent_feedback.py --limit 20
uv run --locked python scripts/review_agent_feedback.py --workflow clanops --json
```

- text mode is for quick local triage
- `--json` is the format to hand to Codex or Claude for “what failed and what should we change?” review

## Context Budgeting (Current)

- Roster context is clipped in `_clan_context()` to avoid prompt bloat.
- Defaults:
  - chat workflows use `MAX_CONTEXT_MEMBERS_DEFAULT` (30)
  - site generation uses `MAX_CONTEXT_MEMBERS_FULL` (50)
- When clipping occurs, context includes an omitted-members summary line.

## Announcements and API drift

The `system_signals` queue was retired in #212 — it had no drain, so nothing it held was ever delivered. The two things it used to carry now have real owners:

- **Feature / release news** → `scripts/cut_release.py`: RELEASES.md, a #announcements post, and email to members with a verified address. One flow, already used for every release.
- **CR API drift** → the `api-sentinel` activity records first-seen schema paths into `api_sentinel_observations`. Nothing in the runtime evaluates them: the Operations Manager runs the 48h *structural*-drift query (new schema path, progress key, or game mode — never routine new event tags, which are pure noise) from `AGENT-TEAM/error-watch.md` step 5. The hand-off is deliberately thin; the AGENT-TEAM **Data Analyst** owns characterizing it and filing the issue.

Elixir also posts a startup check-in to the #elixir-log webhook with the running build hash and a short Clash Royale-flavored line.

## Query Layer (Current)

Elixir’s core member/leader questions should be answered from structured capabilities, query helpers, and tools, not prompt reconstruction. Shared domain answers live in `capabilities/`; LLM tools are adapters over those contracts rather than their sole owners. The versioned capability layer covers canonical game truth, clan game modes, live/season war intelligence, facet-based member intelligence, deck and clan-local metagame intelligence, authoritative management decisions, and provisional-versus-durable awards. These contracts feed tools, awareness, reports, memory synthesis, and admin/Observatory reads. External API refresh remains outside member capabilities, and management capabilities package the engine verdict without rescoring it.

The LLM has a set of domain-aligned tools (defined in `agent/tool_defs.py`) organized into five groups:

- **Member domain**: `resolve_member`, `get_member` (include: profile, form, battles, war, trend, deck, losses, history, memories, chests, awards), `get_member_war_detail` (aspect: summary, attendance, battles, missed_days, vs_clan_avg, war_decks)
- **River Race domain**: `get_river_race` (live race state + competing clan standings, read off the war clock), `get_war_season` (aspect: summary, standings, win_rates, boat_battles, score_trend, season_comparison, trending, perfect_attendance, no_participation), `get_clan_intel_report`
- **Clan domain**: `get_clan_roster` (aspect: list, summary, recent_joins, longest_tenure, role_changes, max_cards, trends), `get_clan_health` (aspect: at_risk, hot_streaks, losing_streaks, trophy_drops, promotion_candidates — at_risk and promotion_candidates read the `member_management` projection, so tools and the leader-action pipeline can never disagree), `get_clan_game_modes` (aspect: summary, ranked, side_modes, events)
- **Deck, card + awards domain**: `get_deck_intelligence` (member primary deck/variants/stability, clan-local archetype spread, leadership-gated named-card balance impact with source/date/direction), `lookup_cards`, `get_member_card_profile`, `lookup_member_cards`, `get_awards`
- **Elixir state + utility**: `get_elixir_state` (aspects: recent stream events / event windows / game modes, awareness decisions and confirmed posts, leader actions, and season state), `cr_api` (live Clash Royale API bridge for any external player/clan/tournament), `update_member`, `save_clan_memory`, `flag_member_watch`, `record_leadership_followup`, `schedule_revisit`

War tools include `war_player_type` (regular/occasional/rare/never) per member. Leadership evaluations include CR account age. Sensitive aspects (at_risk, promotion_candidates) are gated to leadership workflows at execution time.

### Mostly LLM

Almost every message Elixir sends is LLM-generated. Events, scheduled activities, and channel replies pass context to the LLM, which crafts the message using Elixir's identity from `SOUL.md` + `PURPOSE.md`, channel contract from `DISCORD.md`, lane behavior from `lanes/*.md`, and workflow-specific guidance from `agents/*.md` where applicable.

Exceptions: preauthored system-signal announcements may be written directly in code. Awareness posts have no deterministic member-facing fallback: invalid copy gets one bounded LLM wording repair, then the tick fails closed and retries from evidence on the next loop.

### Portability

A new clan forks elixir-bot and primarily rewrites `CLAN.md` and `DISCORD.md`, plus any lane prompts that reflect their own server culture. `SOUL.md`, `PURPOSE.md`, `GAME.md`, and most agent prompts should stay mostly portable. The clan-management policy constants live in `CLAN.md`; their meaning lives in `docs/reference/v5.1/management.md`.

### Future work

- startup linting for lane config, reply policy, and activity registry consistency outside the bot runtime

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
