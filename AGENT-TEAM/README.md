# AGENT-TEAM/ — Elixir product team

Role prompts for the agents that maintain and improve Elixir. Run issue-driven work as a
**normal, app-visible Codex project task**; keep schedules only for activities whose evidence is
inherently time-windowed or whose purpose is operational recovery. Each file is a self-contained
job description: a lane, an explicit boundary, an "Every run" runbook, and a success definition.

**The workflow these roles share** — the GitHub-Issues spine, the approval gate, the label
taxonomy, `wip` claiming, commit lanes, the `notes/` convention, and the operating rules — is
defined once in **[`WORKFLOW.md`](WORKFLOW.md)** and is identical across all of Jamie's
projects. This README covers only what's specific to *this* project. Every role reads
`AGENTS.md` → `WORKFLOW.md` → this file → its role file, then acts.

```
AGENT-TEAM/
  WORKFLOW.md          # the shared contract (identical across projects)
  README.md            # this file — Elixir specifics
  <role>.md            # the roster below
  error-watch.md       # Operations Manager runbook: logs/elixir-error.log triage
  dispatch.toml        # deterministic issue route and inference rules
  automations.toml     # desired time-windowed/recovery Codex schedules
  scripts/             # preflight, queue audit, automation audit, attribution, notes
  notes/               # gitignored per-run scratch
  summaries/           # committed weekly Manager digests
```

## The team

| Role | File | Lane | Commits? |
|------|------|------|----------|
| Data Analyst | `data-analyst.md` | Turns CR API data into product intelligence for the PM | No — issue-only |
| Product Manager | `product-manager.md` | Discovers opportunities worth doing (the approval gate) | No — issue-only |
| Quality Manager | `quality-manager.md` | Judges if Elixir is actually working | No — issue-only |
| Evaluator | `evaluator.md` | Datasets, scoring, benchmarks, regression tests | Yes — eval harnesses & tests only |
| Build Manager | `build-manager.md` | Works the backlog into tested, deployed changes | **Yes — owns feature/bug code** |
| Operations Manager | `operations-manager.md` | Production health & reliability | Yes — operational fixes + deploys |
| Manager | `manager.md` | Weekly meta-review of the team itself | Own `summaries/` only |

Data Analyst and Quality Manager are Elixir's **domain roles**; the rest are the standard core
(shared across projects). Commit lanes and the approval gate are defined in `WORKFLOW.md`.

`confidence-monitor.md` was retired on 2026-07-28. Its runtime/liveness duties are now part of
the Operations Manager; editorial judgment remains with the Quality Manager. This removes a
hidden eighth role that crossed both lanes and could mutate live Discord posts without an issue.

Elixir is a data-driven agent: capability is downstream of the Clash Royale API data. The
**Data Analyst sits at the front of the pipeline** as a primary input to the Product Manager —
a new game mode, card, event, schema field, or behavior pattern arrives as a *fresh data
pattern* before it's ever a feature request. The analyst catches it, quantifies it, and hands
the Product Manager the product-intelligence picture; the PM decides what is worth proposing,
and the Build Manager builds approved work.

## Visible role tasks

Issue-driven team work runs as **one normal local Codex project task per role**, never as a
background shell or launchd run. GitHub chooses the work and records the handoff; the visible task
is the resumable execution record. The selector is deliberately read-only and cannot claim an
issue, invoke Codex, or mutate the repository.

### Start one role

1. From an active Codex conversation in the `elixir-bot` project, run
   `uv run --locked python AGENT-TEAM/scripts/dispatcher.py --shadow --all`. It names eligible
   issues and roles without changing anything. A human may name a specific issue or role instead.
2. Re-read the issue and run `AGENT-TEAM/scripts/preflight.sh`. Confirm there is **no open `wip`
   anywhere in the repository**: one active claim owns the shared checkout. If safe, add `wip`,
   retain or add exactly one `dispatch:*` label, and comment with the role plus an
   America/Chicago timestamp. Do not create the role task if preflight fails or any claim exists.
3. Run `uv run --locked python AGENT-TEAM/scripts/dispatcher.py --handoff <issue>` and create the
   requested **normal local project task** from the active Codex conversation. Pass the generated
   prompt, issue, claim, and role file. Never substitute a subagent, `codex exec`, launchd, or an
   ephemeral/background shell run; those are not the visible project task Jamie expects.
4. The role does one focused issue-scoped run, updates its title only at meaningful phases, and
   completes the authoritative GitHub transition before its final response.
5. The role removes `wip` and its current `dispatch:*` label, then closes the issue, stops at an
   explicit human state, or leaves exactly one next `dispatch:*` label. It does not invoke the next
   role. A later active conversation creates that visible task.

The selector fails closed while any `wip` claim exists. The 24-hour stale-claim rule in
`WORKFLOW.md` is the only way to recover an abandoned claim. This serializes Build, Operations,
Evaluator artifacts, discovery reports, and Manager summaries against the one shared checkout.

### Live task titles

Set the base title immediately, add only a short phase suffix when the work materially changes,
and keep the whole title at 24 characters or fewer.

| Role | Issue title base | Examples |
|------|------------------|----------|
| Operations Manager | `#245 Ops` | `#245 Ops · deploy`, `#245 Ops · verify` |
| Build Manager | `#245 Build` | `#245 Build · code`, `#245 Build · tests` |
| Evaluator | `#245 Eval` | `#245 Eval · baseline`, `#245 Eval · guard` |
| Data Analyst | `#245 Data` | `#245 Data · query`, `#245 Data · brief` |
| Quality Manager | `#245 Quality` | `#245 Quality · evidence`, `#245 Quality · verify` |
| Product Manager | `#245 Product` | `#245 Product · signal`, `#245 Product · brief` |
| Manager | `#245 Team` | `#245 Team · queue`, `#245 Team · digest` |

For a calendar run with no issue, use `Data 2026-07-31`, `Quality 2026-07-31`, `Eval W31`,
`Product W31`, `Ops Recovery`, or `Team W31`. Finish with `✓` only after a valid repository/GitHub
transition, or `!` when blocked or unable to complete safely. A checkmark says the role completed
its run correctly; it does not mean an evaluation passed or that the issue necessarily closed.

### GitHub handoffs

GitHub issue state, never a task's final prose or calendar, drives executable handoffs.

| Handoff label | Worker |
|---------------|--------|
| `dispatch:operations` | Operations Manager |
| `dispatch:build` | Build Manager |
| `dispatch:evaluator` | Evaluator |
| `dispatch:data` | Data Analyst |
| `dispatch:quality` | Quality Manager |
| `dispatch:product` | Product Manager |
| `dispatch:manager` | Manager |

Exactly one handoff label is active at a time. `needs-data`, `needs-quality`, and `needs-eval`
remember downstream work still owed; `needs-deploy` retains its existing operational meaning.
The initiating conversation owns the `wip` claim. The role removes its current handoff before
finishing, then closes, enters `proposal`/`blocked`/`needs-design`, or adds exactly one next
handoff. Product and meta proposals still wait for Jamie; proven defects and approved work flow
autonomously.

The canonical `manager.md` remains byte-identical across projects. For an Elixir issue-driven
`dispatch:manager` task, the initiating conversation supplies the claim and visible title; the
Manager keeps its existing lane, removes `dispatch:manager` and `wip`, leaves any new `meta`
direction at `proposal` for Jamie, and routes an already-approved implementation to
`dispatch:build`. It never edits another role or launches that next task itself.

There is no automatic launcher to install. Read-only inspection is:

```bash
uv run --locked python AGENT-TEAM/scripts/dispatcher.py --check --live
uv run --locked python AGENT-TEAM/scripts/dispatcher.py --shadow --all
```

## Current runtime map

Elixir runs one v5.1 engine and one operational database. Roles should use this
map when gathering evidence:

- **Ingress:** `cr_api.py` is the only Clash Royale API ingress. Every response
  lands first in `raw_api_payloads` under its true endpoint.
- **Engine:** `engine.tick.run_tick` is the five-step materializer: poll → ingest
  → emit → project → manage. The four event streams and their projections live
  in `elixir-v51.db`.
- **Proactive behavior:** `runtime.awareness` is the sole live proactive
  consumer. Its durable evidence is `awareness_thoughts`, `awareness_posts`,
  per-stream cursors, and `runtime_job_status`.
- **Operational database:** `elixir-v51.db` also holds identity, management,
  conversation, durable memory, and LLM telemetry. The pre-cut
  `elixir-v5-archive-2026H2.db` is immutable historical evidence only.
- **Runtime health:** start with `bash scripts/admin.sh status`,
  `logs/elixir-error.log` (ERROR-only, read it whole), and
  `uv run --locked python scripts/confidence_report.py --quick --json`.
  Elixir does not monitor itself — the `runtime_incidents` ledger and the daily
  `engine-health` job were retired 2026-07-28 because the ledger recorded 0 rows
  in 25 days while the log held 159 real errors. Error detection is the
  Operations Manager's job; the runbook is `error-watch.md`.

The short version: **the engine owns facts and deterministic policy; awareness
owns proactive communication; capabilities own shared read meaning.** Name the
layer and table that supplied every finding.

Operations shell activity runs:

```bash
bash scripts/admin.sh activity run api-sentinel
```

This resolves the activity through `runtime/activities.py`, refuses entries with
`manual_trigger_allowed=False`, and uses a short-lived Discord REST client rather than starting
a second gateway bot process.

## Design docs vs. notes

Elixir keeps committed long-form design docs in `docs/tasks/` (the *why* behind an in-flight
arc — per `AGENTS.md` → Work Tracking); when an arc ships its doc moves to `docs/archive/`, and
stable-system docs live in `docs/reference/`. That is separate from `AGENT-TEAM/notes/`, which
is gitignored per-run scratch (see `WORKFLOW.md`). Durable = issues + `docs/tasks/` + the
Manager's `summaries/`; ephemeral = `notes/`.

## Cadence

Issue handoffs are event-driven through GitHub. Calendar tasks remain only where the input is a
time window or the role is the external recovery watcher. Every scheduled execution is still a
normal visible project task and follows the title protocol above. All times America/Chicago.

| Role | Cadence | Why the calendar task remains |
|------|---------|-------------------------------|
| Operations Manager | Every 3 hours + event-driven | External error/liveness recovery; Elixir does not monitor itself |
| Data Analyst | Daily + event-driven | Detect new CR API patterns within a day |
| Quality Manager | Daily + event-driven | Review a bounded recent behavior window |
| Build Manager | Event-driven only | Queue state is the input; polling adds latency and debris |
| Evaluator | Friday baseline + event-driven | Weekly baseline is time-windowed; acceptance is immediate |
| Product Manager | Weekly discovery + event-driven | Weekly horizon scan plus immediate clarification of domain findings |
| Manager | Weekly + event-driven | Team-health review and notes digest need a fixed period |

The desired live Codex configuration is versioned in `automations.toml`. A `PAUSED` entry is the
historical schedule definition for an event-driven role, not a dormant queue to reactivate. Check
the registry with `uv run --locked python AGENT-TEAM/scripts/automation_audit.py`; use Codex's
automation controls, not shell edits, to apply an authorized schedule change. The audit catches
paused/active drift, stale models, wrong cadences, missing role files, and prompts that omit the
full read order, visible-task protocol, or clean-run rules.

The Build Manager handles exactly one issue per visible task. That keeps the task, claim, diff,
verification, and commit aligned with one durable GitHub transition.

All role tasks share one checkout. A clean checkout that is ahead of `origin/main` is read-only
unless Jamie explicitly authorizes bounded local work; it still may not push, deploy, or restart
into pre-existing commits. Dirty, behind, or diverged checkouts stop the run.

## Agent attribution

Agent work should be visibly attributed without pretending that the roles are separate human or
GitHub accounts:

- End every GitHub issue comment with `— **<automation name>** (agent)`. GitHub will still show
  Jamie's authenticated account as the commenter; changing that requires separately provisioned
  bot accounts or a GitHub App, not a mocked identity.
- Make authorized commits through
  `uv run --locked python AGENT-TEAM/scripts/agent_attribution.py commit <automation-id> -- <git commit args>`.
  The commit author becomes the automation name from `automations.toml`, while the configured
  human remains the committer and the real verified email remains attached. Do not alter the
  repository's persistent `user.name`, invent an email, or replace GitHub credentials.
- Post issue comments through
  `uv run --locked python AGENT-TEAM/scripts/agent_attribution.py issue-comment <automation-id> <issue> --body-file <path>`
  when practical; the helper adds the signature consistently.
- For any multiline issue description, use `gh issue create --body-file <path>`
  (or `--body-file -` with a quoted heredoc). Do not put escaped `\\n` sequences
  in an inline `--body` argument: GitHub renders those as visible text rather than
  Markdown line breaks.

The automation id and display name are versioned in `automations.toml`; that file is the identity
registry as well as the schedule registry.

## North star

Every role serves one end: **help POAP KINGS become a stronger, more connected, more memorable
clan** (`prompts/PURPOSE.md`), expressed through Elixir's persona (`prompts/SOUL.md`). The
Product Manager carries an explicit Decision Filter built on this; the other roles inherit it.
When in doubt, prefer **signal over noise** and **grounded in real data** — Elixir's own
operating principles.

## Label ownership notes (Elixir domain labels)

Beyond the shared taxonomy in `WORKFLOW.md`, Elixir adds `prompt`, `persona`, `data`, and
`quality` (see `scripts/setup-labels.sh` → PROJECT EXTENSIONS):

- `quality` is a triage signal, not a build-ready work order. Quality Manager files it for soft
  patterns; Product Manager weighs it and converts it into `proposal`, `eval`, `bug`, or
  `regression` when there is a clear next action. Build Manager skips bare `quality` issues.
- `data` is similar: Data Analyst files it to describe what changed or what is unused; Product
  Manager or Build Manager only acts when it is relabeled into an actionable lane.
