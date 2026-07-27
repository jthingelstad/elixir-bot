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
        "sectionIndex": 0,
        "periodIndex": 5,
        "periodType": "warDay",
        "clan": {"tag": "#J2RGCRVG", "fame": 6870, "participants": []},
        "clans": [
            {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 6870,
                "periodPoints": 3900,
            }
        ],
        "periodLogs": [
            {
                "periodIndex": 3,
                "items": [
                    {
                        "clan": {"tag": "#J2RGCRVG"},
                        "progressEarned": 3000,
                        "progressEarnedFromDefenses": 435,
                        "numOfDefensesRemaining": 15,
                    }
                ],
            },
            {
                "periodIndex": 4,
                "items": [
                    {
                        "clan": {"tag": "#J2RGCRVG"},
                        "progressEarned": 3000,
                        "progressEarnedFromDefenses": 435,
                        "numOfDefensesRemaining": 15,
                    }
                ],
            },
        ],
    }
    proj = project_race_aspect(payload, 134)
    assert proj["our_defense"] == {
        "defense_fame_recent": 435,
        "defenses_remaining": 15,
        "defense_fame_days": [435, 435],
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
        assert (
            conn.execute(
                "SELECT defense_fame FROM war_weeks WHERE season_id=777 AND section_index=0"
            ).fetchone()["defense_fame"]
            == 870
        )
    finally:
        conn.close()


def _log(period_index: int, dfame: int, remaining: int) -> dict:
    return {
        "periodIndex": period_index,
        "items": [
            {
                "clan": {"tag": "#J2RGCRVG"},
                "progressEarned": 3000,
                "progressEarnedFromDefenses": dfame,
                "numOfDefensesRemaining": remaining,
            }
        ],
    }


def _payload(section_index: int, period_index: int, period_type: str, logs: list) -> dict:
    return {
        "sectionIndex": section_index,
        "periodIndex": period_index,
        "periodType": period_type,
        "clan": {"tag": "#J2RGCRVG", "fame": 0, "participants": []},
        "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 0, "periodPoints": 0}],
        "periodLogs": logs,
    }


def test_defense_projection_is_scoped_to_the_current_week():
    """periodLogs spans the WHOLE SEASON, not just this week — a section-2 payload
    still carries section-0 and section-1 battle days. Summing all of them inflated
    war_weeks.defense_fame for every week after the first.
    """
    logs = [
        _log(3, 435, 15),
        _log(4, 435, 15),  # section 0
        _log(10, 400, 12),
        _log(11, 400, 12),  # section 1
        _log(17, 300, 9),
        _log(18, 300, 9),  # section 2 <- the current week
    ]
    proj = project_race_aspect(_payload(2, 19, "warDay", logs), 134)
    assert proj["our_defense"]["defense_fame_days"] == [300, 300], "earlier weeks leaked in"
    assert _week_defense_fame(proj) == 600, "must be THIS week's total, not the season's"
    assert proj["our_defense"]["defenses_remaining"] == 9


def test_practice_day_does_not_report_last_weeks_defenses_as_this_weeks():
    """On a practice day the current section has no closed days yet, so there is
    nothing to report — previously the previous week's final battle day was
    surfaced as if it were the current week's state."""
    logs = [_log(17, 300, 9), _log(18, 300, 9), _log(19, 300, 9), _log(20, 300, 9)]
    proj = project_race_aspect(_payload(3, 21, "training", logs), 134)
    assert proj["our_defense"] is None
