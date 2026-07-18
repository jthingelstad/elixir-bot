"""Awareness relays opt out of the earned-frequency decline throttle.

The throttle is an old-engine artifact meant for recurring management nudges.
The awareness brain's relays are curated per-post, so a leader declining a
copy/paste card is a routing choice, not a signal to post less. Regression
guard for the loops #42/#44 catch-22 where good brain relays were blocked by
the retired auto-relay's inherited decline history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
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


def _seed_open_cards(conn, count, *, posted=True):
    """Seed `count` proposed cards. Posted cards carry a real source_message_id;
    unposted ones are NULL (queued) — only posted cards are on leaders' board."""
    now = datetime.now(timezone.utc)
    for i in range(count):
        stamp = _ts(now - timedelta(minutes=i + 1))
        conn.execute(
            "INSERT INTO leader_action_recommendations "
            "(action_key, action_type, objective, status, prompt_text, "
            " source_message_id, proposed_at, created_at, updated_at, is_test) "
            "VALUES (?, 'in_game_relay', ?, 'proposed', 'x', ?, ?, ?, ?, 0)",
            (f"open-{i}", f"obj-{i}", (str(100 + i) if posted else None), stamp, stamp, stamp),
        )
    conn.commit()


def test_backlog_cap_still_applies_to_awareness_relays(engine_conn):
    """Opting out of the decline throttle must NOT bypass the open-card backlog
    cap — the brain still can't flood an unattended board (posted cards count)."""
    _seed_open_cards(engine_conn, 6, posted=True)  # cap defaults to 5
    allowed, reason = can_post_leader_action(
        action_type="in_game_relay", throttle_on_decline=False, conn=engine_conn
    )
    assert allowed is False
    assert reason and reason.startswith("open_card_backlog")


def test_unposted_cards_do_not_count_against_backlog(engine_conn):
    """Regression for the 2026-07-18 deadlock: proposed-but-unposted cards (queued,
    or stranded at the POSTING_SENTINEL by a failed post) must NOT count against the
    cap that gates their own posting."""
    _seed_open_cards(engine_conn, 6, posted=False)  # queued, no source_message_id
    # a card mid-post / stranded at the sentinel also must not count
    engine_conn.execute(
        "INSERT INTO leader_action_recommendations "
        "(action_key, action_type, objective, status, prompt_text, "
        " source_message_id, proposed_at, created_at, updated_at, is_test) "
        "VALUES ('stuck', 'in_game_relay', 'obj-stuck', 'proposed', 'x', 'posting', ?, ?, ?, 0)",
        (_ts(datetime.now(timezone.utc)),) * 3,
    )
    engine_conn.commit()
    allowed, reason = can_post_leader_action(
        action_type="in_game_relay", throttle_on_decline=False, conn=engine_conn
    )
    assert allowed is True
    assert reason is None


def test_clear_source_message_resets_sentinel(engine_conn):
    """clear_leader_action_source_message resets the sentinel; the plain
    update helper cannot (it treats None as 'no change') — the trap that
    stranded cards at 'posting' during the outage."""
    now = _ts(datetime.now(timezone.utc))
    engine_conn.execute(
        "INSERT INTO leader_action_recommendations "
        "(action_key, action_type, objective, status, prompt_text, "
        " source_message_id, proposed_at, created_at, updated_at, is_test) "
        "VALUES ('c1', 'in_game_relay', 'o', 'proposed', 'x', 'posting', ?, ?, ?, 0)",
        (now, now, now),
    )
    engine_conn.commit()
    aid = engine_conn.execute(
        "SELECT action_id FROM leader_action_recommendations WHERE action_key='c1'"
    ).fetchone()[0]

    # The plain update helper is a no-op on None — sentinel survives.
    db.update_leader_action_message(aid, source_message_id=None, conn=engine_conn)
    still = engine_conn.execute(
        "SELECT source_message_id FROM leader_action_recommendations WHERE action_id=?", (aid,)
    ).fetchone()[0]
    assert still == "posting"

    # The dedicated clear helper resets it to NULL so the poster retries.
    db.clear_leader_action_source_message(aid, conn=engine_conn)
    cleared = engine_conn.execute(
        "SELECT source_message_id FROM leader_action_recommendations WHERE action_id=?", (aid,)
    ).fetchone()[0]
    assert cleared is None
