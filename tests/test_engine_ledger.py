"""Recognition ledger: one real moment → one post (architecture §10)."""
from __future__ import annotations

import json

from engine import recognition
from engine.db import cursor_get, cursor_set
from engine.recognition import ledger
from engine.recognition.recognizers import player_candidates, run_celebrate_pipeline
from engine.recognition.scorer import REASON_ACCRUING, REASON_BACKGROUND, REASON_COHORT

KEY = "arena_up:#A:54000013"
NOW = "2026-07-01T12:00:00Z"


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


def test_cohort_wave_uses_same_day_suppressed_events_across_ticks(engine_conn):
    for tag, name in (("#A", "Al"), ("#B", "Bo"), ("#C", "Cy")):
        engine_conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (tag, name, NOW, NOW),
        )
        engine_conn.execute(
            """INSERT INTO player_events
                 (dedup_key, event_type, player_tag, observed_at, payload_json, created_at)
               VALUES (?, 'badge_earned', ?, ?, ?, ?)""",
            (
                f"badge_earned:{tag}:badge",
                tag,
                NOW,
                json.dumps({"badge_name": "Tenacious"}),
                NOW,
            ),
        )
    engine_conn.commit()
    for tag in ("#A", "#B"):
        key = f"badge_earned:{tag}:badge"
        assert ledger.claim(engine_conn, key, "player", [key], 55)
        ledger.record_suppression(engine_conn, key, REASON_ACCRUING, {"score": 55})
    cursor_set(engine_conn, "recognize:player", 2)

    cands, _ = player_candidates(engine_conn)
    counters = run_celebrate_pipeline(engine_conn, cands, NOW)

    assert counters["cohort_posted"] == 1
    assert engine_conn.execute(
        "SELECT COUNT(*) FROM communication_intents WHERE intent_type = 'cohort:cohort_wave'"
    ).fetchone()[0] == 1
    for tag in ("#A", "#B", "#C"):
        key = f"badge_earned:{tag}:badge"
        blob = json.loads(engine_conn.execute(
            "SELECT event_refs_json FROM recognition_ledger WHERE recognition_key = ?",
            (key,),
        ).fetchone()[0])
        assert blob["suppressed"]["reason"] == REASON_COHORT


def _seed_player(conn, tag, name):
    conn.execute("INSERT OR IGNORE INTO players (player_tag, current_name, first_seen_at, "
                 "last_seen_at) VALUES (?, ?, ?, ?)", (tag, name, NOW, NOW))


def _seed_event(conn, tag, etype, payload, key):
    conn.execute("""INSERT INTO player_events
                      (dedup_key, event_type, player_tag, observed_at, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                 (key, etype, tag, NOW, json.dumps(payload), NOW))


def _run(conn):
    cursor_set(conn, "recognize:player", 0)
    cands, _ = player_candidates(conn)
    return run_celebrate_pipeline(conn, cands, NOW)


def test_card_grind_is_background_not_posted(engine_conn):
    # Clan policy (2026-07-07): card level-ups are context, not a highlight.
    _seed_player(engine_conn, "#A", "Al")
    _seed_event(engine_conn, "#A", "card_level_milestone",
                {"card_name": "Prince", "milestone": 16}, key="cl:#A")
    engine_conn.commit()
    counters = _run(engine_conn)
    assert counters["celebrate_posted"] == 0
    assert engine_conn.execute(
        "SELECT COUNT(*) FROM communication_intents WHERE lane = 'elixir'"
    ).fetchone()[0] == 0
    # the event still exists as context, and the suppression reason is recorded
    assert engine_conn.execute(
        "SELECT COUNT(*) FROM player_events WHERE event_type = 'card_level_milestone'"
    ).fetchone()[0] == 1
    blob = json.loads(engine_conn.execute(
        "SELECT event_refs_json FROM recognition_ledger WHERE recognition_key = 'cl:#A'"
    ).fetchone()[0])
    assert blob["suppressed"]["reason"] == REASON_BACKGROUND


def test_mastery_badge_background_but_real_badge_posts_via_bypass_peer(engine_conn):
    # A card level-up (score 95, prio 80) must NOT coalesce away a real moment
    # sharing the window — the collection milestone (bypass) still posts.
    _seed_player(engine_conn, "#A", "Al")
    _seed_event(engine_conn, "#A", "card_level_milestone",
                {"card_name": "Prince", "milestone": 16}, key="cl:#A")
    _seed_event(engine_conn, "#A", "collection_level_milestone",
                {"milestone": 42}, key="col:#A")
    _seed_event(engine_conn, "#A", "badge_earned",
                {"badge_name": "MasteryRonin"}, key="mb:#A")
    engine_conn.commit()
    _run(engine_conn)
    posted = engine_conn.execute(
        "SELECT intent_type FROM communication_intents WHERE lane = 'elixir'"
    ).fetchall()
    assert [r["intent_type"] for r in posted] == ["celebrate:collection_level_milestone"]
    # both card-grind moments suppressed as background
    for key in ("cl:#A", "mb:#A"):
        blob = json.loads(engine_conn.execute(
            "SELECT event_refs_json FROM recognition_ledger WHERE recognition_key = ?",
            (key,)).fetchone()[0])
        assert blob["suppressed"]["reason"] == REASON_BACKGROUND


def test_card_unlock_wave_still_posts(engine_conn):
    # Card-grind is background individually but STILL forms cohort waves (kept).
    for tag, name in (("#A", "Al"), ("#B", "Bo"), ("#C", "Cy")):
        _seed_player(engine_conn, tag, name)
        _seed_event(engine_conn, tag, "card_unlocked",
                    {"card_name": "Ronin", "rarity": "legendary"}, key=f"cu:{tag}")
    engine_conn.commit()
    counters = _run(engine_conn)
    assert counters["cohort_posted"] == 1
    assert counters["celebrate_posted"] == 0
    assert engine_conn.execute(
        "SELECT COUNT(*) FROM communication_intents WHERE intent_type = 'cohort:cohort_wave'"
    ).fetchone()[0] == 1


def test_poison_event_skips_after_three_repeated_failures(engine_conn, monkeypatch):
    engine_conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES ('#A', 'Al', ?, ?)",
        (NOW, NOW),
    )
    engine_conn.execute(
        """INSERT INTO player_events
             (dedup_key, event_type, player_tag, observed_at, payload_json, created_at)
           VALUES ('bad:#A', 'badge_earned', '#A', ?, ?, ?)""",
        (NOW, json.dumps({"badge_name": "Bad"}), NOW),
    )
    engine_conn.commit()

    def boom(conn):
        raise ValueError("bad player event")

    monkeypatch.setattr("engine.recognition.recognizers.player_candidates", boom)

    for _ in range(3):
        recognition.run_recognizers(engine_conn, {}, NOW)

    assert cursor_get(engine_conn, "recognize:player") == 1
    assert engine_conn.execute(
        "SELECT COUNT(*) FROM prompt_failures WHERE failure_type = 'poison_event'"
    ).fetchone()[0] == 1
