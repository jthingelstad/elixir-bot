# docs/

How the documentation here is organized.

| Path | What it holds | Lifecycle |
|---|---|---|
| `data-model-erd.md` | The SQLite data-model ERD (member/war/memory graph). | Living reference. |
| `reference/` | Enduring design docs for systems that are live and ongoing. | Keep current. |
| `archive/` | Completed work — design plans, runbooks, and analyses for arcs that have shipped. Kept for provenance; not maintained. | Frozen. |
| `tasks/` | **Active** long-form design docs and the product-team's briefs/reports (Data Analyst, Quality Manager, Product Manager write here). | Churns; graduate to `reference/` when stable, or `archive/` when the arc completes. |
| `cr-api-docs/` | Vendored Clash Royale API reference (self-contained, own tooling). | External; update via its own flow. |
| `poap-api-docs/` | Vendored POAP API reference. | External. |

## Current contents

**`reference/`** — `v5.1/` (the v5.1 engine build spec, now the stable-system
reference: architecture, schema, events, recognition, runtime, management,
migration, plus `open-questions.md` as the decision record — shipped
2026-07-03/04), `memory-system.md`, `long-term-trend-data.md`,
`signal-inventory.md`. Note: `signal-inventory.md` and parts of
`memory-system.md` describe the pre-v5.1 signal system — superseded where
they conflict with `v5.1/`.

**`archive/event-core-v5/`** — the v5 Event Core migration + v4 signal-system teardown (shipped 2026-06): the event-sourcing design plan, architecture-boundary decision, completion roadmap, cutover runbook, remediation plan, review findings, the v4-deletion runbook, the internal-data-subsystem pivot, and the original agentic-awareness-loop vision it replaced.

**`archive/assessments/`** — the point-in-time analyses that motivated v5: data-flow gap, front-end stream-adoption gap, and the confirmed stream-redesign direction.
