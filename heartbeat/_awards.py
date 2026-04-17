"""heartbeat._awards — Detectors that grant season and week awards.

Each detector inserts durable rows into the ``awards`` table (idempotent via
INSERT OR IGNORE) and returns one ``award_earned`` signal per newly granted
award so the existing announcement / memory pipelines pick them up.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Iterable, Optional

import db
from db import managed_connection
from storage.war_status import _season_bounds


AWARD_DISPLAY_NAMES = {
    "war_champ": "War Champ",
    "iron_king": "Iron King",
    "donation_champ": "Donation Champ",
    "donation_champ_weekly": "Donation Hero",
    "war_participant": "War Participant",
    "perfect_week": "Perfect Week",
    "victory_lap": "Victory Lap",
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
    """Grant Perfect Week and Victory Lap for every qualifying player in a week."""
    signal_date = _signal_date_for_season(conn, season_id)
    signals: list[dict] = []
    for c in db.get_perfect_week_candidates(
        season_id=season_id, section_index=section_index, conn=conn
    ):
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
    for c in db.get_victory_lap_candidates(
        season_id=season_id, section_index=section_index, conn=conn
    ):
        metadata = {
            "post_victory_days": c.get("post_victory_days"),
            "peak_decks": c.get("peak_decks"),
        }
        signal = _grant(
            conn,
            award_type="victory_lap",
            season_id=season_id,
            section_index=section_index,
            member_id=c["member_id"],
            player_tag=c["tag"],
            player_name=c.get("name"),
            rank=1,
            metric_value=c.get("peak_decks"),
            metric_unit="decks",
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
    return grant_war_participant_for_season(season_id, conn)


def grant_war_participant_for_season(
    season_id: int,
    conn: sqlite3.Connection,
) -> list[dict]:
    """Grant War Participant for one specific season (callable for backfill)."""
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


# -- backfill ---------------------------------------------------------------

def grant_weekly_donation_for_season(
    season_id: int,
    conn: sqlite3.Connection,
) -> list[dict]:
    """Reconstruct weekly donation podiums for a season from member_daily_metrics.

    Finds every Sunday inside the season's date window and, for each, grants
    donation_champ_weekly rank 1/2/3 to the top-3 ``donations_week`` values.
    The ``section_index`` is set to the ordinal of each Sunday (0, 1, 2, ...)
    matching the war-week cadence. Returns the list of newly-granted signals.

    Used by the backfill script for historical seasons where the live
    ``weekly_donation_leader`` detector never fired.
    """
    from storage.awards import _season_metric_date_bounds

    start, end = _season_metric_date_bounds(conn, season_id)
    if not start or not end:
        return []
    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()

    # Enumerate Sundays within [start, end]. A Sunday is weekday()==6 in Python.
    sundays: list[str] = []
    cursor = start_dt
    while cursor <= end_dt:
        if cursor.weekday() == 6:
            sundays.append(cursor.isoformat())
        cursor += timedelta(days=1)
    if not sundays:
        return []

    signals = []
    for section_ordinal, sunday in enumerate(sundays):
        rows = conn.execute(
            """
            SELECT m.player_tag AS tag, m.current_name AS name, m.member_id,
                   d.donations_week AS donations
            FROM member_daily_metrics d
            JOIN members m ON m.member_id = d.member_id
            WHERE d.metric_date = ? AND d.donations_week > 0
              AND m.status = 'active'
            ORDER BY d.donations_week DESC
            LIMIT 3
            """,
            (sunday,),
        ).fetchall()
        if not rows:
            continue
        week_key = datetime.strptime(sunday, "%Y-%m-%d").strftime("%GW%V")
        for i, r in enumerate(rows):
            rank = i + 1
            signal = _grant(
                conn,
                award_type="donation_champ_weekly",
                season_id=season_id,
                section_index=section_ordinal,
                member_id=r["member_id"],
                player_tag=r["tag"],
                player_name=r["name"],
                rank=rank,
                metric_value=r["donations"],
                metric_unit="donations",
                metadata={"week_key": week_key, "week_ending": sunday},
                signal_date=sunday,
            )
            if signal:
                signals.append(signal)
    return signals


def _sweep_stale_award_rows(
    conn: sqlite3.Connection,
    season_id: int,
    *,
    include_season_wide: bool,
) -> dict[str, list[dict]]:
    """Delete awards in this season that no longer match the candidate query.

    Returns ``{award_type: [deleted_rows, ...]}`` keyed by award type for the
    rows that were removed. Each deleted row carries
    ``{member_id, player_tag, player_name, section_index}``.

    Skips season-wide award types when ``include_season_wide`` is False — the
    candidate queries for in-progress seasons aren't meaningful yet.
    """
    from storage.awards import (
        SEASON_WIDE_SECTION,
        get_iron_king_candidates,
        get_perfect_week_candidates,
        get_victory_lap_candidates,
    )
    from db import get_rookie_mvp_candidates, get_season_donation_leaderboard, get_war_champ_standings

    deletions: dict[str, list[dict]] = {}

    def _sweep(award_type: str, section_index: Optional[int], live_member_ids: set[int]):
        section_sql = SEASON_WIDE_SECTION if section_index is None else int(section_index)
        rows = conn.execute(
            "SELECT a.award_id, a.member_id, a.player_tag, m.current_name "
            "FROM awards a LEFT JOIN members m ON m.member_id = a.member_id "
            "WHERE a.award_type = ? AND a.season_id = ? AND a.section_index = ?",
            (award_type, int(season_id), section_sql),
        ).fetchall()
        removed = []
        for row in rows:
            if row["member_id"] in live_member_ids:
                continue
            conn.execute("DELETE FROM awards WHERE award_id = ?", (row["award_id"],))
            removed.append({
                "member_id": row["member_id"],
                "player_tag": row["player_tag"],
                "player_name": row["current_name"],
                "section_index": section_index,
            })
        if removed:
            deletions.setdefault(award_type, []).extend(removed)

    # Weekly awards — always swept.
    section_rows = conn.execute(
        "SELECT section_index FROM war_races WHERE season_id = ? ORDER BY section_index",
        (int(season_id),),
    ).fetchall()
    for r in section_rows:
        section = r["section_index"]
        _sweep(
            "perfect_week", section,
            {c["member_id"] for c in get_perfect_week_candidates(
                season_id=season_id, section_index=section, conn=conn)},
        )
        _sweep(
            "victory_lap", section,
            {c["member_id"] for c in get_victory_lap_candidates(
                season_id=season_id, section_index=section, conn=conn)},
        )

    # Season-wide awards — only swept when the season is closed, since those
    # candidate queries only produce a stable answer after season close.
    if include_season_wide:
        _sweep(
            "iron_king", None,
            {c["member_id"] for c in get_iron_king_candidates(
                season_id=season_id, conn=conn)},
        )
        war_champ_member_ids: set[int] = set()
        for entry in get_war_champ_standings(season_id=season_id, conn=conn)[:3]:
            row = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (entry["tag"],),
            ).fetchone()
            if row:
                war_champ_member_ids.add(row["member_id"])
        _sweep("war_champ", None, war_champ_member_ids)
        _sweep(
            "donation_champ", None,
            {c["member_id"] for c in get_season_donation_leaderboard(
                season_id=season_id, conn=conn)},
        )
        _sweep(
            "rookie_mvp", None,
            {c["member_id"] for c in get_rookie_mvp_candidates(season_id=season_id, conn=conn)},
        )

    conn.commit()
    return deletions


@managed_connection
def backfill_season(
    season_id: int,
    *,
    include_season_wide: Optional[bool] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Grant every award type for one season; return a summary of new rows.

    ``include_season_wide`` defaults to True when the season has closed
    (a newer season exists in ``war_races``) and False otherwise — this lets
    backfill cover weekly awards for the current season without granting
    season-wide podiums before the season has ended.

    Returns ``{award_type: [signal_dicts, ...]}`` keyed by award type for
    newly-inserted rows, plus ``_revoked`` — a dict of the same shape holding
    rows that were removed because the current candidate query no longer
    includes them. Existing rows that still qualify are left alone.
    """
    if include_season_wide is None:
        include_season_wide = db.season_is_complete(season_id, conn=conn)

    summary: dict[str, list[dict]] = {}

    # War Participant — runs for any season (in-progress or closed).
    summary["war_participant"] = grant_war_participant_for_season(season_id, conn)

    # Perfect Week + Victory Lap — for every section_index in war_races.
    rows = conn.execute(
        "SELECT section_index FROM war_races WHERE season_id = ? ORDER BY section_index",
        (season_id,),
    ).fetchall()
    perfect_week_signals: list[dict] = []
    victory_lap_signals: list[dict] = []
    for r in rows:
        for s in grant_week_awards(season_id, r["section_index"], conn):
            if s.get("award_type") == "victory_lap":
                victory_lap_signals.append(s)
            else:
                perfect_week_signals.append(s)
    summary["perfect_week"] = perfect_week_signals
    summary["victory_lap"] = victory_lap_signals

    # Weekly Donation Champ — reconstructed from member_daily_metrics.
    summary["donation_champ_weekly"] = grant_weekly_donation_for_season(season_id, conn)

    # Season-wide awards only when the season has closed.
    if include_season_wide:
        signal_date = _signal_date_for_season(conn, season_id)
        summary["war_champ"] = _grant_war_champ(conn, season_id, signal_date)
        summary["iron_king"] = _grant_iron_king(conn, season_id, signal_date)
        summary["donation_champ"] = _grant_donation_champ(conn, season_id, signal_date)
        summary["rookie_mvp"] = _grant_rookie_mvp(conn, season_id, signal_date)
    else:
        summary["war_champ"] = []
        summary["iron_king"] = []
        summary["donation_champ"] = []
        summary["rookie_mvp"] = []

    summary["_revoked"] = _sweep_stale_award_rows(
        conn, season_id, include_season_wide=include_season_wide,
    )
    return summary
