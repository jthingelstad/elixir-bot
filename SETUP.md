# Elixir Bot — Setup and Operations

This is the operator guide for local and production execution. For architecture
and repository rules, read [AGENTS.md](AGENTS.md). For a shorter orientation,
read [README.md](README.md).

## Install

Elixir supports Python 3.14. Reproduce the locked development environment:

```bash
cd ~/Projects/elixir-bot
uv sync --locked
uv lock --check
uv run --locked pytest tests/ -q
```

Production uses `uv sync --locked --no-dev`. Direct dependencies live in
`pyproject.toml`; `uv.lock` is the only resolved dependency lock.

## Configure

Create `.env` in the repository root.

Required secrets:

```env
DISCORD_TOKEN=...
CLAUDE_API_KEY=...
CR_API_KEY=...
```

Common optional settings:

```env
ELIXIR_DB_PATH=/absolute/path/to/elixir-v51.db
ELIXIR_LOG_WEBHOOK_URL=...
ELIXIR_LOG_WEBHOOK_USERNAME=Elixir
ENGINE_TICK_MINUTES=10
AWARENESS_LOOP_MINUTE=5
```

Activity-specific schedule overrides are read by
[runtime/activities.py](runtime/activities.py) from the runtime module. Prefer
the registry defaults unless there is an operational reason to change them.

Non-secret configuration is checked in:

- [prompts/DISCORD.md](prompts/DISCORD.md) — channel IDs, lanes, reply policy,
  tool policy, and memory scope.
- [prompts/CLAN.md](prompts/CLAN.md) — clan identity and policy constants.

## Database safety

The default operational database is `elixir-v51.db`. An empty database is
initialized from `scripts/migrate_v51/schema_v51.py` and then receives bounded
forward migrations from `db/schema.py`. A non-empty database without the v5.1
spine is refused; it is never rebuilt or upgraded in place.

The immutable pre-cut archive is `elixir-v5-archive-2026H2.db`. Never point the
running bot at it and never change its read-only permissions. It is **not
present on this workstation**; every consumer treats it as optional, so verify
it exists before planning any recovery around it. For historical data the
rolling backups (below) are the practical source — see AGENTS.md.

Create an operational backup before every runtime deployment:

```bash
bash scripts/admin.sh backup
```

## Local run

```bash
uv run --locked python elixir.py
```

A healthy startup connects to Discord, registers enabled activities from the
registry, resumes any active tournament watch, seeds one-time system signals,
and sends a build check-in to the `#elixir-log` webhook when configured.

## Production with launchd

`launchd` is the process owner. Do not mix it with `nohup`, `pkill`, or a
background shell process.

Install the plist once, then start the service:

```bash
bash scripts/admin.sh install
bash scripts/admin.sh start
bash scripts/admin.sh status
```

Normal controls:

```bash
bash scripts/admin.sh stop
bash scripts/admin.sh restart
bash scripts/admin.sh status
```

The helper targets `~/Library/LaunchAgents/com.poapkings.elixir.plist` and logs
stdout/stderr to `./elixir-v5.log`.

## Deploy or update

The supported update path backs up the database, stops the service, fast-forward
pulls `main`, installs the exact production lock, and starts the service:

```bash
bash scripts/admin.sh upgrade
```

After any runtime change, verify both process state and fresh behavior:

```bash
bash scripts/admin.sh status
tail -100 elixir-v5.log
uv run --locked python scripts/confidence_report.py --json
```

Do not call a deployment complete merely because the test suite passed. Inspect
fresh tick/awareness output, open incidents, and the relevant Discord or
Observatory behavior.

## Scheduled activities

[runtime/activities.py](runtime/activities.py) is the only schedule source of
truth. The two heartbeats are:

- `engine-tick` — refreshes raw data, streams, projections, and management
  state through poll → ingest → emit → project → manage.
- `awareness-loop` — reads that state, deliberates, and owns proactive public
  posting.

Inspect or run manual-safe activities through the leadership surface:

```text
/clanops activity list
/clanops activity show
/clanops activity run
```

Shell operators can use the same registry contract without starting a second
Discord gateway session:

```bash
bash scripts/admin.sh activity run engine-health
uv run --locked python -m runtime.activity_runner run daily-clan-insight
```

Activities with `manual_trigger_allowed=False` will refuse a manual run.

## Leadership commands

Member email self-service lives under `/elixir`. Operator commands live under
`/clanops` and are restricted by channel and role.

Common leadership operations include:

```text
/clanops clan status
/clanops clan war
/clanops clan members
/clanops member show
/clanops member set
/clanops relay status
/clanops activity list
/clanops tournament status
```

Use the Observatory for system telemetry, incidents, awareness-loop inspection,
and other management views removed from Discord.

## Health and incident checks

Run the consolidated report:

```bash
uv run --locked python scripts/confidence_report.py --json
```

Inspect unresolved best-effort failures directly when needed:

```bash
sqlite3 elixir-v51.db \
  "SELECT at, component, summary, detail FROM runtime_incidents WHERE resolved_at IS NULL ORDER BY at DESC LIMIT 50"
```

For model, validation, or channel failures:

```bash
uv run --locked python scripts/review_agent_feedback.py --limit 20
uv run --locked python scripts/review_agent_feedback.py --workflow clanops --json
```

For engine changes, run the reality gates before deployment:

```bash
uv run --locked python scripts/replay_gate.py
uv run --locked python scripts/simulate.py
```

## Logs

Two rotating files under `logs/`, written by `runtime/logging_setup.py`:

| File | Level | Rotation | Purpose |
|---|---|---|---|
| `logs/elixir.log` | INFO+ | 10 MB × 5 | The full narrative. |
| `logs/elixir-error.log` | ERROR+ | 2 MB × 5 | **Watch this one.** Errors with tracebacks, ~6 lines a day. |

The split exists so the error log can be read in full rather than grepped. At
current volume `elixir-error.log` is small enough for an agent to read end to
end every few minutes and act on what it finds.

`elixir-v5.log` still receives launchd's stdout/stderr capture. That is the
catch-all for anything that never reaches logging — interpreter tracebacks on a
hard crash, third-party prints — not the record.

Useful reads:

```bash
tail -100 logs/elixir-error.log     # what is broken right now
tail -100 logs/elixir.log           # what happened around it
rg 'prompt_failure' logs/elixir.log
```

Set `ELIXIR_LOG_DIR` to relocate both files, or `ELIXIR_LOG_LEVEL` to change
the main log's threshold. The error log is always ERROR+.

## Stateful files and retention

- `elixir-v51.db` — live operational state, streams, projections, management,
  conversation, and durable memory.
- `elixir-v51.db-wal` / `elixir-v51.db-shm` — SQLite WAL sidecars while live.
- `elixir-v5-archive-2026H2.db` — immutable cold archive (absent here; optional).
- `$ELIXIR_BACKUP_DIR/*.db.gz` — rolling nightly backups. Each froze the
  short-retention `raw_api_payloads` window on its own date, so together they
  are the real historical record; treat them as recoverable evidence, not just
  disaster-recovery copies.
- `elixir-v5.log` — active launchd log.
- `.env` — local secrets; never commit it.

The database has layered retention: raw API payloads are short-lived, event
streams are bounded, and identity, rollups, awards, management history, and
memory are durable. `db-maintenance` applies the configured retention policy.

## Cleanup

Remove caches:

```bash
uv run --locked python scripts/clean.py
```

Also remove known legacy runtime files:

```bash
uv run --locked python scripts/clean.py --db
```

The cleanup command never removes `elixir-v51.db` or either archive.

## Operator source-of-truth map

- Architecture and policy: [AGENTS.md](AGENTS.md)
- Activity registry: [runtime/activities.py](runtime/activities.py)
- Channel contract: [prompts/DISCORD.md](prompts/DISCORD.md)
- Process helper: [scripts/admin.sh](scripts/admin.sh)
- Baseline schema: [scripts/migrate_v51/schema_v51.py](scripts/migrate_v51/schema_v51.py)
- Forward schema changes: [db/schema.py](db/schema.py)
- Runtime confidence: [scripts/confidence_report.py](scripts/confidence_report.py)
