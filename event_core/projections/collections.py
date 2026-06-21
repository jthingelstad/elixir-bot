"""Current-collections projection for PlayerCollections aggregates.

One row per player with the latest observed card/support/badge/achievement JSON.
Folds Registered (creates the row) + Card/Badge/AchievementCollectionObserved
(updates the respective column + observed_at). Keyed by player_tag; correlated to
the aggregate by its UUID.
"""
from __future__ import annotations

from event_core.projections.runner import ProjectionRunner


class PlayerCurrentCollections(ProjectionRunner):
    name = "player_current_collections"
    aggregate_name = "PlayerCollections"

    def setup(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_current_collections (
                aggregate_id        TEXT PRIMARY KEY,
                player_tag          TEXT UNIQUE,
                cards_observed_at   TEXT,
                cards_json          TEXT,
                support_cards_json  TEXT,
                badges_observed_at  TEXT,
                badges_json         TEXT,
                achievements_observed_at TEXT,
                achievements_json   TEXT
            )
            """
        )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS player_current_collections")
        self.conn.commit()
        super().reset()
        self.setup()

    def handle(self, event, notification) -> None:
        cls = type(event).__name__
        agg_id = str(event.originator_id)
        if cls == "Registered":
            self.conn.execute(
                "INSERT OR IGNORE INTO player_current_collections(aggregate_id, player_tag) VALUES(?,?)",
                (agg_id, event.player_tag),
            )
        elif cls == "CardCollectionObserved":
            self.conn.execute(
                "UPDATE player_current_collections SET cards_observed_at=?, cards_json=?, "
                "support_cards_json=? WHERE aggregate_id=?",
                (event.observed_at, event.cards_json, event.support_cards_json, agg_id),
            )
        elif cls == "BadgeCollectionObserved":
            self.conn.execute(
                "UPDATE player_current_collections SET badges_observed_at=?, badges_json=? "
                "WHERE aggregate_id=?",
                (event.observed_at, event.badges_json, agg_id),
            )
        elif cls == "AchievementCollectionObserved":
            self.conn.execute(
                "UPDATE player_current_collections SET achievements_observed_at=?, "
                "achievements_json=? WHERE aggregate_id=?",
                (event.observed_at, event.achievements_json, agg_id),
            )
