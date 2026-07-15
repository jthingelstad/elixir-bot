"""Cold-start full tick — boot a TRULY empty v5.1 schema (no seeding, no home
clan row) and run one real engine.tick.run_tick. Guards the cold-start FK class
the simulator found live (roster players never ensure_player'd, no clans row) —
the "works only because migration pre-seeded" trap. The whole point is that a
fresh install must survive its first tick.
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine import db as engine_db
from engine import tick as tick_mod
from engine.tick import HOME_CLAN
from scripts.migrate_v51.schema_v51 import build

NOW = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)  # a Monday = training day


class _ColdApi:
    """Minimal cr_api-shaped stub: a two-member clan, a training-day race, no
    battles. Nothing pre-exists in the DB — the tick must create it all."""

    def get_clan(self):
        return {
            "tag": HOME_CLAN,
            "name": "COLD KINGS",
            "clanScore": 600,
            "clanWarTrophies": 2000,
            "memberList": [
                {
                    "tag": "#AAA",
                    "name": "Aaa",
                    "role": "member",
                    "expLevel": 42,
                    "trophies": 5100,
                    "donations": 120,
                    "donationsReceived": 40,
                    "clanRank": 1,
                    "previousClanRank": 1,
                    "lastSeen": "20260706T140000.000Z",
                },
                {
                    "tag": "#BBB",
                    "name": "Bbb",
                    "role": "elder",
                    "expLevel": 55,
                    "trophies": 6200,
                    "donations": 300,
                    "donationsReceived": 80,
                    "clanRank": 2,
                    "previousClanRank": 2,
                    "lastSeen": "20260706T140000.000Z",
                },
            ],
        }

    def get_current_war(self):
        return {
            "state": "full",
            "sectionIndex": 0,
            "periodIndex": 0,
            "periodType": "training",
            "clan": {
                "tag": HOME_CLAN,
                "name": "COLD KINGS",
                "fame": 0,
                "participants": [],
            },
            "clans": [
                {
                    "tag": HOME_CLAN,
                    "name": "COLD KINGS",
                    "fame": 0,
                    "periodPoints": 0,
                    "clanScore": 600,
                }
            ],
        }

    def get_player(self, tag):
        return {
            "tag": tag,
            "name": tag.strip("#"),
            "expLevel": 42,
            "trophies": 5100,
            "bestTrophies": 5300,
            "wins": 800,
            "losses": 400,
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

    def get_player_battle_log(self, tag):
        return []


def test_cold_start_tick_survives_empty_db(tmp_path):
    db_path = str(tmp_path / "cold.db")
    build(db_path, None)  # frozen carried_ddl.sql — no archive, no seeding

    conn = engine_db.connect(db_path)
    try:
        # sanity: the DB is genuinely empty (no home clan, no players)
        assert conn.execute("SELECT COUNT(*) FROM clans").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0

        counters = tick_mod.run_tick(conn, NOW, api=_ColdApi())

        # It completed and every step reported (no *_error keys = no swallowed
        # FK/exception in any step).
        assert isinstance(counters, dict)
        errors = {k: v for k, v in counters.items() if k.endswith("_error")}
        assert not errors, f"cold-start tick had step errors: {errors}"

        # First sight emits nothing, but the roster/players got established —
        # the exact thing that FK-failed before the fix.
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 2
    finally:
        conn.close()
