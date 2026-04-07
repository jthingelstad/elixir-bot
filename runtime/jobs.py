import asyncio
import json
import os
import re
from datetime import datetime, timezone

import discord
import cr_api
import db
import elixir_agent
import heartbeat
import prompts
from modules.poap_kings import site as poap_kings_site
from storage.contextual_memory import upsert_war_recap_memory, upsert_weekly_summary_memory
from runtime import app as _app
from runtime.channel_subagents import (
    build_subagent_memory_context,
    maybe_upsert_signal_memory,
    OPTIONAL_PROGRESSION_SIGNAL_TYPES,
    plan_signal_outcomes,
)
from runtime.app import (
    CHICAGO,
    bot,
    log,
)
from runtime.helpers import _channel_scope, _get_singleton_channel_id
from runtime import status as runtime_status
from runtime.system_signals import queue_startup_system_signals

_WEEKLY_RECAP_HEADER_RE = re.compile(r"^\s*[*#_`\s]*weekly recap\b", re.IGNORECASE)
def _promotion_discord_required_text(trophies):
    return f"Required Trophies: [{trophies}]"

def _promotion_reddit_required_token(trophies):
    return f"[{trophies}]"
async def _post_to_elixir(*args, **kwargs):
    return await _app._post_to_elixir(*args, **kwargs)


async def _load_live_clan_context(*args, **kwargs):
    return await _app._load_live_clan_context(*args, **kwargs)


def _build_weekly_clanops_review(*args, **kwargs):
    return _app._build_weekly_clanops_review(*args, **kwargs)


def _build_weekly_clan_recap_context(*args, **kwargs):
    return _app._build_weekly_clan_recap_context(*args, **kwargs)


def _channel_config_by_key(channel_key: str) -> dict:
    config = prompts.discord_channels_by_subagent().get(channel_key)
    if not config:
        raise RuntimeError(f"channel subagent not configured: {channel_key}")
    return config


def _signal_group_needs_recap_memory(signals):
    recap_types = {"war_battle_day_complete", "war_week_complete", "war_completed", "war_season_complete"}
    return any((signal.get("type") in recap_types) for signal in (signals or []))



def _build_outcome_context(outcome, signals, clan, war):
    channel_key = outcome["target_channel_key"]
    first = (signals or [{}])[0]
    lines = [
        f"Target channel subagent: {channel_key}",
        f"Intent: {outcome['intent']}",
        "Write the final post for that destination only.",
        "Do not mention other channels or other internal outcomes from the same signal.",
        "",
        "Signals:",
        json.dumps(signals or [], indent=2, default=str),
    ]
    if channel_key == "river-race":
        lines.extend([
            "",
            "Focus on River Race state, momentum, and what the clan should do right now.",
            "Current war data:",
            json.dumps(war or {}, indent=2, default=str),
        ])
    elif channel_key == "player-progress":
        lines.extend([
            "",
            "Focus on the player's achievement and why it is worth celebrating.",
        ])
    elif channel_key == "clan-events":
        has_likely_kick = any(s.get("likely_kicked") for s in (signals or []))
        if has_likely_kick:
            lines.extend([
                "",
                "This member was likely removed from the clan due to inactivity.",
                "Keep the message brief and neutral. Do not write a warm farewell or thank them for contributions.",
                "A simple factual note that the member is no longer with the clan is enough.",
            ])
        else:
            lines.extend([
                "",
                "Focus on the communal clan moment and keep the tone welcoming and proud.",
            ])
    elif channel_key == "leader-lounge":
        lines.extend([
            "",
            "This is a leadership-facing factual note. Include useful operational context, not public hype.",
        ])
        tag = first.get("tag")
        if tag:
            try:
                profile = db.get_member_profile(tag)
            except Exception:
                profile = None
            if profile:
                lines.extend([
                    "Member profile context:",
                    json.dumps(profile, indent=2, default=str),
                ])
    else:
        lines.extend([
            "",
            "Current clan data:",
            json.dumps(clan or {}, indent=2, default=str),
        ])
    return "\n".join(lines)


async def _mark_signal_group_completed(signals):
    await asyncio.to_thread(_mark_delivered_signals, signals)
    for signal in signals or []:
        if signal.get("signal_key"):
            await asyncio.to_thread(db.mark_system_signal_announced, signal["signal_key"])


async def _deliver_signal_outcome(outcome, signals, clan, war):
    existing = await asyncio.to_thread(
        db.get_signal_outcome,
        outcome["source_signal_key"],
        outcome["target_channel_key"],
        outcome["intent"],
    )
    if existing and existing.get("delivery_status") == "delivered":
        return True

    await asyncio.to_thread(
        db.upsert_signal_outcome,
        outcome["source_signal_key"],
        outcome["source_signal_type"],
        outcome["target_channel_key"],
        outcome["target_channel_id"],
        outcome["intent"],
        required=outcome.get("required", True),
        delivery_status="planned",
        payload=outcome.get("payload"),
    )

    channel_config = _channel_config_by_key(outcome["target_channel_key"])
    channel = bot.get_channel(channel_config["id"])
    if not channel:
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            outcome["source_signal_key"],
            outcome["source_signal_type"],
            outcome["target_channel_key"],
            outcome["target_channel_id"],
            outcome["intent"],
            required=outcome.get("required", True),
            delivery_status="failed",
            payload=outcome.get("payload"),
            error_detail="channel not found",
        )
        return False

    channel_id = channel_config["id"]
    recent_posts = await asyncio.to_thread(
        db.list_channel_messages,
        channel_id,
        10,
        "assistant",
    )
    memory_context = await asyncio.to_thread(
        build_subagent_memory_context,
        channel_config,
        signals=signals,
    )
    context = _build_outcome_context(outcome, signals, clan, war)
    preauthored_result = None
    if len(signals) == 1 and signals[0].get("signal_key"):
        preauthored_result = _preauthored_system_signal_result(signals[0])

    try:
        channel_name = getattr(channel, "name", None)
        if not isinstance(channel_name, str):
            channel_name = None
        channel_kind = getattr(channel, "type", None)
        if channel_kind is not None:
            channel_kind = str(channel_kind)
        if preauthored_result is not None:
            result = preauthored_result
        else:
            result = await asyncio.to_thread(
                elixir_agent.generate_channel_update,
                channel_config["name"],
                channel_config["subagent_key"],
                context,
                recent_posts=recent_posts,
                memory_context=memory_context,
                leadership=(channel_config["memory_scope"] == "leadership"),
            )
        if result is None:
            status = "failed" if outcome.get("required", True) else "skipped"
            await asyncio.to_thread(
                db.upsert_signal_outcome,
                outcome["source_signal_key"],
                outcome["source_signal_type"],
                outcome["target_channel_key"],
                outcome["target_channel_id"],
                outcome["intent"],
                required=outcome.get("required", True),
                delivery_status=status,
                payload=outcome.get("payload"),
                error_detail="generator returned null",
                mark_attempt=True,
            )
            return status == "skipped"

        result = await _app._apply_member_refs_to_result(result)
        posts = _app._entry_posts(result)
        await _post_to_elixir(channel, result)
        summary = result.get("summary")
        event_type = result.get("event_type") or outcome["intent"]
        for index, post in enumerate(posts):
            post_summary = summary if index == 0 else f"{summary} ({index + 1}/{len(posts)})" if summary else None
            post_event_type = event_type if index == 0 else f"{event_type}_part"
            await asyncio.to_thread(
                db.save_message,
                _channel_scope(channel),
                "assistant",
                post,
                summary=post_summary,
                channel_id=channel_id,
                channel_name=channel_name,
                channel_kind=channel_kind,
                workflow=channel_config["subagent_key"],
                event_type=post_event_type,
                raw_json={
                    "source_signal_key": outcome["source_signal_key"],
                    "intent": outcome["intent"],
                    "target_channel_key": outcome["target_channel_key"],
                    "result": result,
                },
            )
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            outcome["source_signal_key"],
            outcome["source_signal_type"],
            outcome["target_channel_key"],
            outcome["target_channel_id"],
            outcome["intent"],
            required=outcome.get("required", True),
            delivery_status="delivered",
            payload={"result": result, "signals": signals},
            mark_attempt=True,
            delivered=True,
        )
        body = "\n\n".join(posts)
        await asyncio.to_thread(
            maybe_upsert_signal_memory,
            source_signal_key=outcome["source_signal_key"],
            signal_type=(signals[0].get("type") or outcome["source_signal_type"]),
            body=body,
            outcome=outcome,
            signals=signals,
        )
        if channel_config["subagent_key"] == "river-race" and _signal_group_needs_recap_memory(signals):
            await asyncio.to_thread(_store_recap_memories_for_signal_batch, signals, posts, channel_id)
        return True
    except Exception as exc:
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            outcome["source_signal_key"],
            outcome["source_signal_type"],
            outcome["target_channel_key"],
            outcome["target_channel_id"],
            outcome["intent"],
            required=outcome.get("required", True),
            delivery_status="failed",
            payload=outcome.get("payload"),
            error_detail=str(exc),
            mark_attempt=True,
        )
        log.error("Signal outcome delivery failed for %s/%s: %s", outcome["source_signal_key"], outcome["target_channel_key"], exc, exc_info=True)
        return False


async def _deliver_signal_group(signals, clan, war):
    outcomes = plan_signal_outcomes(signals)
    if not outcomes:
        return False
    results = []
    for outcome in outcomes:
        delivered = await _deliver_signal_outcome(outcome, signals, clan, war)
        results.append(delivered)
    rows = await asyncio.to_thread(db.list_signal_outcomes, outcomes[0]["source_signal_key"])
    if rows and all(row.get("delivery_status") in {"delivered", "skipped"} for row in rows):
        await _mark_signal_group_completed(signals)
        return True
    return all(results)


def _strip_weekly_recap_header(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    lines = body.splitlines()
    if lines and _WEEKLY_RECAP_HEADER_RE.match(lines[0] or ""):
        lines = lines[1:]
        while lines and not (lines[0] or "").strip():
            lines = lines[1:]
    return "\n".join(lines).strip()


def _format_weekly_recap_post(recap_text: str, *, now: datetime | None = None) -> str:
    body = _strip_weekly_recap_header(recap_text)
    current = (now or datetime.now(timezone.utc)).astimezone(CHICAGO)
    title = f"**Weekly Recap | {current.strftime('%B')} {current.day}, {current.year}**"
    if not body:
        return title
    return f"{title}\n\n{body}"


def _observation_signal_batches(signals):
    if not signals:
        return []
    grouped = []
    completion_batch = []
    batches = []
    completion_signal_types = {
        "war_completed",
        "war_week_complete",
        "war_champ_standings",
    }
    for signal in signals:
        signal_type = signal.get("type") or ""
        if signal_type.startswith("war_"):
            if signal_type in completion_signal_types:
                completion_batch.append(signal)
                continue
            batches.append([signal])
        else:
            grouped.append(signal)
    if grouped:
        batches.insert(0, grouped)
    if completion_batch:
        batches.append(completion_batch)
    return batches


def _progression_signal_batches(signals):
    if not signals:
        return []

    required_signals = [
        signal for signal in signals
        if signal.get("type") not in OPTIONAL_PROGRESSION_SIGNAL_TYPES
    ]
    optional_signals = [
        signal for signal in signals
        if signal.get("type") in OPTIONAL_PROGRESSION_SIGNAL_TYPES
    ]

    batches = []
    if required_signals:
        batches.append(required_signals)
    if optional_signals:
        batches.append(optional_signals)
    return batches


def _system_signal_updates(signals):
    return [signal for signal in (signals or []) if signal.get("signal_key")]


def _store_recap_memories_for_signal_batch(signal_batch, posts, channel_id):
    body = "\n\n".join((post or "").strip() for post in (posts or []) if (post or "").strip())
    if not body:
        return None
    return upsert_war_recap_memory(
        signals=signal_batch,
        body=body,
        channel_id=channel_id,
        workflow="observation",
    )


def _build_system_signal_context(signal, channel_name):
    payload = signal.get("payload") or {}
    details = payload.get("details") or []
    lines = [
        "This is a standalone clan-wide system update about a new Elixir capability.",
        f"Post it for {channel_name}.",
        "Write exactly one Discord message. Do not split it into parts or a series.",
        "Write the full final Discord message yourself, including the subject line.",
        "For system updates, prefer starting with a bolded subject line as the first line.",
        "If you use a subject line, include an Elixir custom emoji in it using :emoji_name: shortcode syntax.",
        "If you use a subject line, do not restate that title again immediately after the first line.",
        "Do not mention hidden system mechanics or call it a system signal.",
        "Make it feel like a self-contained clan update from Elixir.",
        "",
        f"signal_type: {signal.get('type') or 'unknown'}",
        f"signal_key: {signal.get('signal_key') or 'unknown'}",
        f"title: {payload.get('title') or signal.get('title') or ''}",
        f"message: {payload.get('message') or signal.get('message') or ''}",
        f"audience: {payload.get('audience') or 'clan'}",
        f"capability_area: {payload.get('capability_area') or 'general'}",
    ]
    if details:
        lines.append("details:")
        lines.extend(f"- {detail}" for detail in details)
    return "\n".join(lines)


def _preauthored_system_signal_result(signal):
    payload = (signal or {}).get("payload") or {}
    content = (
        payload.get("discord_content")
        or payload.get("preauthored_discord_content")
        or signal.get("discord_content")
    )
    content = (content or "").strip()
    if not content:
        return None
    summary = (
        payload.get("title")
        or signal.get("title")
        or signal.get("signal_key")
        or "System update"
    )
    return {
        "event_type": "channel_update",
        "summary": summary,
        "content": content,
    }


async def _post_system_signal_updates(signals, clan, war):
    system_signals = _system_signal_updates(signals)
    if not system_signals:
        return
    for signal in system_signals:
        await _deliver_signal_group([signal], clan, war)


async def _publish_pending_system_signal_updates(*, seed_startup_signals: bool = False) -> int:
    if seed_startup_signals:
        await asyncio.to_thread(queue_startup_system_signals)
    pending = await asyncio.to_thread(db.list_pending_system_signals)
    if not pending:
        return 0
    await _post_system_signal_updates(pending, {}, {})
    return len(pending)


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
        result_payload = await _app._apply_member_refs_to_result(result_payload)
        posts = _app._entry_posts(result_payload)
        await _post_to_elixir(channel, result_payload)
        event_type = "poapkings_publish_failure" if error_detail else "poapkings_publish_success"
        channel_name = getattr(channel, "name", None)
        channel_kind = str(getattr(channel, "type", "text"))
        for index, post in enumerate(posts):
            await asyncio.to_thread(
                db.save_message,
                _channel_scope(channel),
                "assistant",
                post,
                summary=result_payload.get("summary") if index == 0 else None,
                channel_id=channel.id,
                channel_name=channel_name,
                channel_kind=channel_kind,
                workflow="poapkings-com",
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


def _query_or_default(label: str, fn, default):
    try:
        return fn()
    except Exception as exc:
        log.warning("ask-elixir insight data unavailable for %s: %s", label, exc)
        return default


def _summarize_member_rows(rows, *, name_key="name", value_builder=None, limit=5):
    summary = []
    for row in (rows or [])[:limit]:
        name = row.get(name_key) or row.get("current_name") or row.get("member_ref") or row.get("tag")
        if not name:
            continue
        value = value_builder(row) if value_builder else None
        summary.append(f"{name} ({value})" if value else str(name))
    return summary


def _build_ask_elixir_daily_insight_context(clan, war):
    hot_streaks = _query_or_default(
        "hot_streaks",
        lambda: db.get_members_on_hot_streak(min_streak=4) or [],
        [],
    )
    favourite_cards = _query_or_default(
        "favourite_cards",
        lambda: db.get_clan_favourite_card_counts(limit=10) or [],
        [],
    )
    overlooked = _query_or_default(
        "overlooked_cards",
        lambda: db.get_clan_overlooked_cards(min_owners=3, min_level=14, battle_days=14, limit=10) or [],
        [],
    )
    played_cards = _query_or_default(
        "played_cards",
        lambda: db.get_clan_recently_played_cards(days=14, limit=20) or [],
        [],
    )

    lines = [
        "Write one short daily fun fact for #ask-elixir that teaches members something about a Clash Royale card.",
        "Pick a card from the lists below and teach something useful: a matchup, an elixir trade, a counter, a synergy, a mechanic, or a hidden interaction.",
        "The card lists are just hooks to pick from — do not mention levels, collections, or who owns what.",
        "Focus on gameplay: what the card does well, what beats it, what combos with it, or a non-obvious trick.",
        "Vary your picks — sometimes from popular clan cards, sometimes from overlooked ones, sometimes from cards the clan plays a lot.",
        "Use a playful opener like 'Did you know?', 'Fun fact', or 'Elixir noticed something...'.",
        "Do NOT write about clan wars, River Race, fame, or war participation.",
        "Do NOT mention card levels, who has a card maxed, or collection stats.",
        "Keep it to 1-3 short sentences.",
        "Do not turn it into a recap, reminder, call to action, leadership note, or war order.",
        "If today's data does not support a genuinely interesting insight, return null.",
    ]
    if played_cards:
        lines.extend([
            "",
            "=== CARDS THE CLAN IS PLAYING RIGHT NOW ===",
            ", ".join(row["card_name"] for row in played_cards),
        ])
    if favourite_cards:
        lines.extend([
            "",
            "=== CARDS CLAN MEMBERS LOVE (FAVOURITES) ===",
            ", ".join(row["card_name"] for row in favourite_cards),
        ])
    if overlooked:
        lines.extend([
            "",
            "=== CARDS NOBODY IN THE CLAN IS PLAYING ===",
            ", ".join(row["card_name"] for row in overlooked),
        ])
    if hot_streaks:
        lines.extend([
            "",
            "=== MEMBERS ON HOT STREAKS ===",
            "\n".join(
                f"- {item}"
                for item in _summarize_member_rows(
                    hot_streaks,
                    value_builder=lambda row: f"{row.get('current_streak') or 0} straight wins",
                )
            ),
        ])
    return "\n".join(lines)


def _mark_delivered_signals(signals, *, today: str | None = None):
    for signal in signals or []:
        if signal.get("signal_key"):
            continue
        signal_date = signal.get("signal_date") or today or db.chicago_today()
        signal_type = signal.get("signal_log_type") or signal.get("type")
        if signal_type:
            db.mark_signal_sent(signal_type, signal_date)
        if signal.get("type") == "clan_birthday":
            db.mark_announcement_sent(signal_date, "clan_birthday", None)
        elif signal.get("type") == "join_anniversary":
            for member in signal.get("members") or []:
                tag = member.get("tag")
                if tag:
                    db.mark_announcement_sent(signal_date, "join_anniversary", tag)
        elif signal.get("type") == "member_birthday":
            for member in signal.get("members") or []:
                tag = member.get("tag")
                if tag:
                    db.mark_announcement_sent(signal_date, "birthday", tag)


def _persist_signal_detector_cursors(cursor_updates):
    for update in cursor_updates or []:
        db.upsert_signal_detector_cursor(
            update.get("detector_key") or "",
            update.get("scope_key") or "",
            cursor_text=update.get("cursor_text"),
            cursor_int=update.get("cursor_int"),
            metadata=update.get("metadata"),
        )


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
    for index, post in enumerate(channel_posts):
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            post,
            channel_id=channel.id,
            channel_name=getattr(channel, "name", None),
            channel_kind=str(channel.type),
            workflow="promotion",
            event_type="promotion_content_cycle" if index == 0 else "promotion_content_cycle_part",
        )
    runtime_status.mark_job_success("promotion_content_cycle", "website and Discord promotion content published")


async def _ask_elixir_daily_insight():
    runtime_status.mark_job_start("daily_clan_insight")
    try:
        channel_id = _get_singleton_channel_id("ask-elixir")
    except Exception as exc:
        runtime_status.mark_job_failure("daily_clan_insight", f"ask-elixir channel config error: {exc}")
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        runtime_status.mark_job_failure("daily_clan_insight", "ask-elixir channel not found")
        return

    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.error("Ask Elixir daily insight refresh failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("daily_clan_insight", f"refresh failed: {exc}")
        return

    if not clan.get("memberList"):
        runtime_status.mark_job_success("daily_clan_insight", "no member data")
        return

    recent_posts = await asyncio.to_thread(
        db.list_channel_messages,
        channel.id,
        10,
        "assistant",
    )
    channel_config = _channel_config_by_key("ask-elixir")
    memory_context = await asyncio.to_thread(
        build_subagent_memory_context,
        channel_config,
        signals=[],
    )
    context = await asyncio.to_thread(_build_ask_elixir_daily_insight_context, clan, war)

    try:
        result = await asyncio.to_thread(
            elixir_agent.generate_channel_update,
            channel_config["name"],
            channel_config["subagent_key"],
            context,
            recent_posts=recent_posts,
            memory_context=memory_context,
            leadership=False,
        )
    except Exception as exc:
        log.error("Ask Elixir daily insight generation failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("daily_clan_insight", f"generation failed: {exc}")
        return

    if result is None:
        runtime_status.mark_job_success("daily_clan_insight", "no fresh insight")
        return

    result = await _app._apply_member_refs_to_result(result)
    posts = _app._entry_posts(result)
    if not posts:
        runtime_status.mark_job_success("daily_clan_insight", "no fresh insight")
        return

    await _post_to_elixir(channel, result)
    channel_name = getattr(channel, "name", None)
    channel_kind = str(getattr(channel, "type", "text"))
    for index, post in enumerate(posts):
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            post,
            summary=result.get("summary") if index == 0 else None,
            channel_id=channel.id,
            channel_name=channel_name,
            channel_kind=channel_kind,
            workflow="ask-elixir",
            event_type="daily_clan_insight" if index == 0 else "daily_clan_insight_part",
            raw_json={"result": result, "context_kind": "daily_clan_insight"},
        )
    runtime_status.mark_job_success("daily_clan_insight", "daily insight published")

async def _clan_awareness_tick():
    """Recurring clan-awareness activity for non-war signals and routed clan-event outcomes."""
    runtime_status.mark_job_start("clan_awareness")

    try:
        await asyncio.to_thread(queue_startup_system_signals)

        # Run the clan-awareness tick — fetches data, snapshots, detects signals
        tick_result = await asyncio.to_thread(heartbeat.tick, include_war=False)
        if tick_result.clan.get("memberList"):
            _app._clear_cr_api_failure_alert_if_recovered()
        else:
            await _app._maybe_alert_cr_api_failure("clan awareness")
        signals = tick_result.signals

        if not signals:
            log.info("Clan awareness: no signals, nothing to post")
            runtime_status.mark_job_success("clan_awareness", "no signals")
            return

        log.info("Clan awareness: %d signals detected, routing outcomes", len(signals))

        # Use clan + war data fetched during heartbeat.tick()
        clan = tick_result.clan
        war = tick_result.war

        for signal in signals:
            await _deliver_signal_group([signal], clan, war)

        runtime_status.mark_job_success("clan_awareness", f"{len(signals)} signal(s) processed")

    except Exception as e:
        log.error("Clan awareness error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("clan_awareness", str(e))


async def _war_poll_tick():
    """Predictable hourly war ingest for live state and race-log storage."""
    runtime_status.mark_job_start("war_poll")
    try:
        ingest_result = await asyncio.to_thread(
            heartbeat.ingest_live_war_state,
            refresh_race_log=True,
        )
        war = (ingest_result or {}).get("war") or {}
        if war:
            _app._clear_cr_api_failure_alert_if_recovered()
        else:
            log.info("War poll: no live war data returned")
        detail = "war snapshot stored" if war else "no live war data"
        if ingest_result.get("race_log_refreshed"):
            detail = f"{detail}; river race log refreshed ({ingest_result.get('race_log_items', 0)} row(s) stored)"
        runtime_status.mark_job_success("war_poll", detail)
    except Exception as e:
        log.error("War poll error: %s", e, exc_info=True)
        await _app._maybe_alert_cr_api_failure("war poll")
        runtime_status.mark_job_failure("war_poll", str(e))


async def _war_awareness_tick():
    """Stored-war observer that routes River Race signals on a fixed cadence."""
    runtime_status.mark_job_start("war_awareness")
    try:
        detection_result = await asyncio.to_thread(
            heartbeat.detect_war_signals_from_storage,
        )
        signals = detection_result.signals

        if not signals:
            if detection_result.cursor_updates:
                await asyncio.to_thread(_persist_signal_detector_cursors, detection_result.cursor_updates)
            runtime_status.mark_job_success("war_awareness", "no war signals")
            return

        clan = detection_result.clan
        war = detection_result.war

        delivered_ok = True
        for signal_batch in _observation_signal_batches(signals):
            batch_ok = await _deliver_signal_group(signal_batch, clan, war)
            delivered_ok = delivered_ok and batch_ok

        if not delivered_ok:
            runtime_status.mark_job_failure("war_awareness", "one or more war signal batches failed")
            return

        if detection_result.cursor_updates:
            await asyncio.to_thread(_persist_signal_detector_cursors, detection_result.cursor_updates)

        runtime_status.mark_job_success("war_awareness", f"{len(signals)} war signal(s) processed")
    except Exception as e:
        log.error("War awareness error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("war_awareness", str(e))


# ── Site content for poapkings.com ────────────────────────────────────────────

SITE_DATA_HOUR = int(os.getenv("SITE_DATA_HOUR", "18"))       # 6pm Chicago
SITE_CONTENT_HOUR = int(os.getenv("SITE_CONTENT_HOUR", "18"))  # 6pm Chicago


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
WAR_POLL_MINUTE = int(os.getenv("WAR_POLL_MINUTE", "0"))
WAR_AWARENESS_MINUTE = int(os.getenv("WAR_AWARENESS_MINUTE", "5"))
PLAYER_INTEL_BATCH_SIZE = int(os.getenv("PLAYER_INTEL_BATCH_SIZE", "5"))
PLAYER_INTEL_STALE_HOURS = int(os.getenv("PLAYER_INTEL_STALE_HOURS", "1"))
PLAYER_INTEL_REQUEST_SPACING_SECONDS = float(os.getenv("PLAYER_INTEL_REQUEST_SPACING_SECONDS", "2.0"))
PLAYER_INTEL_REFRESH_JITTER_SECONDS = int(os.getenv("PLAYER_INTEL_REFRESH_JITTER_SECONDS", "900"))
CLANOPS_WEEKLY_REVIEW_DAY = os.getenv("CLANOPS_WEEKLY_REVIEW_DAY", "fri")
CLANOPS_WEEKLY_REVIEW_HOUR = int(os.getenv("CLANOPS_WEEKLY_REVIEW_HOUR", "19"))
WEEKLY_RECAP_DAY = os.getenv("WEEKLY_RECAP_DAY", "mon")
WEEKLY_RECAP_HOUR = int(os.getenv("WEEKLY_RECAP_HOUR", "9"))


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


async def _player_intel_refresh():
    """Refresh stored player profile and battle intelligence for a subset of active members."""
    runtime_status.mark_job_start("player_intel_refresh")
    try:
        clan = await asyncio.to_thread(cr_api.get_clan)
        _app._clear_cr_api_failure_alert_if_recovered()
    except Exception as e:
        log.error("Player intel refresh: clan fetch failed: %s", e)
        await _app._maybe_alert_cr_api_failure("player intel refresh")
        runtime_status.mark_job_failure("player_intel_refresh", f"clan fetch failed: {e}")
        return

    members = clan.get("memberList", [])
    if not members:
        log.info("Player intel refresh: no member data, skipping")
        runtime_status.mark_job_success("player_intel_refresh", "no member data")
        return

    await asyncio.to_thread(db.snapshot_members, members)
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
    progression_signals = []
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
                profile_signals = await asyncio.to_thread(db.snapshot_player_profile, profile)
                if isinstance(profile_signals, list) and profile_signals:
                    progression_signals.extend(profile_signals)
            battle_log = await asyncio.to_thread(cr_api.get_player_battle_log, tag)
            if battle_log is not None:
                battle_log_ok = True
            else:
                battle_log_failures += 1
            if battle_log:
                battle_signals = await asyncio.to_thread(db.snapshot_player_battlelog, tag, battle_log)
                if isinstance(battle_signals, list) and battle_signals:
                    progression_signals.extend(battle_signals)
            if profile_ok or battle_log_ok:
                refreshed += 1
            else:
                failed_targets += 1
            await asyncio.sleep(PLAYER_INTEL_REQUEST_SPACING_SECONDS)
        except Exception as e:
            processing_failures += 1
            failed_targets += 1
            log.warning("Player intel refresh failed for %s: %s", tag, e)

    for signal_batch in _progression_signal_batches(progression_signals):
        await _deliver_signal_group(signal_batch, clan, war)

    if profile_failures or battle_log_failures:
        await _app._maybe_alert_cr_api_failure("player intel refresh")

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


async def _clanops_weekly_review():
    runtime_status.mark_job_start("clanops_weekly_review")
    clanops_channels = prompts.discord_channels_by_workflow("clanops")
    if not clanops_channels:
        runtime_status.mark_job_failure("clanops_weekly_review", "no leadership channel configured")
        return

    target_config = clanops_channels[0]
    channel = bot.get_channel(target_config["id"])
    if not channel:
        runtime_status.mark_job_failure("clanops_weekly_review", "leadership channel not found")
        return

    clan = {}
    war = {}
    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.warning("ClanOps weekly review refresh failed: %s", exc)

    review_content = await asyncio.to_thread(_build_weekly_clanops_review, clan, war)
    if not review_content:
        runtime_status.mark_job_success("clanops_weekly_review", "no review content")
        return

    await _post_to_elixir(channel, {"content": review_content})
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        review_content,
        channel_id=channel.id,
        channel_name=getattr(channel, "name", None),
        channel_kind=str(channel.type),
        workflow="clanops",
        event_type="weekly_clanops_review",
    )
    await asyncio.to_thread(
        upsert_weekly_summary_memory,
        event_type="weekly_clanops_review",
        title="Weekly ClanOps Review",
        body=review_content,
        scope="leadership",
        tags=["weekly", "clanops", "review"],
        metadata={"channel_id": channel.id, "workflow": "clanops"},
    )
    runtime_status.mark_job_success("clanops_weekly_review", "weekly review posted")


async def _weekly_clan_recap():
    runtime_status.mark_job_start("weekly_clan_recap")
    try:
        recap_channel_id = _get_singleton_channel_id("weekly_digest")
    except Exception as exc:
        runtime_status.mark_job_failure("weekly_clan_recap", f"weekly digest channel config error: {exc}")
        return

    channel = bot.get_channel(recap_channel_id)
    if not channel:
        runtime_status.mark_job_failure("weekly_clan_recap", "weekly digest channel not found")
        return

    clan = {}
    war = {}
    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.warning("Weekly clan recap refresh failed: %s", exc)

    recap_context = await asyncio.to_thread(_build_weekly_clan_recap_context, clan, war)
    recent_posts = await asyncio.to_thread(db.list_channel_messages, recap_channel_id, 5, "assistant")
    previous_message = _strip_weekly_recap_header(recent_posts[-1]["content"] if recent_posts else "")
    recap_text = await asyncio.to_thread(
        elixir_agent.generate_weekly_digest,
        recap_context,
        previous_message,
    )
    if not recap_text:
        runtime_status.mark_job_success("weekly_clan_recap", "no recap generated")
        return
    recap_post = _format_weekly_recap_post(recap_text)

    try:
        await _post_to_elixir(channel, {"content": recap_post})
    except discord.Forbidden as exc:
        detail = f"missing Discord permissions in #{getattr(channel, 'name', 'unknown')}"
        runtime_status.mark_job_failure("weekly_clan_recap", detail)
        raise RuntimeError(f"weekly recap post failed: {detail}") from exc
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        recap_post,
        channel_id=channel.id,
        channel_name=getattr(channel, "name", None),
        channel_kind=str(channel.type),
        workflow="announcements",
        event_type="weekly_clan_recap",
    )
    await asyncio.to_thread(
        upsert_weekly_summary_memory,
        event_type="weekly_clan_recap",
        title="Weekly Clan Recap",
        body=recap_post,
        scope="public",
        tags=["weekly", "recap", "clan-history"],
        metadata={"channel_id": channel.id, "workflow": "announcements"},
    )
    if poap_kings_site.site_enabled():
        members_payload = {
            "members": {
                "title": "Weekly Recap",
                "message": recap_text,
                "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "weekly_clan_recap",
            }
        }
        try:
            publish_result = await asyncio.to_thread(
                _publish_poap_kings_site_or_raise,
                members_payload,
                "Elixir POAP KINGS weekly recap sync",
            )
            publish_result = _normalize_poap_kings_publish_result(
                publish_result,
                members_payload,
            )
            await _notify_poapkings_publish("weekly-recap", publish_result=publish_result)
        except Exception as exc:
            log.error("Weekly recap site sync failed: %s", exc, exc_info=True)
            await _notify_poapkings_publish("weekly-recap", error_detail=str(exc))
            runtime_status.mark_job_failure("weekly_clan_recap", f"site sync failed: {exc}")
            return
    runtime_status.mark_job_success("weekly_clan_recap", "weekly recap posted")


# ── Database maintenance ─────────────────────────────────────────────────────


def _format_size(size_bytes):
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes / 1_073_741_824:.2f} GB"
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1_024:
        return f"{size_bytes / 1_024:.1f} KB"
    return f"{size_bytes} B"


def _build_maintenance_report(size_before, size_after, purge_stats, backup_result=None, pruned_count=0):
    freed = size_before - size_after
    pct = (freed / size_before * 100) if size_before > 0 else 0
    rows_purged = sum(purge_stats.values())

    lines = [
        "**Weekly Database Maintenance**",
        "",
    ]

    # Backup section.
    if backup_result is not None:
        if backup_result["ok"]:
            compressed_mb = backup_result["size_compressed"] / 1_048_576
            original_mb = backup_result["size_original"] / 1_048_576
            lines.append(f"**Backup:** {original_mb:.1f} MB -> {compressed_mb:.1f} MB compressed")
            if pruned_count > 0:
                lines.append(f"  Pruned {pruned_count} old backup(s)")
        else:
            lines.append(f"**Backup: FAILED** — {backup_result.get('error', 'unknown error')}")
        lines.append("")

    lines += [
        f"**Before:** {_format_size(size_before)}",
        f"**After:** {_format_size(size_after)}",
        f"**Freed:** {_format_size(freed)} ({pct:.0f}%)",
        "",
    ]

    if rows_purged > 0:
        lines.append(f"**{rows_purged:,} expired rows** removed:")
        for table, count in purge_stats.items():
            if count > 0:
                lines.append(f"  {table}: {count:,}")
    else:
        lines.append("No expired rows to remove this cycle.")

    return "\n".join(lines)


async def _daily_quiz_post():
    """Post the daily Elixir University quiz question."""
    from modules.card_training.views import CARD_TRAINING_CHANNEL_ID, post_daily_question

    runtime_status.mark_job_start("daily_quiz")

    if not CARD_TRAINING_CHANNEL_ID:
        runtime_status.mark_job_failure("daily_quiz", "CARD_TRAINING_CHANNEL_ID not configured for #card-quiz")
        return

    channel = bot.get_channel(CARD_TRAINING_CHANNEL_ID)
    if not channel:
        runtime_status.mark_job_failure("daily_quiz", f"channel {CARD_TRAINING_CHANNEL_ID} not found")
        return

    try:
        message = await post_daily_question(channel)
        if message:
            runtime_status.mark_job_success("daily_quiz", f"posted daily question (msg {message.id})")
            log.info("Daily quiz posted to #card-quiz (msg %s)", message.id)
        else:
            runtime_status.mark_job_failure("daily_quiz", "no question generated (card catalog may be empty)")
    except Exception as exc:
        log.error("Daily quiz post failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("daily_quiz", str(exc))


async def _card_catalog_sync():
    """Sync the Clash Royale card catalog from the API."""
    runtime_status.mark_job_start("card_catalog_sync")
    try:
        api_response = await asyncio.to_thread(cr_api.get_cards)
        if not api_response:
            runtime_status.mark_job_failure("card_catalog_sync", "API returned None")
            return
        count = await asyncio.to_thread(db.sync_card_catalog, api_response)
        runtime_status.mark_job_success("card_catalog_sync", f"synced {count} cards")
        log.info("Card catalog sync complete: %d cards", count)
    except Exception as exc:
        log.error("Card catalog sync failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("card_catalog_sync", str(exc))


async def _db_maintenance_cycle():
    from scripts.backup_db import create_backup, prune_backups

    runtime_status.mark_job_start("db_maintenance")

    try:
        channel_id = _get_singleton_channel_id("leader-lounge")
    except Exception as exc:
        runtime_status.mark_job_failure("db_maintenance", f"leader-lounge channel config error: {exc}")
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        runtime_status.mark_job_failure("db_maintenance", "leader-lounge channel not found")
        return

    try:
        db_path = db.DB_PATH
        size_before = os.path.getsize(db_path)

        # 1. Backup before any destructive operations.
        backup_result = await asyncio.to_thread(create_backup)
        pruned = await asyncio.to_thread(prune_backups) if backup_result["ok"] else []
        if not backup_result["ok"]:
            log.error("DB backup failed: %s", backup_result["error"])

        # 2. Purge expired rows.
        purge_stats = await asyncio.to_thread(db.purge_old_data)

        # 3. VACUUM reclaims disk space; must run outside any transaction.
        def _vacuum():
            conn = db.get_connection()
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()

        await asyncio.to_thread(_vacuum)

        size_after = os.path.getsize(db_path)
        report = _build_maintenance_report(
            size_before, size_after, purge_stats,
            backup_result=backup_result,
            pruned_count=len(pruned),
        )

        await _post_to_elixir(channel, {"content": report})
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            report,
            channel_id=channel.id,
            channel_name=getattr(channel, "name", None),
            channel_kind=str(channel.type),
            workflow="clanops",
            event_type="db_maintenance",
        )
        runtime_status.mark_job_success("db_maintenance", f"freed {_format_size(size_before - size_after)}")
    except Exception as exc:
        log.error("DB maintenance failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("db_maintenance", str(exc))


# ── Tournament watch ──────────────────────────────────────────────────────────

TOURNAMENT_POLL_MINUTES = int(os.getenv("TOURNAMENT_POLL_MINUTES", "5"))
TOURNAMENT_BATTLE_LOG_SPACING_SECONDS = 0.5
_TOURNAMENT_JOB_ID = "tournament-watch"


async def _tournament_watch_tick():
    """Poll the active tournament for standings and capture participant battle logs."""
    tournament = await asyncio.to_thread(db.get_active_tournament)
    if not tournament:
        stop_tournament_watch()
        return

    tag = tournament["tournament_tag"]
    tournament_id = tournament["tournament_id"]
    runtime_status.mark_job_start("tournament_watch")

    try:
        api_data = await asyncio.to_thread(cr_api.get_tournament, tag)
        if api_data is None:
            log.warning("Tournament watch: API returned None for %s", tag)
            runtime_status.mark_job_failure("tournament_watch", f"API returned None for {tag}")
            return

        poll_result = await asyncio.to_thread(db.poll_tournament, tag, api_data)
        participants = poll_result.get("participants") or []
        live_signals = poll_result.get("live_signals") or []

        # Capture battle logs when tournament is active or just ended
        api_status = api_data.get("status") or ""
        battles_captured = 0
        if api_status in ("inProgress", "ended"):
            tournament_tag_with_hash = f"#{tag.lstrip('#')}"
            for p in participants:
                p_tag = p["player_tag"]
                try:
                    battle_log = await asyncio.to_thread(cr_api.get_player_battle_log, p_tag)
                    if battle_log:
                        # Store tournament battles in dedicated table
                        for battle in battle_log:
                            if battle.get("tournamentTag") == tournament_tag_with_hash:
                                inserted = await asyncio.to_thread(
                                    db.store_tournament_battle, tournament_id, battle
                                )
                                if inserted:
                                    battles_captured += 1
                        # Also feed through existing battle log pipeline
                        await asyncio.to_thread(db.snapshot_player_battlelog, p_tag, battle_log)
                except Exception as e:
                    log.warning("Tournament watch: battle log failed for %s: %s", p_tag, e)
                await asyncio.sleep(TOURNAMENT_BATTLE_LOG_SPACING_SECONDS)

        # Handle tournament end
        if api_status == "ended" and tournament["status"] != "ended":
            await asyncio.to_thread(db.finalize_tournament, tag, api_data)
            log.info("Tournament %s ended — generating recap", tag)
            await _tournament_recap(tag)
            stop_tournament_watch()

        # Deliver live signals
        for signal in live_signals:
            try:
                await _deliver_signal_group([signal], {}, {})
            except Exception as e:
                log.warning("Tournament watch: signal delivery failed: %s", e)

        summary = f"poll #{tournament['poll_count'] + 1}, {len(participants)} participants, {battles_captured} new battles"
        if api_status == "ended":
            summary += " [ENDED]"
        runtime_status.mark_job_success("tournament_watch", summary)

    except Exception as exc:
        log.error("Tournament watch failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("tournament_watch", str(exc))


async def _tournament_recap(tournament_tag: str):
    """Generate and post a tournament recap to #clan-events."""
    try:
        context = await asyncio.to_thread(db.build_tournament_recap_context, tournament_tag)
        if not context:
            log.warning("Tournament recap: no context for %s", tournament_tag)
            return

        recap_text = elixir_agent.generate_tournament_recap(context)
        if not recap_text:
            log.warning("Tournament recap: LLM returned empty for %s", tournament_tag)
            return

        tournament = await asyncio.to_thread(db.get_tournament_by_tag, tournament_tag)
        tournament_name = (tournament or {}).get("name") or tournament_tag
        title = f"**Tournament Recap | {tournament_name}**"
        full_post = f"{title}\n\n{recap_text}"

        channel_id = _get_singleton_channel_id("clan-events")
        if not channel_id:
            log.error("Tournament recap: #clan-events channel not configured")
            return
        channel = bot.get_channel(channel_id)
        if not channel:
            log.error("Tournament recap: could not resolve channel %s", channel_id)
            return

        await _post_to_elixir(channel, {"content": full_post})
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            full_post,
            channel_id=channel.id,
            channel_name=getattr(channel, "name", None),
            channel_kind=str(channel.type),
            workflow="channel_update",
            event_type="tournament_recap",
        )

        # Update recap_posted_at
        from db import get_connection, _utcnow
        def _mark_recap_posted():
            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE tournaments SET recap_posted_at = ? WHERE tournament_tag = ?",
                    (_utcnow(), tournament_tag.lstrip("#").upper()),
                )
                conn.commit()
            finally:
                conn.close()
        await asyncio.to_thread(_mark_recap_posted)

        log.info("Tournament recap posted for %s", tournament_tag)
    except Exception as exc:
        log.error("Tournament recap generation failed: %s", exc, exc_info=True)


def start_tournament_watch():
    """Add the tournament watch job to the scheduler."""
    try:
        _app.scheduler.remove_job(_TOURNAMENT_JOB_ID)
    except Exception:
        pass

    def _job_runner():
        bot.loop.call_soon_threadsafe(
            lambda: bot.loop.create_task(_tournament_watch_tick())
        )

    _app.scheduler.add_job(
        _job_runner,
        "interval",
        id=_TOURNAMENT_JOB_ID,
        minutes=TOURNAMENT_POLL_MINUTES,
        max_instances=1,
        coalesce=True,
    )
    log.info("Tournament watch started (every %d minutes)", TOURNAMENT_POLL_MINUTES)


def stop_tournament_watch():
    """Remove the tournament watch job from the scheduler."""
    try:
        _app.scheduler.remove_job(_TOURNAMENT_JOB_ID)
        log.info("Tournament watch stopped")
    except Exception:
        pass


# ── Bot events ────────────────────────────────────────────────────────────────

__all__ = [
    name for name in globals()
    if not name.startswith("__") and name not in {"_post_to_elixir", "_load_live_clan_context", "_build_weekly_clanops_review", "_build_weekly_clan_recap_context"}
]
