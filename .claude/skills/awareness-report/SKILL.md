---
name: awareness-report
description: Assess the live v5.1 awareness loop from thoughts, delivered posts, stream coverage, tool traces, and errors
---

# Awareness Report

Read `/Users/otto/Projects/elixir-bot/elixir-v51.db` and assess whether the
single live awareness loop is making good editorial decisions. This is a
read-only report. It answers “is Elixir making good calls?”; `log-triage`
answers “is the runtime alive?”

Production evidence lives in:

- `awareness_thoughts`: one deliberation or gated silence per loop, including
  plan, reason, model, and tool trace.
- `awareness_posts`: confirmed deliveries, destination lane, Discord message
  id, and covered signal keys.
- `stream_cursors`: the durable awareness positions.
- `runtime_job_status`: scheduler health. Failures themselves are in
  `logs/elixir-error.log` (the `runtime_incidents` ledger was dropped in V20 --
  it never recorded a row).

`awareness_ticks` was deleted at the v5.1 clean break. Never query it from the
operational database.

## Scope

Default to the last 7 days. If the available history is under 24 hours, say so
and stop rather than over-interpreting a bootstrap window. Timestamps are UTC.

```sql
SELECT MIN(at) AS first_loop, MAX(at) AS last_loop, COUNT(*) AS loops
FROM awareness_thoughts;
```

## Queries

Run these against `elixir-v51.db` with read-only SQLite access.

### 1. Loop outcomes

```sql
SELECT COUNT(*) AS loops,
       SUM(chose_silence) AS deliberate_silences,
       SUM(post_count) AS posts_planned,
       SUM(CASE WHEN skipped_reason LIKE '⚠️ tick failed:%' THEN 1 ELSE 0 END) AS failed_loops,
       SUM(CASE WHEN model LIKE 'gate:%' THEN 1 ELSE 0 END) AS gated_loops
FROM awareness_thoughts
WHERE at >= datetime('now', '-7 days');
```

### 2. Delivered lanes and signal coverage

```sql
SELECT lane, COUNT(*) AS posts,
       COUNT(DISTINCT discord_message_id) AS confirmed_messages,
       SUM(json_array_length(covers_json)) AS covered_signals,
       MAX(posted_at) AS last_post
FROM awareness_posts
WHERE posted_at >= datetime('now', '-7 days')
GROUP BY lane
ORDER BY posts DESC;
```

### 3. Silence reasons

```sql
SELECT COALESCE(skipped_reason, '(none)') AS reason, COUNT(*) AS loops
FROM awareness_thoughts
WHERE at >= datetime('now', '-7 days') AND chose_silence = 1
GROUP BY reason
ORDER BY loops DESC
LIMIT 12;
```

### 4. Tool use and failures

```sql
SELECT json_extract(tool.value, '$.tool') AS tool,
       COUNT(*) AS calls,
       SUM(json_extract(tool.value, '$.allowed') = 0) AS denied,
       SUM(json_extract(tool.value, '$.result') NOT LIKE 'ok%') AS non_ok
FROM awareness_thoughts AS thought,
     json_each(COALESCE(thought.tool_trace_json, '[]')) AS tool
WHERE thought.at >= datetime('now', '-7 days')
GROUP BY tool
ORDER BY calls DESC;
```

Also inspect failed thoughts, posts without a linked loop number, awareness
errors in `logs/elixir-error.log`, and the `awareness-loop` row in
`runtime_job_status`. Use exact thought loop
numbers, post ids, Discord message ids, and timestamps as evidence.

## Interpretation

Prioritize:

1. Failed loops, cursor stalls, live errors in `logs/elixir-error.log`, or a
   long gap since the last confirmed post.
2. Hard-post signals repeatedly resurfacing without delivery.
3. Tool denials/errors or unusually large tool-call fan-out.
4. Repetitive lane use, low-value copy, or silence reasons that contradict the
   actual read.

Deliberate silence is healthy when the reason matches the situation. A missing
post is not automatically a defect; distinguish editorial silence from a failed
turn or failed delivery.

## Output

Keep a healthy report under 40 lines:

```text
## Awareness Report — <window>

Summary: <one sentence>

Headline
- <loops, silence, planned and confirmed posts, covered signals>

Priority issues
1. <exact signature and evidence>

Patterns worth noting
- <lane, tool, coverage, or silence pattern>
```

Do not edit code or prompts. Report evidence and recommend a specific next
inspection target. If healthy, say so in one sentence and stop.
