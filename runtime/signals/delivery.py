"""Signal delivery entrypoints."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import db
import elixir_agent
from runtime.channel_subagents import (
    SEASON_AWARDS_SIGNAL_TYPES,
    build_subagent_memory_context,
    is_leadership_only_signal,
    signal_source_key,
)
from runtime.helpers import _channel_scope

log = logging.getLogger("elixir")

ARENA_RELAY_COOLDOWN_HOURS = 18
ARENA_RELAY_MAX_COPY_CHARS = 240
ARENA_RELAY_MAX_NUDGE_ACTIONS = 3
WAR_NUDGE_SIGNAL_TYPES = {
    "war_battle_phase_active",
    "war_battle_day_started",
    "war_battle_day_live_update",
    "war_battle_day_final_hours",
    "war_final_battle_day",
}


def _clip_relay_copy(text: str, limit: int = ARENA_RELAY_MAX_COPY_CHARS) -> str:
    body = " ".join((text or "").split())
    if len(body) <= limit:
        return body
    return body[: max(0, limit - 3)].rstrip() + "..."


def _ordinal(value) -> str | None:
    if not isinstance(value, int) or value <= 0:
        return None
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _signal_names(signals: list[dict], limit: int = 4) -> list[str]:
    names = []
    for signal in signals or []:
        name = str(signal.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def _arena_relay_copy(signals: list[dict]) -> tuple[str, str, str] | None:
    signals = signals or []
    types = {signal.get("type") for signal in signals}
    primary = signals[0] if signals else {}

    if types & {"war_practice_phase_active", "war_practice_day_started", "war_final_practice_day"}:
        return (
            "Practice days are live. Please set boat defenses early so they are ready before battle days start.",
            "boat_defense_setup",
            "Practice timing is the main in-game action before battle days.",
        )

    if types & {"war_final_battle_day", "war_battle_day_final_hours"}:
        return (
            "Final battle day: use any remaining war decks today. Every deck helps lock River Chest rewards and finish the race strong.",
            "war_participation",
            "Final-day reminders are one of the most useful in-game relay moments.",
        )

    if types & {"war_battle_phase_active", "war_battle_day_started"}:
        return (
            "Battle day is live. Please use all 4 war decks when you can; every attack helps keep POAP KINGS moving.",
            "war_participation",
            "Battle-day start messages are useful for members who do not read Discord.",
        )

    if types & {"war_battle_day_live_update"}:
        rank = _ordinal(primary.get("race_rank") or primary.get("our_rank"))
        fame = primary.get("clan_fame") or primary.get("our_fame") or primary.get("fame")
        if rank and isinstance(fame, int):
            copy = f"War check: POAP KINGS is {rank} with {fame:,} fame. Use remaining decks today; every attack keeps pressure on."
        elif rank:
            copy = f"War check: POAP KINGS is {rank}. Use remaining decks today; every attack keeps pressure on."
        else:
            copy = "War check: use remaining decks today if you can. Every attack keeps pressure on and helps the clan chest."
        return (copy, "war_participation", "Current war state is timely enough to relay into game chat.")

    if types & {"war_attacks_complete"}:
        names = _signal_names(signals)
        if names:
            named = ", ".join(names)
            copy = f"Props to {named} for using all 4 war decks today. If you still have decks, jump in and help finish strong."
        else:
            copy = "Props to everyone using all 4 war decks today. If you still have decks, jump in and help finish strong."
        return (copy, "war_recognition", "Recognition can reinforce the exact behavior leaders want repeated.")

    if types & {"war_week_complete", "war_completed"}:
        rank = _ordinal(primary.get("our_rank") or primary.get("race_rank"))
        fame = primary.get("our_fame") or primary.get("clan_fame") or primary.get("fame")
        if rank and isinstance(fame, int):
            copy = f"War week complete: POAP KINGS finished {rank} with {fame:,} fame. Thanks to everyone who used decks."
        elif rank:
            copy = f"War week complete: POAP KINGS finished {rank}. Thanks to everyone who used decks."
        else:
            copy = "War week complete. Thanks to everyone who used decks and helped keep POAP KINGS moving."
        return (copy, "war_recognition", "Week-end recognition is useful in game chat because many contributors are not in Discord.")

    if types & {"war_season_complete"}:
        return (
            "War season complete. Thanks to everyone who kept showing up and using decks for POAP KINGS.",
            "war_recognition",
            "Season-end recognition belongs where the full clan can see it.",
        )

    return None


def _build_arena_relay_result(signals: list[dict]) -> dict | None:
    relay = _arena_relay_copy(signals)
    if relay is None:
        return None
    copy, objective, reason = relay
    copy = _clip_relay_copy(copy)
    card = (
        "**R? 📣 In-game relay**\n"
        f"🎯 `{objective}`\n"
        "📋 Copy the next message into Clash Royale.\n"
        f"🧠 {reason}\n\n"
        "✅ done  ❌ decline  ↩️ reply with note"
    )
    return {
        "event_type": "war_relay_brief",
        "summary": f"In-game relay suggestion: {copy}",
        "content": [card, copy],
        "metadata": {
            "action_type": "in_game_relay",
            "objective": objective,
            "rationale": reason,
            "relay_copy": copy,
            "relay_target": "clash_royale_clan_chat",
            "copy_message_index": 1,
        },
    }


def _attach_leader_action_to_result(result: dict, action: dict) -> dict:
    if not action:
        return result
    action_id = action.get("action_id")
    if action_id:
        result["summary"] = f"Leader action R{action_id}: {result.get('summary') or action.get('prompt_text')}"
        content = result.get("content")
        if isinstance(content, list) and content:
            content[0] = str(content[0] or "").replace("**R? ", f"**R{action_id} ", 1)
        else:
            result["content"] = str(content or "").replace("**R? ", f"**R{action_id} ", 1)
    metadata = result.setdefault("metadata", {})
    metadata.update({
        "leader_action_id": action.get("action_id"),
        "leader_action_key": action.get("action_key"),
        "leader_action_status": action.get("status"),
    })
    return result


def _leader_action_member_name(member: dict) -> str:
    return (
        member.get("member_ref")
        or member.get("name")
        or member.get("player_name")
        or member.get("tag")
        or member.get("player_tag")
        or "member"
    )


def _format_leader_action_card(action: dict, *, title: str, prompt_text: str, rationale: str) -> str:
    action_id = action.get("action_id")
    objective = action.get("objective") or "leader_action"
    action_type = action.get("action_type") or ""
    icon = {
        "in_game_relay": "📣",
        "war_nudge_recommendation": "👋",
        "promotion_recommendation": "⬆️",
        "demotion_recommendation": "⬇️",
        "kick_recommendation": "🚪",
    }.get(action_type, "⚡")
    return (
        f"**R{action_id} {icon} {title}**\n"
        f"🎯 `{objective}`\n"
        "🛠️ Action\n"
        f"```text\n{prompt_text}\n```\n"
        f"🧠 {rationale}\n\n"
        "✅ done  ❌ decline  ↩️ reply with note"
    )


def _war_nudge_candidates(limit: int = ARENA_RELAY_MAX_NUDGE_ACTIONS) -> list[dict]:
    war_day = db.get_current_war_day_state() or {}
    if war_day.get("phase") != "battle":
        return []
    candidates = []
    for member in war_day.get("used_none") or []:
        tag = member.get("tag") or member.get("player_tag")
        if not tag:
            continue
        candidates.append({
            "tag": tag,
            "name": _leader_action_member_name(member),
            "war_day_key": war_day.get("war_day_key"),
            "phase_display": war_day.get("phase_display"),
            "time_left_text": war_day.get("time_left_text"),
            "race_completed": bool(war_day.get("race_completed")),
            "member": member,
        })
        if len(candidates) >= limit:
            break
    return candidates


def _parse_recorded_at(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _arena_relay_recently_posted(recent_posts: list[dict], *, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = current - timedelta(hours=ARENA_RELAY_COOLDOWN_HOURS)
    for post in recent_posts or []:
        recorded = _parse_recorded_at(post.get("recorded_at") or post.get("created_at"))
        if recorded and recorded >= cutoff:
            return True
    return False


def _facade():
    from runtime.jobs import _signals as facade

    return facade


def _runtime_app():
    from runtime import app as runtime_app

    return runtime_app


def _bot():
    return _runtime_app().bot


async def _deliver_signal_outcome(outcome, signals, clan, war):
    facade = _facade()
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

    channel_config = facade._channel_config_by_key(outcome["target_channel_key"])
    channel = _bot().get_channel(channel_config["id"])
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
    recent_posts = await asyncio.to_thread(db.list_channel_messages, channel_id, 10, "assistant")
    memory_context = await asyncio.to_thread(
        build_subagent_memory_context,
        channel_config,
        signals=signals,
    )

    from runtime.channel_subagents import TOURNAMENT_SIGNAL_TYPES, WAR_RECAP_SIGNAL_TYPES

    is_tournament_batch = bool(signals) and all(
        (s or {}).get("type") in TOURNAMENT_SIGNAL_TYPES for s in signals
    )
    is_war_recap_batch = bool(signals) and all(
        (s or {}).get("type") in WAR_RECAP_SIGNAL_TYPES for s in signals
    )
    is_season_awards_batch = bool(signals) and all(
        (s or {}).get("type") in SEASON_AWARDS_SIGNAL_TYPES for s in signals
    )
    if is_tournament_batch or is_war_recap_batch or is_season_awards_batch:
        context = None
    else:
        context = facade._build_outcome_context(outcome, signals, clan, war)

    preauthored_result = None
    if len(signals) == 1 and signals[0].get("signal_key"):
        preauthored_result = facade._preauthored_system_signal_result(signals[0])

    try:
        channel_name = getattr(channel, "name", None)
        if not isinstance(channel_name, str):
            channel_name = None
        channel_kind = getattr(channel, "type", None)
        if channel_kind is not None:
            channel_kind = str(channel_kind)

        if channel_config["subagent_key"] == "arena-relay":
            if _arena_relay_recently_posted(recent_posts):
                await asyncio.to_thread(
                    db.upsert_signal_outcome,
                    outcome["source_signal_key"],
                    outcome["source_signal_type"],
                    outcome["target_channel_key"],
                    outcome["target_channel_id"],
                    outcome["intent"],
                    required=outcome.get("required", True),
                    delivery_status="skipped",
                    payload={"signals": signals},
                    error_detail=f"arena_relay_cooldown:{ARENA_RELAY_COOLDOWN_HOURS}h",
                    mark_attempt=True,
                )
                return True
            result = _build_arena_relay_result(signals)
            if result is not None:
                metadata = result.get("metadata") if isinstance(result, dict) else {}
                baseline = await asyncio.to_thread(
                    db.build_leader_action_baseline,
                    action_type="in_game_relay",
                    signals=signals,
                )
                action = await asyncio.to_thread(
                    db.create_leader_action_recommendation,
                    action_type="in_game_relay",
                    objective=metadata.get("objective") or "war_participation",
                    prompt_text=metadata.get("relay_copy") or result.get("summary") or "",
                    rationale=metadata.get("rationale"),
                    target_channel_key=outcome["target_channel_key"],
                    target_channel_id=outcome["target_channel_id"],
                    source_signal_key=outcome["source_signal_key"],
                    source_signal_type=outcome["source_signal_type"],
                    baseline=baseline,
                )
                result = _attach_leader_action_to_result(result, action)
        elif preauthored_result is not None:
            result = preauthored_result
        elif is_tournament_batch:
            result = await asyncio.to_thread(
                elixir_agent.generate_tournament_update,
                signals,
                recent_posts=recent_posts,
                memory_context=memory_context,
            )
        elif is_war_recap_batch:
            result = await asyncio.to_thread(
                elixir_agent.generate_war_recap_update,
                signals,
                recent_posts=recent_posts,
                memory_context=memory_context,
            )
        elif is_season_awards_batch:
            result = await asyncio.to_thread(
                elixir_agent.generate_season_awards_post,
                signals,
                recent_posts=recent_posts,
                memory_context=memory_context,
            )
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

        app = _runtime_app()
        if result is None:
            await app._maybe_alert_llm_failure("channel update")
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

        app._clear_llm_failure_alert_if_recovered()
        metadata = result.get("metadata") if isinstance(result, dict) else None
        if isinstance(metadata, dict) and metadata.get("decision") == "no_post":
            reason = metadata.get("reason") or "unspecified"
            log.info(
                "channel_update no_post: channel=%s signal_type=%s signal_key=%s reason=%s",
                outcome["target_channel_key"],
                outcome["source_signal_type"],
                outcome["source_signal_key"],
                reason,
            )
            await asyncio.to_thread(
                db.upsert_signal_outcome,
                outcome["source_signal_key"],
                outcome["source_signal_type"],
                outcome["target_channel_key"],
                outcome["target_channel_id"],
                outcome["intent"],
                required=outcome.get("required", True),
                delivery_status="skipped",
                payload={"result": result, "signals": signals},
                error_detail=f"llm_no_post: {reason}",
                mark_attempt=True,
            )
            return True

        posts = app._entry_posts(result)
        sent_messages = await facade._post_to_elixir(channel, result)
        if not isinstance(sent_messages, list):
            sent_messages = []
        if channel_config["subagent_key"] == "arena-relay":
            metadata = result.get("metadata") if isinstance(result, dict) else {}
            action_id = metadata.get("leader_action_id") if isinstance(metadata, dict) else None
            first_message = sent_messages[0] if sent_messages else None
            first_message_id = getattr(first_message, "id", None)
            if action_id and first_message_id is not None:
                await asyncio.to_thread(
                    db.update_leader_action_message,
                    action_id,
                    source_message_id=first_message_id,
                )
            copy_index = metadata.get("copy_message_index") if isinstance(metadata, dict) else None
            if action_id and isinstance(copy_index, int) and copy_index < len(sent_messages):
                copy_message_id = getattr(sent_messages[copy_index], "id", None)
                if copy_message_id is not None:
                    await asyncio.to_thread(
                        db.update_leader_action_copy_message,
                        action_id,
                        copy_message_id=copy_message_id,
                    )
        if (
            channel_config["subagent_key"] == "clan-events"
            and any(s.get("type") == "member_join" for s in signals)
        ):
            from modules.poap_kings import site as _pk_site

            if _pk_site.site_enabled():
                from runtime.jobs._site import _notify_poapkings_publish, _publish_member_join_blog_post

                join_body = "\n\n".join(posts)
                try:
                    blog_result = await asyncio.to_thread(
                        _publish_member_join_blog_post,
                        signals,
                        join_body,
                        result.get("summary"),
                    )
                    await _notify_poapkings_publish("member-join-blog", publish_result=blog_result)
                except Exception as exc:
                    log.error("Member join blog post publish failed: %s", exc, exc_info=True)
                    await _notify_poapkings_publish("member-join-blog", error_detail=str(exc))

        summary = result.get("summary")
        event_type = result.get("event_type") or outcome["intent"]
        for index, post in enumerate(posts):
            sent_message = sent_messages[index] if index < len(sent_messages) else None
            sent_message_id = getattr(sent_message, "id", None)
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
                discord_message_id=sent_message_id,
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
        if channel_config["subagent_key"] != "arena-relay":
            await asyncio.to_thread(
                facade.maybe_upsert_signal_memory,
                source_signal_key=outcome["source_signal_key"],
                signal_type=(signals[0].get("type") or outcome["source_signal_type"]),
                body=body,
                outcome=outcome,
                signals=signals,
            )

        from agent.memory_tasks import store_observation_facts

        await asyncio.to_thread(store_observation_facts, signals, channel_id)
        if channel_config["subagent_key"] == "river-race" and facade._signal_group_needs_recap_memory(signals):
            await asyncio.to_thread(facade._store_recap_memories_for_signal_batch, signals, posts, channel_id)

        from runtime.helpers._common import _safe_create_task

        if channel_config["subagent_key"] != "arena-relay":
            _safe_create_task(
                facade._post_signal_memory(body, outcome, signals),
                name="signal_memory",
            )
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
        log.error(
            "Signal outcome delivery failed for %s/%s: %s",
            outcome["source_signal_key"],
            outcome["target_channel_key"],
            exc,
            exc_info=True,
        )
        return False


async def _deliver_signal_group(signals, clan, war):
    facade = _facade()
    outcomes = facade.plan_signal_outcomes(signals)
    if not outcomes:
        return False
    results = []
    for outcome in outcomes:
        delivered = await facade._deliver_signal_outcome(outcome, signals, clan, war)
        results.append(delivered)
    rows = await asyncio.to_thread(db.list_signal_outcomes, outcomes[0]["source_signal_key"])
    if rows and all(row.get("delivery_status") in {"delivered", "skipped"} for row in rows):
        await facade._mark_signal_group_completed(signals)
        return True
    return all(results)


async def _deliver_arena_relay_sidecars(signals, clan, war) -> int:
    facade = _facade()
    delivered = 0
    for outcome in facade.plan_signal_outcomes(signals or []):
        if outcome.get("target_channel_key") != "arena-relay":
            continue
        ok = await facade._deliver_signal_outcome(outcome, signals, clan, war)
        if ok:
            delivered += 1
    delivered += await _deliver_war_nudge_sidecars(signals)
    return delivered


async def _deliver_war_nudge_sidecars(signals) -> int:
    types = {signal.get("type") for signal in signals or []}
    if not (types & WAR_NUDGE_SIGNAL_TYPES):
        return 0
    try:
        channel_config = _facade()._channel_config_by_key("arena-relay")
    except Exception:
        log.info("war nudge sidecar skipped: arena-relay unavailable", exc_info=True)
        return 0
    app = _runtime_app()
    channel = app.bot.get_channel(channel_config["id"])
    if channel is None:
        log.warning("war nudge sidecar skipped: arena-relay channel not found")
        return 0

    candidates = await asyncio.to_thread(_war_nudge_candidates)
    posted = 0
    channel_name = getattr(channel, "name", "arena-relay")
    channel_kind = getattr(channel, "type", "text")
    if channel_kind is not None:
        channel_kind = str(channel_kind)

    for candidate in candidates:
        name = candidate["name"]
        tag = candidate["tag"]
        prompt_text = f"Nudge {name} to use war decks today."
        if candidate.get("race_completed"):
            rationale = (
                f"{name} has not used war decks on {candidate.get('phase_display') or 'battle day'}; "
                "the race is finished, so this is for personal River Chest rewards."
            )
        else:
            rationale = (
                f"{name} has not used war decks on {candidate.get('phase_display') or 'battle day'}"
                + (f" with {candidate['time_left_text']} left" if candidate.get("time_left_text") else "")
                + "."
            )
        baseline = await asyncio.to_thread(
            db.build_leader_action_baseline,
            action_type="war_nudge_recommendation",
            target_player_tag=tag,
        )
        action = await asyncio.to_thread(
            db.create_leader_action_recommendation,
            action_type="war_nudge_recommendation",
            objective="war_participation",
            prompt_text=prompt_text,
            rationale=rationale,
            target_channel_key="arena-relay",
            target_channel_id=channel_config["id"],
            target_player_tag=tag,
            target_player_name=name,
            source_signal_key=f"war_nudge:{candidate.get('war_day_key') or 'unknown'}:{tag}",
            source_signal_type="war_nudge_recommendation",
            baseline=baseline,
        )
        if not action or action.get("source_message_id"):
            continue
        content = _format_leader_action_card(
            action,
            title="war nudge recommendation",
            prompt_text=prompt_text,
            rationale=rationale,
        )
        sent_messages = await app._post_to_elixir(channel, {"content": content})
        if not isinstance(sent_messages, list):
            sent_messages = []
        first_message = sent_messages[0] if sent_messages else None
        first_message_id = getattr(first_message, "id", None)
        if first_message_id is not None:
            await asyncio.to_thread(
                db.update_leader_action_message,
                action["action_id"],
                source_message_id=first_message_id,
            )
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            content,
            summary=f"Leader action R{action.get('action_id')}: war nudge recommendation",
            channel_id=channel_config["id"],
            channel_name=channel_name,
            channel_kind=channel_kind,
            workflow="arena-relay",
            event_type="war_nudge_recommendation",
            discord_message_id=first_message_id,
            raw_json={"leader_action": action},
        )
        posted += 1
    return posted


async def _deliver_awareness_post(post: dict, signals: list[dict]) -> bool:
    facade = _facade()
    from runtime.situation import CHANNEL_LANES

    channel_key = (post.get("channel") or "").strip()
    if channel_key not in CHANNEL_LANES:
        log.warning("awareness post rejected: unknown channel %r", channel_key)
        return False
    leads_with = (post.get("leads_with") or "").strip()
    if leads_with and leads_with not in CHANNEL_LANES[channel_key]:
        log.warning(
            "awareness post rejected: leads_with=%r not allowed on channel=%r (allowed=%s)",
            leads_with,
            channel_key,
            sorted(CHANNEL_LANES[channel_key]),
        )
        return False

    covers = list(post.get("covers_signal_keys") or [])
    if signals and not covers:
        log.warning(
            "awareness post rejected: empty covers_signal_keys channel=%r despite %d input signal(s)",
            channel_key,
            len(signals),
        )
        return False

    if covers:
        covers_set = set(covers)
        for sig in signals or []:
            if signal_source_key(sig) not in covers_set:
                continue
            if is_leadership_only_signal(sig) and channel_key != "leader-lounge":
                log.warning(
                    "awareness post rejected: leadership-only signal %s routed to public channel %s",
                    signal_source_key(sig),
                    channel_key,
                )
                return False

    try:
        channel_config = facade._channel_config_by_key(channel_key)
    except RuntimeError:
        log.warning("awareness post rejected: channel %r not configured", channel_key)
        return False
    channel = _bot().get_channel(channel_config["id"])
    if not channel:
        log.warning("awareness post rejected: channel %r not found in Discord", channel_key)
        return False

    content = post.get("content")
    if not content:
        log.warning("awareness post on %r had empty content", channel_key)
        return False

    result = {
        "event_type": post.get("event_type") or "awareness_update",
        "summary": post.get("summary"),
        "content": content,
    }
    try:
        await facade._post_to_elixir(channel, result)
    except Exception:
        log.error("awareness post send failed channel=%r", channel_key, exc_info=True)
        return False

    app = _runtime_app()
    posts = app._entry_posts(result)
    channel_id = channel_config["id"]
    channel_name = getattr(channel, "name", None)
    if not isinstance(channel_name, str):
        channel_name = None
    channel_kind = getattr(channel, "type", None)
    if channel_kind is not None:
        channel_kind = str(channel_kind)
    summary = result.get("summary")
    event_type = result.get("event_type")
    for index, body_part in enumerate(posts):
        post_summary = summary if index == 0 else f"{summary} ({index + 1}/{len(posts)})" if summary else None
        post_event_type = event_type if index == 0 else f"{event_type}_part"
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            body_part,
            summary=post_summary,
            channel_id=channel_id,
            channel_name=channel_name,
            channel_kind=channel_kind,
            workflow=channel_config["subagent_key"],
            event_type=post_event_type,
            raw_json={
                "source": "awareness_loop",
                "leads_with": post.get("leads_with"),
                "covers_signal_keys": post.get("covers_signal_keys") or [],
                "result": result,
            },
        )

    body = "\n\n".join(posts)
    for signal in signals or []:
        sig_key = signal_source_key(signal)
        if not sig_key or sig_key not in covers:
            continue
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            sig_key,
            signal.get("type") or "awareness_signal",
            channel_key,
            channel_id,
            event_type,
            required=True,
            delivery_status="delivered",
            payload={"result": result, "signals": [signal]},
            mark_attempt=True,
            delivered=True,
        )

    from runtime.helpers._common import _safe_create_task

    fake_outcome = {
        "intent": event_type,
        "target_channel_key": channel_key,
        "target_channel_id": channel_id,
        "source_signal_key": (covers[0] if covers else "awareness_loop"),
    }
    _safe_create_task(
        facade._post_signal_memory(body, fake_outcome, signals or []),
        name="awareness_signal_memory",
    )
    return True


async def _deliver_awareness_post_plan(plan: dict, signals: list[dict]) -> dict:
    facade = _facade()
    posts = (plan or {}).get("posts") or []
    delivered = 0
    rejected = 0
    covered: set[str] = set()
    attempted: set[str] = set()
    for post in posts:
        post_keys = {str(key) for key in (post.get("covers_signal_keys") or []) if key}
        attempted |= post_keys
        ok = await facade._deliver_awareness_post(post, signals or [])
        if ok:
            delivered += 1
            covered |= post_keys
        else:
            rejected += 1

    if covered:
        covered_signals = [
            s for s in (signals or [])
            if signal_source_key(s) in covered
        ]
        if covered_signals:
            await facade._mark_signal_group_completed(covered_signals)

    return {
        "delivered": delivered,
        "rejected": rejected,
        "covered_signal_keys": covered,
        "attempted_signal_keys": attempted,
    }


async def _deliver_signal_group_via_awareness(signals, clan, war, *, workflow: str | None = None) -> bool:
    facade = _facade()
    from heartbeat import HeartbeatTickResult
    from runtime.situation import build_situation, situation_is_quiet

    bundle = HeartbeatTickResult(signals=signals or [], clan=clan or {}, war=war or {})
    situation = build_situation(bundle)

    if situation_is_quiet(situation):
        log.info("awareness loop: quiet tick, skipping agent call")
        # A quiet tick means nothing here is worth posting; mark the inputs
        # handled so they don't re-emit every cycle. (Signals only get marked
        # completed after a confirmed post/skip — not speculatively up front —
        # so a planned post that fails delivery is retried, not burned.)
        if signals:
            await facade._mark_signal_group_completed(signals)
        return True

    tool_stats: dict = {}
    try:
        plan = await asyncio.to_thread(elixir_agent.run_awareness_tick, situation, tool_stats=tool_stats)
    except Exception as exc:
        log.error("awareness loop run_awareness_tick failed: %s", exc, exc_info=True)
        plan = None

    if plan is None:
        log.warning("awareness loop returned no plan; falling back to per-signal delivery")
        return await facade._deliver_signal_group(signals, clan, war)

    report = await facade._deliver_awareness_post_plan(plan, signals)
    relay_sidecars = await facade._deliver_arena_relay_sidecars(signals, clan, war)

    hard_required_keys = {hp.get("signal_key") for hp in (situation.get("hard_post_signals") or [])}
    covered_keys = report["covered_signal_keys"]
    uncovered = [
        signal for signal in (signals or [])
        if signal_source_key(signal) in hard_required_keys
        and signal_source_key(signal) not in covered_keys
    ]
    fallback_failed_keys: set[str] = set()
    all_ok = True
    if uncovered:
        log.warning(
            "awareness loop: %d hard-post-floor signal(s) uncovered; falling back per-signal",
            len(uncovered),
        )
        for signal in uncovered:
            ok = await facade._deliver_signal_group([signal], clan, war)
            if not ok:
                fallback_failed_keys.add(signal_source_key(signal))
            all_ok = all_ok and ok

    attempted_keys = report.get("attempted_signal_keys") or set()
    # Soft signals the agent planned to post but whose delivery failed. Do NOT
    # mark these completed — leaving them unmarked lets the next tick retry,
    # instead of permanently burning a post that never reached Discord.
    post_failed_keys = {
        signal_source_key(signal)
        for signal in (signals or [])
        if signal_source_key(signal) in attempted_keys
        and signal_source_key(signal) not in covered_keys
        and signal_source_key(signal) not in hard_required_keys
    }

    considered_skipped = [
        signal for signal in (signals or [])
        if signal_source_key(signal) not in covered_keys
        and signal_source_key(signal) not in fallback_failed_keys
        and signal_source_key(signal) not in hard_required_keys
        and signal_source_key(signal) not in post_failed_keys
    ]
    if considered_skipped:
        await facade._mark_signal_group_completed(considered_skipped)

    revisit_keys_seen = set(covered_keys)
    revisit_keys_seen.update(signal_source_key(s) for s in (considered_skipped or []))
    revisit_keys_seen.update(signal_source_key(s) for s in (uncovered or []))
    if revisit_keys_seen:
        try:
            await asyncio.to_thread(db.mark_revisited, sorted(revisit_keys_seen))
        except Exception:
            log.warning("mark_revisited failed", exc_info=True)

    log.info(
        "awareness_tick_result workflow=%r delivered=%d rejected=%d covered=%d considered_skipped=%d "
        "hard_fallback=%d hard_fallback_failed=%d relay_sidecars=%d signals_in=%d skipped_reason=%r",
        workflow,
        report["delivered"],
        report["rejected"],
        len(covered_keys),
        len(considered_skipped),
        len(uncovered),
        len(fallback_failed_keys),
        relay_sidecars,
        len(signals or []),
        (plan or {}).get("skipped_reason"),
    )

    uncovered_keys = {signal_source_key(s) for s in uncovered}
    signal_outcomes: list[dict] = []
    for signal in (signals or []):
        key = signal_source_key(signal)
        if key in covered_keys:
            status = "covered"
        elif key in fallback_failed_keys:
            status = "fallback_failed"
        elif key in uncovered_keys:
            status = "fallback"
        elif key in post_failed_keys:
            status = "post_failed"
        else:
            status = "skipped"
        signal_outcomes.append({
            "signal_key": key,
            "signal_type": signal.get("type") or "",
            "status": status,
        })
    try:
        await asyncio.to_thread(
            db.record_awareness_tick,
            workflow=workflow,
            signals_in=len(signals or []),
            posts_delivered=report["delivered"],
            posts_rejected=report["rejected"],
            covered_keys=len(covered_keys),
            considered_skipped=len(considered_skipped),
            hard_fallback=len(uncovered),
            hard_fallback_failed=len(fallback_failed_keys),
            all_ok=all_ok,
            skipped_reason=(plan or {}).get("skipped_reason"),
            signal_outcomes=signal_outcomes,
            write_calls_issued=int(tool_stats.get("write_calls_issued", 0)),
            write_calls_succeeded=int(tool_stats.get("write_calls_succeeded", 0)),
            write_calls_denied=int(tool_stats.get("write_calls_denied", 0)),
        )
    except Exception:
        log.warning("record_awareness_tick failed", exc_info=True)

    return bool(all_ok)
