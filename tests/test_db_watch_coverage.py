"""Every write path must be visible to the instrument.

This file exists because the first version of the write-lock instrument reported
zero transactions for a day and a half and that read as "the database is
healthy". It was not: `conn.executemany`, `cur.execute` and `with conn:` all
went unrecorded, and an unrecorded write is one the stall watchdog cannot see.

A partial instrument is worse than none — it answers the question with silence.
So the coverage claim is asserted here, path by path, rather than reasoned about.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from storage import db_watch, telemetry


@pytest.fixture
def recorded(monkeypatch):
    """Capture what would have been written to the telemetry file."""
    rows: list[dict] = []

    def _capture(call_site, held_ms, *, statements=0, outcome, sites_json=None, txn_id=None):
        row = {
            "call_site": call_site,
            "held_ms": held_ms,
            "statements": statements,
            "outcome": outcome,
            "sites": json.loads(sites_json) if sites_json else [],
            "txn_id": txn_id,
        }
        if txn_id is not None:  # finalize in place, mirroring the real writer
            rows[txn_id - 1] = row
            return txn_id
        rows.append(row)
        return len(rows)

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


def _opener(conn):
    conn.execute("INSERT INTO t VALUES (100)")


def _bulk_writer(conn):
    for n in range(50):
        conn.execute("INSERT INTO t VALUES (?)", (n,))


def test_sites_breakdown_separates_the_opener_from_the_spender(conn, recorded):
    """`call_site` names who OPENED the transaction — not who spent it.

    This is the whole reason the breakdown exists. A tick's transaction is opened
    by one cheap statement and then filled by thousands from elsewhere; reading
    `call_site` alone points the finger at the wrong line.
    """
    _opener(conn)  # 1 statement, opens the transaction
    _bulk_writer(conn)  # 50 statements, the actual work
    conn.commit()

    row = recorded[0]
    assert row["statements"] == 51
    assert "_opener" not in row["call_site"]  # sanity: it is a file:line label

    sites = {entry["site"].split(":")[0] + ":" + str(entry["n"]): entry for entry in row["sites"]}
    counts = {entry["n"] for entry in row["sites"]}
    assert counts == {1, 50}, f"expected a 1-statement opener and a 50-statement spender: {sites}"

    # The transaction is attributed to the opener, but the breakdown names both.
    opener_entries = [e for e in row["sites"] if e["n"] == 1]
    assert row["call_site"] == opener_entries[0]["site"]
    assert len(row["sites"]) == 2


def test_sites_are_ranked_heaviest_first(conn, recorded):
    _opener(conn)
    _bulk_writer(conn)
    conn.commit()
    times = [entry["ms"] for entry in recorded[0]["sites"]]
    assert times == sorted(times, reverse=True)


def test_breakdown_is_capped(conn, recorded, monkeypatch):
    monkeypatch.setattr(db_watch, "TOP_SITES", 2)
    conn.execute("INSERT INTO t VALUES (1)")
    conn.execute("INSERT INTO t VALUES (2)")
    conn.execute("INSERT INTO t VALUES (3)")
    conn.commit()
    assert len(recorded[0]["sites"]) == 2


def test_a_stalled_transaction_is_recorded_before_it_can_die(conn, recorded, monkeypatch):
    """The worst transactions never close — so waiting for close loses them.

    On 2026-08-03 a 46.1s holder hung its job and died with the process. The
    stall table caught it; db_transactions' largest row was 369ms. The watchdog
    now writes the row provisionally at detection.
    """
    monkeypatch.setattr(db_watch, "STALL_SECONDS", 0.0)
    monkeypatch.setattr(db_watch, "_dump_threads", lambda: "<dump>")
    stalls = []
    monkeypatch.setattr(telemetry, "record_stall", lambda *a, **k: stalls.append(a))

    conn.execute("INSERT INTO t VALUES (1)")
    db_watch._watch_once()

    assert stalls, "stall should be detected"
    assert [r["outcome"] for r in recorded] == ["stalled"]
    assert recorded[0]["call_site"].startswith("tests/test_db_watch_coverage.py:")


def test_a_stalled_transaction_that_later_commits_is_one_row(conn, recorded, monkeypatch):
    """Finalize the provisional row rather than adding a second one."""
    monkeypatch.setattr(db_watch, "STALL_SECONDS", 0.0)
    monkeypatch.setattr(db_watch, "_dump_threads", lambda: "<dump>")
    monkeypatch.setattr(telemetry, "record_stall", lambda *a, **k: None)

    ids = []
    real = telemetry.record_transaction

    def _tracking(call_site, held_ms, **kw):
        ids.append(kw.get("txn_id"))
        return real(call_site, held_ms, **kw)

    monkeypatch.setattr(telemetry, "record_transaction", _tracking)

    conn.execute("INSERT INTO t VALUES (2)")
    db_watch._watch_once()
    conn.commit()

    # Two calls: the provisional insert (txn_id=None) then the finalize (an id).
    assert len(ids) == 2, ids
    assert ids[0] is None
    assert ids[1] is not None, "close must finalize the provisional row, not insert a new one"
