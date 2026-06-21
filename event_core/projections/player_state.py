"""Current-profile projection for Player aggregates.

One row per player with the latest observed scalar profile values. Folds
Registered (creates the row) + ProfileObserved (updates fields). Keyed by
player_tag; correlated to the aggregate by its UUID.
"""
from __future__ import annotations

from event_core.domain.player import PROFILE_SCALAR_FIELDS
from event_core.projections.runner import ProjectionRunner

# Projection columns = aggregate attribute names (the scalar profile fields).
PROFILE_COLUMNS = list(PROFILE_SCALAR_FIELDS.values())


class PlayerCurrentProfile(ProjectionRunner):
    name = "player_current_profile"
    aggregate_name = "Player"

    def setup(self) -> None:
        cols = ",\n            ".join(f"{c} {'TEXT' if c in ('name','role') else 'INTEGER'}" for c in PROFILE_COLUMNS)
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
