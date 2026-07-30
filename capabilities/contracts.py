"""Typed top-level contracts for stable cross-layer capabilities.

Capability payloads remain ordinary dictionaries because they cross tool,
prompt, admin, and JSON boundaries. These ``TypedDict`` definitions make the
shared envelope and high-traffic domain results explicit without forcing
storage rows or internal query shapes into a repository-wide typing scheme.
"""

from __future__ import annotations

from typing import Any, TypedDict


class CapabilityEnvelope(TypedDict):
    capability: str
    contract_version: int


class _GenerationEnvelope(TypedDict, total=False):
    data_generation: dict[str, Any] | None


class SourcedCapabilityEnvelope(CapabilityEnvelope, _GenerationEnvelope):
    sources: list[str]


class ClanGameModesResult(SourcedCapabilityEnvelope):
    window_days: int
    mode_group: str | None
    modes: dict[str, dict[str, Any]]
    game_modes: list[dict[str, Any]]
    ranked: dict[str, Any]
    side_modes: dict[str, Any]
    events: dict[str, Any]
    duos: list[dict[str, Any]]


class ClanGameModeWindowsResult(CapabilityEnvelope, total=False):
    windows: dict[str, dict[str, Any]]
    data_generation: Any


class WarIntelligenceEnvelope(SourcedCapabilityEnvelope):
    available: bool


class WarIntelligenceResult(WarIntelligenceEnvelope, total=False):
    observed_at: str | None
    current_state: dict[str, Any]
    day_state: dict[str, Any]
    clock: dict[str, Any]
    weekly_race: dict[str, Any]
    daily_race: dict[str, Any]
    projection: dict[str, Any]
    period: dict[str, Any]
    engagement: dict[str, Any]
    game_truth: dict[str, Any]


class WarSeasonViewResult(SourcedCapabilityEnvelope):
    view: str
    season_id: Any
    data: Any


class MemberIntelligenceResult(SourcedCapabilityEnvelope, total=False):
    """One key per facet `get_member_intelligence` can return.

    These fell out of sync because the gate only typechecked this file in
    isolation — `mypy capabilities/contracts.py` proves the definitions parse,
    never that an implementation matches them. Seven facets shipped without a
    key here (`loadout`, `losses`, `wins`, `history`, `ranked`,
    `mode_activity`, `awards`); the contract described a smaller capability
    than the one consumers were already calling.
    """

    player_tag: str
    requested_facets: list[str]
    freshness: dict[str, Any]
    data_generation: Any
    profile: Any
    form: Any
    playstyle: Any
    war: Any
    trend: Any
    battles: Any
    events: Any
    loadout: Any
    losses: Any
    wins: Any
    history: Any
    ranked: Any
    mode_activity: Any
    awards: Any


class ManagementDecisionResult(SourcedCapabilityEnvelope):
    view: str
    audience: str
    policy: dict[str, Any]
    readiness: dict[str, Any]
    freshness: dict[str, Any]
    data: Any


class AwardsRecognitionResult(SourcedCapabilityEnvelope):
    view: str
    state: str
    period: dict[str, Any]
    data: Any


class DeckIntelligenceResult(CapabilityEnvelope, total=False):
    """Member view, clan view and card-impact view share this envelope.

    Under-declared for the same reason as `MemberIntelligenceResult`: the gate
    only parsed this file, so ten member-view keys and six clan-view keys were
    returned by code that no checker ever compared against the contract.
    """

    available: bool
    error: str
    view: str
    scope: str
    player_tag: str
    player_name: str | None
    sources: list[str]
    window: dict[str, Any]
    evidence_limits: Any
    # member view
    current_deck: Any
    current_deck_note: Any
    primary_deck: Any
    variants: Any
    recent_change: Any
    stability: Any
    upgrade_bottlenecks: Any
    # clan view
    coverage: Any
    archetype_spread: Any
    win_condition_spread: Any
    common_primary_deck_cards: Any
    members: Any
    # card-impact view
    cards: Any
    battles_considered: Any
    changes: Any
    affected_members: Any
    affected_member_count: Any
    changes_without_member_evidence: Any
    interpretation: Any


class GameTruthResult(CapabilityEnvelope, total=False):
    topic: str
    available: bool
    error: str
    sources: list[str]
    mechanics: dict[str, Any]


__all__ = [
    "CapabilityEnvelope",
    "AwardsRecognitionResult",
    "ClanGameModesResult",
    "ClanGameModeWindowsResult",
    "DeckIntelligenceResult",
    "GameTruthResult",
    "ManagementDecisionResult",
    "MemberIntelligenceResult",
    "SourcedCapabilityEnvelope",
    "WarIntelligenceEnvelope",
    "WarIntelligenceResult",
    "WarSeasonViewResult",
]
