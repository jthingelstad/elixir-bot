"""C1b/C1c regressions: contextual_memory reads must hit v5.1 tables, not the
retired v4 ones (members/member_current_state, war_races).
"""

from __future__ import annotations

import db
from storage import contextual_memory as cm


def _mem_conn():
    from memory_store import ensure_memory_schema

    conn = db.get_connection()
    ensure_memory_schema(conn)
    return conn


def test_member_note_memory_reads_players_not_members():
    """upsert_member_note_memory used to join members/member_current_state
    (dropped in v5.1). It must resolve the member via players and still tag the
    memory with member_tag."""
    conn = _mem_conn()
    try:
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES ('#M1','Marco','x','x')"
        )
        conn.commit()
        mem = cm.upsert_member_note_memory(
            member_tag="#M1",
            member_label="Marco",
            note="Reliable war attacker; never misses a day.",
            created_by="leader:jamie",
            conn=conn,
        )
        assert mem is not None
        assert mem.get("member_tag") == "#M1"
    finally:
        conn.close()


def test_race_streak_skips_null_rank_training_week():
    """_count_race_streak reads war_weeks now. A training/incomplete period
    (our_rank IS NULL) must NOT reset the streak; only a real non-1st does."""
    conn = _mem_conn()
    try:
        conn.execute(
            "INSERT INTO war_seasons (season_id, started_at) VALUES (133,'2026-06-01'), (134,'2026-07-01')"
        )
        rows = [
            (133, 0, None, 1),
            (133, 1, None, 1),
            (133, 2, None, 1),
            (133, 3, None, 1),
            (133, 4, "colosseum", 1),  # 5 straight 1sts
            (134, 0, "training", None),  # no finish → must be skipped
        ]
        for season, section, ptype, rank in rows:
            conn.execute(
                "INSERT INTO war_weeks (season_id, section_index, period_type, our_rank) "
                "VALUES (?,?,?,?)",
                (season, section, ptype, rank),
            )
        conn.commit()

        count, latest_season, latest_week = cm._count_race_streak(conn)
        assert count == 5  # NULL training week did not reset it
        assert latest_season == 133
        assert latest_week == 5  # section_index 4 + 1
    finally:
        conn.close()


def test_race_streak_resets_on_real_non_first():
    """A genuine non-1st finish (rank 2) resets the streak."""
    conn = _mem_conn()
    try:
        conn.execute("INSERT INTO war_seasons (season_id, started_at) VALUES (133,'2026-06-01')")
        for season, section, rank in [(133, 0, 1), (133, 1, 2), (133, 2, 1)]:
            conn.execute(
                "INSERT INTO war_weeks (season_id, section_index, our_rank) VALUES (?,?,?)",
                (season, section, rank),
            )
        conn.commit()
        count, _, _ = cm._count_race_streak(conn)
        assert count == 1  # only the final week 2 counts
    finally:
        conn.close()
