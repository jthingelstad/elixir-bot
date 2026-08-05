"""The v4 regression canary: did two authors tell the same story?

Agentic Loop v2 Phase 2. There are now two composing paths — the scoped
responder and the daily brain — and the whole architecture rests on them never
covering the same moment twice. v4 died of exactly that: a deterministic
recognizer and an LLM both narrating a member's week, in different words, hours
apart.

The cursor design makes double-posting structurally hard (a responder marks its
high-water only on success, and the brain reads from the same cursors), so this
is not the guard — it is the *proof* that the guard holds. It is deliberately
cheap, deterministic, and read-only: two fulfilled intents inside the same window
must not share a signal key, and should not both be about the same member.

A signal-key overlap is a real defect and reports as ``overlap``. A same-member
report is softer — two posts about one member in a day can be perfectly correct
(a farewell and a milestone the same afternoon are different moments) — so it
reports as ``same_member`` for a human to read, never as a failure.

It also counts floor misses, because the phase's exit gate asks for exactly that
and nothing else records it: a wake that fails every tier now writes an episode
naming the signals it left uncovered.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

from db import managed_connection

log = logging.getLogger("elixir")

WINDOW_HOURS_DEFAULT = 24


@managed_connection
def check_divergence(hours: int = WINDOW_HOURS_DEFAULT, conn=None) -> dict:
    """Fulfilled intents in the window that cover the same signal or member."""
    rows = conn.execute(
        "SELECT intent_key, lane, covers_json, post_json, fulfilled_at "
        "FROM awareness_delivery_intents "
        "WHERE status = 'fulfilled' AND fulfilled_at IS NOT NULL "
        "  AND julianday(fulfilled_at) >= julianday('now') - (? / 24.0) "
        "ORDER BY fulfilled_at",
        (int(hours),),
    ).fetchall()

    by_signal: dict[str, list[str]] = defaultdict(list)
    by_member: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        key = row["intent_key"]
        for signal in _json_list(row["covers_json"]):
            by_signal[str(signal)].append(key)
        for tag in _member_tags(row["post_json"]):
            by_member[tag].append(key)

    overlaps = [
        {"signal_key": signal, "intents": sorted(set(keys))}
        for signal, keys in sorted(by_signal.items())
        if len(set(keys)) > 1
    ]
    repeats = [
        {"player_tag": tag, "intents": sorted(set(keys))}
        for tag, keys in sorted(by_member.items())
        if len(set(keys)) > 1
    ]
    return {
        "window_hours": int(hours),
        "intents_examined": len(rows),
        "overlap": overlaps,
        "same_member": repeats,
    }


def floor_misses(hours: int = WINDOW_HOURS_DEFAULT) -> dict:
    """Wakes in the window that failed every tier with a floor still uncovered.

    Reads the telemetry database, where episodes live. A miss here does not mean
    the signal went unposted — an uncovered floor triggers an out-of-band brain
    run and the cursors hold, so the moment is still owned. It means the cheap
    path did not carry it, which is the number the exit gate is about.
    """
    try:
        from storage import telemetry

        tconn = telemetry.connect()
        rows = tconn.execute(
            "SELECT recorded_at, job, reason, episode_json FROM wake_episodes "
            "WHERE handled = 0 AND julianday(recorded_at) >= julianday('now') - (? / 24.0) "
            "ORDER BY recorded_at",
            (int(hours),),
        ).fetchall()
    except Exception:
        log.debug("divergence: telemetry unavailable", exc_info=True)
        return {"window_hours": int(hours), "misses": [], "available": False}

    misses = []
    for row in rows:
        episode = _json_obj(row["episode_json"])
        uncovered = episode.get("floor") or []
        if not uncovered:
            continue
        misses.append(
            {
                "recorded_at": row["recorded_at"],
                "job": row["job"],
                "uncovered": uncovered,
                "reason": (row["reason"] or "")[:200],
            }
        )
    return {"window_hours": int(hours), "misses": misses, "available": True}


def report(hours: int = WINDOW_HOURS_DEFAULT) -> dict:
    """Both halves, plus a single ``clean`` flag a job can branch on."""
    divergence = check_divergence(hours=hours)
    floors = floor_misses(hours=hours)
    divergence["floor_misses"] = floors.get("misses") or []
    divergence["clean"] = not divergence["overlap"] and not divergence["floor_misses"]
    return divergence


def _json_list(raw) -> list:
    value = _json_obj(raw, default=[])
    return value if isinstance(value, list) else []


def _json_obj(raw, default=None):
    if not raw:
        return {} if default is None else default
    try:
        return json.loads(raw)
    except TypeError, ValueError:
        return {} if default is None else default


def _member_tags(post_json) -> list[str]:
    post = _json_obj(post_json)
    if not isinstance(post, dict):
        return []
    tags = post.get("member_tags")
    return [str(t) for t in tags if t] if isinstance(tags, list) else []


__all__ = ["check_divergence", "floor_misses", "report"]
