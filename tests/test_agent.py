"""Tests for elixir_agent.py — tool-calling loop and response parsing."""

import json
from unittest.mock import MagicMock, patch

import pytest

import elixir_agent


@pytest.fixture(autouse=True)
def _mock_openai_client():
    """Inject a mock OpenAI client so tests don't need an API key."""
    mock_client = MagicMock()
    with patch.object(elixir_agent, "_client", mock_client):
        with patch.object(elixir_agent, "_get_client", return_value=mock_client):
            yield mock_client


def test_parse_response_json():
    """Parses valid JSON response."""
    raw = '{"event_type": "clan_observation", "content": "Hello", "summary": "test"}'
    result = elixir_agent._parse_response(raw)
    assert result["event_type"] == "clan_observation"


def test_parse_response_null():
    """Returns None for 'null' response."""
    assert elixir_agent._parse_response("null") is None
    assert elixir_agent._parse_response("NULL") is None


def test_parse_response_markdown_fence():
    """Strips markdown code fences."""
    raw = '```json\n{"event_type": "test", "content": "hi"}\n```'
    result = elixir_agent._parse_response(raw)
    assert result["event_type"] == "test"


def test_parse_response_plain_text_fallback():
    """Wraps plain text as content dict when JSON fails."""
    result = elixir_agent._parse_response("not json at all")
    assert result is not None
    assert result["content"] == "not json at all"
    assert "summary" in result


def test_parse_response_empty():
    """Returns None for empty string."""
    assert elixir_agent._parse_response("") is None
    assert elixir_agent._parse_response("   ") is None


def test_knowledge_in_system_prompt():
    """System prompts include game and clan knowledge."""
    observe = elixir_agent._observe_system()
    assert "POAP KINGS" in observe
    assert "THURSDAY" in observe.upper()
    leader = elixir_agent._leader_system()
    assert "POAP KINGS" in leader
    assert "Elder" in leader


def test_execute_tool_get_war_results():
    """Tool execution returns serialized results."""
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_war_history.return_value = [
            {"season_id": 50, "our_rank": 1, "our_fame": 10000}
        ]
        result = elixir_agent._execute_tool("get_war_results", {"count": 5})
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["our_rank"] == 1


def test_execute_tool_unknown():
    """Unknown tool returns error."""
    result = elixir_agent._execute_tool("nonexistent_tool", {})
    parsed = json.loads(result)
    assert "error" in parsed


def _make_mock_response(content=None, tool_calls=None):
    """Create a mock OpenAI response."""
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_chat_no_tools(_mock_openai_client):
    """Direct response without tool calls."""
    final = '{"event_type": "test", "content": "Hello", "summary": "test"}'
    mock_resp = _make_mock_response(content=final)

    _mock_openai_client.chat.completions.create.return_value = mock_resp
    result = elixir_agent._chat_with_tools("system", "user")
    assert result["event_type"] == "test"
    assert "Hello" in result["content"]


def test_chat_with_tool_call(_mock_openai_client):
    """LLM makes a tool call, gets result, then gives final answer."""
    # First response: tool call
    tool_call = MagicMock()
    tool_call.id = "call_123"
    tool_call.function.name = "get_war_results"
    tool_call.function.arguments = '{"count": 3}'
    first_resp = _make_mock_response(tool_calls=[tool_call])
    first_resp.choices[0].message.content = None

    # Second response: final answer
    final = '{"event_type": "war_update", "content": "We won!", "summary": "victory"}'
    second_resp = _make_mock_response(content=final)

    _mock_openai_client.chat.completions.create.side_effect = [first_resp, second_resp]

    with patch("elixir_agent.db") as mock_db:
        mock_db.get_war_history.return_value = [{"our_rank": 1}]
        result = elixir_agent._chat_with_tools("system", "user")

    assert result["event_type"] == "war_update"


def test_max_rounds_respected(_mock_openai_client):
    """Loop stops after MAX_TOOL_ROUNDS even if model keeps calling tools."""
    tool_call = MagicMock()
    tool_call.id = "call_abc"
    tool_call.function.name = "get_war_results"
    tool_call.function.arguments = "{}"

    tool_resp = _make_mock_response(tool_calls=[tool_call])
    tool_resp.choices[0].message.content = None

    final = '{"event_type": "test", "content": "done", "summary": "done"}'
    final_resp = _make_mock_response(content=final)

    # MAX_TOOL_ROUNDS + 1 tool responses, then a final
    responses = [tool_resp] * (elixir_agent.MAX_TOOL_ROUNDS + 1) + [final_resp]
    _mock_openai_client.chat.completions.create.side_effect = responses

    with patch("elixir_agent.db") as mock_db:
        mock_db.get_war_history.return_value = []
        result = elixir_agent._chat_with_tools("system", "user")

    assert result is not None


def test_null_response_returns_none(_mock_openai_client):
    """When LLM returns null, observe_and_post returns None."""
    mock_resp = _make_mock_response(content="null")
    _mock_openai_client.chat.completions.create.return_value = mock_resp
    result = elixir_agent.observe_and_post({}, {})
    assert result is None


def test_observe_with_signals(_mock_openai_client):
    """Signals are included in the user message to the LLM."""
    final = '{"event_type": "arena_milestone", "content": "King Levy hit 10k!", "summary": "milestone"}'
    mock_resp = _make_mock_response(content=final)
    _mock_openai_client.chat.completions.create.return_value = mock_resp

    signals = [{"type": "trophy_milestone", "name": "King Levy", "milestone": 10000}]

    result = elixir_agent.observe_and_post(
        {"memberList": []}, {}, signals=signals
    )

    # Verify signals appeared in the user message
    call_args = _mock_openai_client.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    user_msg = messages[1]["content"]
    assert "trophy_milestone" in user_msg
    assert "King Levy" in user_msg
    assert result["event_type"] == "arena_milestone"


def test_respond_to_leader_with_history(_mock_openai_client):
    """Conversation history is injected into messages for leader Q&A."""
    final = '{"event_type": "leader_response", "content": "Based on our earlier talk...", "summary": "follow-up"}'
    mock_resp = _make_mock_response(content=final)
    _mock_openai_client.chat.completions.create.return_value = mock_resp

    history = [
        {"role": "user", "content": "Who should we promote?"},
        {"role": "assistant", "content": "Vijay looks ready."},
    ]

    result = elixir_agent.respond_to_leader(
        question="What about King Levy?",
        author_name="LeaderBob",
        clan_data={"memberList": []},
        war_data={},
        conversation_history=history,
    )

    # Verify history appeared in the messages
    call_args = _mock_openai_client.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")

    # messages[0] = system, messages[1] = prior user, messages[2] = prior assistant,
    # messages[3] = current user
    assert len(messages) == 4
    assert messages[1]["role"] == "user"
    assert "promote" in messages[1]["content"]
    assert messages[2]["role"] == "assistant"
    assert "Vijay" in messages[2]["content"]
    assert messages[3]["role"] == "user"
    assert "King Levy" in messages[3]["content"]
    assert result["event_type"] == "leader_response"


def test_respond_to_leader_without_history(_mock_openai_client):
    """Leader Q&A works without conversation history."""
    final = '{"event_type": "leader_response", "content": "Answer here.", "summary": "response"}'
    mock_resp = _make_mock_response(content=final)
    _mock_openai_client.chat.completions.create.return_value = mock_resp

    result = elixir_agent.respond_to_leader(
        question="How's our war going?",
        author_name="LeaderBob",
        clan_data={"memberList": []},
        war_data={},
    )

    call_args = _mock_openai_client.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")

    # Just system + user, no history
    assert len(messages) == 2
    assert result["event_type"] == "leader_response"


def test_leader_share_in_prompt():
    """Leader system prompt describes the leader_share event type."""
    leader = elixir_agent._leader_system()
    assert "leader_share" in leader
    assert "share_content" in leader


def test_respond_to_leader_share(_mock_openai_client):
    """Leader asking to share produces a leader_share response with share_content."""
    final = json.dumps({
        "event_type": "leader_share",
        "member_tags": [],
        "member_names": ["King Levy"],
        "summary": "Shout out to King Levy",
        "content": "Done! I posted a shout-out to King Levy in the broadcast channel.",
        "share_content": "Big shout-out to **King Levy** for crushing it this week! Keep it up, kings!",
        "metadata": {},
    })
    mock_resp = _make_mock_response(content=final)
    _mock_openai_client.chat.completions.create.return_value = mock_resp

    result = elixir_agent.respond_to_leader(
        question="Share a shout-out to King Levy with the clan",
        author_name="LeaderBob",
        clan_data={"memberList": []},
        war_data={},
    )

    assert result["event_type"] == "leader_share"
    assert "share_content" in result
    assert "King Levy" in result["share_content"]


def test_reception_system_prompt():
    """Reception system prompt describes onboarding instructions."""
    reception = elixir_agent._reception_system()
    assert "nickname" in reception.lower()
    assert "reception_response" in reception


def test_respond_in_reception(_mock_openai_client):
    """Reception Q&A returns a helpful onboarding response."""
    final = json.dumps({
        "event_type": "reception_response",
        "content": "Hey! I can see **King Levy** in our roster. Set that as your server nickname and I'll get you in!",
    })
    mock_resp = _make_mock_response(content=final)
    _mock_openai_client.chat.completions.create.return_value = mock_resp

    result = elixir_agent.respond_in_reception(
        question="My name is King Levy",
        author_name="NewUser",
        clan_data={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
    )

    assert result["event_type"] == "reception_response"
    assert "King Levy" in result["content"]

    # Verify roster appeared in user message
    call_args = _mock_openai_client.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    user_msg = messages[1]["content"]
    assert "King Levy" in user_msg
    assert "#ABC" in user_msg


def test_execute_tool_war_champ_standings():
    """War Champ standings tool returns serialized results."""
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_war_champ_standings.return_value = [
            {"tag": "#ABC", "name": "King Levy", "total_fame": 6700, "races_participated": 2}
        ]
        result = elixir_agent._execute_tool("get_war_champ_standings", {})
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "King Levy"
        assert parsed[0]["total_fame"] == 6700


def test_execute_tool_perfect_war_participants():
    """Perfect war participants tool returns serialized results."""
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_perfect_war_participants.return_value = [
            {"tag": "#ABC", "name": "King Levy", "races_participated": 4,
             "total_fame": 12000, "total_races_in_season": 4}
        ]
        result = elixir_agent._execute_tool("get_perfect_war_participants", {})
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "King Levy"
        assert parsed[0]["total_races_in_season"] == 4


def test_editorial_system_prompt():
    """Editorial system prompt describes public-facing website role."""
    editorial = elixir_agent._editorial_system()
    assert "public" in editorial.lower()
    assert "speech bubble" in editorial.lower()
    assert "280 characters" in editorial


def test_write_editorial(_mock_openai_client):
    """write_editorial returns plain text from LLM."""
    mock_resp = _make_mock_response(content="POAP KINGS crushed it in war today. Keep pushing, kings!")
    _mock_openai_client.chat.completions.create.return_value = mock_resp

    result = elixir_agent.write_editorial(
        clan_data={"memberList": []},
        war_data={},
        previous_messages=[{"date": "2026-03-03", "text": "Old message"}],
    )

    assert result is not None

    # Verify previous messages appear in user message
    call_args = _mock_openai_client.chat.completions.create.call_args
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    user_msg = messages[1]["content"]
    assert "Old message" in user_msg


def test_write_editorial_null_response(_mock_openai_client):
    """write_editorial returns None when LLM returns null."""
    mock_resp = _make_mock_response(content="null")
    _mock_openai_client.chat.completions.create.return_value = mock_resp

    result = elixir_agent.write_editorial({}, {}, [])
    assert result is None


def test_clan_context_no_recent():
    """_clan_context no longer includes recent entries section."""
    ctx = elixir_agent._clan_context({"memberList": []}, {})
    assert "RECENT ELIXIR POSTS" not in ctx
    assert "CLAN ROSTER" in ctx
    assert "WAR STATUS" in ctx


def test_generate_message(_mock_openai_client):
    """generate_message returns plain text for events."""
    mock_resp = _make_mock_response(content="Welcome to the clan, NewPlayer!")
    _mock_openai_client.chat.completions.create.return_value = mock_resp

    result = elixir_agent.generate_message(
        "member_join_broadcast",
        "New member 'NewPlayer' joined the clan.",
    )
    assert result is not None
    assert "NewPlayer" in result


def test_generate_message_null(_mock_openai_client):
    """generate_message returns None on null response."""
    mock_resp = _make_mock_response(content="null")
    _mock_openai_client.chat.completions.create.return_value = mock_resp

    result = elixir_agent.generate_message("test_event", "test context")
    assert result is None


def test_generate_message_api_error(_mock_openai_client):
    """generate_message returns None on API error."""
    _mock_openai_client.chat.completions.create.side_effect = Exception("API error")

    result = elixir_agent.generate_message("test_event", "test context")
    assert result is None


def test_event_system_includes_channels():
    """Event system prompt includes channel definitions."""
    event_sys = elixir_agent._event_system()
    assert "#elixir" in event_sys
    assert "#reception" in event_sys
    assert "broadcast" in event_sys.lower()
