"""Reconcile the decision-case store against clan membership.

The awareness brain and #leader-actions read `decision_cases`. Unlike
`member_management` (which drops a row on leave), that store does NOT self-heal,
so an open kick/promote/demote review for a departed member lingers and
resurfaces as a stale recommendation (observed 2026-07-09 loop #12). The tick's
MANAGE step must close those cases keyed off current membership — and a KICK
(fulfilled kick action) is meaningfully different from an organic leave: a kick
RESOLVES the review (action enacted), a leave DISMISSES it (moot).
"""

from __future__ import annotations

import db
from db import managed_connection
from storage.cases import (
    reconcile_departed_member_cases,
    sync_terminal_leader_action_cases,
    upsert_member_review_case,
)


@managed_connection
def _set_membership(player_tag, *, left_at, conn=None):
    # The reconciler only keys off memberships, but the fixture must still obey
    # the database's identity spine so the global FK invariant remains useful.
    conn.execute(
        "INSERT OR IGNORE INTO clans "
        "(clan_tag,name,first_seen_at,last_seen_at,is_home) "
        "VALUES ('#CLAN','Test Clan','2026-01-01','2026-07-04',0)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO players "
        "(player_tag,current_name,first_seen_at,last_seen_at) "
        "VALUES (?, ?, '2026-01-01', '2026-07-04')",
        (player_tag, player_tag),
    )
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, left_at, join_source) "
        "VALUES (?, '#CLAN', '2026-01-01', ?, 'test')",
        (player_tag, left_at),
    )


@managed_connection
def _action(player_tag, *, action_type, status, decided_at, conn=None):
    conn.execute(
        "INSERT INTO leader_action_recommendations "
        "(action_key, action_type, target_player_tag, status, objective, prompt_text, "
        " proposed_at, decided_at, created_at, updated_at, is_test) "
        "VALUES (?, ?, ?, ?, 'x', 'x', ?, ?, ?, ?, 0)",
        (
            f"{action_type}:{player_tag}:{decided_at}",
            action_type,
            player_tag,
            status,
            decided_at,
            decided_at,
            decided_at,
            decided_at,
        ),
    )


def _done_kick(player_tag, *, decided_at):
    _action(
        player_tag,
        action_type="kick_recommendation",
        status="done",
        decided_at=decided_at,
    )


def _open_case(case_type, tag, name):
    return upsert_member_review_case(
        case_type=case_type,
        member={"tag": tag, "name": name, "days_inactive": 20},
    )


def test_closes_departed_case_keeps_current_member():
    _set_membership("#STAYS1", left_at=None)
    _set_membership("#LEFT01", left_at="2026-07-04T03:12:00")
    _open_case("inactivity_review", "#STAYS1", "Stayer")
    _open_case("inactivity_review", "#LEFT01", "Leaver")

    reconciled = reconcile_departed_member_cases()

    tags = {d["target_player_tag"] for d in reconciled}
    assert "#LEFT01" in tags and "#STAYS1" not in tags
    assert db.get_decision_case("inactivity_review:member:#STAYS1")["status"] == db.CASE_OPEN


def test_organic_leave_is_dismissed_as_member_left():
    _set_membership("#LEFT02", left_at="2026-07-04T03:12:00")
    _open_case("inactivity_review", "#LEFT02", "Leaver")

    out = reconcile_departed_member_cases()

    assert out[0]["outcome"] == "left"
    case = db.get_decision_case("inactivity_review:member:#LEFT02")
    assert case["status"] == db.CASE_DISMISSED
    assert case["resolution"] == "member_left"


def test_departure_with_fulfilled_kick_resolves_as_kicked():
    # A done kick 2 days before leaving → the departure is a kick; the review's
    # recommended action was enacted, so the case RESOLVES (not dismissed).
    _set_membership("#KICK01", left_at="2026-07-04T03:12:00")
    _done_kick("#KICK01", decided_at="2026-07-02T23:15:00")
    _open_case("inactivity_review", "#KICK01", "Kicked")

    out = reconcile_departed_member_cases()

    assert out[0]["outcome"] == "kicked"
    case = db.get_decision_case("inactivity_review:member:#KICK01")
    assert case["status"] == db.CASE_RESOLVED
    assert case["resolution"] == "kicked"


def test_kick_outside_window_is_not_attributed():
    # A done kick long before the leave (>14d) is not the cause of this leave.
    _set_membership("#KICK02", left_at="2026-07-04T03:12:00")
    _done_kick("#KICK02", decided_at="2026-05-01T10:00:00")
    _open_case("inactivity_review", "#KICK02", "OldKick")

    out = reconcile_departed_member_cases()

    assert out[0]["outcome"] == "left"  # stale kick doesn't explain this departure


def test_member_with_no_membership_row_is_dismissed():
    _open_case("promotion_review", "#GHOST1", "Ghost")

    out = reconcile_departed_member_cases()

    assert "#GHOST1" in {d["target_player_tag"] for d in out}
    assert db.get_decision_case("promotion_review:member:#GHOST1")["status"] == db.CASE_DISMISSED


def test_covers_all_member_review_case_types():
    _set_membership("#LEFT03", left_at="2026-07-04")
    for ct in (
        "inactivity_review",
        "promotion_review",
        "demotion_review",
        "war_recovery",
    ):
        _open_case(ct, "#LEFT03", "Leaver")

    out = reconcile_departed_member_cases()

    assert {d["case_type"] for d in out} == {
        "inactivity_review",
        "promotion_review",
        "demotion_review",
        "war_recovery",
    }


# ---------------------------------------------------------------------------
# sync_terminal_leader_action_cases — a done/rejected action closes its case
# at decision time, before the member ever leaves the roster.
# ---------------------------------------------------------------------------


def test_done_kick_resolves_inactivity_case_while_member_present():
    # Member is still in the clan; the leader marked the kick done.
    _set_membership("#DONEK1", left_at=None)
    _done_kick("#DONEK1", decided_at="2026-07-02T23:15:00")
    _open_case("inactivity_review", "#DONEK1", "DoneKick")

    out = sync_terminal_leader_action_cases()

    assert {c["target_player_tag"] for c in out} == {"#DONEK1"}
    assert out[0]["outcome"] == "accepted"
    assert db.get_decision_case("inactivity_review:member:#DONEK1")["status"] == db.CASE_RESOLVED


def test_done_promotion_resolves_promotion_case():
    _set_membership("#PROMO1", left_at=None)
    _action(
        "#PROMO1",
        action_type="promotion_recommendation",
        status="done",
        decided_at="2026-07-02T10:00:00",
    )
    _open_case("promotion_review", "#PROMO1", "Promoted")

    out = sync_terminal_leader_action_cases()

    assert db.get_decision_case("promotion_review:member:#PROMO1")["status"] == db.CASE_RESOLVED
    assert out[0]["case_type"] == "promotion_review"


def test_rejected_action_dismisses_case():
    _action(
        "#REJ1",
        action_type="kick_recommendation",
        status="rejected",
        decided_at="2026-07-02T10:00:00",
    )
    _open_case("inactivity_review", "#REJ1", "Rejected")

    sync_terminal_leader_action_cases()

    assert db.get_decision_case("inactivity_review:member:#REJ1")["status"] == db.CASE_DISMISSED


def test_sync_never_creates_a_case():
    # A done action with NO backing case must not conjure one (resolve-only).
    _done_kick("#NOCASE", decided_at="2026-07-02T10:00:00")

    out = sync_terminal_leader_action_cases()

    assert out == []
    assert db.get_decision_case("inactivity_review:member:#NOCASE") is None


def test_sync_is_idempotent():
    _done_kick("#IDEM1", decided_at="2026-07-02T10:00:00")
    _open_case("inactivity_review", "#IDEM1", "Idem")

    first = sync_terminal_leader_action_cases()
    second = sync_terminal_leader_action_cases()

    assert len(first) == 1 and second == []  # already closed → skipped
