<!-- Workflow-Version: 1.1 -->
<!-- CANONICAL SHARED CONTRACT. This file is byte-identical across every project's
     AGENT-TEAM/ directory. Do not add project-specific content here — that lives in
     AGENT-TEAM/README.md and the role files. The Manager diffs this against the
     canonical version each week and files a `meta` issue on drift. -->

# AGENT-TEAM workflow (shared contract)

This is the **common operating contract** for every agent team, identical across all of
Jamie's projects, so the workflow is the same wherever you run. Each project's
`AGENT-TEAM/README.md` names the roster, north star, deploy commands, and domain labels;
this file defines *how the team works* regardless of project.

**Read order, every run:** `AGENTS.md` (repo source of truth) → this `WORKFLOW.md` →
`AGENT-TEAM/README.md` → your role file. Then act.

## The spine: GitHub Issues

GitHub Issues on this repository are the **canonical work queue** — the single place work
is proposed, claimed, handed off, and closed. Roles do not talk to each other directly;
every handoff is a labeled issue. Discovery/insight roles *produce* issues; build/eval/ops
roles *consume* them. Default: **open an issue before non-trivial work.**

Work commits directly to `main` — no PRs — with the issue referenced (`Closes #N`) so
GitHub auto-closes on push. Cross-role findings are handed off by label, never by editing
another lane's code.

## The approval gate (the one human gate)

New product direction passes through Jamie; defects do not.

```
Product Manager files `proposal` (+ a type label)
        │
        ▼
   Jamie reviews  ──decline──▶  close with `wontfix`
        │
     approve  →  swap `proposal` → `approved` + `ready`
        │
        ▼
   Build Manager / Operations Manager / Evaluator pick it up → build AND deploy
```

- **Build/Operations only work `ready`/`approved` issues.** They **skip `proposal`** — an
  un-reviewed idea is never built by accident.
- **Defects skip the gate.** `bug`, `regression`, and `operations` issues flow straight to
  the Build/Operations Managers — no approval needed.
- Once approved, execution is **autonomous**: the Build Manager (and Operations Manager)
  create *and deploy* the change. There is no second gate after Jamie's approval.

## Label taxonomy (identical everywhere)

The core set below is the same in every project (created by
`AGENT-TEAM/scripts/setup-labels.sh`). Projects layer on **domain** work-type labels in the
project-extensions block of that script; the core stays fixed.

| Label | Meaning | Filed by | Worked by |
|-------|---------|----------|-----------|
| `proposal` | New direction **awaiting Jamie's approval — do not build** | Product Manager | Jamie reviews |
| `approved` | Approved by Jamie — cleared to build | Jamie | Build / Ops / Evaluator |
| `ready` | Triaged, actionable now | Jamie / any | Build Manager picks first |
| `needs-design` | Not actionable until the approach is settled | any | (blocks Build) |
| `blocked` | Waiting on an external dependency | any | — |
| `wip` | Claimed — an agent is working this now; others skip it | the working agent | self (released on stop) |
| `needs-deploy` | Committed but **not live** until deployed — e.g. a DB migration; deploy promptly & atomically | Build Manager | Operations Manager (top priority) |
| `bug` | Reproducible defect | any (usually Quality) | Build Manager |
| `regression` | Worked before, now broken — high priority | any | Build Manager (+ Evaluator guard) |
| `enhancement` | New feature or capability | Product Manager | Build Manager |
| `eval` | Missing measurement | any | Evaluator |
| `operations` | Prod health, deploy, runtime | any | Operations Manager |
| `meta` | **Change to the team itself** — a role definition, the workflow, labels, or scripts | Manager (mostly) | Jamie reviews → Build applies |
| `generated` | Filed by an automated agent (not a human) | every agent, on each issue it files | — |

`meta` rides the same gate as `proposal`: the Manager recommends team changes; Jamie
approves; only then are the role files / workflow / scripts edited.

## Commit lanes (who commits what)

Strict lanes stop two agents colliding on the same files:

- **Build Manager** — the only role that commits feature and bug-fix code to `main`.
- **Operations Manager** — commits operational/reliability fixes only; owns deploys/restarts.
- **Evaluator** — commits eval harnesses, datasets, scoring rules, regression tests only.
- **Manager** — commits only its own `AGENT-TEAM/summaries/` digests.
- **Discovery / product / domain-insight roles** (Product Manager, and project roles like
  Data Analyst / Quality Manager / Club Ethnographer) — **never commit product code.** They
  produce GitHub issues and, for durable arcs, committed design docs. They commit those docs
  themselves so the worktree is never left dirty for the Build Manager.

If a role finds work outside its lane, it does **not** reach in — it files a labeled issue.

## Database migrations (never break the live process)

A migration is the one change that touches **shared persistent state a running process is
using**. The hazard: the schema changes while the live process is still on the old code — there
is always an interim between commit and deploy — and *old code + new schema* (or *new code + old
schema*) is a broken service. That is exactly how a split Build→Ops deploy has broken production:
the DB was migrated hours before the matching code was deployed. Three rules keep the window safe:

1. **Additive / backward-compatible only.** A migration must be safe with BOTH the old running
   code and the new code. Add tables, add nullable/defaulted columns, add indexes — old code
   ignores them. A rename / drop / retype / new `NOT NULL` that code depends on is a **breaking**
   change, split across releases: *expand* (add the new, keep writing the old) → *backfill* → cut
   reads over to the new → *contract* (drop the old) only after all old code is gone. Every step
   is safe whichever version is running.
2. **The migration ships with its code and is applied by the deploy — never run against the live
   DB out-of-band.** The Build Manager writes the migration and the code that needs it in the
   **same commit**, and tests it against a throwaway/scratch database. It must never point a
   new-code process — the service, a test run, or a script — at the **live** database, because
   that applies the migration early (many frameworks migrate on first connect). Production is
   migrated exactly once, at the deploy/restart, by the Operations Manager, so schema and running
   code change together.
3. **The migration is committed *inert* and made live by the deploy — coordinated through the
   queue, not a live call.** Roles are independent task-runs; they coordinate only via labeled
   issues (there is no inline "Build calls Ops"). That's fine, and it's why rule 2 matters:
   because the live schema doesn't change until a restart applies it, a committed-but-undeployed
   migration is **inert and safe** — the running old code keeps using the old schema until the
   Operations Manager's next run restarts into the new code, applying the migration atomically
   with it. So the Build Manager labels the issue `needs-deploy` and leaves it open; that is the
   Operations Manager's **top priority every run**, bounding the gap to one Ops cycle. Never
   "resolve" the wait by applying the migration to the live DB early — that is the original break.

Each project's migration framework — how migrations are declared and applied — is documented in
its `AGENTS.md` / `CLAUDE.md`.

## Concurrency: claim before you work

Many agents reading one queue can pick up the same issue. The `wip` label is the claim:

1. **Skip anything already labeled `wip`** — another agent has it.
2. Claim it: add `wip` and comment with role + timestamp (e.g. "Build Manager claiming at
   2026-07-01 09:00 CT") **before** starting.
3. When done, remove `wip` (closing with `Closes #N` clears it automatically). If you stop
   without finishing, remove `wip` so it returns to the queue.
4. **Stale-claim rule:** if a `wip` issue has had no update for 24 hours, another agent may
   take it by commenting with the stale evidence, replacing the claim, and continuing. If the
   stale issue is risky or ambiguous, comment and stop instead of taking over.

## notes/ — per-run scratch (gitignored)

`AGENT-TEAM/notes/` is **gitignored** working memory. At the end of a run, drop a short file
(`AGENT-TEAM/scripts/new-note.sh <role> <slug>` scaffolds one) recording: what you did, the
evidence you used, the issues you filed/claimed/closed, and any handoff. Notes are how the
team (and the weekly Manager) sees recent activity **without polluting git** with per-run noise.

**Signal stays committed; noise does not.** Committed = GitHub issues (the ledger), the
Manager's `AGENT-TEAM/summaries/` digests, and design docs for multi-issue arcs. Ephemeral =
`notes/` run logs.

## Operating rules (all roles)

1. **Start with the git preflight** (`AGENT-TEAM/scripts/preflight.sh`): fetch, check status;
   if the worktree is dirty / behind / diverged / unexpectedly ahead, **stop and report** via
   an issue — do not pull, merge, rebase, or stash from an automated run. **End every run with
   `git status` clean**; commit any doc you wrote before finishing; push only when doing so
   won't publish unrelated pre-existing commits.
2. Do **one** focused thing per run. Never bundle unrelated work.
3. **Claim before you work** (`wip`); skip anything already claimed.
4. Tag every issue you file with `generated` so agent-filed work is distinguishable.
5. **Stay in your lane.** Hand off across lanes via labeled issues, never by editing another
   role's code.
6. Don't flood the queue: file **at most ~3 new issues per run**. More than that is a pattern
   worth one summary issue, not ten.
7. **A quiet run is a valid run.** If there's nothing safe and in-lane to do, say so and stop —
   don't manufacture work.
8. All times **America/Chicago**.

## The Manager (weekly meta-review)

One role stands outside the lanes and keeps the team healthy. Weekly, the **Manager**:
reviews how each role is performing and files any role-definition changes as `meta` proposals
(Jamie approves — never self-applied); writes a committed weekly digest of `notes/` to
`AGENT-TEAM/summaries/`; audits the queue for work slipping through the cracks (unclaimed/aging
issues, stale `wip`, `proposal`s awaiting Jamie, stale `needs-design`/`blocked`); and checks
this `WORKFLOW.md` against the canonical `Workflow-Version`, filing a `meta` issue on drift.
See `AGENT-TEAM/manager.md`.

## Backlog hygiene

The Product Manager grooms the queue weekly (dedupe across roles, close/relabel stale
`needs-design`/`blocked`, surface `proposal`s awaiting Jamie). This is maintenance, not new
direction — no approval gate. The Manager audits that this is happening.

<!-- End of canonical contract — Workflow-Version: 1.1 -->
