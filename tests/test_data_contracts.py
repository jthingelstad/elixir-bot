"""Cross-layer contracts for observations, events, and materialized judgments."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from engine import ingest, management
from engine.clock import WarClock
from engine.db import ensure_player
from engine.emitters import insert_stream_event
from engine.event_contracts import (
    EVENT_CONTRACTS,
    hard_post_event_types,
    lane_by_event_type,
)
from storage.war_analytics import get_members_at_risk


def test_event_registry_is_the_routing_and_hard_post_authority():
    lanes = lane_by_event_type()
    assert set(lanes) == set(EVENT_CONTRACTS)
    assert hard_post_event_types() == {
        name for name, contract in EVENT_CONTRACTS.items() if contract.hard_post
    }
    assert "member_joined" in hard_post_event_types()
    assert "member_left" not in hard_post_event_types()


def test_hard_post_set_is_the_expected_member_facing_floor():
    """Audit guard: the hard-post floor is the exact set of member-facing event
    classes a leader expects Elixir to always surface. Adding or dropping a
    hard_post event is a deliberate editorial decision — it must update this
    list too, so an accidental registry edit can't silently widen or shrink the
    floor (a dropped floor = a silently muteable member event; an added one =
    an unreviewed mandatory post). See elixir-hard-post-registry-v51."""
    assert hard_post_event_types() == frozenset(
        {
            # → #announcements
            "member_joined",
            "member_left_verified",  # verified leave only; raw member_left is held
            "role_changed",
            "clan_birthday",
            "pol_season_podium",
            # → #elixir
            "week_finished",
            "season_closed",
        }
    )


def test_event_boundary_rejects_undeclared_or_incomplete_events(engine_conn):
    with pytest.raises(ValueError, match="undeclared event type"):
        insert_stream_event(
            engine_conn,
            "player_events",
            dedup_key="unknown:#A",
            event_type="unknown_event",
            subject_cols={"player_tag": "#A"},
            observed_at="2026-07-15T12:00:00Z",
            window_start=None,
            payload={},
        )

    ensure_player(engine_conn, "#A", "A", "2026-07-15T12:00:00Z")
    assert (
        insert_stream_event(
            engine_conn,
            "player_events",
            dedup_key="badge_earned:#A:test",
            event_type="badge_earned",
            subject_cols={"player_tag": "#A"},
            observed_at="2026-07-15T12:00:00",
            window_start=None,
            payload={"badge_name": "TestBadge"},
        )
        == 1
    )
    stored = engine_conn.execute(
        "SELECT observed_at FROM player_events WHERE dedup_key = 'badge_earned:#A:test'"
    ).fetchone()
    assert stored["observed_at"] == "2026-07-15T12:00:00Z"
    with pytest.raises(ValueError, match="missing payload fields"):
        insert_stream_event(
            engine_conn,
            "player_events",
            dedup_key="badge_earned:#A:test",
            event_type="badge_earned",
            subject_cols={"player_tag": "#A"},
            observed_at="2026-07-15T12:00:00Z",
            window_start=None,
            payload={},
        )


def test_duplicate_battle_enriches_missing_war_keys(engine_conn, cr_fixture):
    battle = cr_fixture("battlelog")[0]
    tag = battle["team"][0]["tag"]
    ensure_player(engine_conn, tag, battle["team"][0]["name"], "2026-07-04T16:00:00Z")
    now = datetime(2026, 7, 4, 16, 0, tzinfo=timezone.utc)

    assert (
        ingest.mirror_battles(
            engine_conn, tag, [battle], "2026-07-04T16:00:00Z", None, now=now
        )
        == 1
    )
    before = engine_conn.execute(
        "SELECT season_id FROM battle_events WHERE player_tag = ?", (tag,)
    ).fetchone()
    assert before["season_id"] is None

    clock = WarClock(
        phase="war_day",
        season_id=133,
        section_index=2,
        period_index=17,
        day_index=3,
        war_day_index=0,
        is_colosseum_week=False,
        battle_days_remaining=4,
        hours_left_in_period=18.0,
        our_fame=0,
        finish_line=10_000,
        race_finished=False,
        pace_status="behind",
    )
    assert (
        ingest.mirror_battles(
            engine_conn, tag, [battle], "2026-07-04T16:10:00Z", clock, now=now
        )
        == 0
    )
    after = engine_conn.execute(
        "SELECT season_id, section_index, war_day_index "
        "FROM battle_events WHERE player_tag = ?",
        (tag,),
    ).fetchone()
    assert tuple(after) == (133, 2, 0)


def test_management_holds_judgment_when_member_evidence_is_stale(engine_conn):
    now = "2026-07-15T12:00:00Z"
    engine_conn.execute(
        "INSERT OR IGNORE INTO clans "
        "(clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', ?, ?, 1)",
        (now, now),
    )
    ensure_player(engine_conn, "#STALE", "Stale", now)
    engine_conn.execute(
        "INSERT INTO clan_memberships "
        "(player_tag, clan_tag, joined_at, join_source) "
        "VALUES ('#STALE', '#J2RGCRVG', '2026-06-01T00:00:00Z', 'test')"
    )
    engine_conn.execute(
        "INSERT INTO member_management "
        "(player_tag, computed_at, week_anchor, kick_state) "
        "VALUES ('#STALE', ?, '2026-07-13', 'none')",
        (now,),
    )
    engine_conn.execute(
        "INSERT INTO battle_events "
        "(dedup_key, player_tag, battle_time, observed_at) "
        "VALUES ('stale-battle', '#STALE', '20260701T120000.000Z', "
        "'2026-07-01T12:00:00Z')"
    )

    transitions = management.run_tick_evaluators(
        engine_conn,
        now=now,
        readiness={
            "members": {
                "#STALE": {
                    "status": "held",
                    "reasons": ["battlelog_stale"],
                    "evidence_as_of": "2026-07-01T12:00:00Z",
                }
            }
        },
        materialization_id=None,
    )
    assert transitions == []
    row = engine_conn.execute(
        "SELECT kick_state, judgment_status, judgment_reason "
        "FROM member_management WHERE player_tag = '#STALE'"
    ).fetchone()
    assert tuple(row) == ("none", "held", "battlelog_stale")
    summary = management.management_read_summary(engine_conn)
    assert summary["actionable"]["kick"] == []
    assert summary["readiness"]["counts"]["held"] == 1
    assert get_members_at_risk(conn=engine_conn)["members"] == []
