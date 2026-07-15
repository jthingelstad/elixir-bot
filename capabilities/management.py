"""Canonical read contract for clan-management decisions.

The engine's ``member_management`` state machines remain the sole policy
authority.  This capability never evaluates thresholds; it packages those
verdicts with the existing evidence and workflow state so every leadership
surface can explain the same decision.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import db as db_facade
from engine.management import management_read_summary

CAPABILITY_ID = "management_decisions"
CONTRACT_VERSION = 1


def _source(source):
    return source or db_facade


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


def _summary(source, *, conn=None) -> dict:
    with _connection(source, conn) as active:
        return management_read_summary(active)


def _at_risk(source, arguments: dict, *, conn=None) -> dict:
    return _invoke(
        source,
        "get_members_at_risk",
        inactivity_days=arguments.get("inactivity_days", 7),
        min_donations_week=arguments.get("min_donations_week", 20),
        require_war_participation=arguments.get("require_war_participation", False),
        min_war_races=arguments.get("min_war_races", 1),
        season_id=arguments.get("season_id"),
        conn=conn,
    )


def _board(source, *, conn=None) -> dict:
    summary = _summary(source, conn=conn)
    at_risk = _at_risk(source, {"min_donations_week": 0}, conn=conn)
    promotion = _invoke(source, "get_promotion_candidates", conn=conn)
    demotion = _invoke(source, "get_demotion_candidates", conn=conn)
    open_actions = _invoke(
        source, "list_leader_actions", status="proposed", limit=25, conn=conn
    )
    recent_actions = _invoke(source, "list_leader_actions", limit=15, conn=conn)
    cases = _invoke(
        source,
        "decision_case_snapshot",
        open_limit=25,
        due_limit=25,
        dedupe=True,
        conn=conn,
    )
    revisits = _invoke(source, "list_pending_revisits", limit=25, conn=conn)
    return {
        "verdicts": {
            "summary": summary,
            "kick": at_risk,
            "promote": promotion,
            "demote": demotion,
        },
        "workflow": {
            "open_actions": open_actions,
            "recent_actions": recent_actions,
            "decision_cases": cases,
            "pending_revisits": revisits,
        },
    }


def get_management_decisions(
    *, view: str = "summary", arguments: dict | None = None, source=None, conn=None
) -> dict:
    """Return an authoritative leadership-only management view."""
    source = _source(source)
    arguments = dict(arguments or {})
    if view == "summary":
        data = _summary(source, conn=conn)
    elif view == "at_risk":
        data = _at_risk(source, arguments, conn=conn)
    elif view == "promotion_candidates":
        data = _invoke(source, "get_promotion_candidates", conn=conn)
    elif view == "demotion_candidates":
        data = _invoke(source, "get_demotion_candidates", conn=conn)
    elif view == "board":
        data = _board(source, conn=conn)
    else:
        data = {"error": f"Unknown management view: {view}"}
    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "view": view,
        "audience": "leadership",
        "policy": {
            "authority": "engine.management member_management state machines",
            "rescored": False,
        },
        "sources": [
            "member_management",
            "leader_action_recommendations",
            "decision_cases",
            "revisits",
        ],
        "data": data,
    }


__all__ = ["CAPABILITY_ID", "CONTRACT_VERSION", "get_management_decisions"]
