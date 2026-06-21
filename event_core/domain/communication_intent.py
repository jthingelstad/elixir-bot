"""CommunicationIntent aggregate — the Mind→surface boundary.

Records that Elixir *decided to communicate* something: the intent type, subject,
scope, priority, and evidence. It deliberately does NOT carry copy, channel,
message ids, or formatting — those belong to the side-effect surface outside the
Event Core (Core Boundary, §3). A downstream Discord consumer reads intents and
owns presentation.

Lifecycle: raised -> (fulfilled | dropped).
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from eventsourcing.domain import Aggregate, event

INTENT_NAMESPACE = uuid5(NAMESPACE_URL, "elixir.v5.communication_intent")


class InvalidTransition(Exception):
    pass


def intent_id(dedup_key: str) -> UUID:
    return uuid5(INTENT_NAMESPACE, dedup_key)


class CommunicationIntent(Aggregate):
    @event("Raised")
    def __init__(
        self,
        dedup_key: str,
        intent_type: str,
        subject_tag: str | None,
        scope: str,
        priority: int,
        caused_by: list[str],
        summary: dict,
    ) -> None:
        self.dedup_key = dedup_key
        self.intent_type = intent_type
        self.subject_tag = subject_tag
        self.scope = scope
        self.priority = priority
        self.caused_by = caused_by
        # `summary` = compact, presentation-free facts the surface may use; NOT copy.
        self.summary = summary
        self.status = "raised"
        self.drop_reason = None  # set on drop; always present for readers

    @classmethod
    def create_id(cls, dedup_key: str, **_kwargs) -> UUID:
        return intent_id(dedup_key)

    def fulfil(self) -> None:
        if self.status != "raised":
            raise InvalidTransition(f"cannot fulfil a {self.status} intent")
        self._fulfilled()

    @event("Fulfilled")
    def _fulfilled(self) -> None:
        self.status = "fulfilled"

    def drop(self, reason: str) -> None:
        if self.status != "raised":
            raise InvalidTransition(f"cannot drop a {self.status} intent")
        self._dropped(reason)

    @event("Dropped")
    def _dropped(self, reason: str) -> None:
        self.status = "dropped"
        self.drop_reason = reason
