"""Clan aggregate — Observed World (clan-LEVEL state).

Records observations of the clan's own top-level state from the /clans payload:
clanScore, clanWarTrophies, requiredTrophies, member count, plus aggregates
computed across memberList (member-trophy total/avg/top, weekly donations total).

This is deliberately NOT the member roster — per-member roster state lives on the
Player aggregate (RosterStateObserved). This aggregate is one timeline of the
clan as an entity.

Keyed deterministically by clan tag (uuid5, its own namespace) so ingest is
idempotent "get-or-create by tag". A `ClanStateObserved` event is emitted only
when the tracked clan-level content changes (content-hash dedup), mirroring the
legacy snapshot "slide". The event stores a UTC `observed_at`; calendar-day
bucketing (clan_daily_metrics.metric_date) happens at projection time only.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from eventsourcing.domain import Aggregate, event

CLAN_NAMESPACE = uuid5(NAMESPACE_URL, "elixir.v5.clan")

# Clan-level observation field names. The directly-observable group maps onto the
# legacy clan_daily_metrics columns 1:1 (for exact parity); the aggregate group
# is computed across memberList in ingest. metric_date / joins/leaves are NOT
# here: metric_date is derived at projection time (TZ rollup), and the
# join/leave/net fields are a separate roster-lifecycle concern (deferred).
CLAN_STATE_FIELDS = (
    "clan_name",
    "member_count",
    "open_slots",
    "clan_score",
    "clan_war_trophies",
    "required_trophies",
    "donations_per_week_requirement",
    "weekly_donations_total",
    "total_member_trophies",
    "avg_member_trophies",
    "top_member_trophies",
)


def canon_tag(tag: str) -> str:
    """Uppercase, '#'-prefixed canonical clan tag."""
    t = (tag or "").strip().upper()
    if not t.startswith("#"):
        t = "#" + t
    return t


def clan_id(tag: str) -> UUID:
    """Deterministic aggregate id from clan tag."""
    return uuid5(CLAN_NAMESPACE, canon_tag(tag))


class Clan(Aggregate):
    @event("Registered")
    def __init__(self, clan_tag: str) -> None:
        self.clan_tag = clan_tag
        # Latest observed clan-level values (attr -> value); folded current state.
        self.state: dict[str, object] = {}
        self.last_state_hash: str | None = None
        self.last_observed_at: str | None = None
        # Roster membership (player_tag -> role); folded for join/leave diffing.
        self.members: dict[str, str] = {}
        self.roster_seen: bool = False

    @classmethod
    def create_id(cls, clan_tag: str) -> UUID:
        return clan_id(clan_tag)

    def observe_state(
        self, observation: dict, observed_at: str, content_hash: str
    ) -> bool:
        """Record a clan-level observation if its content changed. Returns True if
        a ClanStateObserved event was emitted, False if deduped."""
        if content_hash == self.last_state_hash:
            return False
        self._state_observed(observation, observed_at, content_hash)
        return True

    @event("ClanStateObserved")
    def _state_observed(
        self, observation: dict, observed_at: str, content_hash: str
    ) -> None:
        self.state.update(observation)
        self.last_state_hash = content_hash
        self.last_observed_at = observed_at

    # --- roster membership lifecycle (join/leave/role-change) ---
    def observe_roster(self, roster: dict[str, str], observed_at: str) -> int:
        """Diff the observed member set (player_tag -> role) against the folded
        roster and emit MemberJoined/MemberLeft/MemberRoleChanged. The first
        observation establishes a baseline with no events (mirrors legacy
        bootstrap_seed). Returns the number of lifecycle events emitted."""
        if not self.roster_seen:
            self._roster_baseline(roster, observed_at)
            return 0
        prev = self.members
        changes = 0
        for tag, role in roster.items():
            if tag not in prev:
                self._member_joined(tag, role, observed_at)
                changes += 1
            elif prev[tag] != role:
                self._member_role_changed(tag, prev[tag], role, observed_at)
                changes += 1
        for tag in list(prev):
            if tag not in roster:
                self._member_left(tag, observed_at)
                changes += 1
        return changes

    @event("RosterBaseline")
    def _roster_baseline(self, roster: dict, observed_at: str) -> None:
        self.members = dict(roster)
        self.roster_seen = True

    @event("MemberJoined")
    def _member_joined(self, player_tag: str, role: str, observed_at: str) -> None:
        self.members[player_tag] = role

    @event("MemberLeft")
    def _member_left(self, player_tag: str, observed_at: str) -> None:
        self.members.pop(player_tag, None)

    @event("MemberRoleChanged")
    def _member_role_changed(
        self, player_tag: str, old_role: str, new_role: str, observed_at: str
    ) -> None:
        self.members[player_tag] = new_role
