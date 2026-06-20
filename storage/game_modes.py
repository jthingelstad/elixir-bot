"""Canonical Clash Royale game-mode classification helpers.

The public API exposes a mix of battle ``type`` values, ``gameMode`` ids,
deck-selection hints, event tags, tournament tags, and profile-progress keys.
This module keeps Elixir's runtime classification in one place so storage,
tools, and prompts do not each invent their own mode taxonomy.
"""

from __future__ import annotations

from typing import Optional

MODE_GROUPS = (
    "ladder",
    "ranked",
    "war",
    "special_event",
    "tournament",
    "two_v_two",
    "friendly",
    "side_mode",
    "other",
)

MODE_GROUP_LABELS = {
    "ladder": "Trophy Road",
    "ranked": "Ranked",
    "war": "River Race",
    "special_event": "Events",
    "tournament": "Tournaments",
    "two_v_two": "2v2",
    "friendly": "Friendly",
    "side_mode": "Side Modes",
    "other": "Other",
}

SPECIAL_EVENT_BADGE_CONTEXTS = {
    "AnarchyLeagueCompletion": {
        "event_name": "Anarchy League",
        "badge_label": "Anarchy League Completion",
        "game_mode_id": 72000501,
        "game_mode_name": "All_Random_Princess",
        "mode_group": "special_event",
        "recognition_guidance": (
            "Treat this as Anarchy League completion evidence that can be "
            "supported by recent All_Random_Princess battles; do not describe "
            "Princess and Anarchy League as the same event, and do not infer "
            "rank, reward, or strategy."
        ),
    },
}

RANKED_GAME_MODE_IDS = {72000450, 72000464}
WAR_GAME_MODE_IDS = {72000266, 72000267, 72000268, 72000321}
LADDER_GAME_MODE_IDS = {72000006, 72000060, 72000062, 72000070, 72000073, 72000261}
TWO_V_TWO_GAME_MODE_IDS = {72000014, 72000051}
FRIENDLY_GAME_MODE_IDS = {
    72000007,
    72000031,
    72000032,
    72000042,
    72000050,
    72000065,
    72000087,
    72000232,
    72000254,
    72000314,
}


def _lower(value) -> str:
    return str(value or "").strip().lower()


def _int_or_none(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
        return int(text) if text else None
    except (TypeError, ValueError):
        return None


def mode_group_label(mode_group: str | None) -> str:
    group = _lower(mode_group)
    return MODE_GROUP_LABELS.get(group, str(mode_group or "Other"))


def special_event_badge_names() -> tuple[str, ...]:
    return tuple(SPECIAL_EVENT_BADGE_CONTEXTS.keys())


def special_event_context_for_badge(badge_name: str | None) -> dict | None:
    context = SPECIAL_EVENT_BADGE_CONTEXTS.get(str(badge_name or "").strip())
    return dict(context) if context else None


def special_event_context_for_game_mode(
    *,
    game_mode_id=None,
    game_mode_name: str | None = None,
) -> dict | None:
    mode_id = _int_or_none(game_mode_id)
    mode_name = _lower(game_mode_name)
    for badge_name, context in SPECIAL_EVENT_BADGE_CONTEXTS.items():
        if mode_id is not None and mode_id == context.get("game_mode_id"):
            item = dict(context)
            item["badge_name"] = badge_name
            return item
        if mode_name and mode_name == _lower(context.get("game_mode_name")):
            item = dict(context)
            item["badge_name"] = badge_name
            return item
    return None


def classify_battle_mode(
    *,
    battle_type: str | None = None,
    game_mode_id=None,
    game_mode_name: str | None = None,
    deck_selection: str | None = None,
    event_tag: str | None = None,
    tournament_tag: str | None = None,
    is_hosted_match=None,
    team_size: int | None = None,
    opponent_size: int | None = None,
) -> str:
    """Return Elixir's stable mode family for one battle-like payload."""
    battle_type_l = _lower(battle_type)
    mode_name_l = _lower(game_mode_name)
    deck_selection_l = _lower(deck_selection)
    mode_id = _int_or_none(game_mode_id)
    hosted = bool(is_hosted_match) if is_hosted_match is not None else False

    if (
        battle_type_l in {"riverracepvp", "riverraceduel", "riverraceduelcolosseum", "boatbattle"}
        or mode_id in WAR_GAME_MODE_IDS
        or "clanwar" in mode_name_l
        or mode_name_l.startswith("cw_")
    ):
        return "war"

    if battle_type_l == "pathoflegend" or mode_id in RANKED_GAME_MODE_IDS or "ranked1v1" in mode_name_l:
        return "ranked"

    if battle_type_l == "tournament" or tournament_tag:
        return "tournament"

    if (
        battle_type_l == "clanmate2v2"
        or mode_id in TWO_V_TWO_GAME_MODE_IDS
        or "teamvsteam" in mode_name_l
        or team_size == 2
        or opponent_size == 2
    ):
        return "two_v_two"

    if event_tag or battle_type_l == "trail":
        return "special_event"

    if battle_type_l == "pvp" or (mode_id in LADDER_GAME_MODE_IDS and "ladder" in mode_name_l):
        return "ladder"

    if (
        battle_type_l in {"clanmate", "friendly", "unknown"}
        or hosted
        or mode_id in FRIENDLY_GAME_MODE_IDS
        or "friendly" in mode_name_l
    ):
        return "friendly"

    if deck_selection_l in {"eventdeck", "predefined", "pick", "draft", "draftcompetitive"}:
        return "special_event"

    return "other"


def classify_progress_key(progress_key: str | None) -> str:
    """Classify an opaque ``Player.progress`` key into a high-level family."""
    key = _lower(progress_key)
    if not key:
        return "side_mode"
    if "autochess" in key or "merge" in key:
        return "side_mode"
    if "anarchyleague" in key or "trail" in key:
        return "side_mode"
    if "trophy-road" in key or "trophyroad" in key:
        return "ladder"
    return "side_mode"


def battle_matches_mode(mode: str | None, **battle_fields) -> bool:
    """Return whether battle fields satisfy a tool/user mode filter."""
    requested = _lower(mode).replace("-", "_")
    if not requested:
        return True
    if requested in {"path_of_legend", "path_of_legends", "pol"}:
        requested = "ranked"
    if requested in {"event", "challenge", "challenges"}:
        requested = "special_event"
    if requested == "2v2":
        requested = "two_v_two"
    return classify_battle_mode(**battle_fields) == requested


__all__ = [
    "MODE_GROUPS",
    "MODE_GROUP_LABELS",
    "RANKED_GAME_MODE_IDS",
    "SPECIAL_EVENT_BADGE_CONTEXTS",
    "WAR_GAME_MODE_IDS",
    "LADDER_GAME_MODE_IDS",
    "TWO_V_TWO_GAME_MODE_IDS",
    "FRIENDLY_GAME_MODE_IDS",
    "battle_matches_mode",
    "classify_battle_mode",
    "classify_progress_key",
    "mode_group_label",
    "special_event_badge_names",
    "special_event_context_for_badge",
    "special_event_context_for_game_mode",
]
