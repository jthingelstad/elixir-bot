"""PlayerCollections aggregate — Observed World.

Records observations of a Clash Royale player's *collections*: their card
collection (cards + support cards), badge collection, and achievement
collection. This is a SEPARATE aggregate from `Player` (its own uuid5 namespace),
so a collection timeline can be replayed and projected independently of the
scalar profile timeline. Keyed deterministically by player tag (uuid5) so ingest
is idempotent "get-or-create by tag".

Each collection has its own content-hash dedup slide, mirroring the Player
profile pattern: an `*Observed` event is emitted only when that collection's
tracked content changes. This mirrors the legacy snapshot slides:
  - cards  -> member_card_collection_snapshots (cards_json + support_cards_json)
  - badges -> player_profile_snapshots.badges_json
  - achievements -> player_profile_snapshots.achievements_json
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from eventsourcing.domain import Aggregate, event

# OWN namespace — deliberately distinct from event_core.domain.player's
# PLAYER_NAMESPACE so PlayerCollections ids never collide with Player ids.
COLLECTIONS_NAMESPACE = uuid5(NAMESPACE_URL, "elixir.v5.player_collections")


def canon_tag(tag: str) -> str:
    """Uppercase, '#'-prefixed canonical player tag."""
    t = (tag or "").strip().upper()
    if not t.startswith("#"):
        t = "#" + t
    return t


def collections_id(tag: str) -> UUID:
    """Deterministic aggregate id from player tag."""
    return uuid5(COLLECTIONS_NAMESPACE, canon_tag(tag))


class PlayerCollections(Aggregate):
    @event("Registered")
    def __init__(self, player_tag: str) -> None:
        self.player_tag = player_tag
        # Latest observed collection JSON blobs + their content hashes.
        self.cards_json: str | None = None
        self.support_cards_json: str | None = None
        self.last_cards_hash: str | None = None
        self.badges_json: str | None = None
        self.last_badges_hash: str | None = None
        self.achievements_json: str | None = None
        self.last_achievements_hash: str | None = None
        self.last_observed_at: str | None = None

    @classmethod
    def create_id(cls, player_tag: str) -> UUID:
        return collections_id(player_tag)

    # --- cards ---
    def observe_cards(
        self, cards_json: str, support_cards_json: str, observed_at: str, content_hash: str
    ) -> bool:
        if content_hash == self.last_cards_hash:
            return False
        self._cards_observed(cards_json, support_cards_json, observed_at, content_hash)
        return True

    @event("CardCollectionObserved")
    def _cards_observed(
        self, cards_json: str, support_cards_json: str, observed_at: str, content_hash: str
    ) -> None:
        self.cards_json = cards_json
        self.support_cards_json = support_cards_json
        self.last_cards_hash = content_hash
        self.last_observed_at = observed_at

    # --- badges ---
    def observe_badges(self, badges_json: str, observed_at: str, content_hash: str) -> bool:
        if content_hash == self.last_badges_hash:
            return False
        self._badges_observed(badges_json, observed_at, content_hash)
        return True

    @event("BadgeCollectionObserved")
    def _badges_observed(self, badges_json: str, observed_at: str, content_hash: str) -> None:
        self.badges_json = badges_json
        self.last_badges_hash = content_hash
        self.last_observed_at = observed_at

    # --- achievements ---
    def observe_achievements(
        self, achievements_json: str, observed_at: str, content_hash: str
    ) -> bool:
        if content_hash == self.last_achievements_hash:
            return False
        self._achievements_observed(achievements_json, observed_at, content_hash)
        return True

    @event("AchievementCollectionObserved")
    def _achievements_observed(
        self, achievements_json: str, observed_at: str, content_hash: str
    ) -> None:
        self.achievements_json = achievements_json
        self.last_achievements_hash = content_hash
        self.last_observed_at = observed_at
