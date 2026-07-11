"""Boat-defense fame persists durably in war_weeks (Jamie: keep it in the data
layer with the rest of the clan-wars data), sourced from the API's
periodLogs.progressEarnedFromDefenses — so it survives the season rolling off
the live payload.
"""
from __future__ import annotations

import db
from engine.emitters.war import (
    _ensure_war_weeks_defense_column,
    _upsert_week,
    _week_defense_fame,
    project_race_aspect,
)


def test_project_race_aspect_extracts_defense_from_periodlogs():
    payload = {
        "sectionIndex": 0, "periodIndex": 5, "periodType": "warDay",
        "clan": {"tag": "#J2RGCRVG", "fame": 6870, "participants": []},
        "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 6870, "periodPoints": 3900}],
        "periodLogs": [
            {"periodIndex": 3, "items": [{"clan": {"tag": "#J2RGCRVG"}, "progressEarned": 3000,
                                          "progressEarnedFromDefenses": 435, "numOfDefensesRemaining": 15}]},
            {"periodIndex": 4, "items": [{"clan": {"tag": "#J2RGCRVG"}, "progressEarned": 3000,
                                          "progressEarnedFromDefenses": 435, "numOfDefensesRemaining": 15}]},
        ],
    }
    proj = project_race_aspect(payload, 134)
    assert proj["our_defense"] == {
        "defense_fame_recent": 435, "defenses_remaining": 15, "defense_fame_days": [435, 435]
    }
    assert _week_defense_fame(proj) == 870  # both closed days


def test_upsert_week_persists_defense_fame():
    conn = db.get_connection()
    try:
        conn.execute("INSERT INTO war_seasons (season_id, started_at) VALUES (777, '2026-07-01')")
        _ensure_war_weeks_defense_column(conn)
        _upsert_week(conn, 777, 0, "warDay", "2026-07-11T00:00:00Z", defense_fame=870)
        conn.commit()
        row = conn.execute(
            "SELECT defense_fame FROM war_weeks WHERE season_id=777 AND section_index=0"
        ).fetchone()
        assert row["defense_fame"] == 870
        # MAX-merge never regresses within a week (a momentarily-empty periodLog).
        _upsert_week(conn, 777, 0, "warDay", "2026-07-11T01:00:00Z", defense_fame=None)
        conn.commit()
        assert conn.execute(
            "SELECT defense_fame FROM war_weeks WHERE season_id=777 AND section_index=0"
        ).fetchone()["defense_fame"] == 870
    finally:
        conn.close()
