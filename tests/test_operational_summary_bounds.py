"""The operational summary remains a bounded, complete tool result."""

import json
from unittest.mock import patch

import elixir  # noqa: F401  # initialize the stable runtime facade
from agent.chat import _build_tool_result_envelope
from agent.core import TOOL_RESULT_MAX_CHARS
from agent.tool_exec import _execute_get_elixir_state


def _heavy_awareness(limit: int) -> dict:
    return {
        "thoughts": [
            {
                "loop_number": i,
                "at": "2026-07-09T10:00:00Z",
                "chose_silence": 0,
                "post_count": 1,
                "skipped_reason": None,
                "model": "claude-sonnet-4-6",
            }
            for i in range(limit)
        ],
        "posts": [
            {
                "post_id": i,
                "lane": "elixir",
                "content_preview": "x" * 800,
                "covers_json": "[]",
                "loop_number": i,
                "posted_at": "2026-07-09T10:01:00Z",
                "discord_message_id": str(1000 + i),
            }
            for i in range(limit)
        ],
    }


def test_operational_summary_stays_under_envelope_cap():
    def _fake_activity(*, limit):
        return _heavy_awareness(limit)

    with patch("agent.tool_exec.db.get_awareness_activity", side_effect=_fake_activity):
        result = _execute_get_elixir_state({"aspect": "operational_summary"}, workflow="awareness")
    envelope = _build_tool_result_envelope("get_elixir_state", json.dumps(result, default=str))

    assert json.loads(envelope).get("truncated") is False
    assert len(envelope) <= TOOL_RESULT_MAX_CHARS
    assert len(result["awareness"]["posts"]) <= 15

    cases = result["decision_cases"]
    due_keys = {case.get("case_key") for case in cases.get("due", [])}
    open_keys = {case.get("case_key") for case in cases.get("open", [])}
    assert due_keys.isdisjoint(open_keys)
