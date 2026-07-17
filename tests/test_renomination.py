"""Defer retired 2026-07-10 → declining is the only "not now", and the engine
re-nominates on sustained evidence after a cooldown (a decline note like
"revisit in a month" overrides the window). Covers the kick-path sweep
(renominate_after_cooldown), the shared cooldown gate, the decline-note →
expires_at wiring, and that a decline dismisses its backing case.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine import management
from storage import leader_actions as la

NOW = "2026-07-01T12:00:00Z"
NOW_DT = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _stamp(days_ago: int) -> str:
    # Storage-side stamp format (no trailing Z), matching db._utcnow().
    return (NOW_DT - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_recommended(conn, tag="#AAA", name="Idle", kick_state="recommended"):
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (tag, name, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO member_management (player_tag, computed_at, week_anchor, kick_state) "
        "VALUES (?, ?, '2026-06-29', ?)",
        (tag, NOW, kick_state),
    )
    conn.commit()


def _insert_kick_card(
    conn, tag="#AAA", *, status, decided_at=None, expires_at=None, key_suffix="1"
):
    conn.execute(
        "INSERT INTO leader_action_recommendations "
        "(action_key, action_type, objective, status, prompt_text, proposed_at, "
        " created_at, updated_at, target_player_tag, decided_at, expires_at, is_test) "
        "VALUES (?, 'kick_recommendation', ?, ?, 'p', ?, ?, ?, ?, ?, ?, 0)",
        (
            f"kick:{tag}:{status}:{key_suffix}",
            f"Review kick for {tag}",
            status,
            NOW,
            NOW,
            NOW,
            tag,
            decided_at,
            expires_at,
        ),
    )
    conn.commit()


# ----------------------------------------------------- premise rejection (v7)


def _seed_anchor(conn, tag="#AAA", *, days_ago=40, dedup="anchor"):
    """Give the member a stable evidence anchor (their last battle) so the premise
    fingerprint is reproducible."""
    conn.execute(
        "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at) "
        "VALUES (?, ?, ?, ?)",
        (dedup, tag, _stamp(days_ago), _stamp(days_ago)),
    )
    conn.commit()


def _reject_premise(conn, tag="#AAA"):
    """Mark the latest decided kick card premise-rejected, pinning the current
    evidence fingerprint (mirrors engine.leader_note_effects.apply)."""
    fp = management._premise_fingerprint(conn, tag, "kick_recommendation")
    conn.execute(
        "UPDATE leader_action_recommendations "
        "SET premise_rejected = 1, premise_fingerprint = ? "
        "WHERE target_player_tag = ? AND action_type = 'kick_recommendation'",
        (fp, tag),
    )
    conn.commit()
    return fp


def test_premise_rejection_blocks_renomination_while_evidence_unchanged(engine_conn):
    _seed_recommended(engine_conn)
    _seed_anchor(engine_conn)
    # Declined 10 days ago (past the 7d cooldown → would normally re-raise)...
    _insert_kick_card(engine_conn, status="rejected", decided_at=_stamp(10))
    _reject_premise(engine_conn)
    # ...but the leader rejected the premise and the anchor is unchanged → blocked.
    assert management.renominate_after_cooldown(engine_conn, now=NOW) == []


def test_premise_rejection_lifts_on_materially_new_evidence(engine_conn):
    _seed_recommended(engine_conn)
    _seed_anchor(engine_conn)
    _insert_kick_card(engine_conn, status="rejected", decided_at=_stamp(10))
    _reject_premise(engine_conn)
    assert management.renominate_after_cooldown(engine_conn, now=NOW) == []

    # A fresh battle moves the evidence anchor → fingerprint no longer matches →
    # the premise no longer applies and the normal cooldown gate re-raises.
    engine_conn.execute(
        "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at) "
        "VALUES ('b1', '#AAA', ?, ?)",
        (_stamp(1), _stamp(1)),
    )
    engine_conn.commit()
    fired = management.renominate_after_cooldown(engine_conn, now=NOW)
    assert [f["player_tag"] for f in fired] == ["#AAA"]


# --------------------------------------------------------------- kick sweep


def test_declined_kick_renominates_after_cooldown(engine_conn):
    _seed_recommended(engine_conn)
    # Declined 10 days ago; kick cooldown is 7 → past the window, still recommended.
    _insert_kick_card(engine_conn, status="rejected", decided_at=_stamp(10))
    fired = management.renominate_after_cooldown(engine_conn, now=NOW)
    assert [f["player_tag"] for f in fired] == ["#AAA"]
    assert fired[0]["action_type"] == "kick_recommendation"


def test_declined_kick_within_cooldown_not_renominated(engine_conn):
    _seed_recommended(engine_conn)
    _insert_kick_card(engine_conn, status="rejected", decided_at=_stamp(3))  # < 7d
    assert management.renominate_after_cooldown(engine_conn, now=NOW) == []


def test_decline_note_window_overrides_default_cooldown(engine_conn):
    _seed_recommended(engine_conn)
    # Declined 10 days ago (past the 7d default) BUT a note pushed expires_at out
    # 30 days → still inside the leader's requested window, so no re-nomination.
    _insert_kick_card(engine_conn, status="rejected", decided_at=_stamp(10), expires_at=_stamp(-20))
    assert management.renominate_after_cooldown(engine_conn, now=NOW) == []


def test_open_proposed_card_suppresses_renomination(engine_conn):
    _seed_recommended(engine_conn)
    _insert_kick_card(engine_conn, status="rejected", decided_at=_stamp(10), key_suffix="old")
    _insert_kick_card(engine_conn, status="proposed", key_suffix="open")  # already carded
    assert management.renominate_after_cooldown(engine_conn, now=NOW) == []


def test_last_decision_done_is_not_renominated(engine_conn):
    _seed_recommended(engine_conn)
    # Latest decided card was 'done' (kicked/handled) not a decline → nothing to
    # re-raise. An older decline must not resurrect it.
    _insert_kick_card(engine_conn, status="rejected", decided_at=_stamp(30), key_suffix="old")
    _insert_kick_card(engine_conn, status="done", decided_at=_stamp(10), key_suffix="new")
    assert management.renominate_after_cooldown(engine_conn, now=NOW) == []


def test_non_recommended_state_is_not_renominated(engine_conn):
    _seed_recommended(engine_conn, kick_state="at_risk")  # not yet the actionable tier
    _insert_kick_card(engine_conn, status="rejected", decided_at=_stamp(10))
    assert management.renominate_after_cooldown(engine_conn, now=NOW) == []


# ------------------------------------------------ shared cooldown gate (promote)


def test_renomination_blocked_until_gates_promote(engine_conn):
    tag = "#PRO"
    engine_conn.execute(
        "INSERT INTO leader_action_recommendations "
        "(action_key, action_type, objective, status, prompt_text, proposed_at, "
        " created_at, updated_at, target_player_tag, decided_at, is_test) "
        "VALUES ('p1', 'promotion_recommendation', 'o', 'rejected', 'p', ?, ?, ?, ?, ?, 0)",
        (NOW, NOW, NOW, tag, _stamp(5)),
    )
    engine_conn.commit()
    # Promote cooldown is 14d; declined 5 days ago → still blocked.
    blocked = management._renomination_blocked_until(
        engine_conn,
        tag,
        "promotion_recommendation",
        management.PROMOTE_RENOMINATE_COOLDOWN_DAYS,
    )
    assert blocked is not None and blocked > management._iso_naive(NOW)
    # A first-time candidate with no decided card is never blocked.
    assert (
        management._renomination_blocked_until(engine_conn, "#NEW", "promotion_recommendation", 14)
        is None
    )


# ----------------------------------------------- decline note → expires_at (B)


def test_decline_with_revisit_note_sets_expires_window(engine_conn):
    _insert_kick_card(engine_conn, tag="#NOTE", status="proposed", key_suffix="n")
    card = engine_conn.execute(
        "SELECT action_id FROM leader_action_recommendations WHERE target_player_tag='#NOTE'"
    ).fetchone()
    updated = la.decide_leader_action(
        card["action_id"],
        status=la.ACTION_REJECTED,
        discord_user_id=1,
        emoji="❌",
        decision_note="revisit in a month",
        decided_at=_stamp(0),
        conn=engine_conn,
    )
    assert updated["status"] == la.ACTION_REJECTED
    expires = management._iso_naive(updated["expires_at"])
    assert expires is not None
    # ~30 days out (the note window), give or take a day of arithmetic slack.
    assert expires > NOW_DT.replace(tzinfo=None) + timedelta(days=25)


def test_plain_decline_sets_no_suppression_window(engine_conn):
    _insert_kick_card(engine_conn, tag="#PLAIN", status="proposed", key_suffix="p")
    card = engine_conn.execute(
        "SELECT action_id FROM leader_action_recommendations WHERE target_player_tag='#PLAIN'"
    ).fetchone()
    updated = la.decide_leader_action(
        card["action_id"],
        status=la.ACTION_REJECTED,
        discord_user_id=1,
        emoji="❌",
        decision_note="not the right call",
        decided_at=_stamp(0),
        conn=engine_conn,
    )
    assert updated["expires_at"] is None  # default cooldown applies, no note window


# ----------------------------------------------- decline dismisses its case


def test_decline_dismisses_backing_case(engine_conn):
    engine_conn.execute(
        "INSERT INTO decision_cases (case_key, case_type, status, title, "
        " opened_at, created_at, updated_at) "
        "VALUES ('inactivity_review:#CASE', 'inactivity_review', 'open', "
        " 'Inactivity review', ?, ?, ?)",
        (NOW, NOW, NOW),
    )
    case_id = engine_conn.execute(
        "SELECT case_id FROM decision_cases WHERE case_key='inactivity_review:#CASE'"
    ).fetchone()["case_id"]
    engine_conn.execute(
        "INSERT INTO leader_action_recommendations "
        "(action_key, action_type, objective, status, prompt_text, proposed_at, "
        " created_at, updated_at, target_player_tag, case_id, is_test) "
        "VALUES ('k:#CASE', 'kick_recommendation', 'o', 'proposed', 'p', ?, ?, ?, '#CASE', ?, 0)",
        (NOW, NOW, NOW, case_id),
    )
    engine_conn.commit()
    action_id = engine_conn.execute(
        "SELECT action_id FROM leader_action_recommendations WHERE target_player_tag='#CASE'"
    ).fetchone()["action_id"]

    la.decide_leader_action(
        action_id,
        status=la.ACTION_REJECTED,
        discord_user_id=1,
        emoji="❌",
        conn=engine_conn,
    )
    status = engine_conn.execute(
        "SELECT status FROM decision_cases WHERE case_id=?", (case_id,)
    ).fetchone()["status"]
    assert status == "dismissed"
