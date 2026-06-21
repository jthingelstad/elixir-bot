"""The Observed World application — the event-sourced write model.

Ingest calls these methods; they are the single code path used by both live
ingest (later) and backfill. Each is idempotent get-or-create by natural key.
"""
from __future__ import annotations

from eventsourcing.application import AggregateNotFoundError, Application

from event_core.domain.player import Player, canon_tag, player_id


class ObservedWorld(Application):
    # Snapshot the event store automatically so long-lived aggregates stay cheap
    # to load during backfill/replay.
    snapshotting_intervals = {Player: 100}

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
