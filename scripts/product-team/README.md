# product-team/

Six role-prompts, each meant to run as a **scheduled Codex/Claude agent** that maintains and improves Elixir. Each file is a self-contained job description: a lane, an explicit boundary, an "Every run" runbook, and a success definition. Point a scheduled agent at one file and let it run.

These agents do not talk to each other directly. **GitHub Issues on `jthingelstad/elixir-bot` is the coordination spine** — the canonical queue, exactly as defined in `AGENTS.md` → *Work Tracking*. Discovery, data, and quality roles *produce* issues; the build and eval roles *consume* them. Every handoff is a labeled issue.

## The team

| Role | File | Lane | Commits code? |
|------|------|------|---------------|
| Data Analyst | `data-analyst.md` | Turns CR API data into product intelligence for the PM | No — issue-only |
| Product Manager | `product-manager.md` | Discovers opportunities worth doing | No — issue-only |
| Quality Manager | `quality-manager.md` | Judges if Elixir is actually working | No — issue-only |
| Evaluator | `evaluator.md` | Datasets, scoring, benchmarks, regression tests | Yes — eval harnesses & tests only |
| Build Manager | `build-manager.md` | Works the backlog into tested changes | **Yes — owns feature/bug code** |
| Operations Manager | `operations-manager.md` | Production health & reliability | Yes — operational fixes only |

Elixir is a data-driven agent: capability is downstream of the Clash Royale API data. The **Data Analyst sits at the front of the pipeline** as a primary input to the Product Manager — a new game mode, card, event, schema field, or behavior pattern arrives as a *fresh data pattern* before it's ever a feature request. The analyst catches it, quantifies it, and hands the Product Manager the product-intelligence picture; the PM decides what is worth proposing, and the Build Manager builds approved work.

## Commit ownership (strict lanes)

To stop two scheduled agents colliding on the same files, code-commit rights are partitioned:

- **Build Manager** — the only role that commits feature and bug-fix code to `main`.
- **Operations Manager** — commits operational/reliability fixes only.
- **Evaluator** — commits eval harnesses, datasets, scoring rules, and regression tests only.
- **Data Analyst, Quality Manager & Product Manager** — never commit product code. They produce GitHub issues and `docs/tasks/` reports, and they commit those reports themselves so they never leave the worktree dirty for the Build Manager. They push only when the shared git preflight says doing so will not publish unrelated existing commits.

If a role finds work outside its lane, it does **not** reach in — it files a labeled issue and moves on.

## Current runtime map

Elixir is currently a v5/Event Core hybrid. Product-team agents should use this
map when gathering evidence:

- **v5 event store:** `elixir-v5-events.db` is the authoritative event-sourced
  store for Event Core domain events, detections, recommendations, decision cases,
  and reactive communication intent history.
- **v5 operational DB:** `elixir-v5.db` contains operational read models,
  survivor tables, `detections`, `battle_telemetry`, runtime status, messages,
  memory indexes, and legacy tables that have not yet been retired.
- **v5 memory DB:** `elixir-v5-memory.db` is the durable clan-memory store.
- **Runtime health:** use `python -m event_core.live.health` and
  `python -m event_core.live.monitor` for Event Core health, follower lag,
  deliverable pending work, and recent reactive ticks.
- **Legacy teardown — mostly done:** the v4 signal/awareness code is deleted
  (`runtime/signals/`, `signal_lanes.py`, `situation.py`, `runtime/jobs/_signals.py`),
  and the `game_event_stream` / `event_rollups` storage modules are gone. Their
  tables remain on disk only as dormant data pending a drop migration. A few
  legacy tables (`signal_log`, `signal_detector_cursors`) are still read by live
  code. Do not treat any of these as the primary live reasoning model unless the
  task is explicitly about retiring or auditing legacy behavior.

The short version: **v5/Event Core owns proactive reactive behavior; the
operational DB still owns many read/query surfaces.** When a role investigates
Elixir's flow, check both layers and name which layer supplied the evidence.

Operations shell activity runs:

```bash
bash scripts/admin.sh activity run v5-reactive-tick
```

This resolves the activity through `runtime/activities.py`, refuses entries with
`manual_trigger_allowed=False`, and uses a short-lived Discord REST client
rather than starting a second gateway bot process.

## The issue loop

```
Data Analyst ──data──> Product Manager ──proposal──> Jamie approves ──approved/ready──┐
                                                                                     ├──> Build Manager ──commit (Closes #N)──> main
Quality Manager ──bug / regression──────────────────────────────────────────────────┘
        │                                                                                │
        └──eval──> Evaluator <──eval──┘ (baselines + regression tests guard the change)
                                                                                         │
Operations Manager ──operations / reliability────────────────────────────────────────────┘ (own fixes; deploys & restarts)
```

Product direction passes through Jamie's approval; defects (bug/regression/operations) do not.

- A role **defaults to creating an issue before non-trivial work** (AGENTS.md).
- Work commits directly to `main` — no PRs — with the issue referenced (`Closes #N`) so GitHub auto-closes on push.
- Cross-role findings are handed off by label, never by editing another lane's code.

## Approval gate

**Jamie approves the Product Manager's suggestions before anything gets built.** Defects don't need this — bugs, regressions, and operational fixes flow straight to the Build/Operations Managers. The gate is only on *new product direction* coming from the Product Manager.

```
Product Manager files `proposal` (+ type label)
        │
        ▼
   Jamie reviews  ──decline──>  close with `wontfix`
        │
     approve
        │
        ▼
  swap `proposal` → `approved` + `ready`   ──>  Build Manager / Evaluator picks it up
```

The Build Manager **skips `proposal`** and **only builds `ready`/`approved`** issues, so an un-reviewed idea can never be built by accident.

## Label taxonomy

This is the live set after running `setup-labels.sh`. It's the shared vocabulary that routes work between agents.

| Label | Meaning | Filed by | Worked by |
|-------|---------|----------|-----------|
| `operations` | Prod health, deploy, runtime | any | Operations Manager |
| `reliability` | Stability, runtime, cost, observability | any | Operations Manager |
| `bug` | Reproducible defect | Quality Manager (mostly) | Build Manager (Ops only if operational) |
| `regression` | Worked before, now broken — high priority | Quality Manager | Build Manager (+ Evaluator guard) |
| `enhancement` | New feature or capability | Product Manager | Build Manager |
| `prompt` | Prompt / persona-text change | Product Manager, Quality Manager | Build Manager (+ Evaluator) |
| `persona` | Gap vs. `SOUL.md` / `PURPOSE.md` (existing convention) | Product Manager, Quality Manager | Build Manager |
| `eval` | Missing measurement | Quality Manager, Product Manager | Evaluator |
| `data` | New/changed data pattern or data-quality finding | Data Analyst | Product Manager triages / Build Manager only after relabel |
| `quality` | Recommendation quality: accuracy, noise, routing, delivery | Quality Manager | Product Manager triages / Build Manager only after relabel |
| `proposal` | PM idea **awaiting Jamie's approval — do not build** | Product Manager | Jamie reviews |
| `approved` | Approved by Jamie — cleared to build | Jamie | Build Manager / Evaluator |
| `ready` | Triaged, actionable now | Jamie / any | Build Manager picks these first |
| `needs-design` | Not actionable until the approach is settled | any | (blocks Build Manager) |
| `blocked` | Waiting on an external dependency | any | — |
| `wip` | Claimed — an agent is working this now; others skip it | the working agent | self (removed when done/abandoned) |
| `generated` | Filed by an automated product-team agent | every agent, on each issue it files | — |

Kept defaults: `documentation`, `duplicate`, `invalid`, `question`, `wontfix`, plus Dependabot's `dependencies` / `github_actions` / `python`. Run `setup-labels.sh` (in this directory) to create/rename/clean labels to match this table; it is idempotent.

## Suggested cadence

Recommended defaults — tune to taste. All times America/Chicago (AGENTS.md convention).

| Role | Cadence | Why |
|------|---------|-----|
| Operations Manager | Hourly (or every few hours) | Prod health needs a tight loop |
| Data Analyst | Daily | A new game mode / card / event should surface within a day |
| Quality Manager | Daily | Catch regressions and noise fast |
| Build Manager | Daily | Steady backlog burn-down |
| Evaluator | Weekly + after any router/prompt/workflow change | Keep baselines current; guard changes |
| Product Manager | Weekly | Discovery benefits from a wider window |

## North star

Every role serves one end: **help POAP KINGS become a stronger, more connected, more memorable clan** (`prompts/PURPOSE.md`), expressed through Elixir's persona (`prompts/SOUL.md`). The Product Manager carries an explicit Decision Filter built on this; the other roles inherit it. When in doubt, prefer **signal over noise** and **grounded in real data** — Elixir's own operating principles.

## Concurrency: claiming work

Strict lanes stop two agents committing to the same files, but six agents reading one issue queue can still both pick up the same issue — most likely the Build and Operations Managers, who both scan `bug`/`regression`. The `wip` label is the claim:

1. Before working an issue, **skip anything already labeled `wip`** — another agent has it.
2. Claim it: add `wip` and comment with role + timestamp, e.g. "Build Manager claiming this at 2026-06-23 09:00 CT." *before* starting.
3. When done, remove `wip` (closing the issue with `Closes #N` is enough). If you stop without finishing, remove `wip` so it returns to the queue.
4. `bug` routing: a `bug` defaults to the **Build Manager**. The Operations Manager only takes one if it's genuinely operational — and when it does, it relabels `operations` so ownership is unambiguous.
5. Stale claim rule: if a `wip` issue has no update for 24 hours, another agent may take it by commenting with the stale evidence, replacing the claim, and continuing. If the stale issue is risky or ambiguous, leave a comment and stop instead of taking over.

## Backlog hygiene

Someone has to own the health of the queue, or it silently fills. The **Product Manager** grooms weekly: dedupe overlapping issues across roles, close or relabel stale `needs-design`/`blocked` items, and surface `proposal`s still awaiting Jamie's decision. This is queue maintenance, not new direction — it needs no approval gate.

## Operating rules shared by all roles

1. Start with the shared git preflight:
   - `git fetch origin --prune`
   - `git status --short --branch`
   - If the worktree is dirty, stop and open/comment an issue describing the unexpected state.
   - If the branch is behind or diverged, stop and report; do not pull, merge, rebase, or stash from an automated run.
   - If the branch is ahead before you begin, report that fact and do not push unless your role is explicitly expected to publish the existing ahead commits too.
   - **End every run with `git status` clean.** If you wrote anything to `docs/tasks/`, commit it before you finish. Push only when doing so will not publish unrelated pre-existing local commits.
2. Do **one** focused thing per run. Never bundle unrelated work.
3. Claim before you work (`wip`), and skip anything already claimed. See *Concurrency* above.
4. Tag every issue you file with `generated` so agent-filed work is distinguishable from human-filed.
5. Stay in your lane. Hand off across lanes via labeled issues, never by editing another role's code.
6. Don't flood the queue: file **at most ~3 new issues per run**. If you're seeing more than that, the signal is a pattern worth one summary issue, not ten.
7. A quiet run is a valid run. If there's nothing safe and in-lane to do, report it and stop — don't manufacture work.
8. Read `AGENTS.md` (source of truth for architecture, work tracking, conventions) before acting.

## Label ownership notes

- `quality` is a triage signal, not a build-ready work order. Quality Manager
  files it for soft patterns; Product Manager weighs it and converts it into
  `proposal`, `eval`, `bug`, or `regression` when there is a clear next action.
  Build Manager skips bare `quality` issues.
- `data` is similar: Data Analyst files it to describe what changed or what is
  unused; Product Manager or Build Manager only acts when it is relabeled into an
  actionable lane.
