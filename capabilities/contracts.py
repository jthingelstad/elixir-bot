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


__all__ = [
    "CapabilityEnvelope",
    "ClanGameModesResult",
    "ClanGameModeWindowsResult",
    "SourcedCapabilityEnvelope",
    "WarIntelligenceEnvelope",
    "WarIntelligenceResult",
    "WarSeasonViewResult",
]
