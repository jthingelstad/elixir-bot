"""Current-profile projection for Player aggregates.

One row per player with the latest observed scalar profile values. Folds
Registered (creates the row) + ProfileObserved (updates fields). Keyed by
player_tag; correlated to the aggregate by its UUID.
"""
from __future__ import annotations

from event_core.domain.player import PROFILE_BADGE_FIELD_TYPES, PROFILE_SCALAR_FIELDS
from event_core.projections.runner import ProjectionRunner

# Projection columns = aggregate attribute names for tracked profile fields.
PROFILE_COLUMN_TYPES = {
    **{c: "INTEGER" for c in PROFILE_SCALAR_FIELDS.values()},
    "name": "TEXT",
    "role": "TEXT",
    **PROFILE_BADGE_FIELD_TYPES,
}
PROFILE_COLUMNS = list(PROFILE_SCALAR_FIELDS.values()) + list(PROFILE_BADGE_FIELD_TYPES)


class PlayerCurrentProfile(ProjectionRunner):
    name = "player_current_profile"
    aggregate_name = "Player"

    def setup(self) -> None:
        cols = ",\n            ".join(f"{c} {PROFILE_COLUMN_TYPES[c]}" for c in PROFILE_COLUMNS)
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS player_current_profile (
                aggregate_id TEXT PRIMARY KEY,
                player_tag   TEXT UNIQUE,
                observed_at  TEXT,
                {cols}
            )
            """
        )
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(player_current_profile)")
        }
        for col in PROFILE_COLUMNS:
            if col not in existing:
                self.conn.execute(
                    f"ALTER TABLE player_current_profile ADD COLUMN {col} {PROFILE_COLUMN_TYPES[col]}"
                )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS player_current_profile")
        self.conn.commit()
        super().reset()
        self.setup()

    def handle(self, event, notification) -> None:
        cls = type(event).__name__
        agg_id = str(event.originator_id)
        if cls == "Registered":
            self.conn.execute(
                "INSERT OR IGNORE INTO player_current_profile(aggregate_id, player_tag) VALUES(?,?)",
                (agg_id, event.player_tag),
            )
        elif cls == "ProfileObserved":
            obs = dict(event.observation)
            sets = ["observed_at=?"]
            vals = [event.observed_at]
            for col in PROFILE_COLUMNS:
                if col in obs:
                    sets.append(f"{col}=?")
                    vals.append(obs[col])
            vals.append(agg_id)
            self.conn.execute(
                f"UPDATE player_current_profile SET {', '.join(sets)} WHERE aggregate_id=?",
                vals,
            )
