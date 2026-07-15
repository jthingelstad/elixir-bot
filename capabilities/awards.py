"""Canonical awards and recognition read contract.

Live award races are provisional projections.  Rows in ``awards`` are durable
grants written at close.  Keeping those states explicit prevents an empty
current-season ledger from being misread as "nobody is winning" and gives all
consumers one interpretation of the mixed war-season / Ranked-month period id.
"""

from __future__ import annotations

import db as db_facade
from capabilities.contracts import AwardsRecognitionResult
from capabilities.war import get_war_season_view
from engine.readiness import generation_snapshot

CAPABILITY_ID = "awards_recognition"
CONTRACT_VERSION = 1


def _source(source):
    return source or db_facade


def _invoke(source, name: str, *args, conn=None, **kwargs):
    if conn is not None:
        kwargs["conn"] = conn
    return getattr(source, name)(*args, **kwargs)


def award_period(season_id=None, award_type: str | None = None) -> dict:
    """Describe the overloaded awards ``season_id`` without changing storage."""
    clean_type = (award_type or "").strip().lower()
    numeric = int(season_id) if season_id is not None else None
    is_ranked_month = clean_type.startswith(("pol_", "ranked_")) or (
        numeric is not None and numeric >= 100000
    )
    return {
        "id": numeric,
        "kind": "path_of_legends_month" if is_ranked_month else "war_season",
    }


def get_awards_recognition(
    *,
    view: str = "list",
    award_type: str | None = None,
    season_id=None,
    rank=None,
    member_tag: str | None = None,
    limit: int | None = None,
    source=None,
    conn=None,
) -> AwardsRecognitionResult:
    """Return one awards facet with explicit provisional/durable state."""
    if conn is None and source in (None, db_facade):
        active_source = source or db_facade
        active = active_source.get_connection()
        try:
            active.execute("BEGIN")
            return get_awards_recognition(
                view=view,
                award_type=award_type,
                season_id=season_id,
                rank=rank,
                member_tag=member_tag,
                limit=limit,
                source=active_source,
                conn=active,
            )
        finally:
            active.close()
    source = _source(source)
    target_season = int(season_id) if season_id is not None else None
    target_rank = int(rank) if rank is not None else None
    default_limit = 10 if view == "races" else 20 if view == "leaderboard" else 100
    cap = int(limit) if limit is not None else default_limit

    if view == "current_standings":
        data = _invoke(
            source, "get_season_awards_standings", season_id=target_season, conn=conn
        )
        if isinstance(data, dict):
            data = dict(data)
            war_view = get_war_season_view(
                view="standings",
                metric="points",
                season_id=target_season,
                source=source,
                conn=conn,
            )["data"]
            data["freshness"] = (
                war_view.get("freshness") if isinstance(war_view, dict) else None
            )
            data["provisional"] = True
            data["source_note"] = (
                "live in-progress standings; awards are committed to the durable "
                "ledger only when the season closes."
            )
        state = "provisional"
    elif view == "races":
        data = _invoke(
            source,
            "get_award_races",
            season_id=target_season,
            war_champ_limit=cap,
            rookie_limit=cap,
            conn=conn,
        )
        state = "provisional"
    elif view == "leaderboard":
        results = _invoke(
            source,
            "award_leaderboard",
            award_type=award_type,
            rank=target_rank,
            limit=cap,
            conn=conn,
        )
        data = {
            "mode": "leaderboard",
            "filters": {"award_type": award_type, "rank": target_rank},
            "count": len(results),
            "results": results,
        }
        state = "durable"
    elif view == "member":
        data = _invoke(source, "get_member_trophy_case", member_tag, conn=conn)
        state = "durable"
    elif view == "list":
        results = _invoke(
            source,
            "list_awards",
            award_type=award_type,
            season_id=target_season,
            rank=target_rank,
            member_tag=member_tag,
            limit=cap,
            conn=conn,
        )
        data = {
            "mode": "list",
            "filters": {
                "member_tag": member_tag,
                "award_type": award_type,
                "season_id": target_season,
                "rank": target_rank,
            },
            "count": len(results),
            "truncated": len(results) >= cap,
            "season_id_note": (
                "season_id stores war-season integers and Path of Legends YYYYMM "
                "months; use the capability period.kind to distinguish them."
            ),
            "results": results,
        }
        state = "durable"
    else:
        data = {"error": f"Unknown awards view: {view}"}
        state = "unknown"

    resolved_season = target_season
    if isinstance(data, dict) and data.get("season_id") is not None:
        resolved_season = data.get("season_id")

    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "view": view,
        "state": state,
        "period": award_period(resolved_season, award_type),
        "sources": ["awards", "war_participation", "war_attendance_days"],
        "data_generation": (generation_snapshot(conn) if conn is not None else None),
        "data": data,
    }


__all__ = [
    "CAPABILITY_ID",
    "CONTRACT_VERSION",
    "award_period",
    "get_awards_recognition",
]
