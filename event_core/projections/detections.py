"""Detections projection — queryable read model of Mind detections.

Lets the agent read what's been inferred (by player, type, recency) without
touching the opaque event store.
"""
from __future__ import annotations

import json

from event_core.projections.runner import ProjectionRunner
from event_core.timeutil import cr_utc_timestamp


class DetectionsProjection(ProjectionRunner):
    name = "detections_proj"
    aggregate_name = "Detection"

    def setup(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS detections (
                dedup_key      TEXT PRIMARY KEY,
                detection_type TEXT,
                detector       TEXT,
                subject_tag    TEXT,
                occurred_at    TEXT,
                scope          TEXT,
                payload_json   TEXT
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_detections_subject ON detections(subject_tag, occurred_at DESC)"
        )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS detections")
        self.conn.commit()
        super().reset()
        self.setup()

    def handle(self, event, notification) -> None:
        if type(event).__name__ != "Detected":
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO detections(dedup_key,detection_type,detector,subject_tag,occurred_at,scope,payload_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                event.dedup_key, event.detection_type, event.detector,
                event.subject_tag, cr_utc_timestamp(event.occurred_at), event.scope,
                json.dumps(event.payload, default=str),
            ),
        )
