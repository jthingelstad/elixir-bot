# Documentation

| Path | Purpose | Lifecycle |
|---|---|---|
| `data-model-erd.md` | Current logical map of the v5.1 database. | Living reference. |
| `reference/current-architecture.md` | Concise map of the live data and capability flow. | Living reference. |
| `reference/` | Enduring system design and operational contracts. | Keep current or label historical sections clearly. |
| `reference/v5.1/` | The locked v5.1 build specification plus post-cut design addenda. | Preserve the original rationale; `AGENTS.md` wins when later production decisions supersede it. |
| `tasks/` | Active long-form designs and product-team reports only. | Move completed work to `archive/`. |
| `archive/` | Shipped plans, point-in-time assessments, and completed reports. | Frozen provenance; links must still resolve. |
| `cr-api-docs/` | Vendored Clash Royale API reference with its own tooling. | Update through its own workflow. |

## Source-of-truth order

1. `AGENTS.md` — current architecture and repository rules.
2. Executable registries and schemas — for example `runtime/activities.py`,
   `prompts/DISCORD.md`, and `scripts/migrate_v51/schema_v51.py`.
3. Living documents in `docs/reference/`.
4. Locked build specs and archived reports for historical reasoning.

The v5.1 specification describes the clean-break migration and the system that
shipped on 2026-07-03/04. Later changes made the awareness loop the sole
proactive owner and moved deterministic recognition/delivery to an explicit
offline comparison seam whose queue is a connection-local TEMP table. Current runtime behavior is therefore defined by
`AGENTS.md`, `engine/tick.py`, and `runtime/activities.py`, even where an older
section of the locked build spec narrates the original seven-step design.
