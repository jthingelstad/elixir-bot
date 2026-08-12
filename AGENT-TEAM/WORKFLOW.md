# AGENT-TEAM operating model

Elixir is maintained by three objective owners. An objective owner is accountable
for an outcome, not a type of task or a directory of code. It follows evidence
through diagnosis, implementation, verification, and production acceptance rather
than handing each step to another role.

Read order for every run:

1. `AGENTS.md`
2. this file
3. `AGENT-TEAM/README.md`
4. the objective file named by the automation

## Objectives

| Objective | Owner file | Outcome |
|---|---|---|
| Run Elixir | `run-elixir.md` | Elixir reliably performs its intended work and its cost is visible and controlled. |
| Understand Clash Royale | `understand-clash-royale.md` | Elixir's model of the game and clan data remains current, complete, and accurate. |
| Improve Elixir | `improve-elixir.md` | Elixir is a useful, accurate, timely, well-calibrated agent. |

These are accountability boundaries, not file ownership. If solving an objective
requires changing code, prompts, tests, data contracts, documentation, or tooling,
the owner does that work when it is safe and authorized.

## The operating loop

Every objective run uses the same loop:

1. Run `AGENT-TEAM/scripts/preflight.sh` and establish the live state from the
   objective's authoritative evidence.
2. Measure before changing. Reproduce the observed problem from logs, telemetry,
   persisted state, exact messages, captured API payloads, or a live read-only API
   probe as appropriate.
3. Decide whether there is an actual objective gap. A healthy no-op is a successful
   run; do not manufacture work.
4. If the gap is clear and safe, fix it at the source in the same run. Guards and
   prompt prohibitions are last resorts.
5. Before the first repository mutation, acquire the local checkout lease:

   ```bash
   uv run --locked python AGENT-TEAM/scripts/objective_lease.py claim <run|game|agent>
   ```

   Retain the returned `lease_id` for this run and use it with `check` before the first
   edit and before push, then `release --lease-id <id>` after the repository is clean.
   Read-only work needs no lease. The lease records the run token, Codex task identity,
   host, and starting commit. A held lease means another objective owns the checkout;
   stop before mutation. Never infer inactivity
   from age plus a clean worktree; use the proof-based stale clear or the explicit
   inspected manual clear documented in `README.md`.
6. Add regression coverage, run the proportionate focused checks, then run
   `scripts/gates.sh` before committing.
7. Commit and push only the work created by the current run. Never publish an
   unrelated pre-existing commit. The commit is the durable implementation record;
   an issue number is optional.
8. Run Elixir compares the deployed revision with `origin/main` every cadence and
   deploys safe committed runtime changes. Revision state is the deployment queue.
9. Verify the outcome from natural production evidence. Do not manufacture member
   activity, send an early report, or post synthetic Discord traffic for acceptance.

## Acceptance ownership

Run Elixir owns deployment acceptance: the intended revision is running, the process
is healthy, migrations and scheduled work are sound, and no fresh operational failure
appeared. The objective that originated a change owns semantic acceptance: Understand
Clash Royale proves the data now means the right thing, and Improve Elixir proves the
member or leadership outcome is better from natural evidence. Run reports the deployed
revision to the originating objective; it does not inherit that objective's judgment.

## Issues are the exception ledger

Do not open an issue merely to authorize, claim, route, deploy, or close work that
can be completed in the current run.

Open or retain an issue only when at least one is true:

- the work cannot reasonably finish in the current run;
- Jamie must make a decision;
- an external dependency blocks progress; or
- the objective is substantial enough to need a durable multi-run record.

Every open issue has exactly one objective label:

- `objective:run`
- `objective:game`
- `objective:agent`

The same objective owner retains it until completion. There are no dispatch labels,
handoff labels, `wip` claims, or Build/Operations/Evaluator routing chains. Work-type
labels such as `bug`, `reliability`, `data`, or `quality` may describe the finding,
but they do not choose a worker.

Use `decision` only for a question Jamie must answer. In an active conversation,
ask one concrete yes/no question directly instead of creating a ticket. An issue is
appropriate only when the decision must survive beyond the conversation.

## Authority and human boundary

Standing authority includes read-only production inspection, source fixes, tests,
commits, pushes, monitoring changes, deploys, and restarts when they are within the
objective and preserve the boundary below.

Jamie owns:

- changes to what members see or how Elixir behaves toward them;
- posting to Discord or clan chat outside the natural schedule;
- mass email or triggering a member-facing job early;
- leadership decisions and irreversible actions.

An objective owner may fully diagnose and frame one of these decisions, but it asks
Jamie before implementing or triggering it. Reliability work may restore an already
approved natural schedule; it must not force the missed member-facing run as a test.

## Shared checkout safety

- Dirty, behind, or diverged preflight is terminal for mutation.
- Unexpected commits ahead of `origin/main` make the run read-only unless Jamie has
  explicitly authorized those exact commits.
- Only one objective may hold the local checkout lease. Read-only analysis may run
  concurrently.
- Never stash, reset, clean, rebase, or overwrite another run's work.
- End mutating runs with a clean repository and release the lease.

## Reporting

Every run uses the same closeout contract:

- **HEALTHY** — evidence checked; no action required.
- **CHANGED** — source fix completed and verified; include deployment and acceptance state.
- **WATCHING** — technical work is complete but named natural evidence is still pending.
- **BLOCKED** — an external dependency or safety/concurrency gate prevents progress.
- **NEEDS JAMIE** — one concrete yes/no decision is required, with consequences.

Use this compact shape so Jamie can scan every team the same way:

```text
Outcome: HEALTHY | CHANGED | WATCHING | BLOCKED | NEEDS JAMIE
Objective: <objective name>
Evidence: <most decision-relevant facts>
Action: <what changed, or None>
Next check: <natural event/date, or None>
Jamie: <one yes/no question, or None>
```

Report outcomes and remaining risk, not workflow ceremony.

## Automation memory

Each objective keeps only a compact working set in its automation memory:

1. `Current state` — the latest durable operating facts needed by the next run.
2. `Active watches` — unresolved natural acceptance, decisions, or blockers only.
3. `Latest run` — one replace-in-place summary with timestamp, evidence, action, and
   outcome.

Replace `Latest run` on every pass and remove resolved watches. Commits, issues,
telemetry, and production ledgers hold history; automation memory must not accumulate
a narrative of every prior run.
