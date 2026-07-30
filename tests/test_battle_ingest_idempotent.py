"""Mirroring the same battlelog twice must insert nothing the second time.

The battlelog endpoint re-serves a player's last 25 battles on EVERY poll, so
ingest runs over the same battles roughly every ten minutes forever. Dedup is
therefore not an edge case — it is the steady state, and the only thing standing
between one battle and 144 copies of it a day.

It broke. `dedup_key` is built as `{player_tag}:{battle_time}:{opponent_tag}`,
so it is only stable while battle_time's FORMAT is stable. Schema v25 normalized
that column from CR-compact to ISO-Z; the same battle re-polled afterwards
hashed to a different key, INSERT OR IGNORE saw no collision, and 1,348
duplicate rows landed before anyone noticed:

    #20JJJ2CCRU:20260730T031613.000Z:#UQQ20UJU2   (stored before v25)
    #20JJJ2CCRU:2026-07-30T03:16:13Z:#UQQ20UJU2   (built after v25)

Nothing caught it because every existing test seeds `battle_events` rows
directly. Not one of them ran a battlelog through `mirror_battles` twice — the
one thing production does constantly.

These tests do exactly that, including with the raw CR-compact `battleTime` the
API actually sends, which is what makes them catch a format change at the
boundary rather than after it has multiplied the table.
"""

from __future__ import annotations

import pytest

import db
from engine.ingest import mirror_battles


def _battlelog(battle_time: str = "20260730T031613.000Z"):
    """One battle, shaped like the CR API returns it (compact battleTime)."""
    return [
        {
            "type": "PvP",
            "battleTime": battle_time,
            "gameMode": {"id": 72000006, "name": "Ladder"},
            "arena": {"id": 54000012, "name": "Legendary Arena"},
            "deckSelection": "collection",
            "team": [
                {
                    "tag": "#AAA",
                    "crowns": 1,
                    "trophyChange": 30,
                    "cards": [
                        {"id": 26000000 + i, "name": f"Card{i}", "level": 11} for i in range(8)
                    ],
                }
            ],
            "opponent": [
                {
                    "tag": "#BBB",
                    "crowns": 0,
                    "cards": [
                        {"id": 26000100 + i, "name": f"Opp{i}", "level": 11} for i in range(8)
                    ],
                }
            ],
        }
    ]


@pytest.fixture
def ingest_db(tmp_path, monkeypatch):
    path = str(tmp_path / "ingest.db")
    original = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: original(path))
    conn = original(path)
    try:
        conn.execute(
            "INSERT INTO players (player_tag, current_name, display_name, "
            "first_seen_at, last_seen_at) VALUES ('#AAA','A','A','2026-05-01','2026-07-30')"
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM battle_events").fetchone()[0]


def test_the_same_battlelog_twice_inserts_once(ingest_db):
    """The steady state: every poll re-serves the same battles."""
    first = mirror_battles(ingest_db, "#AAA", _battlelog(), "2026-07-30T03:20:00Z", None)
    ingest_db.commit()
    assert first == 1
    assert _count(ingest_db) == 1

    second = mirror_battles(ingest_db, "#AAA", _battlelog(), "2026-07-30T03:30:00Z", None)
    ingest_db.commit()

    assert second == 0, "a re-poll inserted a duplicate battle"
    assert _count(ingest_db) == 1


def test_ten_polls_still_leave_one_row(ingest_db):
    """144 polls a day; a key that drifts multiplies the table quietly."""
    for minute in range(10):
        mirror_battles(ingest_db, "#AAA", _battlelog(), f"2026-07-30T04:{minute:02d}:00Z", None)
    ingest_db.commit()

    assert _count(ingest_db) == 1


def test_the_key_matches_the_stored_battle_time(ingest_db):
    """The regression, stated directly: the key must be derivable from the row.

    If these ever disagree, the same battle re-polled will hash to a new key
    and dedup silently stops working — which is exactly what v25 caused.
    """
    mirror_battles(ingest_db, "#AAA", _battlelog(), "2026-07-30T03:20:00Z", None)
    ingest_db.commit()

    row = ingest_db.execute(
        "SELECT dedup_key, player_tag, battle_time, opponent_tag FROM battle_events"
    ).fetchone()
    expected = f"{row['player_tag']}:{row['battle_time']}:{row['opponent_tag']}"

    assert row["dedup_key"] == expected, (
        f"dedup_key {row['dedup_key']!r} is not derivable from the row it describes "
        f"(expected {expected!r}) — a battle_time format change will silently "
        "break deduplication again"
    )


def test_battle_time_is_stored_canonical_whatever_the_api_sends(ingest_db):
    """Compact in, ISO-Z stored. The conversion happens at the boundary, so the
    key is built from the canonical value rather than the wire format."""
    mirror_battles(ingest_db, "#AAA", _battlelog(), "2026-07-30T03:20:00Z", None)
    ingest_db.commit()

    stored = ingest_db.execute("SELECT battle_time FROM battle_events").fetchone()[0]
    assert stored == "2026-07-30T03:16:13Z", f"stored non-canonically: {stored!r}"


def test_a_genuinely_different_battle_still_inserts(ingest_db):
    """The guard must not over-collapse — different battles stay distinct."""
    mirror_battles(ingest_db, "#AAA", _battlelog(), "2026-07-30T03:20:00Z", None)
    mirror_battles(
        ingest_db, "#AAA", _battlelog("20260730T041613.000Z"), "2026-07-30T04:20:00Z", None
    )
    ingest_db.commit()

    assert _count(ingest_db) == 2
