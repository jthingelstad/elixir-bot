"""Offline-only adapter for the retired deterministic proactive pipeline.

Production imports neither recognition orchestration nor intent delivery. This
adapter exists solely for migration rehearsals that explicitly compare the old
scorer/renderer behavior against archived data.
"""

from __future__ import annotations

from dataclasses import asdict


def prepare_queue(conn) -> None:
    """Create the retired delivery queue for this connection only.

    A TEMP table keeps the rehearsal contract executable without putting the
    old queue back into the operational schema. Closing the connection drops
    it, and production never calls this module.
    """
    conn.execute(
        """CREATE TEMP TABLE IF NOT EXISTS communication_intents (
            intent_id INTEGER PRIMARY KEY,
            recognition_key TEXT,
            intent_type TEXT NOT NULL,
            lane TEXT NOT NULL,
            scope TEXT NOT NULL CHECK (scope IN ('public','leadership')),
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','fulfilled','failed','expired')),
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            fulfilled_at TEXT,
            discord_message_id TEXT,
            last_error TEXT,
            thread_id INTEGER
        )"""
    )


def run(conn, clock, now_iso: str, *, send_fn, compose_fn) -> tuple[dict, dict]:
    from engine import delivery, recognition

    prepare_queue(conn)
    clock_dict = asdict(clock) if clock is not None else None
    recognized = recognition.run_recognizers(conn, clock_dict, now_iso)
    delivered = delivery.consume(conn, send_fn, compose_fn, now_iso)
    return recognized, delivered


__all__ = ["prepare_queue", "run"]
