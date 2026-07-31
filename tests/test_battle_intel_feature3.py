"""Feature 3: the prose gate (allowlist ∩ date), idempotency, and CHECK guard.
The LLM is mocked — no real Haiku call in the test."""

import sqlite3
from unittest.mock import patch

from db.schema import build_database
from storage.battle_intel import PROSE_PROMPT_VERSION, generate_prose_batch


def _db(tmp_path):
    path = tmp_path / "t.db"
    build_database(str(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for tag, enabled in (("#ALLOW", 1), ("#DENY", 0)):
        conn.execute(
            "INSERT INTO players (player_tag, first_seen_at, last_seen_at) VALUES (?, 'x', 'x')",
            (tag,),
        )
        conn.execute(
            "INSERT INTO player_metadata (player_tag, battle_enrichment_enabled) VALUES (?, ?)",
            (tag, enabled),
        )
    conn.commit()
    return conn


def _battle(conn, key, tag, when):
    conn.execute(
        "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at, outcome) "
        "VALUES (?, ?, ?, 'x', 'L')",
        (key, tag, when),
    )
    conn.execute(
        "INSERT INTO battle_enrichment (battle_dedup_key, player_tag, battle_time, hp_margin, "
        "closeness, performance, our_deck_hash, their_deck_hash) "
        "VALUES (?, ?, ?, -2504, 3, -1, 'h1', 'h2')",
        (key, tag, when),
    )


_FAKE = {
    "commentary": "A squeaker that slipped away.",
    "loss_nature": "close",
    "notable": 1,
    "confidence": "high",
}


def test_gate_allowlist_and_date(tmp_path):
    conn = _db(tmp_path)
    _battle(conn, "ok", "#ALLOW", "2026-07-21T00:00:00Z")  # gated: allowed + after date
    _battle(conn, "old", "#ALLOW", "2026-07-10T00:00:00Z")  # before date -> excluded
    _battle(conn, "denied", "#DENY", "2026-07-21T00:00:00Z")  # not allowlisted -> excluded
    conn.commit()

    with patch("elixir_agent.generate_battle_prose", return_value=_FAKE):
        result = generate_prose_batch(50, conn=conn)
    assert result["prose_written"] == 1  # only the gated battle

    prosed = {
        r[0]
        for r in conn.execute(
            "SELECT battle_dedup_key FROM battle_enrichment WHERE commentary IS NOT NULL"
        )
    }
    assert prosed == {"ok"}
    row = conn.execute(
        "SELECT loss_nature, notable, prompt_version FROM battle_enrichment WHERE battle_dedup_key='ok'"
    ).fetchone()
    assert row["loss_nature"] == "close"
    assert row["notable"] == 1
    assert row["prompt_version"] == PROSE_PROMPT_VERSION


def test_idempotent_no_regenerate(tmp_path):
    conn = _db(tmp_path)
    _battle(conn, "ok", "#ALLOW", "2026-07-21T00:00:00Z")
    conn.commit()
    with patch("elixir_agent.generate_battle_prose", return_value=_FAKE) as m:
        first = generate_prose_batch(50, conn=conn)
        second = generate_prose_batch(50, conn=conn)
    assert first["prose_written"] == 1
    assert second["prose_written"] == 0 and second["scanned"] == 0  # input_hash unchanged
    assert m.call_count == 1  # LLM called once, not twice


def test_invalid_loss_nature_is_nulled(tmp_path):
    conn = _db(tmp_path)
    _battle(conn, "ok", "#ALLOW", "2026-07-21T00:00:00Z")
    conn.commit()
    bad = {**_FAKE, "loss_nature": "made-up-value"}  # not in the CHECK enum
    with patch("elixir_agent.generate_battle_prose", return_value=bad):
        generate_prose_batch(50, conn=conn)  # must not raise the CHECK constraint
    assert (
        conn.execute(
            "SELECT loss_nature FROM battle_enrichment WHERE battle_dedup_key='ok'"
        ).fetchone()[0]
        is None
    )
