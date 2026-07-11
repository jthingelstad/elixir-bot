"""War analytics — v5.1 sources (docs/v5.1/schema.md §9).

The headline upgrade: at_risk / promotion / demotion read the deterministic
member_management projection (management.md states + evidence columns) instead
of recomputing eligibility per call — the tool output and the leader-action
pipeline can no longer disagree. War reads come from war_participation /
war_weeks / war_attendance_days; battle-level war reads (win rates, decks)
come from battle_events war keys + deck_json (a join, not raw_json inference).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from db import (
    _canon_tag,
    _rowdicts,
    managed_connection,
)
from storage._enrichment import _member_reference_fields
from storage.member_ranks import ELDER_ELIGIBILITY_DEFAULTS

_ACTIVE = (
    "EXISTS (SELECT 1 FROM clan_memberships cm "
    "WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL)"
)

# Carried trophy-scaled inactivity anchors (CLAN.md thresholds; the engine's
# management module owns the live kick machine — these back the tool's
# criteria echo only).
INACTIVITY_DAYS_PER_1K_TROPHIES_LOOSE = 1.4
INACTIVITY_DAYS_PER_1K_TROPHIES_TIGHT = 0.7
LOOSE_MEMBER_COUNT = 40
TIGHT_MEMBER_COUNT = 50
ELDER_DONATION_ROLLING_WEEKS = 4


def _member_activity_anchor(conn) -> datetime:
    """Latest battle timestamp clan-wide, as the 'today' anchor for
    activity math (carried semantics: anchor on data, not wall clock; falls
    back to now when the stream is empty)."""
    row = conn.execute("SELECT MAX(battle_time) AS ts FROM battle_events").fetchone()
    ts = row["ts"] if row else None
    if ts:
        from engine.normalize import parse_cr_time  # the single parser

        parsed = parse_cr_time(ts)
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)


def _classify_war_player_rate(total: int, played: int) -> str:
    if not total:
        return "unknown"
    rate = played / total
    if rate >= 0.75:
        return "regular"
    if rate >= 0.25:
        return "occasional"
    return "rare"


def _war_player_type(conn, player_tag) -> str:
    tag = _canon_tag(player_tag)
    row = conn.execute(
        "SELECT COUNT(DISTINCT ww.season_id || ':' || ww.section_index) AS total, "
        "COUNT(DISTINCT CASE WHEN COALESCE(wp.decks_used, 0) > 0 "
        "  THEN wp.season_id || ':' || wp.section_index END) AS played "
        "FROM war_weeks ww "
        "LEFT JOIN war_participation wp ON wp.season_id = ww.season_id "
        "  AND wp.section_index = ww.section_index AND wp.player_tag = ?",
        (tag,),
    ).fetchone()
    return _classify_war_player_rate(row["total"] or 0, row["played"] or 0)


def war_player_types_by_tag(conn, player_tags: list[str]) -> dict[str, str]:
    return {t: _war_player_type(conn, t) for t in {_canon_tag(t) for t in player_tags if t}}


@managed_connection
def get_members_without_war_participation(season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> dict:
    from storage.war_status import get_current_season_id

    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    members = []
    if season_id is not None:
        rows = conn.execute(
            "SELECT m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, cs.role, cs.clan_rank "
            "FROM players m "
            "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
            f"WHERE {_ACTIVE} AND NOT EXISTS ("
            "  SELECT 1 FROM war_participation wp WHERE wp.player_tag = m.player_tag "
            "  AND wp.season_id = ? AND COALESCE(wp.decks_used, 0) > 0) "
            "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE",
            (season_id,),
        ).fetchall()
        members = [_member_reference_fields(conn, r["tag"], dict(r)) for r in rows]
    return {"season_id": season_id, "members": members}


@managed_connection
def compare_member_war_to_clan_average(tag: str, season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    from storage.war_status import get_current_season_id

    member_tag = _canon_tag(tag)
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return None
    mine = conn.execute(
        "SELECT SUM(COALESCE(fame, 0)) AS fame, COUNT(*) AS races, "
        "SUM(COALESCE(decks_used, 0)) AS decks_used "
        "FROM war_participation WHERE season_id = ? AND player_tag = ?",
        (season_id, member_tag),
    ).fetchone()
    clan = conn.execute(
        "SELECT COUNT(DISTINCT player_tag) AS participants, "
        "SUM(COALESCE(fame, 0)) AS total_fame, "
        "AVG(COALESCE(fame, 0)) AS avg_fame_per_row "
        "FROM war_participation WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    participants = clan["participants"] or 0
    avg_fame_per_member = round((clan["total_fame"] or 0) / participants, 1) if participants else 0
    name_row = conn.execute("SELECT COALESCE(display_name, current_name) AS name FROM players WHERE player_tag = ?", (member_tag,)).fetchone()
    return {
        "season_id": season_id,
        "tag": member_tag,
        "name": name_row["name"] if name_row else None,
        "member": {
            "total_fame": mine["fame"] or 0,
            "races_participated": mine["races"] or 0,
            "decks_used": mine["decks_used"] or 0,
        },
        "clan_average": {
            "participants": participants,
            "avg_fame_per_member": avg_fame_per_member,
        },
        "fame_vs_average": (mine["fame"] or 0) - avg_fame_per_member,
    }


def _in_game_idle_days(last_seen_api, now: Optional[str] = None) -> Optional[float]:
    """Days since the CR API lastSeen (the in-game 'idle' roster badge). Pure
    awareness — not an engagement signal (architecture §13.6). None if unknown."""
    if not last_seen_api:
        return None
    from engine.normalize import parse_cr_time

    seen = parse_cr_time(last_seen_api)
    if seen is None:
        return None
    ref = parse_cr_time(now) if now else datetime.now(timezone.utc)
    if ref is None:
        ref = datetime.now(timezone.utc)
    return max(0.0, (ref - seen).total_seconds() / 86400.0)


def _mgmt_rows(conn):
    # last_seen_api arrived after the v5.1 cut (roster-badge awareness); select
    # it only when present so pre-ALTER DBs / lean fixtures still read cleanly.
    has_last_seen = any(
        r[1] == "last_seen_api"
        for r in conn.execute("PRAGMA table_info(player_current_state)")
    )
    last_seen_col = "cs.last_seen_api, " if has_last_seen else "NULL AS last_seen_api, "
    return conn.execute(
        "SELECT mm.*, COALESCE(m.display_name, m.current_name) AS name, cs.clan_rank, cs.trophies, "
        "cs.donations_week, " + last_seen_col + "mm.player_tag AS tag "
        "FROM member_management mm "
        "JOIN players m ON m.player_tag = mm.player_tag "
        "LEFT JOIN player_current_state cs ON cs.player_tag = mm.player_tag "
        "WHERE EXISTS (SELECT 1 FROM clan_memberships cm "
        "  WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL) "
        "ORDER BY COALESCE(cs.clan_rank, 999), name COLLATE NOCASE"
    ).fetchall()


@managed_connection
def get_members_at_risk(inactivity_days: int = 7, min_donations_week: int = 20, require_war_participation: bool = False,
                        min_war_races: int = 1, include_leadership: bool = False,
                        season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> dict:
    """v5.1: reads the member_management projection (kick_state + evidence
    columns) — the same deterministic state the leader-action pipeline uses
    (management.md §3.3). Parameters are kept for caller compatibility; the
    thresholds live in the engine's management constants."""
    from storage.war_status import get_current_season_id

    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    flagged = []
    for row in _mgmt_rows(conn):
        role = (row["role"] or "").strip() if row["role"] else ""
        if not include_leadership and role in {"leader", "coLeader"}:
            continue
        kick_state = row["kick_state"] or "none"
        reasons = []
        if kick_state in {"watch", "at_risk", "recommended"}:
            idle_days = _in_game_idle_days(row["last_seen_api"])
            detail = f"kick_state={kick_state} (battle-based idleness; management.md §3.3)"
            if idle_days is not None and idle_days >= 1:
                detail += f" · in-game idle {int(idle_days)}d (roster badge showing)"
            reasons.append({
                "type": "inactive",
                "detail": detail,
                "value": kick_state,
                "kick_state_since": row["kick_state_since"],
                "battle_days_last_28": row["battle_days_last_28"],
                "in_game_idle_days": round(idle_days, 1) if idle_days is not None else None,
            })
        donations_week = row["donations_week"] or 0
        if donations_week < min_donations_week:
            reasons.append({
                "type": "low_donations",
                "detail": f"{donations_week} donations this week",
                "value": donations_week,
            })
        if require_war_participation and (row["war_attendance_rate"] or 0) <= 0:
            reasons.append({
                "type": "low_war_participation",
                "detail": "no war participation this window",
                "value": 0,
            })
        if reasons:
            item = {
                "tag": row["tag"], "name": row["name"], "role": row["role"],
                "trophies": row["trophies"],
                "clan_rank": row["clan_rank"], "donations_week": donations_week,
                "joined_date": None, "tenure_days": row["tenure_days"],
                "kick_state": kick_state,
                "activity_context": {
                    "battle_days_last_28": row["battle_days_last_28"],
                    "kick_state": kick_state,
                    "kick_state_since": row["kick_state_since"],
                },
                "risk_score": len(reasons) + (2 if kick_state == "recommended" else 1 if kick_state == "at_risk" else 0),
                "reasons": reasons,
                "war_player_type": _war_player_type(conn, row["tag"]),
            }
            flagged.append(_member_reference_fields(conn, row["tag"], item))
    flagged.sort(key=lambda item: (-item["risk_score"], item.get("clan_rank") or 999, (item.get("name") or "").lower()))
    return {
        "season_id": season_id,
        "criteria": {
            "source": "member_management projection (management.md §3.3)",
            "inactivity_days_floor": inactivity_days,
            "inactivity_threshold_formula": "max(7, trophies/1000 * 1.4) days without a battle",
            "min_donations_week": min_donations_week,
            "require_war_participation": require_war_participation,
            "min_war_races": min_war_races,
            "include_leadership": include_leadership,
        },
        "members": flagged,
    }


@managed_connection
def get_trending_war_contributors(season_id: Optional[str] = None, recent_races: int = 2, limit: int = 5, conn: Optional[sqlite3.Connection] = None) -> dict:
    from storage.war_status import get_current_season_id

    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return {"members": []}
    sections = [r["section_index"] for r in conn.execute(
        "SELECT DISTINCT section_index FROM war_participation WHERE season_id = ? ORDER BY section_index DESC",
        (season_id,),
    ).fetchall()]
    recent = sections[:max(1, recent_races)]
    earlier = sections[max(1, recent_races):]
    if not recent:
        return {"members": []}
    members = {}
    for section_set, key in ((recent, "recent"), (earlier, "earlier")):
        if not section_set:
            continue
        qmarks = ",".join("?" for _ in section_set)
        for row in conn.execute(
            f"SELECT player_tag, AVG(COALESCE(fame, 0)) AS avg_fame FROM war_participation "
            f"WHERE season_id = ? AND section_index IN ({qmarks}) GROUP BY player_tag",
            (season_id, *section_set),
        ).fetchall():
            members.setdefault(row["player_tag"], {})[key] = row["avg_fame"] or 0
    out = []
    for tag, vals in members.items():
        recent_avg = vals.get("recent") or 0
        earlier_avg = vals.get("earlier")
        delta = recent_avg - earlier_avg if earlier_avg is not None else None
        name_row = conn.execute("SELECT COALESCE(display_name, current_name) AS name FROM players WHERE player_tag = ?", (tag,)).fetchone()
        item = {
            "tag": tag,
            "name": name_row["name"] if name_row else None,
            "recent_avg_fame": round(recent_avg, 0),
            "earlier_avg_fame": round(earlier_avg, 0) if earlier_avg is not None else None,
            "fame_trend": round(delta, 0) if delta is not None else None,
        }
        out.append(_member_reference_fields(conn, tag, item))
    out.sort(key=lambda i: -(i.get("fame_trend") if i.get("fame_trend") is not None else i.get("recent_avg_fame") or 0))
    return {"season_id": season_id, "recent_races": recent_races, "members": out[:limit]}


@managed_connection
def get_war_champ_standings(season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """THE standings query (schema.md §7.4): cumulative season fame per player
    over war_participation — which the engine upserts live, so the in-progress
    week is already included."""
    from storage.war_status import get_current_season_id

    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return []
    rows = conn.execute(
        "SELECT wp.player_tag AS tag, MAX(COALESCE(m.display_name, m.current_name)) AS name, "
        "SUM(COALESCE(wp.fame, 0)) AS total_fame, COUNT(*) AS races_participated, "
        "ROUND(AVG(COALESCE(wp.fame, 0)), 0) AS avg_fame, "
        "SUM(COALESCE(wp.decks_used, 0)) AS decks_used "
        "FROM war_participation wp "
        "JOIN players m ON m.player_tag = wp.player_tag "
        "WHERE wp.season_id = ? AND COALESCE(wp.fame, 0) > 0 "
        "AND EXISTS (SELECT 1 FROM clan_memberships cm "
        "  WHERE cm.player_tag = wp.player_tag AND cm.left_at IS NULL) "
        "GROUP BY wp.player_tag ORDER BY total_fame DESC, races_participated DESC",
        (season_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["tag"] = _canon_tag(item["tag"])
        item["finalized_fame"] = item["total_fame"] or 0
        item["in_progress_fame"] = 0  # participation is live-updated; split retired
        result.append(_member_reference_fields(conn, item["tag"], item))
    return result


@managed_connection
def get_perfect_war_participants(season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Members with perfect attendance: every deck used on every finalized
    battle day (war_attendance_days; Iron King definition)."""
    from storage.war_status import get_current_season_id

    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return []
    rows = conn.execute(
        "SELECT wad.player_tag AS tag, MAX(COALESCE(m.display_name, m.current_name)) AS name, "
        "COUNT(*) AS battle_days, "
        "SUM(CASE WHEN wad.decks_used >= wad.decks_available THEN 1 ELSE 0 END) AS perfect_days, "
        "SUM(COALESCE(wad.decks_used, 0)) AS decks_used "
        "FROM war_attendance_days wad "
        "JOIN players m ON m.player_tag = wad.player_tag "
        "WHERE wad.season_id = ? "
        "GROUP BY wad.player_tag "
        "HAVING battle_days > 0 AND perfect_days = battle_days "
        "ORDER BY decks_used DESC, name COLLATE NOCASE",
        (season_id,),
    ).fetchall()
    return [_member_reference_fields(conn, r["tag"], dict(r)) for r in rows]


@managed_connection
def get_recent_role_changes(days: int = 30, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """First-class role_changed events (schema.md §9: no more snapshot diffing)."""
    import json as _json
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        "SELECT ce.subject_tag AS tag, COALESCE(p.display_name, p.current_name) AS name, ce.payload_json, ce.observed_at "
        "FROM clan_events ce LEFT JOIN players p ON p.player_tag = ce.subject_tag "
        "WHERE ce.event_type = 'role_changed' AND ce.observed_at >= ? "
        "ORDER BY ce.observed_at DESC",
        (cutoff,),
    ).fetchall()
    out = []
    for row in rows:
        try:
            payload = _json.loads(row["payload_json"] or "{}")
        except ValueError:
            payload = {}
        item = {
            "tag": row["tag"],
            "name": row["name"],
            "old_role": payload.get("prev_role"),
            "new_role": payload.get("new_role"),
            "direction": payload.get("direction"),
            "observed_at": row["observed_at"],
        }
        out.append(_member_reference_fields(conn, row["tag"], item))
    return out


@managed_connection
def get_war_battle_win_rates(season_id: Optional[str] = None, limit: int = 10, min_battles: int = 1, conn: Optional[sqlite3.Connection] = None) -> dict:
    from storage.war_status import get_current_season_id

    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    where = ["b.is_war = 1", "b.outcome IN ('W', 'L')"]
    params: list = []
    if season_id is not None:
        where.append("b.season_id = ?")
        params.append(season_id)
    rows = conn.execute(
        "SELECT b.player_tag AS tag, MAX(COALESCE(m.display_name, m.current_name)) AS name, "
        "COUNT(*) AS battles, "
        "SUM(CASE WHEN b.outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN b.outcome = 'L' THEN 1 ELSE 0 END) AS losses "
        "FROM battle_events b JOIN players m ON m.player_tag = b.player_tag "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY b.player_tag HAVING battles >= ? "
        "ORDER BY (CAST(wins AS REAL) / battles) DESC, battles DESC "
        "LIMIT ?",
        (*params, min_battles, limit),
    ).fetchall()
    members = []
    for row in rows:
        item = dict(row)
        item["win_rate"] = round((row["wins"] or 0) / row["battles"], 3) if row["battles"] else 0
        members.append(_member_reference_fields(conn, row["tag"], item))
    return {"season_id": season_id, "min_battles": min_battles, "members": members}


@managed_connection
def get_clan_boat_battle_record(wars: int = 3, conn: Optional[sqlite3.Connection] = None) -> dict:
    rows = conn.execute(
        "SELECT b.outcome, COUNT(*) AS cnt FROM battle_events b "
        "WHERE b.is_war = 1 AND b.battle_type LIKE '%oat%' "
        "GROUP BY b.outcome"
    ).fetchall()
    outcomes = {row["outcome"]: row["cnt"] for row in rows}
    wins = outcomes.get("W", 0)
    losses = outcomes.get("L", 0)
    return {
        "window_wars": wars,
        "boat_battles": wins + losses,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / (wins + losses), 3) if (wins + losses) else None,
    }


@managed_connection
def get_war_score_trend(days: int = 30, conn: Optional[sqlite3.Connection] = None) -> dict:
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT metric_date, clan_war_trophies FROM clan_daily_metrics "
        "WHERE metric_date >= ? AND clan_war_trophies IS NOT NULL "
        "ORDER BY metric_date ASC",
        (cutoff,),
    ).fetchall()
    points = _rowdicts(rows)
    change = None
    if len(points) >= 2:
        change = (points[-1]["clan_war_trophies"] or 0) - (points[0]["clan_war_trophies"] or 0)
    return {"window_days": days, "points": points, "change": change}


@managed_connection
def compare_fame_per_member_to_previous_season(season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    from storage.war_status import get_current_season_id

    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return None

    def _season_stats(sid):
        row = conn.execute(
            "SELECT SUM(COALESCE(our_fame, 0)) AS total_fame, COUNT(*) AS weeks "
            "FROM war_weeks WHERE season_id = ?",
            (sid,),
        ).fetchone()
        participants = conn.execute(
            "SELECT COUNT(DISTINCT player_tag) AS cnt FROM war_participation "
            "WHERE season_id = ? AND COALESCE(fame, 0) > 0",
            (sid,),
        ).fetchone()["cnt"]
        return {
            "season_id": sid,
            "total_fame": row["total_fame"] or 0,
            "weeks": row["weeks"] or 0,
            "participants": participants,
            "fame_per_member": round((row["total_fame"] or 0) / participants, 1) if participants else 0,
        }

    current = _season_stats(int(season_id))
    previous = _season_stats(int(season_id) - 1)
    if not previous["weeks"]:
        previous = None
    return {
        "current": current,
        "previous": previous,
        "fame_per_member_change": (
            round(current["fame_per_member"] - previous["fame_per_member"], 1)
            if previous else None
        ),
    }


# -- Elder board (member_management-backed) ----------------------------------

@managed_connection
def _elder_role_review(
    min_tenure_days: int = ELDER_ELIGIBILITY_DEFAULTS["min_tenure_days"],
    active_within_days: int = ELDER_ELIGIBILITY_DEFAULTS["active_within_days"],
    min_war_races: int = ELDER_ELIGIBILITY_DEFAULTS["min_war_races"],
    enrich: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """v5.1: the review reads the management projection. in_elder_target =
    promote_state in (eligible, recommended); demotion candidates = elders
    with demote_state in (eligible, recommended)."""
    reviewed = []
    promotion_candidates = []
    demotion_candidates = []
    for row in _mgmt_rows(conn):
        item = {
            "member_id": row["tag"],  # carries the tag in v5.1
            "tag": row["tag"],
            "name": row["name"],
            "role": (row["role"] or "member") if row["role"] else "member",
            "tenure_days": row["tenure_days"],
            "promote_state": row["promote_state"],
            "demote_state": row["demote_state"],
            "sustained_donor": row["sustained_donor"],
            "war_reliable": row["war_reliable"],
            "battle_active": row["battle_active"],
            "donations_4wk_avg": row["donations_4wk_avg"],
            "war_attendance_rate": row["war_attendance_rate"],
            "battle_days_last_28": row["battle_days_last_28"],
            "in_elder_target": row["promote_state"] in ("eligible", "recommended"),
        }
        if enrich:
            item = _member_reference_fields(conn, row["tag"], item)
        reviewed.append(item)
        if item["role"] == "member" and item["in_elder_target"]:
            promotion_candidates.append(item)
        if item["role"] == "elder" and row["demote_state"] in ("eligible", "recommended"):
            demotion_candidates.append(item)
    return {
        "criteria": {
            "source": "member_management projection (management.md §3.1–3.2)",
            "min_tenure_days": min_tenure_days,
            "active_within_days": active_within_days,
            "min_war_races": min_war_races,
        },
        "composition": {
            "elders": sum(1 for i in reviewed if i["role"] == "elder"),
            "members": sum(1 for i in reviewed if i["role"] == "member"),
        },
        "reviewed": reviewed,
        "promotion_candidates": promotion_candidates,
        "demotion_candidates": demotion_candidates,
        "members": promotion_candidates,
    }


@managed_connection
def get_promotion_candidates(
    min_donations_week: int = 50,
    min_tenure_days: int = ELDER_ELIGIBILITY_DEFAULTS["min_tenure_days"],
    active_within_days: int = ELDER_ELIGIBILITY_DEFAULTS["active_within_days"],
    min_war_races: int = ELDER_ELIGIBILITY_DEFAULTS["min_war_races"],
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    del min_donations_week  # kept for caller compatibility
    return _elder_role_review(
        min_tenure_days=min_tenure_days,
        active_within_days=active_within_days,
        min_war_races=min_war_races,
        conn=conn,
    )


@managed_connection
def get_demotion_candidates(min_donations_week: int = 50, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Current elders whose demote_state has crossed eligibility."""
    del min_donations_week
    review = _elder_role_review(conn=conn)
    return {
        "criteria": review["criteria"],
        "composition": review["composition"],
        "members": review["demotion_candidates"],
    }


# -- War-deck reconstruction (battle_events join, §14.5) ----------------------

_WAR_DECK_BATTLE_TYPES = {"riverRacePvP", "riverRaceDuel", "riverRaceDuelColosseum"}


def _deck_card_summary(cards: list[dict]) -> list[dict]:
    """Strip a card list down to display-relevant fields."""
    summary = []
    for card in cards:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        summary.append({
            "name": card["name"],
            "level": card.get("level"),
            "max_level": card.get("maxLevel"),
            "elixir_cost": card.get("elixirCost"),
            "rarity": card.get("rarity"),
            "evolution_level": card.get("evolutionLevel"),
            "icon_url": (card.get("iconUrls") or {}).get("medium") if isinstance(card.get("iconUrls"), dict) else None,
        })
    return summary


def _extract_deck_candidates(rows: list[sqlite3.Row]) -> list[dict]:
    """Walk war battle rows and yield candidate decks (one per duel round, one per riverRacePvP).

    Returns a list of dicts with: cards (list), key (frozenset of names), battle_time, source.
    """
    candidates = []
    for row in rows:
        battle_type = row["battle_type"]
        battle_time = row["battle_time"]
        if battle_type in ("riverRaceDuel", "riverRaceDuelColosseum"):
            try:
                rounds = json.loads(row["team_rounds_json"] or "[]")
            except (TypeError, ValueError):
                rounds = []
            for idx, rnd in enumerate(rounds):
                cards = rnd.get("cards") if isinstance(rnd, dict) else None
                if not isinstance(cards, list) or len(cards) != 8:
                    continue
                names = [c.get("name") for c in cards if isinstance(c, dict) and c.get("name")]
                if len(names) != 8 or len(set(names)) != 8:
                    continue
                candidates.append({
                    "cards": cards,
                    "key": frozenset(names),
                    "battle_time": battle_time,
                    "source": f"{battle_type}#round{idx + 1}",
                })
        elif battle_type == "riverRacePvP":
            try:
                cards = json.loads(row["deck_json"] or "[]")
            except (TypeError, ValueError):
                cards = []
            if len(cards) != 8:
                continue
            names = [c.get("name") for c in cards if isinstance(c, dict) and c.get("name")]
            if len(names) != 8 or len(set(names)) != 8:
                continue
            candidates.append({
                "cards": cards,
                "key": frozenset(names),
                "battle_time": battle_time,
                "source": "riverRacePvP",
            })
    return candidates


def _group_candidates(candidates: list[dict]) -> list[dict]:
    """Group candidates by exact deck composition. Returns list sorted by recency then frequency."""
    grouped: dict[frozenset, dict] = {}
    for cand in candidates:
        bucket = grouped.get(cand["key"])
        if bucket is None:
            grouped[cand["key"]] = {
                "key": cand["key"],
                "cards": cand["cards"],
                "occurrences": 1,
                "latest_battle_time": cand["battle_time"],
                "earliest_battle_time": cand["battle_time"],
                "sources": [cand["source"]],
            }
        else:
            bucket["occurrences"] += 1
            if cand["battle_time"] and (not bucket["latest_battle_time"] or cand["battle_time"] > bucket["latest_battle_time"]):
                bucket["latest_battle_time"] = cand["battle_time"]
                bucket["cards"] = cand["cards"]  # keep most-recent card-level data
            if cand["battle_time"] and (not bucket["earliest_battle_time"] or cand["battle_time"] < bucket["earliest_battle_time"]):
                bucket["earliest_battle_time"] = cand["battle_time"]
            if cand["source"] not in bucket["sources"]:
                bucket["sources"].append(cand["source"])
    return sorted(
        grouped.values(),
        key=lambda d: (d["latest_battle_time"] or "", d["occurrences"]),
        reverse=True,
    )


def _select_war_decks(distinct_decks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Greedy partition: pick up to 4 non-overlapping decks; return (selected, skipped_due_to_overlap)."""
    selected: list[dict] = []
    skipped: list[dict] = []
    used_cards: set[str] = set()
    for deck in distinct_decks:
        if len(selected) >= 4:
            break
        if used_cards.isdisjoint(deck["key"]):
            selected.append(deck)
            used_cards |= deck["key"]
        else:
            skipped.append(deck)
    return selected, skipped


def _war_decks_confidence(
    selected: list[dict],
    skipped: list[dict],
    war_battles_seen: int,
    rows: list[sqlite3.Row],
) -> str:
    """Classify confidence: high / medium / low."""
    if len(selected) < 4:
        # Confidence rules only matter if we're returning decks at all.
        return "low" if skipped else "medium"
    # Look for a recent (top-3) duel that contributed >= 3 selected decks.
    recent_duels = [r for r in rows[:3] if r["battle_type"] in ("riverRaceDuel", "riverRaceDuelColosseum")]
    selected_keys = {d["key"] for d in selected}
    for duel in recent_duels:
        try:
            rounds = json.loads(duel["team_rounds_json"] or "[]")
        except (TypeError, ValueError):
            continue
        round_keys = []
        for rnd in rounds:
            cards = rnd.get("cards") if isinstance(rnd, dict) else None
            if not isinstance(cards, list) or len(cards) != 8:
                continue
            names = [c.get("name") for c in cards if isinstance(c, dict) and c.get("name")]
            if len(names) == 8 and len(set(names)) == 8:
                round_keys.append(frozenset(names))
        matched = sum(1 for k in round_keys if k in selected_keys)
        if matched >= 3 and not skipped:
            return "high"
    if skipped:
        return "low" if len(skipped) >= len(selected) else "medium"
    return "medium"


@managed_connection


@managed_connection
def reconstruct_member_war_decks(
    tag: str,
    lookback_battles: int = 80,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Reconstruct a player's four war decks from battle_events war rows
    (deck_json — a join, not raw_json inference; schema.md §9)."""
    member_tag = _canon_tag(tag)
    member_row = conn.execute(
        "SELECT player_tag, COALESCE(display_name, current_name) AS name FROM players WHERE player_tag = ?",
        (member_tag,),
    ).fetchone()
    if not member_row:
        return {
            "status": "insufficient_data",
            "member_tag": member_tag,
            "reason": "Member not found in roster.",
            "decks": [],
            "evidence": {"war_battles_seen": 0, "distinct_decks_observed": 0},
            "guidance": "Resolve the member tag first or ask the user to confirm who they meant.",
        }
    placeholders = ",".join("?" for _ in _WAR_DECK_BATTLE_TYPES)
    rows = conn.execute(
        f"SELECT battle_time, battle_type, deck_json, NULL AS team_rounds_json, deck_selection "
        f"FROM battle_events "
        f"WHERE player_tag = ? AND is_war = 1 AND battle_type IN ({placeholders}) "
        f"ORDER BY battle_time DESC LIMIT ?",
        (member_tag, *_WAR_DECK_BATTLE_TYPES, lookback_battles),
    ).fetchall()
    war_battles_seen = len(rows)
    candidates = _extract_deck_candidates(rows)
    distinct_decks = _group_candidates(candidates)

    base_payload = {
        "member_tag": member_tag,
        "member_name": member_row["name"],
        "evidence": {
            "war_battles_seen": war_battles_seen,
            "distinct_decks_observed": len(distinct_decks),
            "candidate_decks_extracted": len(candidates),
            "duel_battles_seen": sum(1 for r in rows if r["battle_type"] in ("riverRaceDuel", "riverRaceDuelColosseum")),
        },
    }
    if len(distinct_decks) < 2:
        return {
            **base_payload,
            "status": "insufficient_data",
            "reason": (
                f"Only {len(distinct_decks)} distinct war deck(s) observed across "
                f"{war_battles_seen} recent war battle(s)."
            ),
            "decks": [
                {
                    "deck_index": i + 1,
                    "cards": _deck_card_summary(deck["cards"]),
                    "occurrences": deck["occurrences"],
                    "latest_used_at": deck["latest_battle_time"],
                    "sources": deck["sources"],
                }
                for i, deck in enumerate(distinct_decks)
            ],
            "guidance": (
                "Do not present a half-built reconstruction. Tell the user there isn't "
                "enough recent war battle data, and offer to either build them four war "
                "decks from their card collection (suggest mode) or ask them to paste "
                "the four decks manually."
            ),
        }

    selected, skipped = _select_war_decks(distinct_decks)
    confidence = _war_decks_confidence(selected, skipped, war_battles_seen, rows)
    decks_payload = [
        {
            "deck_index": i + 1,
            "cards": _deck_card_summary(deck["cards"]),
            "occurrences": deck["occurrences"],
            "latest_used_at": deck["latest_battle_time"],
            "earliest_used_at": deck["earliest_battle_time"],
            "sources": deck["sources"],
        }
        for i, deck in enumerate(selected)
    ]
    gaps: list[str] = []
    if len(selected) < 4:
        gaps.append(
            f"Only {len(selected)} of 4 war decks could be reconstructed from {war_battles_seen} "
            f"recent war battle(s). Ask the user to confirm or fill in the missing deck(s)."
        )
    if skipped:
        gaps.append(
            f"{len(skipped)} candidate deck(s) were skipped because they shared cards with already-"
            "selected decks. This often means the player has changed their war decks recently — "
            "ask the user to confirm the reconstruction is current."
        )

    status = "reconstructed" if len(selected) == 4 else "partial"
    return {
        **base_payload,
        "status": status,
        "confidence": confidence,
        "decks": decks_payload,
        "skipped_candidates": [
            {
                "cards": [c.get("name") for c in deck["cards"] if isinstance(c, dict)],
                "occurrences": deck["occurrences"],
                "latest_used_at": deck["latest_battle_time"],
                "sources": deck["sources"],
            }
            for deck in skipped[:5]
        ],
        "gaps": gaps,
        "guidance": (
            "If status is 'reconstructed' with confidence='high', proceed straight to per-deck "
            "review. Otherwise present the reconstructed decks to the user and ask them to "
            "confirm or correct before reviewing. Always enforce the no-overlap rule when "
            "suggesting swaps: a card moved into one deck must come out of wherever it currently "
            "lives across the other three."
        ),
    }
