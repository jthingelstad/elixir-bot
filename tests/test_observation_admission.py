"""CR observations must earn the right to mutate durable engine state."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

from engine import observations
from engine.tick import HOME_CLAN, run_tick
from tests.conftest import load_cr_fixture


def test_real_payload_fixtures_pass_endpoint_contracts():
    clan = load_cr_fixture("clan")
    assert observations.admit("clan", HOME_CLAN, clan).accepted

    for name in ("riverrace_training", "riverrace_warday", "riverrace_colosseum"):
        race = load_cr_fixture(name)
        assert observations.admit("currentriverrace", HOME_CLAN, race).accepted

    for name in ("player_plain", "player_evo"):
        player = load_cr_fixture(name)
        assert observations.admit("player", player["tag"], player).accepted

    battlelog = load_cr_fixture("battlelog")
    subject = battlelog[0]["team"][0]["tag"]
    assert observations.admit("player_battlelog", subject, battlelog).accepted


def test_player_contract_rejects_absence_shape_loss_and_identity_mismatch():
    player = load_cr_fixture("player_plain")
    assert observations.admit("player", player["tag"], None).transport_failure

    empty = observations.admit("player", player["tag"], {})
    assert not empty.accepted
    assert "tag:missing" in empty.errors
    assert "cards:not_nonempty_list" in empty.errors

    missing_wins = copy.deepcopy(player)
    del missing_wins["wins"]
    assert "wins:not_int" in observations.admit("player", player["tag"], missing_wins).errors

    wrong_player = copy.deepcopy(player)
    wrong_player["tag"] = "#SOMEONEELSE"
    assert "tag:mismatch" in observations.admit("player", player["tag"], wrong_player).errors


def test_clan_and_race_contracts_reject_wrong_entity_or_missing_state():
    clan = load_cr_fixture("clan")
    wrong_clan = copy.deepcopy(clan)
    wrong_clan["tag"] = "#SOMEONEELSE"
    assert "tag:mismatch" in observations.admit("clan", HOME_CLAN, wrong_clan).errors

    no_roster = copy.deepcopy(clan)
    no_roster["memberList"] = []
    assert "memberList:not_nonempty_list" in observations.admit("clan", HOME_CLAN, no_roster).errors

    race = load_cr_fixture("riverrace_warday")
    opponent_race = copy.deepcopy(race)
    opponent_race["clan"]["tag"] = "#SOMEONEELSE"
    assert (
        "clan.tag:mismatch"
        in observations.admit("currentriverrace", HOME_CLAN, opponent_race).errors
    )

    del race["periodIndex"]
    assert "periodIndex:not_int" in observations.admit("currentriverrace", HOME_CLAN, race).errors


def test_battlelog_distinguishes_valid_empty_from_failure_and_requires_subject():
    assert observations.admit("player_battlelog", "#A", []).accepted
    assert observations.admit("player_battlelog", "#A", None).transport_failure

    battlelog = load_cr_fixture("battlelog")
    result = observations.admit("player_battlelog", "#NOTONTEAM", battlelog)
    assert not result.accepted
    assert "battles[0].team:subject_missing" in result.errors

    pve = [
        {
            "battleTime": "20260701T110000.000Z",
            "gameMode": {"id": 72000094, "name": "BoatBattle"},
            "team": [{"tag": "#A", "crowns": 1}],
            "opponent": [{}],
        }
    ]
    assert observations.admit("player_battlelog", "#A", pve).accepted


def _clan_payload():
    return {
        "tag": HOME_CLAN,
        "name": "POAP KINGS",
        "clanScore": 600,
        "clanWarTrophies": 2000,
        "memberList": [
            {
                "tag": "#A",
                "name": "Al",
                "role": "member",
                "trophies": 5000,
                "donations": 10,
                "donationsReceived": 0,
                "expLevel": 42,
                "clanRank": 1,
                "previousClanRank": 1,
            }
        ],
    }


def _race_payload():
    return {
        "state": "full",
        "sectionIndex": 0,
        "periodIndex": 0,
        "periodType": "training",
        "clan": {
            "tag": HOME_CLAN,
            "name": "POAP KINGS",
            "fame": 0,
            "participants": [],
        },
        "clans": [
            {
                "tag": HOME_CLAN,
                "name": "POAP KINGS",
                "fame": 0,
                "periodPoints": 0,
                "clanScore": 600,
            }
        ],
    }


def _player_payload():
    return {
        "tag": "#A",
        "name": "Al",
        "expLevel": 42,
        "wins": 800,
        "bestTrophies": 5300,
        "trophies": 5100,
        "cards": [
            {
                "id": 26000000,
                "name": "Knight",
                "rarity": "common",
                "level": 14,
                "maxLevel": 16,
            }
        ],
        "badges": [],
        "arena": {"id": 54000012, "name": "Spooky Town"},
        "currentPathOfLegendSeasonResult": {},
        "lastPathOfLegendSeasonResult": {},
        "bestPathOfLegendSeasonResult": {},
    }


class _Api:
    def __init__(self):
        self.player_payload = _player_payload()

    def get_clan(self):
        return _clan_payload()

    def get_current_war(self):
        return _race_payload()

    def get_player(self, tag):
        return self.player_payload

    def get_player_battle_log(self, tag):
        return []


def test_rejected_profile_does_not_mutate_baseline_or_poll_freshness(engine_conn):
    api = _Api()
    first_at = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
    first = run_tick(engine_conn, first_at, api=api)
    assert first["profile_observations_accepted"] == 1

    baseline_before = engine_conn.execute(
        "SELECT payload_json, observed_at FROM state_baselines "
        "WHERE entity_kind='player' AND entity_tag='#A' AND aspect='profile'"
    ).fetchone()
    poll_before = engine_conn.execute(
        "SELECT last_profile_poll FROM poll_state WHERE player_tag='#A'"
    ).fetchone()[0]
    assert baseline_before is not None

    # Five hours later the warm profile is due.  A decoded object without the
    # player contract is a schema/transport artifact, not an empty profile.
    api.player_payload = {}
    rejected_at = first_at + timedelta(hours=5)
    rejected = run_tick(engine_conn, rejected_at, api=api)
    assert rejected["profile_observation_rejections"] == 1
    assert rejected["profile_observation_contract_rejections"] == 1
    assert rejected["battlelog_observations_accepted"] == 1

    baseline_after = engine_conn.execute(
        "SELECT payload_json, observed_at FROM state_baselines "
        "WHERE entity_kind='player' AND entity_tag='#A' AND aspect='profile'"
    ).fetchone()
    poll_after = engine_conn.execute(
        "SELECT last_profile_poll, last_battlelog_poll FROM poll_state WHERE player_tag='#A'"
    ).fetchone()
    assert tuple(baseline_after) == tuple(baseline_before)
    assert poll_after["last_profile_poll"] == poll_before
    assert poll_after["last_battlelog_poll"] == rejected_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert engine_conn.execute("SELECT COUNT(*) FROM player_events").fetchone()[0] == 0
    assert (
        engine_conn.execute(
            "SELECT COUNT(*) FROM runtime_incidents "
            "WHERE component='engine.observation.player' AND severity='error'"
        ).fetchone()[0]
        == 1
    )

    # Repeated contract failures stay visible in per-tick counters without
    # flooding the durable incident ledger every ten minutes.
    engine_conn.execute("UPDATE poll_state SET heat=2, temperature='warm' WHERE player_tag='#A'")
    repeated = run_tick(engine_conn, rejected_at + timedelta(minutes=10), api=api)
    assert repeated["profile_observation_contract_rejections"] == 1
    assert (
        engine_conn.execute(
            "SELECT COUNT(*) FROM runtime_incidents WHERE component='engine.observation.player'"
        ).fetchone()[0]
        == 1
    )

    # A later healthy response sees the original baseline, so no synthetic
    # loss/restoration cascade is emitted.
    api.player_payload = _player_payload()
    recovered = run_tick(engine_conn, first_at + timedelta(hours=13), api=api)
    assert recovered["profile_observations_accepted"] == 1
    assert engine_conn.execute("SELECT COUNT(*) FROM player_events").fetchone()[0] == 0


def test_storage_facade_rejects_cross_entity_profile_and_bad_battlelog(engine_conn):
    import db

    wrong = _player_payload()
    wrong["tag"] = "#OTHER"
    db.snapshot_player_profile(wrong, expected_tag="#A", conn=engine_conn)
    db.snapshot_player_battlelog(
        "#A",
        [
            {
                "battleTime": "20260701T110000.000Z",
                "gameMode": {"id": 72000006, "name": "Ladder"},
                "team": [{"tag": "#OTHER", "crowns": 1}],
                "opponent": [{"tag": "#OPP", "crowns": 0}],
            }
        ],
        conn=engine_conn,
    )

    assert engine_conn.execute("SELECT COUNT(*) FROM state_baselines").fetchone()[0] == 0
    assert engine_conn.execute("SELECT COUNT(*) FROM battle_events").fetchone()[0] == 0
    assert (
        engine_conn.execute(
            "SELECT COUNT(*) FROM runtime_incidents WHERE component LIKE 'storage.snapshot_player_%'"
        ).fetchone()[0]
        == 2
    )


def test_api_receipts_are_append_only_and_generation_links_exact_input(engine_conn):
    import db
    from engine import materialize, readiness

    player = load_cr_fixture("player_plain")
    raw_key = player["tag"].lstrip("#")
    first = db._store_raw_payload(engine_conn, "player", raw_key, player)
    second = db._store_raw_payload(engine_conn, "player", raw_key, player)

    assert first["payload_id"] == second["payload_id"]
    assert first["receipt_id"] != second["receipt_id"]
    assert (
        engine_conn.execute(
            "SELECT COUNT(*) FROM raw_api_payloads WHERE endpoint = 'player'"
        ).fetchone()[0]
        == 1
    )
    assert (
        engine_conn.execute(
            "SELECT COUNT(*) FROM api_observation_receipts WHERE endpoint = 'player'"
        ).fetchone()[0]
        == 2
    )

    decision, observation = observations.observe(
        "player",
        player["tag"],
        player,
        "2026-07-15T12:00:00Z",
        source="interactive_refresh",
    )
    assert decision.accepted and observation is not None
    readiness.record_admission_decision(engine_conn, decision, player)
    materialize.apply_interactive_observation(engine_conn, observation)

    linked = engine_conn.execute(
        """SELECT mr.run_kind, mr.status, mi.receipt_id, mi.payload_hash
           FROM materialization_runs mr
           JOIN materialization_inputs mi
             ON mi.materialization_id = mr.materialization_id
           ORDER BY mr.materialization_id DESC LIMIT 1"""
    ).fetchone()
    assert linked["run_kind"] == "interactive"
    assert linked["status"] == "complete"
    assert linked["receipt_id"] == second["receipt_id"]
    assert linked["payload_hash"] == second["payload_hash"]
    assert readiness.generation_snapshot(engine_conn)["input_count"] == 1


def test_rejected_observation_marks_its_network_receipt(engine_conn):
    import db
    from engine import readiness

    malformed = {"tag": "#A"}
    receipt = db._store_raw_payload(engine_conn, "player", "A", malformed)
    decision, observation = observations.observe(
        "player", "#A", malformed, "2026-07-15T12:00:00Z", source="engine_tick"
    )
    assert observation is None
    readiness.record_admission_decision(engine_conn, decision, malformed)

    row = engine_conn.execute(
        "SELECT admission_status, admission_errors_json "
        "FROM api_observation_receipts WHERE receipt_id = ?",
        (receipt["receipt_id"],),
    ).fetchone()
    assert row["admission_status"] == "rejected"
    assert "name:not_nonempty_string" in json.loads(row["admission_errors_json"])
