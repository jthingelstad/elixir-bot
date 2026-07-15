"""Admission boundary for Clash Royale API observations.

Raw responses remain byte-true in ``raw_api_payloads``.  This module answers a
different question: is a decoded response trustworthy enough to mutate the
engine's durable state?  Callers must admit an observation before advancing
poll freshness, ingesting battles, diffing baselines, or refreshing
projections.

The contracts are intentionally small and endpoint-specific.  They validate
stable identity and the fields whose absence would turn a transport/schema
failure into a plausible-looking empty state.  Optional CR fields remain
optional so additive API evolution does not stop the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from engine.db import payload_hash
from engine.normalize import canon_tag, canonical_utc_timestamp, parse_cr_time

ObservationEndpoint: TypeAlias = Literal[
    "clan",
    "currentriverrace",
    "player",
    "player_battlelog",
]
ObservationPayload: TypeAlias = dict | list[dict]

_RACE_PERIOD_TYPES = {"training", "warDay", "colosseum"}
_CLAN_ROLES = {"member", "elder", "coLeader", "leader"}


@dataclass(frozen=True)
class Admission:
    """Result of validating one decoded endpoint response."""

    endpoint: str
    entity_key: str
    errors: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.errors

    @property
    def transport_failure(self) -> bool:
        """No decoded response exists; transport telemetry owns the detail."""
        return self.errors == ("payload:missing",)


@dataclass(frozen=True)
class Observation:
    """One admitted, canonically enveloped CR API observation.

    Endpoint payloads intentionally remain byte-faithful dictionaries/lists;
    their domain projections are built by :mod:`engine.materialize`.  The
    envelope removes the representational ambiguity that previously let each
    caller choose its own tag, timestamp, source, and payload identity.
    """

    endpoint: ObservationEndpoint
    entity_key: str
    observed_at: str
    payload_hash: str
    payload: ObservationPayload
    source: str


def observe(
    endpoint: ObservationEndpoint,
    entity_key: str,
    payload,
    observed_at,
    *,
    source: str,
) -> tuple[Admission, Observation | None]:
    """Admit a response and, on success, return its canonical envelope."""
    decision = admit(endpoint, entity_key, payload)
    if not decision.accepted:
        return decision, None
    canonical_at = canonical_utc_timestamp(observed_at)
    if canonical_at is None:
        invalid = Admission(endpoint, decision.entity_key, ("observed_at:invalid",))
        return invalid, None
    return decision, Observation(
        endpoint=endpoint,
        entity_key=decision.entity_key,
        observed_at=canonical_at,
        payload_hash=payload_hash(payload),
        payload=payload,
        source=str(source or "unknown"),
    )


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _object(payload, errors: list[str]) -> bool:
    if payload is None:
        errors.append("payload:missing")
        return False
    if not isinstance(payload, dict):
        errors.append("payload:not_object")
        return False
    return True


def _identity(actual, expected, path: str, errors: list[str]) -> None:
    actual_tag = canon_tag(actual)
    expected_tag = canon_tag(expected)
    if not actual_tag:
        errors.append(f"{path}:missing")
    elif not expected_tag or actual_tag != expected_tag:
        errors.append(f"{path}:mismatch")


def _nonempty_string(value, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}:not_nonempty_string")


def _integer(value, path: str, errors: list[str]) -> None:
    if not _is_int(value):
        errors.append(f"{path}:not_int")


def _nonnegative_integer(value, path: str, errors: list[str]) -> None:
    if not _is_int(value):
        errors.append(f"{path}:not_int")
    elif value < 0:
        errors.append(f"{path}:negative")


def _admit_clan(expected_tag: str, payload, errors: list[str]) -> None:
    if not _object(payload, errors):
        return
    _identity(payload.get("tag"), expected_tag, "tag", errors)
    _nonempty_string(payload.get("name"), "name", errors)
    _integer(payload.get("clanScore"), "clanScore", errors)
    _integer(payload.get("clanWarTrophies"), "clanWarTrophies", errors)

    members = payload.get("memberList")
    if not isinstance(members, list) or not members:
        errors.append("memberList:not_nonempty_list")
        return
    declared_members = payload.get("members")
    if declared_members is not None:
        if not _is_int(declared_members):
            errors.append("members:not_int")
        elif declared_members != len(members):
            errors.append("members:count_mismatch")
    seen: set[str] = set()
    for index, member in enumerate(members):
        prefix = f"memberList[{index}]"
        if not isinstance(member, dict):
            errors.append(f"{prefix}:not_object")
            continue
        tag = canon_tag(member.get("tag"))
        if not tag:
            errors.append(f"{prefix}.tag:missing")
        elif tag in seen:
            errors.append(f"{prefix}.tag:duplicate")
        else:
            seen.add(tag)
        _nonempty_string(member.get("name"), f"{prefix}.name", errors)
        role = member.get("role")
        if role not in _CLAN_ROLES:
            errors.append(f"{prefix}.role:unknown")
        for field in (
            "trophies",
            "donations",
            "donationsReceived",
            "expLevel",
            "clanRank",
            "previousClanRank",
        ):
            _integer(member.get(field), f"{prefix}.{field}", errors)


def _admit_race(expected_tag: str, payload, errors: list[str]) -> None:
    if not _object(payload, errors):
        return
    _nonempty_string(payload.get("state"), "state", errors)
    section_index = payload.get("sectionIndex")
    period_index = payload.get("periodIndex")
    _nonnegative_integer(section_index, "sectionIndex", errors)
    _nonnegative_integer(period_index, "periodIndex", errors)
    if _is_int(section_index) and _is_int(period_index):
        if period_index // 7 != section_index:
            errors.append("periodIndex:section_mismatch")
    if payload.get("periodType") not in _RACE_PERIOD_TYPES:
        errors.append("periodType:unknown")

    clan = payload.get("clan")
    if not isinstance(clan, dict):
        errors.append("clan:not_object")
    else:
        _identity(clan.get("tag"), expected_tag, "clan.tag", errors)
        _nonempty_string(clan.get("name"), "clan.name", errors)
        _integer(clan.get("fame"), "clan.fame", errors)
        participants = clan.get("participants")
        if not isinstance(participants, list):
            errors.append("clan.participants:not_list")
        else:
            participant_tags: set[str] = set()
            for index, participant in enumerate(participants):
                prefix = f"clan.participants[{index}]"
                if not isinstance(participant, dict):
                    errors.append(f"{prefix}:not_object")
                    continue
                participant_tag = canon_tag(participant.get("tag"))
                if not participant_tag:
                    errors.append(f"{prefix}.tag:missing")
                elif participant_tag in participant_tags:
                    errors.append(f"{prefix}.tag:duplicate")
                else:
                    participant_tags.add(participant_tag)
                _nonempty_string(participant.get("name"), f"{prefix}.name", errors)
                for field in (
                    "fame",
                    "repairPoints",
                    "boatAttacks",
                    "decksUsed",
                    "decksUsedToday",
                ):
                    _integer(participant.get(field), f"{prefix}.{field}", errors)

    clans = payload.get("clans")
    if not isinstance(clans, list) or not clans:
        errors.append("clans:not_nonempty_list")
    else:
        clan_tags: set[str] = set()
        for index, item in enumerate(clans):
            prefix = f"clans[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix}:not_object")
                continue
            tag = canon_tag(item.get("tag"))
            if not tag:
                errors.append(f"{prefix}.tag:missing")
            elif tag in clan_tags:
                errors.append(f"{prefix}.tag:duplicate")
            else:
                clan_tags.add(tag)
            _nonempty_string(item.get("name"), f"{prefix}.name", errors)
            for field in ("fame", "periodPoints", "clanScore"):
                _integer(item.get(field), f"{prefix}.{field}", errors)
        if canon_tag(expected_tag) not in clan_tags:
            errors.append("clans:expected_clan_missing")


def _admit_player(expected_tag: str, payload, errors: list[str]) -> None:
    if not _object(payload, errors):
        return
    _identity(payload.get("tag"), expected_tag, "tag", errors)
    _nonempty_string(payload.get("name"), "name", errors)
    for field in ("expLevel", "wins", "bestTrophies", "trophies"):
        _integer(payload.get(field), field, errors)

    cards = payload.get("cards")
    if not isinstance(cards, list) or not cards:
        errors.append("cards:not_nonempty_list")
    else:
        seen: set[int] = set()
        for index, card in enumerate(cards):
            path = f"cards[{index}]"
            if not isinstance(card, dict):
                errors.append(f"{path}:not_object")
                continue
            card_id = card.get("id")
            if not _is_int(card_id):
                errors.append(f"{path}.id:not_int")
            elif card_id in seen:
                errors.append(f"{path}.id:duplicate")
            else:
                seen.add(card_id)
            _nonempty_string(card.get("name"), f"{path}.name", errors)
            _nonempty_string(card.get("rarity"), f"{path}.rarity", errors)
            _integer(card.get("level"), f"{path}.level", errors)
            _integer(card.get("maxLevel"), f"{path}.maxLevel", errors)

    badges = payload.get("badges")
    if not isinstance(badges, list):
        errors.append("badges:not_list")
    else:
        badge_names: set[str] = set()
        for index, badge in enumerate(badges):
            path = f"badges[{index}]"
            if not isinstance(badge, dict):
                errors.append(f"{path}:not_object")
                continue
            badge_name = badge.get("name")
            _nonempty_string(badge_name, f"{path}.name", errors)
            if isinstance(badge_name, str) and badge_name:
                if badge_name in badge_names:
                    errors.append(f"{path}.name:duplicate")
                else:
                    badge_names.add(badge_name)
            _integer(badge.get("progress"), f"{path}.progress", errors)
            if "level" in badge:
                _integer(badge.get("level"), f"{path}.level", errors)

    arena = payload.get("arena")
    if not isinstance(arena, dict):
        errors.append("arena:not_object")
    else:
        _integer(arena.get("id"), "arena.id", errors)
        _nonempty_string(arena.get("name"), "arena.name", errors)

    for field in (
        "currentPathOfLegendSeasonResult",
        "lastPathOfLegendSeasonResult",
        "bestPathOfLegendSeasonResult",
    ):
        if not isinstance(payload.get(field), dict):
            errors.append(f"{field}:not_object")


def _admit_battlelog(expected_tag: str, payload, errors: list[str]) -> None:
    if payload is None:
        errors.append("payload:missing")
        return
    if not isinstance(payload, list):
        errors.append("payload:not_list")
        return
    # An empty battlelog is a successful observation, not a failed fetch.
    for index, battle in enumerate(payload):
        prefix = f"battles[{index}]"
        if not isinstance(battle, dict):
            errors.append(f"{prefix}:not_object")
            continue
        if parse_cr_time(battle.get("battleTime")) is None:
            errors.append(f"{prefix}.battleTime:invalid")
        game_mode = battle.get("gameMode")
        if not isinstance(game_mode, dict) or not _is_int(game_mode.get("id")):
            errors.append(f"{prefix}.gameMode:invalid")

        for side in ("team", "opponent"):
            players = battle.get(side)
            if not isinstance(players, list) or not players:
                errors.append(f"{prefix}.{side}:not_nonempty_list")
                continue
            for player_index, player in enumerate(players):
                path = f"{prefix}.{side}[{player_index}]"
                if not isinstance(player, dict):
                    errors.append(f"{path}:not_object")
                # Boat/PvE opponents may legitimately have no player tag;
                # ingest uses a stable empty sentinel for that identity.  Team
                # tags are required because the subject match depends on them.
                elif side == "team" and not canon_tag(player.get("tag")):
                    errors.append(f"{path}.tag:missing")

        team = battle.get("team")
        if isinstance(team, list) and canon_tag(expected_tag) not in {
            canon_tag(player.get("tag")) for player in team if isinstance(player, dict)
        }:
            errors.append(f"{prefix}.team:subject_missing")


def admit(endpoint: str, entity_key: str, payload) -> Admission:
    """Validate a decoded CR response for its intended endpoint and entity.

    Unknown endpoints are rejected rather than silently admitted; callers that
    do not materialize an endpoint should not call the admission boundary.
    """
    errors: list[str] = []
    if endpoint == "clan":
        _admit_clan(entity_key, payload, errors)
    elif endpoint == "currentriverrace":
        _admit_race(entity_key, payload, errors)
    elif endpoint == "player":
        _admit_player(entity_key, payload, errors)
    elif endpoint == "player_battlelog":
        _admit_battlelog(entity_key, payload, errors)
    else:
        errors.append("endpoint:unsupported")
    return Admission(endpoint, canon_tag(entity_key), tuple(errors))


__all__ = [
    "Admission",
    "Observation",
    "ObservationEndpoint",
    "ObservationPayload",
    "admit",
    "observe",
]
