"""Regression coverage for retained API enum values in the agent reference."""

from pathlib import Path

import pytest

_PLAYERS_REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "cr-api-docs" / "players.md"
_BATTLES_REFERENCE = (
    Path(__file__).resolve().parents[1] / "docs" / "cr-api-docs" / "models" / "battles.md"
)


@pytest.mark.parametrize(
    ("mode_id", "mode_name"),
    [
        (72000011, "DoubleElixir_Friendly"),
        (72000033, "RampUpElixir_Friendly"),
        (72000286, "TeamVsTeam_TripleElixir_Friendly"),
        (72000376, "Event_RestlessDead"),
        (72000501, "All_Random_Princess"),
        (72000505, "Chaos_1v1_Draft"),
    ],
)
def test_observed_game_modes_remain_in_agent_reference(mode_id: int, mode_name: str) -> None:
    """Keep the documented ID/name pair aligned with retained battle-log evidence."""
    reference = _PLAYERS_REFERENCE.read_text()

    assert f"| {mode_id} | {mode_name}" in reference


def test_observed_unknown_deck_selection_remains_in_agent_references() -> None:
    """Keep the retained event selector explicit without assigning it false semantics."""
    assert "| `unknown`          | Observed once on event-tagged `All_Random_Princess`" in (
        _PLAYERS_REFERENCE.read_text()
    )
    assert "- `unknown`" in _BATTLES_REFERENCE.read_text()
