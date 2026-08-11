# AGENT-TEAM — objective owners for Elixir

This directory defines the three agents that maintain Elixir. The model is
objective-owned: each agent is responsible for a durable outcome and may do the
analysis, implementation, testing, and documentation necessary to achieve it.

The former seven-role issue pipeline and dispatcher were retired on 2026-08-11.
Git history and `summaries/` preserve that period; none of its routing machinery is
part of the current operating model.

## The team

| Objective | File | Cadence | Primary question |
|---|---|---|---|
| **Run Elixir** | `run-elixir.md` | Every four hours | Is Elixir healthy, reliable, observable, and spending intentionally? |
| **Understand Clash Royale** | `understand-clash-royale.md` | Daily | What changed in the game or data, and does Elixir understand it correctly? |
| **Improve Elixir** | `improve-elixir.md` | Daily; deeper Friday pass | Is Elixir acting like an effective agent, and what evidence would make it better? |

There is no separate Build Manager. Building is a capability of every objective
owner. There is no Product Manager intermediary: new member-visible direction goes
straight to Jamie as one concrete decision. There is no Team Manager or dispatcher;
Improve Elixir performs a light Friday health pulse instead. The objectives and their
schedules are the operating system.

## Project map

- `AGENTS.md` is the architecture and repository source of truth.
- `runtime/activities.py` is the scheduled-work registry.
- `logs/elixir-error.log`, `runtime_job_status`, and
  `scripts/confidence_report.py` establish operational health.
- `elixir-telemetry.db` is admin-only evidence for LLM calls, database
  transactions, stalls, and wake behavior. Product behavior must never depend on it.
- `elixir-v51.db` contains the operational engine, raw API receipts and payloads,
  event streams, projections, awareness history, conversations, and durable memory.
- `cr_api.py` is the only Clash Royale API ingress.
- `capabilities/` owns shared domain meaning.
- `runtime/awareness/` owns proactive deliberation and delivery.
- `agent/` owns LLM workflows, tools, routing, and validation.

## Skills by objective

The project skills are procedures, not roles:

- **Run Elixir:** `.claude/skills/log-triage`, `llm-cost-report`, and
  `new-release`; use `awareness-report` when delivery or liveness is implicated.
- **Understand Clash Royale:** `.claude/skills/cr-api-doc-audit`; use
  `awareness-report` to see whether current data reaches editorial decisions.
- **Improve Elixir:** `.claude/skills/awareness-report` plus the repository's
  `scripts/eval_*.py`, feedback, and leader-decision reports.

Read a selected skill completely before using it and follow its evidence and safety
rules. Skills are read-only unless the user or objective file authorizes a change.

## Issue policy

Issues are reserved for multi-run work, external blockers, and decisions that must
persist. Same-run findings should be fixed and verified without creating a routing
ticket.

Open issues use exactly one ownership label:

| Label | Owner |
|---|---|
| `objective:run` | Run Elixir |
| `objective:game` | Understand Clash Royale |
| `objective:agent` | Improve Elixir |

`decision` means Jamie must answer before the objective can continue. Descriptive
labels (`bug`, `reliability`, `data`, `quality`, `enhancement`, `eval`) remain useful,
but they never trigger a handoff.

Run `AGENT-TEAM/scripts/queue-audit.sh` for a read-only view of decisions, objective
backlogs, stale items, and unowned issues.

## Checkout ownership

All three objectives share this checkout. Before a mutation, acquire the local lease
defined in `WORKFLOW.md`. It lives under `.git`, not GitHub, so coordination does not
create issues or comments. A stale lease may be cleared only after eight hours and
only when the worktree is clean:

```bash
uv run --locked python AGENT-TEAM/scripts/objective_lease.py clear-stale --hours 8
```

The normal clean/synchronized preflight rules still apply. The lease does not make a
dirty or ahead checkout safe.

## Deployment

Run Elixir is accountable for the live process. Every four-hour pass compares the
running revision with `origin/main`, inspects the intervening commits, and deploys
safe authorized runtime changes. Prompt-only changes hot-load, but still require the
same human boundary when they affect members.

Do not create a deployment ticket solely to move a commit between agents. The commit,
the running revision, and production verification are the durable record.

Run Elixir owns deployment and technical-health acceptance. The objective that made a
change retains its semantic natural-acceptance watch; a successful restart does not
prove that game meaning or agent behavior improved.

## Human boundary

Jamie retains member-visible, leadership, early-job, and irreversible decisions.
When one is needed, ask one simple yes/no question with the evidence and consequence.
Do not turn it into a chain of internal proposals.

## Attribution

Use the objective identity for commits and durable issue comments:

```bash
uv run --locked python AGENT-TEAM/scripts/agent_attribution.py commit <automation-id> -- <git commit args>
uv run --locked python AGENT-TEAM/scripts/agent_attribution.py issue-comment <automation-id> <issue> --body-file <path>
```

The automation IDs remain stable so existing scheduled-task history and memory are
preserved; their display names and role files now express the objectives.

## North star

Every objective serves one end: help POAP KINGS become a stronger, more connected,
more memorable clan. Prefer signal over noise, facts over assumptions, and a source
fix over a permanent warning.
