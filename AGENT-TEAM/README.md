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
  scripts/             # setup-labels.sh · preflight.sh · queue-audit.sh · new-note.sh
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
  conversation, durable memory, incidents, and LLM telemetry. The pre-cut
  `elixir-v5-archive-2026H2.db` is immutable historical evidence only.
- **Runtime health:** start with `bash scripts/admin.sh status`,
  `uv run python scripts/confidence_report.py --quick --json`, open
  `runtime_incidents`, and the repo-root `elixir-v5.log`.

The short version: **the engine owns facts and deterministic policy; awareness
owns proactive communication; capabilities own shared read meaning.** Name the
layer and table that supplied every finding.

Operations shell activity runs:

```bash
bash scripts/admin.sh activity run engine-health
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
| Build Manager | Daily | Steady backlog burn-down |
| Evaluator | Weekly + after any router/prompt/workflow change | Keep baselines current; guard changes |
| Product Manager | Weekly | Discovery benefits from a wider window |
| Manager | Weekly | Team-health review + the notes digest |

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
