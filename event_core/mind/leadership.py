"""Leadership generators — Followers that produce Recommendations and DecisionCases.

Proves the Mind decision pipeline end to end:
  roster observation -> inactive_member_risk Detection -> kick Recommendation
  + inactivity_review DecisionCase (each with evidence + policy version).

Parity note: legacy recommendations/cases came from a recompute-first policy scan
that §6 explicitly REPLACES. So validation here is structural (lifecycle invariants,
evidence links) plus a sanity comparison of flagged players vs legacy
inactivity_review targets — not row-for-row reproduction of legacy's policy output.
"""
from __future__ import annotations

from datetime import datetime, timezone

from eventsourcing.application import AggregateNotFoundError

from event_core.domain.decision_case import DecisionCase, case_id
from event_core.domain.recommendation import Recommendation, recommendation_id
from event_core.mind.follower import FollowerRunner

POLICY_VERSION = "v5.inactivity.1"


def _parse_ts(value: str | None):
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    # CR compact form: 20260615T193251.000+00:00
    for fmt in ("%Y%m%dT%H%M%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def days_inactive(last_seen: str | None, observed_at: str | None) -> float | None:
    a, b = _parse_ts(last_seen), _parse_ts(observed_at)
    if a is None or b is None:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return (b - a).total_seconds() / 86400.0


class InactivityRiskDetector(FollowerRunner):
    name = "detector:inactive_member_risk"
    aggregate_name = "Player"
    THRESHOLD_DAYS = 7

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "RosterStateObserved":
            return
        last_seen = event.observation.get("last_seen_api")
        d = days_inactive(last_seen, event.observed_at)
        if d is None or d < self.THRESHOLD_DAYS:
            return
        # One detection per inactivity episode (keyed by the stale lastSeen value).
        self.emit_detection(
            dedup_key=f"inactive_member_risk:{event.player_tag}:{last_seen}",
            detection_type="inactive_member_risk",
            subject_tag=event.player_tag,
            occurred_at=event.observed_at,
            caused_by=[self.evidence(notification)],
            payload={"days_inactive": round(d, 1), "last_seen": last_seen},
            scope="leadership",
        )


class LeadershipGenerator(FollowerRunner):
    """Follows Detection events and opens leadership recommendations + cases."""

    name = "generator:leadership"
    aggregate_name = "Detection"

    def _ensure_recommendation(self, dedup_key, rec_type, tag, reasons, severity, caused_by):
        try:
            self.app.repository.get(recommendation_id(dedup_key))
            return False
        except AggregateNotFoundError:
            self.app.save(Recommendation(
                dedup_key=dedup_key, recommendation_type=rec_type, player_tag=tag,
                reason_codes=reasons, policy_version=POLICY_VERSION, severity=severity,
                caused_by=caused_by,
            ))
            return True

    def _ensure_case(self, dedup_key, case_type, tag, priority, caused_by):
        try:
            self.app.repository.get(case_id(dedup_key))
            return False
        except AggregateNotFoundError:
            self.app.save(DecisionCase(
                dedup_key=dedup_key, case_type=case_type, player_tag=tag,
                priority=priority, due_at=None, caused_by=caused_by,
            ))
            return True

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "Detected":
            return
        if event.detection_type != "inactive_member_risk":
            return
        tag = event.subject_tag
        evidence = [self.evidence(notification)]
        rec = self._ensure_recommendation(
            f"kick:{tag}", "kick", tag, ["inactivity"], "medium", evidence
        )
        case = self._ensure_case(
            f"inactivity_review:{tag}", "inactivity_review", tag, 1, evidence
        )
        if rec or case:
            self.emitted += 1


ALL_LEADERSHIP = [InactivityRiskDetector, LeadershipGenerator]
