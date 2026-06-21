"""Player aggregate — Observed World.

Records observations of a Clash Royale player's profile. Keyed deterministically
by player tag (uuid5) so ingest is idempotent "get-or-create by tag".

Tiering note (§5.6): a `ProfileObserved` event is emitted only when the tracked
profile content changes (content-hash dedup, mirroring the legacy snapshot
"slide"). Battles are NOT events on this aggregate — they are retention-managed
telemetry. High-frequency churn fields (trophies, donations) ride along inside
`ProfileObserved` for now; splitting them to pure telemetry is a later refinement
that does not change the pipeline this proves.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from eventsourcing.domain import Aggregate, event

PLAYER_NAMESPACE = uuid5(NAMESPACE_URL, "elixir.v5.player")

# CR /players payload key -> aggregate attribute name. Scalar profile fields that
# map 1:1 onto the legacy player_profile_snapshots columns (for exact parity).
PROFILE_SCALAR_FIELDS: dict[str, str] = {
    "name": "name",
    "role": "role",
    "expLevel": "exp_level",
    "trophies": "trophies",
    "bestTrophies": "best_trophies",
    "wins": "wins",
    "losses": "losses",
    "battleCount": "battle_count",
    "threeCrownWins": "three_crown_wins",
    "challengeCardsWon": "challenge_cards_won",
    "challengeMaxWins": "challenge_max_wins",
    "tournamentCardsWon": "tournament_cards_won",
    "tournamentBattleCount": "tournament_battle_count",
    "donations": "donations",
    "donationsReceived": "donations_received",
    "totalDonations": "total_donations",
    "warDayWins": "war_day_wins",
    "clanCardsCollected": "clan_cards_collected",
    "starPoints": "star_points",
    "expPoints": "exp_points",
    "totalExpPoints": "total_exp_points",
    "legacyTrophyRoadHighScore": "legacy_trophy_road_high_score",
}


def canon_tag(tag: str) -> str:
    """Uppercase, '#'-prefixed canonical player tag."""
    t = (tag or "").strip().upper()
    if not t.startswith("#"):
        t = "#" + t
    return t


def player_id(tag: str) -> UUID:
    """Deterministic aggregate id from player tag."""
    return uuid5(PLAYER_NAMESPACE, canon_tag(tag))


class Player(Aggregate):
    @event("Registered")
    def __init__(self, player_tag: str) -> None:
        self.player_tag = player_tag
        # Latest observed profile values (attr -> value); the folded current state.
        self.profile: dict[str, object] = {}
        self.last_profile_hash: str | None = None
        self.last_observed_at: str | None = None

    @classmethod
    def create_id(cls, player_tag: str) -> UUID:
        return player_id(player_tag)

    def observe_profile(
        self, observation: dict, observed_at: str, content_hash: str
    ) -> bool:
        """Record a profile observation if its content changed. Returns True if a
        ProfileObserved event was emitted, False if deduped."""
        if content_hash == self.last_profile_hash:
            return False
        self._profile_observed(observation, observed_at, content_hash)
        return True

    @event("ProfileObserved")
    def _profile_observed(
        self, observation: dict, observed_at: str, content_hash: str
    ) -> None:
        self.profile.update(observation)
        self.last_profile_hash = content_hash
        self.last_observed_at = observed_at
