---
name: llm-cost-report
description: Analyze the llm_calls table in elixir-telemetry.db to break down Elixir's LLM spend by workflow, model, and day; identify cost drivers; recommend signal-side or model-tier cuts
---

# LLM Cost Report

Read the admin-only telemetry database and hand the user a short prioritized
report: where the money goes, whether caching is paying off, daily trend,
anomalies, and concrete next levers. Pairs with `log-triage` (health) and
`awareness-report` (decision quality); this one answers "where is the money
going?"

Budget context: the ratified hard ceiling on discretionary awareness/Ask Elixir
spend is **$3.20/day** (`agent/spend_budget.py`). It is a stop, not a target —
budgeted spend actually runs around **$0.90/day**. Scheduled reports and
operational jobs are outside this ledger entirely (about half of all spend) and
must be reported separately.

## Canonical report path

Run the repository tool from `/Users/otto/Projects/elixir-bot`:

```bash
uv run --locked python scripts/llm_cost_report.py --days 7 --json
```

Use `--days 1`, `--days 14`, or `--days 30` when the user requests another
rolling window. The script opens SQLite read-only and consumes
`agent/pricing.py`, the same date-aware authority that charges the runtime
budget and stores each call's `cost_usd`. Do not recreate model rates in SQL,
this skill, or prose.

The JSON includes:

- exact ISO-Z cutoff and lifetime first/last call;
- calls, failures, total and projected monthly cost;
- stored-receipt versus timestamp-priced fallback row counts;
- daily cost and call volume;
- workflow/model cost breakdown;
- cache read/write ratios; and
- any unknown model that had to use the explicitly marked conservative
  fallback.

### The database moved — never read the clan DB

`llm_calls` moved to `elixir-telemetry.db` on 2026-08-03. The clan database's
old copy is frozen. If spend appears to end abruptly on that date, the database
path is wrong, not the spend.

### Stored costs are historical receipts

Prefer non-null `cost_usd`. It records what the runtime charged when the call
ran and remains immutable across later price changes. Rows from before per-call
capture have null cost; the report tool falls back to the canonical rate at
each row's `recorded_at` timestamp. Never replace a stored receipt with a
current-price recomputation.

### Bootstrap guard

Check `first_call`, `last_call`, and `lifetime_calls` in the report. If fewer
than roughly 24 hours of rows exist, say so and stop; the averages are not yet
representative.

## Cost per turn and waste checks

`turn_id` groups the several API calls made by one tool-using turn. If per-turn
cost matters, run this read-only query; every selected row already has its
stored receipt:

```sql
SELECT workflow,
       COUNT(DISTINCT COALESCE(turn_id, 'call:' || call_id)) AS turns,
       ROUND(SUM(cost_usd), 4) AS cost,
       ROUND(SUM(cost_usd) /
         COUNT(DISTINCT COALESCE(turn_id, 'call:' || call_id)), 4) AS cost_per_turn,
       ROUND(1.0 * COUNT(*) /
         COUNT(DISTINCT COALESCE(turn_id, 'call:' || call_id)), 1) AS calls_per_turn
FROM llm_calls
WHERE cost_usd IS NOT NULL
  AND recorded_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 days')
GROUP BY workflow ORDER BY cost DESC;
```

Check these three directly visible waste signals before recommending a model
swap:

```sql
-- API round trips above one are rejected parameters or retries.
SELECT workflow, model, COUNT(*) n, SUM(attempts) trips
FROM llm_calls WHERE attempts > 1 GROUP BY workflow, model;

-- Spend that produced a truncated response.
SELECT workflow, max_tokens, COUNT(*) n, ROUND(SUM(cost_usd), 4) wasted
FROM llm_calls WHERE stop_reason = 'max_tokens' GROUP BY workflow, max_tokens;

-- Effort versus completion and cost.
SELECT workflow, effort, COUNT(*) n,
       ROUND(AVG(completion_tokens)) avg_out, ROUND(AVG(cost_usd), 5) avg_cost
FROM llm_calls WHERE effort IS NOT NULL GROUP BY workflow, effort;
```

`block_census` carries block counts, not sizes. Thinking present with no text or
tool use means the model spent budget without a deliverable; pair the census
with completion tokens.

## Interpretation

- Workflow above 25% of spend: name it as the primary lever.
- Cache read/write below 0.28: caching is net-negative for that workflow;
  verify the prefix/cadence before recommending a change.
- Top day above twice the 14-day median: drill into workflow/model for that day.
- High Haiku volume with tiny cost: leave it alone.
- Separate member-driven spikes from scheduled baseline before changing a cron.

Prefer recommendations in this order:

1. Fix retries, truncations, or empty responses that waste paid calls.
2. Reduce low-value signals before they invoke a model.
3. Move suitable analysis to a lighter model only after quality evidence.
4. Remove stale prompt/tool payload that invalidates or bloats cached prefixes.
5. Reduce cadence when measured editorial value does not justify it.

Do not change member-visible cadence, hard-post coverage, or the daily ceiling
from this read-only report. Route those decisions to their owning issue.

## Output

Keep the report under roughly 40 lines:

```text
## LLM Cost Report — <window>
Summary: $X over N days; budgeted $Y/day against the $3.20 ceiling.
Top driver: workflow/model, calls, cost, share.
Daily trend: ...
Cache flags: ...
Waste/anomalies: ...
Recommended next levers: 1-3 measured actions with estimated savings.
```

If spend is below target, say so in one sentence and stop. This skill reports;
code changes happen only in explicitly authorized follow-up work.

## Arguments

$ARGUMENTS
