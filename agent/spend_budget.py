"""A hard daily ceiling on model spend, and what it sheds when it bites.

The problem this solves is not "Elixir costs money" — it is that the cost is
UNBOUNDED. Measured over 14 days: a median day is $4.02 and the worst is $9.70,
and the spikes are member-driven (one day: interactive $4.15 + deck_review
$2.83). Nobody watching means nobody notices until the bill.

So this converts an open-ended risk into a known worst case. Past the ceiling
Elixir does less, deliberately and in a stated order, instead of doing
everything until the money runs out.

**Two rules it must never break.**

1. **A hard post is never budget-gated.** The floor guarantee predates this and
   outranks it: a join, a verified departure, a role change, a podium, a war
   week close MUST reach the clan. Those workflows are in `ESSENTIAL` and are
   exempt at every level. If the ceiling could silence a farewell, the ceiling
   would be a bug.
2. **The counter lives in the CLAN database.** A number that can refuse a model
   call is a decision, and decisions may not depend on `elixir-telemetry.db`,
   which is admin history and safe to delete. Same rule that moved the wake
   budget on 2026-08-05.

Shedding order, cheapest loss first. Everything not listed as ESSENTIAL is shed
at the ceiling; `DEFERRABLE` is shed earlier, at the warning line, because it is
the work a member will not miss.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from db import managed_connection

log = logging.getLogger("elixir")

SPEND_CURSOR_KEY = "budget:llm:spend_micros"

# $/1M tokens. Cache writes bill at 1.25x input, cache reads at 0.1x.
_RATES = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
_DEFAULT_RATE = (3.0, 15.0)

# Never shed. These carry the hard-post floor to members, or are the cheap
# routing that decides whether anything happens at all.
ESSENTIAL = frozenset(
    {
        "wake_response",  # the scoped responder — joins, farewells, role changes
        "wake_response_chat",  # its escalation rung
        "awareness",  # the brain, which owns every floor the responder misses
        "clan_chat_copy",  # the in-game sibling of a floor post
        "intent_router",  # Haiku, ~$5/mo, decides if a turn is needed at all
        "reception",  # onboarding replies to a real person, waiting
    }
)

# Shed first, at the warning line rather than the ceiling: scheduled colour and
# analysis nobody is waiting on. A member notices none of these are missing.
DEFERRABLE = frozenset(
    {
        "deck_review",
        "ask_elixir_daily",
        "promotion_content",
        "release_notes",
        "memory_distill",
        "leader_action_feedback",
    }
)


def daily_ceiling_usd() -> float:
    """Hard stop. 0 disables the ceiling entirely."""
    try:
        return max(0.0, float(os.getenv("ELIXIR_DAILY_SPEND_USD", "3.20")))
    except ValueError:
        log.warning("bad ELIXIR_DAILY_SPEND_USD, using 3.20")
        return 3.20


def warn_fraction() -> float:
    """Where DEFERRABLE work starts being shed, as a fraction of the ceiling."""
    try:
        return min(1.0, max(0.0, float(os.getenv("ELIXIR_SPEND_WARN_AT", "0.75"))))
    except ValueError:
        return 0.75


def call_cost_usd(
    model: str, prompt_tokens, completion_tokens, cache_creation, cache_read
) -> float:
    rate_in, rate_out = _RATES.get(model, _DEFAULT_RATE)
    return (
        (prompt_tokens or 0) * rate_in
        + (cache_creation or 0) * rate_in * 1.25
        + (cache_read or 0) * rate_in * 0.1
        + (completion_tokens or 0) * rate_out
    ) / 1_000_000


def _today(now: datetime = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


@managed_connection
def spend_today_usd(*, now: datetime = None, conn=None) -> float:
    row = conn.execute(
        "SELECT cursor_int FROM stream_cursors WHERE consumer_key = ? AND scope_key = ?",
        (SPEND_CURSOR_KEY, _today(now)),
    ).fetchone()
    return (int(row[0] or 0) if row else 0) / 1_000_000


@managed_connection
def record_spend_usd(usd: float, *, now: datetime = None, conn=None) -> None:
    """Add one call's cost to today's total. Stored as integer micro-dollars.

    Never raises into the caller: a spend counter that can fail a model call is
    worse than one that occasionally undercounts.
    """
    micros = max(0, int(round((usd or 0) * 1_000_000)))
    if not micros:
        return
    stamp = _today(now)
    conn.execute(
        "INSERT INTO stream_cursors (consumer_key, scope_key, cursor_int, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(consumer_key, scope_key) DO UPDATE SET "
        "cursor_int = COALESCE(stream_cursors.cursor_int, 0) + excluded.cursor_int, "
        "updated_at = excluded.updated_at",
        (
            SPEND_CURSOR_KEY,
            stamp,
            micros,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )


_ANNOUNCED: dict[str, str] = {}


def _announce_once(kind: str, detail: str) -> None:
    """Tell #leaders the first time the ceiling bites on a given day.

    A ceiling nobody is told about is indistinguishable from Elixir being
    broken — especially with nobody watching. Once per day per kind, so a busy
    afternoon does not turn into a notification storm.
    """
    stamp = f"{kind}:{_today()}"
    if _ANNOUNCED.get(kind) == stamp:
        return
    _ANNOUNCED[kind] = stamp
    try:
        from runtime import alerts

        alerts.schedule_spend_ceiling_notice(detail)
    except Exception:
        log.warning("spend budget: could not announce the ceiling (%s)", detail, exc_info=True)


def member_facing_message() -> str:
    """What a member sees when their request hits the ceiling.

    Honest and specific, because the generic "try again in a sec" the router
    falls back to is actively wrong here: retrying does not help until the day
    rolls, and a member who believes it will retry until they give up. It also
    says what still works, so nobody thinks Elixir is broken.
    """
    return (
        "I've hit my spending limit for the day, so I'm holding off on the "
        "bigger stuff until it resets at midnight UTC. Clan announcements and "
        "welcomes still go out as normal \u2014 ask me again tomorrow and I'll "
        "have room."
    )


def may_run(workflow: str, *, conn=None) -> tuple[bool, str]:
    """May this workflow spend right now? Returns ``(allowed, reason)``.

    Fails OPEN on any error. An unreadable counter must never be what stops
    Elixir from welcoming a new member.

    ``conn`` follows the repo convention — pass one in tests, omit in
    production, where the managed connection is opened per read.
    """
    ceiling = daily_ceiling_usd()
    if not ceiling:
        return True, ""
    if workflow in ESSENTIAL:
        return True, ""
    try:
        spent = spend_today_usd(conn=conn) if conn is not None else spend_today_usd()
    except Exception:
        log.debug("spend budget: could not read today's total", exc_info=True)
        return True, ""
    if spent >= ceiling:
        _announce_once("ceiling", f"${spent:.2f} of ${ceiling:.2f}")
        return False, f"daily spend ceiling reached (${spent:.2f} of ${ceiling:.2f})"
    if workflow in DEFERRABLE and spent >= ceiling * warn_fraction():
        return False, (
            f"deferrable work paused at ${spent:.2f} of ${ceiling:.2f} "
            f"({warn_fraction():.0%} of the daily ceiling)"
        )
    return True, ""


__all__ = [
    "DEFERRABLE",
    "ESSENTIAL",
    "call_cost_usd",
    "daily_ceiling_usd",
    "may_run",
    "record_spend_usd",
    "spend_today_usd",
]
