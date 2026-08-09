---
name: log-triage
description: Analyze Elixir's logs plus the telemetry and clan databases for failures, DB write-lock stalls, recurring issues, and operational signals, then recommend concrete actions
---

# Log Triage

Surface what's actually going wrong (vs. noise), group recurring issues, and hand the user a short prioritized action list. The goal is to answer "what should I fix next?" — not to paraphrase the log.

Triage has three evidence sources, not one: the **log files**, the **telemetry DB** (`elixir-telemetry.db`), and a few **operational tables in the clan DB** (`elixir-v51.db`). The log says what broke; telemetry says what was slow, expensive, or holding the write lock; the clan DB says which scheduled jobs actually ran and which member-facing prompts failed.

## Sources

### Log files

Start with the error log. It exists so that this triage is cheap.

| File | Contents | How to use it |
|---|---|---|
| `logs/elixir-error.log` | ERROR+ with tracebacks | **Read this first.** It is the list of things that are broken. Check its size before reading in full (see below). |
| `logs/elixir.log` | INFO+, the full narrative | Read *around* an error's timestamp for context, and for the health checks below. |
| `logs/elixir-control.log` | Restart ledger, one line per `scripts/admin.sh` restart: timestamp, pid, `revision=<sha>`, `reason=` | **Check this early.** Errors that start or stop at a restart boundary are a deploy story, not a runtime story. |
| `elixir-v5.log` (repo root) | launchd stdout/stderr catch-all | Only when a crash left nothing in the rotating logs — an interpreter traceback that killed the process, or third-party output. Not the record. Nothing rotates it. |

Rotation (`runtime/logging_setup.py`): `logs/elixir.log` at 10 MB × 5 backups, `logs/elixir-error.log` at 2 MB × 5 backups. `.1`–`.5` siblings exist only *after* a rotation has happened — do not assume they are there. `elixir-control.log` and `elixir-v5.log` are not rotated by the app at all.

The old "~6 lines/day, read it in full" rule no longer holds — the error log is bursty. It sat at 247 KB / 148 ERROR lines across ten days on 2026-08-06, and 110 of those came from a single bad afternoon. So: `wc -l logs/elixir-error.log` first. Under a few hundred lines, read it whole. Larger, scope it by date prefix and count signatures instead — but still look at what came *before* your window, because knowing whether a signature is new is most of the finding.

### Telemetry DB — `elixir-telemetry.db`

Split out of the clan DB on 2026-08-03 because every model call was taking the clan database's single write lock. ~119 MB, and **not covered by `scripts/backup_db.py`**.

> **Read-only, always.** Per `AGENTS.md`, this file is admin-only and Elixir's behaviour may never depend on it. For triage that means: open it `file:elixir-telemetry.db?mode=ro`, never write, and never let a finding here become an input to something that decides. It is evidence for a human, not state.

| Table | Rows about | Triage value |
|---|---|---|
| `db_transactions` | Every write transaction held ≥250 ms (`ELIXIR_DB_REPORT_MS`): `call_site`, `held_ms`, `statements`, `outcome`, `sites_json` | The main SQL signal. Long holds are what starve the single writer. |
| `db_stalls` | Write transactions open past 45 s (`ELIXIR_DB_STALL_SECONDS`), with a full `thread_dump` | Rare and decisive. One row names the culprit outright. |
| `llm_calls` | Per-call workflow, model, `ok`, `error`, `duration_ms`, token and cache counts — plus, since 2026-08-09, what the call was *asked* to do (`effort`, `max_tokens`, `timeout_s`) and what happened (`stop_reason`, `block_census`, `attempts`, `cost_usd`, `turn_id`) | Failed/slow model calls. Cost analysis belongs to `/llm-cost-report`, not here. |
| `wake_observations` | What the wake evaluator considered, `fired` 0/1, and `reason` when held | Scoped responder (Phase 2) holding back or firing too often. |
| `wake_episodes` | Responder runs: `job`, `workflow`, `tier`, `handled`, `delivered`, `reason` | An episode that was handled but not delivered is a real failure. |

Retention: LLM calls 90 d (prompt/response blobs 14 d), DB metrics 30 d.

**There is no lock-wait table, and waiting is not measured anywhere.** A `db_lock_waits` table existed from the 2026-08-03 split until 2026-08-06 and recorded zero rows for its whole life, because nothing ever called its writer; it was dropped from the live file along with the code. Should you meet it in an older copy of the telemetry DB, do not read "no lock contention" off it — it measures nothing. Waiting on the lock is not observable from Python at all: `PRAGMA busy_timeout` makes SQLite block inside the C layer, and `sqlite3` exposes no busy-handler callback. Contention surfaces as `database is locked` in `logs/elixir-error.log`; its **cause** is a long hold, which `db_transactions` and `db_stalls` do measure.

### Clan DB — `elixir-v51.db`

**`?mode=ro` on this file fails most of the time — that is expected, not a fault.** The runtime opens the clan DB per operation and closes it, and SQLite deletes the `-shm` sidecar on the last clean close. A WAL database needs `-shm` to be read, and a *read-only* handle is not permitted to create one, so between operations you get:

```
Error: in prepare, unable to open database file (14)
```

Open it plainly and issue only `SELECT`s:

```bash
cd /Users/otto/Projects/elixir-bot && sqlite3 -header elixir-v51.db "SELECT ..."
```

This is safe: in WAL mode readers never block the writer and a `SELECT` takes no write lock. The read-write *handle* only buys the right to create the `-shm` file, which is exactly what was missing. (An earlier version of this skill claimed the opposite and told you to use `mode=ro`; that advice blocked two triage passes before it was measured.)

**Never use `immutable=1` on the live clan DB.** It reads, but it tells SQLite the file cannot change, disabling locking entirely; against a database the bot is actively writing that can return torn or inconsistent rows. It is correct only for the frozen cold archive, which never changes.

The telemetry DB does not have this problem — the runtime holds a per-thread connection open, so `file:elixir-telemetry.db?mode=ro` works and stays the right way to read it.

| Table | Triage value |
|---|---|
| `runtime_job_status` | One row per job, `status_json` carries `run_count` / `success_count` / `failure_count` / `last_success_at` / `last_failure_at` / `last_error` / `last_summary`. The authoritative "did the scheduler actually run it" answer. |
| `prompt_failures` | Agent-response failures that reached a member: workflow, failure type/stage, channel, question text, `llm_last_error`. `scripts/review_agent_feedback.py` is the friendlier reader. |
| `admin_command_invocations` | Leader/admin command use, including rejected ones (`accepted=0`). |

## Timestamps — get the comparison right

**Two different formats are in play, and the obvious query is wrong for both.**

- Telemetry DB: `2026-08-06T11:54:54Z` — ISO, `T` separator, **`Z` suffix**.
- Clan DB (`prompt_failures`, `runtime_job_status`): `2026-08-06T16:32:15` — ISO, `T` separator, **no suffix**.
- Log files: `2026-08-06 10:52:19,156` — space separator, local time (America/Chicago). The DBs are UTC. A log line and a telemetry row for the same event are ~5 hours apart; convert before you correlate.

SQLite's `datetime('now','-1 day')` returns `2026-08-05 16:36:39` — a **space** separator. Comparing that against a `T`-separated column is a string comparison where `'T' > ' '`, so every row from earlier in the cutoff day leaks into the window. Measured on 2026-08-06: the naive form returned 4242 rows where the correct one returned 2428, a 75% over-count.

Use a cutoff that matches the stored format:

```bash
# telemetry DB (Z suffix)
sqlite3 "file:elixir-telemetry.db?mode=ro" "SELECT count(*) FROM db_transactions WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day');"
```

```bash
# clan DB (no suffix)
sqlite3 elixir-v51.db "SELECT count(*) FROM prompt_failures WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%S','now','-1 day');"
```

## Scope

Default to the last 24 hours unless the user specifies a window (e.g. "since the last check", "since yesterday's deploy"). For log files, filter by timestamp prefix; for the DBs, use the `strftime` cutoffs above.

## Starter queries

Run these against the read-only URIs. They are the standing set; widen or narrow the window as the user's scope requires.

```bash
cd /Users/otto/Projects/elixir-bot && sqlite3 -header "file:elixir-telemetry.db?mode=ro" "
SELECT recorded_at, call_site, held_ms, statements, outcome, sites_json
FROM db_transactions
WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')
ORDER BY held_ms DESC LIMIT 10;"
```

```bash
cd /Users/otto/Projects/elixir-bot && sqlite3 -header "file:elixir-telemetry.db?mode=ro" "
SELECT recorded_at, call_site, open_ms FROM db_stalls
WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 day') ORDER BY recorded_at DESC;"
```

```bash
cd /Users/otto/Projects/elixir-bot && sqlite3 -header "file:elixir-telemetry.db?mode=ro" "
SELECT workflow, model, count(*) n, substr(max(error),1,80) sample
FROM llm_calls WHERE ok=0 AND recorded_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')
GROUP BY workflow, model ORDER BY n DESC;"
```

Three failure modes that used to need live instrumentation are now columns. A
truncation or a wasted round trip is a *successful* call by `ok`, so these will
not appear in the query above:

```bash
cd /Users/otto/Projects/elixir-bot && sqlite3 -header "file:elixir-telemetry.db?mode=ro" "
SELECT workflow, stop_reason, max_tokens, effort, attempts, timeout_s, count(*) n
FROM llm_calls
WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')
  AND (stop_reason = 'max_tokens' OR attempts > 1)
GROUP BY workflow, stop_reason, max_tokens, effort, attempts, timeout_s;"
```

- `stop_reason = 'max_tokens'` — the answer was cut off; `max_tokens` is the ceiling it hit and `effort` is usually the real lever (see the two-levers note in `agent/core.py`).
- `attempts > 1` — a wasted API round trip, from a rejected parameter. It was 2 on every Claude 5 call until 2026-08-08 with nothing recording it.
- A `block_census` showing a `thinking` block but no `text` or `tool_use` is a response that spent its whole budget thinking and returned nothing — a real failure wearing a successful `stop_reason`.

```bash
cd /Users/otto/Projects/elixir-bot && sqlite3 -header "file:elixir-telemetry.db?mode=ro" "
SELECT job, workflow, tier, handled, delivered, count(*) n, substr(max(reason),1,60) reason
FROM wake_episodes WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')
GROUP BY job, workflow, tier, handled, delivered;"
```

```bash
cd /Users/otto/Projects/elixir-bot && sqlite3 -header elixir-v51.db "
SELECT job_name, updated_at,
       json_extract(status_json,'\$.failure_count') failures,
       json_extract(status_json,'\$.last_failure_at') last_failure,
       substr(json_extract(status_json,'\$.last_error'),1,80) last_error
FROM runtime_job_status ORDER BY updated_at DESC;"
```

```bash
cd /Users/otto/Projects/elixir-bot && sqlite3 -header elixir-v51.db "
SELECT recorded_at, workflow, failure_type, failure_stage, channel_name, substr(question,1,60) q
FROM prompt_failures WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%S','now','-2 day')
ORDER BY recorded_at DESC LIMIT 20;"
```

## Reading a DB write-lock finding

`storage/db_watch.py` instruments the single SQLite writer. Two rules for interpreting what it records:

1. **`call_site` names whoever OPENED the transaction, which is usually not who spent the time.** Read `sites_json` before blaming a line — it is the per-site breakdown *within* that transaction, heaviest first: `[{"site":"engine/x.py:12","n":3969,"ms":118.4}, …]`, top 8 sites.
2. **A `stalled` outcome is a provisional row, not a completed one.** The watchdog writes the transaction row immediately on crossing 45 s, because the worst offenders never close — a 46.1 s stall on 2026-08-03 hung its job and died with the process. If the transaction later closes, that same row is finalized in place rather than duplicated.

In the log, a stall appears as `[ERROR] elixir: DB STALL: write transaction open Ns from <site> (thread T, N statements). Thread dump follows.` — and the same dump is in `db_stalls.thread_dump`, which is easier to read from SQL than from the log.

Routine engine ticks legitimately hold the lock in the hundreds of milliseconds (`engine/readiness.py` commits thousands of statements per generation). Judge by outliers and trend, not by the presence of rows.

## What to look for

### Actionable signals (surface these)

- `[ERROR]` and `[CRITICAL]` at any logger — always report.
- `Traceback` / `Exception` blocks — report with the exception type and the top app-code frame (`runtime/…`, `engine/…`, `agent/…`), not just the framework frame.
- `DB STALL:` in the log, or any `db_stalls` row, or a `db_transactions` outlier well above the usual sub-second holds.
- `elixir_agent: validation_failure workflow=... reason=...` — schema or parse errors in agent JSON output. Group by `workflow` + `reason`.
- `elixir: prompt_failure ... type=... stage=... workflow=...`, corroborated against the `prompt_failures` table.
- `elixir: prompt_feedback emoji=thumbs_down ...` (WARNING) — a member reacted thumbs-down in `#ask-elixir`. Group by `workflow` + `channel`; include `message_id` and `reactor` so the user can grep the conversation. Thumbs-up is INFO and not a triage concern unless asked.
- `llm_truncated workflow=... model=... max_tokens=... completion_tokens=...` (ERROR, from `agent/core.py`) — the model's answer was cut off mid-thought, which the API reports as a *successful* call. Group by `workflow`; a workflow that truncates repeatedly needs its ceiling raised, not a retry. Cross-check the rate against telemetry: `SELECT workflow, count(*), sum(json_extract(response_json,'$.stop_reason')='max_tokens') FROM llm_calls GROUP BY workflow`. A second WARNING line from `agent/chat.py` adds the `phase` for workflows that pass `return_errors=True`; the two describe one event.
- `job_failed job=... error=...` (ERROR, from `runtime_status.mark_job_failure`) — the canonical "a scheduled job stopped" record, emitted for all 50 failure sites. Always corroborate against `runtime_job_status`, which carries the counts and `last_success_at`.
- `tool_call_failure`, `tool_error`, `ingest_failed`, `signal_failure`, `retry_exhausted`, `truncation`, `unexpected_error` — any custom failure tag.
- Failed `llm_calls` (`ok=0`) clustered on one workflow or model.
- `wake_episodes` rows with `handled=1, delivered=0` — the responder composed something and it never reached Discord.
- Discord reconnect storms (`Attempting a reconnect` repeated within minutes) — note if >3 in an hour.
- APScheduler missed / misfired jobs, cross-checked against `runtime_job_status`.
- New failure signatures that did not appear earlier in the log — the most interesting category.

### Known noise (suppress by default, mention only if asked)

- `PyNaCl is not installed, voice will NOT be supported` — environmental, harmless.
- `discord.gateway: Shard ID None has connected to Gateway` — normal connect.
- `apscheduler.scheduler: Adding job tentatively` / `Added job ... to job store` — startup chatter.
- `discord.http: We are being rate limited ... Retrying in Ns` (429, WARNING) — discord.py retries these itself. Only report a sustained burst, e.g. during leader-action card refresh on startup.
- `'error': None` inside the engine-tick INFO dict — a field name, not an error.
- `db_transactions` rows in the 250 ms–1 s band from `engine/readiness.py` — that is the engine tick working normally.

### Operational health checks

After triaging failures, spot-check these even if nothing errored:

- **Does the row count match the registry?** `runtime/activities.py` holds 20 activities (plus the leader-started `tournament-watch`, which is dynamic and not registered). Nothing prunes `runtime_job_status`, so a retired activity leaves its row forever — `battle_intel_prose` sat there until 2026-08-09, four months after the job was deleted, making the table report a job that could never run. Do not automate this: `job_name` and `activity_key` are not the same string (`weekly-recap` writes `weekly_clan_recap`, `promotion-content` writes `promotion_content_cycle`), so a naive reconcile would delete live history. Compare by hand when the count looks wrong.
- Is `engine-tick` firing roughly every 10 minutes, and `awareness-loop` on its twice-daily cadence? Compare `runtime_job_status.updated_at` against now; long gaps mean the scheduler stalled.
- Any job in `runtime_job_status` with a non-zero `failure_count` or a `last_failure_at` newer than its `last_success_at`.
- Is anything in `runtime_job_status` conspicuously stale relative to its registered schedule in `runtime/activities.py`?
- Do `runtime_job_status` failures have corresponding tracebacks in `logs/elixir-error.log`? A failure recorded in one and absent from the other is itself the finding.
- Did the process restart in the window (`logs/elixir-control.log`, or repeated `logging to …` init lines in the main log)? Note the `revision=` so errors can be tied to a build.
- Any signal that silently went to zero — no `llm_calls` for a workflow that normally runs, no `wake_observations` at all, no engine ticks.

## Grouping and dedup

Do not dump raw log lines or query output. For each issue:

1. **Signature**: a short stable key (e.g. `validation_failure/channel_update/schema_error`, `db_stall/battle_intel`).
2. **Count**: how many times it fired in the window.
3. **First/last seen**: earliest and most recent occurrence.
4. **Representative line**: one full log line or row so the user can grep for it.
5. **Context**: for an exception, the top app-code frame and the relevant `workflow=` / `channel_id=` / `author_id=` fields; for a DB finding, the `sites_json` breakdown.

A recurring signature that fires 40 times is one issue, not 40.

## Output format

Write a short triage report, top-down by priority:

```
## Log Triage — <window>

**Summary:** <1 sentence — e.g. "2 recurring failures, 1 new, scheduler healthy, no DB stalls">

### Priority issues

1. **<signature>** — <count> occurrences, <first-seen> → <last-seen>
   - Representative: `<one log line or row>`
   - Likely cause: <your read>
   - Recommended action: <concrete next step — file to inspect, test to run, config to change>

2. ...

### Low priority / noise

- <one-liner per suppressed category, with counts>

### Health

- Scheduler: <engine-tick cadence, awareness-loop cadence, any stale job>
- Jobs with failures: <from runtime_job_status, or "none">
- DB write lock: <worst held_ms and its site, stalls, or "nothing above routine">
- LLM calls: <failure count by workflow, or "all ok">
- Restarts in window: <timestamps + revision, or "none">
- New signatures this window: <list or "none">
```

Keep the whole report tight — under ~40 lines for a healthy day. If nothing is wrong, say so in one sentence and stop.

## Related tooling

Prefer these over hand-rolling equivalent analysis:

- `uv run --locked python scripts/review_agent_feedback.py --limit 20` (add `--json`) — prompt failures and `#ask-elixir` feedback, already grouped.
- `uv run --locked python scripts/confidence_report.py --quick` (add `--json`) — the three-pillar health surface: recent errors, confidence tests, post quality. Exits non-zero when there are findings.
- `uv run --locked python scripts/wake_shadow_report.py --days 7` — wake evaluator latency, volume, and cost.
- `/llm-cost-report` — spend breakdown. This skill reports LLM *failures*; that one owns cost.

## Recommended actions — be specific

Do not write "investigate the error." Point to the file and what to check:

- "Recurring `validation_failure workflow=channel_update reason=schema_error detail=null response is not allowed`. The model returns bare `null` where an object or allowed null sentinel is expected. Check `_proactive_channel_system` in `agent/prompt_builders.py` — the schema instruction may be ambiguous about when null is allowed."
- "One `db_stalls` row at 2026-08-03T17:48, 46.1 s open from `storage/battle_intel.py:382`. Read `thread_dump` from that row; the transaction never closed, so the job died holding it. Check whether that path batches an unbounded read inside its write transaction."
- "Three Discord reconnects in 20 minutes around 03:29. Likely transient gateway flap; only act if it repeats. If it does, check network / token health on the host."
- "`engine_tick` last succeeded 04:12 against a 10-minute cadence — a 4-hour gap. Check for APScheduler missed-job warnings and confirm the process wasn't restarted or OOM-killed (`logs/elixir-control.log`)."

## When to act vs. just report

Only edit code if the user explicitly asks you to fix an issue. By default this skill is **read-only analysis** — it produces a report and stops. Never write to either database. The user picks which issue to dig into next.

## Arguments

$ARGUMENTS
