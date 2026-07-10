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
    assert management.PROMOTE_QUALIFYING_WEEKS == 3  # elder band (2026-07-05)
    assert management.DEMOTE_WEEKS == 2               # demotion easier than promotion
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


def _set_war_history(conn, tag, *, fame_avg=None, attendance=None):
    conn.execute(
        "UPDATE member_management SET war_fame_3season_avg = ?, "
        "war_attendance_rate = ? WHERE player_tag = ?",
        (fame_avg, attendance, tag))
    conn.commit()


def test_war_contributor_gets_extended_confirm_window(engine_conn):
    # A plain member at 5000 trophies / 15 days idle → recommended (7+7).
    # A durable war contributor (3-season fame over the bar) gets +7 confirm
    # days, so at 15 days idle they are still only at_risk — the card waits.
    _seed_member(engine_conn, trophies=5000, last_battle_days_ago=15)
    _set_war_history(engine_conn, "#A", fame_avg=7300.0, attendance=0.875)
    management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn) == "at_risk"


def test_war_contributor_still_escalates_after_extended_window(engine_conn):
    # Past the extended threshold (7 at_risk + 14 confirm = 21) the card fires.
    _seed_member(engine_conn, trophies=5000, last_battle_days_ago=22)
    _set_war_history(engine_conn, "#A", fame_avg=7300.0, attendance=0.875)
    transitions = management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn) == "recommended"
    assert any(t["player_tag"] == "#A" for t in transitions)


def test_war_grace_qualifies_on_attendance_alone(engine_conn):
    # Attendance over the bar earns the grace even with modest fame.
    _seed_member(engine_conn, trophies=5000, last_battle_days_ago=15)
    _set_war_history(engine_conn, "#A", fame_avg=500.0, attendance=0.80)
    management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn) == "at_risk"


def test_weak_war_record_does_not_earn_grace(engine_conn):
    # Below both bars → no grace → normal 15-day recommendation still fires.
    _seed_member(engine_conn, trophies=5000, last_battle_days_ago=15)
    _set_war_history(engine_conn, "#A", fame_avg=1200.0, attendance=0.30)
    management.run_tick_evaluators(engine_conn, now=NOW)
    assert _kick_state(engine_conn) == "recommended"


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


def _seed_ranked(conn, tag, *, role="member", tenure=120, donations=0,
                 war_used=0, war_avail=16, war_days=0, name=None,
                 ranked_league=0, ranked_rating=0, last_league=0, last_rating=0):
    """A rankable roster member for elder-band tests: role, tenure, closed-week
    donations (Sun 2026-06-28), and recent war attendance (used/avail split over
    `war_days` days inside the 14-day floor window; anchored ~2026-06-25)."""
    conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, "
                 "last_seen_at, is_home) VALUES ('#J2RGCRVG','POAP KINGS','2026-02-04',?,1)", (NOW,))
    joined = (NOW_DT - timedelta(days=tenure)).strftime("%Y-%m-%d")
    conn.execute("INSERT INTO players (player_tag, current_name, first_seen_at, "
                 "last_seen_at) VALUES (?,?,?,?)", (tag, name or tag, joined, NOW))
    conn.execute("INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
                 "VALUES (?,?, 'test')", (tag, joined))
    conn.execute("INSERT INTO player_current_state (player_tag, observed_at, role, "
                 "trophies, ranked_league, ranked_trophies) VALUES (?,?,?,5000,?,?)",
                 (tag, NOW, role, ranked_league or None, ranked_rating or None))
    if last_league:
        import json as _json
        conn.execute("INSERT INTO state_baselines (entity_kind, entity_tag, aspect, "
                     "payload_json, payload_hash, observed_at) VALUES "
                     "('player', ?, 'ranked', ?, 'h', ?)",
                     (tag, _json.dumps({"league": ranked_league, "trophies": ranked_rating,
                      "last": {"league": last_league, "trophies": last_rating}}), NOW))
    conn.execute("INSERT INTO member_management (player_tag, computed_at, week_anchor, "
                 "tenure_days, role) VALUES (?,?, '2026-06-22', ?, ?)", (tag, NOW, tenure, role))
    if donations:
        conn.execute("INSERT INTO player_daily_metrics (player_tag, metric_date, "
                     "donations_week) VALUES (?, '2026-06-28', ?)", (tag, donations))
    for d in range(war_days):
        obs = (NOW_DT - timedelta(days=6 + d)).strftime("%Y-%m-%dT12:00:00Z")
        conn.execute("INSERT OR IGNORE INTO war_attendance_days (season_id, section_index, "
                     "war_day_index, player_tag, decks_used, decks_available, observed_at) "
                     "VALUES (133, 0, ?, ?, ?, ?, ?)",
                     (d, tag, war_used // max(war_days, 1), war_avail // max(war_days, 1), obs))
    conn.commit()


ANCHOR = "2026-06-29"          # Monday review anchor
ANCHOR_NOW = "2026-06-29T07:00:00Z"


def test_war_floor_and_scores(engine_conn):
    _seed_ranked(engine_conn, "#W", war_used=12, war_avail=16, war_days=3, donations=300)
    _seed_ranked(engine_conn, "#N", war_used=0, war_avail=0, war_days=0, donations=900)
    assert management._passes_war_floor(engine_conn, "#W", ANCHOR_NOW) is True
    assert management._passes_war_floor(engine_conn, "#N", ANCHOR_NOW) is False
    scores = management._elder_scores(engine_conn, ANCHOR_NOW, ANCHOR)
    # #W plays war (rate .75) so despite fewer donations it out-scores the
    # war-absent #N (war weighted 0.65).
    assert scores["#W"]["war_rate"] > 0
    assert scores["#N"]["war_rate"] == 0
    assert scores["#W"]["score"] > scores["#N"]["score"]


def test_ranked_floor_and_prestige(engine_conn):
    # UC member with zero wars passes the competitive floor via ranked;
    # a Master-tier member with zero wars fails both.
    _seed_ranked(engine_conn, "#UC", war_days=0, ranked_league=7, ranked_rating=1867)
    _seed_ranked(engine_conn, "#MAS", war_days=0, ranked_league=2, ranked_rating=600)
    assert management._passes_ranked_floor(engine_conn, "#UC") is True
    assert management._passes_ranked_floor(engine_conn, "#MAS") is False
    assert management._passes_competitive_floor(engine_conn, "#UC", ANCHOR_NOW) is True
    assert management._passes_competitive_floor(engine_conn, "#MAS", ANCHOR_NOW) is False
    assert management._ranked_prestige(7, 1867) > 0.9
    assert management._ranked_prestige(4, 0) == 0.5
    assert management._ranked_prestige(2, 800) == 0.0


def test_competitive_is_max_of_war_and_ranked(engine_conn):
    # A UC grinder with no clan wars is top-tier on the competitive term (via
    # ranked prestige), just like a full-attendance war stalwart (via war
    # percentile) — competitive = max(war_pct, ranked_prestige).
    for i in range(8):                             # filler so percentiles mean something
        _seed_ranked(engine_conn, f"#F{i}", war_used=2, war_avail=16, war_days=1, donations=50)
    _seed_ranked(engine_conn, "#WARDOG", war_used=16, war_avail=16, war_days=4, donations=200)
    _seed_ranked(engine_conn, "#UCGRIND", war_days=0, ranked_league=7, ranked_rating=1900, donations=200)
    scores = management._elder_scores(engine_conn, ANCHOR_NOW, ANCHOR)
    assert scores["#UCGRIND"]["war_rate"] == 0
    # max picked ranked prestige, not the near-zero war percentile
    assert scores["#UCGRIND"]["competitive"] >= 0.9
    assert scores["#UCGRIND"]["competitive"] > scores["#UCGRIND"]["war_pct"]
    # the war stalwart is also top-tier on the competitive term
    assert scores["#WARDOG"]["competitive"] >= 0.85


def test_uc_elder_no_wars_is_protected(engine_conn):
    # The OllieTurtle case: UC elder, zero clan-war decks → passes competitive
    # floor via ranked → NOT demotable. A no-war no-ranked elder IS.
    for i in range(11):
        _seed_ranked(engine_conn, f"#P{i}", war_used=8, war_avail=16, war_days=2, donations=200)
    _seed_ranked(engine_conn, "#OLLIE", role="elder", war_days=0,
                 ranked_league=7, ranked_rating=1867, donations=193)
    _seed_ranked(engine_conn, "#DEADBEAT", role="elder", war_days=0,
                 ranked_league=0, donations=100)
    scores, band = _band(engine_conn)
    assert "#OLLIE" not in band["demotable"]       # UC protects the ranked grinder
    assert "#DEADBEAT" in band["demotable"]         # both floors failed


def _band(conn):
    scores = management._elder_scores(conn, ANCHOR_NOW, ANCHOR)
    return scores, management._elder_band(conn, scores, ANCHOR_NOW)


def test_below_floor_promotes_top_worthy_only(engine_conn):
    # 7-member roster, 0 elders → floor round(.15*7)=1. Top worthy member is
    # promotable; a war-absent member never is (fails the floor).
    _seed_ranked(engine_conn, "#TOP", war_used=16, war_avail=16, war_days=4, donations=800)
    for i in range(5):
        _seed_ranked(engine_conn, f"#M{i}", war_used=4, war_avail=16, war_days=2, donations=100)
    _seed_ranked(engine_conn, "#NOWAR", war_used=0, war_avail=0, war_days=0, donations=999)
    scores, band = _band(engine_conn)
    assert band["floor"] == 1 and band["current_elders"] == 0
    assert "#TOP" in band["promotable"]
    assert "#NOWAR" not in band["promotable"]      # war floor filter
    assert len(band["promotable"]) == 1            # only need floor-0 = 1


def test_worthiness_floor_blocks_weak_promotion(engine_conn):
    # Below floor, but the only tenure-eligible members rank below the roster
    # median (young high-war stars, still inside the 28-day tenure filter,
    # dominate the top and lift the median). Nobody worthy → promote no one.
    for i in range(5):                             # young stars: fail tenure filter
        _seed_ranked(engine_conn, f"#Y{i}", tenure=10, war_used=15, war_avail=16,
                     war_days=3, donations=700 + i)
    for i in range(2):                             # eligible veterans, weak output
        _seed_ranked(engine_conn, f"#V{i}", tenure=200, war_used=2, war_avail=16,
                     war_days=1, donations=20)
    scores, band = _band(engine_conn)
    assert band["current_elders"] < band["floor"]
    # veterans pass filters but score below median → worthiness blocks them
    assert all(scores[t]["score"] < band["median"] for t in ("#V0", "#V1"))
    assert band["promotable"] == set()


def test_inside_band_no_moves(engine_conn):
    # 13 members, 2 elders → floor round(1.95)=2, ceil round(2.6)=3: in band.
    for i in range(11):
        _seed_ranked(engine_conn, f"#P{i}", war_used=8, war_avail=16, war_days=2, donations=200)
    _seed_ranked(engine_conn, "#E0", role="elder", war_used=14, war_avail=16, war_days=3, donations=400)
    _seed_ranked(engine_conn, "#E1", role="elder", war_used=13, war_avail=16, war_days=3, donations=380)
    scores, band = _band(engine_conn)
    assert band["floor"] <= band["current_elders"] <= band["ceil"]
    assert band["promotable"] == set()
    assert band["demotable"] == set()


def test_elder_failing_war_floor_is_demotable(engine_conn):
    for i in range(11):
        _seed_ranked(engine_conn, f"#P{i}", war_used=8, war_avail=16, war_days=2, donations=200)
    _seed_ranked(engine_conn, "#EGOOD", role="elder", war_used=14, war_avail=16, war_days=3, donations=400)
    _seed_ranked(engine_conn, "#EDEAD", role="elder", war_used=0, war_avail=0, war_days=0, donations=900)
    scores, band = _band(engine_conn)
    assert "#EDEAD" in band["demotable"]           # abandoned war duty, any rank
    assert "#EGOOD" not in band["demotable"]


def _topup_week(conn, tag, anchor_dt, war_used, war_avail, donations):
    """Fresh war days (inside the 14-day floor) + closed-Sunday donations for a
    review at anchor_dt, so filters keep passing as the anchor advances."""
    for d in range(3):
        obs = (anchor_dt - timedelta(days=2 + d)).strftime("%Y-%m-%dT12:00:00Z")
        conn.execute("INSERT OR IGNORE INTO war_attendance_days (season_id, section_index, "
                     "war_day_index, player_tag, decks_used, decks_available, observed_at) "
                     "VALUES (140, ?, ?, ?, ?, ?, ?)",
                     (anchor_dt.isocalendar()[1], d, tag, war_used // 3, war_avail // 3, obs))
    sunday = (anchor_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    conn.execute("INSERT OR REPLACE INTO player_daily_metrics (player_tag, metric_date, "
                 "donations_week) VALUES (?, ?, ?)", (tag, sunday, donations))
    conn.commit()


def test_above_ceiling_demotes_lowest_participating(engine_conn):
    # 10 members + 3 elders → N=13, ceiling round(2.6)=3. Four participating
    # elders is one over the ceiling → the single lowest-ranked one is demotable.
    for i in range(10):
        _seed_ranked(engine_conn, f"#P{i}", war_used=6, war_avail=16, war_days=2, donations=150)
    _seed_ranked(engine_conn, "#EA", role="elder", war_used=16, war_avail=16, war_days=4, donations=500)
    _seed_ranked(engine_conn, "#EB", role="elder", war_used=15, war_avail=16, war_days=4, donations=480)
    _seed_ranked(engine_conn, "#EC", role="elder", war_used=14, war_avail=16, war_days=4, donations=460)
    _seed_ranked(engine_conn, "#ED", role="elder", war_used=9, war_avail=16, war_days=3, donations=120)
    scores, band = _band(engine_conn)
    assert band["ceil"] == 3 and band["current_elders"] == 4
    assert band["demotable"] == {"#ED"}           # exactly the lowest, back to ceiling


def test_promote_hysteresis_three_weeks(engine_conn):
    _seed_ranked(engine_conn, "#TOP", war_used=16, war_avail=16, war_days=4, donations=800)
    for i in range(5):
        _seed_ranked(engine_conn, f"#M{i}", war_used=4, war_avail=16, war_days=2, donations=100)
    saw_eligible = None
    for wk in range(4):
        anchor_dt = datetime(2026, 6, 29, tzinfo=timezone.utc) + timedelta(weeks=wk)
        _topup_week(engine_conn, "#TOP", anchor_dt, 16, 16, 800)
        for i in range(5):
            _topup_week(engine_conn, f"#M{i}", anchor_dt, 4, 16, 100)
        res = management.run_weekly_review(
            engine_conn, anchor_dt.strftime("%Y-%m-%d"),
            now=anchor_dt.strftime("%Y-%m-%dT07:00:00Z"))
        if any(r["player_tag"] == "#TOP" for r in res["promote_eligible"]):
            saw_eligible = wk
            break
    assert saw_eligible == 2, f"eligible on the 3rd sustained week, got {saw_eligible}"
    cand = next(r for r in res["promote_eligible"] if r["player_tag"] == "#TOP")
    assert "rank" in cand["rationale"] and "band" in cand["rationale"]


def test_demote_hysteresis_two_weeks_no_grace(engine_conn):
    for i in range(11):
        _seed_ranked(engine_conn, f"#P{i}", war_used=8, war_avail=16, war_days=2, donations=200)
    _seed_ranked(engine_conn, "#EGOOD", role="elder", war_used=14, war_avail=16, war_days=3, donations=400)
    _seed_ranked(engine_conn, "#EDEAD", role="elder", war_used=0, war_avail=0, war_days=0, donations=900)
    saw = None
    for wk in range(3):
        anchor_dt = datetime(2026, 6, 29, tzinfo=timezone.utc) + timedelta(weeks=wk)
        for i in range(11):
            _topup_week(engine_conn, f"#P{i}", anchor_dt, 8, 16, 200)
        _topup_week(engine_conn, "#EGOOD", anchor_dt, 14, 16, 400)
        # #EDEAD gets NO fresh war days → stays below the war floor every week
        res = management.run_weekly_review(
            engine_conn, anchor_dt.strftime("%Y-%m-%d"),
            now=anchor_dt.strftime("%Y-%m-%dT07:00:00Z"))
        if any(r["player_tag"] == "#EDEAD" for r in res["demote_eligible"]):
            saw = wk
            break
    assert saw == 1, f"demote-eligible on the 2nd week, got {saw}"


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


def test_manual_leadership_action_survives_reconciliation(engine_conn):
    # A leadership-initiated manual promotion must NOT be auto-withdrawn just
    # because the evaluator state is 'none' (off-cycle override — live 2026-07-05).
    from storage.leader_actions import create_leader_action_recommendation
    _seed_member(engine_conn, tag="#MANUAL", role="member")
    create_leader_action_recommendation(
        action_type="promotion_recommendation",
        objective="Promote #MANUAL",
        rationale="Leadership override.",
        target_player_tag="#MANUAL",
        source_signal_key="manual:promote:test",
        source_signal_type="leadership_manual",
        conn=engine_conn,
    )
    management.withdraw_stale_actions(engine_conn, now=NOW)
    status = engine_conn.execute(
        "SELECT status FROM leader_action_recommendations WHERE target_player_tag='#MANUAL'"
    ).fetchone()["status"]
    assert status == "proposed"  # not auto-withdrawn


def test_swap_member_outranks_elder_by_margin(engine_conn):
    # 13 members, 2 elders (in band 2-3). A strong member clearly out-scores a
    # weak elder by > SWAP_MARGIN → the member is promotable, the elder demotable
    # with reason 'outranked' (a swap, count-neutral).
    for i in range(11):
        _seed_ranked(engine_conn, f"#P{i}", war_used=4, war_avail=16, war_days=2, donations=100)
    # strong member: UC ranked + high war → top of the roster
    _seed_ranked(engine_conn, "#STRONG", ranked_league=7, ranked_rating=1980,
                 war_used=15, war_avail=16, war_days=3, donations=500)
    _seed_ranked(engine_conn, "#E_STRONG", role="elder", war_used=14, war_avail=16,
                 war_days=3, donations=450)
    _seed_ranked(engine_conn, "#E_WEAK", role="elder", war_used=3, war_avail=16,
                 war_days=1, donations=60)
    scores, band = _band(engine_conn)
    assert band["floor"] <= band["current_elders"] <= band["ceil"]
    assert "#STRONG" in band["promotable"]
    assert "#E_WEAK" in band["demotable"]
    assert band["demote_reasons"]["#E_WEAK"] == "outranked"
    assert "#E_STRONG" not in band["demotable"]      # the strong elder holds


def test_swap_deadband_blocks_hair_lead(engine_conn):
    # A member out-scores an elder by LESS than SWAP_MARGIN → no swap (anti-flap).
    for i in range(11):
        _seed_ranked(engine_conn, f"#P{i}", war_used=4, war_avail=16, war_days=2, donations=100)
    # near-tie: member and elder with almost identical output
    _seed_ranked(engine_conn, "#NEAR_M", war_used=12, war_avail=16, war_days=3, donations=300)
    _seed_ranked(engine_conn, "#NEAR_E", role="elder", war_used=12, war_avail=16,
                 war_days=3, donations=295)
    _seed_ranked(engine_conn, "#E2", role="elder", war_used=13, war_avail=16,
                 war_days=3, donations=350)
    scores, band = _band(engine_conn)
    gap = scores["#NEAR_M"]["score"] - scores["#NEAR_E"]["score"]
    assert abs(gap) < management.SWAP_MARGIN            # within the deadband
    assert band["promotable"] == set()                 # no flap
    assert band["demotable"] == set()


def test_outranked_demotion_uses_three_week_cadence(engine_conn):
    # An outranked elder demotes on the 3-week promote cadence (not the 2-week
    # abandonment cadence) so the swap lands with the challenger's promotion.
    for i in range(11):
        _seed_ranked(engine_conn, f"#P{i}", war_used=4, war_avail=16, war_days=2, donations=100)
    _seed_ranked(engine_conn, "#CHALLENGER", ranked_league=7, ranked_rating=1980,
                 war_used=15, war_avail=16, war_days=3, donations=500)
    _seed_ranked(engine_conn, "#E_HELD", role="elder", war_used=14, war_avail=16,
                 war_days=3, donations=450)
    _seed_ranked(engine_conn, "#E_OUT", role="elder", war_used=3, war_avail=16,
                 war_days=1, donations=60)
    # two reviews: not yet eligible (needs 3 for outranked)
    for wk in ("2026-06-29", "2026-07-06"):
        management.run_weekly_review(engine_conn, wk, now=NOW)
    row = engine_conn.execute(
        "SELECT demote_state FROM member_management WHERE player_tag='#E_OUT'").fetchone()
    assert row["demote_state"] == "building"           # 2 weeks < 3 → not eligible yet


def test_elder_evidence_shape_and_none(engine_conn):
    _seed_ranked(engine_conn, "#EV", ranked_league=7, ranked_rating=1980,
                 war_used=15, war_avail=16, war_days=3, donations=226)
    ev = management.elder_evidence(engine_conn, "#EV", now=NOW)
    assert ev and ev["ranked_league_name"] == "Ultimate Champion"
    assert ev["war_deck_rate"] >= 0.9 and ev["donations_week"] == 226
    assert ev["rank"] and ev["elder_score"] > 0
    assert management.elder_evidence(engine_conn, "#GHOST", now=NOW) is None


def test_promotion_fallback_carries_the_why():
    # The deterministic #clan-events promotion copy must explain WHY, drawn
    # from elder_evidence — not a bare "🎉 promoted!" (Jamie 2026-07-05).
    from engine.recognition import compose
    row = {
        "intent_type": "clan:role_changed",
        "payload_json": json.dumps({
            "event_type": "role_changed", "direction": "promoted",
            "new_role": "elder", "player_name": "Atternam",
            "elder_evidence": {"ranked_league_name": "Ultimate Champion",
                               "war_deck_rate": 0.96, "donations_week": 226},
        }),
    }
    out = compose.render_intent(row)
    assert "Atternam" in out and "Elder" in out
    assert "Ultimate Champion" in out          # the why is present
    assert out != "🎉 Atternam was promoted to elder!"   # not the old bare line


def test_promotion_fallback_sane_without_evidence():
    from engine.recognition import compose
    row = {
        "intent_type": "clan:role_changed",
        "payload_json": json.dumps({
            "event_type": "role_changed", "direction": "promoted",
            "new_role": "elder", "player_name": "Newbie"}),
    }
    out = compose.render_intent(row)
    assert "Newbie" in out and "Elder" in out   # graceful with no evidence


def test_management_read_summary_reflects_engine_states():
    """The awareness read must see the engine's actionable verdicts (recommended/
    eligible), NOT trending states (building/watch/at_risk), and only for members
    who are still in the clan."""
    import db

    conn = db.get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, "
                     "last_seen_at, is_home) VALUES ('#CLAN','C','2026-02-04',?,1)", (NOW,))

        def _seed(tag, name, *, left=None, kick="none", promote="none", demote="none", role="member"):
            conn.execute("INSERT INTO players (player_tag, current_name, first_seen_at, "
                         "last_seen_at) VALUES (?,?,?,?)", (tag, name, NOW, NOW))
            conn.execute("INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, "
                         "left_at, join_source) VALUES (?, '#CLAN', '2026-06-01', ?, 'test')", (tag, left))
            conn.execute(
                "INSERT INTO member_management (player_tag, role, kick_state, promote_state, "
                "demote_state, computed_at, week_anchor, promote_qualifying_weeks, state_json) "
                "VALUES (?,?,?,?,?,?,?,0,'{}')",
                (tag, role, kick, promote, demote, NOW, '2026-06-28'),
            )

        _seed("#KREC", "KickRec", kick="recommended")           # actionable kick
        _seed("#KWATCH", "KickWatch", kick="at_risk")           # trending only
        _seed("#PROM", "Promote", promote="eligible", role="member")  # actionable promote
        _seed("#DEM", "Demote", demote="recommended", role="elder")   # actionable demote
        _seed("#PLAIN", "Plain")                                 # nothing
        _seed("#GONE", "Gone", left="2026-07-04", kick="recommended")  # left → excluded
        conn.commit()

        summary = management.management_read_summary(conn)

        kick_tags = {m["player_tag"] for m in summary["actionable"]["kick"]}
        assert kick_tags == {"#KREC"}                    # recommended only, #GONE excluded
        assert {m["player_tag"] for m in summary["actionable"]["promote"]} == {"#PROM"}
        assert {m["player_tag"] for m in summary["actionable"]["demote"]} == {"#DEM"}
        assert summary["building_counts"]["kick"] == 1   # #KWATCH trending, not actionable
        assert summary["members_evaluated"] == 5         # #GONE not counted
    finally:
        conn.close()
