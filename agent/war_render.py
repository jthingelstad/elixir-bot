"""Agent-layer rendering of the "war now" moment into a human/LLM-facing text
block. This lives in the agent (compose) layer, NOT storage: the storage layer
(storage.war_status.build_war_now_context) returns structured data only, and
this module turns that data into the Discord-ready prose the brain reads via the
get_river_race tool and the war-status prompt context. Keeping the rendering
here means a copy change never touches the data/signal layer."""

from __future__ import annotations


def _coerce_int(value) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return 0


def render_war_now(data: dict) -> str:
    """Render the structured 'war now' data into the two-scoreboard text block.

    Two scoreboards, each single-field so they can never be conflated: today's
    period-point race (the live action) and the weekly fame race (the boat / who
    wins the week). In Colosseum there is no weekly fame — only the period-point
    race matters."""
    parts = [f"Season {data['season_id']} · Week {data['week']}"]
    phase_with_total = data.get("phase_display")
    day_number = data.get("day_number")
    day_total = data.get("day_total")
    if phase_with_total and day_total:
        phase_with_total = f"{phase_with_total} of {day_total}"
        if day_number is not None:
            after_today = max(0, day_total - day_number)
            phase_word = "battle" if data.get("phase") == "battle" else "practice"
            if after_today > 0:
                more = "day" if after_today == 1 else "days"
                phase_with_total += f" (today + {after_today} more {phase_word} {more})"
    if phase_with_total:
        parts.append(phase_with_total)
    if data.get("is_colosseum_week"):
        parts.append("Colosseum (final week, 100 trophy stakes)")
    if data.get("is_final_battle_day"):
        parts.append("Final battle day")
    elif data.get("is_final_practice_day"):
        parts.append("Final practice day")

    lines = ["=== RIVER RACE — CURRENT MOMENT ===", " · ".join(parts)]
    if data.get("is_colosseum_week"):
        lines.append(
            "Canonical Colosseum rule: no finish line; every battle across all "
            "four battle days continues to count toward clan and member standings."
        )
    if data.get("time_left_text"):
        lines.append(f"Period ends in {data['time_left_text']}")

    colosseum = bool(data.get("is_colosseum_week"))
    day_standings = data.get("day_standings") or []
    if day_standings and data.get("phase") == "battle" and data.get("day_scored"):
        lines.append("Today's period points (resets at day reset):")
        for clan in day_standings:
            marker = " (us)" if clan.get("is_us") else ""
            lines.append(
                f"  {clan['rank']}. {clan.get('clan_name', '?')}{marker} | "
                f"{_coerce_int(clan.get('period_points')):,} points"
            )
    race_standings = data.get("race_standings") or []
    if race_standings and not colosseum:
        label = "Weekly fame race (the boat — decides the week):"
        if not data.get("boat_scored"):
            label = "Weekly fame race (boat has not scored yet — awarded at day close):"
        lines.append(label)
        for clan in race_standings:
            marker = " (us)" if clan.get("is_us") else ""
            lines.append(
                f"  {clan['rank']}. {clan.get('clan_name', '?')}{marker} | "
                f"{_coerce_int(clan.get('fame')):,} fame"
            )
    return "\n".join(lines)


__all__ = ["render_war_now"]
