"""Roster lifecycle projection — member join/leave/role-change events.

A queryable ledger of clan membership transitions, derived from Clan aggregate
events. Feeds clan join/leave metrics and roster-history queries.
"""
from __future__ import annotations

from event_core.projections.runner import ProjectionRunner

_HANDLED = {"MemberJoined", "MemberLeft", "MemberRoleChanged"}


class RosterLifecycle(ProjectionRunner):
    name = "roster_lifecycle_proj"
    aggregate_name = "Clan"

    def setup(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS roster_lifecycle (
                event_type  TEXT,
                player_tag  TEXT,
                role        TEXT,
                old_role    TEXT,
                occurred_at TEXT,
                UNIQUE(event_type, player_tag, occurred_at)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_roster_lifecycle_player ON roster_lifecycle(player_tag, occurred_at)"
        )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS roster_lifecycle")
        self.conn.commit()
        super().reset()
        self.setup()

    def handle(self, event, notification) -> None:
        cls = type(event).__name__
        if cls not in _HANDLED:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO roster_lifecycle(event_type,player_tag,role,old_role,occurred_at) VALUES(?,?,?,?,?)",
            (
                cls,
                event.player_tag,
                getattr(event, "role", None) if cls != "MemberRoleChanged" else getattr(event, "new_role", None),
                getattr(event, "old_role", None),
                event.observed_at,
            ),
        )
