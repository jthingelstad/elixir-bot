"""Agent read side — read-only tools over projections (§8.1).

The agent starts from a pre-distilled trigger (a detection / communication intent)
and drills into telemetry on demand via these tools; it never queries the opaque
event store. Scope-enforced; public callers cannot read leadership-scoped rows.
"""
