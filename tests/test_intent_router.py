"""Tests for the LLM intent router and the registry it draws from."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import intent_router
from agent.core import _Choice, _Function, _LLMResponse, _Message, _ToolCall, _Usage
from runtime import intent_registry


# ── Registry sanity ──────────────────────────────────────────────────────────


def test_every_route_has_required_fields():
    for route in intent_registry.ROUTES:
        assert isinstance(route["key"], str) and route["key"]
        assert isinstance(route["label"], str) and route["label"]
        assert isinstance(route["router_description"], str) and route["router_description"]
        assert isinstance(route["examples"], list) and route["examples"]
        assert isinstance(route["workflows"], set) and route["workflows"]
        assert isinstance(route.get("requires_mention", False), bool)


def test_route_keys_includes_llm_chat_and_not_for_bot():
    assert "llm_chat" in intent_registry.ROUTE_KEYS
    assert "not_for_bot" in intent_registry.ROUTE_KEYS


def test_route_keys_are_unique():
    assert len(intent_registry.ROUTE_KEYS) == len(set(intent_registry.ROUTE_KEYS))


def test_help_routes_excludes_meta_routes():
    """Help report shouldn't list 'llm_chat' or 'not_for_bot' as user-facing capabilities."""
    keys = {r["key"] for r in intent_registry.help_routes_for_workflow("interactive")}
    assert "llm_chat" not in keys
    assert "not_for_bot" not in keys
    assert "help" in keys  # but help itself is shown


def test_router_route_summaries_includes_descriptions():
    text = intent_registry.router_route_summaries(["clanops"])
    assert "**kick_risk**" in text
    assert "**top_war_contributors**" in text
    # Interactive-only routes are filtered out when only clanops is asked,
    # but routes available in both should appear:
    assert "**deck_review**" in text


# ── Intent router output construction ────────────────────────────────────────


def _mock_tool_call_response(args: dict) -> _LLMResponse:
    """Build the wrapped LLM response shape that classify_intent expects."""
    return _LLMResponse(
        choices=[_Choice(
            message=_Message(
                role="assistant",
                content=None,
                tool_calls=[_ToolCall(
                    id="call_1",
                    type="function",
                    function=_Function(name="select_route", arguments=json.dumps(args)),
                )],
            ),
        )],
        usage=_Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )


def _patch_classify(args: dict | None, *, raise_exc: Exception | None = None):
    """Patch the LLM call inside classify_intent."""
    if raise_exc is not None:
        return patch("agent.intent_router._create_chat_completion", side_effect=raise_exc)
    return patch(
        "agent.intent_router._create_chat_completion",
        return_value=_mock_tool_call_response(args or {}),
    )


def test_classify_returns_route_from_tool_call():
    args = {
        "route": "deck_suggest",
        "mode": "war",
        "target_member": "self",
        "confidence": 0.93,
        "rationale": "User wants four new war decks built from scratch.",
    }
    with _patch_classify(args):
        intent = intent_router.classify_intent(
            "recommend four new war decks for me",
            workflow="interactive", mentioned=True,
        )
    assert intent["route"] == "deck_suggest"
    assert intent["mode"] == "war"
    assert intent["target_member"] == "self"
    assert intent["confidence"] == pytest.approx(0.93)
    assert "fallback_reason" not in intent


def test_classify_help_phrasing_routes_to_help():
    args = {"route": "help", "confidence": 0.95, "rationale": "general capability question"}
    with _patch_classify(args):
        intent = intent_router.classify_intent(
            "how can you help me?", workflow="interactive", mentioned=True,
        )
    assert intent["route"] == "help"
    assert intent["mode"] is None  # help has no mode_choices


def test_classify_strips_mode_for_modeless_routes():
    """If the LLM hands back a mode for a route that doesn't support modes, drop it."""
    args = {"route": "kick_risk", "mode": "war", "confidence": 0.9, "rationale": "x"}
    with _patch_classify(args):
        intent = intent_router.classify_intent("who's at risk", workflow="clanops", mentioned=True)
    assert intent["route"] == "kick_risk"
    assert intent["mode"] is None


def test_classify_drops_invalid_mode_for_deck_routes():
    args = {"route": "deck_review", "mode": "supersonic", "confidence": 0.8, "rationale": "x"}
    with _patch_classify(args):
        intent = intent_router.classify_intent("review my deck", workflow="interactive", mentioned=True)
    assert intent["route"] == "deck_review"
    assert intent["mode"] is None


def test_classify_falls_back_when_route_unknown():
    args = {"route": "totally_made_up", "confidence": 0.5, "rationale": "?"}
    with _patch_classify(args):
        intent = intent_router.classify_intent("hi", workflow="interactive", mentioned=True)
    assert intent["route"] == "llm_chat"
    assert intent["fallback_reason"].startswith("unknown_route")


def test_classify_falls_back_when_no_tool_call():
    """If the LLM returned plain text instead of calling the tool, we fall back."""
    no_tool_resp = _LLMResponse(
        choices=[_Choice(message=_Message(role="assistant", content="I think it's deck_review", tool_calls=None))],
        usage=_Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )
    with patch("agent.intent_router._create_chat_completion", return_value=no_tool_resp):
        intent = intent_router.classify_intent("anything", workflow="interactive", mentioned=True)
    assert intent["route"] == "llm_chat"
    assert intent["fallback_reason"] == "no_tool_call"


def test_classify_falls_back_on_llm_error():
    with _patch_classify(None, raise_exc=RuntimeError("network down")):
        intent = intent_router.classify_intent("anything", workflow="interactive", mentioned=True)
    assert intent["route"] == "llm_chat"
    assert intent["fallback_reason"].startswith("llm_error")


def test_classify_target_member_is_normalized():
    args = {"route": "deck_review", "target_member": "stranger", "confidence": 0.7, "rationale": "x"}
    with _patch_classify(args):
        intent = intent_router.classify_intent("review", workflow="interactive", mentioned=True)
    assert intent["target_member"] is None


def test_classify_uses_lightweight_model_by_default():
    """Router should call Haiku, not Sonnet."""
    captured = {}

    def fake_call(**kwargs):
        captured["model"] = kwargs.get("model")
        captured["workflow"] = kwargs.get("workflow")
        captured["tool_choice"] = kwargs.get("tool_choice")
        return _mock_tool_call_response({"route": "help", "confidence": 0.9, "rationale": "x"})

    with patch("agent.intent_router._create_chat_completion", side_effect=fake_call):
        intent_router.classify_intent("help", workflow="interactive", mentioned=False)

    assert captured["model"] == "claude-haiku-4-5-20251001"
    assert captured["workflow"] == intent_router.INTENT_ROUTER_WORKFLOW
    assert captured["tool_choice"] == "required"


# ── Help report sources from registry ────────────────────────────────────────


def test_help_report_interactive_includes_registry_capabilities():
    from runtime.helpers._reports import _build_help_report

    report = _build_help_report("interactive")
    assert "Elixir Help — Interactive" in report
    # The deck_suggest route's example should now appear in help text.
    assert "recommend four new war decks for me" in report or "build me a deck" in report


def test_help_report_clanops_keeps_operator_command_section():
    from runtime.helpers._reports import _build_help_report

    report = _build_help_report("clanops")
    assert "Elixir Help — ClanOps" in report
    assert "/elixir system status" in report
    assert "Operator commands" in report
