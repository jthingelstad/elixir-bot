# AGENT-TEAM/ — Elixir product team

Role-prompts, each meant to run as a **scheduled Codex/Claude agent** that maintains and
improves Elixir. Each file is a self-contained job description: a lane, an explicit boundary,
an "Every run" runbook, and a success definition. Point a scheduled agent at one file and let
it run.

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
  automations.toml     # desired Codex schedules for every rostered role
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

## Suggested cadence

Recommended defaults — the actual scheduling lives in Codex/Claude routines. All times
America/Chicago.

| Role | Cadence | Why |
|------|---------|-----|
| Operations Manager | Hourly (or every few hours) | Prod health needs a tight loop |
| Data Analyst | Daily | A new game mode / card / event should surface within a day |
| Quality Manager | Daily | Catch regressions and noise fast |
| Build Manager | Every 6 hours | Drain up to three issues sequentially without batching work |
| Evaluator | Weekly + after any router/prompt/workflow change | Keep baselines current; guard changes |
| Product Manager | Weekly | Discovery benefits from a wider window |
| Manager | Weekly | Team-health review + the notes digest |

The desired live Codex configuration is versioned in `automations.toml`. Check it with
`uv run --locked python AGENT-TEAM/scripts/automation_audit.py`; use `--apply` only when Jamie
has authorized schedule changes. The audit also catches paused roles, stale models, wrong
cadences, missing role files, and prompts that do not enforce the full read order and clean-run
rules.

The Build Manager has one approved project-specific refinement to the shared workflow's “one
focused thing per run” rule: it may complete up to three issues **sequentially** inside one
90-minute run. “One focused thing” applies to each claim, change, verification cycle, and commit;
it never permits a batched claim or a commit spanning issues. `build-manager.md` owns the exact
loop and stop conditions.

All scheduled roles share one checkout. A clean checkout that is ahead of `origin/main` is
read-only: agents may inspect evidence and file issues, but must not commit, push, deploy, or
restart into those pre-existing commits. Dirty, behind, or diverged checkouts stop the run.

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
