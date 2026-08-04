"""Player and opponent intelligence jobs.

Two scheduled jobs live here: the rolling player-intel refresh (profile +
battle-log snapshots for stale active members) and the Clan Wars Intel
Report, a thin shell around the `intel_report` LLM workflow that fetches
via tools and composes the Discord-ready post itself.
"""

__all__ = [
    "_clan_wars_intel_email",
]

import logging

from runtime import status as runtime_status
from storage.contextual_memory import upsert_intel_report_memory

log = logging.getLogger("elixir")


def _runtime_app():
    import runtime.app as app

    return app


def _bot():
    return _runtime_app().bot


# _clan_wars_intel_report (the #elixir Discord version) was REMOVED 2026-08-03.
# Email is the path for this report (Jamie). It was already unscheduled — the
# clan-wars-intel activity points at _clan_wars_intel_email — but it survived as
# reachable dead code writing the SAME runtime_status key, so a green Discord run
# could mask a failed email run.


# How long after a war season opens the intel report is still worth sending.
# Past this the "what to expect this season" framing is stale.
INTEL_LOOKBACK_DAYS = 6


async def _clan_wars_intel_email():
    """Email the Clan Wars Intel Report once per war season."""
    import asyncio as _asyncio
    from datetime import datetime, timedelta, timezone

    import db as _db
    from agent.mail import outbound
    from agent.workflows import generate_war_intel_narrative
    from runtime import war_intel

    runtime_status.mark_job_start("clan_wars_intel")

    if not outbound.enabled():
        runtime_status.mark_job_success("clan_wars_intel", "skipped: mail not configured")
        return

    def _due() -> int | None:
        """The season id owed a report, or None."""
        with _db.get_connection() as conn:
            row = conn.execute(
                "SELECT season_id, started_at FROM war_seasons "
                "WHERE ended_at IS NULL ORDER BY season_id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            season_id, started_at = int(row[0]), str(row[1] or "")
            try:
                started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            except ValueError:
                return None
            if datetime.now(timezone.utc) - started > timedelta(days=INTEL_LOOKBACK_DAYS):
                return None  # too late to be "the start of the season"
        # Ask through the same API the writer uses -- the memories table does not
        # store a plain "clan_wars_intel:135" key, and hand-rolling the format here
        # would make the idempotency check quietly never match.
        from storage.contextual_memory import list_memories

        already = list_memories(
            viewer_scope="system_internal",
            include_system_internal=True,
            filters={"event_type": "clan_wars_intel", "event_id": str(season_id)},
            limit=1,
        )
        return None if already else season_id

    season_id = await _asyncio.to_thread(_due)
    if season_id is None:
        runtime_status.mark_job_success("clan_wars_intel", "no season owed a report")
        return

    try:
        ctx = await _asyncio.to_thread(war_intel.build_intel_context, season_id=season_id)
    except ValueError as exc:
        runtime_status.mark_job_success("clan_wars_intel", f"skipped: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - report failure must not kill the scheduler
        log.exception("intel email: context build failed")
        runtime_status.mark_job_failure("clan_wars_intel", f"context build failed: {exc}")
        return

    narrative = await _asyncio.to_thread(
        generate_war_intel_narrative, war_intel.facts_for_model(ctx)
    )
    subject, body = war_intel.render_war_intel_email(ctx, narrative or {})

    recipients = await _asyncio.to_thread(lambda: [m["email"] for m in _db.list_member_emails()])
    if not recipients:
        runtime_status.mark_job_success("clan_wars_intel", "no members with a verified email")
        return

    import os as _os

    sender = _os.getenv("ELIXIR_EMAIL_ADDRESS", "elixir@poapkings.com")
    await _asyncio.to_thread(outbound.send, to=sender, bcc=recipients, subject=subject, body=body)

    # The memory IS the idempotency. Written after the send, because a memory
    # without a send would silently skip the season -- but that ordering means a
    # failed write leaves a sent email with nothing recording it, and the next
    # run broadcasts the same report to the whole clan again.
    #
    # This happened on the first live run (2026-08-03): the send landed, the
    # upsert hit "database is locked" behind the bot's own writer, and it was
    # logged at WARNING and swallowed. A warning is the wrong severity for
    # "a duplicate mass email is now scheduled." Retry, then fail the job loudly.
    memory_ok = False
    for attempt in range(3):
        try:
            await _asyncio.to_thread(
                upsert_intel_report_memory,
                season_id=season_id,
                body=body,
                metadata={"clan_count": len(ctx["opponents"]), "delivery": "email"},
            )
            memory_ok = True
            break
        except Exception:
            log.warning("intel email: memory upsert attempt %d failed", attempt + 1, exc_info=True)
            await _asyncio.sleep(5)
    if not memory_ok:
        runtime_status.mark_job_failure(
            "clan_wars_intel",
            f"season {season_id} EMAILED to {len(recipients)} member(s) but the idempotency "
            "memory could not be written -- the next run will re-send unless it is added by hand",
        )
        return

    runtime_status.mark_job_success(
        "clan_wars_intel",
        f"emailed season {season_id} intel ({len(ctx['opponents'])} opponents) "
        f"to {len(recipients)} member(s)",
    )
