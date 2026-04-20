"""heartbeat._awards — Detectors that grant season-wide clan awards.

Each detector inserts durable rows into the ``awards`` table (idempotent via
INSERT OR IGNORE) and returns one ``award_earned`` signal per newly granted
award so the existing announcement / memory pipelines pick them up.

All awards are season-wide. Weekly awards (perfect_week, victory_lap,
donation_champ_weekly) were removed — they produced too much noise. Existing
rows of those types are pruned from the database by ``backfill_season``'s
sweep step.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import db
from db import managed_connection
from storage.war_status import _season_bounds


AWARD_DISPLAY_NAMES = {
    "war_champ": "War Champ",
    "iron_king": "Iron King",
    "donation_champ": "Donation Champ",
    "war_participant": "War Participant",
    "rookie_mvp": "Rookie MVP",
}


DEPRECATED_AWARD_TYPES = ("perfect_week", "victory_lap", "donation_champ_weekly")


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


# -- detectors --------------------------------------------------------------

def _build_season_awards_signal(
    season_id: int,
    per_award_signals: list[dict],
    signal_date: Optional[str],
) -> Optional[dict]:
    """Collapse the per-award grant signals for one season into a single
    ``season_awards_granted`` payload. War Participant grants are omitted —
    they land in the DB but don't belong on the podium post (20+ names is
    noise; the per-member tools surface them).

    Returns None when the season has no podium grants (all entries were
    war_participant or empty).
    """
    if not per_award_signals:
        return None
    buckets = {
        "war_champ": [],
        "iron_king": [],
        "donation_champ": [],
        "rookie_mvp": [],
    }
    for signal in per_award_signals:
        award_type = signal.get("award_type")
        if award_type == "war_participant" or award_type not in buckets:
            continue
        buckets[award_type].append({
            "rank": signal.get("rank"),
            "tag": signal.get("tag"),
            "name": signal.get("name"),
            "metric_value": signal.get("metric_value"),
            "metric_unit": signal.get("metric_unit"),
            "metadata": signal.get("metadata") or {},
        })
    if not any(buckets.values()):
        return None
    for key in buckets:
        buckets[key].sort(key=lambda e: (e.get("rank") or 99, e.get("name") or ""))
    return {
        "type": "season_awards_granted",
        "signal_log_type": f"season_awards_granted::{season_id}",
        "signal_date": signal_date,
        "season_id": season_id,
        "war_champ": buckets["war_champ"],
        "iron_kings": buckets["iron_king"],
        "donation_champs": buckets["donation_champ"],
        "rookie_mvps": buckets["rookie_mvp"],
    }


@managed_connection
def detect_season_awards(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Grant season-wide awards for any season that has ended and isn't yet
    awarded, and emit one consolidated ``season_awards_granted`` signal per
    newly-granted season.

    A season is considered ended once a newer season has appeared in
    ``war_races``. We skip seasons that already have at least one ``war_champ``
    row so the detector is safe to run every heartbeat tick — and because that
    guard prevents re-granting, historical seasons (whose awards were already
    emitted under the old per-award signal pattern) will never re-fire under
    the new aggregated pattern either.
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
        per_award = grant_season_awards(season_id, conn)
        aggregated = _build_season_awards_signal(
            season_id, per_award, _signal_date_for_season(conn, season_id),
        )
        if aggregated:
            signals.append(aggregated)
    return signals


@managed_connection
def detect_war_participant_awards(
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Grant War Participant to every active member with fame > 0 this season.

    Silent in Discord: the grants land in the awards table (where tools and
    the site can read them) but no signal is returned. Naming 20+ participants
    on a post isn't useful; the per-member trophy case is.
    """
    season_id = db.get_current_season_id(conn=conn)
    if season_id is None:
        return []
    grant_war_participant_for_season(season_id, conn)
    return []


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

def _sweep_stale_award_rows(
    conn: sqlite3.Connection,
    season_id: int,
    *,
    include_season_wide: bool,
) -> dict[str, list[dict]]:
    """Delete awards in this season that no longer match the current rules.

    Removes two kinds of stale rows:
    1. Deprecated award types (``perfect_week``, ``victory_lap``,
       ``donation_champ_weekly``) — these are purged unconditionally since
       the feature was removed.
    2. Season-wide award rows whose holder is no longer in the candidate
       query (only when ``include_season_wide`` is True).

    Returns ``{award_type: [deleted_rows, ...]}`` keyed by award type.
    """
    from storage.awards import SEASON_WIDE_SECTION, get_iron_king_candidates
    from db import get_rookie_mvp_candidates, get_season_donation_leaderboard, get_war_champ_standings

    deletions: dict[str, list[dict]] = {}

    def _record_deletions(award_type: str, rows: list):
        for row in rows:
            deletions.setdefault(award_type, []).append({
                "member_id": row["member_id"],
                "player_tag": row["player_tag"],
                "player_name": row["current_name"],
                "section_index": (
                    None if row["section_index"] == SEASON_WIDE_SECTION
                    else row["section_index"]
                ),
            })

    # Purge every row of a deprecated award type for this season.
    for award_type in DEPRECATED_AWARD_TYPES:
        rows = conn.execute(
            "SELECT a.award_id, a.member_id, a.player_tag, a.section_index, "
            "m.current_name "
            "FROM awards a LEFT JOIN members m ON m.member_id = a.member_id "
            "WHERE a.award_type = ? AND a.season_id = ?",
            (award_type, int(season_id)),
        ).fetchall()
        if not rows:
            continue
        _record_deletions(award_type, rows)
        conn.execute(
            "DELETE FROM awards WHERE award_type = ? AND season_id = ?",
            (award_type, int(season_id)),
        )

    # Season-wide: drop any award whose holder is no longer in the candidate
    # set. Only run when the season has closed — in-progress candidate
    # queries aren't stable yet.
    def _sweep_season_wide(award_type: str, live_member_ids: set[int]):
        rows = conn.execute(
            "SELECT a.award_id, a.member_id, a.player_tag, a.section_index, "
            "m.current_name "
            "FROM awards a LEFT JOIN members m ON m.member_id = a.member_id "
            "WHERE a.award_type = ? AND a.season_id = ? AND a.section_index = ?",
            (award_type, int(season_id), SEASON_WIDE_SECTION),
        ).fetchall()
        stale = [r for r in rows if r["member_id"] not in live_member_ids]
        if not stale:
            return
        _record_deletions(award_type, stale)
        for row in stale:
            conn.execute("DELETE FROM awards WHERE award_id = ?", (row["award_id"],))

    if include_season_wide:
        _sweep_season_wide(
            "iron_king",
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
        _sweep_season_wide("war_champ", war_champ_member_ids)
        _sweep_season_wide(
            "donation_champ",
            {c["member_id"] for c in get_season_donation_leaderboard(
                season_id=season_id, conn=conn)},
        )
        _sweep_season_wide(
            "rookie_mvp",
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
    """Grant season-wide awards for one season; return a summary of new rows.

    ``include_season_wide`` defaults to True when the season has closed
    (a newer season exists in ``war_races``) and False otherwise — this lets
    backfill cover ``war_participant`` for the current season without
    granting season-wide podiums before the season has ended.

    Returns ``{award_type: [signal_dicts, ...]}`` keyed by award type for
    newly-inserted rows, plus ``_revoked`` — a dict of the same shape holding
    rows that were removed either because (a) the current candidate query no
    longer includes them, or (b) their award type was deprecated.
    """
    if include_season_wide is None:
        include_season_wide = db.season_is_complete(season_id, conn=conn)

    summary: dict[str, list[dict]] = {}

    # War Participant — runs for any season (in-progress or closed).
    summary["war_participant"] = grant_war_participant_for_season(season_id, conn)

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
