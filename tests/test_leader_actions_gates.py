"""The gates that decide whether a leader is asked something twice, or at all.

`storage/leader_actions.py` is the largest module in `storage/` (1,785 lines)
and drives every promote / demote / kick / departure card that reaches a real
person. A 2026-08-06 audit found ten of its functions with no test coverage at
all; these are the ones where a bug reaches a member rather than a dashboard.

What each guards, and what breaking it looks like:

* **`has_recent_leader_action`** is the dedup gate. Return False when it should
  be True and leadership gets the same ask twice; return True when it should be
  False and a real departure never gets verified — which means no farewell, on a
  surface where silence is indistinguishable from neglect.
* **`auto_withdraw_leader_actions`** is the structural auto-withdraw guarantee:
  any transition *out* of a recommending state pulls the open card. Break it and
  the board accumulates asks about things that are no longer true, which is
  exactly how a leader learns to stop trusting the board.
* **`set_leader_action_premise`** returns the PRIOR values so an Undo can
  restore them. Its `premise_rejected` flag is read by the engine's
  re-nomination filter, so a wrong value silently re-cards a member whose
  premise leadership already rejected.

None of these have a symptom when they fail. That is the argument for testing
them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from storage import leader_actions as la

NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _card(conn, **kw):
    """One card on the board. Defaults to a kick recommendation for #AAA."""
    kw.setdefault("action_type", "kick_recommendation")
    kw.setdefault("objective", "remove an idle member")
    kw.setdefault("target_player_tag", "#AAA")
    return la.create_leader_action_recommendation(conn=conn, **kw)


# ------------------------------------------------------------ the dedup gate


def test_a_fresh_card_suppresses_a_duplicate_ask(engine_conn):
    _card(engine_conn)
    assert la.has_recent_leader_action(
        action_type="kick_recommendation", target_player_tag="#AAA", conn=engine_conn
    )


def test_the_gate_is_scoped_to_the_member_and_the_action(engine_conn):
    """A card about one member must not suppress a card about another — the
    failure mode is a member who is never actioned because someone else was."""
    _card(engine_conn, target_player_tag="#AAA")
    for kwargs in (
        {"action_type": "kick_recommendation", "target_player_tag": "#BBB"},
        {"action_type": "promotion_recommendation", "target_player_tag": "#AAA"},
    ):
        assert not la.has_recent_leader_action(conn=engine_conn, **kwargs), kwargs


def test_tags_are_canonicalised_before_matching(engine_conn):
    """`#aaa`, `aaa` and `#AAA` are one member. If the gate compared raw text a
    caller passing an un-hashed or lowercase tag would raise a duplicate."""
    _card(engine_conn, target_player_tag="#AAA")
    for variant in ("#AAA", "aaa", "#aaa", " #AAA "):
        assert la.has_recent_leader_action(
            action_type="kick_recommendation", target_player_tag=variant, conn=engine_conn
        ), variant


def test_freshness_follows_expires_at_when_it_is_set(engine_conn):
    """The gate has TWO freshness rules and picks between them on whether
    `expires_at` is set. An expired card must stop suppressing, however recently
    it was proposed — otherwise a deliberately short-lived card blocks its own
    replacement for the whole default window."""
    _card(engine_conn, target_player_tag="#EXP", expires_at=_iso(NOW - timedelta(hours=1)))
    assert not la.has_recent_leader_action(
        action_type="kick_recommendation", target_player_tag="#EXP", conn=engine_conn
    ), "an expired card must not suppress a new one"

    _card(engine_conn, target_player_tag="#LIVE", expires_at=_iso(NOW + timedelta(hours=1)))
    assert la.has_recent_leader_action(
        action_type="kick_recommendation", target_player_tag="#LIVE", conn=engine_conn
    )


def test_freshness_falls_back_to_the_window_when_expires_at_is_null(engine_conn):
    """With no expiry the gate uses `proposed_at >= cutoff`. Verified by asking
    with a window narrow enough to exclude a card that is genuinely there."""
    _card(engine_conn, target_player_tag="#OLD")
    assert la.has_recent_leader_action(
        action_type="kick_recommendation",
        target_player_tag="#OLD",
        within_hours=168,
        conn=engine_conn,
    )
    # Age the card past the window. (`within_hours=0` is NOT "match nothing":
    # the comparison is `>=`, so a card proposed this second still matches.)
    engine_conn.execute(
        "UPDATE leader_action_recommendations SET proposed_at = ? WHERE target_player_tag = ?",
        (_iso(NOW - timedelta(hours=200)), "#OLD"),
    )
    assert not la.has_recent_leader_action(
        action_type="kick_recommendation",
        target_player_tag="#OLD",
        within_hours=168,
        conn=engine_conn,
    ), "a card older than the window must stop suppressing"


def test_test_cards_never_suppress_a_real_ask(engine_conn):
    """`is_test` rows are excluded by `COALESCE(is_test, 0) = 0`. A rehearsal
    card silently blocking a real one would be the worst kind of test pollution
    — it costs a member their farewell and leaves no trace."""
    _card(engine_conn, target_player_tag="#SIM", is_test=True)
    assert not la.has_recent_leader_action(
        action_type="kick_recommendation", target_player_tag="#SIM", conn=engine_conn
    )


def test_the_objective_narrows_the_gate_when_given(engine_conn):
    _card(engine_conn, target_player_tag="#OBJ", objective="remove an idle member")
    assert la.has_recent_leader_action(
        action_type="kick_recommendation",
        target_player_tag="#OBJ",
        objective="remove an idle member",
        conn=engine_conn,
    )
    assert not la.has_recent_leader_action(
        action_type="kick_recommendation",
        target_player_tag="#OBJ",
        objective="something else entirely",
        conn=engine_conn,
    )


# -------------------------------------------------------- the auto-withdraw


def test_auto_withdraw_closes_the_open_card(engine_conn):
    """The structural guarantee: when the state machine stops supporting a
    recommendation, the ask comes off the board."""
    card = _card(engine_conn, target_player_tag="#WD")
    n = la.auto_withdraw_leader_actions(
        action_type="kick_recommendation",
        target_player_tag="#WD",
        reason="member battled again",
        conn=engine_conn,
    )
    assert n == 1
    row = engine_conn.execute(
        "SELECT status, decision_note FROM leader_action_recommendations WHERE action_id = ?",
        (card["action_id"],),
    ).fetchone()
    assert row["status"] == "rejected", "withdrawn cards land in the board's terminal status"
    assert "battled again" in (row["decision_note"] or ""), (
        "the note must say the SYSTEM withdrew it, not that a leader declined it"
    )
    # Subtlety worth pinning: withdrawing does NOT reopen the dedup gate.
    # `has_recent_leader_action` filters on freshness and is deliberately blind
    # to status — it answers "was this asked recently", not "is there an open
    # card". Re-asking is governed by the re-nomination cooldown instead. A
    # future reader assuming otherwise would remove the cooldown and re-card a
    # member the next tick.
    assert la.has_recent_leader_action(
        action_type="kick_recommendation", target_player_tag="#WD", conn=engine_conn
    ), "the gate is status-blind by design"


def test_auto_withdraw_refuses_to_run_unscoped(engine_conn):
    """Missing a type or a tag returns 0 rather than matching broadly. An
    unscoped withdraw would silently clear the whole board."""
    _card(engine_conn, target_player_tag="#KEEP")
    for kwargs in (
        {"action_type": "", "target_player_tag": "#KEEP"},
        {"action_type": "kick_recommendation", "target_player_tag": None},
    ):
        assert la.auto_withdraw_leader_actions(reason="nope", conn=engine_conn, **kwargs) == 0, (
            kwargs
        )
    assert la.has_recent_leader_action(
        action_type="kick_recommendation", target_player_tag="#KEEP", conn=engine_conn
    ), "the untouched card must still be there"


def test_auto_withdraw_leaves_a_decided_card_alone(engine_conn):
    """A leader's decision is not the system's to overwrite."""
    card = _card(engine_conn, target_player_tag="#DONE")
    la.decide_leader_action(
        card["action_id"], status="done", discord_user_id="123", conn=engine_conn
    )
    assert (
        la.auto_withdraw_leader_actions(
            action_type="kick_recommendation",
            target_player_tag="#DONE",
            reason="state changed",
            conn=engine_conn,
        )
        == 0
    )
    row = engine_conn.execute(
        "SELECT status FROM leader_action_recommendations WHERE action_id = ?",
        (card["action_id"],),
    ).fetchone()
    assert row["status"] == "done"


# ------------------------------------------------------------- the premise


def test_setting_the_premise_returns_the_prior_state_for_undo(engine_conn):
    """The return value IS the undo record. If it reported the NEW values, an
    undo would restore the thing it was meant to reverse."""
    card = _card(engine_conn, target_player_tag="#PREM")
    prior = la.set_leader_action_premise(
        card["action_id"], rejected=True, fingerprint="fp-1", conn=engine_conn
    )
    assert prior == {"premise_rejected": False, "premise_fingerprint": None}

    prior2 = la.set_leader_action_premise(
        card["action_id"], rejected=False, fingerprint=None, conn=engine_conn
    )
    assert prior2 == {"premise_rejected": True, "premise_fingerprint": "fp-1"}, (
        "the second call must report what the first one set"
    )
    row = engine_conn.execute(
        "SELECT premise_rejected, premise_fingerprint FROM leader_action_recommendations "
        "WHERE action_id = ?",
        (card["action_id"],),
    ).fetchone()
    assert not row["premise_rejected"] and row["premise_fingerprint"] is None


def test_setting_the_premise_on_a_missing_card_is_none_not_a_crash(engine_conn):
    assert (
        la.set_leader_action_premise(999_999, rejected=True, fingerprint="fp", conn=engine_conn)
        is None
    )


@pytest.mark.parametrize("rejected", [True, False])
def test_the_premise_flag_round_trips(engine_conn, rejected):
    """`premise_rejected` is read by the engine's re-nomination filter, so a
    value that does not persist re-cards a member whose premise leadership
    already rejected."""
    card = _card(engine_conn, target_player_tag=f"#RT{int(rejected)}")
    la.set_leader_action_premise(
        card["action_id"], rejected=rejected, fingerprint="fp", conn=engine_conn
    )
    row = engine_conn.execute(
        "SELECT premise_rejected FROM leader_action_recommendations WHERE action_id = ?",
        (card["action_id"],),
    ).fetchone()
    assert bool(row["premise_rejected"]) is rejected
