"""A daily ceiling for awareness and Ask Elixir model spend.

The problem this solves is not "Elixir costs money" — it is that the cost is
UNBOUNDED. Measured over 14 days: a median day is $4.02 and the worst is $9.70,
and the spikes are member-driven (one day: interactive $4.15 + deck_review
$2.83). Nobody watching means nobody notices until the bill.

So this converts the two discretionary, open-ended surfaces into a known worst
case. Scheduled reports and operational jobs are deliberately outside this
ledger: a cost control for conversation must not make promised work disappear.

**Two rules it must never break.**

1. **A hard post is never budget-gated.** The floor guarantee predates this and
   outranks it: a join, a verified departure, a role change, a podium, a war
   week close MUST reach the clan. Those workflows are in `ESSENTIAL` and are
   outside the ledger. The awareness brain is normally budgeted, but its
   explicit third-rung floor recovery runs inside `required_work()`.
2. **The counter lives in the CLAN database.** A number that can refuse a model
   call is a decision, and decisions may not depend on `elixir-telemetry.db`,
   which is admin history and safe to delete. Same rule that moved the wake
   budget on 2026-08-05.

Only workflows in `BUDGETED` are charged or gated. `DEFERRABLE` Ask Elixir work
is shed earlier, at the warning line, so direct conversation has the last room.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from agent.pricing import call_cost_usd
from db import managed_connection

log = logging.getLogger("elixir")

SPEND_CURSOR_KEY = "budget:llm:awareness_ask_elixir_micros"
# $50/month expressed as a 30-day daily ceiling. Keep the exact fraction so the
# default cannot silently exceed the approved monthly target through rounding.
DEFAULT_DAILY_CEILING_USD = 50.0 / 30.0
DEFAULT_DAILY_CEILING_ENV = str(DEFAULT_DAILY_CEILING_USD)

# This is the policy boundary. Everything absent from this set always runs and
# is not charged to the ceiling. Keep support calls that are part of these two
# surfaces here too, or their real cost leaks outside the bound.
BUDGETED = frozenset(
    {
        # Deliberative awareness (the scoped hard-post responder is essential).
        "awareness",
        "awareness_triage",
        "awareness_repair",
        # Member conversation and discovery on the Ask Elixir surface.
        "interactive",
        "help",
        "deck_review",
        "game_factual_repair",
        "ask_elixir_daily",
    }
)

# Never shed. These carry the hard-post floor to members, or are the cheap
# routing that decides whether anything happens at all.
ESSENTIAL = frozenset(
    {
        "wake_response",  # the scoped responder — joins, farewells, role changes
        "wake_response_chat",  # its escalation rung
        "clan_chat_copy",  # the in-game sibling of a floor post
        "intent_router",  # Haiku, ~$5/mo, decides if a turn is needed at all
        "reception",  # onboarding replies to a real person, waiting
    }
)

# Shed first, at the warning line rather than the ceiling: optional Ask Elixir
# depth and discovery, preserving the last room for a member's direct question.
DEFERRABLE = frozenset(
    {
        "deck_review",
        "ask_elixir_daily",
    }
)

_REQUIRED_WORK: ContextVar[bool] = ContextVar("elixir_spend_required_work", default=False)


@contextmanager
def required_work():
    """Let an explicit hard-post recovery cross the discretionary ceiling.

    The call is still charged to the awareness/Ask Elixir ledger. This is not a
    generic escape hatch: production uses it only after every scoped responder
    tier failed while a mandatory signal remained uncovered.
    """
    token = _REQUIRED_WORK.set(True)
    try:
        yield
    finally:
        _REQUIRED_WORK.reset(token)


def daily_ceiling_usd() -> float:
    """Hard stop. 0 disables the ceiling entirely."""
    try:
        return max(0.0, float(os.getenv("ELIXIR_DAILY_SPEND_USD", DEFAULT_DAILY_CEILING_ENV)))
    except ValueError:
        log.warning("bad ELIXIR_DAILY_SPEND_USD, using %.2f", DEFAULT_DAILY_CEILING_USD)
        return DEFAULT_DAILY_CEILING_USD


def warn_fraction() -> float:
    """Where DEFERRABLE work starts being shed, as a fraction of the ceiling."""
    try:
        return min(1.0, max(0.0, float(os.getenv("ELIXIR_SPEND_WARN_AT", "0.75"))))
    except ValueError:
        return 0.75


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
def _record_budgeted_spend_usd(usd: float, *, now: datetime = None, conn=None) -> None:
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


def record_spend_usd(workflow: str, usd: float, *, now: datetime = None, conn=None) -> None:
    """Charge a budgeted call to today's total as integer micro-dollars.

    Never raises into the caller: a spend counter that can fail a model call is
    worse than one that occasionally undercounts. Unbudgeted workflows return
    before the managed-connection seam, so jobs do not even touch the ledger.
    """
    if workflow not in BUDGETED:
        return
    if conn is not None:
        _record_budgeted_spend_usd(usd, now=now, conn=conn)
    else:
        _record_budgeted_spend_usd(usd, now=now)


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


def may_run(workflow: str, *, conn=None, now: datetime = None) -> tuple[bool, str]:
    """May this workflow spend right now? Returns ``(allowed, reason)``.

    Fails OPEN on any error. An unreadable counter must never be what stops
    Elixir from welcoming a new member.

    ``conn`` follows the repo convention — pass one in tests, omit in
    production, where the managed connection is opened per read. ``now`` makes
    the clock injectable, which matters because the counter is keyed by UTC
    DATE: a test that writes with a fixed date and reads with the real one
    passes only on the day it was written. (It did exactly that — these tests
    were green until UTC rolled past midnight a few hours later.)
    """
    if workflow not in BUDGETED:
        return True, ""
    ceiling = daily_ceiling_usd()
    if not ceiling:
        return True, ""
    if _REQUIRED_WORK.get():
        return True, ""
    try:
        spent = (
            spend_today_usd(conn=conn, now=now) if conn is not None else spend_today_usd(now=now)
        )
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
    "BUDGETED",
    "DEFERRABLE",
    "ESSENTIAL",
    "call_cost_usd",
    "daily_ceiling_usd",
    "may_run",
    "record_spend_usd",
    "required_work",
    "spend_today_usd",
]
