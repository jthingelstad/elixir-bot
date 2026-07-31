"""Time windows, mode scopes, and clan-relative standing (question-audit fixes).

A 20-question audit of realistic member questions found: no time dimension at all
("am I improving?" unanswerable), scope="competitive" covering 96% of battles so war
and ranked could not be isolated, and no clan-relative framing for "am I above average?".
"""

import sqlite3

from capabilities.battle_intel import _plays_from, _since, get_battle_intelligence
from db.schema import build_database


def _db(tmp_path):
    path = tmp_path / "t.db"
    build_database(str(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_since_bounds_and_disables():
    assert _since(None) is None
    assert _since(0) is None
    assert _since(7) is not None
    assert _since(100000) == _since(365), "window is clamped to a year"


def test_mode_scopes_join_battle_events_but_competitive_does_not():
    """battle_card_plays only denormalized is_competitive; war/ranked/ladder must join."""
    assert "JOIN battle_events" in _plays_from("war")
    assert "JOIN battle_events" in _plays_from("ranked")
    assert "JOIN battle_events" in _plays_from("ladder")
    assert "JOIN" not in _plays_from("competitive")
    assert "JOIN" not in _plays_from("all")


def test_every_windowed_view_reports_the_window_it_used(tmp_path):
    """A view that silently ignores `days` would quietly answer the wrong question."""
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO card_catalog (card_id, name, card_type, synced_at, first_seen_at) "
        "VALUES (1, 'Arrows', 'spell', 'x', 'x')"
    )
    conn.commit()
    for view, kwargs in (
        ("card", {"card": "Arrows"}),
        ("nemesis", {}),
    ):
        r = get_battle_intelligence(view=view, days=7, conn=conn, **kwargs)
        assert r.get("window_days") == 7, f"{view} did not report its window"
    conn.close()


def test_unknown_scope_falls_back_to_all_rather_than_crashing(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO card_catalog (card_id, name, card_type, synced_at, first_seen_at) "
        "VALUES (1, 'Arrows', 'spell', 'x', 'x')"
    )
    conn.commit()
    r = get_battle_intelligence(view="card", card="Arrows", scope="nonsense", conn=conn)
    assert r["available"] is True
    conn.close()
