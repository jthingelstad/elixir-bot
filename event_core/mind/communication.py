"""CommunicationPolicy — the reactive trigger (the v5 thesis).

Follows Detection and Recommendation events and decides what warrants action,
emitting CommunicationIntent events. This replaces schedule-first awareness: the
arrival of a noteworthy event is what triggers Elixir to communicate. The intent
is presentation-free; a downstream Discord consumer owns copy/channel.

Idempotent: intent ids are deterministic from the source event's evidence.
"""
from __future__ import annotations

from eventsourcing.application import AggregateNotFoundError

from event_core.domain.communication_intent import CommunicationIntent, intent_id
from event_core.mind.follower import FollowerRunner

# Public detection_type -> intent_type prefix. The prefix selects the channel in
# route_intent (celebrate->#player-highlights, welcome->#welcome, war->#river-race,
# cohort->#clan-events).
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
)
# Clan-social detections that go to #clan-events (the "clan" prefix in route_intent).
_CLAN_SOCIAL = (
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
    "member_joined": "welcome",
    "war_update": "war",
    "war_complete": "war",
    "new_season": "war",
    "cohort_wave": "cohort",
}


class CommunicationPolicy(FollowerRunner):
    name = "policy:communication"
    aggregate_name = None  # consumes Detection + Recommendation; filters internally

    def __init__(self, app, conn):
        super().__init__(app, conn)
        # Subjects that already got a #player-highlights (celebrate) intent in the
        # current run — used to coalesce to ONE post per player per tick.
        self._celebrated_this_run: set = set()

    def run(self, batch: int = 500) -> int:
        # Reset per run (one tick) so coalescing is per-tick, not per-process.
        self._celebrated_this_run = set()
        return super().run(batch)

    def _raise_intent(self, *, dedup_key, intent_type, subject, scope, priority, caused_by, summary):
        try:
            self.app.repository.get(intent_id(dedup_key))
            return False
        except AggregateNotFoundError:
            self.app.save(CommunicationIntent(
                dedup_key=dedup_key, intent_type=intent_type, subject_tag=subject,
                scope=scope, priority=priority, caused_by=caused_by, summary=summary,
            ))
            self.emitted += 1
            return True

    def detect(self, event, notification) -> None:
        cls = type(event).__name__
        ev = self.evidence(notification)
        if cls == "Detected":
            prefix = PUBLIC_INTENT_PREFIX.get(event.detection_type)
            if prefix is None:
                return  # e.g. inactive_member_risk drives recommendations, not posts
            if prefix == "celebrate":
                # One #player-highlights post per player per tick. A grinder who
                # trips several celebrate detectors at once (card max + trophy push
                # + …) otherwise gets multiple posts that all narrate the same week
                # (the recent_history enrichment makes them overlap). Keep the first;
                # its post folds in the rest via that enrichment.
                if event.subject_tag in self._celebrated_this_run:
                    return
                self._celebrated_this_run.add(event.subject_tag)
            self._raise_intent(
                dedup_key=f"intent:detection:{event.dedup_key}",
                intent_type=f"{prefix}:{event.detection_type}",
                subject=event.subject_tag,
                scope="public",
                priority=1,
                caused_by=[ev, *event.caused_by],
                summary={"detection_type": event.detection_type, **event.payload},
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
