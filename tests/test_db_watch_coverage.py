"""Every write path must be visible to the instrument.

This file exists because the first version of the write-lock instrument reported
zero transactions for a day and a half and that read as "the database is
healthy". It was not: `conn.executemany`, `cur.execute` and `with conn:` all
went unrecorded, and an unrecorded write is one the stall watchdog cannot see.

A partial instrument is worse than none — it answers the question with silence.
So the coverage claim is asserted here, path by path, rather than reasoned about.
"""

from __future__ import annotations

import sqlite3

import pytest

from storage import db_watch, telemetry


@pytest.fixture
def recorded(monkeypatch):
    """Capture what would have been written to the telemetry file."""
    rows: list[dict] = []

    def _capture(call_site, held_ms, *, statements=0, outcome):
        rows.append(
            {
                "call_site": call_site,
                "held_ms": held_ms,
                "statements": statements,
                "outcome": outcome,
            }
        )

    # Report every transaction, not just slow ones: these are microseconds.
    monkeypatch.setattr(db_watch, "REPORT_MS", 0)
    monkeypatch.setattr(telemetry, "record_transaction", _capture)
    db_watch._open_writes.clear()
    db_watch._reported.clear()
    yield rows
    db_watch._open_writes.clear()
    db_watch._reported.clear()


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "probe.db", factory=db_watch.InstrumentedConnection)
    c.execute("CREATE TABLE t (v INTEGER)")
    c.commit()
    yield c
    c.close()


def test_connection_execute_then_commit(conn, recorded):
    conn.execute("INSERT INTO t VALUES (1)")
    assert db_watch.open_write_transactions(), "write should register as an open transaction"
    conn.commit()
    assert [r["outcome"] for r in recorded] == ["commit"]


def test_context_manager_commit_is_recorded(conn, recorded):
    # `with conn:` commits inside CPython's C __exit__, which does not call a
    # Python commit() override. This is the path that used to leave a record
    # open and attribute its hold time to whatever committed next.
    with conn:
        conn.execute("INSERT INTO t VALUES (2)")
    assert [r["outcome"] for r in recorded] == ["commit"]
    assert not db_watch.open_write_transactions()


def test_context_manager_rollback_is_recorded(conn, recorded):
    with pytest.raises(ValueError):
        with conn:
            conn.execute("INSERT INTO t VALUES (3)")
            raise ValueError("boom")
    assert [r["outcome"] for r in recorded] == ["rollback"]
    assert not db_watch.open_write_transactions()


def test_connection_executemany(conn, recorded):
    conn.executemany("INSERT INTO t VALUES (?)", [(4,), (5,)])
    assert db_watch.open_write_transactions()
    conn.commit()
    assert [r["outcome"] for r in recorded] == ["commit"]


def test_cursor_execute(conn, recorded):
    cur = conn.cursor()
    cur.execute("INSERT INTO t VALUES (6)")
    assert db_watch.open_write_transactions()
    conn.commit()
    assert [r["outcome"] for r in recorded] == ["commit"]


def test_cursor_executemany(conn, recorded):
    cur = conn.cursor()
    cur.executemany("INSERT INTO t VALUES (?)", [(7,), (8,)])
    assert db_watch.open_write_transactions()
    conn.commit()
    assert [r["outcome"] for r in recorded] == ["commit"]


def test_statements_are_counted_once(conn, recorded):
    """conn.execute() routes through our cursor; it must not observe twice."""
    conn.execute("INSERT INTO t VALUES (9)")
    conn.execute("INSERT INTO t VALUES (10)")
    conn.commit()
    assert recorded[0]["statements"] == 2


def test_reads_do_not_open_a_transaction(conn, recorded):
    conn.execute("SELECT * FROM t").fetchall()
    assert not db_watch.open_write_transactions()
    assert recorded == []


def test_autocommit_ddl_does_not_leak_an_open_record(conn, recorded):
    """DDL runs outside a transaction, so it must not leave a record open.

    A record that never closes reaches the 45s watchdog threshold and reports a
    stall that is not happening — the false-alarm direction of this bug.
    """
    conn.execute("CREATE TABLE ddl_probe (v INTEGER)")
    assert not db_watch.open_write_transactions()


def test_executescript_closes_its_record(conn, recorded):
    conn.executescript("INSERT INTO t VALUES (11); INSERT INTO t VALUES (12);")
    assert not db_watch.open_write_transactions()


def test_call_site_attributes_to_the_caller(conn, recorded):
    """The hold time is useless without knowing whose it is."""
    conn.execute("INSERT INTO t VALUES (13)")
    conn.commit()
    assert recorded[0]["call_site"].startswith("tests/test_db_watch_coverage.py:")
