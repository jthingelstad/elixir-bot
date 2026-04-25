---
name: log-triage
description: Analyze elixir.log for errors, recurring failures, and operational signals, then recommend concrete actions
---

# Log Triage

Read `/Users/jamie/Projects/elixir-bot/elixir.log`, surface what's actually going wrong (vs. noise), group recurring issues, and hand the user a short prioritized action list. The goal is to answer "what should I fix next?" — not to paraphrase the log.

## Scope

Default to the last 24 hours of entries unless the user specifies a window (e.g. "since yesterday's deploy", "last hour"). Log lines begin with `YYYY-MM-DD HH:MM:SS,mmm [LEVEL] logger: ...`, so filter by timestamp prefix when narrowing.

If the log is very large (>10k lines), start by tailing the last ~2000 lines. Only widen the window if the user asked for historical context or an issue's first occurrence isn't in the tail.

## What to look for

### Actionable signals (surface these)

- `[ERROR]` and `[CRITICAL]` at any logger — always report.
- `Traceback` / `Exception` blocks — report with the exception type and the top app-code frame (not just the Python framework frame).
- `elixir_agent: validation_failure workflow=... reason=...` — schema or parse errors in the agent's JSON output. Group by `workflow` + `reason`.
- `elixir: prompt_failure ... type=... stage=... workflow=...` — agent-response failures that reached the user path. Group by `workflow` + `type`.
- `elixir: prompt_feedback emoji=thumbs_down ...` (WARNING) — a user reacted thumbs-down on an Elixir response in #ask-elixir. Each is a quality signal worth surfacing. Group by `workflow` + `channel` and report counts; the representative line should include `message_id` and `reactor` so the user can grep the conversation. Thumbs-up is INFO and not a triage concern unless the user asks.
- `tool_call_failure`, `tool_error`, `ingest_failed`, `signal_failure`, `retry_exhausted`, `truncation`, `unexpected_error` — any custom failure tag from the app.
- Discord reconnect storms (`Attempting a reconnect` repeated within minutes) — note if >3 in an hour.
- APScheduler missed / misfired jobs.
- New failure signatures that did not appear earlier in the log — those are the most interesting.

### Known noise (suppress by default, mention only if user asks)

- `PyNaCl is not installed, voice will NOT be supported` — environmental, harmless.
- `discord.gateway: Shard ID None has connected to Gateway` — normal connect.
- `apscheduler.scheduler: Adding job tentatively` / `Added job ... to job store` — startup chatter.
- `elixir_heartbeat: Heartbeat: N signals detected` — routine heartbeat. Only flag if heartbeats stop or if `N` spikes abnormally.

### Operational health checks

After triaging failures, spot-check these even if nothing errored:

- Are heartbeats still firing on their expected cadence (roughly every 30–60 min)? Long gaps mean the scheduler stalled.
- Are agent_loop entries for each scheduled channel update (river-race, clan-events, leader-lounge) landing on time?
- Any signal that silently went to zero — e.g. no `agent_loop` entries for a workflow that usually runs hourly?

## Grouping and dedup

Do not dump raw log lines. For each issue:

1. **Signature**: a short stable key (e.g. `validation_failure/channel_update/schema_error`).
2. **Count**: how many times it fired in the window.
3. **First/last seen**: timestamps of earliest and most recent occurrence in the window.
4. **Representative line**: one full log line so the user can grep for it.
5. **Context**: if an exception, the top app-code frame (`runtime/…`, `agent/…`) and the relevant `workflow=` / `channel_id=` / `author_id=` fields.

A recurring signature that fires 40 times is one issue, not 40.

## Output format

Write a short triage report, top-down by priority. Use this structure:

```
## Log Triage — <window>

**Summary:** <1 sentence — e.g. "2 recurring failures, 1 new, scheduler healthy">

### Priority issues

1. **<signature>** — <count> occurrences, <first-seen> → <last-seen>
   - Representative: `<one log line>`
   - Likely cause: <your read>
   - Recommended action: <concrete next step — file to inspect, test to run, config to change>

2. ...

### Low priority / noise

- <one-liner per suppressed category, with counts>

### Health

- Heartbeats: <cadence ok / gap at HH:MM>
- Scheduled updates: <all landed / missing X>
- New signatures this window: <list or "none">
```

Keep the whole report tight — under ~40 lines for a healthy day. If nothing is wrong, say so in one sentence and stop.

## Recommended actions — be specific

Do not write "investigate the error." Point to the file and what to check. Examples of the level of specificity expected:

- "Recurring `validation_failure workflow=channel_update reason=schema_error detail=null response is not allowed`. The model is returning bare `null` when it should return an object or allowed null sentinel. Check `_proactive_channel_system` in `agent/prompts.py` — the schema instruction may be ambiguous about when null is allowed."
- "Three Discord reconnects in 20 minutes around 03:29. Likely transient gateway flap; only act if this repeats. If it does, check network / token health on the host."
- "Heartbeat gap from 04:12 → 08:21 (4 hours, expected ~30 min cadence). APScheduler may have stalled — check for missed-job warnings and confirm the process didn't get OOM-killed."

## When to act vs. just report

Only edit code if the user explicitly asks you to fix an issue. By default, this skill is **read-only analysis** — it produces a report and stops. The user will pick which issue to dig into next.

## Arguments

$ARGUMENTS
