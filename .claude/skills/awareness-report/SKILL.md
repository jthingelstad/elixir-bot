---
name: awareness-report
description: Analyze the awareness_ticks table to assess how Elixir's v4.5 awareness loop is actually spending its agent turns — coverage, silent skips, fallback failures — and recommend tuning actions
---

# Awareness Report

Read `/Users/jamie/Projects/elixir-bot/elixir.db` (table `awareness_ticks`), surface how Elixir's awareness loop is actually making decisions, and hand the user a short prioritized list of *what to tune next*. Pairs with `log-triage`: that skill answers "is the bot alive?"; this one answers "is the bot making good calls?"

## Scope

Default to the **last 7 days** of `awareness_ticks` rows. Overridable by the user ("last 24 hours", "since deploy", "last 30 days"). `ticked_at` is UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SS`).

### Bootstrap guard

If fewer than ~24 hours of rows exist (table just rolled out, or bot was down), say so in one line and stop. Early data is too noisy to draw conclusions from — tell the user to come back once the window has filled.

```sql
SELECT MIN(ticked_at) AS first_tick, MAX(ticked_at) AS last_tick, COUNT(*) AS n
FROM awareness_ticks;
```

## Queries to run

Run these in one pass (sqlite3 `.read` or four separate calls) and interpret the results together.

### 1. Per-workflow headline

```sql
SELECT workflow,
       COUNT(*)                         AS ticks,
       SUM(signals_in)                  AS signals_seen,
       SUM(posts_delivered)             AS posts,
       SUM(covered_keys)                AS covered,
       SUM(considered_skipped)          AS skipped,
       SUM(hard_fallback)               AS fallbacks,
       SUM(hard_fallback_failed)        AS fallback_failed,
       SUM(CASE WHEN all_ok = 0 THEN 1 ELSE 0 END) AS failed_ticks
FROM awareness_ticks
WHERE ticked_at >= datetime('now', '-7 days')
GROUP BY workflow
ORDER BY ticks DESC;
```

What it tells you:
- `posts / signals_seen` is the *coverage rate*. Dropping week-over-week often means the agent is getting overwhelmed or the prompt drifted.
- `skipped / signals_seen` — how often the agent saw a signal and consciously passed on it. Expected for optional progression; concerning for clan-event-family signals.
- `fallback_failed > 0` is *always* a priority issue — a hard-required signal didn't reach Discord.

### 2. Skipped-reason distribution

```sql
SELECT COALESCE(skipped_reason, '(none given)') AS reason,
       COUNT(*) AS ticks,
       SUM(signals_in) AS signals
FROM awareness_ticks
WHERE ticked_at >= datetime('now', '-7 days')
  AND posts_delivered = 0
GROUP BY reason
ORDER BY ticks DESC
LIMIT 10;
```

What it tells you: when the agent chooses silence, is it citing a consistent reason, or is `skipped_reason` usually null? A high `(none given)` share is a prompt-hygiene signal — the agent should be required to name its reason.

### 3. Signal types the agent habitually skips

Needs JSON expansion; use `json_each` over `signal_outcomes_json`.

```sql
SELECT
    json_extract(value, '$.signal_type') AS signal_type,
    SUM(CASE WHEN json_extract(value, '$.status') = 'skipped'  THEN 1 ELSE 0 END) AS skipped,
    SUM(CASE WHEN json_extract(value, '$.status') = 'covered'  THEN 1 ELSE 0 END) AS covered,
    SUM(CASE WHEN json_extract(value, '$.status') LIKE 'fallback%' THEN 1 ELSE 0 END) AS fallback,
    COUNT(*) AS total
FROM awareness_ticks, json_each(awareness_ticks.signal_outcomes_json)
WHERE ticked_at >= datetime('now', '-7 days')
GROUP BY signal_type
HAVING total >= 5
ORDER BY (1.0 * skipped / total) DESC
LIMIT 15;
```

What it tells you: which signal types the agent almost always ignores. If a signal type shows `skipped / total` above ~0.9 *and* it's not in `OPTIONAL_PROGRESSION_SIGNAL_TYPES`, either:
- The detector is noisy and the signal shouldn't fire that often, or
- The agent's system prompt isn't giving it a reason to care, or
- The signal is genuinely low-value and should be moved to optional-progression and quiet-skipped upstream.

### 4. Ticks that need attention

```sql
SELECT tick_id, ticked_at, workflow, signals_in, posts_delivered,
       hard_fallback, hard_fallback_failed, all_ok, skipped_reason
FROM awareness_ticks
WHERE ticked_at >= datetime('now', '-7 days')
  AND (all_ok = 0 OR hard_fallback_failed > 0)
ORDER BY ticked_at DESC
LIMIT 20;
```

What it tells you: the specific ticks where something went wrong. Cross-reference the timestamps with `elixir.log` (via `log-triage`) to pin down the cause.

## Grouping and interpretation

Do not dump raw rows. Summarize:

- **Coverage rate** per workflow, with week-over-week delta if you can compute it (compare `-7 days` vs. `-14 days to -7 days`).
- **Named outliers** — e.g., "`arena_change` skipped 94% of the time (48 of 51 appearances)." Prefer absolute numbers with denominators over percentages alone.
- **Failure ticks** — name the timestamp and the workflow. "2026-04-17 10:05 UTC (war_awareness): hard_fallback_failed=1 for signal_key=war_final_battle_day::s00131-w02-p013." Suggest cross-referencing `elixir.log`.

## Output format

```
## Awareness Report — <window>

**Summary:** <1 sentence — e.g., "clan_awareness healthy; war_awareness lost 3 hard-fallback signals this week">

### Headline

- clan_awareness: 168 ticks, 412 signals seen, 140 posts, 12% fallback fail-free
- war_awareness: 168 ticks, 520 signals seen, 168 posts, 1 fallback failure
- <whatever the data actually says>

### Priority issues

1. **<signature>** — <count>, <timestamps or range>
   - Context: <what the data shows>
   - Recommended action: <specific file/prompt/signal to look at>

### Patterns worth noting

- <e.g., "arena_change is now skipped 94% of the time — worth moving to OPTIONAL_PROGRESSION_SIGNAL_TYPES so the quiet-check short-circuits it">
- <e.g., "skipped_reason is null on 78% of silent ticks — consider making it required in the agent schema">

### Ticks to investigate

- <timestamp> <workflow> — <one-line anomaly> → cross-ref elixir.log at <timestamp>
```

Keep the whole report tight — under ~40 lines for a healthy week. If nothing is wrong, say so in one sentence and stop.

## Recommended actions — be specific

Do not write "investigate the prompt." Point to the file, the signal type, or the config value. Examples of the level of specificity expected:

- "`arena_change` is skipped 94% of the time (48 of 51 appearances this week). If that's intended, add it to `OPTIONAL_PROGRESSION_SIGNAL_TYPES` in `runtime/channel_subagents.py:35` so the quiet-check skips these ticks entirely — would save ~48 LLM calls/week."
- "3 `hard_fallback_failed` ticks all have `workflow=war_awareness` around the 10:00 UTC reset. Likely the period-index-stale-at-boundary pattern — re-check `heartbeat/_war.py:detect_war_day_markers` for observation-time-vs-reset-window alignment."
- "Coverage rate for `clan_awareness` dropped from 41% last week to 28% this week. Check if `_clan_awareness_system` in `agent/prompts.py` changed recently, or if signal volume spiked (detector regression) vs. the LLM actually choosing silence more often."

## When to act vs. just report

Read-only analysis by default. This skill produces a report and stops. The user will pick which finding to dig into next.

## Arguments

Optional natural-language window ("last 24 hours", "last 30 days", "since 2026-04-15"). If none given, default to last 7 days.
