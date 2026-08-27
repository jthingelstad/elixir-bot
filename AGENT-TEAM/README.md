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
| **Improve Elixir** | `improve-elixir.md` | Daily; deeper Friday pass | Is Elixir turning play and feedback into better stewardship, memory, and member improvement over time? |

There is no separate Build Manager. Building is a capability of every objective
owner. There is no Product Manager intermediary: new member-visible direction goes
straight to Jamie as one concrete decision. There is no Team Manager or dispatcher;
Improve Elixir performs a light Friday health pulse instead. The objectives and their
schedules are the operating system.

## Intelligence and efficiency remain objective-owned

Do not add a fourth Intelligence, Data Analyst, Evaluator, or Cost Optimizer agent.
Those names split outcomes the current objectives already own and would recreate the
retired handoff pipeline.

| Need | Owner | Decision standard |
|---|---|---|
| Better reasoning, learned context, tool choice, and editorial judgment | **Improve Elixir** | Exact natural behavior must improve, not merely an offline score. |
| Newly useful Clash Royale or clan data | **Understand Clash Royale** | Trace receipts through events and capabilities before calling a signal meaningful. |
| Spend, retries, cache efficiency, and model-call reliability | **Run Elixir** | A lower bill is a win only when it preserves the relevant quality outcome. |

Run supplies the canonical cost and reliability evidence. Improve owns quality
acceptance whenever a savings change could alter model behavior, cadence, context, or
routing. Understand owns any missing source representation discovered along the way.
The originating objective keeps the work through acceptance; this is a shared evidence
contract, not an analyst-to-builder handoff.

## How Jamie engages the team

Jamie can start with the outcome instead of choosing a role or preparing a ticket:

- `Run <objective> now and own the highest-impact measured gap.`
- `Investigate <symptom>; choose the owner by the failed outcome, not the file.`
- `Show me team status only; make no changes.`
- `What across this team needs Jamie?`
- `Resume the active watch for <objective or issue>.`

Choose **Run Elixir** for execution, delivery, persistence, health, recovery, or cost;
**Understand Clash Royale** for game facts, payload meaning, projections, or source
semantics; and **Improve Elixir** when the sources are sound but behavior, judgment,
timing, grounding, or usefulness is wrong. Cross-cutting work keeps one originating
owner through acceptance.

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

Run `python3 AGENT-TEAM/scripts/automation_audit.py` to validate the registry against
installed Codex tasks; add `--registry-only` when only repository state is in scope.

## Checkout ownership

All three objectives share this checkout. Before a mutation, acquire the local lease
defined in `WORKFLOW.md` and retain its returned `lease_id`; only that token may check
or release the lease normally. It lives under `.git`, not GitHub, so coordination does
not create issues or comments. Each lease records its objective, run token, holder
identity, host, and starting commit. Automatic stale recovery additionally requires a durable holder
PID, proof that the process is gone, a clean worktree, and an unchanged commit:

```bash
uv run --locked python AGENT-TEAM/scripts/objective_lease.py clear-stale --hours 8
```

When holder inactivity cannot be proved automatically, inspect the lease and active
work first, then clear it explicitly by repeating its exact `holder_id`:

```bash
uv run --locked python AGENT-TEAM/scripts/objective_lease.py status
uv run --locked python AGENT-TEAM/scripts/objective_lease.py clear-manual \
  --holder-id <exact-holder-id> --confirm-inactive
```

For a pre-upgrade lease with no recorded holder, the exact compatibility identifier
is `legacy-unidentified`; the same inspection and confirmation are still required.

The normal clean/synchronized preflight rules still apply. The lease does not make a
dirty or ahead checkout safe.

## Deployment

Run Elixir is accountable for the live process. Every four-hour pass compares the
running revision with `origin/main`, inspects the intervening commits, and deploys
safe authorized runtime changes. Prompt-only changes hot-load, but still require the
same human boundary when they affect members. An originating owner may make the narrow
backed-up restart exception defined in `WORKFLOW.md` for its own already-pushed runtime
fix; it does not assume Run Elixir's technical-health acceptance role.

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
uv run --locked python AGENT-TEAM/scripts/prepare_commit.py <current-run paths>
uv run --locked python AGENT-TEAM/scripts/agent_attribution.py commit <automation-id> -- -m "Commit message"
uv run --locked python AGENT-TEAM/scripts/agent_attribution.py issue-comment <automation-id> <issue> --body-file <path>
```

The prepare helper formats changed Python paths and stages only the named files; it
refuses a mixed index. The commit helper invokes `git commit` itself and refuses an
empty index; pass only its arguments after `--`.

The automation IDs remain stable so existing scheduled-task history and memory are
preserved; their display names and role files now express the objectives.

## North star

Every objective serves one end: help POAP KINGS become a stronger, more connected,
more memorable clan. Prefer signal over noise, facts over assumptions, and a source
fix over a permanent warning.
