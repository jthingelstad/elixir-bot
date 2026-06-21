"""Detection aggregate — Elixir's Mind.

A derived observation ("hot streak", "level-up milestone", ...) emitted by a
Follower after reading base events. Written ONLY by Followers. Deterministic id
from a dedup key so emission is idempotent across replays (get-or-create).

Carries causal evidence (caused_by) back into the Observed World per the guardrail
"no derived/Mind event without causal evidence". UTC only — no local_date.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from eventsourcing.domain import Aggregate, event

DETECTION_NAMESPACE = uuid5(NAMESPACE_URL, "elixir.v5.detection")


def detection_id(dedup_key: str) -> UUID:
    return uuid5(DETECTION_NAMESPACE, dedup_key)


class Detection(Aggregate):
    @event("Detected")
    def __init__(
        self,
        dedup_key: str,
        detection_type: str,
        detector: str,
        subject_tag: str | None,
        occurred_at: str,
        caused_by: list[str],
        payload: dict,
        scope: str = "public",
    ) -> None:
        self.dedup_key = dedup_key
        self.detection_type = detection_type
        self.detector = detector
        self.subject_tag = subject_tag
        self.occurred_at = occurred_at  # UTC
        self.caused_by = caused_by
        self.payload = payload
        self.scope = scope

    @classmethod
    def create_id(cls, dedup_key: str, **_kwargs) -> UUID:
        return detection_id(dedup_key)
