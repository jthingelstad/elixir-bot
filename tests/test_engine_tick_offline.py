"""OfflineEngine end-to-end on a tiny synthetic payload sequence."""

from __future__ import annotations

import inspect
import json
import sqlite3

from engine.offline import OfflineEngine
from engine.tick import run_tick


def _clan(members):
    return json.dumps(
        {
            "tag": "#J2RGCRVG",
            "name": "POAP KINGS",
            "clanScore": 60000,
            "clanWarTrophies": 900,
            "memberList": [
                {
                    "tag": t,
                    "name": n,
                    "role": "member",
                    "trophies": 5000,
                    "donations": d,
                    "donationsReceived": 0,
                    "expLevel": 40,
                    "clanRank": index + 1,
                    "previousClanRank": index + 1,
                }
                for index, (t, n, d) in enumerate(members)
            ],
        }
    )


def _profile(tag, name, level, wins, collection_level=None):
    badges = []
    if collection_level is not None:
        # Collection Level rides the CollectionLevel badge (the CR 2026 metric);
        # it projects into the cards aspect where collection_level_milestone fires.
        badges.append(
            {"name": "CollectionLevel", "level": 8, "progress": collection_level}
        )
    return json.dumps(
        {
            "tag": tag,
            "name": name,
            "expLevel": level,
            "wins": wins,
            "bestTrophies": 6000,
            "trophies": 5500,
            "arena": {"id": 54000012, "name": "Spooky Town"},
            "badges": badges,
            "cards": [
                {
                    "id": 26000000,
                    "name": "Knight",
                    "rarity": "common",
                    "level": 14,
                    "maxLevel": 16,
                }
            ],
            "currentPathOfLegendSeasonResult": {},
            "lastPathOfLegendSeasonResult": {},
            "bestPathOfLegendSeasonResult": {},
        }
    )


def _battlelog(opponent="#OPP"):
    return json.dumps(
        [
            {
                "type": "PvP",
                "battleTime": "20260701T110000.000Z",
                "gameMode": {"id": 72000006, "name": "Ladder"},
                "arena": {"id": 54000012, "name": "Spooky Town"},
                "team": [
                    {
                        "tag": "#A",
                        "crowns": 2,
                        "trophyChange": 30,
                        "startingTrophies": 5470,
                        "cards": [],
                    }
                ],
                "opponent": [{"tag": opponent, "crowns": 0, "cards": []}],
            }
        ]
    )


def test_offline_end_to_end(tmp_path, v51_schema_template):
    import shutil

    db_path = str(tmp_path / "offline.db")
    shutil.copy(v51_schema_template, db_path)
    eng = OfflineEngine(db_path)
    # simulate T3 (production seeds the home clan row before any join event)
    eng.conn.execute(
        "INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-01', 1)"
    )
    eng.conn.commit()

    # first roster poll is first-sight (silent baseline; tenure comes from the
    # T5 transform in production); the second poll's join is the event path
    eng.apply(
        "clan",
        "#J2RGCRVG",
        _clan([("#A", "Al", 10), ("#B", "Bo", 20)]),
        "2026-07-01T09:50:00Z",
    )
    members = [("#A", "Al", 10), ("#B", "Bo", 20), ("#C", "Cy", 0)]
    eng.apply("clan", "#J2RGCRVG", _clan(members), "2026-07-01T10:00:00Z")
    eng.apply(
        "player",
        "#A",
        _profile("#A", "Al", 44, 4990, collection_level=1673),
        "2026-07-01T10:01:00Z",
    )
    eng.apply("player_battlelog", "#A", _battlelog(), "2026-07-01T10:02:00Z")
    # second observation: collection level crosses 1700 (bypass → posts)
    eng.apply(
        "player",
        "#A",
        _profile("#A", "Al", 45, 4991, collection_level=1712),
        "2026-07-01T11:00:00Z",
    )
    # The retired deterministic poster remains available only as an explicit
    # shadow seam; awareness-only is the production/offline default.
    counters = eng.finish(legacy_proactive=True)

    conn = eng.conn
    # events: collection_level_milestone emitted exactly once
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM player_events WHERE event_type='collection_level_milestone'"
        ).fetchone()[0]
        == 1
    )
    # battle mirrored once, dedup-keyed
    assert conn.execute("SELECT COUNT(*) FROM battle_events").fetchone()[0] == 1
    # projections: current state and form exist for #A
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM player_recent_form WHERE player_tag='#A'"
        ).fetchone()[0]
        >= 1
    )
    # exactly one intent for the collection-level moment, and it delivered offline
    assert counters.get("deliver_delivered", 0) >= 1
    # zero duplicate ledger claims (the Phase 6 acceptance rule)
    dupes = conn.execute(
        "SELECT COUNT(*) - COUNT(DISTINCT recognition_key) FROM recognition_ledger"
    ).fetchone()[0]
    assert dupes == 0
    # the second roster poll's join opened #C's membership (first-sight roster
    # is deliberately silent — production tenure comes from the T5 transform)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM clan_memberships WHERE left_at IS NULL AND player_tag='#C'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM clan_events WHERE event_type='member_joined'"
        ).fetchone()[0]
        == 1
    )
    eng.close()


def test_offline_finish_defaults_to_awareness_only(tmp_path, v51_schema_template):
    import shutil

    db_path = str(tmp_path / "offline-awareness.db")
    shutil.copy(v51_schema_template, db_path)
    eng = OfflineEngine(db_path)
    eng.apply(
        "player",
        "#A",
        _profile("#A", "Al", 44, 4990, collection_level=1673),
        "2026-07-01T10:00:00Z",
    )
    eng.apply(
        "player",
        "#A",
        _profile("#A", "Al", 45, 4990, collection_level=1712),
        "2026-07-01T11:00:00Z",
    )

    counters = eng.finish()

    assert counters["proactive_mode"] == "awareness_only"
    assert (
        eng.conn.execute("SELECT COUNT(*) FROM recognition_ledger").fetchone()[0] == 0
    )
    assert (
        eng.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='communication_intents'"
        ).fetchone()
        is None
    )
    eng.close()


def test_offline_replay_idempotent(tmp_path, v51_schema_template):
    import shutil

    db_path = str(tmp_path / "offline2.db")
    shutil.copy(v51_schema_template, db_path)
    eng = OfflineEngine(db_path)
    payload = _profile("#A", "Al", 44, 4990, collection_level=1673)
    eng.apply("player", "#A", payload, "2026-07-01T10:00:00Z")
    eng.apply(
        "player",
        "#A",
        _profile("#A", "Al", 45, 4990, collection_level=1712),
        "2026-07-01T11:00:00Z",
    )
    # replaying the same observation emits nothing new
    eng.apply(
        "player",
        "#A",
        _profile("#A", "Al", 45, 4990, collection_level=1712),
        "2026-07-01T12:00:00Z",
    )
    assert (
        eng.conn.execute(
            "SELECT COUNT(*) FROM player_events WHERE event_type='collection_level_milestone'"
        ).fetchone()[0]
        == 1
    )
    eng.close()


def test_tick_ingest_commit_survives_later_emit_failure(
    tmp_path, v51_schema_template, monkeypatch
):
    import shutil

    db_path = str(tmp_path / "tick-ingest.db")
    shutil.copy(v51_schema_template, db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    class Api:
        def get_clan(self):
            return json.loads(_clan([("#A", "Al", 10)]))

        def get_current_war(self):
            return None

        def get_player_battle_log(self, tag):
            return json.loads(_battlelog())

        def get_player(self, tag):
            return json.loads(_profile(tag, "Al", 44, 4990))

    def boom(*args, **kwargs):
        raise RuntimeError("emit failed after ingest")

    monkeypatch.setattr("engine.tick.emit", boom)

    try:
        counters = run_tick(
            conn,
            api=Api(),
        )
        assert "emit_error" in counters
        assert conn.execute("SELECT COUNT(*) FROM battle_events").fetchone()[0] == 1
    finally:
        conn.close()


def test_production_tick_has_no_legacy_delivery_switch():
    assert set(inspect.signature(run_tick).parameters) == {"conn", "now", "api"}
