"""Projection runner base.

A follower that reads the event store's notification log forward from its tracked
position, decodes each notification to a domain event, and dispatches it to a
handler. Projection writes and the tracking update commit together (atomic,
co-located), so a crash never leaves a projection ahead of or behind its position.
Rebuild-from-zero == replay determinism.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("elixir.event_core")


class ProjectionRunner:
    name: str = ""
    # Aggregate this projection consumes (the class name as it appears in the
    # event topic, e.g. "Player"). Required because event class names like
    # "Registered" collide across aggregates in the shared notification log.
    aggregate_name: str | None = None

    def __init__(self, app, conn):
        self.app = app
        self.conn = conn

    @staticmethod
    def _aggregate_of(notification) -> str:
        # topic is "module.path:Aggregate.Event"
        return notification.topic.split(":")[-1].split(".")[0]

    # --- lifecycle hooks (override) ---
    def setup(self) -> None:
        """Create projection tables (idempotent)."""

    def reset(self) -> None:
        """Drop projection tables + tracking for a clean from-zero rebuild."""
        self.conn.execute(
            "DELETE FROM projection_tracking WHERE projection_name=?", (self.name,)
        )
        self.conn.commit()

    def handle(self, event, notification) -> None:
        """Apply one decoded domain event to projection tables. Override."""

    # --- engine ---
    def last_position(self) -> int:
        row = self.conn.execute(
            "SELECT last_global_position FROM projection_tracking WHERE projection_name=?",
            (self.name,),
        ).fetchone()
        return row["last_global_position"] if row else 0

    def _save_position(self, pos: int) -> None:
        self.conn.execute(
            "INSERT INTO projection_tracking(projection_name,last_global_position,updated_at) "
            "VALUES(?,?,?) ON CONFLICT(projection_name) DO UPDATE SET "
            "last_global_position=excluded.last_global_position, updated_at=excluded.updated_at",
            (self.name, pos, datetime.now(timezone.utc).isoformat()),
        )

    def run(self, batch: int = 500) -> int:
        pos = self.last_position()
        handled = 0
        while True:
            notifs = self.app.recorder.select_notifications(start=pos + 1, limit=batch)
            if not notifs:
                break
            for n in notifs:
                if self.aggregate_name is None or self._aggregate_of(n) == self.aggregate_name:
                    try:
                        event = self.app.mapper.to_domain_event(n)
                        self.handle(event, n)
                        handled += 1
                    except Exception:
                        # One malformed event must not abort the whole tick. Skip,
                        # log, and continue — the position still advances so we
                        # don't wedge on it.
                        log.exception(
                            "%s: skipped notification %s (%s)", self.name, n.id, n.topic
                        )
                pos = n.id
            self._save_position(pos)  # same transaction as the handle() writes
            self.conn.commit()
        return handled
