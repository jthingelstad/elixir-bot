"""Recognition ledger: one real moment → one post (architecture §10)."""
from __future__ import annotations

import json

from engine.recognition import ledger

KEY = "arena_up:#A:54000013"


def test_two_claimants_one_key_exactly_one_wins(engine_conn):
    first = ledger.claim(engine_conn, KEY, "battle", ["b1"], 85)
    second = ledger.claim(engine_conn, KEY, "player", ["p1"], 85)
    assert first is True and second is False
    rows = engine_conn.execute(
        "SELECT stream FROM recognition_ledger WHERE recognition_key = ?", (KEY,)
    ).fetchall()
    assert len(rows) == 1 and rows[0]["stream"] == "battle"


def test_suppressed_claims_have_null_intent(engine_conn):
    ledger.claim(engine_conn, KEY, "player", ["p1"], 40)
    ledger.record_suppression(engine_conn, KEY, "player_highlight_accruing",
                              {"score": 40, "threshold": 80})
    row = engine_conn.execute(
        "SELECT intent_id, event_refs_json FROM recognition_ledger "
        "WHERE recognition_key = ?", (KEY,)).fetchone()
    assert row["intent_id"] is None
    refs = json.loads(row["event_refs_json"])
    text = json.dumps(refs)
    assert "player_highlight_accruing" in text  # "why didn't you post X?" answerable


def test_attach_intent(engine_conn):
    ledger.claim(engine_conn, KEY, "player", ["p1"], 85)
    ledger.attach_intent(engine_conn, KEY, 42)
    row = engine_conn.execute(
        "SELECT intent_id FROM recognition_ledger WHERE recognition_key = ?",
        (KEY,)).fetchone()
    assert row["intent_id"] == 42
