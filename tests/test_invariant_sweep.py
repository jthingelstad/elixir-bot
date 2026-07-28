"""The invariant sweep must actually run.

`assert_db_invariants` is the safety net under every test: whatever a test did
to the default DB, the structural truths still have to hold. A net that stops
running is worse than no net, because the suite still goes green.

That is exactly what happened. #207 dropped `recognition_ledger`; the sweep's
second check queried it; the whole sweep shared one `except OperationalError`
at the call site. From that commit until this test was written, *every* test
ran with no invariant checking at all, and nothing said so.

So these tests do not check any individual invariant. They check that the sweep
is alive against the real schema, and that it still fails when it should.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import assert_db_invariants


def test_sweep_skips_nothing_on_the_full_schema(v51_schema_template):
    """No check may silently opt out against a full-schema database.

    A non-empty `skipped` here means a check names a table the schema no longer
    has — the sweep has rotted and that check is not running.
    """
    conn = sqlite3.connect(v51_schema_template)
    try:
        skipped = assert_db_invariants(conn, label="liveness")
    finally:
        conn.close()

    assert skipped == [], (
        "invariant checks skipped against the full v5.1 template: "
        f"{skipped}. Each names a table or column the schema does not have, "
        "so that check is dead. Update or remove it."
    )


def test_sweep_still_catches_a_violation(v51_schema_template, tmp_path):
    """The net has to catch something, or 'skips nothing' proves nothing."""
    db_path = str(tmp_path / "violating.db")
    with open(v51_schema_template, "rb") as src, open(db_path, "wb") as dst:
        dst.write(src.read())

    conn = sqlite3.connect(db_path)
    try:
        # A space-separated timestamp — the ' ' vs 'T' bug class the sweep exists
        # to catch, since a bare datetime('now') compares wrong against ISO-T.
        conn.execute(
            "INSERT INTO memories (kind, title, body, created_by, created_at, updated_at) "
            "VALUES ('system', 't', 'b', 'test', '2026-07-28 12:00:00', '2026-07-28T12:00:00')"
        )
        conn.commit()

        with pytest.raises(AssertionError, match="space-format timestamp"):
            assert_db_invariants(conn, label="negative control")
    finally:
        conn.close()


def test_a_missing_table_skips_only_its_own_check(tmp_path):
    """A minimal fixture is legitimate; it must not disable the other checks.

    This is the granularity that was missing. One absent table records a skip
    and the sweep carries on, rather than aborting everything below it.
    """
    db_path = str(tmp_path / "minimal.db")
    conn = sqlite3.connect(db_path)
    try:
        # Only one of the tables the sweep looks at, carrying a bad value.
        conn.execute(
            "CREATE TABLE war_seasons (season_id INTEGER PRIMARY KEY, "
            "started_at TEXT, ended_at TEXT)"
        )
        conn.execute("INSERT INTO war_seasons (started_at) VALUES ('20260728T000000')")
        conn.commit()

        # The violation is still found even though most tables are absent.
        with pytest.raises(AssertionError, match="noncanonical timestamp"):
            assert_db_invariants(conn, label="minimal fixture")
    finally:
        conn.close()
