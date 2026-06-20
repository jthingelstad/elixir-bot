"""Phase 3b — leadership followups always open a durable decision case.

A `record_leadership_followup` is action-oriented by definition, so it now opens
a decision case (the single home for the concern) by default, with the memory as
its annotation. `flag_member_watch` stays memory-only unless a case_type makes it
explicitly action-oriented.
"""
from __future__ import annotations

import db
from agent.tool_exec import _execute_flag_member_watch, _execute_record_leadership_followup


def test_followup_opens_generic_case_by_default():
    result = _execute_record_leadership_followup({
        "topic": "Week 3 war deck check",
        "recommendation": "Ask Gareth to fix his war deck before Saturday.",
        "member_tag": "#ABC123",
    })
    assert result["success"] is True
    assert result.get("memory_id")  # narrative annotation still written
    assert result.get("case_id")

    case = db.get_decision_case(result["case_key"])
    assert case is not None
    assert case["case_type"] == "leadership_followup"
    assert case["target_player_tag"] == "#ABC123"
    assert case["status"] == db.CASE_OPEN


def test_followup_with_case_type_routes_to_member_review():
    result = _execute_record_leadership_followup({
        "topic": "promotion review for Gareth",
        "recommendation": "Promote Gareth to Elder; consistent war participation.",
        "member_tag": "#GARETH1",
        "case_type": "promotion_review",
    })
    case = db.get_decision_case(result["case_key"])
    assert case["case_type"] == "promotion_review"
    assert case["case_key"].startswith("promotion_review:member:")


def test_distinct_topics_open_distinct_cases_for_same_member():
    r1 = _execute_record_leadership_followup({
        "topic": "war deck check", "recommendation": "Fix war deck.", "member_tag": "#DUP1",
    })
    r2 = _execute_record_leadership_followup({
        "topic": "donation slump", "recommendation": "Nudge on donations.", "member_tag": "#DUP1",
    })
    assert r1["case_key"] != r2["case_key"]
    keys = {c["case_key"] for c in db.list_decision_cases(case_type="leadership_followup", limit=20)}
    assert r1["case_key"] in keys and r2["case_key"] in keys


def test_operation_followup_without_member():
    result = _execute_record_leadership_followup({
        "topic": "recruiting funnel slow",
        "recommendation": "Post fresh recruiting copy this week.",
    })
    case = db.get_decision_case(result["case_key"])
    assert case["case_type"] == "leadership_followup"
    assert case["case_key"] == "leadership_followup:recruiting-funnel-slow"
    assert case["subject_type"] == "operation"


def test_flag_member_watch_default_is_memory_only():
    result = _execute_flag_member_watch({
        "member_tag": "#WATCH1",
        "reason": "Quiet for 3 days, keep an eye out.",
    })
    assert result["success"] is True
    assert result.get("memory_id")
    assert "case_id" not in result  # a plain watch is an annotation, not a case


def test_flag_member_watch_with_case_type_opens_case():
    result = _execute_flag_member_watch({
        "member_tag": "#WATCH2",
        "reason": "No battles in 9 days; over threshold.",
        "case_type": "inactivity_review",
    })
    assert result.get("case_id")
    case = db.get_decision_case(result["case_key"])
    assert case["case_type"] == "inactivity_review"
