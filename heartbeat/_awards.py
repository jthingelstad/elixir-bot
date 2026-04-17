"""heartbeat._awards — Detectors that grant season and week awards.

Each detector inserts durable rows into the ``awards`` table (idempotent via
INSERT OR IGNORE) and returns one ``award_earned`` signal per newly granted
award so the existing announcement / memory pipelines pick them up.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

import db
from db import managed_connection
from storage.war_status import _season_bounds


AWARD_DISPLAY_NAMES = {
    "war_champ": "War Champ",
    "iron_king": "Iron King",
    "donation_champ": "Donation Champ",
    "donation_champ_weekly": "Weekly Donation Champ",
    "war_participant": "War Participant",
    "perfect_week": "Perfect Week",
    "rookie_mvp": "Rookie MVP",
}


# -- signal helpers ---------------------------------------------------------

def _signal_log_type(
    award_type: str,
    season_id: int,
    section_index: Optional[int],
    player_tag: str,
    rank: int,
) -> str:
    scope = "season" if section_index is None else f"w{section_index}"
    return f"award_earned::{award_type}::{season_id}::{scope}::{player_tag}::r{rank}"


def _signal_date_for_season(conn: sqlite3.Connection, season_id: int) -> Optional[str]:
    start, end = _season_bounds(conn, season_id)
    # war_status helpers store CR format (YYYYMMDDTHHMMSS.000Z); strip to date.
    anchor = end or start
    if anchor and len(anchor) >= 8:
        return f"{anchor[0:4]}-{anchor[4:6]}-{anchor[6:8]}"
    return None


def _build_award_signal(
    *,
    award_type: str,
    season_id: int,
    section_index: Optional[int],
    member_id: int,
    player_tag: str,
    player_name: Optional[str],
    rank: int,
    metric_value: Optional[float],
    metric_unit: Optional[str],
    metadata: Optional[dict],
    signal_date: Optional[str],
) -> dict:
    return {
        "type": "award_earned",
        "signal_log_type": _signal_log_type(award_type, season_id, section_index, player_tag, rank),
        "signal_date": signal_date,
        "award_type": award_type,
        "award_display_name": AWARD_DISPLAY_NAMES.get(award_type, award_type),
        "season_id": season_id,
        "section_index": section_index,
        "tag": player_tag,
        "name": player_name,
        "member": {
            "tag": player_tag,
            "name": player_name,
            "member_id": member_id,
        },
        "rank": rank,
        "metric_value": metric_value,
        "metric_unit": metric_unit,
        "metadata": metadata or {},
    }


def _grant(
    conn: sqlite3.Connection,
    *,
    award_type: str,
    season_id: int,
    section_index: Optional[int],
    member_id: int,
    player_tag: str,
    player_name: Optional[str],
    rank: int = 1,
    metric_value: Optional[float] = None,
    metric_unit: Optional[str] = None,
    metadata: Optional[dict] = None,
    signal_date: Optional[str] = None,
) -> Optional[dict]:
    """Insert the award row and return an award_earned signal iff it's new."""
    inserted = db.insert_award(
        award_type,
        season_id,
        member_id,
        player_tag,
        section_index=section_index,
        rank=rank,
        metric_value=metric_value,
        metric_unit=metric_unit,
        metadata=metadata,
        conn=conn,
    )
    if not inserted:
        return None
    return _build_award_signal(
        award_type=award_type,
        season_id=season_id,
        section_index=section_index,
        member_id=member_id,
        player_tag=player_tag,
        player_name=player_name,
        rank=rank,
        metric_value=metric_value,
        metric_unit=metric_unit,
        metadata=metadata,
        signal_date=signal_date or _signal_date_for_season(conn, season_id),
    )


# -- season-wide awards -----------------------------------------------------

def _grant_war_champ(conn: sqlite3.Connection, season_id: int, signal_date: Optional[str]) -> list[dict]:
    standings = db.get_war_champ_standings(season_id=season_id, conn=conn)[:3]
    new_signals = []
    for i, entry in enumerate(standings):
        rank = i + 1
        member_row = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = ?",
            (entry["tag"],),
        ).fetchone()
        if not member_row:
            continue
        metadata = {
            "races_participated": entry.get("races_participated"),
            "avg_fame": entry.get("avg_fame"),
        }
        signal = _grant(
            conn,
            award_type="war_champ",
            season_id=season_id,
            section_index=None,
            member_id=member_row["member_id"],
            player_tag=entry["tag"],
            player_name=entry.get("name"),
            rank=rank,
            metric_value=entry.get("total_fame"),
            metric_unit="fame",
            metadata=metadata,
            signal_date=signal_date,
        )
        if signal:
            new_signals.append(signal)
    return new_signals


def _grant_iron_king(conn: sqlite3.Connection, season_id: int, signal_date: Optional[str]) -> list[dict]:
    candidates = db.get_iron_king_candidates(season_id=season_id, conn=conn)
    new_signals = []
    for c in candidates:
        metadata = {
            "perfect_days": c.get("perfect_days"),
            "total_battle_days": c.get("total_battle_days"),
        }
        signal = _grant(
            conn,
            award_type="iron_king",
            season_id=season_id,
            section_index=None,
            member_id=c["member_id"],
            player_tag=c["tag"],
            player_name=c.get("name"),
            rank=1,
            metric_value=c.get("total_battle_days"),
            metric_unit="battle_days",
            metadata=metadata,
            signal_date=signal_date,
        )
        if signal:
            new_signals.append(signal)
    return new_signals


def _grant_donation_champ(conn: sqlite3.Connection, season_id: int, signal_date: Optional[str]) -> list[dict]:
    leaderboard = db.get_season_donation_leaderboard(season_id=season_id, conn=conn)
    new_signals = []
    for entry in leaderboard:
        signal = _grant(
            conn,
            award_type="donation_champ",
            season_id=season_id,
            section_index=None,
            member_id=entry["member_id"],
            player_tag=entry["tag"],
            player_name=entry.get("name"),
            rank=entry["rank"],
            metric_value=entry.get("total_donations"),
            metric_unit="donations",
            metadata=None,
            signal_date=signal_date,
        )
        if signal:
            new_signals.append(signal)
    return new_signals


def _grant_rookie_mvp(conn: sqlite3.Connection, season_id: int, signal_date: Optional[str]) -> list[dict]:
    candidates = db.get_rookie_mvp_candidates(season_id=season_id, conn=conn)
    new_signals = []
    for entry in candidates:
        metadata = {"races_participated": entry.get("races_participated")}
        signal = _grant(
            conn,
            award_type="rookie_mvp",
            season_id=season_id,
            section_index=None,
            member_id=entry["member_id"],
            player_tag=entry["tag"],
            player_name=entry.get("name"),
            rank=entry["rank"],
            metric_value=entry.get("total_fame"),
            metric_unit="fame",
            metadata=metadata,
            signal_date=signal_date,
        )
        if signal:
            new_signals.append(signal)
    return new_signals


def grant_season_awards(season_id: int, conn: sqlite3.Connection) -> list[dict]:
    """Grant every season-wide award type for a completed season."""
    signal_date = _signal_date_for_season(conn, season_id)
    signals = []
    signals.extend(_grant_war_champ(conn, season_id, signal_date))
    signals.extend(_grant_iron_king(conn, season_id, signal_date))
    signals.extend(_grant_donation_champ(conn, season_id, signal_date))
    signals.extend(_grant_rookie_mvp(conn, season_id, signal_date))
    return signals


# -- week-scoped awards -----------------------------------------------------

def grant_week_awards(season_id: int, section_index: int, conn: sqlite3.Connection) -> list[dict]:
    """Grant Perfect Week to every qualifying player for a completed week."""
    candidates = db.get_perfect_week_candidates(
        season_id=season_id, section_index=section_index, conn=conn
    )
    signal_date = _signal_date_for_season(conn, season_id)
    signals = []
    for c in candidates:
        metadata = {"total_battle_days": c.get("total_battle_days")}
        signal = _grant(
            conn,
            award_type="perfect_week",
            season_id=season_id,
            section_index=section_index,
            member_id=c["member_id"],
            player_tag=c["tag"],
            player_name=c.get("name"),
            rank=1,
            metric_value=c.get("total_battle_days"),
            metric_unit="battle_days",
            metadata=metadata,
            signal_date=signal_date,
        )
        if signal:
            signals.append(signal)
    return signals


# -- detectors --------------------------------------------------------------

@managed_connection
def detect_season_awards(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Grant season-wide awards for any season that has ended and isn't yet awarded.

    A season is considered ended once a newer season has appeared in
    ``war_races``. We skip seasons that already have at least one ``war_champ``
    row so the detector is safe to run every heartbeat tick.
    """
    rows = conn.execute(
        "SELECT DISTINCT season_id FROM war_races ORDER BY season_id DESC LIMIT 20"
    ).fetchall()
    seasons = [r["season_id"] for r in rows if r["season_id"] is not None]
    if len(seasons) < 2:
        return []
    signals = []
    for season_id in seasons[1:]:
        already = conn.execute(
            "SELECT 1 FROM awards WHERE award_type = 'war_champ' AND season_id = ? LIMIT 1",
            (season_id,),
        ).fetchone()
        if already:
            continue
        signals.extend(grant_season_awards(season_id, conn))
    return signals


@managed_connection
def detect_weekly_awards(
    completion_signals: Optional[Iterable[dict]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Grant Perfect Week for each war_completed signal whose week is final."""
    signals = []
    seen = set()
    for signal in completion_signals or []:
        if signal.get("type") != "war_completed":
            continue
        season_id = signal.get("season_id")
        section_index = signal.get("section_index")
        if season_id is None or section_index is None:
            continue
        key = (season_id, section_index)
        if key in seen:
            continue
        seen.add(key)
        signals.extend(grant_week_awards(season_id, section_index, conn))
    return signals


@managed_connection
def detect_weekly_donation_awards(
    weekly_leader_signals: Optional[Iterable[dict]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Persist donation_champ_weekly for each weekly_donation_leader payload.

    The existing signal carries a ``leaders`` list with top-3 donors of the
    prior CR week. We pin each to the current season and the section_index of
    the most recently completed war week so the row lines up with the
    weekly war recap on the site.
    """
    signals = []
    for signal in weekly_leader_signals or []:
        if signal.get("type") != "weekly_donation_leader":
            continue
        leaders = signal.get("leaders") or []
        if not leaders:
            continue
        season_id = db.get_current_season_id(conn=conn)
        if season_id is None:
            continue
        last_week_row = conn.execute(
            "SELECT section_index FROM war_races WHERE season_id = ? ORDER BY section_index DESC LIMIT 1",
            (season_id,),
        ).fetchone()
        section_index = last_week_row["section_index"] if last_week_row else None
        signal_date = signal.get("week_ending")
        for entry in leaders:
            tag = entry.get("tag")
            if not tag:
                continue
            member_row = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (tag,),
            ).fetchone()
            if not member_row:
                continue
            new_signal = _grant(
                conn,
                award_type="donation_champ_weekly",
                season_id=season_id,
                section_index=section_index,
                member_id=member_row["member_id"],
                player_tag=tag,
                player_name=entry.get("name"),
                rank=entry.get("rank") or 1,
                metric_value=entry.get("donations"),
                metric_unit="donations",
                metadata={"week_key": signal.get("week_key")},
                signal_date=signal_date,
            )
            if new_signal:
                signals.append(new_signal)
    return signals


@managed_connection
def detect_war_participant_awards(
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Grant War Participant to every active member with fame > 0 this season."""
    season_id = db.get_current_season_id(conn=conn)
    if season_id is None:
        return []
    candidates = db.get_war_participant_candidates(season_id=season_id, conn=conn)
    signal_date = _signal_date_for_season(conn, season_id)
    signals = []
    for c in candidates:
        signal = _grant(
            conn,
            award_type="war_participant",
            season_id=season_id,
            section_index=None,
            member_id=c["member_id"],
            player_tag=c["tag"],
            player_name=c.get("name"),
            rank=1,
            metric_value=c.get("total_fame"),
            metric_unit="fame",
            metadata=None,
            signal_date=signal_date,
        )
        if signal:
            signals.append(signal)
    return signals
