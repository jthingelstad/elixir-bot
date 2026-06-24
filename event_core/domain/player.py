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

# Badge-backed profile facts that are stable player metrics rather than
# achievements. These ride the Player profile observation so read-model parity
# does not require callers to reconstruct profile state from raw badge JSON.
PROFILE_BADGE_FIELD_TYPES: dict[str, str] = {
    "cr_account_age_days": "INTEGER",
    "cr_account_age_years": "INTEGER",
    "cr_collection_level": "INTEGER",
    "cr_collection_level_badge_tier": "INTEGER",
    "cr_collection_level_badge_max_tier": "INTEGER",
    "cr_clan_war_wins": "INTEGER",
    "cr_battle_wins": "INTEGER",
    "cr_clan_wars_veteran": "INTEGER",
    "cr_clan_wars_veteran_badge_tier": "INTEGER",
    "cr_clan_wars_veteran_badge_max_tier": "INTEGER",
    "cr_clan_donations": "INTEGER",
    "cr_banner_count": "INTEGER",
    "cr_emote_count": "INTEGER",
}

# Roster-state fields observed via the /clans memberList entry, mapped to the
# legacy member_current_state columns. Attached to the Player aggregate so a
# player's profile and roster observations share one timeline. (Clan-level
# membership lifecycle — join/left — is a separate Clan-aggregate concern.)
ROSTER_FIELDS = (
    "role",
    "exp_level",
    "trophies",
    "best_trophies",
    "clan_rank",
    "donations_week",
    "donations_received_week",
    "arena_id",
    "arena_name",
    "arena_raw_name",
    "last_seen_api",
)


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
        # Latest observed roster state (from the clan endpoint).
        self.roster: dict[str, object] = {}
        self.last_roster_hash: str | None = None

    @classmethod
    def create_id(cls, player_tag: str) -> UUID:
        return player_id(player_tag)

    def observe_profile(
        self, observation: dict, observed_at: str, content_hash: str
    ) -> bool:
        """Record a profile observation if its content changed. Returns True if a
        ProfileObserved event was emitted, False if deduped.

        Also emits granular durable change events (the Mind's base-event contract)
        for milestone-worthy fields, by diffing against current folded state. The
        coarse ProfileObserved remains the source for the current-profile
        projection; granular events are what detectors (Followers) consume.
        High-frequency churn (trophies/donations) stays in ProfileObserved only —
        it is telemetry, not durable per §5.6. Career wins also ride in the profile,
        but we emit a granular change event so the Mind can catch 1,000-win
        boundaries without making every profile observation a Discord event.
        """
        if content_hash == self.last_profile_hash:
            return False
        old = self.profile
        # Granular events only after a baseline exists (mirrors legacy: first
        # snapshot emits no signals).
        if old:
            tag = self.player_tag
            new_name = observation.get("name")
            if new_name is not None and new_name != old.get("name"):
                self._name_changed(new_name, old.get("name"), observed_at, tag)
            new_level = observation.get("exp_level")
            if new_level is not None and new_level != old.get("exp_level"):
                self._level_changed(new_level, old.get("exp_level"), observed_at, tag)
            new_best = observation.get("best_trophies")
            if new_best is not None and new_best != old.get("best_trophies"):
                self._best_trophies_changed(new_best, old.get("best_trophies"), observed_at, tag)
            new_wins = observation.get("wins")
            if new_wins is not None and new_wins != old.get("wins"):
                self._wins_changed(new_wins, old.get("wins"), observed_at, tag)
            # Path-of-Legend: emit on any league or rank movement (detectors decide
            # promotion vs demotion vs rank improvement). Only when PoL is present.
            if "pol_league_number" in observation or "pol_rank" in observation:
                old_league = old.get("pol_league_number")
                new_league = observation.get("pol_league_number")
                old_rank = old.get("pol_rank")
                new_rank = observation.get("pol_rank")
                if new_league != old_league or new_rank != old_rank:
                    self._path_of_legend_changed(
                        new_league, old_league, new_rank, old_rank,
                        observation.get("pol_trophies"), observed_at, tag,
                    )
        self._profile_observed(observation, observed_at, content_hash)
        return True

    @event("ProfileObserved")
    def _profile_observed(
        self, observation: dict, observed_at: str, content_hash: str
    ) -> None:
        self.profile.update(observation)
        self.last_profile_hash = content_hash
        self.last_observed_at = observed_at

    @event("PlayerNameChanged")
    def _name_changed(self, new_name: str, old_name, observed_at: str, player_tag: str) -> None:
        self.profile["name"] = new_name

    @event("PlayerLevelChanged")
    def _level_changed(self, new_level: int, old_level, observed_at: str, player_tag: str) -> None:
        self.profile["exp_level"] = new_level

    @event("BestTrophiesChanged")
    def _best_trophies_changed(self, new_best: int, old_best, observed_at: str, player_tag: str) -> None:
        self.profile["best_trophies"] = new_best

    @event("PlayerWinsChanged")
    def _wins_changed(self, new_wins: int, old_wins, observed_at: str, player_tag: str) -> None:
        self.profile["wins"] = new_wins

    @event("PathOfLegendChanged")
    def _path_of_legend_changed(
        self, new_league, old_league, new_rank, old_rank,
        new_trophies, observed_at: str, player_tag: str,
    ) -> None:
        self.profile["pol_league_number"] = new_league
        self.profile["pol_rank"] = new_rank
        self.profile["pol_trophies"] = new_trophies

    def observe_roster_state(
        self, observation: dict, observed_at: str, content_hash: str
    ) -> bool:
        """Record a roster-state observation (from /clans) if it changed."""
        if content_hash == self.last_roster_hash:
            return False
        self._roster_observed(observation, observed_at, content_hash, self.player_tag)
        return True

    @event("RosterStateObserved")
    def _roster_observed(
        self, observation: dict, observed_at: str, content_hash: str, player_tag: str
    ) -> None:
        self.roster.update(observation)
        self.last_roster_hash = content_hash
        self.last_observed_at = observed_at
