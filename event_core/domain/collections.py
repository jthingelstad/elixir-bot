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

import json
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


def _index_cards(cards_json: str | None) -> dict:
    """Index a stored cards_json blob by card id -> card dict.

    cards_json is the normalized list produced by ingest.collections (so `level`
    is the legacy display level capped at 16, and `evolutionLevel` is the raw
    evolution level). Cards missing an `id` are skipped (cannot be diffed).
    """
    out: dict = {}
    for card in json.loads(cards_json or "[]"):
        if isinstance(card, dict) and card.get("id") is not None:
            out[card["id"]] = card
    return out


def _index_badges(badges_json: str | None) -> dict:
    """Index a stored badges_json blob by badge name -> badge dict."""
    out: dict = {}
    for badge in json.loads(badges_json or "[]"):
        if isinstance(badge, dict) and badge.get("name") is not None:
            out[badge["name"]] = badge
    return out


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
        """Record a card-collection observation if it changed. Returns True if a
        CardCollectionObserved event was emitted, False if deduped.

        Also emits granular durable change events (the Mind's base-event contract)
        by diffing the new card list against the prior folded `cards_json`, matched
        by card id. The coarse CardCollectionObserved remains the source for the
        collections projection; granular events are what detectors (Followers)
        consume. Mirrors the Player.observe_profile granular-diff pattern: granular
        events only after a baseline exists (the first observation emits none, like
        legacy: no previous_card_row -> no card signals).
        """
        if content_hash == self.last_cards_hash:
            return False
        old_cards = _index_cards(self.cards_json)
        if old_cards:  # baseline exists
            tag = self.player_tag
            for card in _index_cards(cards_json).values():
                cid = card["id"]
                prev = old_cards.get(cid)
                new_level = card.get("level")
                new_evo = card.get("evolutionLevel") or 0
                rarity = (str(card.get("rarity") or "").strip().lower() or None)
                name = card.get("name")
                if prev is None:
                    self._card_unlocked(
                        cid, name, rarity, new_level, new_evo, observed_at, tag
                    )
                    continue
                old_level = prev.get("level")
                if (
                    isinstance(new_level, int)
                    and isinstance(old_level, int)
                    and new_level != old_level
                ):
                    self._card_level_changed(
                        cid, name, rarity, old_level, new_level, observed_at, tag
                    )
                old_evo = prev.get("evolutionLevel") or 0
                if new_evo != old_evo:
                    self._card_evolution_changed(
                        cid, name, rarity, old_evo, new_evo, observed_at, tag
                    )
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

    @event("CardUnlocked")
    def _card_unlocked(
        self, card_id, card_name, rarity, new_level, new_evolution_level,
        observed_at: str, player_tag: str,
    ) -> None:
        pass

    @event("CardLevelChanged")
    def _card_level_changed(
        self, card_id, card_name, rarity, old_level, new_level,
        observed_at: str, player_tag: str,
    ) -> None:
        pass

    @event("CardEvolutionChanged")
    def _card_evolution_changed(
        self, card_id, card_name, rarity, old_evolution_level, new_evolution_level,
        observed_at: str, player_tag: str,
    ) -> None:
        pass

    # --- badges ---
    def observe_badges(self, badges_json: str, observed_at: str, content_hash: str) -> bool:
        """Record a badge-collection observation if it changed. Returns True if a
        BadgeCollectionObserved event was emitted, False if deduped.

        Also emits granular durable change events by diffing the new badge list
        against the prior folded `badges_json`, matched by badge name. Granular
        events only after a baseline exists (first observation emits none).
        """
        if content_hash == self.last_badges_hash:
            return False
        old_badges = _index_badges(self.badges_json)
        if old_badges:  # baseline exists
            tag = self.player_tag
            for name, badge in _index_badges(badges_json).items():
                prev = old_badges.get(name)
                new_level = badge.get("level")
                new_progress = badge.get("progress")
                if prev is None:
                    self._badge_earned(
                        name, new_level, new_progress, observed_at, tag
                    )
                    continue
                old_level = prev.get("level")
                old_progress = prev.get("progress")
                if new_level != old_level or new_progress != old_progress:
                    self._badge_level_changed(
                        name, old_level, new_level, old_progress, new_progress,
                        observed_at, tag,
                    )
        self._badges_observed(badges_json, observed_at, content_hash)
        return True

    @event("BadgeCollectionObserved")
    def _badges_observed(self, badges_json: str, observed_at: str, content_hash: str) -> None:
        self.badges_json = badges_json
        self.last_badges_hash = content_hash
        self.last_observed_at = observed_at

    @event("BadgeEarned")
    def _badge_earned(
        self, badge_name, level, progress, observed_at: str, player_tag: str
    ) -> None:
        pass

    @event("BadgeLevelChanged")
    def _badge_level_changed(
        self, badge_name, old_level, new_level, old_progress, new_progress,
        observed_at: str, player_tag: str,
    ) -> None:
        pass

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
