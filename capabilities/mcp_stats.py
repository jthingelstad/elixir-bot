"""Member stats served from Elixir MCP (phase 1, Jamie 2026-09-04).

These builders go DIRECTLY to the sibling data service and return None
on any failure — callers fall back to the local tables, so member Q&A
survives an Elixir MCP incident. Presentation stays elixir-bot's job:
each builder emits the same shapes the local storage layer produced, so
prompts and downstream readers are unchanged.

What routes here: the trend facet (get_member include=trend), war
attendance (get_member_war_detail aspect=attendance), and the new
get_clan_standing tool. Battle-intelligence views (archetypes, adjusted
lift, closeness) stay on local enrichment tables — that's analysis, not
data plumbing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import elixir_mcp

log = logging.getLogger("elixir.mcp_stats")


def _iso_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def trend_context_via_mcp(tag: str, days: int = 30, window_days: int = 7) -> str | None:
    """Preformatted MEMBER TREND SUMMARY block from Elixir MCP data.

    Same labels as storage.trends.build_member_trend_summary_context,
    including the hard-won separation of snapshot trophy deltas from
    battle_trophy_delta (trophies actually won/lost in battles).
    """
    now = datetime.now(timezone.utc)
    timeline = elixir_mcp.call_tool(
        "players_timeline",
        {"player_tag": tag, "from": _iso_date(now - timedelta(days=days))},
    )
    perf = elixir_mcp.call_tool(
        "battles_performance",
        {
            "player_tag": tag,
            "from": _iso_date(now - timedelta(days=2 * window_days)),
            "before_after": _iso_date(now - timedelta(days=window_days)),
        },
    )
    if timeline is None or perf is None:
        return None
    points = timeline.get("points") or timeline.get("series") or []
    latest = points[-1] if points else {}

    def _snapshot_delta(start: datetime, end: datetime) -> int | None:
        window = [
            p for p in points if p.get("date") and _iso_date(start) <= p["date"] <= _iso_date(end)
        ]
        vals = [p.get("trophies") for p in window if p.get("trophies") is not None]
        if len(vals) < 2:
            return None
        return vals[-1] - vals[0]

    cur_delta = _snapshot_delta(now - timedelta(days=window_days), now)
    prev_delta = _snapshot_delta(
        now - timedelta(days=2 * window_days), now - timedelta(days=window_days)
    )
    before = perf.get("before") or {}
    after = perf.get("after") or {}

    def _rec(seg: dict) -> str:
        return f"{seg.get('wins')}-{seg.get('losses')}-{seg.get('draws', 0)}"

    lines = [
        "=== MEMBER TREND SUMMARY ===",
        f"member: {tag}",
        f"player_tag: {tag}",
        f"window_days: {days}",
        (
            f"latest_snapshot: {latest.get('date') or 'n/a'} | "
            f"trophies {latest.get('trophies')} | best_trophies n/a"
        ),
        (
            f"current_{window_days}d_vs_previous_{window_days}d: "
            f"trophies {cur_delta} vs {prev_delta} | "
            f"battles {after.get('battles')} vs {before.get('battles')} | "
            f"record {_rec(after)} vs {_rec(before)} | "
            f"battle_trophy_delta {after.get('net_trophies')} vs {before.get('net_trophies')}"
        ),
        f"daily_battle_rows: {len(points)}",
        "source: elixir-mcp (recorded battles; capture starts may postdate real history)",
    ]
    return "\n".join(lines)


def war_attendance_via_mcp(tag: str) -> dict | None:
    """Season + last-4-weeks attendance in the local shape.

    A race counts as played when decks_used > 0 — identical semantics to
    storage.war_members.get_member_war_attendance.
    """
    body = elixir_mcp.call_tool("war_history", {"player_tag": tag, "seasons": 2})
    if body is None:
        return None
    weeks = body.get("weeks") or []
    member_weeks = body.get("member_weeks") or []
    if not weeks:
        return None
    season_id = max(w["season_id"] for w in weeks)
    season_weeks = [w for w in weeks if w["season_id"] == season_id]
    played_rows = [
        m for m in member_weeks if m["season_id"] == season_id and (m.get("decks_used") or 0) > 0
    ]
    total_races = len(season_weeks)
    races_played = len(played_rows)
    # Latest four recorded weeks across seasons (weeks arrive newest-first).
    last4 = weeks[:4]
    last4_keys = {(w["season_id"], w["section_index"]) for w in last4}
    recent_played = sum(
        1
        for m in member_weeks
        if (m["season_id"], m["section_index"]) in last4_keys and (m.get("decks_used") or 0) > 0
    )
    return {
        "season_id": season_id,
        "tag": tag,
        "season": {
            "races_played": races_played,
            "total_races": total_races,
            "participation_rate": round(races_played / total_races, 4) if total_races else 0,
            "total_points": sum(m.get("points") or 0 for m in played_rows),
            "total_decks_used": sum(m.get("decks_used") or 0 for m in played_rows),
            "races_missed": max(0, total_races - races_played),
        },
        "last_4_weeks": {
            "races_played": recent_played,
            "total_races": len(last4),
            "participation_rate": round(recent_played / len(last4), 4) if last4 else 0,
        },
        "source": "elixir-mcp",
        "note": body.get("note"),
    }


def clan_standing_via_mcp(
    member_tag: str | None = None, days: int = 30, min_battles: int = 20
) -> dict | None:
    """Ranked clan win-rate standings; marks the asking member if given."""
    body = elixir_mcp.call_tool("clans_standings", {"days": days, "min_battles": min_battles})
    if body is None:
        return None
    members = body.get("members") or []
    ranked_members = body.get("ranked_members") or 0
    mine = None
    if member_tag:
        mine = next((m for m in members if m["player_tag"] == member_tag), None)
        if mine and ranked_members:
            mine = dict(mine)
            mine["percentile"] = round(1 - (mine["rank"] - 1) / ranked_members, 3)
    return {
        "clan_tag": body.get("clan_tag"),
        "window_days": body.get("window_days"),
        "basis": body.get("basis"),
        "median_win_rate": body.get("median_win_rate"),
        "ranked_members": ranked_members,
        "standings": members,
        "asker": mine,
        "below_floor_count": len(body.get("below_floor") or []),
        "note": body.get("note"),
        "source": "elixir-mcp",
    }
