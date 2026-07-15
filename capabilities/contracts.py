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


class SourcedCapabilityEnvelope(CapabilityEnvelope):
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


class ClanGameModeWindowsResult(CapabilityEnvelope):
    windows: dict[str, dict[str, Any]]


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
    player_tag: str
    requested_facets: list[str]
    freshness: dict[str, Any]
    profile: Any
    form: Any
    playstyle: Any
    war: Any
    trend: Any
    battles: Any
    events: Any


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
    available: bool
    error: str
    view: str
    player_tag: str
    sources: list[str]


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
