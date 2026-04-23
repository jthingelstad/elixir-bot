"""Trophy-scaled inactivity threshold and multi-signal activity flagging.

Rule: per-member threshold = max(floor_days, trophies/1000 * INACTIVITY_DAYS_PER_1K_TROPHIES).
The floor guards against very-low-trophy players being flagged immediately;
the trophy term gives high-trophy members proportionally more rope.

Primary signal is days since the member's last battle (PvP or war, whichever
is more recent). Login freshness is only context — a member who opens the
game but never battles is inactive.
"""

from datetime import date, datetime, timedelta, timezone

import db
from storage import war_analytics
from storage.war_analytics import (
    BATTLE_RETENTION_DAYS,
    INACTIVITY_DAYS_PER_1K_TROPHIES,
    INACTIVITY_DAYS_PER_1K_TROPHIES_LOOSE,
    INACTIVITY_DAYS_PER_1K_TROPHIES_TIGHT,
    LOOSE_MEMBER_COUNT,
    TIGHT_MEMBER_COUNT,
    _activity_breakdown,
    _effective_inactivity_threshold,
    _inactivity_multiplier,
    flag_inactive_members,
    get_members_at_risk,
)


def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d")


def _seed_member(conn, tag, name, *, role="member", trophies=8000,
                 donations_week=200, last_seen="20260418T120000.000Z",
                 joined_days_ago=60, today=None):
    db.snapshot_members(
        [{"tag": tag, "name": name, "role": role, "expLevel": 60,
          "trophies": trophies, "clanRank": 1, "donations": donations_week,
          "lastSeen": last_seen}],
        conn=conn,
    )
    member_id = conn.execute(
        "SELECT member_id FROM members WHERE player_tag = ?", (tag,)
    ).fetchone()["member_id"]
    if today and joined_days_ago is not None:
        joined = (today - timedelta(days=joined_days_ago)).isoformat()
        db.set_member_join_date(tag, name, joined, conn=conn)
    return member_id


def _seed_battle(conn, member_id, battle_time, *, is_war=0):
    conn.execute(
        "INSERT INTO member_battle_facts (member_id, battle_time, battle_type, "
        "is_ladder, is_war, outcome) VALUES (?, ?, ?, ?, ?, 'W')",
        (member_id, battle_time, "riverRacePvP" if is_war else "PvP",
         0 if is_war else 1, is_war),
    )
    conn.commit()


def _cr_ts(d):
    """Render a datetime as CR-API-compact timestamp (YYYYMMDDTHHMMSS.sssZ)."""
    return d.strftime("%Y%m%dT%H%M%S.000Z")


# --- Threshold formula ------------------------------------------------------

def test_threshold_floor_holds_for_low_trophy_member():
    # 3000 trophies × 1.4/1000 = 4.2 → below 7 floor, so floor wins.
    assert _effective_inactivity_threshold(3000, floor_days=7) == 7.0


def test_threshold_floor_holds_when_trophies_missing_or_zero():
    assert _effective_inactivity_threshold(None, floor_days=7) == 7.0
    assert _effective_inactivity_threshold(0, floor_days=7) == 7.0


def test_threshold_uses_trophy_scaling_at_5k():
    assert _effective_inactivity_threshold(5000, floor_days=7) == 7.0


def test_threshold_scales_above_floor_for_10k_member():
    assert _effective_inactivity_threshold(10000, floor_days=7) == 14.0


def test_threshold_scales_for_12500_trophy_member():
    assert _effective_inactivity_threshold(12500, floor_days=7) == 17.5


def test_floor_param_can_be_raised():
    assert _effective_inactivity_threshold(2000, floor_days=10) == 10.0
    assert _effective_inactivity_threshold(15000, floor_days=10) == 21.0


def test_threshold_accepts_explicit_per_1k_days():
    assert _effective_inactivity_threshold(10000, floor_days=7, per_1k_days=1.4) == 14.0
    assert _effective_inactivity_threshold(10000, floor_days=7, per_1k_days=0.7) == 7.0
    assert _effective_inactivity_threshold(12500, floor_days=7, per_1k_days=0.7) == 8.75


# --- Roster-size-adaptive multiplier ---------------------------------------

def test_multiplier_is_loose_at_or_below_40_members():
    assert _inactivity_multiplier(30) == INACTIVITY_DAYS_PER_1K_TROPHIES_LOOSE
    assert _inactivity_multiplier(LOOSE_MEMBER_COUNT) == INACTIVITY_DAYS_PER_1K_TROPHIES_LOOSE


def test_multiplier_is_tight_at_or_above_50_members():
    assert _inactivity_multiplier(TIGHT_MEMBER_COUNT) == INACTIVITY_DAYS_PER_1K_TROPHIES_TIGHT
    assert _inactivity_multiplier(55) == INACTIVITY_DAYS_PER_1K_TROPHIES_TIGHT


def test_multiplier_interpolates_between_anchors():
    # 45 members is halfway; should produce halfway between 1.4 and 0.7 = 1.05.
    assert abs(_inactivity_multiplier(45) - 1.05) < 1e-9
    # 41 members costs one slope-step (0.07) of rope.
    assert abs(_inactivity_multiplier(41) - 1.33) < 1e-9
    # 49 members is one step from the tight end.
    assert abs(_inactivity_multiplier(49) - 0.77) < 1e-9


def test_multiplier_matches_user_anchor_points():
    """User spec: 10k-trophy threshold = 14d at 40 members, 7d at 50 members."""
    at_40 = _effective_inactivity_threshold(10000, floor_days=7,
                                            per_1k_days=_inactivity_multiplier(40))
    at_50 = _effective_inactivity_threshold(10000, floor_days=7,
                                            per_1k_days=_inactivity_multiplier(50))
    assert at_40 == 14.0
    assert at_50 == 7.0


def test_multiplier_none_defaults_to_loose():
    assert _inactivity_multiplier(None) == INACTIVITY_DAYS_PER_1K_TROPHIES_LOOSE


# --- Activity breakdown (pure) ---------------------------------------------

def test_breakdown_xeraden_style_fresh_login_stale_battles():
    today = date(2026, 4, 22)
    activity = {
        "login": _dt("2026-04-22"),
        "pvp": _dt("2026-04-02"),
        "war": None,
    }
    out = _activity_breakdown(activity, today)
    assert out["battle_days_ago"] == 20
    assert out["login_days_ago"] == 0
    assert out["pvp_days_ago"] == 20
    assert out["war_days_ago"] is None
    assert "hasn't battled in 20 days" in out["hint"]


def test_breakdown_active_battler_uses_most_recent_of_either():
    today = date(2026, 4, 22)
    activity = {
        "login": _dt("2026-04-10"),       # login is stale
        "pvp": _dt("2026-04-21"),          # yesterday
        "war": _dt("2026-04-05"),          # old
    }
    out = _activity_breakdown(activity, today)
    assert out["battle_days_ago"] == 1   # PvP wins (more recent than war)
    assert out["login_days_ago"] == 12
    assert out["pvp_days_ago"] == 1
    assert out["war_days_ago"] == 17


def test_breakdown_war_only_player():
    today = date(2026, 4, 22)
    activity = {
        "login": _dt("2026-04-22"),
        "pvp": _dt("2026-04-05"),
        "war": _dt("2026-04-20"),
    }
    out = _activity_breakdown(activity, today)
    assert out["battle_days_ago"] == 2  # war was more recent


def test_breakdown_no_battles_in_window_falls_back_to_retention():
    today = date(2026, 4, 22)
    activity = {"login": _dt("2026-04-22"), "pvp": None, "war": None}
    out = _activity_breakdown(activity, today)
    assert out["battle_days_ago"] == BATTLE_RETENTION_DAYS
    assert out["last_battle_at"] is None
    assert f"{BATTLE_RETENTION_DAYS}+ days" in out["hint"]


# --- flag_inactive_members integration -------------------------------------

def test_flag_inactive_catches_xeraden_style_member():
    today = date(2026, 4, 22)
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(
            conn, "#XERA", "Xeraden", trophies=8000,
            last_seen=_cr_ts(datetime(2026, 4, 22, 11, 45)),  # fresh login
            today=today,
        )
        # Last battle 20 days ago.
        _seed_battle(conn, member_id, "20260402T015201.000Z", is_war=0)
        result = flag_inactive_members(today=today, conn=conn)
        tags = {m["tag"] for m in result}
        assert "#XERA" in tags
        xera = next(m for m in result if m["tag"] == "#XERA")
        assert xera["battle_days_ago"] == 20
        assert xera["login_days_ago"] == 0
        assert xera["threshold_days"] == 11.2  # 8000 × 1.4/1000
    finally:
        conn.close()


def test_flag_inactive_skips_active_battler_with_stale_login():
    today = date(2026, 4, 22)
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(
            conn, "#PLAY", "RegularPlayer", trophies=8000,
            last_seen=_cr_ts(datetime(2026, 4, 10, 9, 0)),  # 12d stale login
            today=today,
        )
        _seed_battle(conn, member_id, "20260421T180000.000Z", is_war=0)  # yesterday
        result = flag_inactive_members(today=today, conn=conn)
        assert all(m["tag"] != "#PLAY" for m in result)
    finally:
        conn.close()


def test_flag_inactive_uses_most_recent_of_pvp_or_war():
    today = date(2026, 4, 22)
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(
            conn, "#WAR", "WarPlayer", trophies=6000,
            last_seen=_cr_ts(datetime(2026, 4, 22, 9, 0)),
            today=today,
        )
        _seed_battle(conn, member_id, "20260405T120000.000Z", is_war=0)  # 17d stale pvp
        _seed_battle(conn, member_id, "20260420T090000.000Z", is_war=1)  # 2d fresh war
        result = flag_inactive_members(today=today, conn=conn)
        assert all(m["tag"] != "#WAR" for m in result)
    finally:
        conn.close()


def test_flag_inactive_no_battle_records_treats_as_retention_window():
    today = date(2026, 4, 22)
    conn = db.get_connection(":memory:")
    try:
        _seed_member(
            conn, "#GHST", "Ghost", trophies=3000,
            last_seen=_cr_ts(datetime(2026, 4, 22, 9, 0)),
            today=today,
        )
        result = flag_inactive_members(today=today, conn=conn)
        ghost = next((m for m in result if m["tag"] == "#GHST"), None)
        assert ghost is not None
        assert ghost["battle_days_ago"] == BATTLE_RETENTION_DAYS
    finally:
        conn.close()


def test_flag_inactive_excludes_leadership_when_requested():
    today = date(2026, 4, 22)
    conn = db.get_connection(":memory:")
    try:
        _seed_member(
            conn, "#LEAD", "TheLeader", role="leader", trophies=8000,
            last_seen=_cr_ts(datetime(2026, 4, 22, 9, 0)),
            today=today,
        )
        # No battles → past threshold, but leadership is filtered.
        result = flag_inactive_members(today=today, include_leadership=False, conn=conn)
        assert all(m["tag"] != "#LEAD" for m in result)
        result_with = flag_inactive_members(today=today, include_leadership=True, conn=conn)
        assert any(m["tag"] == "#LEAD" for m in result_with)
    finally:
        conn.close()


def test_get_members_at_risk_reports_multi_signal_reason():
    today = date(2026, 4, 22)
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(
            conn, "#XERA", "Xeraden", trophies=8000, donations_week=500,
            last_seen=_cr_ts(datetime(2026, 4, 22, 11, 45)),
            today=today,
        )
        _seed_battle(conn, member_id, "20260402T015201.000Z", is_war=0)
        result = get_members_at_risk(conn=conn)
        members = [m for m in result["members"] if m["tag"] == "#XERA"]
        assert members, "Xeraden should be flagged"
        inactive = next(r for r in members[0]["reasons"] if r["type"] == "inactive")
        assert inactive["battle_days_ago"] == 20
        assert inactive["login_days_ago"] == 0
        assert inactive["pvp_days_ago"] == 20
        assert inactive["war_days_ago"] is None
        assert "tenure_grace_days" not in result["criteria"]
    finally:
        conn.close()


def test_flag_inactive_tightens_with_full_roster():
    """A 10k-trophy member with 8 stale days clears the floor at loose but
    not at tight. The same member flips from safe to flagged as the clan
    fills up."""
    today = date(2026, 4, 22)

    def _setup(member_count):
        conn = db.get_connection(":memory:")
        # Target member: 10k trophies, last battle 8 days ago, fresh login.
        mid = _seed_member(
            conn, "#TOP", "HighTrophyPlayer", trophies=10000,
            last_seen=_cr_ts(datetime(2026, 4, 22, 9, 0)),
            today=today,
        )
        _seed_battle(conn, mid, "20260414T120000.000Z", is_war=0)  # 8 days ago
        # Filler members so the roster hits the target size. They all have
        # fresh battles so they stay under threshold.
        for i in range(member_count - 1):
            fid = _seed_member(
                conn, f"#F{i:03d}", f"Filler{i}", trophies=5000,
                last_seen=_cr_ts(datetime(2026, 4, 22, 9, 0)),
                today=today,
            )
            _seed_battle(conn, fid, "20260421T120000.000Z", is_war=0)
        return conn

    # 40-member roster: threshold for 10k = 14d → 8d is safe.
    conn = _setup(40)
    try:
        flagged = flag_inactive_members(today=today, conn=conn)
        assert all(m["tag"] != "#TOP" for m in flagged)
    finally:
        conn.close()

    # 50-member roster: threshold for 10k = 7d → 8d is flagged.
    conn = _setup(50)
    try:
        flagged = flag_inactive_members(today=today, conn=conn)
        assert any(m["tag"] == "#TOP" for m in flagged)
        top = next(m for m in flagged if m["tag"] == "#TOP")
        assert top["threshold_days"] == 7.0
        assert top["per_1k_days"] == INACTIVITY_DAYS_PER_1K_TROPHIES_TIGHT
        assert top["active_member_count"] == 50
    finally:
        conn.close()


def test_get_members_at_risk_criteria_reports_active_multiplier():
    today = date(2026, 4, 22)
    conn = db.get_connection(":memory:")
    try:
        _seed_member(
            conn, "#ONLY", "OnlyMember", trophies=3000,
            last_seen=_cr_ts(datetime(2026, 4, 22, 9, 0)),
            today=today,
        )
        result = get_members_at_risk(conn=conn)
        assert result["criteria"]["active_member_count"] == 1
        assert result["criteria"]["inactivity_days_per_1k_trophies"] == \
            INACTIVITY_DAYS_PER_1K_TROPHIES_LOOSE
        assert result["criteria"]["multiplier_anchors"]["loose_members"] == LOOSE_MEMBER_COUNT
        assert result["criteria"]["multiplier_anchors"]["tight_members"] == TIGHT_MEMBER_COUNT
    finally:
        conn.close()


def test_get_members_at_risk_no_longer_skips_new_members():
    """The 14-day tenure grace was removed; new members are flagged on merit."""
    today = date(2026, 4, 22)
    conn = db.get_connection(":memory:")
    try:
        # New member, joined 5 days ago, no battles.
        _seed_member(
            conn, "#NEW", "Newcomer", trophies=3000, donations_week=500,
            last_seen=_cr_ts(datetime(2026, 4, 22, 9, 0)),
            joined_days_ago=5, today=today,
        )
        result = get_members_at_risk(conn=conn)
        # Under the old grace they'd be skipped; now they should appear
        # because battle_days_ago defaults to BATTLE_RETENTION_DAYS.
        tags = {m["tag"] for m in result["members"]}
        assert "#NEW" in tags
    finally:
        conn.close()
