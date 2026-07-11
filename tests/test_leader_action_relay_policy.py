"""Awareness relays opt out of the earned-frequency decline throttle.

The throttle is an old-engine artifact meant for recurring management nudges.
The awareness brain's relays are curated per-post, so a leader declining a
copy/paste card is a routing choice, not a signal to post less. Regression
guard for the loops #42/#44 catch-22 where good brain relays were blocked by
the retired auto-relay's inherited decline history.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from runtime.leader_action_policy import can_post_leader_action

_FMT = "%Y-%m-%dT%H:%M:%S"


def _ts(dt: datetime) -> str:
    return dt.replace(tzinfo=None).strftime(_FMT)


def _seed_declined_relays(conn, *, done: int, rejected: int) -> None:
    """Seed decided in_game_relay cards so the earned-frequency gate would fire:
    decided >= MIN_DECIDED, decline_rate >= threshold, and a recent action."""
    now = datetime.now(timezone.utc)
    rows = [("done",)] * done + [("rejected",)] * rejected
    for i, (status,) in enumerate(rows):
        stamp = _ts(now - timedelta(minutes=i + 1))
        conn.execute(
            "INSERT INTO leader_action_recommendations "
            "(action_key, action_type, objective, status, prompt_text, "
            " proposed_at, decided_at, created_at, updated_at, is_test) "
            "VALUES (?, 'in_game_relay', ?, ?, 'x', ?, ?, ?, ?, 0)",
            (f"seed-{i}", f"obj-{i}", status, stamp, stamp, stamp, stamp),
        )
    conn.commit()


def test_relay_throttle_fires_by_default(engine_conn):
    # 2 done / 4 rejected -> decline_rate 0.67, decided 6 (>= 5), recent -> gated.
    _seed_declined_relays(engine_conn, done=2, rejected=4)
    allowed, reason = can_post_leader_action(action_type="in_game_relay", conn=engine_conn)
    assert allowed is False
    assert reason and reason.startswith("earned_frequency:in_game_relay")


def test_awareness_relay_bypasses_decline_throttle(engine_conn):
    """throttle_on_decline=False (the awareness relay path) skips the gate even
    when the identical decline history would otherwise block it."""
    _seed_declined_relays(engine_conn, done=2, rejected=4)
    allowed, reason = can_post_leader_action(
        action_type="in_game_relay", throttle_on_decline=False, conn=engine_conn
    )
    assert allowed is True
    assert reason is None


def test_backlog_cap_still_applies_to_awareness_relays(engine_conn):
    """Opting out of the decline throttle must NOT bypass the open-card backlog
    cap — the brain still can't flood an unattended board."""
    now = datetime.now(timezone.utc)
    for i in range(6):  # LEADER_ACTION_OPEN_CARD_CAP defaults to 5
        stamp = _ts(now - timedelta(minutes=i + 1))
        engine_conn.execute(
            "INSERT INTO leader_action_recommendations "
            "(action_key, action_type, objective, status, prompt_text, "
            " proposed_at, created_at, updated_at, is_test) "
            "VALUES (?, 'in_game_relay', ?, 'proposed', 'x', ?, ?, ?, 0)",
            (f"open-{i}", f"obj-{i}", stamp, stamp, stamp),
        )
    engine_conn.commit()
    allowed, reason = can_post_leader_action(
        action_type="in_game_relay", throttle_on_decline=False, conn=engine_conn
    )
    assert allowed is False
    assert reason and reason.startswith("open_card_backlog")
