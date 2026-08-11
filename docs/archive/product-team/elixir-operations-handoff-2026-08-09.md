# Historical Elixir operations handoff — 2026-08-09

Archived 2026-08-11 when AGENT-TEAM moved from job-oriented roles to three
objective owners. The incidents and lessons below remain useful historical
evidence; its current-state and open-work sections are no longer authoritative.

Written at the end of a week of operating Elixir, for whoever picks it up next.
This is state and judgement, not a task list; `AGENTS.md` remains the
architecture reference and is not repeated here.

## The role

Operate Elixir with a bias toward **operational resilience and visibility**.
Standing authority from Jamie: restart, commit, push, and change or create
monitoring tooling without asking. What still belongs to Jamie is anything that
changes what members **see** — posting to Discord, mass email, triggering a
member-facing job early — and anything irreversible.

Two working rules earned the hard way this week:

**Measure before changing.** Nearly every real finding came from querying
`elixir-telemetry.db` or probing the live API. Nearly every wrong conclusion
came from reasoning about what the code looked like it did. Two worked examples:
`budget_tokens` looks like the way to bound thinking and is a **400** on these
models; a cluster of failures at 181.7s looked like slow work and was 60s x 3
SDK retries.

**Fix at the source; guards are a last resort.** Jamie pushed back on this
directly. A guard that makes production code responsible for detecting a
developer mistake — using a heuristic threshold that can silently drop real data
— is the same antipattern as a `DO NOT` in a prompt. The worked example is in
`803cb518`: a duration-threshold guard was reverted in favour of
`scripts/probe_llm.py`, the tool that removes the reason the mistake happens.

## Current state

- Running revision **6513ed03**, which equals `origin/main`. Verify with
  `bash scripts/admin.sh status` and the last line of `logs/elixir-control.log`.
- 20 registered activities (`runtime/activities.py`) plus the leader-started
  `tournament-watch`, which is dynamic and deliberately unregistered.
- Zero errors, zero truncations, zero wasted API round trips in the last 24h.

## What changed this week, and why it matters

The through-line: **Elixir was never noisy about being broken — it was quietly
reporting success.** Everything below is a variation on that.

1. **Truncated answers and failed jobs now log at ERROR.** Both were WARNING or
   silent, so neither reached `logs/elixir-error.log` — the file an operator and
   `scripts/confidence_report.py` read to decide Elixir is healthy. 39
   truncations and a job with zero lifetime successes had gone unnoticed.
2. **The model-call surface is one policy.** `agent.core.MODEL_CALL_POLICY` owns
   ceiling, thinking effort and timeout per workflow; previously `max_tokens` was
   a literal at ~25 call sites and `effort` was set nowhere. Guard tests fail on
   a reintroduced literal.
3. **`llm_calls` records what a call was asked to do**, not just what it cost —
   `effort`, `max_tokens`, `timeout_s`, `stop_reason`, `block_census`,
   `attempts`, `cost_usd`, `turn_id`.
4. **Job history survives restarts.** It did not: the first `mark_job_*` after a
   restart built a fresh default and persisted it over the real row, so
   `last_success_at` was erased weekly by the very run that should have been
   checking it.
5. **`elixir-telemetry.db` is backed up.** It was not, which `AGENTS.md` called
   "a known gap, not a design decision".

## The open work, in priority order

### 1. Catch-up for scheduled jobs — the big one

**`weekly_member_report` last succeeded 2026-07-27 and made zero LLM calls on
its scheduled 2026-08-03 Monday.** A member email silently did not go out.

The mechanism is not retry-shaped. APScheduler uses the default **memory
jobstore**, so on restart each job's next fire time is recomputed from boot: a
slot that passes while the process is down *never existed*, and there is nothing
for the scheduler to report. `misfire_grace_time: 300` only covers a job missed
while the process is up and busy.

Jamie's framing, which is the right one: *"Long background jobs should be
resilient but they may do it via idempotency instead of simple retry in the LLM
call."* Retrying the model call cannot fix a job that never ran.

The design, now buildable because item 4 above landed: give each job a **period
key** from its schedule (`2026-W32` weekly, `2026-08-09` daily, a season id for
war intel). Record success against the key. A sweep at startup and on a cadence
asks whether the current slot passed without a success, and runs it once. The
period key **is** the idempotency key, so re-running is safe by construction.

This also replaces the fragile pattern in `clan_wars_intel`, whose own comment
records a 2026-08-03 incident where a failed memory write left a sent email
unrecorded, arming a duplicate mass email on the next run.

Seven of the twenty activities are weekly, so a missed slot there costs a week.

### 2. Unverified by production

- `memory_synthesis` — **zero lifetime successes**. Sunday 22:00 CT. Its ceiling
  went 3000 → 16384 after thinking exhausted the budget; tonight is the first
  real test of that diagnosis.
- `promotion_content_cycle` — zero successes, Friday 09:00 CT. `recruiting_copy`
  went 1500 → 8192. The composer was exercised directly and produced all five
  channel keys, but the job has not run.

### 3. Smaller, real

- **`elixir-v5.log` never rotates** — 5.5 MB and growing. It is the launchd
  catch-all where crashes land when the process dies before logging starts, so
  it is exactly the file you need during an outage.
- **`max_retries` is the SDK default of 2 and applies to timeouts**, so wall
  clock is `timeout x 3`. Right for 429/529; questionable for a long background
  call where a timeout is rarely transient. A behaviour trade-off, not a
  cleanup — worth a recommendation to Jamie rather than a unilateral change.
- Seven `ask_elixir_daily` rows around 2026-08-09T05:40Z are probe-origin real
  calls (~$0.70) that inflate that workflow's cost history. Left in place
  because the spend was real; future probes are isolated.

## Traps that cost real time

- **`sqlite3 "file:elixir-v51.db?mode=ro"` fails most of the time** with
  `unable to open database file (14)`. The runtime opens the clan DB per
  operation and closes it; SQLite removes the `-shm` sidecar on clean close, a
  WAL database needs it to be read, and a read-only handle may not create one.
  Open it plainly and issue only SELECTs — WAL readers never block the writer.
  **Never `immutable=1` on the live DB**; it disables locking and can return torn
  rows. That is for the frozen archive only.
- **`scripts/admin.sh status` used to lie** (fixed 2026-08-08): it grepped the
  label as a substring and the sibling agent `com.poapkings.elixir-drop-cr-bridge`
  matched, so it printed "running" while the bot was crash-looping. Column 2 of
  `launchctl list` is the LAST exit status, not the current one.
- **Two timestamp formats.** Telemetry is `...Z`-suffixed, the clan DB is not,
  and SQLite's `datetime('now')` is space-separated — so the naive comparison
  over-selects by a whole boundary day (measured: 4242 rows vs 2428).
- **Never run an ad-hoc probe against the repo.** `telemetry_path()` defaults to
  the production database; four scripts wrote fabricated rows into it. Use
  `scripts/probe_llm.py`, which isolates telemetry by construction.
- **A restart can fail for reasons unrelated to your change.** Discord returned
  503 on the authenticated login endpoint for ~3 minutes on 2026-08-07 and the
  process crash-looped; `KeepAlive` recovered it. Crashes like that appear only
  in `elixir-v5.log`, never in `logs/elixir-error.log`.

## Tooling

- `/log-triage` — the standing health pass. Reads logs, telemetry, and the clan
  DB, and now knows the failure modes that are not failures: a truncation and a
  wasted round trip are both `ok=1`, so an error-only sweep misses them.
- `/llm-cost-report` — spend. Prefers the stored `cost_usd`; keeps a pricing
  ladder only as a fallback for rows before 2026-08-09.
- `scripts/probe_llm.py <workflow>` — exercise a real workflow against the live
  API, isolated. Composes, never delivers.
- `scripts/confidence_report.py`, `scripts/review_agent_feedback.py`.

A 6-hourly `/log-triage` loop ran during this period. It is stopped.
