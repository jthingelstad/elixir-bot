"""Detectors — Followers that turn base events into Detections.

Each consumes one base aggregate's events and emits Detection events. These prove
the Mind mechanism; breadth (card/badge/battle/roster detectors) follows the same
shape. Validation is vs legacy signal_log *dates* (it has no per-event evidence).
"""
from __future__ import annotations

import sqlite3

from event_core.mind.follower import FollowerRunner


def _milestones(old: int, new: int, step: int) -> list[int]:
    """Multiples of `step` in the open-closed interval (old, new].

    Suppressed when `old` is missing or <= 0: that signals no real baseline (a
    first/zeroed observation), and backfilling every milestone from 0 would emit a
    burst of posts for a newly-observed member.
    """
    if old is None or old <= 0 or new is None or new <= old:
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


class PathOfLegendDetector(FollowerRunner):
    """Path-of-Legend milestones -> #player-highlights. Follows PathOfLegendChanged.
    Emits league promotions, Ultimate Champion (crossing into league 10), and global
    rank attained/improved (lower rank = better). Mirrors the v4 PoL signals. A lot
    of members play PoL, so this is a first-class celebration lane."""

    name = "detector:path_of_legend"
    aggregate_name = "Player"
    ULTIMATE_CHAMPION_LEAGUE = 10

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "PathOfLegendChanged":
            return
        ol, nl = event.old_league, event.new_league
        orank, nrank = event.old_rank, event.new_rank
        ev = [self.evidence(notification)]

        if isinstance(ol, int) and isinstance(nl, int) and nl > ol:
            self.emit_detection(
                dedup_key=f"path_of_legend_promotion:{event.player_tag}:{nl}",
                detection_type="path_of_legend_promotion",
                subject_tag=event.player_tag,
                occurred_at=event.observed_at,
                caused_by=ev,
                payload={"from_league": ol, "to_league": nl,
                         "trophies": event.new_trophies, "rank": nrank},
            )
            if nl == self.ULTIMATE_CHAMPION_LEAGUE and ol < self.ULTIMATE_CHAMPION_LEAGUE:
                self.emit_detection(
                    dedup_key=f"ultimate_champion_reached:{event.player_tag}",
                    detection_type="ultimate_champion_reached",
                    subject_tag=event.player_tag,
                    occurred_at=event.observed_at,
                    caused_by=ev,
                    payload={"trophies": event.new_trophies, "rank": nrank},
                )

        if isinstance(nrank, int) and (orank is None or (isinstance(orank, int) and nrank < orank)):
            self.emit_detection(
                dedup_key=f"path_of_legend_global_rank:{event.player_tag}:{nrank}",
                detection_type="path_of_legend_global_rank_attained",
                subject_tag=event.player_tag,
                occurred_at=event.observed_at,
                caused_by=ev,
                payload={"from_rank": orank, "to_rank": nrank,
                         "league": nl, "trophies": event.new_trophies},
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
        # Total deterministic order (matches the telemetry PK) so streaks are
        # reproducible even when a player has multiple battles at the same second.
        rows = self.conn.execute(
            "SELECT player_tag, battle_time, battle_type, opponent_tag, outcome "
            "FROM battle_telemetry WHERE is_competitive=1 "
            "ORDER BY player_tag, battle_time, battle_type, opponent_tag, crowns_for, crowns_against"
        ).fetchall()
        streak = 0
        current_tag = None
        for r in rows:
            if r["player_tag"] != current_tag:
                current_tag, streak = r["player_tag"], 0
            if r["outcome"] == "W":
                streak += 1
                if streak == self.STREAK:  # just became hot
                    bid = f"{r['battle_time']}:{r['battle_type']}:{r['opponent_tag']}"
                    self.emit_detection(
                        dedup_key=f"battle_hot_streak:{r['player_tag']}:{bid}",
                        detection_type="battle_hot_streak",
                        subject_tag=r["player_tag"],
                        occurred_at=r["battle_time"],
                        caused_by=[f"battle_telemetry:{r['player_tag']}:{bid}"],
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
            bid = f"{last['battle_time']}:{last['battle_type']}:{last['opponent_tag']}"
            self.emit_detection(
                dedup_key=f"battle_trophy_push:{tag}:{bid}",
                detection_type="battle_trophy_push",
                subject_tag=tag,
                occurred_at=last["battle_time"],
                caused_by=[f"battle_telemetry:{tag}:{bid}"],
                payload={
                    "battle_count": len(run),
                    "trophy_delta": delta,
                    "from_trophies": run[0]["starting_trophies"],
                    "to_trophies": last["starting_trophies"] + last["trophy_change"],
                },
            )

    def run(self, batch: int = 500) -> int:
        rows = self.conn.execute(
            "SELECT player_tag, battle_time, battle_type, opponent_tag, trophy_change, starting_trophies "
            "FROM battle_telemetry "
            "WHERE is_competitive=1 AND trophy_change IS NOT NULL "
            "AND starting_trophies IS NOT NULL "
            "ORDER BY player_tag, battle_time, battle_type, opponent_tag, crowns_for, crowns_against"
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


class MemberJoinedDetector(FollowerRunner):
    """New clan member -> welcome (restores #welcome). Follows Clan MemberJoined."""

    name = "detector:member_joined"
    aggregate_name = "Clan"

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "MemberJoined":
            return
        self.emit_detection(
            dedup_key=f"member_joined:{event.player_tag}:{event.observed_at}",
            detection_type="member_joined",
            subject_tag=event.player_tag,
            occurred_at=event.observed_at,
            caused_by=[self.evidence(notification)],
            payload={"role": event.role},
        )


# A leader-action kick within this window before a departure means the member was
# kicked, not a voluntary leave — suppress the departure post.
_KICK_SUPPRESS_DAYS = 14


class MemberLeftDetector(FollowerRunner):
    """Clan departure -> #clan-events, enriched with the member's name/last stats.

    Suppresses the post when the departure was a leader-action KICK (a
    kick_recommendation accepted by a leader within the recent window) — we don't
    announce kicks as departures. Reads the operational tables (members /
    member_current_state / leader_action_recommendations), which live in the same
    consolidated DB as the projections."""

    name = "detector:member_left"
    aggregate_name = "Clan"

    def _enrich(self, tag: str) -> dict:
        row = self.conn.execute(
            "SELECT m.current_name AS name, cs.role AS role, cs.trophies AS trophies, "
            "cs.best_trophies AS best_trophies, cs.clan_rank AS clan_rank, "
            "cs.last_seen_api AS last_seen_api "
            "FROM members m LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.player_tag = ?",
            (tag,),
        ).fetchone()
        return dict(row) if row else {}

    def _was_kicked(self, tag: str, observed_at: str) -> bool:
        from datetime import datetime, timedelta, timezone
        try:
            anchor = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        except ValueError:
            anchor = datetime.now(timezone.utc)
        cutoff = (anchor - timedelta(days=_KICK_SUPPRESS_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        row = self.conn.execute(
            "SELECT 1 FROM leader_action_recommendations "
            "WHERE action_type = 'kick_recommendation' AND target_player_tag = ? "
            "AND status = 'done' AND COALESCE(is_test, 0) = 0 "
            "AND COALESCE(decided_at, proposed_at) >= ? LIMIT 1",
            (tag, cutoff),
        ).fetchone()
        return row is not None

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "MemberLeft":
            return
        tag = event.player_tag
        try:
            if self._was_kicked(tag, event.observed_at):
                return  # kicked, not a voluntary departure — don't announce
        except sqlite3.Error:
            pass  # leader-action table missing/empty -> treat as a normal leave
        info = {}
        try:
            info = self._enrich(tag)
        except sqlite3.Error:
            pass
        self.emit_detection(
            dedup_key=f"member_left:{event.player_tag}:{event.observed_at}",
            detection_type="member_left",
            subject_tag=event.player_tag,
            occurred_at=event.observed_at,
            caused_by=[self.evidence(notification)],
            payload={k: v for k, v in info.items() if v is not None},
        )


# Clan role hierarchy, low -> high. Used to distinguish promotions from demotions.
_ROLE_RANK = {"member": 0, "elder": 1, "coleader": 2, "leader": 3}


class MemberRoleChangeDetector(FollowerRunner):
    """Role promotions -> #clan-events (celebratory, matches v4 elder_promotion).
    Follows Clan MemberRoleChanged. Demotions are intentionally NOT posted (v4
    didn't publicly announce demotions either — they drove leader-action cards)."""

    name = "detector:member_role_change"
    aggregate_name = "Clan"

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "MemberRoleChanged":
            return
        old = _ROLE_RANK.get((event.old_role or "").lower(), -1)
        new = _ROLE_RANK.get((event.new_role or "").lower(), -1)
        if new <= old:  # demotion or lateral/unknown -> not a public celebration
            return
        self.emit_detection(
            dedup_key=f"member_promoted:{event.player_tag}:{event.new_role}:{event.observed_at}",
            detection_type="member_promoted",
            subject_tag=event.player_tag,
            occurred_at=event.observed_at,
            caused_by=[self.evidence(notification)],
            payload={"old_role": event.old_role, "new_role": event.new_role},
        )


class WarUpdateDetector(FollowerRunner):
    """One war-progress update per active battle DAY -> #river-race.

    Follows RiverRace CurrentStateObserved. Fires at most once per (clan, section,
    period_index) — i.e. once per battle day — and only on active battle days
    (period_type == 'warDay'), so it gives ~1/day during the race (no training-day
    or off-season noise, no fame-churn spam). The payload carries the day's fame /
    period points / clan score so the agent can compose a real "where we stand"
    update rather than a bare phase-transition note.

    Ceiling: standings vs other clans and who-hasn't-attacked still need the
    per-participant war data, which is not captured in the event store yet
    (see event-core-v5-autonomous-session-log.md backlog 2a)."""

    name = "detector:war_update"
    aggregate_name = "RiverRace"
    ACTIVE_PERIOD_TYPES = {"warDay"}

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "CurrentStateObserved":
            return
        obs = event.observation or {}
        if obs.get("period_type") not in self.ACTIVE_PERIOD_TYPES:
            return  # only post on active battle days; skip training / off-season
        clan = obs.get("clan_tag") or ""
        section = obs.get("section_index")
        period_index = obs.get("period_index")
        if section is None or period_index is None:
            return
        self.emit_detection(
            dedup_key=f"war_update:{clan}:{section}:{period_index}",
            detection_type="war_update",
            subject_tag=clan,
            occurred_at=event.observed_at,
            caused_by=[self.evidence(notification)],
            payload={
                "section_index": section,
                "period_index": period_index,
                "period_type": obs.get("period_type"),
                "war_state": obs.get("war_state"),
                "fame": obs.get("fame"),
                "period_points": obs.get("period_points"),
                "clan_score": obs.get("clan_score"),
            },
        )


class CohortWaveDetector(FollowerRunner):
    """Clan-wide wave -> #clan-events. Scans the detections projection: when >=3
    distinct members share a celebratory detection_type on the same Chicago day,
    emit one cohort_wave. Runs AFTER the detections projection is current."""

    name = "detector:cohort_wave"
    MIN_MEMBERS = 3
    WAVE_TYPES = ("badge_earned", "card_level_milestone", "new_card_unlocked", "new_champion_unlocked")

    def detect(self, event, notification) -> None:  # unused (scans projection)
        pass

    def run(self, batch: int = 500) -> int:
        from event_core.timeutil import chicago_day_for_utc

        rows = self.conn.execute(
            "SELECT detection_type, subject_tag, occurred_at FROM detections "
            "WHERE detection_type IN (%s) AND subject_tag IS NOT NULL"
            % ",".join("?" for _ in self.WAVE_TYPES),
            self.WAVE_TYPES,
        ).fetchall()
        groups: dict[tuple, set] = {}
        for r in rows:
            day = chicago_day_for_utc(r["occurred_at"])
            groups.setdefault((r["detection_type"], day), set()).add(r["subject_tag"])
        for (dtype, day), members in groups.items():
            if len(members) >= self.MIN_MEMBERS:
                self.emit_detection(
                    dedup_key=f"cohort_wave:{dtype}:{day}",
                    detection_type="cohort_wave",
                    subject_tag=None,
                    occurred_at=f"{day}T12:00:00Z",
                    caused_by=[f"cohort:{dtype}:{day}"],
                    payload={"wave_type": dtype, "day": day, "member_count": len(members)},
                )
        return self.emitted


_CLAN_FOUNDED_DEFAULT = "2026-02-04"


def _clan_founded() -> str:
    """Clan founding date (YYYY-MM-DD) from prompts config, with a safe default."""
    try:
        import prompts
        return prompts.thresholds().get("clan_founded", _CLAN_FOUNDED_DEFAULT)
    except Exception:
        return _CLAN_FOUNDED_DEFAULT


def _chicago_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Chicago")).date()


def _table_exists(conn, name: str) -> bool:
    """The date-driven detectors read v4 operational tables that exist in the live
    consolidated DB but not in isolated build/test stores — skip cleanly if absent."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


class CakeDayDetector(FollowerRunner):
    """Date-driven scan -> #clan-events: clan birthday, member join anniversaries
    (quarterly milestones on the join day), and member birthdays. Runs each tick;
    dedup keys include the Chicago date so each fires at most once per day."""

    name = "detector:cake_day"

    def detect(self, event, notification) -> None:  # unused (date-driven scan)
        pass

    def run(self, batch: int = 500) -> int:
        from datetime import datetime
        if not _table_exists(self.conn, "member_metadata"):
            return self.emitted
        today = _chicago_today()
        today_str = today.isoformat()
        stamp = f"{today_str}T12:00:00Z"

        founded = _clan_founded()
        if founded[5:] == today_str[5:]:
            years = today.year - int(founded[:4])
            if years >= 1:
                row = self.conn.execute(
                    "SELECT clan_name FROM clan_daily_metrics WHERE clan_name IS NOT NULL "
                    "ORDER BY metric_date DESC LIMIT 1"
                ).fetchone() if _table_exists(self.conn, "clan_daily_metrics") else None
                self.emit_detection(
                    dedup_key=f"clan_birthday:{today_str}",
                    detection_type="clan_birthday",
                    subject_tag=None,
                    occurred_at=stamp,
                    caused_by=[f"cake_day:clan_birthday:{today_str}"],
                    payload={"years": years, "clan_name": (row["clan_name"] if row else None)},
                )

        for r in self.conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name FROM member_metadata md "
            "JOIN members m ON m.member_id = md.member_id "
            "WHERE md.birth_month = ? AND md.birth_day = ? AND m.status = 'active'",
            (today.month, today.day),
        ).fetchall():
            self.emit_detection(
                dedup_key=f"member_birthday:{r['tag']}:{today_str}",
                detection_type="member_birthday",
                subject_tag=r["tag"],
                occurred_at=stamp,
                caused_by=[f"cake_day:birthday:{r['tag']}:{today_str}"],
                payload={"name": r["name"]},
            )

        for r in self.conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name, md.joined_at AS joined_at "
            "FROM member_metadata md JOIN members m ON m.member_id = md.member_id "
            "WHERE md.joined_at IS NOT NULL AND m.status = 'active'"
        ).fetchall():
            try:
                jd = datetime.fromisoformat(str(r["joined_at"])[:10]).date()
            except ValueError:
                continue
            if jd.day != today.day:
                continue
            months = (today.year - jd.year) * 12 + (today.month - jd.month)
            if months >= 3 and months % 3 == 0:
                self.emit_detection(
                    dedup_key=f"join_anniversary:{r['tag']}:{today_str}",
                    detection_type="join_anniversary",
                    subject_tag=r["tag"],
                    occurred_at=stamp,
                    caused_by=[f"cake_day:join_anniversary:{r['tag']}:{today_str}"],
                    payload={"name": r["name"], "months": months, "years": months // 12},
                )
        return self.emitted


class WeeklyDonationLeaderDetector(FollowerRunner):
    """Weekly top donor(s) -> #clan-events. Reads the most-recently-completed week's
    frozen donations from member_daily_metrics (last Sunday's row, which survives the
    Monday reset). Dedup per ISO week -> fires once per week."""

    name = "detector:weekly_donation_leader"
    TOP_N = 3

    def detect(self, event, notification) -> None:  # unused (date-driven scan)
        pass

    def run(self, batch: int = 500) -> int:
        from datetime import timedelta
        if not _table_exists(self.conn, "member_daily_metrics"):
            return self.emitted
        today = _chicago_today()
        # Most recent COMPLETED week's Sunday (strictly before this week).
        days_since_sunday = (today.weekday() + 1) % 7  # Mon->1 ... Sun->0
        offset = days_since_sunday if days_since_sunday else 7
        last_sunday = today - timedelta(days=offset)
        iso = last_sunday.isocalendar()
        week_key = f"{iso[0]}W{iso[1]:02d}"

        rows = self.conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name, d.donations_week AS donations "
            "FROM member_daily_metrics d JOIN members m ON m.member_id = d.member_id "
            "WHERE d.metric_date = ? AND d.donations_week > 0 AND m.status = 'active' "
            "ORDER BY d.donations_week DESC LIMIT ?",
            (last_sunday.isoformat(), self.TOP_N),
        ).fetchall()
        if not rows:
            return self.emitted  # no frozen data for that week yet / no donations

        leaders = [{"tag": r["tag"], "name": r["name"], "donations": r["donations"]} for r in rows]
        top = leaders[0]
        self.emit_detection(
            dedup_key=f"weekly_donation_leader:{week_key}",
            detection_type="weekly_donation_leader",
            subject_tag=top["tag"],
            occurred_at=f"{last_sunday.isoformat()}T12:00:00Z",
            caused_by=[f"weekly_donation_leader:{week_key}"],
            payload={"week_ending": last_sunday.isoformat(), "leaders": leaders},
        )
        return self.emitted


# Per-event detectors run in advance()'s detector loop. CohortWaveDetector is run
# separately (after the detections projection is current) — see live/engine.advance.
# BattleHotStreakDetector intentionally NOT registered: hot-streak is the
# less-interesting twin of battle_trophy_push and posted redundantly alongside it.
# We celebrate trophy/rank MOVEMENT instead (battle_trophy_push; mode-aware /
# Path-of-Legend movement is the 2f follow-up). The class is retained for reference.
ALL_DETECTORS = [
    PlayerLevelUpDetector,
    BestTrophiesPeakDetector,
    PathOfLegendDetector,
    CardLevelMilestoneDetector,
    NewCardUnlockedDetector,
    BadgeEarnedDetector,
    BattleTrophyPushDetector,
    MemberJoinedDetector,
    MemberLeftDetector,
    MemberRoleChangeDetector,
    WarUpdateDetector,
    CakeDayDetector,
    WeeklyDonationLeaderDetector,
]
