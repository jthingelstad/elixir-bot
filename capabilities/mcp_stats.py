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

A field that is absent or null is UNAVAILABLE, never 0 (Jamie,
2026-09-25). The hub removes an unreliable response field as a patch, not
a major (elixir_mcp.py explains why there is no version pin), so these
readers are where a wire change is caught: a count the answer depends on
makes the builder return None and the caller falls back, and a field that
is only passed through stays None rather than becoming 0.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import elixir_mcp

log = logging.getLogger("elixir.mcp_stats")


def _iso_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _absent(row: dict, *fields: str) -> bool:
    """True when any field is missing or null: unavailable, not zero."""
    return any(row.get(field) is None for field in fields)


def _missing_war_field(weeks: list[dict], member_weeks: list[dict] | None) -> str | None:
    """Name the first field war_attendance_via_mcp counts that is absent or
    null, or None when every one is present. Only rows the counts read are
    checked: the current season's member weeks and the latest four."""
    if member_weeks is None:
        return "member_weeks"
    key = ("season_id", "section_index")
    if any(_absent(w, *key) for w in weeks):
        return "weeks[].season_id/section_index"
    if any(_absent(m, *key) for m in member_weeks):
        return "member_weeks[].season_id/section_index"
    season_id = max(w["season_id"] for w in weeks)
    last4_keys = {(w["season_id"], w["section_index"]) for w in weeks[:4]}
    counted = [
        m
        for m in member_weeks
        if m["season_id"] == season_id or (m["season_id"], m["section_index"]) in last4_keys
    ]
    if any(_absent(m, "decks_used") for m in counted):
        return "member_weeks[].decks_used"
    played = [m for m in counted if m["season_id"] == season_id and m["decks_used"] > 0]
    if any(_absent(m, "points") for m in played):
        return "member_weeks[].points"
    return None


def trend_context_via_mcp(tag: str, days: int = 30, window_days: int = 7) -> str | None:
    """Preformatted MEMBER TREND SUMMARY block from Elixir MCP data.

    Same labels as storage.trends.build_member_trend_summary_context,
    including the hard-won separation of snapshot trophy deltas from
    battle_trophy_delta (trophies actually won/lost in battles).
    """
    now = datetime.now(timezone.utc)
    # The series is `series`, one point per game day keyed `day` (4.0.0:
    # date is gone); `metrics` names the columns (trophies is the default,
    # best_trophies is the profile's lifetime best on the same row).
    timeline = elixir_mcp.call_tool(
        "players_timeline",
        {
            "player_tag": tag,
            "from": _iso_date(now - timedelta(days=days)),
            "metrics": ["trophies", "best_trophies"],
        },
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
    points = timeline.get("series") or []
    latest = points[-1] if points else {}

    def _day(p: dict) -> str | None:
        return p.get("day")

    def _snapshot_delta(start: datetime, end: datetime) -> int | None:
        lo, hi = _iso_date(start), _iso_date(end)
        window = [p for p in points if (d := _day(p)) is not None and lo <= d <= hi]
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
        # An absent draws count prints as None like wins and losses do,
        # never as a 0 the hub did not send.
        return f"{seg.get('wins')}-{seg.get('losses')}-{seg.get('draws')}"

    lines = [
        "=== MEMBER TREND SUMMARY ===",
        f"member: {tag}",
        f"player_tag: {tag}",
        f"window_days: {days}",
        (
            f"latest_snapshot: {_day(latest) or 'n/a'} | "
            f"trophies {latest.get('trophies')} | best_trophies {latest.get('best_trophies', 'n/a')}"
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

    Every field the counts read must be present: a member week without
    decks_used, a played week without points, or no member_weeks at all
    returns None (the caller answers from local tables). Read as 0, a
    withdrawn decks_used would have turned every member into one who
    played no war decks. An EMPTY member_weeks list is real: no recorded
    race rows for the member, so zero races played.
    """
    body = elixir_mcp.call_tool("war_history", {"player_tag": tag, "seasons": 2})
    if body is None:
        return None
    weeks = body.get("weeks") or []
    if not weeks:
        return None
    member_weeks = body.get("member_weeks")
    missing = _missing_war_field(weeks, member_weeks)
    if missing or member_weeks is None:
        # A warning, not a line: a field the hub stops sending is how its
        # wire change reaches this bot now that no version pin stands in.
        log.warning(
            "elixir-mcp: war_history %s absent or null for %s; unavailable, answering from local tables",
            missing,
            tag,
        )
        return None
    season_id = max(w["season_id"] for w in weeks)
    season_weeks = [w for w in weeks if w["season_id"] == season_id]
    played_rows = [m for m in member_weeks if m["season_id"] == season_id and m["decks_used"] > 0]
    total_races = len(season_weeks)
    races_played = len(played_rows)
    # Latest four recorded weeks across seasons (weeks arrive newest-first).
    last4 = weeks[:4]
    last4_keys = {(w["season_id"], w["section_index"]) for w in last4}
    recent_played = sum(
        1
        for m in member_weeks
        if (m["season_id"], m["section_index"]) in last4_keys and m["decks_used"] > 0
    )
    return {
        "season_id": season_id,
        "tag": tag,
        "season": {
            "races_played": races_played,
            "total_races": total_races,
            "participation_rate": round(races_played / total_races, 4) if total_races else 0,
            "total_points": sum(m["points"] for m in played_rows),
            "total_decks_used": sum(m["decks_used"] for m in played_rows),
            "races_missed": max(0, total_races - races_played),
        },
        "last_4_weeks": {
            "races_played": recent_played,
            "total_races": len(last4),
            "participation_rate": round(recent_played / len(last4), 4) if last4 else 0,
        },
        "source": "elixir-mcp",
        # 1.0.0: caveats are `notes: string[]` (one sentence each), not `note`.
        "notes": list(body.get("notes") or []),
    }


def clan_standing_via_mcp(
    member_tag: str | None = None, days: int = 30, min_battles: int = 20
) -> dict | None:
    """Ranked clan win-rate standings; marks the asking member if given."""
    body = elixir_mcp.call_tool("clans_standings", {"days": days, "min_battles": min_battles})
    if body is None:
        return None
    members = body.get("members") or []
    # Absent reads as None (unavailable), not 0 ranked members.
    ranked_members = body.get("ranked_members")
    below_floor = body.get("below_floor")
    mine = None
    if member_tag:
        mine = next((m for m in members if m.get("player_tag") == member_tag), None)
        if mine and ranked_members and mine.get("rank") is not None:
            mine = dict(mine)
            mine["percentile"] = round(1 - (mine["rank"] - 1) / ranked_members, 3)
    # 1.0.0: the one echo block. `applied.days` echoes the sugar we sent;
    # `applied.window` carries the resolved bounds and whether they were
    # given or defaulted. The old `basis` paragraph is now a line in `notes`.
    applied = body.get("applied") or {}
    window = applied.get("window") or {}
    return {
        "clan_tag": body.get("clan_tag"),
        "window_days": applied.get("days") or days,
        "window": {
            "from": window.get("from"),
            "to": window.get("to"),
            "source": window.get("source"),
        },
        "median_win_rate": body.get("median_win_rate"),
        "ranked_members": ranked_members,
        "standings": members,
        "asker": mine,
        "below_floor_count": len(below_floor) if below_floor is not None else None,
        "notes": list(body.get("notes") or []),
        "source": "elixir-mcp",
    }
