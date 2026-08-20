"""The awareness loop — Elixir's central deliberative heartbeat.

Builds "the read" (the situation snapshot), hands it to the brain with its full
read + write tool surface, persists the train of thought, delivers the plan's
posts to the member-facing channels, and writes a bot-native diagnostic to the
leader-only #thinking channel. The scoped responder shares its delivery owner
for qualifying events between brain runs.
"""

from __future__ import annotations

from runtime.awareness.diagnostic import build_diagnostic_render
from runtime.awareness.loop import run_awareness_loop
from runtime.awareness.read import build_read
from runtime.awareness.store import (
    ensure_awareness_schema,
    list_recent_thoughts,
    persist_thought,
)

__all__ = [
    "build_read",
    "run_awareness_loop",
    "build_diagnostic_render",
    "ensure_awareness_schema",
    "persist_thought",
    "list_recent_thoughts",
]
