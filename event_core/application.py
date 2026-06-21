"""The Observed World application — the event-sourced write model.

Ingest calls these methods; they are the single code path used by both live
ingest (later) and backfill. Each is idempotent get-or-create by natural key.
"""
from __future__ import annotations

from eventsourcing.application import AggregateNotFoundError, Application

from event_core.domain.clan import Clan
from event_core.domain.clan import canon_tag as clan_canon_tag
from event_core.domain.clan import clan_id
from event_core.domain.collections import PlayerCollections, collections_id
from event_core.domain.player import Player, canon_tag, player_id
from event_core.domain.riverrace import RiverRace


class ObservedWorld(Application):
    # Snapshot the event store automatically so long-lived aggregates stay cheap
    # to load during backfill/replay.
    snapshotting_intervals = {
        Player: 100,
        PlayerCollections: 100,
        Clan: 100,
        RiverRace: 100,
    }

    def _get_or_create_player(self, tag: str) -> Player:
        try:
            return self.repository.get(player_id(tag))
        except AggregateNotFoundError:
            return Player(player_tag=tag)

    def observe_player_profile(
        self,
        player_tag: str,
        observation: dict,
        observed_at: str,
        content_hash: str,
    ) -> bool:
        """Record a player profile observation. Returns True if it changed state."""
        tag = canon_tag(player_tag)
        player = self._get_or_create_player(tag)
        changed = player.observe_profile(observation, observed_at, content_hash)
        if changed:
            self.save(player)
        return changed

    def observe_member_roster(
        self,
        player_tag: str,
        observation: dict,
        observed_at: str,
        content_hash: str,
    ) -> bool:
        """Record a member's roster-state observation. Returns True if changed."""
        tag = canon_tag(player_tag)
        player = self._get_or_create_player(tag)
        changed = player.observe_roster_state(observation, observed_at, content_hash)
        if changed:
            self.save(player)
        return changed

    # --- PlayerCollections (cards / badges / achievements) ---
    def _get_or_create_collections(self, tag: str) -> PlayerCollections:
        try:
            return self.repository.get(collections_id(tag))
        except AggregateNotFoundError:
            return PlayerCollections(player_tag=tag)

    def observe_player_collections(
        self,
        player_tag: str,
        *,
        cards_json: str,
        support_cards_json: str,
        cards_hash: str,
        badges_json: str,
        badges_hash: str,
        achievements_json: str,
        achievements_hash: str,
        observed_at: str,
    ) -> dict:
        tag = canon_tag(player_tag)
        coll = self._get_or_create_collections(tag)
        changed = {
            "cards": coll.observe_cards(cards_json, support_cards_json, observed_at, cards_hash),
            "badges": coll.observe_badges(badges_json, observed_at, badges_hash),
            "achievements": coll.observe_achievements(achievements_json, observed_at, achievements_hash),
        }
        if any(changed.values()):
            self.save(coll)
        return changed

    # --- Clan (clan-level state) ---
    def _get_or_create_clan(self, tag: str) -> Clan:
        try:
            return self.repository.get(clan_id(tag))
        except AggregateNotFoundError:
            return Clan(clan_tag=tag)

    def observe_clan_state(
        self, clan_tag: str, observation: dict, observed_at: str, content_hash: str
    ) -> bool:
        tag = clan_canon_tag(clan_tag)
        clan = self._get_or_create_clan(tag)
        changed = clan.observe_state(observation, observed_at, content_hash)
        if changed:
            self.save(clan)
        return changed

    def observe_clan_roster(
        self, clan_tag: str, roster: dict, observed_at: str
    ) -> int:
        """Diff the clan roster (player_tag -> role) -> join/leave/role events."""
        tag = clan_canon_tag(clan_tag)
        clan = self._get_or_create_clan(tag)
        changes = clan.observe_roster(roster, observed_at)
        self.save(clan)  # persists baseline or lifecycle events; no-op if unchanged
        return changes
