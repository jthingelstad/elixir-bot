"""Resolve Elixir's R<n>, L<n>, and M<n> shorthand codes."""

from __future__ import annotations

import json

from agent.tool_exec import _execute_lookup_reference

NOW = "2026-07-01T12:00:00Z"


def _seed_leader_action(conn):
    conn.execute(
        "INSERT INTO leader_action_recommendations "
        "(action_id, action_key, action_type, objective, prompt_text, status, "
        " target_player_tag, target_player_name, rationale, copy_current_text, "
        " proposed_at, created_at, updated_at) "
        "VALUES (137, 'kick:test137', 'kick_recommendation', "
        " 'Review kick candidacy for pokemon', 'Review kick candidacy for pokemon.', "
        " 'proposed', '#V0CRYP2GG', "
        " 'pokemon', 'idle 9.5 days', 'heads up, pokemon has been idle - E', "
        " ?, ?, ?)",
        (NOW, NOW, NOW),
    )
    conn.commit()


def _seed_loop(conn):
    from runtime.awareness.store import ensure_awareness_schema

    ensure_awareness_schema(conn)
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "milestone",
                "summary": "Gem triple milestone",
                "member_names": ["Gem"],
            }
        ]
    }
    read = {"_error": None, "_degraded": [], "hard_post_signals": []}
    conn.execute(
        "INSERT INTO awareness_thoughts "
        "(thought_id, at, read_json, plan_json, chose_silence, post_count, loop_number) "
        "VALUES ('t60', ?, ?, ?, 0, 1, 60)",
        (NOW, json.dumps(read), json.dumps(plan)),
    )
    conn.commit()


def test_resolve_leader_action_reference(engine_conn, _isolate_default_sqlite_db):
    _seed_leader_action(engine_conn)
    out = _execute_lookup_reference({"reference": "R137"})
    assert out["kind"] == "leader_action"
    assert out["action_id"] == 137
    assert out["target_member"] == "pokemon"
    assert out["status"] == "proposed"
    # case-insensitive + bare-number-with-kind resolve the same row
    assert _execute_lookup_reference({"reference": "r137"})["action_id"] == 137
    assert (
        _execute_lookup_reference({"reference": "137", "kind": "leader_action"})["action_id"] == 137
    )


def test_resolve_loop_reference(engine_conn, _isolate_default_sqlite_db):
    _seed_loop(engine_conn)
    out = _execute_lookup_reference({"reference": "L60"})
    assert out["kind"] == "loop"
    assert out["loop_number"] == 60
    assert out["decision"] == "posted"
    assert out["posts"][0]["members"] == ["Gem"]
    assert out["read_health"]["hard_post_signal_count"] == 0


def test_resolve_memory_reference(_isolate_default_sqlite_db):
    from memory_store import create_memory

    mem = create_memory(
        body="pokemon idle 9.5 days; watching for one more week before a kick card.",
        source_type="leader_note",
        is_inference=False,
        confidence=1.0,
        created_by="test",
        scope="leadership",
        title="Watch: pokemon",
    )
    out = _execute_lookup_reference({"reference": f"M{mem['memory_id']}"}, workflow="clanops")
    assert out["kind"] == "memory"
    assert out["memory_id"] == mem["memory_id"]
    assert out["title"] == "Watch: pokemon"
    assert "idle" in out["body"]


def test_reference_error_cases(engine_conn, _isolate_default_sqlite_db):
    from runtime.awareness.store import ensure_awareness_schema

    ensure_awareness_schema(engine_conn)  # so an L<n> miss is not_found, not a crash
    assert _execute_lookup_reference({"reference": "banana"})["error"] == "unparseable_reference"
    # bare number with no letter and no kind is ambiguous
    assert _execute_lookup_reference({"reference": "500"})["error"] == "ambiguous_reference"
    # well-formed but nonexistent
    assert _execute_lookup_reference({"reference": "R9999"})["error"] == "not_found"
    assert _execute_lookup_reference({"reference": "L9999"})["error"] == "not_found"
    assert _execute_lookup_reference({"reference": "C9999"})["error"] == "unparseable_reference"
    assert (
        _execute_lookup_reference({"reference": "9999", "kind": "case"})["error"]
        == "retired_reference_kind"
    )
    assert (
        _execute_lookup_reference({"reference": "M9999"}, workflow="clanops")["error"]
        == "not_found"
    )
