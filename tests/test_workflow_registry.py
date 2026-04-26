import elixir_agent

from agent.workflow_registry import canonical_workflow_name, get_workflow_spec


def _names(tools):
    return [tool["name"] for tool in tools]


def test_observe_alias_resolves_to_observation():
    assert canonical_workflow_name("observe") == "observation"
    assert get_workflow_spec("observe") is get_workflow_spec("observation")


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
        "war_recap",
        "season_awards",
        "awareness",
        "memory_synthesis",
    ):
        spec = get_workflow_spec(workflow)
        assert elixir_agent.MAX_ROUNDS_BY_WORKFLOW[workflow] == spec.max_tool_rounds
        assert elixir_agent.RESPONSE_SCHEMAS_BY_WORKFLOW[workflow] == spec.response_schema
        assert _names(elixir_agent.TOOLSETS_BY_WORKFLOW[workflow]) == _names(spec.tools)


def test_legacy_observe_toolset_still_available():
    assert _names(elixir_agent.TOOLSETS_BY_WORKFLOW["observe"]) == _names(
        elixir_agent.TOOLSETS_BY_WORKFLOW["observation"]
    )


def test_registry_model_selection_matches_existing_defaults(monkeypatch):
    monkeypatch.setenv("ELIXIR_CHAT_MODEL", "chat-model")
    monkeypatch.setenv("ELIXIR_PROMOTION_MODEL", "promotion-model")
    monkeypatch.setenv("ELIXIR_LIGHTWEIGHT_MODEL", "light-model")

    assert elixir_agent._model_for_workflow("interactive") == "light-model"
    assert elixir_agent._model_for_workflow("site_promote_content") == "promotion-model"
    assert elixir_agent._model_for_workflow("intel_report") == "chat-model"
    assert elixir_agent._model_for_workflow("memory_synthesis") == "chat-model"
    assert elixir_agent._model_for_workflow("observe") == "light-model"


def test_empty_toolsets_stay_empty():
    for workflow in ("reception", "war_recap", "season_awards", "memory_synthesis"):
        assert elixir_agent.TOOLSETS_BY_WORKFLOW[workflow] == []
