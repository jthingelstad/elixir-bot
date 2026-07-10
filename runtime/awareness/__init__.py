"""The awareness loop — Elixir's central deliberative heartbeat.

Shadow-mode v1: builds "the read" (the situation snapshot), hands it to the
benched brain with write tools disabled, persists the train of thought, and
writes a bot-native diagnostic to the leader-only #thinking channel. It posts
NOTHING to any member-facing channel.
"""

from __future__ import annotations

from runtime.awareness.loop import run_awareness_loop
from runtime.awareness.read import build_read
from runtime.awareness.shadow import build_shadow_render
from runtime.awareness.store import (
    ensure_awareness_schema,
    list_recent_thoughts,
    open_watches,
    persist_thought,
)

__all__ = [
    "build_read",
    "run_awareness_loop",
    "build_shadow_render",
    "ensure_awareness_schema",
    "persist_thought",
    "list_recent_thoughts",
    "open_watches",
]
