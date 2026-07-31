"""Tests for the get_battle_intelligence capability views + n>=30 claim floor."""

import sqlite3

from capabilities.battle_intel import get_battle_intelligence
from db.schema import build_database


def _db(tmp_path):
    path = tmp_path / "t.db"
    build_database(str(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # a catalog card to resolve by name
    conn.execute(
        "INSERT INTO card_catalog (card_id, name, card_type, synced_at, first_seen_at) "
        "VALUES (26000059, 'Royal Hogs', 'troop', 'x', 'x')"
    )
    return conn


def _plays(conn, n, side, wins, *, evo=None):
    for i in range(n):
        conn.execute(
            "INSERT OR IGNORE INTO battle_card_plays "
            "(battle_dedup_key, side, card_id, evolution_level, player_tag, battle_time, "
            " outcome, is_competitive) VALUES (?, ?, 26000059, ?, '#M', ?, ?, 1)",
            (
                f"{side}-{evo}-{i}",
                side,
                evo,
                f"2026-07-20T00:00:{i:02d}Z",
                "W" if i < wins else "L",
            ),
        )
    conn.commit()


def test_card_view_applies_n30_floor(tmp_path):
    conn = _db(tmp_path)
    _plays(conn, 40, "member", 24)  # 60% over n=40 -> reported
    _plays(conn, 10, "opponent", 3)  # n=10 -> insufficient
    r = get_battle_intelligence(view="card", member_tag="#M", card="Royal Hogs", conn=conn)
    playing = {e["card"]: e for e in r["playing"]}
    facing = {e["card"]: e for e in r["facing"]}
    assert playing["Royal Hogs"]["win_rate"] == 0.6
    assert playing["Royal Hogs"]["insufficient_sample"] is False
    assert facing["Royal Hogs"]["win_rate"] is None
    assert facing["Royal Hogs"]["insufficient_sample"] is True


def test_card_view_is_form_aware(tmp_path):
    conn = _db(tmp_path)
    _plays(conn, 30, "member", 15, evo=None)  # base
    _plays(conn, 30, "member", 27, evo=1)  # Evo — distinct card, different rate
    r = get_battle_intelligence(view="card", member_tag="#M", card="Royal Hogs", conn=conn)
    labels = {e["card"]: e["win_rate"] for e in r["playing"]}
    assert labels["Royal Hogs"] == 0.5
    assert labels["Evo Royal Hogs"] == 0.9


def test_unknown_card_and_missing_tag(tmp_path):
    conn = _db(tmp_path)
    assert (
        get_battle_intelligence(view="card", card="Nonexistent", conn=conn)["error"]
        == "unknown_card"
    )
    assert get_battle_intelligence(view="battle", conn=conn)["error"] == "member_tag_required"


def test_unsupported_view(tmp_path):
    conn = _db(tmp_path)
    assert get_battle_intelligence(view="bogus", conn=conn)["error"] == "unsupported_view"
