"""The v5.1 baseline and its explicit forward-schema contract."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import db
from db.schema import (
    CURRENT_SCHEMA_FINGERPRINT,
    CURRENT_SCHEMA_VERSION,
    apply_schema_migrations,
    schema_fingerprint,
)
from scripts.migrate_v51.schema_v51 import build
from storage.identity import get_system_status


def test_fresh_build_matches_committed_schema_fingerprint(tmp_path):
    path = tmp_path / "fresh.db"
    build(str(path), None)
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        )
        assert schema_fingerprint(conn) == CURRENT_SCHEMA_FINGERPRINT
    finally:
        conn.close()


def test_legacy_migration_backfills_awareness_then_removes_retired_queue(tmp_path):
    path = tmp_path / "pre-v1.db"
    build(str(path), None)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE awareness_posts")
        conn.execute("DROP TABLE awareness_thoughts")
        conn.execute(
            "CREATE TABLE awareness_thoughts ("
            "thought_id TEXT PRIMARY KEY, at TEXT NOT NULL, read_json TEXT, "
            "plan_json TEXT, chose_silence INTEGER NOT NULL DEFAULT 0, "
            "post_count INTEGER NOT NULL DEFAULT 0, skipped_reason TEXT, "
            "model TEXT, shadow INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO awareness_thoughts (thought_id, at) "
            "VALUES ('old-thought', '2026-07-13T12:00:00Z')"
        )
        conn.execute(
            """CREATE TABLE communication_intents (
                intent_id INTEGER PRIMARY KEY,
                intent_type TEXT NOT NULL,
                lane TEXT NOT NULL,
                scope TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                fulfilled_at TEXT,
                discord_message_id TEXT
            )"""
        )
        payload = json.dumps(
            {
                "content": "A migrated awareness post",
                "covers_signal_keys": ["signal:1"],
                "loop_number": 9,
            }
        )
        conn.execute(
            "INSERT INTO communication_intents "
            "(intent_type, lane, scope, payload_json, status, attempts, "
            "created_at, expires_at, fulfilled_at, discord_message_id) "
            "VALUES ('awareness:post', 'elixir', 'public', ?, 'fulfilled', 1, "
            "'2026-07-13T12:05:00Z', '2026-07-13T12:05:00Z', "
            "'2026-07-13T12:05:00Z', '123')",
            (payload,),
        )
        conn.execute("PRAGMA user_version = 0")
        conn.commit()

        apply_schema_migrations(conn)

        thought = conn.execute(
            "SELECT loop_number, tool_trace_json FROM awareness_thoughts"
        ).fetchone()
        post = conn.execute(
            "SELECT lane, content_preview, loop_number, discord_message_id "
            "FROM awareness_posts"
        ).fetchone()
        assert thought == (1, None)
        assert post == ("elixir", "A migrated awareness post", 9, "123")
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'communication_intents'"
            ).fetchone()
            is None
        )
        assert "shadow" not in {
            row[1] for row in conn.execute("PRAGMA table_info(awareness_thoughts)")
        }
        assert "legacy_intent_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(awareness_posts)")
        }
    finally:
        conn.close()


def test_empty_get_connection_builds_complete_current_schema(tmp_path):
    path = tmp_path / "empty.db"
    conn = db.get_connection(path)
    try:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='discord_users'"
        ).fetchone()
        assert schema_fingerprint(conn) == CURRENT_SCHEMA_FINGERPRINT
    finally:
        conn.close()


def test_v5_migration_adds_editor_verdicts_forward_from_v4(tmp_path):
    """A live v4 database (no editor_verdicts) migrates forward to v5, gaining
    the editorial-critic verdict ledger. The migration is idempotent and the
    result matches the committed fingerprint."""
    path = tmp_path / "v4.db"
    build(str(path), None)
    conn = sqlite3.connect(path)
    try:
        # Simulate a pre-V5 database: drop the table, roll user_version back.
        conn.execute("DROP TABLE editor_verdicts")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='editor_verdicts'"
        ).fetchone()

        apply_schema_migrations(conn)

        assert (
            conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='editor_verdicts'"
        ).fetchone()
        # Verdict CHECK constraint is enforced.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(editor_verdicts)")}
        assert {
            "verdict_id",
            "intent_key",
            "loop_number",
            "lane",
            "verdict",
            "at",
        } <= cols
        # Re-running is a no-op (idempotent) and stays on the committed fingerprint.
        apply_schema_migrations(conn)
        assert schema_fingerprint(conn) == CURRENT_SCHEMA_FINGERPRINT
    finally:
        conn.close()


def test_wrong_generation_database_is_refused_without_enabling_wal(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE old_runtime (id INTEGER PRIMARY KEY)")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()

    try:
        db.get_connection(path)
    except RuntimeError as exc:
        assert "does not carry the v5.1 spine" in str(exc)
    else:
        raise AssertionError("pre-v5.1 database was accepted")

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()


def test_system_status_reads_awareness_native_ledgers(tmp_path):
    path = tmp_path / "status.db"
    conn = db.get_connection(path)
    try:
        conn.execute(
            "INSERT INTO awareness_thoughts "
            "(thought_id, loop_number, at, read_json, plan_json, chose_silence, "
            "post_count, skipped_reason) VALUES "
            "('ok', 1, strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
            "?, '{}', 1, 0, 'quiet'), "
            "('failed', 2, strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
            "'{}', '{}', 0, 0, '⚠️ tick failed: delivery @ post: boom')",
            (
                json.dumps(
                    {"signals_by_lane": {"elixir": [{"key": "a"}, {"key": "b"}]}}
                ),
            ),
        )
        conn.execute(
            "INSERT INTO awareness_posts "
            "(lane, content_preview, covers_json, posted_at, discord_message_id) "
            "VALUES ('elixir', 'posted', '[]', "
            "strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'message-1')"
        )
        status = get_system_status(conn=conn)

        assert status["awareness_7d"] == {
            "ticks": 2,
            "signals_in": 2,
            "posts_delivered": 1,
            "delivery_failed": 1,
            "failed_ticks": 1,
        }
    finally:
        conn.close()


def test_runtime_schema_mutation_is_centralized():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for package in ("engine", "storage", "runtime", "memory_store", "db"):
        for path in (root / package).rglob("*.py"):
            if path == root / "db" / "schema.py" or path.name == "_migrations.py":
                continue
            text = path.read_text()
            if "ALTER TABLE" in text or "CREATE TABLE IF NOT EXISTS" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
