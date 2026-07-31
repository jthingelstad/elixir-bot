import elixir_agent
from agent.workflow_registry import get_workflow_spec


def _names(tools):
    return [tool["name"] for tool in tools]


def test_registry_generates_compatibility_maps():
    for workflow in (
        "channel_update",
        "channel_update_leadership",
        "interactive",
        "clanops",
        "reception",
        "roster_bios",
        "deck_review",
        "intel_report",
        "tournament_recap",
        "tournament_update",
        "awareness",
        "memory_synthesis",
        "clan_chat_copy",
    ):
        spec = get_workflow_spec(workflow)
        assert elixir_agent.MAX_ROUNDS_BY_WORKFLOW[workflow] == spec.max_tool_rounds
        assert elixir_agent.RESPONSE_SCHEMAS_BY_WORKFLOW[workflow] == spec.response_schema
        assert _names(elixir_agent.TOOLSETS_BY_WORKFLOW[workflow]) == _names(spec.tools)


def test_registry_model_selection_matches_existing_defaults(monkeypatch):
    monkeypatch.setenv("ELIXIR_CHAT_MODEL", "chat-model")
    monkeypatch.setenv("ELIXIR_CREATIVE_MODEL", "creative-model")
    monkeypatch.setenv("ELIXIR_LIGHTWEIGHT_MODEL", "light-model")
    monkeypatch.setenv("ELIXIR_INTENSIVE_MODEL", "intensive-model")

    assert elixir_agent._model_for_workflow("interactive") == "chat-model"
    assert elixir_agent._model_for_workflow("recruiting_copy") == "creative-model"
    assert elixir_agent._model_for_workflow("intel_report") == "intensive-model"
    # leader_action_feedback demoted intensive->chat 2026-07-23 (Sonnet at parity, ~half cost)
    assert elixir_agent._model_for_workflow("leader_action_feedback") == "chat-model"
    assert elixir_agent._model_for_workflow("memory_synthesis") == "intensive-model"
    assert elixir_agent._model_for_workflow("clan_chat_copy") == "chat-model"


def test_empty_toolsets_stay_empty():
    for workflow in (
        "reception",
        "memory_synthesis",
        "leader_action_feedback",
        "clan_chat_copy",
    ):
        assert elixir_agent.TOOLSETS_BY_WORKFLOW[workflow] == []


def test_shared_tool_block_has_fifteen_non_overlapping_owners():
    shared = get_workflow_spec("clanops").tools
    names = _names(shared)

    assert len(names) == len(set(names)) == 15
    assert "get_battle_intelligence" in names
    assert "get_member_cards" in names
    assert "record_leadership_followup" in names
    assert {
        "get_war_season",
        "get_clan_health",
        "get_clan_game_modes",
        "get_member_card_profile",
        "lookup_member_cards",
        "get_clan_intel_report",
        "update_member",
        "flag_member_watch",
        "raise_clan_chat_relay",
        "schedule_revisit",
    }.isdisjoint(names)


def test_intel_report_uses_only_narrow_cr_api_surface():
    assert _names(get_workflow_spec("intel_report").tools) == ["cr_api"]
