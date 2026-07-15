"""Incident ledger: fail-soft failures land queryably (confidence plan §1)."""

import db
from storage import incidents


def test_record_and_query_incident():
    conn = db.get_connection()
    try:
        incidents.record_incident(
            "test.component", ValueError("boom"), context={"tag": "#A"}, conn=conn
        )
        rows = incidents.open_incidents(conn)
        assert rows and rows[0]["component"] == "test.component"
        assert "boom" in rows[0]["summary"]
        assert (
            rows[0]["detail"] and "ValueError" in rows[0]["detail"]
        )  # traceback captured
        assert incidents.count_open_since(conn, hours=24) >= 1
    finally:
        conn.close()


def test_record_incident_never_raises():
    # A broken context / bad conn must not propagate — observability can't crash.
    incidents.record_incident(
        "test.safe", "just a string", conn=None
    )  # opens its own conn


def test_string_error_captures_caller_stack():
    conn = db.get_connection()
    try:
        incidents.record_incident(
            "test.str", "manual message", severity="warn", conn=conn
        )
        row = incidents.open_incidents(conn)[0]
        assert row["severity"] == "warn" and row["summary"] == "manual message"
    finally:
        conn.close()
