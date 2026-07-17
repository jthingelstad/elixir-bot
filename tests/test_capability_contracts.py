"""The typed capability boundary is deliberate and reviewable."""

from __future__ import annotations

from typing import get_type_hints

from capabilities.contracts import (
    CapabilityEnvelope,
    ClanGameModesResult,
    ClanGameModeWindowsResult,
    SourcedCapabilityEnvelope,
    WarIntelligenceEnvelope,
    WarIntelligenceResult,
    WarSeasonViewResult,
)
from capabilities.game_modes import get_clan_game_mode_windows, get_clan_game_modes
from capabilities.war import get_war_intelligence, get_war_season_view


def test_shared_capability_envelopes_require_identity_version_and_sources():
    assert CapabilityEnvelope.__required_keys__ == {"capability", "contract_version"}
    assert SourcedCapabilityEnvelope.__required_keys__ == {
        "capability",
        "contract_version",
        "sources",
    }
    assert WarIntelligenceEnvelope.__required_keys__ == {
        "available",
        "capability",
        "contract_version",
        "sources",
    }


def test_high_traffic_capabilities_publish_concrete_return_contracts():
    assert get_type_hints(get_clan_game_modes)["return"] is ClanGameModesResult
    assert get_type_hints(get_clan_game_mode_windows)["return"] is ClanGameModeWindowsResult
    assert get_type_hints(get_war_intelligence)["return"] is WarIntelligenceResult
    assert get_type_hints(get_war_season_view)["return"] is WarSeasonViewResult
