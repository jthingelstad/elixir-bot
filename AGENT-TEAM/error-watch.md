# Error Watch

**Owner: the Operations Manager** (`operations-manager.md`). This is that role's
error-log runbook, not a separate role — it runs inside the Operations Manager's
normal cadence and uses its lane, boundaries, and commit rules.

Read `AGENTS.md`, `AGENT-TEAM/WORKFLOW.md`, `AGENT-TEAM/README.md`, and
`operations-manager.md` before acting.

**Lane:** be the thing that notices Elixir is broken. Read
`logs/elixir-error.log` end to end, group what's there into distinct failure
kinds, separate what is *still firing* from what already stopped, trace each
live one to a root cause from its traceback, and either fix it or file it.

## Why this exists

Elixir used to watch itself. Every fail-soft `except` wrote a row to a
`runtime_incidents` table, and a daily `engine-health` job read that table and
posted to `#elixir-log` when it found something. It found nothing, ever: in 25
days the ledger recorded **0 rows** while the log held **159 real errors**. The
health check consulted the ledger, so it reported "all clear" straight through a
`#thinking` permission outage that ran for hours.

Two lessons, and they are the design of this runbook:

1. **The log was always the record.** A bot that reports on its own health is
   reporting from inside the failure. Detection belongs to an operator.
2. **A monitor nobody reads is worse than none** — it manufactures the
   confidence that stops anyone looking.

So the product runtime no longer monitors itself (schema v20 dropped the ledger;
`runtime/health.py` is gone). `runtime/logging_setup.py` writes one small
ERROR-and-up file with tracebacks, deliberately sized so an agent can read the
**whole** file in one pass. That file is the interface. You are the reader.

## The interface

| Path | What it is |
|------|-----------|
| `logs/elixir-error.log` | **ERROR+ only, with tracebacks.** ~6 lines/day. Read it whole. Rotates at 2 MB × 5. |
| `logs/elixir-error.log.1` … `.5` | Older rotations — where "when did this start?" is answered. |
| `logs/elixir.log` | INFO+ narrative. Context *around* an error, never the starting point. |
| `elixir-v5.log` (repo root) | launchd's stdout catch-all: interpreter tracebacks on a hard crash, third-party prints. Check when the process died without logging. |

Line format is `%(asctime)s [%(levelname)s] %(name)s: %(message)s`; every
following indented line belongs to the traceback above it. The logger name
(`engine.tick`, `elixir.threads`, `elixir.storage.player`, …) is the component —
call sites carry a stable `<component> failed: k=v k=v` prefix, so the message is
groupable as-is.

**Cadence:** every Operations Manager run (hourly, or every few hours).

## Every run

1. **Read the whole error log.** Not a `grep` for something you already suspect —
   the point of a file this small is that you can read all of it, and the failure
   you don't yet have a word for is the one grep misses.

2. **Group by kind.** One broken path firing 40 times is *one* finding with a
   count, not 40. Group on (logger, message shape) — strip tags, IDs, and counts
   out of the key. `scripts/confidence_report.py` does exactly this grouping and
   is the fast path:

   ```bash
   uv run --locked python scripts/confidence_report.py --quick --json
   ```

   The JSON has `errors` (grouped, with `count` / `first` / `last`), `liveness`,
   `tests`, `quality`. Exit 0 = nothing found.

3. **Separate still-firing from historical — this is the whole triage.** `last`
   is the field that matters, not `count`. A kind with 200 hits that stopped
   three days ago is *fixed or self-healed*: note it, don't work it. A kind with
   2 hits in the last hour is live. Say which each one is, explicitly, before
   proposing any work.

4. **Trace root cause from the traceback, not from the message.** The message
   says what gave up; the traceback's deepest frame says what broke. Read the
   `k=v` context on the log line (component call sites carry the identifiers —
   `player_tag`, `action_id`, `lane`, `channel_id`) and pull the surrounding
   INFO narrative from `logs/elixir.log` at that timestamp. Then confirm the
   cause in the code before you believe it. Known traps:
   - A single upstream LLM 529 with the cursor held is self-healing — report and
     watch the next loop, don't push a fix.
   - Anything the bot spawns as a subprocess loses `/opt/homebrew/bin`, so `uv`
     and `gh` vanish there while working fine from your shell.
   - Comments describing "X owns this" are hypotheses about an older era. Find
     the live reader before trusting them.

5. **Check CR API drift** (moved here from the retired `check_api_drift`, #212).
   The `api-sentinel` activity records first-seen schema paths into
   `api_sentinel_observations` and nothing in the runtime evaluates them — that
   evaluation is this query:

   ```sql
   SELECT sentinel_type, name, endpoint, first_seen_at
     FROM api_sentinel_observations
    WHERE sentinel_type IN ('schema_path', 'progress_key', 'battle_game_mode')
      AND first_seen_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-2 days')
    ORDER BY first_seen_at DESC
    LIMIT 10;
   ```

   Why it is shaped that way, and don't loosen it without knowing:
   - **Structural types only.** New `event` tags appear constantly as Supercell
     runs events — pure noise. A new schema path, progress key, or game mode
     means Elixir's *model of the payload* may now be wrong.
   - **48-hour window**, because the historical backlog is ~740 rows (mostly the
     sentinel's initial seeding) and would otherwise alert forever. This is the
     ONLY thing bounding the alert: `announced_signal_key` was dropped in schema
     v35 (2026-08-04) because nothing had ever written it, so filtering on it was
     a no-op that hid 30 legacy rows while appearing to do work.
   - **`first_seen_at` is the only timestamp now.** The sentinel writes on
     novelty only; `last_seen_at` was dropped with the touch that maintained it.

   This alert is deliberately thin — it says *something changed*, nothing more.
   Hand it to the **Data Analyst** (`data` issue) to characterize and quantify;
   that role's runbook already owns the full-backlog query. Only work it yourself
   if ingest is actually failing right now, which is an `operations` outage.

6. **Act, one finding at a time.** Per the Operations Manager's boundary:
   - Small, obvious, test-backed operational fix → claim the issue with `wip`,
     fix it, `uv run --locked pytest -q` must be green, commit, deploy, close.
   - Anything larger, ambiguous, or outside the operations lane → file a labeled
     issue with the **grouped** evidence: component, count, first/last seen, and
     the traceback. Route product/quality/prompt findings by label; never reach
     into another lane's code.
   - Nothing live → say "no live errors" in one line and stop. A healthy run is a
     clean one-liner and no churn.

## What the log cannot tell you

An error log only reports failures that *produced an error*. The failures that
killed Elixir quietly did not: the `can_post_leader_action` `NameError` stopped
all card posting and the only symptom was silence. That is what `liveness` in
the confidence report covers — no successful awareness terminal decision in N
hours (a deliberate silence or a delivered plan), or leader-action cards
proposed but never posted. **Treat a `liveness` finding as high-signal
even when the error log is empty.** Quiet is not calm.

## Success

Every live error in the log is either fixed or a filed issue within one cadence,
and every finding you report says whether it is still firing. Measured by how
little sits unread in `logs/elixir-error.log` — not by how many findings you
file, and never again by a green report from a source nobody validated.
