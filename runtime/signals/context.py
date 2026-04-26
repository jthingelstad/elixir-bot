"""Context assembly helpers for signal delivery."""

from __future__ import annotations

import json
import logging

import db
from runtime.channel_subagents import CLAN_RECORD_SIGNAL_TYPES

log = logging.getLogger("elixir")


def _extract_race_standings_summary(war):
    clans = (war or {}).get("clans") or []
    if not clans:
        return []
    our_tag = None
    clan_obj = (war or {}).get("clan") or {}
    if clan_obj.get("tag"):
        our_tag = clan_obj["tag"].strip("#").upper()
    ranked = sorted(
        clans,
        key=lambda c: (c.get("fame") or 0, c.get("repairPoints") or 0),
        reverse=True,
    )
    our_fame = None
    for c in ranked:
        tag = (c.get("tag") or "").strip("#").upper()
        if our_tag and tag == our_tag:
            our_fame = c.get("fame") or 0
            break
    lines = []
    for rank, c in enumerate(ranked, start=1):
        tag = (c.get("tag") or "").strip("#").upper()
        is_us = our_tag and tag == our_tag
        fame = c.get("fame") or 0
        name = c.get("name") or "Unknown"
        gap = ""
        if our_fame is not None and not is_us:
            diff = fame - our_fame
            gap = f" ({diff:+,} vs us)" if diff != 0 else " (tied)"
        marker = " \u2190 POAP KINGS" if is_us else ""
        lines.append(f"  #{rank} {name}: {fame:,} fame{gap}{marker}")
    return lines


def _build_compact_war_context(war):
    war = war or {}
    lines = []
    state = war.get("state")
    if state:
        lines.append(f"war_state: {state}")
    clan = war.get("clan") or {}
    if clan:
        lines.append(
            f"our_clan: {clan.get('name')} | fame {clan.get('fame', 0):,} | "
            f"repair {clan.get('repairPoints', 0):,} | score {clan.get('clanScore', 0):,}"
        )
        finish_time = clan.get("finishTime")
        if finish_time:
            lines.append(f"finish_time: {finish_time}")
        participants = clan.get("participants") or []
        if participants:
            lines.append(f"our_participant_count: {len(participants)}")
    period_logs = war.get("periodLogs") or []
    if period_logs:
        lines.append(f"period_logs_available: {len(period_logs)} week(s)")
    if not lines:
        lines.append("(no war data available)")
    return lines


def _build_river_race_insight_layer(signals):
    lines = []
    for sig in (signals or []):
        if sig.get("lead_pressure"):
            lines.append(f"lead_pressure: {sig['lead_pressure']}")
        if sig.get("lead_story"):
            lines.append(f"lead_story: {sig['lead_story']}")
        if sig.get("lead_call_to_action"):
            lines.append(f"lead_call_to_action: {sig['lead_call_to_action']}")
        if sig.get("gained_ground"):
            lines.append(f"rank_movement: gained ground (was #{sig.get('previous_rank')} -> now #{sig.get('race_rank')})")
        elif sig.get("lost_ground"):
            lines.append(f"rank_movement: lost ground (was #{sig.get('previous_rank')} -> now #{sig.get('race_rank')})")
        elif sig.get("race_rank") is not None and "gained_ground" not in sig:
            lines.append(f"rank_movement: holding at #{sig['race_rank']}")
        if sig.get("engagement_pct") is not None:
            lines.append(
                f"engagement: {sig['completion_pct']}% finished all decks, "
                f"{sig['engagement_pct']}% have battled, "
                f"{100 - sig['engagement_pct']}% untouched"
            )
        elif sig.get("total_participants"):
            total = sig["total_participants"]
            finished = sig.get("finished_count") or 0
            engaged = sig.get("engaged_count") or 0
            lines.append(
                f"engagement: {round(100 * finished / max(1, total))}% finished all decks, "
                f"{round(100 * engaged / max(1, total))}% have battled, "
                f"{round(100 * (total - engaged) / max(1, total))}% untouched"
            )
        if sig.get("pace_status"):
            fame_target = sig.get("fame_target") or "10,000"
            lines.append(f"pace_status: {sig['pace_status']} (finish line: {fame_target:,} fame)" if isinstance(fame_target, int) else f"pace_status: {sig['pace_status']}")
        hours_remaining = sig.get("hours_remaining") or sig.get("checkpoint_hours_remaining")
        if hours_remaining is not None:
            lines.append(f"hours_remaining: {hours_remaining}")
        if sig.get("trophy_stakes_text"):
            lines.append(f"trophy_stakes: {sig['trophy_stakes_text']}")
    seen = set()
    unique = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return unique


def _build_player_insight_context(tag):
    lines = []
    try:
        form = db.get_member_recent_form(tag)
        if form:
            parts = [f"recent_form: {form.get('form_label', 'unknown')}"]
            if form.get("summary"):
                parts.append(f"({form['summary']})")
            lines.append(" ".join(parts))
            if form.get("current_streak") and form.get("current_streak_type"):
                lines.append(f"current_streak: {form['current_streak']}{form['current_streak_type']}")
    except Exception:
        log.warning("compare_member_form failed for %s", tag, exc_info=True)
    try:
        trend = db.compare_member_trend_windows(tag, window_days=7)
        if trend:
            current = trend.get("current") or {}
            previous = trend.get("previous") or {}
            ct = current.get("trophies") or {}
            pt = previous.get("trophies") or {}
            if ct.get("delta") is not None:
                prev_label = f" (prior 7 days: {pt['delta']:+d})" if pt.get("delta") is not None else ""
                lines.append(f"trophy_trend_7d: {ct['delta']:+d}{prev_label}")
            ca = current.get("battle_activity") or {}
            pa = previous.get("battle_activity") or {}
            if ca.get("battles"):
                prev_label = f" (prior: {pa.get('battles', 0)})" if pa.get("battles") else ""
                lines.append(f"battles_this_week: {ca['battles']}{prev_label}")
    except Exception:
        log.warning("compare_member_trend_windows failed for %s", tag, exc_info=True)
    return lines


def _build_outcome_context(outcome, signals, clan, war):
    channel_key = outcome["target_channel_key"]
    first = (signals or [{}])[0]
    lines = [
        f"Target channel subagent: {channel_key}",
        f"Intent: {outcome['intent']}",
        "Write the final post for that destination only.",
        "Do not mention other channels or other internal outcomes from the same signal.",
        "",
        "Signals:",
        json.dumps(signals or [], indent=2, default=str),
    ]
    try:
        from heartbeat import build_situation_time
        situation_time = build_situation_time()
    except Exception:
        log.warning("build_situation_time failed", exc_info=True)
        situation_time = None
    if situation_time:
        lines.extend(["", "=== TIME / PHASE (current ambient context, use narratively) ===", json.dumps(situation_time, indent=2, default=str)])
    if channel_key == "river-race":
        lines.extend(["", "Focus on momentum, change, and what the clan cannot easily see in-game."])
        insight_lines = _build_river_race_insight_layer(signals)
        if insight_lines:
            lines.extend(["", "=== INSIGHT LAYER (lead with this) ==="] + insight_lines)
        standings_lines = _extract_race_standings_summary(war)
        if standings_lines:
            lines.extend(["", "=== RACE STANDINGS ==="] + standings_lines)
        lines.extend(["", "=== BACKGROUND DATA (for reasoning, do not restate as-is) ==="])
        lines.extend(_build_compact_war_context(war))
    elif channel_key == "player-progress":
        lines.extend(["", "Focus on the player's achievement and why it is worth celebrating."])
        tag = first.get("tag")
        if tag:
            insight_lines = _build_player_insight_context(tag)
            if insight_lines:
                lines.extend(["", "=== PLAYER CONTEXT (use to interpret the achievement) ==="] + insight_lines)
    elif channel_key == "trophy-road":
        lines.extend([
            "",
            "Focus on the *push happening right now* \u2014 non-war battle activity. Investigate before you post: when a streak names a player, "
            "use cr_api(aspect='player_battles') to see who they were beating, then cr_api(aspect='player') on a notable opponent if it sharpens the post.",
        ])
        tag = first.get("tag")
        if tag:
            insight_lines = _build_player_insight_context(tag)
            if insight_lines:
                lines.extend(["", "=== PLAYER CONTEXT (current form / streak / trend) ==="] + insight_lines)
    elif channel_key == "clan-events":
        has_likely_kick = any(s.get("likely_kicked") for s in (signals or []))
        is_clan_record = any((s.get("type") in CLAN_RECORD_SIGNAL_TYPES) for s in (signals or []))
        if has_likely_kick:
            lines.extend(["", "This member was likely removed from the clan due to inactivity.", "Keep the message brief and neutral. Do not write a warm farewell or thank them for contributions.", "A simple factual note that the member is no longer with the clan is enough."])
        elif is_clan_record:
            lines.extend(["", "This is an all-time clan record \u2014 the highest the metric has ever been since records began, not a seasonal peak.", "Do NOT call it a 'season high', 'season record', 'weekly high', or any other time-windowed framing. It is a lifetime clan high.", "Do NOT frame this as a personal achievement \u2014 the metric belongs to the clan, not any player.", "Report what the metric is, the previous record, the new record, and the date. Keep it short and celebratory."])
        else:
            lines.extend(["", "Focus on the communal clan moment and keep the tone welcoming and proud."])
    elif channel_key == "leader-lounge":
        lines.extend(["", "This is a leadership-facing factual note. Include useful operational context, not public hype."])
        tag = first.get("tag")
        if tag:
            try:
                profile = db.get_member_profile(tag)
            except Exception:
                profile = None
            if profile:
                lines.extend(["Member profile context:", json.dumps(profile, indent=2, default=str)])
    else:
        lines.extend(["", "Current clan data:", json.dumps(clan or {}, indent=2, default=str)])
    return "\n".join(lines)


def _build_system_signal_context(signal, channel_name):
    payload = signal.get("payload") or {}
    details = payload.get("details") or []
    lines = [
        "This is a standalone clan-wide system update about a new Elixir capability.",
        f"Post it for {channel_name}.",
        "Write exactly one Discord message. Do not split it into parts or a series.",
        "Write the full final Discord message yourself, including the subject line.",
        "For system updates, prefer starting with a bolded subject line as the first line.",
        "If you use a subject line, include an Elixir custom emoji in it using :emoji_name: shortcode syntax.",
        "If you use a subject line, do not restate that title again immediately after the first line.",
        "Do not mention hidden system mechanics or call it a system signal.",
        "Make it feel like a self-contained clan update from Elixir.",
        "",
        f"signal_type: {signal.get('type') or 'unknown'}",
        f"signal_key: {signal.get('signal_key') or 'unknown'}",
        f"title: {payload.get('title') or signal.get('title') or ''}",
        f"message: {payload.get('message') or signal.get('message') or ''}",
        f"audience: {payload.get('audience') or 'clan'}",
        f"capability_area: {payload.get('capability_area') or 'general'}",
    ]
    if details:
        lines.append("details:")
        lines.extend(f"- {detail}" for detail in details)
    return "\n".join(lines)
