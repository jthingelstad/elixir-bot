"""Follower framework — process applications that consume the notification log.

A Follower reads base events forward from its tracked position and emits Mind
events (Detections) into the SAME event store via the shared app. Key properties:

- Tracking position is co-located in elixir-v5.db (reuses projection_tracking),
  committed after each batch — replay-safe.
- Emission is idempotent: Detection ids are deterministic from a dedup key, so a
  re-run (or a full event-store rebuild) produces the same detections (get-or-create).
- A run snapshots max_notification_id at start and never processes past it, so a
  Follower never consumes the detections it just emitted (no self-amplification).
- aggregate_name filters the log to the base aggregate the detector consumes
  (event class names collide across aggregates).
"""
from __future__ import annotations

from datetime import datetime, timezone

from eventsourcing.application import AggregateNotFoundError

from event_core.domain.detection import Detection, detection_id


class FollowerRunner:
    name: str = ""
    aggregate_name: str | None = None  # base aggregate consumed (e.g. "Player")

    def __init__(self, app, conn):
        self.app = app
        self.conn = conn
        self.emitted = 0

    @staticmethod
    def _aggregate_of(notification) -> str:
        return notification.topic.split(":")[-1].split(".")[0]

    @staticmethod
    def evidence(notification) -> str:
        return f"{notification.originator_id}:{notification.originator_version}"

    # --- emission ---
    def emit_detection(
        self,
        *,
        dedup_key: str,
        detection_type: str,
        subject_tag: str | None,
        occurred_at: str,
        caused_by: list[str],
        payload: dict,
        scope: str = "public",
    ) -> bool:
        """Idempotently emit a Detection. Returns True if newly created."""
        try:
            self.app.repository.get(detection_id(dedup_key))
            return False
        except AggregateNotFoundError:
            d = Detection(
                dedup_key=dedup_key,
                detection_type=detection_type,
                detector=self.name,
                subject_tag=subject_tag,
                occurred_at=occurred_at,
                caused_by=caused_by,
                payload=payload,
                scope=scope,
            )
            self.app.save(d)
            self.emitted += 1
            return True

    # --- detection logic (override) ---
    def detect(self, event, notification) -> None:
        raise NotImplementedError

    # --- engine ---
    def last_position(self) -> int:
        row = self.conn.execute(
            "SELECT last_global_position FROM projection_tracking WHERE projection_name=?",
            (self.name,),
        ).fetchone()
        return row["last_global_position"] if row else 0

    def _save_position(self, pos: int) -> None:
        self.conn.execute(
            "INSERT INTO projection_tracking(projection_name,last_global_position,updated_at) "
            "VALUES(?,?,?) ON CONFLICT(projection_name) DO UPDATE SET "
            "last_global_position=excluded.last_global_position, updated_at=excluded.updated_at",
            (self.name, pos, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.execute(
            "DELETE FROM projection_tracking WHERE projection_name=?", (self.name,)
        )
        self.conn.commit()

    def fast_forward(self) -> int:
        """Skip to the current log head WITHOUT processing — drains a backlog.

        Used once at cutover so the Discord consumer posts only intents raised
        after go-live, not the entire historical backlog (caught in the Stage-5
        rehearsal). Returns the new position.
        """
        head = self.app.recorder.max_notification_id() or 0
        self._save_position(head)
        return head

    def run(self, batch: int = 500) -> int:
        stop = self.app.recorder.max_notification_id()  # snapshot: ignore our own emissions
        pos = self.last_position()
        while pos < stop:
            notifs = self.app.recorder.select_notifications(
                start=pos + 1, limit=batch, stop=stop
            )
            if not notifs:
                break
            for n in notifs:
                if self.aggregate_name is None or self._aggregate_of(n) == self.aggregate_name:
                    event = self.app.mapper.to_domain_event(n)
                    self.detect(event, n)
                pos = n.id
            self._save_position(pos)
        return self.emitted
