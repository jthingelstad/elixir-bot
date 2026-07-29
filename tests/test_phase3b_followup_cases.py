"""A followup opens a decision case ONLY when it becomes a #actions card.

Phase 3b originally made `record_leadership_followup` always open a decision
case. For the member-review path (`case_type` + `member_tag`) that is right — the
case backs a card a leader decides. For the generic paths it was not: those cases
were invisible (no card, no post; the only surface was an unlabelled Observatory
count) and immortal (`leadership_followup` is absent from CASE_TYPES, the set
every reconciler iterates, so nothing could ever close one).

The brain also READS open cases each tick, so it saw its own stale pile and could
only respond by filing another — 3 of the 9 ever created were Elixir reporting on
the staleness of the other 6. It cost a real report: case #376 correctly diagnosed
the stale-role bug four days before it caused damage, and nobody saw it.

The observation is still recorded durably as a memory. What is gone is the false
promise of an open work item nobody owns.
"""

from __future__ import annotations

import db
from agent.tool_exec import (
    _execute_flag_member_watch,
    _execute_record_leadership_followup,
)


def test_generic_followup_records_memory_but_opens_no_case():
    result = _execute_record_leadership_followup(
        {
            "topic": "Week 3 war deck check",
            "recommendation": "Ask Gareth to fix his war deck before Saturday.",
            "member_tag": "#ABC123",
        }
    )
    assert result["success"] is True
    assert result.get("memory_id")  # the observation is still durable
    assert result.get("case_id") is None
    assert result["escalated"] is False  # and it says so plainly
    assert "note" in result  # telling the model how to actually reach a human


def test_operation_followup_without_member_opens_no_case():
    """The shape that produced all 9 orphans: no member, no case_type."""
    before = len(db.list_decision_cases(limit=100))
    result = _execute_record_leadership_followup(
        {
            "topic": "recruiting funnel slow",
            "recommendation": "Post fresh recruiting copy this week.",
        }
    )
    assert result["escalated"] is False
    assert result.get("case_id") is None
    assert len(db.list_decision_cases(limit=100)) == before


def test_followup_with_case_type_still_routes_to_member_review():
    """The one path that reaches a leader must keep working."""
    result = _execute_record_leadership_followup(
        {
            "topic": "promotion review for Gareth",
            "recommendation": "Promote Gareth to Elder; consistent war participation.",
            "member_tag": "#GARETH1",
            "case_type": "promotion_review",
        }
    )
    assert result["escalated"] is True
    case = db.get_decision_case(result["case_key"])
    assert case["case_type"] == "promotion_review"
    assert case["case_key"].startswith("promotion_review:member:")


def test_case_type_without_member_does_not_escalate():
    """A member review with no member cannot become a card, so it must not
    pretend to have escalated."""
    result = _execute_record_leadership_followup(
        {
            "topic": "someone should be promoted",
            "recommendation": "Review the elder band.",
            "case_type": "promotion_review",
        }
    )
    assert result["escalated"] is False
    assert result.get("case_id") is None


def test_no_leadership_followup_case_can_be_created():
    """Regression guard on the type itself: `leadership_followup` is not in
    CASE_TYPES, so any row of that type is unclosable by construction."""
    for args in (
        {"topic": "a", "recommendation": "x", "member_tag": "#NOPE1"},
        {"topic": "b", "recommendation": "y"},
    ):
        _execute_record_leadership_followup(args)
    assert db.list_decision_cases(case_type="leadership_followup", limit=20) == []


def test_flag_member_watch_default_is_memory_only():
    result = _execute_flag_member_watch(
        {
            "member_tag": "#WATCH1",
            "reason": "Quiet for 3 days, keep an eye out.",
        }
    )
    assert result["success"] is True
    assert result.get("memory_id")
    assert "case_id" not in result  # a plain watch is an annotation, not a case


def test_flag_member_watch_rejects_legacy_case_type_and_opens_no_case():
    """Member watches are private state; followups exclusively own review cards."""
    before = len(db.list_decision_cases(limit=100))
    result = _execute_flag_member_watch(
        {
            "member_tag": "#WATCH2",
            "reason": "No battles in 9 days; over threshold.",
            "case_type": "inactivity_review",
        }
    )
    assert result["error"] == "unsupported_case_type"
    assert "record_leadership_followup" in result["detail"]
    assert "case_id" not in result
    assert len(db.list_decision_cases(limit=100)) == before


def test_unknown_case_type_is_refused_at_the_storage_boundary():
    """Only a type some reconciler OWNS may become a case.

    `leadership_followup` was the counter-example: never in CASE_TYPES, so no
    reconciler iterated it and every row stayed open forever while being
    invisible to leadership. The tool schemas constrain case_type to an enum, but
    that is enforced by the model API — this keeps the invariant true for any
    caller, so an unclosable case cannot be created by construction.
    """
    import pytest

    for bad in ("leadership_followup", "kick_review", "war_readiness_review", ""):
        with pytest.raises(ValueError):
            db.upsert_member_review_case(case_type=bad, member={"tag": "#GUARD1", "name": "Guard"})


def test_known_case_types_are_still_accepted():
    from storage.cases import CASE_TYPES

    for good in sorted(CASE_TYPES):
        case = db.upsert_member_review_case(
            case_type=good, member={"tag": f"#OK{abs(hash(good)) % 9999}", "name": "Ok"}
        )
        assert case is not None
        assert case["case_type"] == good
