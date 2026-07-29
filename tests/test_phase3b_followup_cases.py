"""Leadership followups use the action board as their only decision store."""

from __future__ import annotations

import db
from agent.tool_exec import (
    _execute_flag_member_watch,
    _execute_record_leadership_followup,
)


def test_generic_followup_records_memory_without_action():
    before = len(db.list_leader_actions(limit=100))
    result = _execute_record_leadership_followup(
        {
            "topic": "Week 3 war deck check",
            "recommendation": "Ask Gareth to fix their war deck before Saturday.",
            "member_tag": "#ABC123",
        }
    )
    assert result["success"] is True
    assert result.get("memory_id")
    assert result.get("action_id") is None
    assert result["escalated"] is False
    assert "note" in result
    assert len(db.list_leader_actions(limit=100)) == before


def test_followup_with_action_type_creates_visible_action():
    result = _execute_record_leadership_followup(
        {
            "topic": "promotion review for Gareth",
            "recommendation": "Promote Gareth to Elder; consistent war participation.",
            "member_tag": "#GARETH1",
            "action_type": "promotion_recommendation",
        }
    )
    assert result["escalated"] is True
    action = db.get_leader_action_by_id(result["action_id"])
    assert action["action_type"] == "promotion_recommendation"
    assert action["status"] == "proposed"
    assert action["action_key"] == "awareness:followup:promotion_recommendation:#GARETH1"


def test_action_type_without_member_does_not_escalate():
    result = _execute_record_leadership_followup(
        {
            "topic": "someone should be promoted",
            "recommendation": "Review the elder band.",
            "action_type": "promotion_recommendation",
        }
    )
    assert result["escalated"] is False
    assert result.get("action_id") is None


def test_followup_does_not_reopen_decided_action():
    args = {
        "topic": "removal review",
        "recommendation": "Review removal after sustained inactivity.",
        "member_tag": "#ROOK1",
        "action_type": "kick_recommendation",
    }
    first = _execute_record_leadership_followup(args)
    db.decide_leader_action(
        first["action_id"],
        status="rejected",
        discord_user_id=1,
        emoji="❌",
    )

    second = _execute_record_leadership_followup(args)
    assert second["escalated"] is False
    assert second["action_id"] == first["action_id"]
    assert second["action_status"] == "rejected"
    assert db.get_leader_action_by_id(first["action_id"])["status"] == "rejected"


def test_flag_member_watch_is_memory_only_and_rejects_legacy_case_type():
    result = _execute_flag_member_watch(
        {"member_tag": "#WATCH1", "reason": "Quiet for 3 days, keep an eye out."}
    )
    assert result["success"] is True
    assert result.get("memory_id")
    assert "action_id" not in result

    legacy = _execute_flag_member_watch(
        {
            "member_tag": "#WATCH2",
            "reason": "No battles in 9 days; over threshold.",
            "case_type": "inactivity_review",
        }
    )
    assert legacy["error"] == "unsupported_case_type"
    assert "action_type" in legacy["detail"]
