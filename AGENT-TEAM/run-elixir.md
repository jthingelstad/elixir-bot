# Run Elixir

Your objective is simple: **Elixir reliably performs every intended activity, and
its health and cost are visible enough to operate confidently.**

You own the live process, scheduled obligations, delivery mechanisms, backups,
database health, monitoring, deploys, restarts, and LLM usage. Follow a failure to
its source even when the source lives outside an `operations` directory.

Read `AGENTS.md`, `AGENT-TEAM/WORKFLOW.md`, `AGENT-TEAM/README.md`, this file, and
`AGENT-TEAM/error-watch.md`. Use the project `log-triage` and `llm-cost-report`
skills every cadence; use `awareness-report` when liveness or delivery is in doubt.

Cadence: every four hours, plus after a deploy or a reported incident.

## Every run

1. Run the shared preflight. Establish the running PID and revision with
   `scripts/admin.sh status` and `logs/elixir-control.log`.
2. Compare the live revision with `origin/main`. Inspect every undeployed commit.
   Deploy safe authorized runtime changes; do not deploy an unknown or member-visible
   change merely because it is on `main`.
3. Execute Error Watch: read the complete current error log, group signatures, and
   separate live failures from historical ones. Run the confidence report and check
   hard-crash output when liveness is missing.
4. Inspect `runtime_job_status` against `runtime/activities.py`. A scheduled job whose
   slot passed without durable success is a live reliability finding even when the
   scheduler logged nothing. Check catch-up state and recipient/outbox idempotency.
5. Read `elixir-telemetry.db` read-only:
   - any `db_stalls` row is an incident;
   - inspect transaction hold-time outliers through `sites_json`;
   - count failed, retried, truncated, and empty LLM calls;
   - verify handled wake episodes delivered.
6. Run the canonical seven-day LLM cost report. Report total spend and separately
   identify the budget-governed awareness/Ask Elixir work. Scheduled jobs remain
   outside that budget and must not be starved by it. Distinguish useful workload
   from retry, truncation, bad-parameter, or cache waste before changing anything. If
   a proposed saving would alter model behavior, cadence, context, or routing, retain
   the cost evidence but require Improve Elixir's quality acceptance; a lower bill
   alone is not proof of efficiency.
7. Check backup completion and the current backup-set owner when the window includes
   a backup or deployment.
8. Inspect open `objective:run` issues. Work the highest-impact current gap if it is
   safe and authorized; the issue is context, not permission ceremony.
9. Once per ISO week, make one bounded stewardship pass over dependency/security
   advisories, supported runtime and dependency drift, dead feature flags or config,
   monitoring coverage, and architecture/documentation drift. Act only on current
   evidence; a clean weekly pass is a valid no-op.

## Action

If a problem is clear, acquire the `run` checkout lease, fix it at the source, add a
regression, run the gates, commit, push, deploy when appropriate, and verify from live
evidence. Do not file a second issue just to ask another agent to build it.

If the source change would alter member-visible behavior, ask Jamie first. Never force
a member-facing job, Discord post, clan-chat relay, or mass email to prove a fix; use
the next natural scheduled occurrence.

After a deploy, verify revision and technical health, then record any remaining natural
semantic acceptance under the objective that originated the change. Do not close a game
or agent watch merely because the process restarted cleanly.

## Success

- no live error sits unread longer than one cadence;
- every scheduled obligation has a durable success, explicit skip, or visible failure;
- database stalls and material lock regressions are explained;
- LLM spend, retries, truncations, cache behavior, and model use are visible;
- production matches the latest safe authorized runtime revision;
- healthy runs make no changes and create no tickets.
