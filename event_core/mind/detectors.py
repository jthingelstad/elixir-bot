"""Detectors — Followers that turn base events into Detections.

Each consumes one base aggregate's events and emits Detection events. These prove
the Mind mechanism; breadth (card/badge/battle/roster detectors) follows the same
shape. Validation is vs legacy signal_log *dates* (it has no per-event evidence).
"""
from __future__ import annotations

from event_core.mind.follower import FollowerRunner


def _milestones(old: int, new: int, step: int) -> list[int]:
    """Multiples of `step` in the open-closed interval (old, new]."""
    if old is None or new is None or new <= old:
        return []
    first = (old // step + 1) * step
    return list(range(first, new + 1, step))


class PlayerLevelUpDetector(FollowerRunner):
    name = "detector:player_level_up"
    aggregate_name = "Player"

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "PlayerLevelChanged":
            return
        for level in _milestones(event.old_level, event.new_level, 5):
            self.emit_detection(
                dedup_key=f"player_level_up:{event.player_tag}:{level}",
                detection_type="player_level_up",
                subject_tag=event.player_tag,
                occurred_at=event.observed_at,
                caused_by=[self.evidence(notification)],
                payload={"level": level, "from": event.old_level, "to": event.new_level},
            )


class BestTrophiesPeakDetector(FollowerRunner):
    name = "detector:best_trophies_peak"
    aggregate_name = "Player"

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "BestTrophiesChanged":
            return
        for boundary in _milestones(event.old_best, event.new_best, 100):
            self.emit_detection(
                dedup_key=f"best_trophies_peak:{event.player_tag}:{boundary}",
                detection_type="best_trophies_peak",
                subject_tag=event.player_tag,
                occurred_at=event.observed_at,
                caused_by=[self.evidence(notification)],
                payload={"peak": boundary, "from": event.old_best, "to": event.new_best},
            )


class BattleHotStreakDetector(FollowerRunner):
    """Telemetry-input detector (§5.3): scans battle_telemetry rather than the log.

    Fires once per streak when a player reaches 4 consecutive competitive wins
    ("≥4 W, not already hot", mirroring legacy _detect_battle_pulse_signals),
    keyed by the battle_time of the 4th win for idempotency.
    """

    name = "detector:battle_hot_streak"
    STREAK = 4

    def detect(self, event, notification) -> None:  # unused (not log-driven)
        pass

    def run(self, batch: int = 500) -> int:
        rows = self.conn.execute(
            "SELECT player_tag, battle_time, outcome FROM battle_telemetry "
            "WHERE is_competitive=1 ORDER BY player_tag, battle_time ASC"
        ).fetchall()
        streak = 0
        current_tag = None
        for r in rows:
            if r["player_tag"] != current_tag:
                current_tag, streak = r["player_tag"], 0
            if r["outcome"] == "W":
                streak += 1
                if streak == self.STREAK:  # just became hot
                    self.emit_detection(
                        dedup_key=f"battle_hot_streak:{r['player_tag']}:{r['battle_time']}",
                        detection_type="battle_hot_streak",
                        subject_tag=r["player_tag"],
                        occurred_at=r["battle_time"],
                        caused_by=[f"battle_telemetry:{r['player_tag']}:{r['battle_time']}"],
                        payload={"streak": self.STREAK},
                    )
            else:
                streak = 0
        return self.emitted


ALL_DETECTORS = [
    PlayerLevelUpDetector,
    BestTrophiesPeakDetector,
    BattleHotStreakDetector,
]
