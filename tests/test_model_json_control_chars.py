"""A model that writes real paragraph breaks must not lose its whole answer.

2026-08-07, 12:00 CT: the #ask-elixir daily post failed with
`compose failed: parse_error: Invalid control character at: line 1 column 548
(char 547)`. The response was a valid {"post", "topic"} object; it just carried
five literal newlines inside the post string, because a Discord post has
paragraph breaks and the model wrote them rather than escaping them as \\n.
Python's json.loads rejects raw control characters in strings by default, so a
good answer was thrown away.

This is the failure the ceiling fix uncovered: raising ask_elixir_daily to 4096
got the composer past its first tool round for the first time since 2026-07-26,
and it reached the JSON parse — which then rejected it.

`strict=False` permits control characters INSIDE strings and changes nothing
else about the grammar. It is applied to model output only; envelopes we
serialize ourselves stay strict, because a control character there is a bug.
"""

from __future__ import annotations

import json

import pytest

from agent import chat

# Verbatim shape of the response that failed in production: one line, real
# newlines inside the post value.
_REAL_POST = (
    "Every one of you has a card the rest of the clan is still leveling.\n\n"
    "Curious where you land? Ask me.\n> Show my max cards"
)
_MODEL_TEXT = json.dumps({"post": _REAL_POST, "topic": "max-cards"}).replace("\\n", "\n")


def test_the_production_payload_is_actually_rejected_by_strict_json():
    """Pin the premise — without this, the fix below proves nothing."""
    with pytest.raises(json.JSONDecodeError, match="Invalid control character"):
        json.loads(_MODEL_TEXT)


def test_parse_json_response_accepts_literal_newlines_in_strings():
    parsed = chat._parse_json_response(_MODEL_TEXT)
    assert parsed["topic"] == "max-cards"
    assert parsed["post"] == _REAL_POST
    assert "\n\n" in parsed["post"], "the paragraph break must survive the parse"


def test_parse_response_accepts_literal_newlines_in_strings():
    """The other model-output parser — recruiting_copy uses this one, and it
    composes prose for five channels in a single object."""
    parsed = chat._parse_response(_MODEL_TEXT)
    assert parsed["post"] == _REAL_POST


def test_fenced_and_preamble_forms_also_survive():
    fenced = f"Here you go:\n```json\n{_MODEL_TEXT}\n```"
    assert chat._parse_json_response(fenced)["topic"] == "max-cards"

    preamble = f"Sure thing. {_MODEL_TEXT}"
    assert chat._parse_json_response(preamble)["topic"] == "max-cards"


def test_malformed_json_still_fails():
    """strict=False loosens control characters only — it must not turn broken
    JSON into a silent pass."""
    with pytest.raises((json.JSONDecodeError, ValueError)):
        chat._parse_json_response(
            '{"post": "unclosed',
        )


def test_tool_envelopes_stay_strict():
    """Envelopes are ours, not the model's. A raw control character in one is a
    bug we want to hear about, so the loosened parser must not be used there."""
    import inspect

    source = inspect.getsource(chat._summarize_tool_result)
    assert "_loads_model_json" not in source
    assert "json.loads(raw)" in source
