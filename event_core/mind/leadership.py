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
    """SCAN-style, not log-driven. Inactivity is the *absence* of activity, so it
    cannot be reliably detected by following roster-change events: an idle member
    stops changing and therefore stops generating RosterStateObserved events (they
    only surface incidentally when their clan rank shifts), so an event-driven check
    flags them late or never — especially when roster observation itself is sparse
    (e.g. a flaky /clans endpoint). Instead, each run scans the CURRENT clan roster's
    last_seen against *now* and flags anyone past the threshold. Idempotent via the
    (tag, last_seen) dedup key — one detection per inactivity episode; a returning
    member gets a fresh last_seen, so a later lapse is a new episode."""

    name = "detector:inactive_member_risk"
    THRESHOLD_DAYS = 7

    def detect(self, event, notification) -> None:  # unused (scan-style, not log-driven)
        pass

    def run(self, batch: int = 500) -> int:
        now = datetime.now(timezone.utc).isoformat()
        # Current roster = members present in the most recent roster observation batch
        # (all current members share that observed_at; anyone who left has an older one).
        rows = self.conn.execute(
            "SELECT m.player_tag AS tag, cs.last_seen_api AS last_seen "
            "FROM members m JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE cs.observed_at = (SELECT MAX(observed_at) FROM member_current_state)"
        ).fetchall()
        for r in rows:
            last_seen = r["last_seen"]
            d = days_inactive(last_seen, now)
            if d is None or d < self.THRESHOLD_DAYS:
                continue
            self.emit_detection(
                dedup_key=f"inactive_member_risk:{r['tag']}:{last_seen}",
                detection_type="inactive_member_risk",
                subject_tag=r["tag"],
                occurred_at=now,
                caused_by=[f"member_current_state:{r['tag']}:{last_seen}"],
                payload={"days_inactive": round(d, 1), "last_seen": last_seen},
                scope="leadership",
            )
        return self.emitted


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
