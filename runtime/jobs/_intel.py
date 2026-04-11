"""Clan Wars Intel Report job."""

__all__ = ["_clan_wars_intel_report", "INTEL_REQUEST_SPACING_SECONDS"]

import asyncio
import logging
import os

import cr_api
import elixir_agent
from storage.opponent_intel import build_intel_report
from storage.contextual_memory import upsert_intel_report_memory
from runtime.helpers._intel_report import (
    format_intel_report,
    format_intel_summary_for_memory,
)
from runtime.app import bot, log
from runtime.helpers import _get_singleton_channel_id
from runtime.jobs._signals import _channel_config_by_key, _post_to_elixir
from runtime.channel_subagents import build_subagent_memory_context
from runtime import status as runtime_status

INTEL_REQUEST_SPACING_SECONDS = float(os.getenv("INTEL_REQUEST_SPACING_SECONDS", "1.5"))


async def _clan_wars_intel_report():
    """Generate and post the Clan Wars Intel Report to #river-race."""
    runtime_status.mark_job_start("clan_wars_intel")

    # 1. Resolve target channel
    try:
        channel_id = _get_singleton_channel_id("river-race")
    except Exception as exc:
        runtime_status.mark_job_failure("clan_wars_intel", f"channel config error: {exc}")
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        runtime_status.mark_job_failure("clan_wars_intel", "river-race channel not found")
        return

    # 2. Fetch current war data
    try:
        war = await asyncio.to_thread(cr_api.get_current_war)
    except Exception as exc:
        log.error("Intel report: war fetch failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("clan_wars_intel", f"war fetch failed: {exc}")
        return

    if not war:
        runtime_status.mark_job_success("clan_wars_intel", "no active war data")
        return

    competing_clans = war.get("clans") or []
    our_clan = war.get("clan") or {}
    our_tag = (our_clan.get("tag") or "").lstrip("#").upper()

    if not competing_clans:
        runtime_status.mark_job_success("clan_wars_intel", "no competing clans in war data")
        return

    # 3. Collect all clan tags to fetch (competitors + our clan)
    all_tags = set()
    for c in competing_clans:
        tag = (c.get("tag") or "").lstrip("#").upper()
        if tag:
            all_tags.add(tag)
    if our_tag:
        all_tags.add(our_tag)

    # 4. Fetch each clan's full profile with rate-limit spacing
    clan_profiles: dict[str, dict | None] = {}
    for tag in all_tags:
        profile = await asyncio.to_thread(cr_api.get_clan_by_tag, tag)
        clan_profiles[tag] = profile
        if len(clan_profiles) < len(all_tags):
            await asyncio.sleep(INTEL_REQUEST_SPACING_SECONDS)

    # 5. Build analysis
    analyses = await asyncio.to_thread(
        build_intel_report, war, clan_profiles, our_tag,
    )

    if not analyses:
        runtime_status.mark_job_success("clan_wars_intel", "no clans to analyze")
        return

    season_id = war.get("seasonId")

    # 6. Generate LLM strategic summary
    llm_summary = None
    try:
        data_summary = await asyncio.to_thread(format_intel_summary_for_memory, analyses)
        channel_config = _channel_config_by_key("river-race")
        memory_context = await asyncio.to_thread(
            build_subagent_memory_context, channel_config, signals=[],
        )

        context = (
            f"A new clan wars season has begun (Season {season_id}).\n"
            f"Here is the scouting data on our {len([a for a in analyses if not a['is_us']])} opponents:\n\n"
            f"{data_summary}\n\n"
            "Write a brief strategic assessment (2-4 sentences) highlighting which clans "
            "pose the biggest threats and why, and any notable weaknesses we could exploit. "
            "Be direct and actionable."
        )
        llm_summary = await asyncio.to_thread(
            elixir_agent.generate_channel_update,
            channel_config["name"],
            channel_config["subagent_key"],
            context,
            memory_context=memory_context,
            leadership=False,
        )
        if isinstance(llm_summary, dict):
            llm_summary = llm_summary.get("content")
        if isinstance(llm_summary, list):
            llm_summary = "\n\n".join(str(item) for item in llm_summary if item)
        llm_summary = (llm_summary or "").strip() or None
    except Exception as exc:
        log.warning("Intel report: LLM summary generation failed (continuing without): %s", exc)
        llm_summary = None

    # 7. Format Discord messages
    messages = await asyncio.to_thread(
        format_intel_report, analyses, season_id=season_id, llm_summary=llm_summary,
    )

    # 8. Post to river-race
    for message_text in messages:
        await _post_to_elixir(channel, {"content": message_text})

    # 9. Persist memory
    if season_id is not None:
        try:
            memory_body = await asyncio.to_thread(format_intel_summary_for_memory, analyses)
            if llm_summary:
                memory_body = f"{llm_summary}\n\nData: {memory_body}"
            await asyncio.to_thread(
                upsert_intel_report_memory,
                season_id=season_id,
                body=memory_body,
                metadata={"clan_count": len([a for a in analyses if not a["is_us"]])},
            )
        except Exception as exc:
            log.warning("Intel report: memory upsert failed: %s", exc)

    runtime_status.mark_job_success(
        "clan_wars_intel",
        f"posted {len(messages)} messages for {len([a for a in analyses if not a['is_us']])} opponents",
    )
