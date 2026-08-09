"""Telemetry SQLite files are admin-only regardless of process umask."""

from __future__ import annotations

import os
import sqlite3
import stat

from storage import telemetry


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _close_cached_connection() -> None:
    conn = telemetry._local.__dict__.pop("conn", None)
    if conn is not None:
        conn.close()
    telemetry._schema_ready = False


def test_fresh_database_and_sidecars_are_owner_only_under_permissive_umask(tmp_path, monkeypatch):
    path = tmp_path / "telemetry.db"
    monkeypatch.setenv("ELIXIR_TELEMETRY_DB_PATH", str(path))

    previous_umask = os.umask(0)
    try:
        conn = telemetry.connect()
        conn.execute(
            "INSERT INTO db_transactions "
            "(recorded_at, call_site, held_ms, statements, outcome) "
            "VALUES ('2026-08-09T00:00:00Z', 'test', 1, 1, 'committed')"
        )
        conn.commit()
    finally:
        os.umask(previous_umask)

    family = (path, tmp_path / "telemetry.db-wal", tmp_path / "telemetry.db-shm")
    assert all(candidate.exists() for candidate in family)
    assert {_mode(candidate) for candidate in family} == {0o600}
    _close_cached_connection()


def test_startup_narrows_an_existing_database_and_sidecars(tmp_path, monkeypatch):
    path = tmp_path / "telemetry.db"
    seed = sqlite3.connect(path)
    seed.execute("PRAGMA journal_mode = WAL")
    seed.execute("CREATE TABLE seed (value INTEGER)")
    seed.execute("INSERT INTO seed VALUES (1)")
    seed.commit()

    family = (path, tmp_path / "telemetry.db-wal", tmp_path / "telemetry.db-shm")
    assert all(candidate.exists() for candidate in family)
    for candidate in family:
        candidate.chmod(0o644)

    monkeypatch.setenv("ELIXIR_TELEMETRY_DB_PATH", str(path))
    telemetry.connect()

    assert {_mode(candidate) for candidate in family} == {0o600}
    _close_cached_connection()
    seed.close()
