# v5.1 Specification Status

This directory is the locked design record for the 2026-07-03/04 clean break.
It explains the migration and original decisions; it is not a verbatim current
runtime manual.

The most important post-cut change is that the unified awareness loop is now
the sole proactive owner. The deterministic recognition/delivery pipeline is
an explicit offline comparison seam, and its queue is created as a
connection-local TEMP table only for those rehearsals. The operational schema
therefore has no `communication_intents` or `editor_verdicts` table.

For current behavior, read in this order:

1. [`AGENTS.md`](../../../AGENTS.md)
2. [`current-architecture.md`](../current-architecture.md)
3. Executable registries and schema (`runtime/activities.py`,
   `scripts/migrate_v51/schema_v51.py`, `db/schema.py`)
4. The topic document in this directory for original rationale
