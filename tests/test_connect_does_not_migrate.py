"""Connecting must never migrate an existing database.

Migrate-on-connect took production down twice: any process with newer code that
opened the live DB (often because `source .env` overrode ELIXIR_DB_PATH) silently
applied a new _apply_vN, and the running older build then failed every tick with
"schema newer than this build". A migration is a deploy — it must be explicit.
"""

import sqlite3

import pytest

import db
from db.schema import CURRENT_SCHEMA_VERSION


def _current_db(tmp_path):
    path = tmp_path / "t.db"
    db.get_connection(str(path)).close()  # empty -> initialized (still allowed)
    return str(path)


def _set_version(path, version):
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


def test_connect_refuses_a_behind_database_and_leaves_it_untouched(tmp_path):
    path = _current_db(tmp_path)
    _set_version(path, CURRENT_SCHEMA_VERSION - 1)

    with pytest.raises(db.SchemaNotCurrentError):
        db.get_connection(path)

    after = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
    assert after == CURRENT_SCHEMA_VERSION - 1, "connecting must not have migrated it"


def test_connect_refuses_a_database_newer_than_the_build(tmp_path):
    path = _current_db(tmp_path)
    _set_version(path, CURRENT_SCHEMA_VERSION + 1)
    with pytest.raises(db.SchemaNotCurrentError):
        db.get_connection(path)


def test_explicit_migrate_to_current_is_the_supported_path(tmp_path):
    path = _current_db(tmp_path)
    _set_version(path, CURRENT_SCHEMA_VERSION - 1)

    assert db.migrate_to_current(path) == CURRENT_SCHEMA_VERSION
    db.get_connection(path).close()  # now connectable


def test_empty_database_is_still_initialized_on_connect(tmp_path):
    """Creating a fresh schema destroys nothing, and the fixtures depend on it."""
    path = str(tmp_path / "fresh.db")
    conn = db.get_connection(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_error_names_the_path_and_the_explicit_remedy(tmp_path):
    """The message must be actionable — the last outage was diagnosed from it."""
    path = _current_db(tmp_path)
    _set_version(path, CURRENT_SCHEMA_VERSION - 1)
    with pytest.raises(db.SchemaNotCurrentError) as exc:
        db.get_connection(path)
    message = str(exc.value)
    assert path in message
    assert "migrate_to_current" in message
    assert "ELIXIR_DB_PATH" in message
