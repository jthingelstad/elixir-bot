"""Shared domain capabilities consumed by every Elixir surface.

Capabilities own structured, audience-neutral answers derived from the engine's
events and projections. Agent tools, awareness, reports, and admin surfaces are
adapters over these contracts; they do not reimplement the underlying facts.
"""

from capabilities.awards import get_awards_recognition
from capabilities.contracts import (
    CapabilityEnvelope,
    ClanGameModesResult,
    ClanGameModeWindowsResult,
    SourcedCapabilityEnvelope,
    WarIntelligenceEnvelope,
    WarIntelligenceResult,
    WarSeasonViewResult,
)
from capabilities.decks import get_deck_intelligence
from capabilities.game_modes import get_clan_game_mode_windows, get_clan_game_modes
from capabilities.game_truth import get_game_truth
from capabilities.management import get_management_decisions
from capabilities.members import get_member_intelligence
from capabilities.war import get_war_intelligence, get_war_season_view

__all__ = [
    "CapabilityEnvelope",
    "ClanGameModesResult",
    "ClanGameModeWindowsResult",
    "SourcedCapabilityEnvelope",
    "WarIntelligenceEnvelope",
    "WarIntelligenceResult",
    "WarSeasonViewResult",
    "get_clan_game_mode_windows",
    "get_clan_game_modes",
    "get_deck_intelligence",
    "get_game_truth",
    "get_awards_recognition",
    "get_management_decisions",
    "get_member_intelligence",
    "get_war_intelligence",
    "get_war_season_view",
]
