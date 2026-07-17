"""Receipt/content split stays bounded without dropping recently reused content."""

from datetime import datetime, timedelta, timezone

import db
from storage import metadata


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def test_receipt_retention_keeps_content_with_a_recent_receipt(engine_conn):
    now = datetime.now(timezone.utc)
    old = _iso(now - timedelta(days=db.RAW_PAYLOAD_RETENTION_DAYS + 2))
    recent = _iso(now - timedelta(days=1))
    old_payload = engine_conn.execute(
        """INSERT INTO raw_api_payloads
               (endpoint, entity_key, fetched_at, last_fetched_at,
                payload_hash, payload_json)
           VALUES ('player', 'OLD', ?, ?, 'old-hash', '{}')""",
        (old, old),
    ).lastrowid
    shared_payload = engine_conn.execute(
        """INSERT INTO raw_api_payloads
               (endpoint, entity_key, fetched_at, last_fetched_at,
                payload_hash, payload_json)
           VALUES ('player', 'SHARED', ?, ?, 'shared-hash', '{}')""",
        (old, recent),
    ).lastrowid
    engine_conn.executemany(
        """INSERT INTO api_observation_receipts
               (payload_id, endpoint, entity_key, fetched_at, payload_hash)
           VALUES (?, 'player', ?, ?, ?)""",
        (
            (old_payload, "OLD", old, "old-hash"),
            (shared_payload, "SHARED", old, "shared-hash"),
            (shared_payload, "SHARED", recent, "shared-hash"),
        ),
    )

    stats = metadata.purge_old_data(conn=engine_conn)

    assert stats["api_observation_receipts"] == 2
    assert stats["raw_api_payloads"] == 1
    remaining = engine_conn.execute("SELECT entity_key FROM raw_api_payloads").fetchall()
    assert [row["entity_key"] for row in remaining] == ["SHARED"]
    receipt = engine_conn.execute("SELECT entity_key FROM api_observation_receipts").fetchone()
    assert receipt["entity_key"] == "SHARED"
