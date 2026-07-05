"""Clan-management rules (management.md, ratified 2026-07-03): hysteresis,
promote path, kick path with guards."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from engine import management
from engine.management import advance_layer1

NOW_DT = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-01T12:00:00Z"


def test_ratified_constants():
    assert not hasattr(management, "DONOR_WEEK_MIN")  # removed: donor is median-relative
    assert management.WAR_QUALIFY_RATE == 0.75
    assert management.BATTLE_DAYS_MIN == 8
    assert management.PROMOTE_TENURE_MIN == 28
    assert management.PROMOTE_QUALIFYING_WEEKS == 4
    assert management.DEMOTE_WEEKS == 4
    assert management.KICK_CONFIRM_DAYS == 7
    assert management.NEW_MEMBER_GRACE == 14


def test_one_good_week_never_holding():
    assert advance_layer1("none", [True]) == "building"
    assert advance_layer1("building", [True]) == "building"  # window < 4


def test_three_of_four_reaches_holding():
    assert advance_layer1("building", [True, True, False, True]) == "holding"


def test_two_of_four_stays_building():
    assert advance_layer1("building", [True, False, False, True]) == "building"


def test_holding_breaks_only_at_one_of_four():
    assert advance_layer1("holding", [False, True, False, True]) == "holding"
    assert advance_layer1("holding", [False, False, True, False]) == "lapsed"


def test_lapsed_reenters_on_next_qualifying():
    assert advance_layer1("lapsed", [False, False, False, True]) == "building"
    assert advance_layer1("lapsed", [False, False, False, False]) == "lapsed"


def test_skipped_weeks_excluded_from_window():
    # training-only war weeks are None — not failures
    assert advance_layer1("holding", [None, True, True, None, False, True]) == "holding"


def test_sustained_donor_is_roster_median_relative():
    import db

    conn = db.get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, "
                     "last_seen_at, is_home) VALUES ('#J2RGCRVG','POAP KINGS','2026-02-04',?,1)", (NOW,))
        # Roster donations for the closed Sunday: 0, 100, 200, 300, 400 → median 200.
        for i, don in enumerate((0, 100, 200, 300, 400)):
            tag = f"#D{i}"
            conn.execute("INSERT INTO players (player_tag, current_name, first_seen_at, "
                         "last_seen_at) VALUES (?,?,?,?)", (tag, f"D{i}", NOW, NOW))
            conn.execute("INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, "
                         "join_source) VALUES (?, '#J2RGCRVG', '2026-06-01', 'test')", (tag,))
            conn.execute("INSERT INTO player_daily_metrics (player_tag, metric_date, "
                         "donations_week) VALUES (?, '2026-06-28', ?)", (tag, don))
        conn.commit()
        anchor = "2026-06-29"
        # below median (100) → not sustained; at median (200) and above (400) → sustained
        assert management._week_qualifies_donor(conn, "#D1", anchor) is False
        assert management._week_qualifies_donor(conn, "#D2", anchor) is True
        assert management._week_qualifies_donor(conn, "#D4", anchor) is True
        # a zero-donor never qualifies even though the freeloaders drag the median down
        assert management._week_qualifies_donor(conn, "#D0", anchor) is False
    finally:
        conn.close()


def _seed_member(conn, tag="#A", role="member", trophies=5000, joined_days_ago=60,
                 last_battle_days_ago=None, now_dt=NOW_DT):
    joined = (now_dt - timedelta(days=joined_days_ago)).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, "
        "is_home) VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)", (NOW,))
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, 'X', ?, ?)", (tag, joined, NOW))
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
        "VALUES (?, ?, 'test')", (tag, joined))
    conn.execute(
        "INSERT INTO player_current_state (player_tag, observed_at, role, trophies) "
        "VALUES (?, ?, ?, ?)", (tag, NOW, role, trophies))
    conn.execute(
        "INSERT INTO member_management (player_tag, computed_at, week_anchor, "
        "tenure_days, role) VALUES (?, ?, '2026-06-29', ?, ?)",
        (tag, NOW, joined_days_ago, role))
    if last_battle_days_ago is not None:
        bt = (now_dt - timedelta(days=last_battle_days_ago)).strftime("%Y%m%dT%H%M%S.000Z")
        conn.execute(
            "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at) "
            "VALUES (?, ?, ?, ?)", (f"{tag}:{bt}:#OPP", tag, bt, NOW))
    conn.commit()


def _kick_state(conn, tag="#A"):
    return conn.execute(
        "SELECT kick_state FROM member_management WHERE player_tag=?", (tag,)
    ).fetchone()["kick_state"]


def test_kick_watch_at_three_days(engine_conn):
    _seed_member(engine_conn, last_battle_days_ago=4)
    management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn) == "watch"


def test_kick_at_risk_trophy_scaled(engine_conn):
    # 5000 trophies → threshold max(7, 5*1.4)=7 days; 8 days idle → at_risk
    _seed_member(engine_conn, trophies=5000, last_battle_days_ago=8)
    management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn) == "at_risk"
    # 10000 trophies → 14 days; 8 days idle → still watch
    _seed_member(engine_conn, tag="#B", trophies=10000, last_battle_days_ago=8)
    management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn, "#B") == "watch"


def test_kick_recommended_fires_transition(engine_conn):
    # past at_risk threshold (7d) + KICK_CONFIRM_DAYS (7) → 15 days idle
    _seed_member(engine_conn, trophies=5000, last_battle_days_ago=15)
    transitions = management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn) == "recommended"
    assert any(t["player_tag"] == "#A" for t in transitions)
    # transition fires once — a second tick does not re-fire
    again = management.run_tick_evaluators(engine_conn, now=NOW)
    assert not any(t.get("player_tag") == "#A" for t in again)


def test_any_battle_resets_to_none(engine_conn):
    _seed_member(engine_conn, trophies=5000, last_battle_days_ago=15)
    management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn) == "recommended"
    bt = (NOW_DT - timedelta(hours=1)).strftime("%Y%m%dT%H%M%S.000Z")
    engine_conn.execute(
        "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at) "
        "VALUES (?, '#A', ?, ?)", (f"#A:{bt}:#OPP2", bt, NOW))
    engine_conn.commit()
    management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn) == "none"


def test_kick_reset_auto_withdraws_open_action(engine_conn):
    from storage.leader_actions import (
        ACTION_REJECTED,
        create_leader_action_recommendation,
    )

    _seed_member(engine_conn, trophies=5000, last_battle_days_ago=15)
    management.run_tick_evaluators(engine_conn, now=NOW)
    create_leader_action_recommendation(
        action_type="kick_recommendation",
        target_player_tag="#A",
        objective="Review kick candidacy for #A",
        rationale="Test kick candidate.",
        conn=engine_conn,
    )
    bt = (NOW_DT - timedelta(hours=1)).strftime("%Y%m%dT%H%M%S.000Z")
    engine_conn.execute(
        "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at) "
        "VALUES (?, '#A', ?, ?)", (f"#A:{bt}:#OPP2", bt, NOW))
    engine_conn.commit()
    management.run_tick_evaluators(engine_conn, now=NOW)

    withdrawn = management.withdraw_stale_actions(engine_conn, now=NOW)
    assert any(w["player_tag"] == "#A" and w["kind"] == "kick" for w in withdrawn)
    row = engine_conn.execute(
        "SELECT status, decision_note FROM leader_action_recommendations "
        "WHERE target_player_tag = '#A' AND action_type = 'kick_recommendation'"
    ).fetchone()
    assert row["status"] == ACTION_REJECTED
    assert "Auto-withdrawn" in row["decision_note"]


def test_new_member_grace_never_past_watch(engine_conn):
    _seed_member(engine_conn, joined_days_ago=10, last_battle_days_ago=10)
    management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn) in ("none", "watch")


def test_elder_never_reactive_recommended(engine_conn):
    _seed_member(engine_conn, role="elder", trophies=5000, last_battle_days_ago=20)
    transitions = management.run_tick_evaluators(engine_conn, now=NOW)
    assert not any(t.get("player_tag") == "#A" for t in transitions)
    assert _kick_state(engine_conn) != "recommended"


def _run_weeks(conn, tag, qualifying_weeks):
    """Drive run_weekly_review over consecutive week anchors with the donor
    metric qualifying (or not) each week."""
    results = []
    for i, qualifies in enumerate(qualifying_weeks):
        anchor_dt = datetime(2026, 5, 4, tzinfo=timezone.utc) + timedelta(weeks=i)
        anchor = anchor_dt.strftime("%Y-%m-%d")
        # donor input: the closed week's Sunday metric row
        sunday = (anchor_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT OR REPLACE INTO player_daily_metrics (player_tag, metric_date, "
            "donations_week) VALUES (?, ?, ?)",
            (tag, sunday, 100 if qualifies else 0))
        # battle-active input: battles spread over the trailing window
        if qualifies:
            for d in range(0, 28, 3):
                bt = (anchor_dt - timedelta(days=d + 1, hours=2)).strftime(
                    "%Y%m%dT%H%M%S.000Z")
                conn.execute(
                    "INSERT OR IGNORE INTO battle_events (dedup_key, player_tag, "
                    "battle_time, observed_at) VALUES (?, ?, ?, ?)",
                    (f"{tag}:{bt}:#O{d}", tag, bt, bt))
        conn.commit()
        # the tick refreshes management inputs (step 4) before reviews run
        from engine import projections
        anchor_iso = anchor_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        projections.refresh_management_inputs(conn, tag, now=anchor_iso)
        conn.commit()
        results.append(management.run_weekly_review(conn, anchor, now=anchor_iso))
    return results


def test_promote_path_four_qualifying_weeks(engine_conn):
    _seed_member(engine_conn, joined_days_ago=120)
    results = _run_weeks(engine_conn, "#A", [True] * 8)
    row = engine_conn.execute(
        "SELECT promote_state, promote_qualifying_weeks FROM member_management "
        "WHERE player_tag='#A'").fetchone()
    assert row["promote_state"] in ("eligible", "recommended")
    assert row["promote_qualifying_weeks"] >= 4
    assert any("#A" in json.dumps(r.get("promote_eligible", [])) for r in results)
    candidate = next(
        item
        for result in results
        for item in result.get("promote_eligible", [])
    )
    assert candidate["player_tag"] == "#A"
    assert candidate.get("rationale")


def test_promote_grace_then_sustained_slippage_withdraws(engine_conn):
    _seed_member(engine_conn, joined_days_ago=240)
    # Build to eligible, survive an isolated miss (hysteresis: Layer-1 holds
    # through one bad week and the battle window spans ~4 weeks), then slip
    # for 5 straight weeks — evaluators lapse, the gate fails twice in a row,
    # and §3.1's auto-withdraw pulls the candidacy.
    results = _run_weeks(engine_conn, "#A",
                         [True] * 6 + [False, True] + [False] * 5)
    assert any(r.get("promote_eligible") for r in results[:8]), \
        "should have reached eligible before the slippage"
    assert any(w.get("kind") == "promote" for r in results
               for w in r.get("withdrawn", [])), "sustained slippage must withdraw"
    row = engine_conn.execute(
        "SELECT promote_state, state_json FROM member_management "
        "WHERE player_tag='#A'").fetchone()
    assert row["promote_state"] not in ("eligible", "recommended"), row["state_json"]


def test_never_battled_member_anchored_on_own_seen_not_epoch(engine_conn):
    # Cold review 2026-07-04 #10: zero-battle members used the SHARED stream
    # epoch as their idle reference — every such member marched toward
    # at_risk in lockstep. Anchor is now max(joined_at, players.last_seen_at).
    # Seed another member's old battle so the stream epoch is 30d ago...
    _seed_member(engine_conn, tag="#EPOCH", last_battle_days_ago=30)
    # ...and a roster-present member (last_seen_at = NOW) with zero battles.
    _seed_member(engine_conn, tag="#NOBATTLES", joined_days_ago=60,
                 last_battle_days_ago=None)
    from engine import management
    management.run_tick_evaluators(engine_conn, now=NOW)
    # Old code: idle-since-epoch (30d) -> at_risk/recommended. New: their own
    # last_seen_at is fresh -> none.
    assert _kick_state(engine_conn, "#NOBATTLES") == "none"
