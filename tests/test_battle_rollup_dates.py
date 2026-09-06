"""Late battle receipts belong to their Chicago battle day, not the poll day."""

from datetime import datetime, timedelta, timezone

import pytest

from engine import materialize, observations


@pytest.mark.parametrize("midnight", ["2026-09-06T05:00:00Z", "2026-01-06T06:00:00Z"])
def test_late_battlelogs_rebuild_historical_days_without_rewriting_profile_history(
    engine_conn, midnight
):
    boundary = datetime.fromisoformat(midnight.replace("Z", "+00:00"))
    prior_day = (boundary - timedelta(days=1)).date().isoformat()
    older_day = (boundary - timedelta(days=2)).date().isoformat()
    today = boundary.date().isoformat()

    def battle(at):
        return {
            "battleTime": at.strftime("%Y%m%dT%H%M%S.000Z"),
            "type": "PvP",
            "gameMode": {"id": 72000006, "name": "Ladder"},
            "team": [{"tag": "#A", "crowns": 2, "trophyChange": 30}],
            "opponent": [{"tag": "#B", "crowns": 1}],
        }

    def apply(payload, at):
        decision, observation = observations.observe(
            "player_battlelog", "#A", payload, at, source="test"
        )
        assert decision.accepted
        return materialize.apply_interactive_observation(engine_conn, observation)

    early = battle(boundary - timedelta(hours=2))
    apply([early], boundary - timedelta(hours=1))
    engine_conn.execute(
        "INSERT INTO player_daily_metrics (player_tag, metric_date, trophies) "
        "VALUES ('#A', ?, 4000)",
        (prior_day,),
    )
    engine_conn.execute(
        "INSERT INTO player_current_state (player_tag, observed_at, trophies) "
        "VALUES ('#A', ?, 5000)",
        (midnight,),
    )
    engine_conn.execute(
        "UPDATE player_daily_battle_rollups SET expected_battle_delta = 2, "
        "completeness_ratio = 0.5, is_complete = 0"
    )
    payload = [
        early,
        battle(boundary - timedelta(minutes=1)),
        battle(boundary - timedelta(days=1, minutes=1)),
        battle(boundary),
    ]
    assert apply(payload, boundary + timedelta(minutes=10)).battles_ingested == 3
    assert apply(payload, boundary + timedelta(minutes=20)).battles_ingested == 0

    rollups = {
        r["battle_date"]: dict(r)
        for r in engine_conn.execute("SELECT * FROM player_daily_battle_rollups")
    }
    assert {day: r["battles"] for day, r in rollups.items()} == {
        older_day: 1,
        prior_day: 2,
        today: 1,
    }
    assert rollups[prior_day]["expected_battle_delta"] == 2
    assert rollups[prior_day]["completeness_ratio"] == 1.0
    assert rollups[prior_day]["is_complete"] == 1
    assert rollups[older_day]["expected_battle_delta"] is None
    assert rollups[older_day]["is_complete"] == 0
    assert rollups[prior_day]["wins"] == 2
    assert rollups[prior_day]["trophy_change_total"] == 60
    assert dict(
        engine_conn.execute("SELECT metric_date, trophies FROM player_daily_metrics").fetchall()
    ) == {prior_day: 4000, today: 5000}


def test_empty_battlelog_still_updates_todays_profile_metrics(engine_conn):
    from engine.db import ensure_player

    now = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
    ensure_player(engine_conn, "#A", "A", now.isoformat())
    engine_conn.execute(
        "INSERT INTO player_current_state (player_tag, observed_at, trophies) "
        "VALUES ('#A', ?, 5000)",
        (now.isoformat(),),
    )
    decision, observation = observations.observe("player_battlelog", "#A", [], now, source="test")
    assert decision.accepted
    materialize.apply_interactive_observation(engine_conn, observation)
    assert (
        engine_conn.execute("SELECT COUNT(*) FROM player_daily_battle_rollups").fetchone()[0] == 0
    )
    assert tuple(
        engine_conn.execute("SELECT metric_date, trophies FROM player_daily_metrics").fetchone()
    ) == ("2026-09-06", 5000)
