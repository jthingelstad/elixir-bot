"""Signal delivery entrypoints."""

from __future__ import annotations

import asyncio
import logging

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

        if preauthored_result is not None:
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
        await facade._post_to_elixir(channel, result)
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
    for post in posts:
        ok = await facade._deliver_awareness_post(post, signals or [])
        if ok:
            delivered += 1
            for key in post.get("covers_signal_keys") or []:
                if key:
                    covered.add(str(key))
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
    }


async def _deliver_signal_group_via_awareness(signals, clan, war, *, workflow: str | None = None) -> bool:
    facade = _facade()
    from heartbeat import HeartbeatTickResult
    from runtime.situation import build_situation, situation_is_quiet

    bundle = HeartbeatTickResult(signals=signals or [], clan=clan or {}, war=war or {})
    situation = build_situation(bundle)

    if signals:
        await facade._mark_signal_group_completed(signals)

    if situation_is_quiet(situation):
        log.info("awareness loop: quiet tick, skipping agent call")
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

    considered_skipped = [
        signal for signal in (signals or [])
        if signal_source_key(signal) not in covered_keys
        and signal_source_key(signal) not in fallback_failed_keys
        and signal_source_key(signal) not in hard_required_keys
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
        "hard_fallback=%d hard_fallback_failed=%d signals_in=%d skipped_reason=%r",
        workflow,
        report["delivered"],
        report["rejected"],
        len(covered_keys),
        len(considered_skipped),
        len(uncovered),
        len(fallback_failed_keys),
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
