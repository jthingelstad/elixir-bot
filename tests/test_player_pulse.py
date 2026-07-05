"""The player Pulse (pulse.md): anchored tiling, window facts, spotlight
scoring, rotation fairness, intent+claim idempotence, compose ask + fallback,
and window partitioning (no row narrated twice)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import db
from runtime.jobs import player_pulse as pp

UTC = timezone.utc


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _seed_member(conn, tag: str, name: str):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-05', 1)")
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, '2026-03-01', '2026-07-05')", (tag, name))
    conn.execute(
        "INSERT OR IGNORE INTO clan_memberships (player_tag, joined_at, join_source) "
        "VALUES (?, '2026-03-01', 'test')", (tag,))


def _seed_battle(conn, tag: str, when: str, *, outcome="W", cf=3, ca=0,
                 trophy=30, mode="ladder", mode_name="Ladder", teammate=None,
                 key=None):
    conn.execute(
        """INSERT INTO battle_events
           (dedup_key, player_tag, battle_time, mode_group, game_mode_name,
            outcome, crowns_for, crowns_against, trophy_change, teammate_tag,
            is_competitive, observed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (key or f"{tag}:{when}:x", tag, when, mode, mode_name, outcome, cf, ca,
         trophy, teammate, "2026-07-05T04:00:00Z"))


def _set_anchor(conn, iso: str):
    # The anchor lives in stream_cursors (the restart-proof home; the
    # runtime_job_status row is re-created on restart — live 2026-07-05).
    pp.write_anchor(conn, pp._parse_iso(iso))
    conn.commit()


# ------------------------------------------------------------------ tiling

def test_seed_anchor_snaps_to_latest_grid_boundary():
    # 07:30Z (02:30 CT) → latest boundary is 01:00Z (20:00 CT yesterday-eve tile)
    assert pp.seed_anchor(_dt("2026-07-05T07:30:00Z")) == _dt("2026-07-05T01:00:00Z")
    # Just after a boundary sits ON it
    assert pp.seed_anchor(_dt("2026-07-05T09:00:01Z")) == _dt("2026-07-05T09:00:00Z")
    # Early UTC morning reaches back to yesterday's 17:00Z
    assert pp.seed_anchor(_dt("2026-07-05T00:59:00Z")) == _dt("2026-07-04T17:00:00Z")


def test_first_run_seeds_and_does_not_post():
    conn = db.get_connection()
    try:
        summary = pp.run_check(conn, _dt("2026-07-05T07:30:00Z"))
        assert summary.startswith("seeded")
        assert "anchor=2026-07-05T01:00:00Z" in summary
        n = conn.execute(
            "SELECT COUNT(*) FROM communication_intents WHERE intent_type = ?",
            (pp.INTENT_TYPE,)).fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_tiling_survives_missed_checks_and_posts_only_latest_window():
    conn = db.get_connection()
    try:
        _seed_member(conn, "#T1", "Tiler")
        _set_anchor(conn, "2026-07-05T01:00:00Z")
        # 20 hours later: windows 01-09 and 09-17 are both complete.
        summary = pp.run_check(conn, _dt("2026-07-05T21:00:00Z"))
        # Only the LATEST complete window (09-17) posts; 01-09 skipped.
        assert "skipped=1" in summary
        assert "anchor=2026-07-05T17:00:00Z" in summary
        rows = conn.execute(
            "SELECT payload_json FROM communication_intents WHERE intent_type = ?",
            (pp.INTENT_TYPE,)).fetchall()
        assert len(rows) == 1
        p = json.loads(rows[0][0])
        assert p["window_start"] == "2026-07-05T09:00:00Z"
        assert p["window_end"] == "2026-07-05T17:00:00Z"
    finally:
        conn.close()


def test_double_run_is_idempotent_one_intent():
    conn = db.get_connection()
    try:
        _seed_member(conn, "#T1", "Tiler")
        for _ in range(2):
            _set_anchor(conn, "2026-07-05T01:00:00Z")   # simulate anchor rollback
            pp.run_check(conn, _dt("2026-07-05T09:30:00Z"))
        n = conn.execute(
            "SELECT COUNT(*) FROM communication_intents WHERE intent_type = ?",
            (pp.INTENT_TYPE,)).fetchone()[0]
        assert n == 1  # ledger claim pulse:player:{end} blocked the second raise
    finally:
        conn.close()


def test_windows_partition_rows_exactly():
    conn = db.get_connection()
    try:
        _seed_member(conn, "#T1", "Tiler")
        # One battle per hour across two adjacent windows
        for h in range(2, 17):
            _seed_battle(conn, "#T1", f"20260705T{h:02d}3000.000Z",
                         key=f"b{h}")
        w1 = pp.build_window_facts(conn, _dt("2026-07-05T01:00:00Z"),
                                   _dt("2026-07-05T09:00:00Z"))
        w2 = pp.build_window_facts(conn, _dt("2026-07-05T09:00:00Z"),
                                   _dt("2026-07-05T17:00:00Z"))
        assert w1["battles_total"] + w2["battles_total"] == 15
        assert w1["battles_total"] == 7   # hours 02..08
        assert w2["battles_total"] == 8   # hours 09..16
    finally:
        conn.close()


# ------------------------------------------------------------------ facts

def test_empty_window_facts_are_quiet_and_fallback_safe():
    conn = db.get_connection()
    try:
        _seed_member(conn, "#T1", "Tiler")
        facts = pp.build_window_facts(conn, _dt("2026-07-05T01:00:00Z"),
                                      _dt("2026-07-05T09:00:00Z"))
        assert facts["quiet_window"] is True
        assert facts["battles_total"] == 0
        # Fallback renders without error on the empty facts
        from engine.recognition.compose import render_intent
        copy = render_intent({"payload_json": json.dumps(facts),
                              "intent_type": pp.INTENT_TYPE})
        assert "0 battles" in copy
    finally:
        conn.close()


def test_spotlight_scoring_prefers_duo_decider_over_plain_sweep():
    members = {"#A": "Ace", "#B": "Bee"}
    plain_sweep = {"player_tag": "#A", "battle_time": "20260705T020000.000Z",
                   "mode_group": "ladder", "game_mode_name": "Ladder",
                   "outcome": "W", "crowns_for": 3, "crowns_against": 0,
                   "trophy_change": 30, "teammate_tag": None, "dedup_key": "b1",
                   "is_competitive": 1}
    duo_win = {**plain_sweep, "player_tag": "#B", "dedup_key": "b2",
               "battle_time": "20260705T030000.000Z", "crowns_for": 2,
               "teammate_tag": "#A", "mode_group": "two_v_two",
               "game_mode_name": "TeamVsTeam"}
    spot = pp.spotlight_battle(members, [plain_sweep, duo_win], set())
    assert spot["name"] == "Bee" and "alongside Ace" in spot["why"]
    # An arena-up decider outranks everything
    spot2 = pp.spotlight_battle(members, [plain_sweep, duo_win], {"b1"})
    assert spot2["name"] == "Ace" and "clinched" in spot2["why"]


def test_rotation_fairness_carries_prior_featured_names():
    conn = db.get_connection()
    try:
        _seed_member(conn, "#T1", "Tiler")
        for i in range(6):
            _seed_battle(conn, "#T1", f"20260705T02{i:02d}00.000Z", key=f"r{i}")
        _set_anchor(conn, "2026-07-05T01:00:00Z")
        pp.run_check(conn, _dt("2026-07-05T09:30:00Z"))
        # Second window: previous pulse's featured names surface in context
        facts = pp.build_window_facts(conn, _dt("2026-07-05T09:00:00Z"),
                                      _dt("2026-07-05T17:00:00Z"))
        assert "Tiler" in facts.get("recently_featured", [])
    finally:
        conn.close()


def test_compose_ask_renders_for_pulse_intents():
    conn = db.get_connection()
    try:
        _seed_member(conn, "#T1", "Tiler")
        facts = {"event_type": "player_pulse", "battles_total": 3,
                 "quiet_window": True, "window_end": "2026-07-05T09:00:00Z"}
        from engine.recognition.compose import intent_context
        ctx = intent_context(conn, {
            "intent_type": pp.INTENT_TYPE, "scope": "public",
            "payload_json": json.dumps(facts)})
        assert "#battle-feed" in ctx and "recently_featured" in ctx
        assert "quiet window" in ctx or "quiet_window" in ctx
    finally:
        conn.close()
