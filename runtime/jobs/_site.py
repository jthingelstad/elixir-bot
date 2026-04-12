"""POAP KINGS site content and promotion."""

__all__ = [
    "SITE_DATA_HOUR", "SITE_CONTENT_HOUR",
    "_promotion_discord_required_text", "_promotion_reddit_required_token",
    "_promotion_channel_posts", "_unwrap_outer_bold",
    "_validate_promote_content_or_raise", "_write_site_content_or_raise",
    "_commit_site_content_or_raise", "_publish_poap_kings_site_or_raise",
    "_normalize_poap_kings_publish_result", "_poapkings_publish_context",
    "_poapkings_publish_fallback", "_notify_poapkings_publish",
    "_promotion_content_cycle", "_site_data_refresh", "_site_content_cycle",
]

import asyncio
import os
from datetime import datetime, timezone

import discord
import cr_api
import db
import elixir_agent
from modules.poap_kings import site as poap_kings_site
from runtime import app as _app
from runtime.channel_subagents import build_subagent_memory_context
from runtime.app import bot, log
from runtime.helpers import _channel_msg_kwargs, _channel_scope, _get_singleton_channel_id
from runtime import status as runtime_status
from runtime.jobs._signals import (
    _channel_config_by_key,
    _load_live_clan_context,
    _post_to_elixir,
)


SITE_DATA_HOUR = int(os.getenv("SITE_DATA_HOUR", "18"))       # 6pm Chicago
SITE_CONTENT_HOUR = int(os.getenv("SITE_CONTENT_HOUR", "18"))  # 6pm Chicago


def _promotion_discord_required_text(trophies):
    return f"Required Trophies: [{trophies}]"


def _promotion_reddit_required_token(trophies):
    return f"[{trophies}]"


def _promotion_channel_posts(promote):
    posts = []
    discord_body = (((promote or {}).get("discord") or {}).get("body") or "").strip()
    reddit = (promote or {}).get("reddit") or {}
    reddit_title = (reddit.get("title") or "").strip()
    reddit_body = (reddit.get("body") or "").strip()

    if discord_body:
        posts.append(
            "**Discord recruiting copy**\n"
            f"```text\n{discord_body}\n```"
        )
    if reddit_title or reddit_body:
        reddit_lines = ["**Reddit recruiting copy**"]
        if reddit_title:
            reddit_lines.append(f"Title: `{reddit_title}`")
        if reddit_body:
            reddit_lines.append(f"```text\n{reddit_body}\n```")
        posts.append("\n".join(reddit_lines))
    return posts


def _unwrap_outer_bold(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) >= 4:
        return stripped[2:-2].strip()
    return stripped


def _validate_promote_content_or_raise(promote, required_trophies=2000) -> None:
    discord_text = _promotion_discord_required_text(required_trophies)
    reddit_token = _promotion_reddit_required_token(required_trophies)

    discord = (promote or {}).get("discord") or {}
    discord_body = (discord.get("body") or "").strip()
    if discord_body:
        first_line = next((line.strip() for line in discord_body.splitlines() if line.strip()), "")
        first_line = _unwrap_outer_bold(first_line)
        if discord_text not in first_line:
            raise ValueError(
                f"discord.body first line must include exact text `{discord_text}`"
            )
        if not first_line.endswith(discord_text):
            raise ValueError(
                f"discord.body first line must end with exact text `{discord_text}`"
            )

    reddit = (promote or {}).get("reddit") or {}
    reddit_title = (reddit.get("title") or "").strip()
    reddit_body = (reddit.get("body") or "").strip()
    if (reddit_title or reddit_body) and reddit_token not in reddit_title:
        raise ValueError(
            f"reddit.title must include exact token `{reddit_token}`"
        )


def _write_site_content_or_raise(content_type: str, data) -> None:
    if not poap_kings_site.write_content(content_type, data):
        raise RuntimeError(f"{content_type} content write failed")


def _commit_site_content_or_raise(message: str) -> None:
    if not poap_kings_site.commit_and_push(message):
        raise RuntimeError("site publish failed")


def _publish_poap_kings_site_or_raise(payloads: dict[str, object], message: str) -> dict[str, object]:
    return poap_kings_site.publish_site_content(payloads, message)


def _normalize_poap_kings_publish_result(result, payloads: dict[str, object]) -> dict[str, object]:
    content_types = list((payloads or {}).keys())
    if isinstance(result, dict):
        normalized = {
            "changed": bool(result.get("changed")),
            "commit_sha": result.get("commit_sha"),
            "commit_url": result.get("commit_url"),
            "repo": result.get("repo"),
            "branch": result.get("branch"),
            "changed_content_types": list(result.get("changed_content_types") or []),
            "changed_paths": list(result.get("changed_paths") or []),
        }
        if normalized["changed"] and not normalized["changed_content_types"]:
            normalized["changed_content_types"] = content_types
        if normalized["changed"] and not normalized["changed_paths"]:
            normalized["changed_paths"] = [
                poap_kings_site.target_path(content_type)
                for content_type in normalized["changed_content_types"]
            ]
        return normalized
    changed = bool(result)
    return {
        "changed": changed,
        "commit_sha": None,
        "commit_url": None,
        "repo": None,
        "branch": None,
        "changed_content_types": content_types if changed else [],
        "changed_paths": [poap_kings_site.target_path(content_type) for content_type in content_types] if changed else [],
    }


def _poapkings_publish_context(activity_key: str, *, publish_result=None, error_detail: str | None = None) -> str:
    result = publish_result or {}
    status = "failure" if error_detail else "success"
    changed_content_types = result.get("changed_content_types") or []
    changed_paths = result.get("changed_paths") or []
    lines = [
        "Write one short operational update for #poapkings-com.",
        "This channel exists only for POAP KINGS website publish visibility.",
        f"Activity key: {activity_key}",
        f"Publish status: {status}",
    ]
    if error_detail:
        lines.extend([
            "This publish failed.",
            f"Error detail: {error_detail}",
        ])
    else:
        lines.extend([
            "This publish succeeded and created a real GitHub commit.",
            f"Repo: {result.get('repo') or 'unknown'}",
            f"Branch: {result.get('branch') or 'unknown'}",
            f"Commit SHA: {result.get('commit_sha') or 'unknown'}",
            f"Commit URL: {result.get('commit_url') or 'unknown'}",
        ])
    if changed_content_types:
        lines.append(f"Changed content types: {', '.join(str(item) for item in changed_content_types)}")
    if changed_paths:
        lines.append("Changed paths:")
        lines.extend(f"- {path}" for path in changed_paths)
    lines.extend([
        "",
        "Required behavior:",
        "- Be concise and clear.",
        "- Include the commit SHA and GitHub URL when they are provided.",
        "- State which publish activity this came from.",
        "- Do not mention hidden mechanics, JSON, prompts, or internal code.",
    ])
    return "\n".join(lines)


def _poapkings_publish_fallback(activity_key: str, *, publish_result=None, error_detail: str | None = None) -> str:
    result = publish_result or {}
    label = activity_key.replace("-", " ")
    if error_detail:
        return f"**POAP KINGS publish failed**\n`{label}` hit an error: {error_detail}"
    sha = (result.get("commit_sha") or "")[:7] or "unknown"
    repo = result.get("repo") or "unknown"
    branch = result.get("branch") or "unknown"
    url = result.get("commit_url") or ""
    types = result.get("changed_content_types") or []
    changed = f" Changed: {', '.join(types)}." if types else ""
    link = f"\n{url}" if url else ""
    return f"**POAP KINGS publish succeeded**\n`{label}` pushed `{sha}` to `{repo}@{branch}`.{changed}{link}"


async def _notify_poapkings_publish(activity_key: str, *, publish_result=None, error_detail: str | None = None) -> bool:
    result = publish_result or {}
    if not error_detail and not result.get("changed"):
        return False
    try:
        channel_config = _channel_config_by_key("poapkings-com")
    except Exception as exc:
        log.warning("POAP KINGS publish notification skipped: %s", exc)
        return False

    channel = bot.get_channel(channel_config["id"])
    if not channel:
        log.warning("POAP KINGS publish notification skipped: channel not found")
        return False

    recent_posts = await asyncio.to_thread(
        db.list_channel_messages,
        channel.id,
        5,
        "assistant",
    )
    memory_context = await asyncio.to_thread(
        build_subagent_memory_context,
        channel_config,
        signals=[],
    )
    context = _poapkings_publish_context(activity_key, publish_result=result, error_detail=error_detail)

    try:
        generated = await asyncio.to_thread(
            elixir_agent.generate_channel_update,
            channel_config["name"],
            channel_config["subagent_key"],
            context,
            recent_posts=recent_posts,
            memory_context=memory_context,
            leadership=False,
        )
    except Exception as exc:
        log.error("POAP KINGS publish notification generation failed: %s", exc)
        generated = None

    result_payload = generated if isinstance(generated, dict) and generated.get("content") else {
        "event_type": "channel_update",
        "summary": f"POAP KINGS publish {activity_key}",
        "content": _poapkings_publish_fallback(activity_key, publish_result=result, error_detail=error_detail),
    }

    try:
        posts = _app._entry_posts(result_payload)
        await _post_to_elixir(channel, result_payload)
        event_type = "poapkings_publish_failure" if error_detail else "poapkings_publish_success"
        ch = _channel_msg_kwargs(channel)
        for index, post in enumerate(posts):
            await asyncio.to_thread(
                db.save_message,
                _channel_scope(channel), "assistant", post,
                summary=result_payload.get("summary") if index == 0 else None,
                **ch, workflow="poapkings-com",
                event_type=event_type if index == 0 else f"{event_type}_part",
                raw_json={
                    "activity_key": activity_key,
                    "publish_result": result,
                    "error_detail": error_detail,
                    "result": result_payload,
                },
            )
        return True
    except Exception as exc:
        log.error("POAP KINGS publish notification send failed: %s", exc, exc_info=True)
        return False


async def _promotion_content_cycle():
    runtime_status.mark_job_start("promotion_content_cycle")
    if not poap_kings_site.site_enabled():
        runtime_status.mark_job_success("promotion_content_cycle", "POAP KINGS site integration disabled")
        return
    try:
        promotion_channel_id = _get_singleton_channel_id("promotion")
    except Exception as exc:
        runtime_status.mark_job_failure("promotion_content_cycle", f"promotion channel config error: {exc}")
        return

    channel = bot.get_channel(promotion_channel_id)
    if not channel:
        runtime_status.mark_job_failure("promotion_content_cycle", "promotion channel not found")
        return

    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.error("Promotion content refresh failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("promotion_content_cycle", f"refresh failed: {exc}")
        return

    if not clan.get("memberList"):
        runtime_status.mark_job_success("promotion_content_cycle", "no member data")
        return

    roster_data = await asyncio.to_thread(poap_kings_site.build_roster_data, clan, True)
    promote = await asyncio.to_thread(
        elixir_agent.generate_promote_content,
        clan,
        war_data=war,
        roster_data=roster_data,
    )
    if not promote:
        runtime_status.mark_job_success("promotion_content_cycle", "no promotion content")
        return
    try:
        _validate_promote_content_or_raise(promote, required_trophies=clan.get("requiredTrophies", 2000))
    except Exception as exc:
        log.error("Promotion content validation failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("promotion_content_cycle", f"invalid promotion content: {exc}")
        return

    try:
        publish_result = await asyncio.to_thread(
            _publish_poap_kings_site_or_raise,
            {"promote": promote},
            "Elixir POAP KINGS promotion content update",
        )
    except Exception as exc:
        log.error("Promotion content publish error: %s", exc, exc_info=True)
        await _notify_poapkings_publish("promotion-content", error_detail=str(exc))
        runtime_status.mark_job_failure("promotion_content_cycle", f"site publish failed: {exc}")
        return
    publish_result = _normalize_poap_kings_publish_result(publish_result, {"promote": promote})
    await _notify_poapkings_publish("promotion-content", publish_result=publish_result)

    channel_posts = _promotion_channel_posts(promote)
    if not channel_posts:
        runtime_status.mark_job_success("promotion_content_cycle", "website updated, no promotion channel copy")
        return

    await _post_to_elixir(channel, {"content": channel_posts})
    ch = _channel_msg_kwargs(channel)
    for index, post in enumerate(channel_posts):
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel), "assistant", post,
            **ch, workflow="promotion",
            event_type="promotion_content_cycle" if index == 0 else "promotion_content_cycle_part",
        )
    runtime_status.mark_job_success("promotion_content_cycle", "website and Discord promotion content published")


async def _site_data_refresh():
    """On-demand site data refresh — refresh clan data and roster on poapkings.com."""
    runtime_status.mark_job_start("site_data_refresh")
    if not poap_kings_site.site_enabled():
        runtime_status.mark_job_success("site_data_refresh", "POAP KINGS site integration disabled")
        return
    try:
        try:
            clan = await asyncio.to_thread(cr_api.get_clan)
            _app._clear_cr_api_failure_alert_if_recovered()
        except Exception:
            log.error("Site data refresh: CR API failed")
            await _app._maybe_alert_cr_api_failure("site data refresh")
            clan = {}

        if not clan.get("memberList"):
            log.info("Site data refresh: no member data, skipping")
            runtime_status.mark_job_success("site_data_refresh", "no member data")
            return

        roster_data = await asyncio.to_thread(poap_kings_site.build_roster_data, clan)
        clan_stats = await asyncio.to_thread(poap_kings_site.build_clan_data, clan)
        publish_result = await asyncio.to_thread(
            _publish_poap_kings_site_or_raise,
            {"roster": roster_data, "clan": clan_stats},
            "Elixir POAP KINGS site data refresh",
        )
        publish_result = _normalize_poap_kings_publish_result(
            publish_result,
            {"roster": roster_data, "clan": clan_stats},
        )
        await _notify_poapkings_publish("site-data-refresh", publish_result=publish_result)
        log.info("Site data refresh complete: %d members", len(roster_data.get("members", [])))
        runtime_status.mark_job_success("site_data_refresh", f"{len(roster_data.get('members', []))} members")
    except Exception as e:
        log.error("Site data refresh error: %s", e, exc_info=True)
        await _notify_poapkings_publish("site-data-refresh", error_detail=str(e))
        runtime_status.mark_job_failure("site_data_refresh", str(e))


async def _site_content_cycle():
    """Daily site publish — refresh data, generate content, and push updates."""
    runtime_status.mark_job_start("site_content_cycle")
    if not poap_kings_site.site_enabled():
        runtime_status.mark_job_success("site_content_cycle", "POAP KINGS site integration disabled")
        return
    try:
        try:
            clan = await asyncio.to_thread(cr_api.get_clan)
            _app._clear_cr_api_failure_alert_if_recovered()
        except Exception:
            await _app._maybe_alert_cr_api_failure("site content cycle")
            clan = {}
        try:
            war = await asyncio.to_thread(cr_api.get_current_war)
        except Exception:
            await _app._maybe_alert_cr_api_failure("site content war refresh")
            war = {}

        # Build and write data (second daily refresh)
        roster_data = None
        payloads = {}
        if clan.get("memberList"):
            roster_data = await asyncio.to_thread(
                poap_kings_site.build_roster_data,
                clan,
                include_cards=True,
            )
            clan_stats = await asyncio.to_thread(poap_kings_site.build_clan_data, clan)
            payloads["roster"] = roster_data
            payloads["clan"] = clan_stats

        # Generate home message
        try:
            prev_home = await asyncio.to_thread(poap_kings_site.load_published, "home")
            if prev_home is None:
                prev_home = await asyncio.to_thread(poap_kings_site.load_current, "home")
            prev_msg = prev_home.get("message", "") if prev_home else ""
            home_text = await asyncio.to_thread(
                elixir_agent.generate_home_message,
                clan,
                war,
                prev_msg,
                roster_data=roster_data,
            )
        except Exception as e:
            log.error("Home message error: %s", e)
            home_text = None
        if home_text:
            payloads["home"] = {
                "message": home_text,
                "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        if payloads:
            publish_result = await asyncio.to_thread(
                _publish_poap_kings_site_or_raise,
                payloads,
                "Elixir POAP KINGS daily site sync",
            )
            publish_result = _normalize_poap_kings_publish_result(publish_result, payloads)
            await _notify_poapkings_publish("site-content", publish_result=publish_result)
        log.info("Site content cycle complete")
        runtime_status.mark_job_success("site_content_cycle", "content updated")
    except Exception as e:
        log.error("Site content cycle error: %s", e, exc_info=True)
        await _notify_poapkings_publish("site-content", error_detail=str(e))
        runtime_status.mark_job_failure("site_content_cycle", str(e))
