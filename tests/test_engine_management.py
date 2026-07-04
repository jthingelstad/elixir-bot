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
    assert management.DONOR_WEEK_MIN == 50
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
