"""Player and opponent intelligence jobs.

Two scheduled jobs live here: the rolling player-intel refresh (profile +
battle-log snapshots for stale active members) and the Clan Wars Intel
Report, a thin shell around the `intel_report` LLM workflow that fetches
via tools and composes the Discord-ready post itself.
"""

__all__ = [
    "_clan_wars_intel_report",
]

import asyncio
import logging

import cr_api
import elixir_agent
from storage.contextual_memory import upsert_intel_report_memory
from runtime.helpers import _get_singleton_channel_id, _channel_config_by_key, build_lane_memory_context
from runtime.helpers._common import _post_to_elixir
from runtime import status as runtime_status

log = logging.getLogger("elixir")


def _runtime_app():
    import runtime.app as app

    return app


def _bot():
    return _runtime_app().bot








async def _clan_wars_intel_report():
    """Generate and post the Clan Wars Intel Report to #elixir."""
    runtime_status.mark_job_start("clan_wars_intel")

    try:
        channel_id = _get_singleton_channel_id("elixir")
    except Exception as exc:
        runtime_status.mark_job_failure("clan_wars_intel", f"channel config error: {exc}")
        return

    channel = _bot().get_channel(channel_id)
    if not channel:
        runtime_status.mark_job_failure("clan_wars_intel", "elixir channel not found")
        return

    try:
        war = await asyncio.to_thread(cr_api.get_current_war)
    except Exception as exc:
        log.error("Intel report: war fetch failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("clan_wars_intel", f"war fetch failed: {exc}")
        return

    if not war:
        runtime_status.mark_job_success("clan_wars_intel", "no active war data")
        return

    our_tag = cr_api.CLAN_TAG
    competitors = [
        (c.get("tag") or "").lstrip("#").upper()
        for c in (war.get("clans") or [])
        if (c.get("tag") or "").lstrip("#").upper() and (c.get("tag") or "").lstrip("#").upper() != our_tag
    ]
    if not competitors:
        runtime_status.mark_job_success("clan_wars_intel", "no competitors in current war")
        return

    season_id = war.get("seasonId")

    memory_context = None
    try:
        channel_config = _channel_config_by_key("elixir")
        memory_context = await asyncio.to_thread(
            build_lane_memory_context, channel_config, signals=[],
        )
    except Exception as exc:
        log.warning("Intel report: memory context setup failed: %s", exc)

    response = await asyncio.to_thread(
        elixir_agent.generate_intel_report,
        our_tag,
        competitors,
        season_id=season_id,
        memory_context=memory_context,
    )

    if not isinstance(response, dict):
        runtime_status.mark_job_failure("clan_wars_intel", "intel_report workflow returned no response")
        return

    content = response.get("content")
    if isinstance(content, str):
        messages = [content]
    elif isinstance(content, list):
        messages = [str(m) for m in content if m]
    else:
        messages = []

    if not messages:
        runtime_status.mark_job_failure("clan_wars_intel", "intel_report workflow returned empty content")
        return

    for message_text in messages:
        await _post_to_elixir(channel, {"content": message_text})

    if season_id is not None:
        memory_body = "\n\n".join(messages)
        summary = response.get("summary")
        if summary:
            memory_body = f"{summary}\n\n{memory_body}"
        try:
            await asyncio.to_thread(
                upsert_intel_report_memory,
                season_id=season_id,
                body=memory_body,
                metadata={"clan_count": len(competitors)},
            )
        except Exception as exc:
            log.warning("Intel report: memory upsert failed: %s", exc)

    runtime_status.mark_job_success(
        "clan_wars_intel",
        f"posted {len(messages)} messages for {len(competitors)} opponents",
    )
