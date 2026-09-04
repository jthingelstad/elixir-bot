"""Canonical local intelligence for one Clash Royale member.

The capability is deliberately read-only.  Callers that need a fresh Clash
Royale profile or battle log refresh those external sources first, then ask
this module for one coherent view over Elixir's local projections and history.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

import db as db_facade
from capabilities.contracts import MemberIntelligenceResult
from engine import profiles as profile_engine

log = logging.getLogger("elixir.capabilities.members")

CAPABILITY_ID = "member_intelligence"
CONTRACT_VERSION = 1

DEFAULT_FACETS = ("profile", "form", "playstyle")


def _source(source):
    return source or db_facade


def _tag(value: str) -> str:
    clean = str(value or "").strip().upper()
    return clean if clean.startswith("#") else f"#{clean}"


def _invoke(source, name: str, *args, conn=None, **kwargs):
    if conn is not None:
        kwargs["conn"] = conn
    return getattr(source, name)(*args, **kwargs)


@contextmanager
def _connection(source, conn=None) -> Iterator[Any]:
    if conn is not None:
        yield conn
        return
    opened = source.get_connection()
    try:
        yield opened
    finally:
        opened.close()


def _playstyle(source, player_tag: str, days: int, *, conn=None) -> dict | None:
    try:
        with _connection(source, conn) as active:
            value = profile_engine.player_mode_profile(active, player_tag, days=days)
        return value if isinstance(value, dict) else None
    except Exception:
        log.warning(
            "playstyle profile failed for %s — omitted from the answer", player_tag, exc_info=True
        )
        return None


def _freshness(result: Mapping[str, Any]) -> dict:
    profile = result.get("profile") or {}
    form = result.get("form") or {}
    war = result.get("war") or {}
    battles = result.get("battles") or []
    if isinstance(battles, dict):
        battles = battles.get("battles") or battles.get("results") or []
    latest_battle = battles[0] if isinstance(battles, list) and battles else {}
    return {
        "profile_at": profile.get("player_profile_at") or profile.get("observed_at"),
        "form_at": form.get("computed_at"),
        "war_at": war.get("observed_at"),
        "latest_battle_at": (
            latest_battle.get("battle_time") or latest_battle.get("observed_at")
            if isinstance(latest_battle, dict)
            else None
        ),
    }


def get_member_intelligence(
    player_tag: str,
    *,
    facets: Iterable[str] | None = None,
    days: int = 30,
    scope: str = "competitive_10",
    battles_scope: str = "overall_10",
    battles_limit: int = 10,
    losses_limit: int = 30,
    event_limit: int = 50,
    source=None,
    conn=None,
) -> MemberIntelligenceResult:
    """Return selected facets under one stable member contract."""
    if conn is None and source in (None, db_facade):
        active_source = source or db_facade
        with _connection(active_source) as active:
            active.execute("BEGIN")
            return get_member_intelligence(
                player_tag,
                facets=facets,
                days=days,
                scope=scope,
                battles_scope=battles_scope,
                battles_limit=battles_limit,
                losses_limit=losses_limit,
                event_limit=event_limit,
                source=active_source,
                conn=active,
            )
    source = _source(source)
    tag = _tag(player_tag)
    requested = tuple(dict.fromkeys(facets or DEFAULT_FACETS))
    days = max(1, int(days or 30))
    result: MemberIntelligenceResult = {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "player_tag": tag,
        "requested_facets": list(requested),
        "sources": [
            "players",
            "player_current_state",
            "battle_events",
            "player_events",
            "clan_memberships",
        ],
        "data_generation": None,
    }
    if conn is not None:
        from engine.readiness import generation_snapshot

        result["data_generation"] = generation_snapshot(conn)

    if "profile" in requested:
        result["profile"] = _invoke(source, "get_member_profile", tag, conn=conn)
    if "form" in requested:
        result["form"] = _invoke(source, "get_member_recent_form", tag, scope=scope, conn=conn)
    if "playstyle" in requested:
        result["playstyle"] = _playstyle(source, tag, days, conn=conn)
    if "war" in requested:
        result["war"] = _invoke(source, "get_member_war_status", tag, season_id=None, conn=conn)
    if "trend" in requested:
        # Phase 1 (Jamie, 2026-09-04): trend goes DIRECTLY to Elixir MCP;
        # local tables are the error fallback only.
        from capabilities import mcp_stats

        result["trend"] = mcp_stats.trend_context_via_mcp(
            tag, days=days, window_days=min(days // 4, 7) or 7
        ) or _invoke(
            source,
            "build_member_trend_summary_context",
            tag,
            days=days,
            window_days=min(days // 4, 7) or 7,
            conn=conn,
        )
    if "loadout" in requested:
        result["loadout"] = {
            "current_deck": _invoke(source, "get_member_current_deck", tag, conn=conn),
            "signature_cards": _invoke(
                source,
                "get_member_signature_cards",
                tag,
                mode_scope="overall",
                conn=conn,
            ),
        }
    if "losses" in requested:
        result["losses"] = _invoke(
            source,
            "get_member_recent_losses",
            tag,
            scope=scope,
            limit=losses_limit,
            conn=conn,
        )
    if "wins" in requested:
        result["wins"] = _invoke(
            source,
            "get_member_recent_wins",
            tag,
            scope=scope,
            limit=losses_limit,
            conn=conn,
        )
    if "battles" in requested:
        result["battles"] = _invoke(
            source,
            "get_member_recent_battles",
            tag,
            scope=battles_scope,
            limit=battles_limit,
            conn=conn,
        )
    if "history" in requested:
        result["history"] = _invoke(source, "get_member_history", tag, days=days, conn=conn)
    if "ranked" in requested:
        result["ranked"] = _invoke(source, "get_member_ranked_status", tag, days=days, conn=conn)
    if "mode_activity" in requested:
        result["mode_activity"] = _invoke(
            source, "get_member_mode_activity", tag, days=days, conn=conn
        )
    if "events" in requested:
        result["events"] = _invoke(
            source,
            "list_recent_events",
            days=days,
            subject_key=tag,
            limit=event_limit,
            conn=conn,
        )
    if "awards" in requested:
        from capabilities.awards import get_awards_recognition

        result["awards"] = get_awards_recognition(
            view="member", member_tag=tag, source=source, conn=conn
        )["data"]

    result["freshness"] = _freshness(result)
    return result


__all__ = [
    "CAPABILITY_ID",
    "CONTRACT_VERSION",
    "DEFAULT_FACETS",
    "get_member_intelligence",
]
