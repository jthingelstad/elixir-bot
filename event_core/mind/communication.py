"""CommunicationPolicy — the reactive trigger (the v5 thesis).

Follows Detection and Recommendation events and decides what warrants action,
emitting CommunicationIntent events. This replaces schedule-first awareness: the
arrival of a noteworthy event is what triggers Elixir to communicate. The intent
is presentation-free; a downstream Discord consumer owns copy/channel.

Idempotent: intent ids are deterministic from the source event's evidence.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from eventsourcing.application import AggregateNotFoundError

from event_core.domain.communication_intent import CommunicationIntent, intent_id
from event_core.mind.follower import FollowerRunner

log = logging.getLogger("elixir.event_core")

# Public detection_type -> intent_type prefix. The prefix selects the channel in
# route_intent (celebrate->#player-highlights, clan/cohort->#clan-events,
# war->#river-race).
# battle_hot_streak intentionally NOT here: it's the same "doing well in battle"
# signal as battle_trophy_push (which posted alongside it — redundant double-post)
# and is the less interesting of the two. We celebrate trophy/rank MOVEMENT, which
# is the mode-appropriate metric. (Mode-aware movement incl. Path-of-Legend is the
# 2f/3 follow-up.) The detector still runs only if re-added to ALL_DETECTORS.
_CELEBRATE = (
    "best_trophies_peak",
    "battle_trophy_push",
    "career_wins_milestone",
    "card_level_milestone",
    "collection_level_milestone",
    # new_card_unlocked covers legendary AND champion unlocks (its payload carries
    # rarity, so the agent frames champions specially). new_champion_unlocked is a
    # strict subset (same card_id) — it stays a detection for parity/cohort logic
    # but is intentionally NOT a public intent type, else every champion unlock
    # double-posts (seen live: pigsareus' Archer Queen posted twice in one tick).
    "new_card_unlocked",
    "badge_earned",
    "player_level_up",
    # Path-of-Legend (ranked ladder) milestones — a first-class celebration lane.
    "path_of_legend_promotion",
    "ultimate_champion_reached",
    "path_of_legend_global_rank_attained",
    "ranked_activity_pulse",
)
# Clan-social detections that go to #clan-events (the "clan" prefix in route_intent).
_CLAN_SOCIAL = (
    "member_joined",
    "member_left",
    "member_promoted",
    "clan_birthday",
    "member_birthday",
    "join_anniversary",
    "weekly_donation_leader",
)
PUBLIC_INTENT_PREFIX = {
    **{t: "celebrate" for t in _CELEBRATE},
    **{t: "clan" for t in _CLAN_SOCIAL},
    "war_update": "war",
    "war_complete": "war",
    "new_season": "war",
    "cohort_wave": "cohort",
}

PLAYER_HIGHLIGHT_WINDOW = timedelta(days=14)
PLAYER_HIGHLIGHT_THRESHOLD = 80
PLAYER_HIGHLIGHT_POLICY = "player_highlight_score:v1"
PLAYER_HIGHLIGHT_ACCRUING_REASON = "player_highlight_accruing"
PLAYER_HIGHLIGHT_COALESCED_REASON = "player_highlight_coalesced:same_tick"

_PLAYER_HIGHLIGHT_BASE_SCORES = {
    "ultimate_champion_reached": 120,
    "path_of_legend_global_rank_attained": 110,
    "card_level_milestone": 95,
    "career_wins_milestone": 85,
    "collection_level_milestone": 80,
    "player_level_up": 80,
    "badge_earned": 55,
    "path_of_legend_promotion": 45,
    "best_trophies_peak": 40,
    "ranked_activity_pulse": 30,
}

_PLAYER_HIGHLIGHT_BYPASS_TYPES = {
    "ultimate_champion_reached",
    "path_of_legend_global_rank_attained",
    "card_level_milestone",
    "career_wins_milestone",
    "collection_level_milestone",
    "player_level_up",
}

_CELEBRATE_PRIORITY = {
    "ultimate_champion_reached": 100,
    "path_of_legend_global_rank_attained": 95,
    "card_level_milestone": 80,
    "new_card_unlocked": 75,
    "badge_earned": 70,
    "collection_level_milestone": 65,
    "career_wins_milestone": 60,
    "player_level_up": 55,
    "path_of_legend_promotion": 50,
    "best_trophies_peak": 40,
    "ranked_activity_pulse": 15,
    "battle_trophy_push": 10,
}

_UTC_MIN = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class _CelebrateCandidate:
    event: object
    notification_id: int
    evidence: str

    @property
    def dedup_key(self) -> str:
        return self.event.dedup_key

    @property
    def detection_type(self) -> str:
        return self.event.detection_type

    @property
    def subject_tag(self) -> str | None:
        return self.event.subject_tag

    @property
    def occurred_at(self) -> datetime | None:
        return _parse_utc(getattr(self.event, "occurred_at", None))


@dataclass(frozen=True)
class _RecognitionEvidence:
    dedup_key: str
    detection_type: str
    score: int
    bypass: bool
    occurred_at: datetime | None
    occurred_at_text: str | None
    notification_id: int


def _parse_utc(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _summary_for_detection(event, **extra) -> dict:
    return {
        "detection_type": event.detection_type,
        **event.payload,
        "occurred_at": event.occurred_at,
        **{k: v for k, v in extra.items() if v is not None},
    }


def _int_payload(payload: dict, key: str) -> int:
    value = payload.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _score_detection(event) -> tuple[int, bool]:
    payload = getattr(event, "payload", None) or {}
    detection_type = getattr(event, "detection_type", "")
    if detection_type == "new_card_unlocked":
        rarity = str(payload.get("rarity") or "").strip().lower()
        if rarity == "champion":
            return 90, True
        if rarity == "legendary":
            return 65, False
        return 0, False
    if detection_type == "battle_trophy_push":
        delta = _int_payload(payload, "trophy_delta")
        bonus = max(0, delta - 100) // 50 * 5
        return min(45, 25 + bonus), False
    score = _PLAYER_HIGHLIGHT_BASE_SCORES.get(detection_type, 0)
    return score, detection_type in _PLAYER_HIGHLIGHT_BYPASS_TYPES


class CommunicationPolicy(FollowerRunner):
    name = "policy:communication"
    aggregate_name = None  # consumes Detection + Recommendation; filters internally

    def __init__(self, app, conn):
        super().__init__(app, conn)
        # Subjects that already got a #player-highlights candidate in the current
        # run — kept for compatibility with old tests/debuggers; same-tick
        # selection now happens by scoring all candidates before raising intents.
        self._celebrated_this_run: set = set()

    def run(self, batch: int = 500) -> int:
        # Reset per run (one tick) so coalescing is per-tick, not per-process.
        self._celebrated_this_run = set()
        stop = self.app.recorder.max_notification_id()
        pos = self.last_position()
        celebrate_candidates: list[_CelebrateCandidate] = []
        while pos < stop:
            notifs = self.app.recorder.select_notifications(
                start=pos + 1, limit=batch, stop=stop
            )
            if not notifs:
                break
            for n in notifs:
                if self.aggregate_name is None or self._aggregate_of(n) == self.aggregate_name:
                    try:
                        event = self.app.mapper.to_domain_event(n)
                        if self._is_celebrate_detection(event):
                            celebrate_candidates.append(
                                _CelebrateCandidate(event, n.id, self.evidence(n))
                            )
                        else:
                            self.detect(event, n)
                    except Exception:
                        log.exception(
                            "%s: skipped notification %s (%s)", self.name, n.id, n.topic
                        )
                pos = n.id
        if celebrate_candidates:
            self._process_celebrate_candidates(celebrate_candidates)
        if pos:
            self._save_position(pos)
        return self.emitted

    @staticmethod
    def _is_celebrate_detection(event) -> bool:
        return (
            type(event).__name__ == "Detected"
            and PUBLIC_INTENT_PREFIX.get(event.detection_type) == "celebrate"
        )

    def _raise_intent(
        self,
        *,
        dedup_key,
        intent_type,
        subject,
        scope,
        priority,
        caused_by,
        summary,
        drop_reason: str | None = None,
    ):
        try:
            self.app.repository.get(intent_id(dedup_key))
            return False
        except AggregateNotFoundError:
            intent = CommunicationIntent(
                dedup_key=dedup_key, intent_type=intent_type, subject_tag=subject,
                scope=scope, priority=priority, caused_by=caused_by, summary=summary,
            )
            if drop_reason:
                intent.drop(drop_reason)
            self.app.save(intent)
            if not drop_reason:
                self.emitted += 1
                return True
            return False

    def _active_player_highlights(self) -> dict[str, list[CommunicationIntent]]:
        highlights: dict[str, list[CommunicationIntent]] = {}
        pos = 0
        while True:
            notifs = self.app.recorder.select_notifications(start=pos + 1, limit=1000)
            if not notifs:
                break
            for n in notifs:
                if (
                    self._aggregate_of(n) == "CommunicationIntent"
                    and n.topic.endswith(".Raised")
                ):
                    try:
                        event = self.app.mapper.to_domain_event(n)
                        intent = self.app.repository.get(event.originator_id)
                    except Exception:
                        log.exception(
                            "%s: could not inspect intent notification %s for recognition policy",
                            self.name,
                            n.id,
                        )
                        continue
                    if (
                        intent.status in {"raised", "fulfilled"}
                        and intent.scope == "public"
                        and (intent.intent_type or "").startswith("celebrate:")
                        and intent.subject_tag
                    ):
                        highlights.setdefault(intent.subject_tag, []).append(intent)
                pos = n.id
        return highlights

    @staticmethod
    def _intent_reference_time(intent) -> tuple[datetime | None, bool]:
        summary_time = None
        if isinstance(getattr(intent, "summary", None), dict):
            summary_time = _parse_utc(intent.summary.get("occurred_at"))
        if summary_time is not None:
            return summary_time, True
        return _parse_utc(getattr(intent, "created_on", None)), False

    @staticmethod
    def _candidate_sort_key(candidate: _CelebrateCandidate) -> tuple:
        payload = getattr(candidate.event, "payload", None) or {}
        payload_score = (
            payload.get("milestone")
            or payload.get("level")
            or payload.get("peak")
            or payload.get("trophy_delta")
            or 0
        )
        return (
            _CELEBRATE_PRIORITY.get(candidate.detection_type, 0),
            payload_score if isinstance(payload_score, int | float) else 0,
            candidate.occurred_at or _UTC_MIN,
            candidate.notification_id,
        )

    def _latest_public_highlight(
        self,
        candidate: _CelebrateCandidate,
        prior_intents: list[CommunicationIntent],
    ) -> CommunicationIntent | None:
        candidate_time = candidate.occurred_at or datetime.now(timezone.utc)
        matches: list[tuple[datetime, CommunicationIntent]] = []
        for intent in prior_intents:
            reference_time, from_signal = self._intent_reference_time(intent)
            if reference_time is None:
                continue
            if from_signal and reference_time > candidate_time:
                continue
            matches.append((reference_time, intent))
        if not matches:
            return None
        return max(matches, key=lambda item: item[0])[1]

    @staticmethod
    def _recognition_evidence_for_event(event, notification_id: int) -> _RecognitionEvidence | None:
        score, bypass = _score_detection(event)
        if score <= 0:
            return None
        return _RecognitionEvidence(
            dedup_key=event.dedup_key,
            detection_type=event.detection_type,
            score=score,
            bypass=bypass,
            occurred_at=_parse_utc(getattr(event, "occurred_at", None)),
            occurred_at_text=getattr(event, "occurred_at", None),
            notification_id=notification_id,
        )

    @staticmethod
    def _evidence_is_in_window(
        evidence: _RecognitionEvidence,
        *,
        anchor_time: datetime,
        cutoff_time: datetime,
        latest_highlight_time: datetime | None,
    ) -> bool:
        if evidence.occurred_at is None:
            return False
        if evidence.occurred_at < cutoff_time or evidence.occurred_at > anchor_time:
            return False
        return latest_highlight_time is None or evidence.occurred_at > latest_highlight_time

    def _recognition_evidence(
        self,
        *,
        subject_tag: str | None,
        group: list[_CelebrateCandidate],
        selected: _CelebrateCandidate,
        latest_highlight: CommunicationIntent | None,
    ) -> list[_RecognitionEvidence]:
        anchor_time = selected.occurred_at or datetime.now(timezone.utc)
        cutoff_time = anchor_time - PLAYER_HIGHLIGHT_WINDOW
        latest_highlight_time = None
        if latest_highlight is not None:
            latest_highlight_time, _from_signal = self._intent_reference_time(latest_highlight)

        evidence_by_key: dict[str, _RecognitionEvidence] = {}

        def add(evidence: _RecognitionEvidence | None) -> None:
            if evidence is None:
                return
            if not self._evidence_is_in_window(
                evidence,
                anchor_time=anchor_time,
                cutoff_time=cutoff_time,
                latest_highlight_time=latest_highlight_time,
            ):
                return
            evidence_by_key[evidence.dedup_key] = evidence

        if subject_tag:
            pos = 0
            while True:
                notifs = self.app.recorder.select_notifications(start=pos + 1, limit=1000)
                if not notifs:
                    break
                for n in notifs:
                    if self._aggregate_of(n) != "Detection":
                        pos = n.id
                        continue
                    try:
                        event = self.app.mapper.to_domain_event(n)
                    except Exception:
                        log.exception(
                            "%s: could not inspect detection notification %s for recognition policy",
                            self.name,
                            n.id,
                        )
                        pos = n.id
                        continue
                    if (
                        type(event).__name__ == "Detected"
                        and event.subject_tag == subject_tag
                        and PUBLIC_INTENT_PREFIX.get(event.detection_type) == "celebrate"
                    ):
                        add(self._recognition_evidence_for_event(event, n.id))
                    pos = n.id

        for candidate in group:
            add(self._recognition_evidence_for_event(candidate.event, candidate.notification_id))

        return sorted(
            evidence_by_key.values(),
            key=lambda e: (e.occurred_at or _UTC_MIN, e.notification_id, e.dedup_key),
        )

    @staticmethod
    def _recognition_summary(
        *,
        decision: str,
        evidence: list[_RecognitionEvidence],
    ) -> dict:
        return {
            "recognition_policy": PLAYER_HIGHLIGHT_POLICY,
            "recognition_decision": decision,
            "recognition_score": sum(item.score for item in evidence),
            "recognition_threshold": PLAYER_HIGHLIGHT_THRESHOLD,
            "recognition_evidence": [
                {
                    "dedup_key": item.dedup_key,
                    "detection_type": item.detection_type,
                    "score": item.score,
                    "occurred_at": item.occurred_at_text,
                }
                for item in evidence
            ],
        }

    def _drop_candidate(
        self,
        candidate: _CelebrateCandidate,
        *,
        reason: str,
        suppressed_by_intent: CommunicationIntent | None = None,
        selected_candidate: _CelebrateCandidate | None = None,
        summary_extra: dict | None = None,
    ) -> None:
        self._raise_intent(
            dedup_key=f"intent:detection:{candidate.dedup_key}",
            intent_type=f"celebrate:{candidate.detection_type}",
            subject=candidate.subject_tag,
            scope="public",
            priority=1,
            caused_by=[candidate.evidence, *candidate.event.caused_by],
            summary=_summary_for_detection(
                candidate.event,
                policy_decision="suppressed",
                suppression_reason=reason,
                suppressed_by_intent_key=getattr(suppressed_by_intent, "dedup_key", None),
                selected_detection_key=getattr(selected_candidate, "dedup_key", None),
                selected_detection_type=getattr(selected_candidate, "detection_type", None),
                **(summary_extra or {}),
            ),
            drop_reason=reason,
        )

    def _process_celebrate_candidates(self, candidates: list[_CelebrateCandidate]) -> None:
        active_highlights = self._active_player_highlights()
        by_subject: dict[str, list[_CelebrateCandidate]] = {}
        for candidate in candidates:
            subject_key = candidate.subject_tag or f"candidate:{candidate.dedup_key}"
            by_subject.setdefault(subject_key, []).append(candidate)

        for subject_key in sorted(by_subject):
            group = by_subject[subject_key]
            selected = max(group, key=self._candidate_sort_key)
            if selected.subject_tag:
                self._celebrated_this_run.add(selected.subject_tag)
            latest_highlight = self._latest_public_highlight(
                selected,
                active_highlights.get(selected.subject_tag or "", []),
            )
            evidence = self._recognition_evidence(
                subject_tag=selected.subject_tag,
                group=group,
                selected=selected,
                latest_highlight=latest_highlight,
            )
            score = sum(item.score for item in evidence)
            selected_score, selected_bypass = _score_detection(selected.event)
            if not selected_bypass and score < PLAYER_HIGHLIGHT_THRESHOLD:
                summary_extra = self._recognition_summary(
                    decision="accruing",
                    evidence=evidence,
                )
                summary_extra["recognition_evidence_count"] = len(evidence)
                for candidate in group:
                    self._drop_candidate(
                        candidate,
                        reason=PLAYER_HIGHLIGHT_ACCRUING_REASON,
                        suppressed_by_intent=latest_highlight,
                        summary_extra=summary_extra,
                    )
                continue

            decision = "bypass" if selected_bypass else "accrued"
            self._raise_intent(
                dedup_key=f"intent:detection:{selected.dedup_key}",
                intent_type=f"celebrate:{selected.detection_type}",
                subject=selected.subject_tag,
                scope="public",
                priority=1,
                caused_by=[selected.evidence, *selected.event.caused_by],
                summary=_summary_for_detection(
                    selected.event,
                    **self._recognition_summary(decision=decision, evidence=evidence),
                    recognition_selected_score=selected_score,
                ),
            )
            for candidate in group:
                if candidate is selected:
                    continue
                self._drop_candidate(
                    candidate,
                    reason=PLAYER_HIGHLIGHT_COALESCED_REASON,
                    selected_candidate=selected,
                )

    def detect(self, event, notification) -> None:
        cls = type(event).__name__
        ev = self.evidence(notification)
        if cls == "Detected":
            prefix = PUBLIC_INTENT_PREFIX.get(event.detection_type)
            if prefix is None:
                return  # e.g. inactive_member_risk drives recommendations, not posts
            self._raise_intent(
                dedup_key=f"intent:detection:{event.dedup_key}",
                intent_type=f"{prefix}:{event.detection_type}",
                subject=event.subject_tag,
                scope="public",
                priority=1,
                caused_by=[ev, *event.caused_by],
                summary=_summary_for_detection(event),
            )
        elif cls == "CandidateDetected":  # Recommendation
            self._raise_intent(
                dedup_key=f"intent:recommendation:{event.dedup_key}",
                intent_type=f"leadership:{event.recommendation_type}",
                subject=event.player_tag,
                scope="leadership",
                priority=2,
                caused_by=[ev, *event.caused_by],
                summary={
                    "recommendation_type": event.recommendation_type,
                    "reason_codes": event.reason_codes,
                    "policy_version": event.policy_version,
                },
            )
