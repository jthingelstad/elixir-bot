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


def test_receipt_retention_commits_raw_payload_deletes_in_bounded_batches(engine_conn, monkeypatch):
    """Weekly raw-payload expiry must yield SQLite's writer between batches."""
    monkeypatch.setattr(metadata, "_PURGE_BATCH_SIZE", 2)
    old = _iso(datetime.now(timezone.utc) - timedelta(days=1))
    for index in range(5):
        engine_conn.execute(
            """INSERT INTO raw_api_payloads
                   (endpoint, entity_key, fetched_at, last_fetched_at,
                    payload_hash, payload_json)
               VALUES ('player', ?, ?, ?, ?, '{}')""",
            (f"BATCH-{index}", old, old, f"batch-hash-{index}"),
        )

    class TrackingConnection:
        def __init__(self, connection):
            self.connection = connection
            self.commits = 0

        def execute(self, *args, **kwargs):
            return self.connection.execute(*args, **kwargs)

        def commit(self):
            self.commits += 1
            self.connection.commit()

    tracked = TrackingConnection(engine_conn)
    deleted = metadata._delete_expired_rows(
        tracked,
        table="raw_api_payloads",
        predicate="entity_key GLOB 'BATCH-*' AND COALESCE(last_fetched_at, fetched_at)",
        cutoff=_iso(datetime.now(timezone.utc)),
    )

    assert deleted == 5
    assert tracked.commits == 3
    assert (
        engine_conn.execute(
            "SELECT count(*) FROM raw_api_payloads WHERE entity_key GLOB 'BATCH-*'"
        ).fetchone()[0]
        == 0
    )
