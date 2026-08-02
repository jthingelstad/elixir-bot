"""A leave of absence must be recordable by the tools the model actually has.

This flow has now broken twice the same way, so the tests go end to end rather
than stopping at any one layer:

1. `raise_clan_chat_relay` shipped into `_WRITE_TOOL_NAMES` but never
   `AWARENESS_WRITE_TOOL_NAMES`, so it was offered to a model zero times.
2. `flag_member_watch` — the only pre-emptive writer of the `Hold:` memory —
   was moved out of the advertised set by the trim to 14 tools, ten hours after
   a commit titled "Make the LOA write path reachable".

Both times the consumer was fine, the executor was fine, the suite was green,
and `prompts/CLAN.md` kept promising leaders a capability that no longer had a
producer. So the assertions below deliberately cross the boundaries that the
unit tests do not: advertised → executor → memory → kick clock.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import agent.tool_defs as tool_defs
import agent.tool_exec as tool_exec
import db
from engine.management import _has_leadership_hold

TAG = "#LOAMEM"

# Relative, not a literal. The hold is compared against SQLite's julianday('now'),
# which no test clock can freeze, so a hardcoded date silently becomes a PAST date
# and this assertion flips to False on that day — a green test that rots into a red
# one with no code change. See elixir-game-mode-tests-flake-nightly.
_AWAY_UNTIL = (date.today() + timedelta(days=1)).isoformat()


@pytest.fixture
def memdb(tmp_path, monkeypatch):
    """Route every db.get_connection() to one temp file (see
    tests/test_awareness_write_tools.py — managed_connection opens a fresh
    connection per call, so an in-memory DB is closed after the first)."""
    db_path = str(tmp_path / "elixir_test.db")
    original_get = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: original_get(db_path))
    setup_conn = original_get(db_path)
    try:
        yield setup_conn
    finally:
        setup_conn.close()


def _advertised() -> dict[str, dict]:
    return {t["name"]: t for t in tool_defs.TOOLS}


def _accepts_away_until(tool: dict) -> bool:
    return "away_until" in tool["input_schema"]["properties"]


def _call(name: str, arguments: dict) -> dict:
    """_execute_tool returns a JSON string, not a dict."""
    return json.loads(tool_exec._execute_tool(name, arguments, workflow="awareness"))


def test_a_hold_can_be_written_by_an_advertised_tool():
    """The capability must live on a tool the model is actually offered.

    Not "an executor exists" — legacy executors stay registered for persisted
    traces, which is exactly why the last break passed every unit test.
    """
    holders = [name for name, tool in _advertised().items() if _accepts_away_until(tool)]
    assert holders, (
        "No advertised tool accepts away_until, so nothing can pause a member's "
        "kick clock — while prompts/CLAN.md still promises leaders a hold."
    )


def test_the_prompt_promise_has_a_producer():
    """CLAN.md ships on every call and tells leaders holds exist. If the tool
    surface cannot deliver one, the model confirms something untrue."""
    from pathlib import Path

    clan_md = Path(__file__).resolve().parents[1] / "prompts" / "CLAN.md"
    promises_hold = "hold" in clan_md.read_text(encoding="utf-8").lower()
    if promises_hold:
        assert any(_accepts_away_until(t) for t in _advertised().values()), (
            "CLAN.md promises a leave-of-absence hold that no advertised tool can record"
        )


def test_hold_pauses_the_kick_clock_end_to_end(memdb):
    """The whole chain: tool call → `Hold:` memory → engine guard sees it."""
    result = _call(
        "record_leadership_followup",
        {
            "topic": "Away next week",
            "recommendation": "Told leaders he's travelling; back the 3rd.",
            "member_tag": TAG,
            "away_until": _AWAY_UNTIL,
        },
    )
    assert result.get("success"), result
    assert result["type"] == "leave_hold"
    assert result["hold_until"].startswith(_AWAY_UNTIL)

    # The engine guard is the consumer that matters — it matches the `Hold:`
    # title prefix, not `Followup:` and not `Watch:`.
    assert _has_leadership_hold(TAG) is True


def test_a_plain_followup_does_not_pause_the_clock(memdb):
    """Grace is only for a member who told leaders they'd be away. Someone
    merely quiet must stay on the clock."""
    result = _call(
        "record_leadership_followup",
        {
            "topic": "Gone quiet",
            "recommendation": "No word from him, watch it.",
            "member_tag": TAG,
        },
    )
    assert result.get("success"), result
    assert result["type"] == "followup"
    assert _has_leadership_hold(TAG) is False


def test_an_unreadable_date_is_refused_not_stored(memdb):
    """A hold that cannot be compared as a time protects nobody. Refusing is
    the only honest answer — storing it told the leader the leave was recorded
    while the member stayed fully exposed."""
    result = _call(
        "record_leadership_followup",
        {
            "topic": "Away",
            "recommendation": "Back soon.",
            "member_tag": TAG,
            "away_until": "next week sometime",
        },
    )
    assert result.get("error") == "invalid_away_until"
    assert _has_leadership_hold(TAG) is False


def test_a_hold_needs_a_member(memdb):
    result = _call(
        "record_leadership_followup",
        {"topic": "Away", "recommendation": "Someone is out.", "away_until": "2026-08-03"},
    )
    assert result.get("error") == "away_until_requires_member"
