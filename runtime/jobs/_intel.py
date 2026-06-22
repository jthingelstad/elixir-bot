"""Player and opponent intelligence jobs.

Two scheduled jobs live here: the rolling player-intel refresh (profile +
battle-log snapshots for stale active members) and the Clan Wars Intel
Report, a thin shell around the `intel_report` LLM workflow that fetches
via tools and composes the Discord-ready post itself.
"""

__all__ = [
    "_clan_wars_intel_report",
    "_player_intel_refresh",
    "_player_intel_refresh_minutes",
    "PLAYER_INTEL_REFRESH_MINUTES", "PLAYER_INTEL_REFRESH_HOURS",
    "PLAYER_INTEL_BATCH_SIZE", "PLAYER_INTEL_STALE_HOURS",
    "PLAYER_INTEL_REQUEST_SPACING_SECONDS",
]

import asyncio
import logging
import os

import cr_api
import db
import elixir_agent
from storage.contextual_memory import upsert_intel_report_memory
from runtime.helpers import _get_singleton_channel_id
from runtime.jobs._signals import (
    _channel_config_by_key,
    _post_to_elixir,
)
from runtime.signal_lanes import build_lane_memory_context
from runtime import status as runtime_status

log = logging.getLogger("elixir")


def _runtime_app():
    import runtime.app as app

    return app


def _bot():
    return _runtime_app().bot


def _player_intel_refresh_minutes() -> int:
    minutes = os.getenv("PLAYER_INTEL_REFRESH_MINUTES")
    if minutes:
        return max(1, int(minutes))
    legacy_hours = os.getenv("PLAYER_INTEL_REFRESH_HOURS")
    if legacy_hours:
        return max(1, int(float(legacy_hours) * 60))
    return 30


PLAYER_INTEL_REFRESH_MINUTES = _player_intel_refresh_minutes()
PLAYER_INTEL_REFRESH_HOURS = PLAYER_INTEL_REFRESH_MINUTES / 60
PLAYER_INTEL_BATCH_SIZE = int(os.getenv("PLAYER_INTEL_BATCH_SIZE", "5"))
PLAYER_INTEL_STALE_HOURS = int(os.getenv("PLAYER_INTEL_STALE_HOURS", "1"))
PLAYER_INTEL_REQUEST_SPACING_SECONDS = float(os.getenv("PLAYER_INTEL_REQUEST_SPACING_SECONDS", "2.0"))


async def _player_intel_refresh():
    """Refresh stored player profile and battle intelligence for a subset of active
    members. This job is now REFRESH-ONLY: it keeps the v4 read model (profile /
    battle snapshots the agent's tools read) current. Member highlights are posted
    by the v5 celebrate detectors (#player-highlights); the old v4 progression
    delivery was removed in the item-7 decommission."""
    runtime_status.mark_job_start("player_intel_refresh")
    try:
        clan = await asyncio.to_thread(cr_api.get_clan)
        _runtime_app()._clear_cr_api_failure_alert_if_recovered()
    except Exception as e:
        log.error("Player intel refresh: clan fetch failed: %s", e)
        await _runtime_app()._maybe_alert_cr_api_failure("player intel refresh")
        runtime_status.mark_job_failure("player_intel_refresh", f"clan fetch failed: {e}")
        return

    members = clan.get("memberList", [])
    if not members:
        log.info("Player intel refresh: no member data, skipping")
        runtime_status.mark_job_success("player_intel_refresh", "no member data")
        return

    war = await asyncio.to_thread(db.get_current_war_status) or {}

    targets = await asyncio.to_thread(
        db.get_player_intel_refresh_targets,
        PLAYER_INTEL_BATCH_SIZE,
        PLAYER_INTEL_STALE_HOURS,
    )
    if not targets:
        log.info("Player intel refresh: no stale targets")
        runtime_status.mark_job_success("player_intel_refresh", "no stale targets")
        return

    refreshed = 0
    profile_failures = 0
    battle_log_failures = 0
    failed_targets = 0
    processing_failures = 0
    for target in targets:
        tag = target["tag"]
        try:
            profile_ok = False
            battle_log_ok = False
            profile = await asyncio.to_thread(cr_api.get_player, tag)
            if profile is not None:
                profile_ok = True
            else:
                profile_failures += 1
            if profile:
                # snapshot_player_profile writes the v4 read model (the refresh);
                # its returned progression signals are no longer delivered (v5 owns
                # #player-highlights) so we don't collect them.
                await asyncio.to_thread(db.snapshot_player_profile, profile)
            battle_log = await asyncio.to_thread(cr_api.get_player_battle_log, tag)
            if battle_log is not None:
                battle_log_ok = True
            else:
                battle_log_failures += 1
            if battle_log:
                await asyncio.to_thread(db.snapshot_player_battlelog, tag, battle_log)
            if profile_ok or battle_log_ok:
                refreshed += 1
            else:
                failed_targets += 1
            await asyncio.sleep(PLAYER_INTEL_REQUEST_SPACING_SECONDS)
        except Exception as e:
            processing_failures += 1
            failed_targets += 1
            log.warning("Player intel refresh failed for %s: %s", tag, e)

    if profile_failures or battle_log_failures:
        await _runtime_app()._maybe_alert_cr_api_failure("player intel refresh")

    total_targets = len(targets)
    failure_summary = []
    if profile_failures:
        failure_summary.append(f"profile failures {profile_failures}")
    if battle_log_failures:
        failure_summary.append(f"battle log failures {battle_log_failures}")
    if failed_targets:
        failure_summary.append(f"full target failures {failed_targets}")
    if processing_failures:
        failure_summary.append(f"processing failures {processing_failures}")

    if refreshed == 0 and failure_summary:
        detail = f"refreshed 0 of {total_targets} member(s); " + "; ".join(failure_summary)
        log.error("Player intel refresh failed: %s", detail)
        runtime_status.mark_job_failure("player_intel_refresh", detail)
        return

    summary = f"refreshed {refreshed} of {total_targets} member(s)"
    if failure_summary:
        summary = f"{summary}; " + "; ".join(failure_summary)
        log.warning("Player intel refresh completed with partial failures: %s", summary)
    else:
        log.info("Player intel refresh complete: %s", summary)
    runtime_status.mark_job_success("player_intel_refresh", summary)


async def _clan_wars_intel_report():
    """Generate and post the Clan Wars Intel Report to #river-race."""
    runtime_status.mark_job_start("clan_wars_intel")

    try:
        channel_id = _get_singleton_channel_id("river-race")
    except Exception as exc:
        runtime_status.mark_job_failure("clan_wars_intel", f"channel config error: {exc}")
        return

    channel = _bot().get_channel(channel_id)
    if not channel:
        runtime_status.mark_job_failure("clan_wars_intel", "river-race channel not found")
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
        channel_config = _channel_config_by_key("river-race")
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
