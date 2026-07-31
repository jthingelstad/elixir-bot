"""Stage-A worker regression tests: card-play extraction, duel/2v2 skip,
idempotency, and the ranked level_gap guard (Battle Intelligence F1)."""

import json
import sqlite3

from db.schema import build_database
from storage.battle_intel import enrich_battles


def _deck(*ids, evo=None):
    cards = [{"id": i, "name": f"c{i}", "level": 11, "evolution_level": None} for i in ids]
    if evo is not None:
        cards[0]["evolution_level"] = evo
    return json.dumps(cards)


def _insert_battle(conn, key, **cols):
    base = {
        "dedup_key": key,
        "player_tag": "#M",
        "battle_time": "2026-07-20T00:00:00Z",
        "observed_at": "2026-07-20T00:05:00Z",
        "outcome": "W",
        "mode_group": "ladder",
        "is_competitive": 1,
        "is_ranked": 0,
    }
    base.update(cols)
    keys = ", ".join(base)
    conn.execute(
        f"INSERT INTO battle_events ({keys}) VALUES ({', '.join('?' for _ in base)})",
        tuple(base.values()),
    )


def _db(tmp_path):
    path = tmp_path / "t.db"
    build_database(str(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_enriches_1v1_both_sides_and_skips_duel_and_2v2(tmp_path):
    conn = _db(tmp_path)
    # a clean 1v1 with both decks
    _insert_battle(
        conn,
        "b1",
        deck_json=_deck(1, 2, 3, 4, 5, 6, 7, 8),
        opponent_deck_json=_deck(9, 10, 11, 12, 13, 14, 15, 16),
    )
    # a duel (rounds_json) and a 2v2 (teammate_tag) — must be skipped
    _insert_battle(conn, "b2", deck_json=_deck(1, 2, 3, 4, 5, 6, 7, 8), rounds_json="[]")
    _insert_battle(conn, "b3", deck_json=_deck(1, 2, 3, 4, 5, 6, 7, 8), teammate_tag="#T")
    conn.commit()

    result = enrich_battles(100, conn=conn)
    assert result["enriched"] == 1  # only the 1v1
    assert result["card_plays"] == 16  # 8 member + 8 opponent

    enr = conn.execute("SELECT COUNT(*) FROM battle_enrichment").fetchone()[0]
    assert enr == 1
    sides = dict(
        conn.execute("SELECT side, COUNT(*) FROM battle_card_plays GROUP BY side").fetchall()
    )
    assert sides == {"member": 8, "opponent": 8}
    # duel/2v2 left no rows
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM battle_enrichment WHERE battle_dedup_key IN ('b2','b3')"
        ).fetchone()[0]
        == 0
    )


def test_member_only_when_opponent_deck_absent(tmp_path):
    conn = _db(tmp_path)
    _insert_battle(conn, "b1", deck_json=_deck(1, 2, 3, 4, 5, 6, 7, 8))  # no opponent deck
    conn.commit()
    result = enrich_battles(100, conn=conn)
    assert result["card_plays"] == 8  # member only
    row = conn.execute("SELECT our_deck_hash, their_deck_hash FROM battle_enrichment").fetchone()
    assert row["our_deck_hash"] is not None
    assert row["their_deck_hash"] is None


def test_idempotent_rerun_is_noop(tmp_path):
    conn = _db(tmp_path)
    _insert_battle(
        conn,
        "b1",
        deck_json=_deck(1, 2, 3, 4, 5, 6, 7, 8),
        opponent_deck_json=_deck(9, 10, 11, 12, 13, 14, 15, 16),
    )
    conn.commit()
    first = enrich_battles(100, conn=conn)
    second = enrich_battles(100, conn=conn)
    assert first["enriched"] == 1
    assert second == {"enriched": 0, "card_plays": 0, "scanned": 0}


def test_ranked_suppresses_level_gap(tmp_path):
    conn = _db(tmp_path)
    _insert_battle(
        conn,
        "b1",
        is_ranked=1,
        deck_json=_deck(1, 2, 3, 4, 5, 6, 7, 8),
        opponent_deck_json=_deck(9, 10, 11, 12, 13, 14, 15, 16),
    )
    conn.commit()
    enrich_battles(100, conn=conn)
    assert conn.execute("SELECT level_gap FROM battle_enrichment").fetchone()[0] is None
