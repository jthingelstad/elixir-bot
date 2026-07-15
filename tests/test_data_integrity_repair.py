"""Issue #184: the live integrity repair is complete, safe, and idempotent."""

from __future__ import annotations

import json

from engine.baselines import set_baseline
from scripts.repair_data_integrity_20260715 import audit, run_repair


def test_integrity_repair_dry_run_apply_and_idempotence(engine_conn):
    c = engine_conn
    c.execute(
        "INSERT OR IGNORE INTO clans "
        "(clan_tag,name,first_seen_at,last_seen_at,is_home) "
        "VALUES ('#J2RGCRVG','POAP KINGS','2026-01-01','2026-07-15',1)"
    )
    for tag in ("#FIX", "#OVER"):
        c.execute(
            "INSERT INTO players (player_tag,current_name,first_seen_at,last_seen_at) "
            "VALUES (?, ?, '2026-01-01', '2026-07-15')",
            (tag, tag),
        )
    c.execute(
        "INSERT INTO clan_memberships "
        "(player_tag,clan_tag,joined_at,join_source) "
        "VALUES ('#FIX','#J2RGCRVG','2026-01-01','test')"
    )
    set_baseline(
        c,
        "player",
        "#FIX",
        "profile",
        {"best_trophies": 5100, "exp_level": 51},
        "2026-07-06T12:00:00Z",
    )
    c.execute(
        "INSERT INTO player_current_state "
        "(player_tag,observed_at,best_trophies,exp_level) "
        "VALUES ('#FIX','2026-07-06T12:00:00Z',NULL,0)"
    )
    c.execute(
        "INSERT INTO raw_api_payloads "
        "(endpoint,entity_key,fetched_at,payload_hash,payload_json) "
        "VALUES ('player','FIX','2026-07-04T12:00:00Z','fix-1',?)",
        (json.dumps({"bestTrophies": 5000, "expLevel": 50}),),
    )
    c.execute(
        "INSERT INTO raw_api_payloads "
        "(endpoint,entity_key,fetched_at,payload_hash,payload_json) "
        "VALUES ('player','FIX','2026-07-06T12:00:00Z','fix-2',?)",
        (json.dumps({"bestTrophies": 5100, "expLevel": 51}),),
    )
    c.execute(
        "INSERT INTO player_daily_metrics "
        "(player_tag,metric_date,best_trophies,exp_level) "
        "VALUES ('#FIX','2026-07-04',5000,50)"
    )
    c.execute(
        "INSERT INTO player_daily_metrics "
        "(player_tag,metric_date,best_trophies,exp_level) "
        "VALUES ('#FIX','2026-07-05',NULL,0)"
    )
    c.execute(
        "INSERT INTO war_seasons (season_id,started_at) "
        "VALUES (190,'20260701T000000.000Z')"
    )
    c.execute(
        "INSERT INTO war_weeks (season_id,section_index,created_date) "
        "VALUES (190,0,'20260701T093700.000Z')"
    )
    c.execute(
        "INSERT INTO war_participation "
        "(season_id,section_index,player_tag,observed_at) "
        "VALUES (190,0,'#FIX','20260701T093700.000Z')"
    )
    c.execute(
        "INSERT INTO clan_memberships "
        "(player_tag,clan_tag,joined_at,left_at,join_source,leave_source) "
        "VALUES ('#OVER','#J2RGCRVG','2026-03-01','2026-07-04',"
        "'bootstrap_seed','pre_cut_reconciliation')"
    )
    c.execute(
        "INSERT INTO clan_memberships "
        "(player_tag,clan_tag,joined_at,left_at,join_source,leave_source) "
        "VALUES ('#OVER','#J2RGCRVG','2026-03-01','2026-03-20',"
        "'backfill','manual_clear')"
    )
    c.commit()

    # Recreate the retired-channel residue: FK enforcement was disabled during
    # the historical reshape, which is exactly how live reached this state.
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute(
        "INSERT INTO conversation_threads "
        "(thread_id,scope_type,scope_key,channel_id,created_at,last_active_at) "
        "VALUES (9001,'channel','retired','missing-channel','2026-07-01','2026-07-02')"
    )
    c.execute(
        "INSERT INTO messages "
        "(message_id,thread_id,channel_id,author_type,content,created_at) "
        "VALUES (9001,9001,'missing-channel','assistant','old','2026-07-02')"
    )
    c.commit()
    c.execute("PRAGMA foreign_keys=ON")

    before = audit(c)
    assert before["foreign_key_violations"] == 2
    assert before["membership_overlaps"] == 1
    assert before["profile_best_projection_mismatches"] == 1
    assert before["daily_best_drops_to_null"] == 1
    assert before["noncanonical_war_timestamps"] >= 3

    dry = run_repair(c, apply=False)
    assert dry["applied"] is False
    assert all(
        dry["after"][key] == 0
        for key in (
            "foreign_key_violations",
            "membership_overlaps",
            "profile_best_projection_mismatches",
            "daily_best_drops_to_null",
            "noncanonical_war_timestamps",
        )
    )
    assert audit(c) == before

    applied = run_repair(c, apply=True)
    assert applied["applied"] is True
    assert audit(c)["foreign_key_violations"] == 0
    state = c.execute(
        "SELECT best_trophies,exp_level FROM player_current_state WHERE player_tag='#FIX'"
    ).fetchone()
    assert (state["best_trophies"], state["exp_level"]) == (5100, 51)
    daily = c.execute(
        "SELECT best_trophies,exp_level FROM player_daily_metrics "
        "WHERE player_tag='#FIX' AND metric_date='2026-07-05'"
    ).fetchone()
    assert (daily["best_trophies"], daily["exp_level"]) == (5000, 50)
    assert (
        c.execute("SELECT created_date FROM war_weeks WHERE season_id=190").fetchone()[
            0
        ]
        == "2026-07-01T09:37:00Z"
    )
    assert (
        c.execute(
            "SELECT channel_id FROM conversation_threads WHERE thread_id=9001"
        ).fetchone()[0]
        is None
    )
    assert (
        c.execute(
            "SELECT COUNT(*) FROM clan_memberships WHERE player_tag='#OVER'"
        ).fetchone()[0]
        == 1
    )

    again = run_repair(c, apply=True)
    assert all(value == 0 for value in again["changes"].values())
