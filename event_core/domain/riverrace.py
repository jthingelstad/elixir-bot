"""RiverRace (clan war) aggregate — Observed World.

Records observations of a clan's River Race. Keyed deterministically by the
natural war key (clan_tag, seasonId, sectionIndex) via uuid5, so ingest is
idempotent "get-or-create by war".

Two observation streams land on this aggregate:

- `CurrentStateObserved` — the live `currentriverrace` clan-level summary
  (war_state, fame, repair, period points, clan score). Content-hash deduped to
  mirror the legacy `war_current_state` "slide": repeated identical polls only
  refresh `observed_at`; a new event is emitted whenever the tracked summary
  changes. The dedup is *within this aggregate* (per war), which is finer-grained
  than the legacy single global "latest" slide — see the parity note. For
  reproducing the legacy table's latest-per-clan row this is equivalent.

- `LogStandingObserved` — a finalized race standing from the `riverracelog`
  (`clan_war_log`), carrying the clan summary + per-participant fame/decks. This
  is the source for `war_participation`. Content-hash deduped on the standing.

The live `currentriverrace` payload frequently omits `seasonId`; legacy infers it
from the race log. To keep the aggregate self-contained and deterministic we key
the *live* observations by the inferred season id supplied by the ingest layer
(falling back to a sentinel when unknown), matching legacy's inference path.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from eventsourcing.domain import Aggregate, event

RIVERRACE_NAMESPACE = uuid5(NAMESPACE_URL, "elixir.v5.riverrace")

# Live clan-summary fields tracked on war_current_state (CR key -> attr).
CURRENT_STATE_FIELDS: dict[str, str] = {
    "war_state": "war_state",
    "clan_tag": "clan_tag",
    "clan_name": "clan_name",
    "fame": "fame",
    "repair_points": "repair_points",
    "period_points": "period_points",
    "clan_score": "clan_score",
    "section_index": "section_index",
    "period_index": "period_index",
    "period_type": "period_type",
}

# Per-participant fields tracked on war_participation (attr names == columns).
PARTICIPANT_FIELDS = (
    "player_tag",
    "player_name",
    "fame",
    "repair_points",
    "boat_attacks",
    "decks_used",
    "decks_used_today",
)

# Race-summary fields tracked on war_races (attr names == columns).
RACE_SUMMARY_FIELDS = (
    "created_date",
    "our_rank",
    "trophy_change",
    "our_fame",
    "our_clan_score",
    "total_clans",
    "finish_time",
)

SEASON_UNKNOWN = -1


def canon_tag(tag: str) -> str:
    """Uppercase, '#'-prefixed canonical clan tag."""
    t = (tag or "").strip().upper()
    if not t.startswith("#"):
        t = "#" + t
    return t


def tag_key(tag: str) -> str:
    """Canonical tag without the leading '#'."""
    return canon_tag(tag).lstrip("#")


def riverrace_id(clan_tag: str, season_id: int | None, section_index: int | None) -> UUID:
    """Deterministic aggregate id from the natural war key."""
    season = SEASON_UNKNOWN if season_id is None else int(season_id)
    section = -1 if section_index is None else int(section_index)
    name = f"{canon_tag(clan_tag)}|{season}|{section}"
    return uuid5(RIVERRACE_NAMESPACE, name)


class RiverRace(Aggregate):
    @event("Registered")
    def __init__(self, clan_tag: str, season_id: int, section_index: int) -> None:
        self.clan_tag = clan_tag
        self.season_id = season_id
        self.section_index = section_index
        # Latest observed live clan-summary state (attr -> value).
        self.current_state: dict[str, object] = {}
        self.last_state_hash: str | None = None
        self.last_observed_at: str | None = None
        # Finalized race-log summary (set once the race is in the log).
        self.race_summary: dict[str, object] = {}
        self.last_log_hash: str | None = None
        # Per-participant finalized standing (player_tag -> field dict).
        self.participants: dict[str, dict] = {}

    @classmethod
    def create_id(cls, clan_tag: str, season_id: int, section_index: int) -> UUID:
        return riverrace_id(clan_tag, season_id, section_index)

    # --- live currentriverrace state ---
    def observe_current_state(
        self, observation: dict, observed_at: str, content_hash: str
    ) -> bool:
        """Record a live war-state observation if its tracked content changed."""
        if content_hash == self.last_state_hash:
            return False
        self._current_state_observed(observation, observed_at, content_hash)
        return True

    @event("CurrentStateObserved")
    def _current_state_observed(
        self, observation: dict, observed_at: str, content_hash: str
    ) -> None:
        self.current_state.update(observation)
        self.last_state_hash = content_hash
        self.last_observed_at = observed_at

    # --- finalized riverracelog standing ---
    def observe_log_standing(
        self,
        race_summary: dict,
        participants: list[dict],
        observed_at: str,
        content_hash: str,
    ) -> bool:
        """Record a finalized race-log standing if it changed."""
        if content_hash == self.last_log_hash:
            return False
        self._log_standing_observed(race_summary, participants, observed_at, content_hash)
        return True

    @event("LogStandingObserved")
    def _log_standing_observed(
        self,
        race_summary: dict,
        participants: list[dict],
        observed_at: str,
        content_hash: str,
    ) -> None:
        self.race_summary.update(race_summary)
        for p in participants:
            self.participants[tag_key(p["player_tag"])] = dict(p)
        self.last_log_hash = content_hash
        self.last_observed_at = observed_at
