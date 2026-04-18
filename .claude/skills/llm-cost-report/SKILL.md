---
name: llm-cost-report
description: Analyze the llm_calls table in elixir.db to break down Elixir's LLM spend by workflow, model, and day; identify cost drivers; recommend signal-side or model-tier cuts
---

# LLM Cost Report

Read `/Users/jamie/Projects/elixir-bot/elixir.db` (table `llm_calls`), compute real spend using correct Sonnet 4.6 and Haiku 4.5 pricing, and hand the user a short prioritized report: where the money goes, whether caching is paying off, daily trend, anomalies, and concrete next levers. Pairs with `log-triage` ("is the bot alive?") and `awareness-report` ("is the bot making good calls?") — this one answers "where is the money going?"

Budget context: the user's target is **$15/month (~$0.50/day)**. Report current spend against that bar.

## Scope

Default to the **last 7 days** of `llm_calls` rows. Overridable by the user ("last 24h", "last 30 days", "since 2026-04-10"). `recorded_at` is an ISO-8601 timestamp.

### Bootstrap guard

If fewer than ~24 hours of rows exist, say so in one line and stop. Short windows produce misleading averages.

```sql
SELECT MIN(recorded_at) AS first_call,
       MAX(recorded_at) AS last_call,
       COUNT(*) AS n
FROM llm_calls;
```

### Schema reference

```
llm_calls(call_id, recorded_at, workflow, model, ok, error, duration_ms,
          prompt_tokens, completion_tokens, total_tokens,
          cache_creation_tokens, cache_read_tokens)
```

## Pricing constants

Use these exact numbers (per 1M tokens). Update this block if Anthropic changes prices.

| Model | Prompt | Completion | Cache read | Cache write |
|---|---:|---:|---:|---:|
| Sonnet 4.6 (`claude-sonnet-4-6`) | $3.00 | $15.00 | $0.30 | $3.75 |
| Haiku 4.5 (`claude-haiku-4-5-20251001`) | $1.00 | $5.00 | $0.10 | $1.25 |

Test models (`claude-test-*`) are priced at $0 — filter them out of cost totals, but count them if you're checking call volume.

**Caching break-even:** caching is net-cheaper than no-cache when
`cache_read_tokens / cache_creation_tokens ≥ 0.28`
(derived: cache_write = 1.25× base, cache_read = 0.10× base; 0.25·X = 0.90·Y → Y = 0.28·X).

## Queries to run

Run all four queries against `elixir.db` via `sqlite3` and interpret them together.

### Q1 — Daily totals and trend

```sql
SELECT date(recorded_at) AS day,
       COUNT(*) AS calls,
       ROUND(SUM(CASE WHEN model LIKE 'claude-sonnet%'
            THEN prompt_tokens*3 + cache_read_tokens*0.3 + cache_creation_tokens*3.75 + completion_tokens*15
            WHEN model LIKE 'claude-haiku%'
            THEN prompt_tokens*1 + cache_read_tokens*0.1 + cache_creation_tokens*1.25 + completion_tokens*5
            ELSE 0 END) / 1e6, 2) AS cost_usd
FROM llm_calls
WHERE recorded_at >= datetime('now', '-7 days')
GROUP BY day
ORDER BY day;
```

What it tells you: daily burn rate, spikes, and whether the trend is rising or flat. Median day × 30 is the projected monthly cost.

### Q2 — Per-workflow, per-model breakdown

```sql
SELECT workflow,
       model,
       COUNT(*) AS calls,
       ROUND(SUM(CASE WHEN model LIKE 'claude-sonnet%'
            THEN prompt_tokens*3 + cache_read_tokens*0.3 + cache_creation_tokens*3.75 + completion_tokens*15
            WHEN model LIKE 'claude-haiku%'
            THEN prompt_tokens*1 + cache_read_tokens*0.1 + cache_creation_tokens*1.25 + completion_tokens*5
            ELSE 0 END) / 1e6, 2) AS cost_usd,
       ROUND(AVG(prompt_tokens))        AS avg_prompt,
       ROUND(AVG(cache_read_tokens))    AS avg_cache_read,
       ROUND(AVG(cache_creation_tokens))AS avg_cache_write,
       ROUND(AVG(completion_tokens))    AS avg_completion
FROM llm_calls
WHERE recorded_at >= datetime('now', '-7 days')
  AND model LIKE 'claude-%'
GROUP BY workflow, model
ORDER BY cost_usd DESC;
```

What it tells you: which workflows are the real cost drivers. Anything over 25% of total is a primary lever. Per-call averages show whether a workflow is expensive due to volume or prompt size.

### Q3 — Cache efficiency

```sql
SELECT workflow,
       SUM(cache_creation_tokens) AS cc_total,
       SUM(cache_read_tokens)     AS cr_total,
       ROUND(SUM(cache_read_tokens) * 1.0 / NULLIF(SUM(cache_creation_tokens), 0), 2)
         AS read_per_write
FROM llm_calls
WHERE recorded_at >= datetime('now', '-7 days')
  AND cache_creation_tokens > 0
GROUP BY workflow
ORDER BY cc_total DESC;
```

What it tells you: whether cache is paying off per workflow. Flag any workflow where `read_per_write < 0.28` — cache is costing more than it saves. Don't flag cache as the lever without checking this number.

### Q4 — Anomaly / spike detection

```sql
WITH daily AS (
  SELECT date(recorded_at) AS day,
         SUM(CASE WHEN model LIKE 'claude-sonnet%'
              THEN prompt_tokens*3 + cache_read_tokens*0.3 + cache_creation_tokens*3.75 + completion_tokens*15
              WHEN model LIKE 'claude-haiku%'
              THEN prompt_tokens*1 + cache_read_tokens*0.1 + cache_creation_tokens*1.25 + completion_tokens*5
              ELSE 0 END) / 1e6 AS cost
  FROM llm_calls
  WHERE recorded_at >= datetime('now', '-14 days')
  GROUP BY day
)
SELECT day, ROUND(cost, 2) AS cost_usd
FROM daily
ORDER BY cost DESC
LIMIT 3;
```

If the top day is >2× the 14-day median, it's a spike. Drill in by re-running Q2 with a single-day `WHERE date(recorded_at) = '<spike-date>'` filter and name the driver workflow.

## Interpretation rules

- **Workflow >25% of total spend** = primary lever. Name it explicitly.
- **Sonnet workflow with `read_per_write < 0.28`** = cache is net-negative on that workflow.
- **Daily cost trending up week-over-week** = regression worth calling out.
- **High Haiku call volume but tiny cost** = noise, leave alone. Don't recommend cron kills just because the call count is high.
- **Apr 16 2026 spike** is a known one-time autonomous-sprint event — if it still falls inside the window, flag it once and don't over-weight the median.

## Recommended-actions playbook

When recommending next levers, prefer them in this order:

1. **Signal-side cuts** — reduce how often a workflow is *triggered* in the first place. Tighter signal filters, longer dedup windows, per-channel cooldowns. This is the user's stated preference.
2. **Model swap to Haiku** — fair game for any workflow that isn't long-form written content.
   - **Keep Sonnet** on: `weekly_digest`, `site_promote_content`, `tournament_recap`, `intel_report`, `memory_synthesis`.
   - Everything else can default to Haiku.
3. **Prompt trim** — remove stale or unused fields from Situation JSON. Don't add new rules or restrictions; *removing bloat* is fine.
4. **Cadence** — lower cron frequency (e.g. `HEARTBEAT_INTERVAL_MINUTES` 30 → 60).

**Do NOT recommend:**
- Per-user quotas or daily caps.
- Daily-budget circuit breakers.
- Killing low-cost Haiku crons (site content, roster bios, events) — the user has said these aren't moving the needle.
- Disabling caching — check the break-even math (Q3) first; it's almost never the right call.

Always quote estimated weekly and monthly savings with the recommendation, derived from current volume × price delta.

## Output format

Compact report, top-down by priority. Keep the whole thing under ~40 lines.

```
## LLM Cost Report — <window>

**Summary:** $X.XX over N days ≈ $YY/month (target $15/mo). Top driver: `<workflow>` at $Z.ZZ (W%).

### Cost by workflow (top 10 by cost)
| Workflow | Model | Calls | Cost | % | r/w |
| ...

### Daily trend
<day>: $X.XX  (N calls)
...

### Cache efficiency flags
- `<workflow>`: read/write = 0.YY — caching is net-negative; move to non-cached prompt OR stabilize the prefix.
(or: "All cached workflows above 0.28 break-even." if nothing to flag)

### Anomalies
- <e.g. "2026-04-16: $5.48 vs 14-day median $1.80 — driver `awareness` from autonomous signal sprint. Non-recurring.">
(or: "No days >2× median in window." if clean)

### Recommended next levers
1. <concrete: workflow, change, saving>. E.g. "Swap `deck_review` → Haiku — ~$2.50/wk saved at current volume."
2. ...
```

If spend is already under target, say so in one sentence and stop. No need to keep hunting for cuts that don't matter.

## When to act vs. just report

Read-only by default. This skill produces the report and stops. User picks which lever to pull; code changes happen in follow-up work.

## Arguments

$ARGUMENTS
