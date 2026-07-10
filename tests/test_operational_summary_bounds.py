"""The get_elixir_state `operational_summary` payload must stay under the tool
envelope's char cap so it is never blindly trimmed mid-deliberation. This is a
foundation guarantee for the awareness brain: when it drills into operational
state, it must get the whole (bounded) picture, not a silently-gutted one.
"""

import json
from unittest.mock import patch

# Full runtime init before importing tool internals (avoids a circular import).
import elixir  # noqa: F401

from agent.chat import _build_tool_result_envelope
from agent.core import TOOL_RESULT_MAX_CHARS
from agent.tool_exec import _compact_intent, _execute_get_elixir_state


def _heavy_intent_rows(n: int, payload_chars: int = 900) -> list[dict]:
    """Full communication_intents rows with a heavy payload_json — the field
    that used to blow the summary past the cap."""
    return [
        {
            "intent_id": i, "recognition_key": f"rk_{i}",
            "intent_type": "celebrate:milestone", "lane": "member-highlights",
            "scope": "public", "payload_json": json.dumps({"blob": "x" * payload_chars}),
            "status": "fulfilled", "attempts": 1, "created_at": "2026-07-09T10:00:00Z",
            "expires_at": "2026-07-16T10:00:00Z", "fulfilled_at": "2026-07-09T10:01:00Z",
            "discord_message_id": str(1000 + i), "last_error": None,
        }
        for i in range(n)
    ]


def test_compact_intent_drops_heavy_fields():
    row = {
        "intent_id": 5, "recognition_key": "rk", "intent_type": "celebrate",
        "lane": "member-highlights", "scope": "public", "payload_json": "x" * 5000,
        "status": "fulfilled", "attempts": 1, "created_at": "t", "expires_at": "t",
        "fulfilled_at": "t", "discord_message_id": "123", "last_error": None,
    }
    out = _compact_intent(row)
    assert set(out) == {"intent_type", "lane", "status", "created_at", "posted", "last_error"}
    assert "payload_json" not in out
    assert out["posted"] is True  # derived from discord_message_id


def test_operational_summary_stays_under_envelope_cap():
    # Far more intents than the summary keeps, each heavy — the pre-fix payload
    # would be ~40KB of intents alone and trigger a blind trim.
    heavy = _heavy_intent_rows(30)

    def _fake_list(**kw):  # simulate SQL LIMIT so we exercise the summary's own cap
        if kw.get("status") == "failed":
            return []
        return heavy[: kw.get("limit", len(heavy))]

    with patch("agent.tool_exec.db.list_recent_communication_intents", side_effect=_fake_list):
        result = _execute_get_elixir_state({"aspect": "operational_summary"}, workflow="awareness")
    envelope = _build_tool_result_envelope("get_elixir_state", json.dumps(result, default=str))

    assert json.loads(envelope).get("truncated") is False
    assert len(envelope) <= TOOL_RESULT_MAX_CHARS

    # Intents are compact and bounded — no payload_json survives.
    intents = result["recent_intents"]
    assert len(intents) <= 15
    assert intents and all("payload_json" not in i for i in intents)

    # Cases are deduped: a due case never also appears in open.
    cases = result["decision_cases"]
    due_keys = {c.get("case_key") for c in cases.get("due", [])}
    open_keys = {c.get("case_key") for c in cases.get("open", [])}
    assert due_keys.isdisjoint(open_keys)
