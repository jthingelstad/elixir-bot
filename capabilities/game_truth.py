"""Canonical game-truth contract shared by every composition surface.

The engine owns deterministic mechanics in :mod:`engine.game_rules`.  This
capability turns those rules plus live state into a versioned, audience-neutral
evidence contract.  LLMs may choose wording; they may not change this truth.
"""

from __future__ import annotations

from typing import Iterable

from capabilities.contracts import GameTruthResult
from engine.game_rules import river_race_mechanics

CAPABILITY_ID = "game_truth"
CONTRACT_VERSION = 1


def _live_period_type(live_war: dict | None) -> str | None:
    live = live_war or {}
    period = live.get("period") if isinstance(live.get("period"), dict) else {}
    state = live.get("current_state") if isinstance(live.get("current_state"), dict) else {}
    clock = live.get("clock") if isinstance(live.get("clock"), dict) else {}
    if (
        period.get("is_colosseum_week")
        or state.get("colosseum_week")
        or clock.get("is_colosseum_week")
    ):
        return "colosseum"
    return period.get("period_type") or state.get("period_type") or clock.get("period_type")


def get_game_truth(*, topic: str = "river_race", live_war: dict | None = None) -> GameTruthResult:
    """Return one versioned mechanics contract.

    Unsupported topics fail explicitly rather than inviting a caller to infer
    game rules from prompt prose.
    """
    if topic != "river_race":
        return {
            "capability": CAPABILITY_ID,
            "contract_version": CONTRACT_VERSION,
            "topic": topic,
            "available": False,
            "error": "unsupported_topic",
        }
    mechanics = river_race_mechanics(_live_period_type(live_war))
    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "topic": topic,
        "available": True,
        "sources": ["engine.game_rules", "live_war_state"],
        "mechanics": mechanics,
    }


def live_war_claim_facts(live_war: dict | None) -> dict:
    """Flatten the live contract into facts consumed by deterministic checks."""
    truth = get_game_truth(topic="river_race", live_war=live_war)
    mechanics = truth.get("mechanics") or {}
    return {
        "is_colosseum_week": bool(mechanics.get("is_colosseum_week")),
        "period_type": mechanics.get("period_type"),
        "finish_line": mechanics.get("finish_line"),
        "battle_day_total": mechanics.get("battle_days"),
        "every_battle_counts_for_standings": mechanics.get("every_battle_counts_for_standings"),
        "canonical_game_truth": truth,
    }


def _signals(read: dict) -> Iterable[dict]:
    seen: set[str] = set()
    for values in (read.get("signals_by_lane") or {}).values():
        for signal in values or []:
            if not isinstance(signal, dict):
                continue
            key = str(signal.get("signal_key") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            yield signal
    for signal in read.get("hard_post_signals") or []:
        if not isinstance(signal, dict):
            continue
        key = str(signal.get("signal_key") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        yield signal


def _war_from_awareness_read(read: dict) -> dict:
    time = read.get("time") if isinstance(read.get("time"), dict) else {}
    standing = read.get("standing") if isinstance(read.get("standing"), dict) else {}
    season = read.get("war_season") if isinstance(read.get("war_season"), dict) else {}
    race = ((season.get("state") or {}).get("race") or {}) if isinstance(season, dict) else {}
    colosseum = bool(
        time.get("is_colosseum_week")
        or race.get("colosseum_week")
        or standing.get("primary_metric") == "period_points"
    )
    return {
        "period": {
            "period_type": "colosseum" if colosseum else time.get("period_type"),
            "is_colosseum_week": colosseum,
        },
        "current_state": race,
        "clock": time,
    }


def awareness_post_facts(read: dict, post: dict) -> dict:
    """Build the exact evidence admitted for one awareness post."""
    facts = live_war_claim_facts(_war_from_awareness_read(read or {}))
    covered = set(post.get("covers_signal_keys") or [])
    covered_signals = [
        signal for signal in _signals(read or {}) if signal.get("signal_key") in covered
    ]
    for signal in covered_signals:
        payload = signal.get("payload") if isinstance(signal.get("payload"), dict) else {}
        for key, value in {**payload, **signal}.items():
            if key not in facts and value is not None:
                facts[key] = value
    facts["covered_signals"] = covered_signals
    facts["notable_moment"] = any(
        str(signal.get("badge_tier") or "").lower() == "legendary"
        or signal.get("event_type")
        in {
            "arena_changed",
            "arena_up",
            "pol_promotion",
            "path_of_legend_promotion",
            "war_week_complete",
            "war_season_complete",
        }
        or bool(signal.get("is_annual"))
        or bool(signal.get("notable"))
        for signal in covered_signals
    )
    return facts


def deterministic_correction(facts: dict) -> str:
    """Safe fallback when a repair still contradicts canonical mechanics."""
    if facts.get("is_colosseum_week"):
        return (
            "Colosseum has no finish line. Every battle across all four battle days "
            "continues to count toward the clan and member standings."
        )
    return "I could not verify that game-mechanics claim against Elixir's current data."


__all__ = [
    "CAPABILITY_ID",
    "CONTRACT_VERSION",
    "awareness_post_facts",
    "deterministic_correction",
    "get_game_truth",
    "live_war_claim_facts",
]
