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

### Enable the commit gates (one time per clone)

```bash
git config core.hooksPath .githooks
```

`.githooks/pre-commit` runs `scripts/gates.sh`, which is the **same list CI
runs** — lock check, dependency CVE audit, docs, exception policy, ruff, mypy,
tests with coverage, and the war-week simulation. Without this config the hooks
directory is inert and nothing checks a commit.

Gates belong in `scripts/gates.sh` and nowhere else. A gate added to CI alone is
invisible until it fails after a push: that is exactly what happened on
2026-08-03, when a `pip-audit` CVE disclosure failed three consecutive pushes
that had each passed cleanly on the way out. `tests/test_ci_local_parity.py`
fails if CI or the hook stops using the shared script.

The audit gate needs network, so it cannot pass offline — that is a true
failure, not a false one, since the push would fail in CI too.

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

The default operational database is `elixir-v51.db`. An empty database and all
bounded forward migrations are initialized through `db/schema.py`. A non-empty
database without the v5.1 spine is refused; it is never rebuilt or upgraded in
place.

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
cat logs/elixir-error.log
uv run --locked python scripts/confidence_report.py --json
```

Do not call a deployment complete merely because the test suite passed. Inspect
fresh tick/awareness output, the error log, and the relevant Discord behavior.

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
bash scripts/admin.sh activity run api-sentinel
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

System telemetry and awareness-loop inspection are read from the databases
directly: the `log-triage`, `awareness-report` and `llm-cost-report` skills,
`scripts/admin.sh`, `runtime/tick_history.py`, or plain SQL against
`elixir-v51.db` / `elixir-telemetry.db`.

### Wake shadow report (Agentic Loop v2, Phase 0)

The wake evaluator runs at the end of every engine tick and records what it
*would* have fired to `wake_observations` in the telemetry database. It is
measurement only — Phase 0 composes nothing and posts nothing.

```bash
uv run --locked python scripts/wake_shadow_report.py --days 7
uv run --locked python scripts/wake_shadow_report.py --simulate --days 20
```

`--simulate` replays historical events through the current wake policy instead
of reading live shadow rows, so a policy change can be evaluated against real
history immediately rather than after a week of observation. Wake policy itself
lives on the event contracts in [engine/event_contracts.py](engine/event_contracts.py);
`ELIXIR_WAKE_POLICY=0` disables evaluation entirely. See
[docs/plans/agentic-loop.md](docs/plans/agentic-loop.md).

## Health and error checks

Elixir does not monitor itself. `logs/elixir-error.log` (ERROR+ with tracebacks,
rotated at 2 MB × 5) is the operator interface; reading and triaging it is the
Operations Manager's job, per `AGENT-TEAM/error-watch.md`. It is deliberately
small enough (~6 lines/day) to read in full:

```bash
cat logs/elixir-error.log
```

Run the consolidated report, which groups that log by error kind and adds the
`liveness` silence alarm:

```bash
uv run --locked python scripts/confidence_report.py --json
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
- Baseline and forward schema: [db/schema.py](db/schema.py)
- Runtime confidence: [scripts/confidence_report.py](scripts/confidence_report.py)
