"""Recommendation aggregate — Elixir's Mind (leadership-scoped).

A promotion/demotion/kick/watch recommendation with a real lifecycle and
invariants, written only by leadership-generator Followers. Carries evidence,
reason codes, and a policy version per the guardrail "no recommendation event
without scope, policy version, and evidence". scope='leadership'.

Lifecycle: detected -> refreshed* -> (suppressed | expired) ; outcome_observed.
Terminal states (suppressed/expired) reject further refresh.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from eventsourcing.domain import Aggregate, event

RECOMMENDATION_NAMESPACE = uuid5(NAMESPACE_URL, "elixir.v5.recommendation")

TYPES = {"promotion", "demotion", "kick", "watch", "no_action"}
TERMINAL = {"suppressed", "expired"}


class InvalidTransition(Exception):
    pass


def recommendation_id(dedup_key: str) -> UUID:
    return uuid5(RECOMMENDATION_NAMESPACE, dedup_key)


class Recommendation(Aggregate):
    @event("CandidateDetected")
    def __init__(
        self,
        dedup_key: str,
        recommendation_type: str,
        player_tag: str,
        reason_codes: list[str],
        policy_version: str,
        severity: str,
        caused_by: list[str],
    ) -> None:
        self.dedup_key = dedup_key
        self.recommendation_type = recommendation_type
        self.player_tag = player_tag
        self.reason_codes = reason_codes
        self.policy_version = policy_version
        self.severity = severity
        self.caused_by = caused_by
        self.scope = "leadership"
        self.status = "detected"
        self.outcome = None
        self.suppression_reason = None  # set on suppress; always present for readers

    @classmethod
    def create_id(cls, dedup_key: str, **_kwargs) -> UUID:
        return recommendation_id(dedup_key)

    def refresh(self, reason_codes: list[str], caused_by: list[str]) -> None:
        if self.status in TERMINAL:
            raise InvalidTransition(f"cannot refresh a {self.status} recommendation")
        self._refreshed(reason_codes, caused_by)

    @event("Refreshed")
    def _refreshed(self, reason_codes: list[str], caused_by: list[str]) -> None:
        self.reason_codes = reason_codes
        self.caused_by = caused_by
        self.status = "refreshed"

    def suppress(self, reason: str) -> None:
        if self.status in TERMINAL:
            raise InvalidTransition(f"cannot suppress a {self.status} recommendation")
        self._suppressed(reason)

    @event("Suppressed")
    def _suppressed(self, reason: str) -> None:
        self.status = "suppressed"
        self.suppression_reason = reason

    def expire(self) -> None:
        if self.status in TERMINAL:
            raise InvalidTransition(f"cannot expire a {self.status} recommendation")
        self._expired()

    @event("Expired")
    def _expired(self) -> None:
        self.status = "expired"

    def observe_outcome(self, outcome: str) -> None:
        if self.outcome is not None:
            raise InvalidTransition("outcome already recorded")
        self._outcome_observed(outcome)

    @event("OutcomeObserved")
    def _outcome_observed(self, outcome: str) -> None:
        self.outcome = outcome
