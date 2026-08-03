"""entity_key is stored canonical so its index can be used.

The receipt lookup used to compare `UPPER(LTRIM(entity_key,'#')) = ?`. An index
exists for exactly that lookup, but wrapping the column in functions makes it
unusable past its first column: SQLite searched on `endpoint=?` alone, scanned,
and sorted in a temp B-tree — 0.651 ms/lookup against 18,901 rows and worsening
as the table grew. The normalization was already true of every stored tag; it was
being re-derived on every read for an invariant the data already held.

The plan assertion is the point. Someone re-adding a function around the column
would still pass a behavioural test — and quietly restore the table scan.
"""

from __future__ import annotations

import db
from engine.normalize import bare_tag


def _plan(conn, sql, params):
    return " ".join(row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_receipt_lookup_uses_the_covering_index(engine_conn):
    sql = (
        "SELECT receipt_id FROM api_observation_receipts "
        "WHERE endpoint = ? AND entity_key = ? AND payload_hash = ? "
        "ORDER BY receipt_id DESC LIMIT 1"
    )
    plan = _plan(engine_conn, sql, ("player", "ABC123", "h"))
    assert "idx_api_receipts_lookup" in plan, plan
    # The whole cost was the scan plus the sort; the index supplies both.
    assert "TEMP B-TREE" not in plan.upper(), plan


def test_the_old_predicate_would_not_use_the_index(engine_conn):
    """Guards the reason for the change, not just its result."""
    sql = (
        "SELECT receipt_id FROM api_observation_receipts "
        "WHERE endpoint = ? AND UPPER(LTRIM(entity_key, '#')) = ? AND payload_hash = ? "
        "ORDER BY receipt_id DESC LIMIT 1"
    )
    plan = _plan(engine_conn, sql, ("player", "ABC123", "h")).upper()
    assert "TEMP B-TREE" in plan, f"expected the wrapped column to force a sort: {plan}"


def test_write_normalizes_entity_key(engine_conn):
    for raw in ("#abc123", "abc123", " #AbC123 ", "ABC123"):
        result = db._store_raw_payload(engine_conn, "player", raw, {"tag": raw})
        assert result is not None
        stored = engine_conn.execute(
            "SELECT entity_key FROM api_observation_receipts WHERE receipt_id = ?",
            (result["receipt_id"],),
        ).fetchone()["entity_key"]
        assert stored == "ABC123", f"{raw!r} stored as {stored!r}"


def test_payload_and_receipt_share_the_normalized_key(engine_conn):
    """Both tables are written by one function; they must agree on the key."""
    result = db._store_raw_payload(engine_conn, "player", "#dEf456", {"tag": "x"})
    receipt = engine_conn.execute(
        "SELECT entity_key, payload_id FROM api_observation_receipts WHERE receipt_id = ?",
        (result["receipt_id"],),
    ).fetchone()
    payload = engine_conn.execute(
        "SELECT entity_key FROM raw_api_payloads WHERE payload_id = ?",
        (receipt["payload_id"],),
    ).fetchone()
    assert receipt["entity_key"] == payload["entity_key"] == "DEF456"


def test_bare_tag_matches_the_sql_canonical_form(engine_conn):
    """The Python normalizer and the migration's SQL must not drift apart."""
    for raw in ("#abc", "abc", " #AbC ", "##ABC", "global"):
        sql_form = engine_conn.execute("SELECT UPPER(LTRIM(TRIM(?), '#'))", (raw,)).fetchone()[0]
        assert bare_tag(raw) == sql_form, f"{raw!r}: python={bare_tag(raw)!r} sql={sql_form!r}"
