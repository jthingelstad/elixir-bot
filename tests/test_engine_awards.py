"""Season awards — the Q5 consumer (engine/awards.py): podium, free-pass
rotation metadata, iron-king insufficient-data guard, silent participants,
idempotency. Plus the projected-roster-key regression on
projections.refresh_player_state (live incident 2026-07-04)."""
from __future__ import annotations

import db
from engine import awards as engine_awards
from engine import projections

AT = "2026-07-06T10:00:00Z"
SEASON = 133


def _seed_season(conn, *, full_attendance: bool = False):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-06', 1)"
    )
    for tag, name in (("#A", "Alpha"), ("#B", "Bravo"), ("#C", "Carol"),
                      ("#D", "Dora"), ("#R", "Rook")):
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES (?, ?, '2026-02-01', '2026-07-06')", (tag, name))
        joined = "2026-06-20" if tag == "#R" else "2026-03-01"  # Rook = mid-season
        conn.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
            "VALUES (?, ?, 'test')", (tag, joined))
    conn.execute(
        "INSERT INTO war_seasons (season_id, started_at, ended_at, final_rank, weeks, "
        "war_champ_tag, free_pass_tag) VALUES (?, '2026-06-10', ?, 1, 2, '#A', '#B')",
        (SEASON, AT))  # rotation applied: champ #A, pass falls to #B
    for section in (3, 4):
        conn.execute(
            "INSERT INTO war_weeks (season_id, section_index, created_date, finish_time) "
            "VALUES (?, ?, ?, ?)",
            (SEASON, section, f"2026-06-{10 + section}", f"2026-06-{17 + section}"))
        for tag, fame in (("#A", 3000), ("#B", 2500), ("#C", 2000), ("#R", 900)):
            conn.execute(
                "INSERT INTO war_participation (season_id, section_index, player_tag, "
                "fame, decks_used, observed_at) VALUES (?, ?, ?, ?, 16, ?)",
                (SEASON, section, tag, fame, AT))
    # attendance: full coverage only when asked (else section 4 only — the
    # real s133 situation)
    sections = (3, 4) if full_attendance else (4,)
    for section in sections:
        for day in (0, 1):
            for tag, used in (("#A", 4), ("#B", 4), ("#C", 3)):
                conn.execute(
                    "INSERT INTO war_attendance_days (season_id, section_index, "
                    "war_day_index, player_tag, decks_used, decks_available, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, 4, ?)",
                    (SEASON, section, day, tag, used, AT))
    # donations: Carol leads
    for d, (tag, don) in enumerate((("#C", 400), ("#A", 250), ("#B", 100))):
        conn.execute(
            "INSERT INTO player_daily_metrics (player_tag, metric_date, donations_week) "
            "VALUES (?, ?, ?)", (tag, f"2026-06-1{5 + d}", don))
    conn.commit()


def test_grant_season_awards_full_slate():
    conn = db.get_connection()
    try:
        _seed_season(conn)
        grants = engine_awards.grant_season_awards(conn, SEASON, AT)

        podium = grants["war_champ"]
        assert [(e["rank"], e["tag"]) for e in podium] == [(1, "#A"), (2, "#B"), (3, "#C")]
        assert podium[0]["metric_value"] == 6000  # 3000 × 2 sections

        fp = grants["free_pass"]
        assert len(fp) == 1 and fp[0]["tag"] == "#B" and fp[0]["rotation_applied"] is True
        fp_rows = conn.execute(
            "SELECT COUNT(*) FROM awards WHERE award_type='free_pass' AND season_id=?",
            (SEASON,)).fetchone()[0]
        assert fp_rows == 1

        # s133 reality: attendance only covers one of two sections → guarded skip
        assert grants["iron_kings"] == []
        assert grants["iron_king_skipped"] == "insufficient attendance data"

        assert [e["tag"] for e in grants["donation_champs"]] == ["#C", "#A", "#B"]
        assert [e["tag"] for e in grants["rookie_mvps"]] == ["#R"]  # mid-season joiner only
        assert grants["war_participants"] == 4  # A, B, C, R (fame > 0)

        # ledger keys claimed per recognition.md §5
        claims = conn.execute(
            "SELECT COUNT(*) FROM recognition_ledger WHERE recognition_key LIKE 'award:%'"
        ).fetchone()[0]
        assert claims == grants["granted"]

        # idempotent: re-run grants nothing
        again = engine_awards.grant_season_awards(conn, SEASON, AT)
        assert again["granted"] == 0
    finally:
        conn.close()


def test_iron_king_grants_with_full_coverage():
    conn = db.get_connection()
    try:
        _seed_season(conn, full_attendance=True)
        grants = engine_awards.grant_season_awards(conn, SEASON, AT)
        assert grants["iron_king_skipped"] is None
        assert [e["tag"] for e in grants["iron_kings"]] == ["#A", "#B"]  # 4/4 every day
    finally:
        conn.close()


def test_refresh_player_state_reads_projected_roster_keys():
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES ('#X', 'Xeno', '2026-01-01', '2026-07-06')")
        # projected (snake_case) roster entry — the shape the tick passes
        projections.refresh_player_state(conn, "#X", None, {
            "role": "member", "trophies": 5100, "donations": 42,
            "donations_received": 8, "exp_level": 44,
            "clan_rank": 7, "previous_clan_rank": 9,
        }, AT)
        row = conn.execute(
            "SELECT exp_level, clan_rank, previous_clan_rank, donations_received_week "
            "FROM player_current_state WHERE player_tag='#X'").fetchone()
        assert (row["exp_level"], row["clan_rank"], row["previous_clan_rank"],
                row["donations_received_week"]) == (44, 7, 9, 8)
        # roster-only refresh without those keys preserves existing values
        projections.refresh_player_state(conn, "#X", None, {"trophies": 5150}, AT)
        row = conn.execute(
            "SELECT exp_level, clan_rank FROM player_current_state "
            "WHERE player_tag='#X'").fetchone()
        assert (row["exp_level"], row["clan_rank"]) == (44, 7)
    finally:
        conn.close()
