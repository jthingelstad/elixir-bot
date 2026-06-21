"""DecisionCase aggregate — Elixir's Mind (leadership decision state machine).

The lifecycle state of a leadership concern, written only by Followers. The case
table becomes a projection of these events; the event store answers "what
happened, what policy, what evidence". Deferred cases resurface because case state
says they are due.

Lifecycle: opened -> (refreshed | deferred)* -> (accepted | rejected | resolved).
Terminal states reject further transitions.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from eventsourcing.domain import Aggregate, event

CASE_NAMESPACE = uuid5(NAMESPACE_URL, "elixir.v5.decision_case")

TERMINAL = {"accepted", "rejected", "resolved"}


class InvalidTransition(Exception):
    pass


def case_id(dedup_key: str) -> UUID:
    return uuid5(CASE_NAMESPACE, dedup_key)


class DecisionCase(Aggregate):
    @event("Opened")
    def __init__(
        self,
        dedup_key: str,
        case_type: str,
        player_tag: str,
        priority: int,
        due_at: str | None,
        caused_by: list[str],
    ) -> None:
        self.dedup_key = dedup_key
        self.case_type = case_type
        self.player_tag = player_tag
        self.priority = priority
        self.due_at = due_at
        self.caused_by = caused_by
        self.scope = "leadership"
        self.status = "open"
        self.resolution = None

    @classmethod
    def create_id(cls, dedup_key: str, **_kwargs) -> UUID:
        return case_id(dedup_key)

    def _guard_open(self, action: str) -> None:
        if self.status in TERMINAL:
            raise InvalidTransition(f"cannot {action} a {self.status} case")

    def refresh(self, priority: int, caused_by: list[str]) -> None:
        self._guard_open("refresh")
        self._refreshed(priority, caused_by)

    @event("Refreshed")
    def _refreshed(self, priority: int, caused_by: list[str]) -> None:
        self.priority = priority
        self.caused_by = caused_by
        self.status = "open"

    def defer(self, due_at: str) -> None:
        self._guard_open("defer")
        self._deferred(due_at)

    @event("Deferred")
    def _deferred(self, due_at: str) -> None:
        self.status = "deferred"
        self.due_at = due_at

    def accept(self) -> None:
        self._guard_open("accept")
        self._accepted()

    @event("Accepted")
    def _accepted(self) -> None:
        self.status = "accepted"

    def reject(self) -> None:
        self._guard_open("reject")
        self._rejected()

    @event("Rejected")
    def _rejected(self) -> None:
        self.status = "rejected"

    def resolve(self, resolution: str) -> None:
        self._guard_open("resolve")
        self._resolved(resolution)

    @event("Resolved")
    def _resolved(self, resolution: str) -> None:
        self.status = "resolved"
        self.resolution = resolution
