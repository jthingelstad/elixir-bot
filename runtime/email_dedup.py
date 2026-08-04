"""Has this email already gone out?

Three of the four email deliverables had no idempotency at all: re-running the
weekly recap, a release cut, or the member-report cycle re-broadcast to every
recipient. That is not hypothetical — on 2026-08-03 the weekly recap was
manually re-triggered twice while debugging, and each run mailed everyone again.

The mechanism is the one the Clan Wars Intel job already uses: a contextual
memory row keyed by (kind, key), checked before sending and written after. No
new table — the centralized-schema rule means a table is a migration, and this
does not need one.

**Write-after-send is deliberate, and it is the lesser of two evils.** Recording
BEFORE the send means a failed send is remembered as sent, and the deliverable
silently never goes out — the exact failure mode this codebase spent 2026-08-03
fixing. Recording after leaves a narrow window where a crash between send and
record causes ONE duplicate. A duplicate is visible and annoying; a silent
omission is invisible and worse.
"""

from __future__ import annotations

import logging

log = logging.getLogger("elixir")

_EVENT_PREFIX = "email"


def _event_type(kind: str) -> str:
    return f"{_EVENT_PREFIX}:{kind}"


def already_sent(kind: str, key: str) -> bool:
    """True when this (kind, key) was already mailed.

    Fails OPEN — an unreadable memory store returns False, so the email still
    goes out. A duplicate beats a silently skipped deliverable.
    """
    try:
        from storage.contextual_memory import list_memories

        rows = list_memories(
            viewer_scope="system_internal",
            include_system_internal=True,
            filters={"event_type": _event_type(kind), "event_id": str(key)},
            limit=1,
        )
        return bool(rows)
    except Exception:  # noqa: BLE001 - fail open, see docstring
        log.warning(
            "email dedup: lookup failed for %s/%s; sending anyway", kind, key, exc_info=True
        )
        return False


def record_sent(kind: str, key: str, *, detail: str = "") -> bool:
    """Remember that this (kind, key) was mailed. True when recorded.

    A False return means the NEXT run may re-send — callers that care should say
    so out loud rather than swallowing it.
    """
    try:
        from storage.contextual_memory import upsert_summary_memory

        upsert_summary_memory(
            event_type=_event_type(kind),
            event_id=str(key),
            title=f"email sent: {kind} {key}",
            body=detail or f"{kind} email delivered for {key}",
            scope="system_internal",
            tags=["email", kind],
        )
        return True
    except Exception:  # noqa: BLE001 - reported by the caller
        log.warning("email dedup: could not record %s/%s", kind, key, exc_info=True)
        return False
