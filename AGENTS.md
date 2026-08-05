# Elixir Bot

Discord bot for the POAP KINGS Clash Royale clan (#J2RGCRVG). Uses discord.py
plus Anthropic Claude model routing. Models resolve by **family**, not by
workflow — each workflow declares a `model_family` and the family maps to an id
in `agent/core.py`:

| Family | Default | Used for |
|---|---|---|
| `chat` | `claude-sonnet-5` | interactive, reception, clanops, awareness |
| `creative` | `claude-opus-5` | recruiting copy |
| `intensive` | `claude-opus-5` | weekly recap, memory synthesis |
| `lightweight` | `claude-haiku-4-5-20251001` | triage, wake responder first rung |

Each is overridable by env (`ELIXIR_CHAT_MODEL` and friends). Read
`_model_for_workflow` in `agent/core.py` for the mapping — the table above is
prose and can drift.

`AGENTS.md` is the single source of truth for repository-specific instructions
and architecture notes. `CLAUDE.md` is a symlink to this file, so Claude Code
and Codex read the same thing; do not fork them.

**This file is a map, not a mirror.** Where a registry, schema, or script owns
the real list, link to it and describe the *shape* instead of copying entries.
Every hand-maintained copy in here has drifted at least once — a 2026-08-05
audit found ~26 factual errors, almost all of them in copied lists.

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

- `elixir.py` — a 15-line `sys.modules` alias installer; the runtime itself is `runtime/app.py` (Discord events, APScheduler, channel routing)
- `elixir_agent.py` — Stable public LLM entrypoint; routes observation, channel replies, and content generation through the `agent/` package
- `cr_api.py` — Clash Royale API client (clan roster, war status, river race log). The **only** API ingress; every successful response appends an `api_observation_receipts` row under its true endpoint, while identical response bodies share one `raw_api_payloads` content row
- `engine/` — The v5.1 data engine (spec: `docs/reference/v5.1/`): `tick.py` (production orchestrator), `observations.py` (admission + canonical envelopes), `materialize.py` (the shared observation application path used by production, interactive refresh, and replay), `readiness.py` (source freshness + durable materialization generations), `event_contracts.py` (event vocabulary/routing), `clock.py`, `ingest.py`, `baselines.py` + `emitters/`, `change_sets.py`, `management.py`, `polling.py`, `projections.py`, and `offline.py`. The deterministic `recognition/` + `delivery.py` proactive stack was retired entirely in #207 — the awareness stack is the sole proactive owner. `offline.py` remains as the API-free replay harness `scripts/replay_gate.py` drives.
- `capabilities/` — Canonical domain answers shared by agent tools, awareness, scheduled reports, memory synthesis, and admin surfaces. Consumers may compact or present these facts differently, but do not recalculate them. Each module declares a `CONTRACT_VERSION`; grep for it rather than trusting a list here. Management answers are explicitly leadership-scoped, while the other contracts are audience-neutral facts.
- `db/` — SQLite access package: connection discipline, the canonical schema builder and ordered migration ladder (`schema.py`, with private migration-0 assets beside it), identity helpers, and the storage facade
- `cr_knowledge.py` — Static Clash Royale + POAP KINGS game knowledge
- `prompts.py` — Loads and caches external prompt/config files from `prompts/`
- `prompts/lanes/` — Discord destination-lane behavior prompts
- `prompts/agents/` — Executable workflow prompts that are not tied to one Discord destination
- `prompts/jobs/` — Per-job prose for the scoped responder, read by `agent/chassis.py`
- `scripts/review_agent_feedback.py` — Review recent LLM/channel failures and `#ask-elixir` feedback from SQLite for debugging and prompt/tool routing analysis
- `runtime/activities.py` — Canonical registry for recurring automated activities
- `runtime/clan_chat_copy.py` — Dedicated Clash Royale in-game clan chat copy generation, validation, and fallback guardrails
- `runtime/channel_router.py` — Discord message routing for interactive channels
- `storage/`, `agent/`, `runtime/` — Domain-first implementation packages for persistence, LLM behavior, and Discord runtime; root modules remain the stable public API surface
- Facade discipline: `elixir_agent.py` and `storage/war.py` are explicit static facades. `db` uses an explicit name→source registry with lazy resolution to avoid its storage import cycle; duplicate declarations fail at import instead of being chosen by order. `elixir` is a sys.modules alias for `runtime.app`, whose explicit import blocks declare the runtime surface that tests and `runtime.activities` address by name. No namespace-copy re-export machinery — if a name should be public, add it to the explicit lists.

## The Engine (v5.1)

One data flow, spec'd in `docs/reference/v5.1/`:

- **One ingress:** `cr_api` → append-only `api_observation_receipts` → hash-deduplicated `raw_api_payloads` content (60-day rolling analysis buffer, never the system of record). Admission decisions attach to receipts; `materialization_inputs` link admitted receipts/content hashes to the generation that applied them.
- **Four event streams:** `battle_events` (native — battles mirror in with exact timestamps; war keys resolved from the battle's own time), `player_events`, `clan_events`, `war_events` (emitted — each poll diffs against its `state_baselines` row; first sight emits nothing; dedup keys make re-processing safe). `engine/event_contracts.py` is the single vocabulary for event ownership, payload floors, time semantics, awareness lanes, and hard-post policy.
- **One proactive owner (the awareness stack):** the brain reads the event streams and current projections, decides worthiness and framing in one turn, and posts with hard-floor coverage; the scoped responder handles single qualifying events between brain runs. Both post through the same validator and outbox. The ported deterministic recognizers/delivery consumer remain an explicit offline comparison seam only; production `run_tick` does not import or run them.
- **Composition policy:** the awareness workflow owns voice and routing. Deterministic code validates the complete plan before any send (including member pronouns and unranked-war claims), permits one wording-only repair, then fails closed so the event resurfaces next loop. There is no member-facing template fallback.
- **Delivery:** awareness validates hard-post coverage before any send, persists every planned post to `awareness_delivery_intents`, then advances each intent `pending → sending → fulfilled`. Explicit send failures return only that intent to pending; the next turn skips already-fulfilled posts. `awareness_posts` remains the delivered channel-memory ledger. A failed turn does not advance event cursors. A crash while an intent is `sending` fails closed during its 15-minute lease, then returns to pending for an at-least-once retry instead of wedging the outbox forever. The retired consumer creates `communication_intents` only as a connection-local TEMP table in explicit offline legacy rehearsals.
- **Clan management:** `engine/management.py` is the source of truth for every promote/demote/kick rule and constant. Human-readable policy is `prompts/POLICY.md`; original rationale (with the drifted parts marked) is `docs/reference/v5.1/management.md`. Elder promotion and demotion come from the **elder band** — a score ranks the non-leadership roster, the band sizes the corps, and hysteresis paces the moves. The three Layer-1 signals (`sustained_donor` / `war_reliable` / `battle_active`, 3-of-4-week hysteresis) are still computed and stored but **feed nothing**; they survive as rendered evidence only, which is why `WAR_QUALIFY_RATE` is commented "legacy … evidence rendering only". The kick path is separate: pure idle-days-from-battles arithmetic, reactive mid-tick through the policy gate. Promote/demote surface in the Monday 07:00 CT weekly review, the only place weekly counters roll. Engagement is measured from battles — `lastSeen`/logins are deliberately ignored. Every verdict carries `judgment_status` (`ready` / `held` / `unknown`), its evidence timestamp/reason, and the `materialization_id` that produced it; stale evidence fails closed and is excluded from actionable capability reads.
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
deterministic recognizer in #207. The awareness stack owns all proactive posting
(the brain plus the scoped responder, one delivery path). Emitter change sets must satisfy their event/table postconditions
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
- Tests use temp-file/in-memory SQLite and mocked external services (no API keys needed). ~2,070 tests, green in ~25 s.
- `tests/conftest.py` builds the current schema through `db.schema`, the same public builder used by runtime cold starts, into a session template copied per test.
- Test fixtures handle DB connection lifecycle — use `pytest.fixture` instead of manual try/finally.
- **The pre-commit hook and CI are one list: `scripts/gates.sh`.** `.githooks/pre-commit` is a 7-line shim that execs it. They used to be two hand-maintained lists that mirrored each other, and they drifted — pip-audit found three aiohttp CVEs the hook never ran, and three pushes failed CI ~20 s after committing clean. Do not enumerate the gates here; read the script. `git commit --no-verify` bypasses in an emergency.
- Piping a commit through `tail` swallows gate failures. Assert HEAD actually moved before pushing.

### Reality-based testing

Three levers beyond the unit suite — see [docs/reference/context-and-confidence-lessons.md](docs/reference/context-and-confidence-lessons.md).

### Confidence layer

Where silent failures go, and how to know Elixir is healthy: `logs/elixir-error.log` (small enough to read whole, which is the point), `tests/test_entrypoints_smoke.py`, `scripts/confidence_report.py`. **Elixir does not monitor itself** — a self-check ledger recorded 0 rows in 25 days while the log held 159 real errors, so both were retired 2026-07-28. Detection is an operator job (`AGENT-TEAM/error-watch.md`). Full account: [docs/reference/context-and-confidence-lessons.md](docs/reference/context-and-confidence-lessons.md).

## Cleanup

```bash
uv run --locked python scripts/clean.py
uv run --locked python scripts/clean.py --db
```

- default: remove cache directories like `__pycache__` and `.pytest_cache`
- `--db`: also remove legacy local runtime files (e.g. a stray `elixir.db`, `elixir.pid`) — it never touches `elixir-v51.db` or the archives

## Database

SQLite at `elixir-v51.db` (overridable via `ELIXIR_DB_PATH`; gitignored).
Four database files exist, with distinct roles:

- **`elixir-v51.db`** — the operational engine DB. `db/schema.py` is the canonical schema entrypoint for the private clean-break baseline and ordered post-cut evolution. `db.get_connection()` refuses databases without the v5.1 spine, migrates compatible v5.1 databases forward, and is the sole initializer.
- Durable memory lives IN the engine DB since 2026-07-04 (the v5.1 memory pass, `docs/reference/v5.1/memory.md`): `memories` + `memory_tags` + `memories_fts`, accessed through the `memory_store` seam. `inference` rows carry a 90-day default TTL and are reclaimed by db-maintenance; curated kinds (`leader_note`, `synthesis`, `system`) never expire by default (#215). The old `elixir-v5-memory.db` is archived (`elixir-v5-memory-archive-2026H2.db`, read-only); `ELIXIR_V5_MEMORY_DB` is retired.
- **`elixir-telemetry.db`** — LLM call telemetry, split out 2026-08-03 (`storage/telemetry.py`, `ELIXIR_TELEMETRY_DB_PATH`). Schema v34 dropped `llm_calls` from the clan DB because **every model call was taking the clan database's single write lock**. It is ~116 MB and **not covered by `scripts/backup_db.py`** — a known gap, not a design decision.
- **`elixir-v5-archive-2026H2.db`** — the pre-cut cold archive. Read-only (chmod 444), never written; open with `file:…?immutable=1`. **Not present on this workstation** — `db.schema.build_database()` and the test fixture treat it as optional and fall back to the frozen private migration-0 SQL in `db/`, which is why nothing has failed. Do not assume it is reachable; verify before planning any recovery around it.

**Historical recovery actually comes from the rolling backups** in `$ELIXIR_BACKUP_DIR` (see `scripts/backup_db.py`). Each nightly `.db.gz` froze the short-retention `raw_api_payloads` window as it stood on its own date, so their UNION reaches much further back than any single snapshot. `scripts/backfill_battle_fields.py` is the current worked example: it reads the live database plus every backup through the current extractor.

The engine DB follows the layered retention model (`docs/reference/v5.1/schema.md`):

- L1 API provenance: append-only `api_observation_receipts` plus deduplicated `raw_api_payloads` content (**60 d**, widened from 14 on 2026-07-30; the weekly purge cadence stretches the effective window up to 7 days further)
- L2 current-state baselines: `state_baselines` (diff substrate; not a read model)
- L3 event streams: `battle_events` (**730 d**), `player_events` (180 d), `clan_events` (365 d), `war_events` (365 d)
- L4 rollups (durable): `player_daily_metrics`, `player_daily_battle_rollups`, `clan_daily_metrics`
- L5 identity & tenure (durable): `players`, `player_metadata`, `player_aliases`, `clans`, `discord_users`, `discord_links`, `clan_memberships` — **the CR tag is the key everywhere**; "is X a member" = has an open `clan_memberships` row
- L6 projections (disposable, rebuilt from streams): `player_current_state`, `player_card_collection`, `player_recent_form`, `member_management`
- Awareness control: per-stream positions in `stream_cursors`, plus `awareness_thoughts`, `awareness_delivery_intents`, and `awareness_posts`. Standing concerns live in `memories` as `Watch:` / `Hold:` titles — the separate `watches` table was never written and was dropped in #211
- Award idempotency: `UNIQUE(award_type, season_id, section_index, player_tag)` on `awards`. The `recognition_ledger` that once mirrored it held intentless claims nothing read, and went with the recognizer in #207
- Clan management: `leader_action_recommendations`, `revisits`. The legacy `decision_cases` table and nullable leader-action link were removed by schema v21 after #216 made leader actions authoritative.
- Bounded war stream: `war_seasons` (durable), `war_weeks`, `war_week_clans`, `war_participation`, `war_attendance_days`
- Awards (durable): `awards` — `war_champ` is a ranked podium (season points); `iron_king` is PARTICIPATION (4/4 decks every battle day — unranked, any number earn it, never crown one); `rookie_mvp` = members in their FIRST war season; `free_pass` rotates to the highest-ranked War Champ who did NOT win it last month (`engine/emitters/war.py:close_season`). The LIVE in-progress races are computed on demand via `storage.awards.get_award_races` (top-10, points, tie-aware) and surfaced in the awareness read as `award_races`; `war_champ_lead_change` / `rookie_mvp_lead_change` events emit on a leader change.
- Engine control: `stream_cursors` (durable), `poll_state`, `runtime_job_status`, `materialization_runs`, `materialization_inputs`
- Ops telemetry (`prompt_failures`, `admin_command_invocations`) + tournaments star + the conversation set (`conversation_threads`, `messages`, `memory_episodes`). `llm_calls` moved to `elixir-telemetry.db` in v34; `memory_facts` was retired in the v5.1 memory pass — both are absent from the live DB

All `db` module functions accept an optional `conn` parameter — pass one in tests, omit in production.

### Schema changes

The private clean-break v5.1 baseline beside `db/schema.py` is migration 0.
All post-cut forward changes are ordered and versioned in that same schema
package; `db.get_connection()` applies them before returning. Runtime
and domain modules may validate required columns but must never issue `CREATE`
or `ALTER`. The retired pre-v5.1 migration history lives in Git and the cold
archive rather than executable runtime code. The committed fresh-schema fingerprint test changes with every
intentional schema change; it hashes the semantic contract (columns, checks,
keys, indexes, foreign keys, triggers, and virtual tables), so fresh
declarations and equivalent `ALTER TABLE` history compare equally.

Read `CURRENT_SCHEMA_VERSION` and `EXPECTED_TABLE_COUNT` from `db/schema.py`
rather than trusting a number here. **A new `_apply_vN` is a deploy** — it runs
against the live database on the next connection, so rehearse it on a copy with
`ELIXIR_DB_PATH` first.

Backups: `scripts/backup_db.py` covers the operational DB only. The cold
archive needs none (it never changes), but `elixir-telemetry.db` is genuinely
uncovered.

## Website Note

Elixir no longer publishes to poapkings.com — site publishing was removed
entirely on 2026-06-21 (the website has its own standalone update script), and
the `poapkings-com` lane is gone too. Don't add site-publish behavior back
into the bot.

The site does still carry clan policy, though: `src/members.njk` and
`src/faq.njk` explain how Elder works, hand-copied from
`engine/management.py`. Retuning an elder constant means editing them.

## Agents And Lanes

Elixir has one identity and several executable workflows. Discord destinations are **lanes**, not independent agents.

Core rule: one signal is not one post. The awareness loop reads the whole current situation, decides which moments deserve communication, and may combine several events into one post while proving coverage for every hard-post signal.

**Lanes are declared in `prompts/DISCORD.md` and parsed by `runtime/lanes.py`.**
That file is the list; lane keys resolve by exact string, so a name invented
here would simply fail to resolve. There are 8 today, and the two worth knowing
before you read the file are `elixir` (the awareness brain's public voice —
"everything worth *saying* about the game lives here") and `actions` (leader
action cards, and the fail-closed destination for unknown intent prefixes).
`#thinking` documents itself as *not* a lane: it carries decision transcripts,
replacing the old `#elixir-log` webhook on 2026-07-09.

> Four lanes listed here until 2026-08-05 no longer exist — `river-race`,
> `member-highlights`, `clan-events`, `poapkings-com` — two of whose channels
> were deleted 2026-07-11. `tests/test_prompts.py` actively asserts their
> absence, so this prose was contradicted by a passing test.

Current executable workflows (specs in `agent/workflow_registry.py`):
- `awareness` — the deliberative brain. Reads current streams, projections,
  history, and channel memory, then returns one structured post plan.
- `wake_response` / `wake_response_chat` — the scoped responder (Phases 1-2,
  live). An engine-tick wake evaluator (`runtime/awareness/wake.py`) picks up
  qualifying events and `runtime/awareness/respond.py` composes a single
  focused post on the shared chassis (`agent/chassis.py`), escalating Haiku →
  Sonnet → an out-of-band brain run. Gated by `ELIXIR_WAKE_RESPONDER`.
  **Which events it claims, and which surfaces each job may speak on, is the
  `JOBS` table in `respond.py` — data, never a code path.** Five jobs today:
  welcome, farewell, role_change, podium, milestone_batch. Adding one means a
  row plus a `prompts/jobs/*.md` file (hyphenated: `role_change` reads
  `role-change.md`). If anything else needs to change per event type, stop —
  that is v4's `delivery.py` growing back.
- `interactive` — public read-only conversation in member-facing lanes.
- `clanops` — private leadership conversation with gated write tools.
- `reception` — constrained onboarding and identity-verification replies.
- `memory_synthesis` — weekly memory hygiene and canonical arc synthesis.
- `content` workflows — recruiting, weekly recap, other publishable content.
- specialists such as `deck_review`, `tournament_update`, `clan_chat_copy`,
  `intent_router`.

**One delivery owner, no longer one author.** Since Phase 1 there are two
composing paths (the brain and the responder), but every post still leaves
through `runtime/awareness/deliver.deliver_posts`, and posting itself is a tool
call validated by `agent/post_validation.py`. Say "one delivery owner" — the
older "sole proactive voice" phrasing is no longer true. Plan:
`docs/plans/agentic-loop.md`.

## Recurring Activities

The canonical source of truth for scheduled automated work is `runtime/activities.py`, not scattered scheduler calls or prose docs.

Each activity declares:
- owner lane
- purpose
- schedule
- executor function
- delivery targets
- whether manual triggering is allowed

**Read the list from `runtime/activities.py`.** It is ~20 activities and it
changes; a copy here drifted to missing six of them, including the brain's own
cadence. Only the load-bearing *shape* belongs in this file:

- **The engine heartbeat is `engine-tick`** — `_engine_tick`, every 10 minutes,
  `max_instances=1`: one `engine.tick.run_tick` pass (poll → atomic observation
  apply → readiness-gated manage), plus leader-action card posting and the wake
  evaluator. It replaced the deleted `v5-reactive-tick`, `war-poll`,
  `player-progression`, and `award-detection` activities — awards now grant on
  the war stream's `season_closed` event, and polling is the adaptive
  scheduler's job.
- **The brain runs on a cron, not continuously** — `awareness-loop`, **twice**
  a day since 2026-08-05 (Phase 2), down from four. The scoped responder covers
  the hard posts within a tick, so the cron is for deliberation: digest signals,
  trends, and the backstop sweep.
- **Weeks roll in exactly one place** — `weekly-leadership-review`, Mon 07:00
  America/Chicago. Hysteresis counters advance nowhere else. The old
  `leadership-action-scan` is gone; its role lives in the engine's reactive
  kick path.
- **War attendance finalizes before the war-day boundary** —
  `war-attendance-snapshot` daily at 04:15 CT, ahead of the observed
  ~09:37-10:00 UTC boundary. Evaluators read finalized days only. Anchor
  war-day math on the *observed* period-start, never a fixed hour.
- **Tournaments are not a registry entry** — the watch is leader-started and
  stopped (`runtime/jobs/_tournament.py`), a dynamic job that resumes on
  restart.
- `engine-health` was retired 2026-07-28: production-problem detection is an
  operator/AGENT-TEAM job (`AGENT-TEAM/error-watch.md`), not a function of the
  clan bot.

## Architecture: Prompts vs Code

Principle: **Prompts define what Elixir says and why. Code defines when, where, and how.**

### Prompt files (`prompts/`)

- `SOUL.md` — Elixir's persistent identity, stance, and non-human sense of self.
- `PURPOSE.md` — Elixir's mission, responsibilities, and guardrails.
- `GAME.md` — Clash Royale mechanics (game-generic, rarely changes).
- `CLAN.md` — Clan-specific identity, rules, history, and thresholds (inactivity, donation highlights, clan lore). Its clan-management numbers are a **mirror** of `engine/management.py`, not a source.
- `POLICY.md` — how Elder is earned, held and lost, plus the removal rules. Leader-facing prose, injected into ~9 system prompts. Must match `engine/management.py`; `tests/test_cr_knowledge.py` asserts it against the imported constants.
- `DISCORD.md` — Declarative Discord channel contract: IDs, lanes, workflows, reply policies, memory scope, and durable-memory flags. The engine resolves lane→channel from this file at runtime (no hard-coded channel ids).
- `lanes/*.md` — Destination-lane behavior prompts.
- `agents/*.md` — Executable workflow prompts for awareness, memory synthesis, routing, and specialist agents.
- `jobs/*.md` — Per-job prose read by the chassis (`agent/chassis.py`), one file per responder job (e.g. `welcome.md`).

All of these **hot-load** — `prompts/*.md` is re-read on every call, so a
prompt-only fix needs no restart.

### What stays in code

Activity scheduling, channel routing, stream emission, hard-post floors, copy-policy invariants, outcome fan-out, delivery bookkeeping, tool execution, JSON response contracts, memory enforcement, nickname matching, LLM parameters, Elixir data normalization, and in-game clan chat copy guardrails. The awareness model makes editorial worthiness judgments; code still owns factual and delivery invariants.

### A "DO NOT" in a prompt is a bug report about the tool layer

Every `NEVER` / `DO NOT` / `don't` in `prompts/` is a suspect. Almost all of them
were written the day a model said something wrong, and the fastest fix was a
sentence telling it not to. That sentence then ships on **every call, forever** —
we rent the workaround while the defect stays. Worse, a rule the data can violate
is a rule the model will eventually break anyway.

**Before adding one, try to make it unnecessary:**

| Prompt says | Usually means | Real fix |
|---|---|---|
| "never quote X from memory" | X is stale or absent in the read | put the live value in the data |
| "never confuse A with B" | A and B are named alike, or share a field | rename at the source so they cannot be confused |
| "don't repeat a post/write" | dedup is not enforced | a key/constraint in the store |
| "don't cite N below a sample floor" | the view hands over a weak number | omit it in the capability, return `insufficient_sample` |
| "that field is unreliable" | it is | fix, or stop sending it |

**When adding one is right:** the rule needs judgment a constraint cannot express
(semantic duplication, editorial worthiness, tone), or it reinforces a fix already
shipped in code and is cheap insurance. Say which, in the prompt, with the date.

**When deleting one is right:** the defect it guarded is fixed. Grep the negative
instructions periodically and re-check each against live data — several outlive
their cause. Verify end to end before deleting: the join-floor rule looked like a
fossil until `prompts.py:_live_required_trophies()` was confirmed to substitute
the live value into what the model actually receives, which made it cheap
reinforcement rather than a mask.

The same discipline applies to prompt text that *describes* data. Check documented
fields against real captured payloads — `season_window` and `roster_vitals` were
documented in the awareness prompt long after they stopped being sent.

### Prompts work with the player; tools do the answering

The rule above is the negative case. The general one: **a prompt owns the
conversation, a capability owns the domain.** Prompts should be about understanding
what the member wants, asking when it is ambiguous, and how to say the answer. The
answer itself — what a deck needs, what counts as an air answer, which cards to
name — belongs in a capability, where it is computed once, tested, and identical
across every surface that asks.

Domain knowledge written as prose in a prompt fails three ways at once: it is
untestable, it is invisible to the weekly report, and the model
follows it only approximately. Measured on the `deck_review` prompt (2026-08-01):

| Prompt was doing | Tool already did it |
|---|---|
| "list all 32 cards and verify no duplicates" | `war_set` is disjoint by construction and returns `distinct_cards` |
| "avoid four variants of the same archetype" | `_pick_disjoint` prefers a different family per pick |
| "cite WHY for each card (win condition, anti-air…)" | `role_coverage` + per-card `roles`, from enriched `card_facts` |
| "call lookup_cards before computing elixir" | `avg_elixir` and per-card `elixir_cost` ship in the payload |

That last one was not merely redundant: it drove a 30+ call fan-out that blew the
output limit and left a member with no reply at all. Prose asking the model to do
arithmetic the tool has already done is a latency and reliability bug, not just
clutter.

When you find yourself writing domain rules into a prompt, that is the signal the
capability is missing a field.

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
  - `screenshot_readout` -> read tools only
  - `channel_update` -> read tools only
  - `channel_update_leadership` -> read tools only
  - `interactive` -> read tools only
  - `clanops` -> read + write tools
  - `reception` -> no tools
  - `roster_bios` -> read tools only
- Write tools are gated by workflow policy. Four workflows carry them today:
  `clanops`, `awareness`, `wake_response`, `wake_response_chat`; every other
  workflow is read-only or toolless.
- **There are THREE tool-name sets, and that is the trap this bullet exists
  for.** `_WRITE_TOOL_NAMES` (clanops), `AWARENESS_WRITE_TOOL_NAMES`
  (awareness), and `_SURFACE_TOOL_NAMES` (`post_to_discord`,
  `post_to_clan_chat` — handed out per turn by the chassis, deliberately
  excluded from `ALL_TOOLS`). Adding a tool to one set reaches only that
  audience: a shipped tool was once offered to a model **zero** times because
  it landed in `_WRITE_TOOL_NAMES` alone. Check all three, and check
  `ADVERTISED_TOOL_EXECUTOR_NAMES` / `SURFACE_TOOL_EXECUTOR_NAMES` too.
- Tool outputs are wrapped in a compact envelope (`ok`, `error`, `truncated`, `meta`, `data`) and truncated for context budget safety.
- Leader/member factual answers should prefer structured query tools over clipped roster context. Resolve members by name/Discord handle before using tag-based tools when needed.
- Strict JSON workflow contracts are validated in code with one repair retry:
  - `screenshot_readout`: requires `event_type`, `summary`, `content` (or `null`)
  - `channel_update` / `channel_update_leadership` / `interactive` / `clanops`: require `event_type`, `summary`, `content`
  - `clanops` `channel_share` responses also require `share_content`
  - `reception`: requires `event_type=reception_response` and `content`
  - `roster_bios`: requires `intro` and `members` map
- Loop telemetry is logged per request: workflow, tool rounds, tools called, denied tools, validation failures, prompt/completion size estimates, and completion latencies.
- Channel/reception failures are also persisted in `prompt_failures` with the cleaned question text, workflow, failure type/stage, Discord metadata, result preview/raw JSON, and the last LLM error/model snapshot.
- Reply behavior is enforced in code from channel config:
  - `mention_only` for channels like `#clan-chat` and `#leaders`
  - `open_channel` for `#ask-elixir`
  - `disabled` for notification-only channels like `#announcements`
- `#actions` is normally action-board style with disabled general replies, but `runtime/channel_router.py` special-cases leader-posted Clash Royale screenshots as observation evidence and replies with a concise `leader_screenshot_observation` readout.

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
  - recruiting content uses `MAX_CONTEXT_MEMBERS_FULL` (50) — this was "site generation" until 2026-08-05, but site publishing was removed 2026-06-21 and `generate_promote_content` is the only remaining caller
- When clipping occurs, context includes an omitted-members summary line.

### Auditing a read block

Ask what a block could ever CHANGE the brain's behaviour about. Match literal values, not themes — and remember some blocks work by *preventing* output, so absence from posts is them succeeding. Worked examples: [docs/reference/context-and-confidence-lessons.md](docs/reference/context-and-confidence-lessons.md).

## Announcements and API drift

The `system_signals` queue was retired in #212 — it had no drain, so nothing it held was ever delivered. The two things it used to carry now have real owners:

- **Feature / release news** → `scripts/cut_release.py`: RELEASES.md, a #announcements post, and email to members with a verified address. One flow, already used for every release.
- **CR API drift** → the `api-sentinel` activity records first-seen schema paths into `api_sentinel_observations`. Nothing in the runtime evaluates them: the Operations Manager runs the 48h *structural*-drift query (new schema path, progress key, or game mode — never routine new event tags, which are pure noise) from `AGENT-TEAM/error-watch.md` step 5. The hand-off is deliberately thin; the AGENT-TEAM **Data Analyst** owns characterizing it and filing the issue.

Elixir also posts a startup check-in to the #elixir-log webhook with the running build hash and a short Clash Royale-flavored line.

## Query Layer (Current)

Elixir’s core member/leader questions should be answered from structured capabilities, query helpers, and tools, not prompt reconstruction. Shared domain answers live in `capabilities/`; LLM tools are adapters over those contracts rather than their sole owners. The versioned capability layer covers canonical game truth, clan game modes, live/season war intelligence, facet-based member intelligence, deck and clan-local metagame intelligence, authoritative management decisions, and provisional-versus-durable awards. These contracts feed tools, awareness, reports, memory synthesis, and admin reads. External API refresh remains outside member capabilities, and management capabilities package the engine verdict without rescoring it.

The LLM has a domain-aligned tool surface defined in `agent/tool_defs.py`, with
one owner per question. Count it from `TOOLS` / `SURFACE_TOOLS` rather than
quoting a number here — a stale "14" survived four additions. Domains:

- **Member domain**: `resolve_member`, `get_member` (include: profile, form, battles, war, trend, deck, losses, history, memories, chests, awards), `get_member_war_detail` (aspect: summary, attendance, battles, missed_days, vs_clan_avg, war_decks)
- **River Race domain**: `get_river_race` (live race state + competing clan standings, read off the war clock)
- **Clan domain**: `get_clan_roster` (aspect: list, summary, recent_joins, longest_tenure, role_changes, max_cards, card_owners, donations, trends)
- **Deck, card + awards domain**: `get_deck_intelligence`, `get_deck_recommendations`, `read_deck_link`, `lookup_cards`, `get_member_cards`, `get_awards`
- **Battle + game-mode intelligence**: `get_battle_intelligence`, `get_game_mode_performance`
- **Elixir state + utility**: `get_elixir_state`, `cr_api`, `save_clan_memory`, `record_leadership_followup`, `lookup_reference`
- **Surface tools** (`post_to_discord`, `post_to_clan_chat`): the most consequential writes Elixir has. They are deliberately **not** in `ALL_TOOLS` — the chassis hands them out per turn via `agent.chassis.surface_tools`, and they stage rather than send, so `deliver_posts` stays the single delivery path.

War tools include `war_player_type` (regular/occasional/rare/never) per member. Management judgments come from the deterministic management capability and leader-action pipeline, not a parallel LLM health tool.

### Mostly LLM

Almost every message Elixir sends is LLM-generated. Events, scheduled activities, and channel replies pass context to the LLM, which crafts the message using Elixir's identity from `SOUL.md` + `PURPOSE.md`, channel contract from `DISCORD.md`, lane behavior from `lanes/*.md`, and workflow-specific guidance from `agents/*.md` where applicable.

Exceptions: preauthored system-signal announcements may be written directly in code. Awareness posts have no deterministic member-facing fallback: invalid copy gets one bounded LLM wording repair, then the tick fails closed and retries from evidence on the next loop.

### Portability

A new clan forks elixir-bot and primarily rewrites `CLAN.md` and `DISCORD.md`, plus any lane prompts that reflect their own server culture. `SOUL.md`, `PURPOSE.md`, `GAME.md`, and most agent prompts should stay mostly portable.

The clan-management constants live in **`engine/management.py`** and nowhere
else. `prompts/CLAN.md` and `prompts/POLICY.md` mirror them as prose for the
model and for human leaders, and both have drifted from the engine before — so
a retune is a four-file change (engine, POLICY.md, and the two poapkings.com
pages). The comment above `ELDER_BAND_FLOOR` names them all.

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
