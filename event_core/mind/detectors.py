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


class CardLevelMilestoneDetector(FollowerRunner):
    """A card reaches level >= 16 (legacy card_level_milestone).

    Legacy fires per milestone level crossed at/above CARD_UPGRADE_SIGNAL_MIN_LEVEL
    (=16); since the stored `level` is the legacy display level capped at 16, the
    only milestone is level 16. We fire when new_level crosses to >= 16, keyed by
    (player, card, milestone) so it is idempotent and at most once per card.
    """

    name = "detector:card_level_milestone"
    aggregate_name = "PlayerCollections"
    MIN_LEVEL = 16

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "CardLevelChanged":
            return
        old = event.old_level
        new = event.new_level
        if not isinstance(new, int):
            return
        old = old if isinstance(old, int) else -1
        for milestone in range(max(old + 1, self.MIN_LEVEL), new + 1):
            self.emit_detection(
                dedup_key=f"card_level_milestone:{event.player_tag}:{event.card_id}:{milestone}",
                detection_type="card_level_milestone",
                subject_tag=event.player_tag,
                occurred_at=event.observed_at,
                caused_by=[self.evidence(notification)],
                payload={
                    "card_id": event.card_id,
                    "card_name": event.card_name,
                    "rarity": event.rarity,
                    "milestone": milestone,
                    "from": event.old_level,
                    "to": new,
                },
            )


class NewCardUnlockedDetector(FollowerRunner):
    """A legendary or champion card is unlocked (legacy new_card_unlocked /
    new_champion_unlocked).

    Rarity IS carried on the granular CardUnlocked event (preserved from the raw
    card dict through ingest normalization), so we gate on rarity in
    {legendary, champion} exactly like legacy CARD_UNLOCK_SIGNAL_RARITIES. Emits
    detection_type new_card_unlocked for both, and additionally
    new_champion_unlocked when rarity == champion.
    """

    name = "detector:new_card_unlocked"
    aggregate_name = "PlayerCollections"
    UNLOCK_RARITIES = {"legendary", "champion"}

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "CardUnlocked":
            return
        rarity = (event.rarity or "").strip().lower() or None
        if rarity not in self.UNLOCK_RARITIES:
            return
        evidence = [self.evidence(notification)]
        self.emit_detection(
            dedup_key=f"new_card_unlocked:{event.player_tag}:{event.card_id}",
            detection_type="new_card_unlocked",
            subject_tag=event.player_tag,
            occurred_at=event.observed_at,
            caused_by=evidence,
            payload={
                "card_id": event.card_id,
                "card_name": event.card_name,
                "rarity": rarity,
            },
        )
        if rarity == "champion":
            self.emit_detection(
                dedup_key=f"new_champion_unlocked:{event.player_tag}:{event.card_id}",
                detection_type="new_champion_unlocked",
                subject_tag=event.player_tag,
                occurred_at=event.observed_at,
                caused_by=evidence,
                payload={
                    "card_id": event.card_id,
                    "card_name": event.card_name,
                    "rarity": rarity,
                },
            )


class BadgeEarnedDetector(FollowerRunner):
    """A badge is newly earned (legacy badge_earned). Fires on the granular
    BadgeEarned event, keyed by (player, badge) for idempotency.

    Divergence note: legacy excludes "mastery"-category badges from badge_earned;
    the granular BadgeEarned event does not carry category, so this detector fires
    for any newly-present badge. In the archive window mastery badges are not newly
    earned, so this does not over-fire vs legacy (confirmed by date overlap).
    """

    name = "detector:badge_earned"
    aggregate_name = "PlayerCollections"

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "BadgeEarned":
            return
        self.emit_detection(
            dedup_key=f"badge_earned:{event.player_tag}:{event.badge_name}",
            detection_type="badge_earned",
            subject_tag=event.player_tag,
            occurred_at=event.observed_at,
            caused_by=[self.evidence(notification)],
            payload={
                "badge_name": event.badge_name,
                "level": event.level,
                "progress": event.progress,
            },
        )


class BattleTrophyPushDetector(FollowerRunner):
    """Telemetry-input detector (like BattleHotStreakDetector): scans
    battle_telemetry rather than the log.

    Mirrors legacy _detect_battle_pulse_signals battle_trophy_push: a run of
    competitive trophy-change battles with >= 3 battles totaling >= 100 trophy
    delta. We scan each player's competitive battles chronologically, accumulating
    a run; a run ends (and is evaluated) at a non-positive trophy_change battle or
    end-of-stream. Keyed by the battle_time of the run's last battle for
    idempotency.
    """

    name = "detector:battle_trophy_push"
    MIN_BATTLES = 3
    MIN_DELTA = 100

    def detect(self, event, notification) -> None:  # unused (not log-driven)
        pass

    def _flush(self, tag, run) -> None:
        delta = sum(r["trophy_change"] for r in run)
        if len(run) >= self.MIN_BATTLES and delta >= self.MIN_DELTA:
            last = run[-1]
            self.emit_detection(
                dedup_key=f"battle_trophy_push:{tag}:{last['battle_time']}",
                detection_type="battle_trophy_push",
                subject_tag=tag,
                occurred_at=last["battle_time"],
                caused_by=[f"battle_telemetry:{tag}:{last['battle_time']}"],
                payload={
                    "battle_count": len(run),
                    "trophy_delta": delta,
                    "from_trophies": run[0]["starting_trophies"],
                    "to_trophies": last["starting_trophies"] + last["trophy_change"],
                },
            )

    def run(self, batch: int = 500) -> int:
        rows = self.conn.execute(
            "SELECT player_tag, battle_time, trophy_change, starting_trophies "
            "FROM battle_telemetry "
            "WHERE is_competitive=1 AND trophy_change IS NOT NULL "
            "AND starting_trophies IS NOT NULL "
            "ORDER BY player_tag, battle_time ASC"
        ).fetchall()
        current_tag = None
        run: list = []
        for r in rows:
            if r["player_tag"] != current_tag:
                if current_tag is not None:
                    self._flush(current_tag, run)
                current_tag, run = r["player_tag"], []
            if r["trophy_change"] > 0:
                run.append(r)
            else:
                self._flush(current_tag, run)
                run = []
        if current_tag is not None:
            self._flush(current_tag, run)
        return self.emitted


ALL_DETECTORS = [
    PlayerLevelUpDetector,
    BestTrophiesPeakDetector,
    BattleHotStreakDetector,
    CardLevelMilestoneDetector,
    NewCardUnlockedDetector,
    BadgeEarnedDetector,
    BattleTrophyPushDetector,
]
