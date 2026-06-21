"""member_current_state projection — latest observed roster state per member.

Folds Registered (creates row) + RosterStateObserved (updates fields). Mirrors the
legacy member_current_state columns sourced from snapshot_members.
"""
from __future__ import annotations

from event_core.domain.player import ROSTER_FIELDS
from event_core.projections.runner import ProjectionRunner

_TEXT = {"role", "arena_name", "arena_raw_name", "last_seen_api"}


class MemberCurrentState(ProjectionRunner):
    name = "member_current_state_proj"
    aggregate_name = "Player"

    def setup(self) -> None:
        cols = ",\n            ".join(
            f"{c} {'TEXT' if c in _TEXT else 'INTEGER'}" for c in ROSTER_FIELDS
        )
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS member_current_state_proj (
                aggregate_id TEXT PRIMARY KEY,
                player_tag   TEXT UNIQUE,
                observed_at  TEXT,
                {cols}
            )
            """
        )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS member_current_state_proj")
        self.conn.commit()
        super().reset()
        self.setup()

    def handle(self, event, notification) -> None:
        cls = type(event).__name__
        agg_id = str(event.originator_id)
        if cls == "Registered":
            self.conn.execute(
                "INSERT OR IGNORE INTO member_current_state_proj(aggregate_id, player_tag) VALUES(?,?)",
                (agg_id, event.player_tag),
            )
        elif cls == "RosterStateObserved":
            obs = dict(event.observation)
            sets = ["observed_at=?"]
            vals = [event.observed_at]
            for col in ROSTER_FIELDS:
                if col in obs:
                    sets.append(f"{col}=?")
                    vals.append(obs[col])
            vals.append(agg_id)
            self.conn.execute(
                f"UPDATE member_current_state_proj SET {', '.join(sets)} WHERE aggregate_id=?",
                vals,
            )
