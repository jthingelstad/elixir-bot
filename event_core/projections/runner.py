"""Projection runner base.

A follower that reads the event store's notification log forward from its tracked
position, decodes each notification to a domain event, and dispatches it to a
handler. Projection writes and the tracking update commit together (atomic,
co-located), so a crash never leaves a projection ahead of or behind its position.
Rebuild-from-zero == replay determinism.
"""
from __future__ import annotations

from datetime import datetime, timezone


class ProjectionRunner:
    name: str = ""

    def __init__(self, app, conn):
        self.app = app
        self.conn = conn

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
        total = 0
        while True:
            notifs = self.app.recorder.select_notifications(start=pos + 1, limit=batch)
            if not notifs:
                break
            for n in notifs:
                event = self.app.mapper.to_domain_event(n)
                self.handle(event, n)
                pos = n.id
            self._save_position(pos)  # same transaction as the handle() writes
            self.conn.commit()
            total += len(notifs)
        return total
