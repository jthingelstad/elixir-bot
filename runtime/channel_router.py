"""Discord message routing for Elixir channel traffic."""

from __future__ import annotations

import asyncio
import logging

import cr_api
import db
import elixir_agent

_log = logging.getLogger("elixir.channel_router")


def _persist_inline_memories(memories, channel_id, workflow):
    """Persist memories declared inline in the LLM's JSON response."""
    from agent.tool_exec import _resolve_member_tag
    from memory_store import archive_memory, attach_tags, create_memory, search_memories
    from storage.contextual_memory import upsert_member_note_memory

    created_by = f"leader:inline-{workflow}"
    saved = 0
    for mem in memories:
        title = (mem.get("title") or "").strip()
        body = (mem.get("body") or "").strip()
        if not title or not body:
            _log.warning("inline_memory skipped: missing title or body")
            continue
        action = (mem.get("action") or "save").strip().lower()
        member_tag_input = mem.get("member_tag")
        tags = [str(t).strip().lower() for t in (mem.get("tags") or []) if t]

        try:
            # For corrections, search for and archive conflicting memories
            if action == "correct":
                candidates = search_memories(
                    title,
                    viewer_scope="system_internal",
                    include_system_internal=True,
                    limit=5,
                )
                for result in candidates:
                    old = result.memory
                    if old.get("status") == "active":
                        archive_memory(old["memory_id"], actor=created_by)
                        _log.info(
                            "inline_memory corrected: archived memory_id=%s title=%r",
                            old["memory_id"], old.get("title"),
                        )

            # Save the new memory
            if member_tag_input:
                try:
                    resolved_tag = _resolve_member_tag(member_tag_input)
                except (ValueError, Exception):
                    resolved_tag = None
                if resolved_tag:
                    memory = upsert_member_note_memory(
                        member_tag=resolved_tag,
                        member_label=member_tag_input,
                        note=body,
                        created_by=created_by,
                        metadata={"title": title, "source": "inline_memory"},
                    )
                    if memory and tags:
                        attach_tags(memory["memory_id"], tags, actor=created_by)
                    if memory:
                        saved += 1
                        _log.info("inline_memory saved: member_note id=%s title=%r", memory["memory_id"], title)
                else:
                    # Couldn't resolve member, save as general memory
                    memory = create_memory(
                        title=title, body=body, summary=body[:220],
                        source_type="leader_note", is_inference=False, confidence=1.0,
                        created_by=created_by, scope="leadership",
                        channel_id=str(channel_id) if channel_id else None,
                    )
                    if tags:
                        attach_tags(memory["memory_id"], tags, actor=created_by)
                    saved += 1
                    _log.info("inline_memory saved: leader_note id=%s title=%r (unresolved member)", memory["memory_id"], title)
            else:
                memory = create_memory(
                    title=title, body=body, summary=body[:220],
                    source_type="leader_note", is_inference=False, confidence=1.0,
                    created_by=created_by, scope="leadership",
                    channel_id=str(channel_id) if channel_id else None,
                )
                if tags:
                    attach_tags(memory["memory_id"], tags, actor=created_by)
                saved += 1
                _log.info("inline_memory saved: leader_note id=%s title=%r", memory["memory_id"], title)
        except Exception:
            _log.warning("inline_memory failed for %r", title, exc_info=True)
    return saved


async def _post_conversation_memory(
    user_message_id, assistant_message_id,
    user_content, assistant_content,
    channel_id, discord_user_id, workflow, author_name,
):
    """Fire-and-forget: distill summaries and extract inference facts after a turn."""
    try:
        from agent.memory_tasks import distill_summary, extract_inference_facts, save_inference_facts

        # Step 1: Distill real summaries for both messages
        if user_message_id and user_content:
            user_summary = await asyncio.to_thread(distill_summary, user_content)
            # Always write user summary — distilled if available, truncated fallback otherwise.
            # save_message no longer writes last_user_summary to avoid persisting verbatim text.
            final_user_summary = user_summary or (user_content[:200] if user_content else "")
            await asyncio.to_thread(db.update_message_summary, user_message_id, final_user_summary)

        if assistant_message_id and assistant_content:
            assistant_summary = await asyncio.to_thread(distill_summary, assistant_content)
            if assistant_summary:
                await asyncio.to_thread(db.update_message_summary, assistant_message_id, assistant_summary)

        # Step 2: Extract inference facts (clanops and interactive only)
        if workflow in {"clanops", "interactive"} and user_content and assistant_content:
            combined = f"User ({author_name or 'unknown'}): {user_content}\n\nElixir: {assistant_content}"
            facts = await asyncio.to_thread(
                extract_inference_facts, combined, f"{workflow} conversation",
            )
            if facts:
                await asyncio.to_thread(save_inference_facts, facts, channel_id)
    except Exception:
        _log.warning("_post_conversation_memory failed", exc_info=True)


def _agent_failure_payload(result):
    if isinstance(result, dict):
        error = result.get("_error")
        if isinstance(error, dict):
            return error
    return None


def _agent_failure_detail(error: dict) -> str | None:
    detail = (error.get("detail") or "").strip()
    phase = (error.get("phase") or "").strip()
    if phase and detail:
        return f"{phase}: {detail}"
    if phase:
        return phase
    return detail or None


def _primary_discord_message_id(sent_messages) -> str | None:
    for item in sent_messages or []:
        message_id = getattr(item, "id", None)
        if isinstance(message_id, (int, str)):
            return str(message_id)
    return None


def _stored_assistant_content(content) -> str:
    if isinstance(content, list):
        parts = [str(item).strip() for item in content if str(item).strip()]
        return "\n\n".join(parts)
    return (content or "").strip()


async def _save_reply_save(app, message, conversation_scope, raw_question, content, workflow, event_type):
    """Common pattern: save user message, reply, save assistant message."""
    ch = app._channel_msg_kwargs(message.channel)
    author = app._author_msg_kwargs(message.author)
    await asyncio.to_thread(
        db.save_message,
        conversation_scope, "user", raw_question,
        **ch, **author, workflow=workflow,
        discord_message_id=message.id,
    )
    await app._reply_text(message, content)
    await asyncio.to_thread(
        db.save_message,
        conversation_scope, "assistant", content,
        **ch, **author, workflow=workflow,
        event_type=event_type,
    )


def _log_route(app, route, message, mentioned, subagent, workflow, raw_question, **extra):
    parts = [f"message_route route={route} channel_id=%s author_id=%s mentioned=%s subagent=%s workflow=%s"]
    args = [message.channel.id, message.author.id, mentioned, subagent, workflow]
    for k, v in extra.items():
        parts.append(f"{k}=%r")
        args.append(v)
    parts.append("raw_question=%r original=%r")
    args.extend([raw_question, message.content])
    app.log.info(" ".join(parts), *args)


async def _handle_report_route(app, message, ctx, route, content, event_type=None):
    """Handle a simple report route: log, save, reply, save."""
    _log_route(app, route, message, ctx["mentioned"], ctx["subagent"], ctx["workflow"], ctx["raw_question"])
    await _save_reply_save(
        app, message, ctx["conversation_scope"], ctx["raw_question"],
        content, ctx["workflow"], event_type or route,
    )


async def _route_legacy_clanops_hint(app, message, ctx):
    raw_question = ctx["raw_question"]
    if not (ctx["workflow"] == "clanops" and app._is_legacy_clanops_command_text(raw_question)):
        return False
    if ctx["mentioned"] and app.parse_admin_command(raw_question, require_prefix=True):
        return False
    hint_content = app._build_clanops_command_hint()
    await _save_reply_save(
        app, message, ctx["conversation_scope"], raw_question,
        hint_content, "clanops", "clanops_command_hint",
    )
    return True


async def _perform_deck_review(app, message, ctx, *, mode, subject):
    """Run the deck_review workflow. Caller has already decided this is the right route."""
    deck_target = await asyncio.to_thread(app._extract_member_deck_target, ctx["raw_question"], message)
    target_tag = deck_target if isinstance(deck_target, str) and deck_target.startswith("#") else None
    target_name = None
    if target_tag:
        try:
            row = await asyncio.to_thread(db.get_member_profile, target_tag)
            if isinstance(row, dict):
                target_name = row.get("current_name") or row.get("name")
        except Exception:
            target_name = None

    route = f"deck_review_{mode}_{subject}"
    _log_route(app, route, message, ctx["mentioned"], ctx["subagent"], ctx["workflow"],
               ctx["raw_question"], deck_target=target_tag, mode=mode, subject=subject)

    async with message.channel.typing():
        try:
            channel_config = app._get_channel_behavior(message.channel.id)
            conversation_scope = ctx["conversation_scope"]
            conversation_history = await asyncio.to_thread(
                db.list_thread_messages, conversation_scope, app.CHANNEL_CONVERSATION_LIMIT,
            )
            memory_context = await asyncio.to_thread(
                db.build_memory_context,
                discord_user_id=message.author.id,
                channel_id=message.channel.id,
                viewer_scope=channel_config.get("memory_scope") or "public",
            )

            ch = app._channel_msg_kwargs(message.channel)
            author = app._author_msg_kwargs(message.author)
            user_msg_id = await asyncio.to_thread(
                db.save_message,
                conversation_scope, "user", ctx["raw_question"],
                **ch, **author, workflow="deck_review",
                discord_message_id=message.id,
            )

            result = await asyncio.to_thread(
                elixir_agent.respond_in_deck_review,
                question=ctx["raw_question"],
                author_name=message.author.display_name,
                channel_name=app._channel_reply_target_name(channel_config),
                mode=mode,
                subject=subject,
                target_member_tag=target_tag,
                target_member_name=target_name,
                conversation_history=conversation_history,
                memory_context=memory_context,
            )

            agent_error = _agent_failure_payload(result)
            if agent_error or result is None or not isinstance(result, dict):
                failure_type = (
                    (agent_error.get("kind") if isinstance(agent_error, dict) else None)
                    or ("agent_none" if result is None else "invalid_result_type")
                )
                app._log_prompt_failure(
                    question=ctx["raw_question"], workflow="deck_review",
                    failure_type=failure_type, failure_stage="respond_in_deck_review",
                    channel=message.channel, author=message.author,
                    discord_message_id=message.id,
                    detail=_agent_failure_detail(agent_error) if agent_error else None,
                )
                await message.reply(app._fallback_channel_response(ctx["raw_question"], "interactive"))
                return True

            result = await app._apply_member_refs_to_result(result)
            content = result.get("content") or result.get("summary") or ""
            if not content:
                app._log_prompt_failure(
                    question=ctx["raw_question"], workflow="deck_review",
                    failure_type="empty_result", failure_stage="respond_in_deck_review",
                    channel=message.channel, author=message.author,
                    discord_message_id=message.id, raw_json=result,
                )
                await message.reply(app._fallback_channel_response(ctx["raw_question"], "interactive"))
                return True

            sent = await app._reply_text(message, content)
            asst_msg_id = None
            try:
                asst_msg_id = await asyncio.to_thread(
                    db.save_message,
                    conversation_scope, "assistant", _stored_assistant_content(content),
                    **ch, **author, workflow="deck_review",
                    event_type=result.get("event_type"),
                    discord_message_id=_primary_discord_message_id(sent),
                )
            except Exception as exc:
                app.log.error("deck_review reply save error: %s", exc, exc_info=True)
            app._safe_create_task(
                _post_conversation_memory(
                    user_msg_id, asst_msg_id,
                    ctx["raw_question"], _stored_assistant_content(content),
                    message.channel.id, message.author.id,
                    "deck_review", message.author.display_name,
                ),
                name="deck_review_memory",
            )
        except Exception as e:
            app.log.error("deck_review error: %s", e, exc_info=True)
            app._log_prompt_failure(
                question=ctx["raw_question"], workflow="deck_review",
                failure_type="exception", failure_stage="route_deck_review",
                channel=message.channel, author=message.author,
                discord_message_id=message.id, detail=str(e),
            )
            await message.reply("Hit an error reviewing the deck. Try again in a sec.")
    return True


async def _route_admin_command(app, message, ctx):
    if ctx["workflow"] != "clanops" or not ctx["mentioned"]:
        return False
    admin_command = app.parse_admin_command(ctx["raw_question"], require_prefix=True)
    if not admin_command:
        return False
    if admin_command.get("kind") == "command" and app.admin_command_requires_leader(admin_command) and not app._has_leader_role(message.author):
        await _save_reply_save(
            app, message, ctx["conversation_scope"], ctx["raw_question"],
            "Leader role required for this command.", "clanops", "clanops_admin_denied",
        )
        return True
    route_key = (admin_command.get("key") or admin_command.get("command") or "admin").replace(".", "_").replace("-", "_")
    route = f"clanops_admin_{route_key}"
    if admin_command.get("preview"):
        route += "_preview"
    _log_route(app, route, message, ctx["mentioned"], ctx["subagent"], ctx["workflow"], ctx["raw_question"])
    content = await app.dispatch_admin_command(admin_command)
    await _save_reply_save(
        app, message, ctx["conversation_scope"], ctx["raw_question"],
        content, "clanops", route,
    )
    return True


async def _dispatch_intent(app, message, ctx, intent) -> bool:
    """Dispatch a classified intent to its handler.

    Returns True if the message was handled. Returns False to fall through to
    the generic LLM channel/reception workflow (the `llm_chat` route also
    returns False — its work is the existing channel_llm code path).
    """
    route = intent.get("route") or "llm_chat"
    workflow = ctx["workflow"]

    if route == "not_for_bot":
        # Quietly do nothing — message was conversation between humans.
        return True

    if route == "help":
        role = "clanops" if workflow == "clanops" else "interactive"
        event = "clanops_help" if role == "clanops" else "interactive_help"
        _log_route(app, event, message, ctx["mentioned"], ctx["subagent"], workflow, ctx["raw_question"])
        channel_config = app._get_channel_behavior(message.channel.id) or {}
        memory_context = await asyncio.to_thread(
            db.build_memory_context,
            discord_user_id=message.author.id,
            channel_id=message.channel.id,
            viewer_scope=channel_config.get("memory_scope") or "public",
        )
        result = await asyncio.to_thread(
            elixir_agent.respond_to_help_request,
            ctx["raw_question"],
            author_name=message.author.display_name,
            channel_name=app._channel_reply_target_name(channel_config),
            role=role,
            memory_context=memory_context,
        )
        content = (result or {}).get("content")
        if not content:
            # LLM call failed or returned empty — fall back to the static report
            # so the user always gets a useful answer.
            app.log.warning("help_llm_empty: falling back to static help report")
            content = await asyncio.to_thread(app._build_help_report, role)
        await _save_reply_save(
            app, message, ctx["conversation_scope"], ctx["raw_question"],
            content, workflow, event,
        )
        return True

    if route == "roster_join_dates":
        if workflow not in {"clanops", "interactive"}:
            return False
        content = await asyncio.to_thread(app._build_roster_join_dates_report)
        await _handle_report_route(app, message, ctx, "roster_join_dates_report", content)
        return True

    if route == "kick_risk":
        if workflow != "clanops":
            return False
        content = await asyncio.to_thread(app._build_kick_risk_report)
        await _handle_report_route(app, message, ctx, "kick_risk_report", content)
        return True

    if route == "top_war_contributors":
        if workflow != "clanops":
            return False
        content = await asyncio.to_thread(app._build_top_war_contributors_report)
        await _handle_report_route(app, message, ctx, "top_war_contributors_report", content)
        return True

    if route == "status_report":
        if workflow != "clanops":
            return False
        _log_route(app, "status_report", message, ctx["mentioned"], ctx["subagent"], workflow, ctx["raw_question"])
        content = await asyncio.to_thread(app._build_status_report)
        await _save_reply_save(
            app, message, ctx["conversation_scope"], ctx["raw_question"],
            content, "clanops", "status_report",
        )
        return True

    if route == "schedule_report":
        if workflow != "clanops":
            return False
        _log_route(app, "schedule_report", message, ctx["mentioned"], ctx["subagent"], workflow, ctx["raw_question"])
        content = await asyncio.to_thread(app._build_schedule_report)
        await _save_reply_save(
            app, message, ctx["conversation_scope"], ctx["raw_question"],
            content, "clanops", "schedule_report",
        )
        return True

    if route == "clan_status":
        if workflow != "clanops":
            return False
        mode = intent.get("mode") or "full"
        try:
            clan, war = await app._load_live_clan_context()
        except Exception as exc:
            app.log.warning("Clan status refresh failed: %s", exc)
            clan, war = {}, {}
        if mode == "short":
            route_name = "clan_status_short_report"
            content = await asyncio.to_thread(app._build_clan_status_short_report, clan, war)
        else:
            route_name = "clan_status_report"
            content = await asyncio.to_thread(app._build_clan_status_report, clan, war)
        _log_route(app, route_name, message, ctx["mentioned"], ctx["subagent"], workflow, ctx["raw_question"])
        await _save_reply_save(
            app, message, ctx["conversation_scope"], ctx["raw_question"],
            content, "clanops", route_name,
        )
        return True

    if route == "deck_display":
        if workflow not in {"clanops", "interactive"}:
            return False
        deck_target = await asyncio.to_thread(app._extract_member_deck_target, ctx["raw_question"], message)
        if not deck_target:
            # Router thought this was a deck display but we couldn't resolve a member.
            # Fall through to llm_chat so the model can ask a clarifying question.
            return False
        _log_route(app, "member_deck_report", message, ctx["mentioned"], ctx["subagent"],
                   workflow, ctx["raw_question"], deck_target=deck_target)
        content = await asyncio.to_thread(app._build_member_deck_report, deck_target)
        await _save_reply_save(
            app, message, ctx["conversation_scope"], ctx["raw_question"],
            content, workflow, "member_deck_report",
        )
        return True

    if route in {"deck_review", "deck_suggest"}:
        if workflow not in {"clanops", "interactive"}:
            return False
        if not ctx["mentioned"] and not ctx["allows_open_channel_reply"]:
            return False
        subject = "review" if route == "deck_review" else "suggest"
        mode = intent.get("mode") or "regular"
        if mode not in {"regular", "war"}:
            mode = "regular"
        return await _perform_deck_review(app, message, ctx, mode=mode, subject=subject)

    # llm_chat (or unknown) — let the existing channel_llm path handle it.
    return False


def _log_intent_classification(app, message, ctx, intent, *, mode_label="dispatch"):
    """Log a classified intent for both live and shadow modes."""
    app.log.info(
        "intent_router mode=%s channel_id=%s author_id=%s workflow=%s mentioned=%s "
        "route=%s confidence=%.2f sub_mode=%r target_member=%r latency_ms=%.1f "
        "fallback_reason=%r rationale=%r raw_question=%r",
        mode_label,
        message.channel.id,
        message.author.id,
        ctx["workflow"],
        ctx["mentioned"],
        intent.get("route"),
        float(intent.get("confidence") or 0.0),
        intent.get("mode"),
        intent.get("target_member"),
        float(intent.get("latency_ms") or 0.0),
        intent.get("fallback_reason"),
        intent.get("rationale"),
        ctx["raw_question"],
    )


async def route_message(message):
    import runtime.app as app
    from agent import intent_router as _intent_router

    if message.author.bot:
        return
    await asyncio.to_thread(
        db.upsert_discord_user,
        message.author.id,
        username=message.author.name,
        global_name=getattr(message.author, "global_name", None),
        display_name=message.author.display_name,
    )
    channel_config = app._get_channel_behavior(message.channel.id)
    mentioned = app._is_bot_mentioned(message)

    if not channel_config:
        await app.bot.process_commands(message)
        return

    subagent = channel_config.get("subagent") or channel_config.get("role")
    workflow = channel_config.get("workflow", "interactive")
    reply_policy = channel_config.get("reply_policy", "mention_only")
    allows_open_channel_reply = reply_policy == "open_channel"
    scope = app._channel_scope(message.channel)
    conversation_scope = app._channel_conversation_scope(message.channel, message.author.id)
    raw_question = app._strip_bot_mentions(message.content).strip() if mentioned else message.content.strip()

    ctx = {
        "mentioned": mentioned,
        "subagent": subagent,
        "workflow": workflow,
        "raw_question": raw_question,
        "conversation_scope": conversation_scope,
        "allows_open_channel_reply": allows_open_channel_reply,
    }

    # Privileged regex routes that stay regex-gated:
    #  - legacy clanops hint: deprecation nudge directing users to /elixir or @Elixir do
    #  - admin command: privileged prefix-based protocol
    if await _route_legacy_clanops_hint(app, message, ctx):
        return
    if await _route_admin_command(app, message, ctx):
        return

    # If the bot wasn't addressed and the channel doesn't allow proactive
    # replies, skip routing entirely. Avoids wasting an LLM router call on
    # human-to-human chatter.
    bot_should_consider = mentioned or allows_open_channel_reply or workflow == "reception"

    # LLM intent classifier — only for interactive/clanops where the bot was
    # addressed. Reception has its own onboarding pipeline below.
    if workflow in {"interactive", "clanops"} and bot_should_consider:
        try:
            intent = await asyncio.to_thread(
                _intent_router.classify_intent,
                ctx["raw_question"],
                workflow=workflow,
                mentioned=mentioned,
                allows_open_channel_reply=allows_open_channel_reply,
            )
        except Exception as exc:
            app.log.warning("intent_router_dispatch_failed: %s", exc, exc_info=True)
            intent = {"route": "llm_chat", "fallback_reason": "dispatch_exception"}
        _log_intent_classification(app, message, ctx, intent)
        if await _dispatch_intent(app, message, ctx, intent):
            return
        # `llm_chat` (and routes that bailed for wrong workflow) fall through
        # to the generic channel_llm path below.

    if not mentioned and not allows_open_channel_reply:
        await app.bot.process_commands(message)
        return

    if subagent == "reception" or workflow == "reception":
        async with message.channel.typing():
            try:
                clan = await asyncio.to_thread(cr_api.get_clan)
                question = raw_question
                memory_context = await asyncio.to_thread(
                    db.build_memory_context,
                    discord_user_id=message.author.id,
                    channel_id=message.channel.id,
                    viewer_scope=channel_config.get("memory_scope") or "public",
                )
                ch = app._channel_msg_kwargs(message.channel)
                author = app._author_msg_kwargs(message.author)
                _reception_user_msg_id = await asyncio.to_thread(
                    db.save_message,
                    scope, "user", question,
                    **ch, **author, workflow="reception",
                    discord_message_id=message.id,
                )
                result = await asyncio.to_thread(
                    elixir_agent.respond_in_reception,
                    question=question,
                    author_name=message.author.display_name,
                    clan_data=clan,
                    memory_context=memory_context,
                )
                agent_error = _agent_failure_payload(result)
                if agent_error:
                    app._log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type=agent_error.get("kind") or "agent_error",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        detail=_agent_failure_detail(agent_error),
                        result_preview=agent_error.get("result_preview"),
                        raw_json=agent_error.get("raw_json") or {"response_text": agent_error.get("response_text")},
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                if result is None:
                    app._log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="agent_none",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                if not isinstance(result, dict):
                    app._log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="invalid_result_type",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        detail=type(result).__name__,
                        result_preview=app._preview_text(result),
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                content = result.get("content", result.get("summary", ""))
                if not content:
                    app._log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="empty_result",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        result_preview=app._preview_text(result),
                        raw_json=result,
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                sent_messages = await app._reply_text(message, content)
                _reception_asst_msg_id = None
                try:
                    _reception_asst_msg_id = await asyncio.to_thread(
                        db.save_message,
                        scope, "assistant", _stored_assistant_content(content),
                        **ch, workflow="reception",
                        event_type=result.get("event_type"),
                        discord_message_id=_primary_discord_message_id(sent_messages),
                    )
                except Exception as exc:
                    app.log.error("reception reply save error: %s", exc, exc_info=True)
                app._safe_create_task(
                    _post_conversation_memory(
                        _reception_user_msg_id, _reception_asst_msg_id,
                        question, _stored_assistant_content(content),
                        message.channel.id, message.author.id,
                        "reception", message.author.display_name,
                    ),
                    name="reception_memory",
                )
            except Exception as e:
                app.log.error("reception error: %s", e)
                app._log_prompt_failure(
                    question=raw_question,
                    workflow="reception",
                    failure_type="exception",
                    failure_stage="on_message_reception",
                    channel=message.channel,
                    author=message.author,
                    discord_message_id=message.id,
                    detail=str(e),
                )
                await message.reply("Hit an error. Try again in a moment.")
        return

    if workflow in {"interactive", "clanops"}:
        app.log.info(
            "message_route route=channel_llm channel_id=%s author_id=%s mentioned=%s subagent=%s workflow=%s proactive=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            subagent,
            workflow,
            False,
            raw_question,
            message.content,
        )
        async with message.channel.typing():
            try:
                clan, war = await app._load_live_clan_context()
                question = raw_question
                conversation_history = await asyncio.to_thread(
                    db.list_thread_messages,
                    conversation_scope,
                    app.CHANNEL_CONVERSATION_LIMIT,
                )
                memory_context = await asyncio.to_thread(
                    db.build_memory_context,
                    discord_user_id=message.author.id,
                    channel_id=message.channel.id,
                    viewer_scope=channel_config.get("memory_scope") or "public",
                )

                ch = app._channel_msg_kwargs(message.channel)
                author = app._author_msg_kwargs(message.author)
                _channel_user_msg_id = await asyncio.to_thread(
                    db.save_message,
                    conversation_scope, "user", question,
                    **ch, **author, workflow=workflow,
                    discord_message_id=message.id,
                )

                result = await asyncio.to_thread(
                    elixir_agent.respond_in_channel,
                    question=question,
                    author_name=message.author.display_name,
                    channel_name=app._channel_reply_target_name(channel_config),
                    workflow=workflow,
                    clan_data=clan,
                    war_data=war,
                    conversation_history=conversation_history,
                    memory_context=memory_context,
                )
                agent_error = _agent_failure_payload(result)
                if agent_error:
                    app._log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type=agent_error.get("kind") or "agent_error",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        detail=_agent_failure_detail(agent_error),
                        result_preview=agent_error.get("result_preview"),
                        raw_json=agent_error.get("raw_json") or {"response_text": agent_error.get("response_text")},
                    )
                    if mentioned or allows_open_channel_reply:
                        await message.reply(app._fallback_channel_response(raw_question, workflow))
                    return
                if result is None:
                    app._log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="agent_none",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                    )
                    if mentioned or allows_open_channel_reply:
                        await message.reply(app._fallback_channel_response(raw_question, workflow))
                    return
                if not isinstance(result, dict):
                    app.log.error("%s channel error: invalid result type %s", workflow, type(result).__name__)
                    app._log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="invalid_result_type",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        detail=type(result).__name__,
                        result_preview=app._preview_text(result),
                    )
                    if mentioned or allows_open_channel_reply:
                        await message.reply(app._fallback_channel_response(raw_question, workflow))
                    return

                result = await app._apply_member_refs_to_result(result)
                inline_memories = result.pop("memories", None) or []
                if inline_memories:
                    try:
                        await asyncio.to_thread(
                            _persist_inline_memories, inline_memories, message.channel.id, workflow,
                        )
                    except Exception:
                        _log.error("inline memory persistence failed", exc_info=True)
                content = result.get("content", result.get("summary", ""))
                if not content:
                    app.log.error("%s channel error: empty result payload %s", workflow, result)
                    app._log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="empty_result",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        result_preview=app._preview_text(result),
                        raw_json=result,
                    )
                    if mentioned or allows_open_channel_reply:
                        await message.reply(app._fallback_channel_response(raw_question, workflow))
                    return
                sent_messages = await app._reply_text(message, content)
                try:
                    await app._share_channel_result(result, workflow)
                except Exception as exc:
                    app.log.error("%s channel share error: %s", workflow, exc, exc_info=True)
                _channel_asst_msg_id = None
                try:
                    _channel_asst_msg_id = await asyncio.to_thread(
                        db.save_message,
                        conversation_scope, "assistant", _stored_assistant_content(content),
                        **ch, **author, workflow=workflow,
                        event_type=result.get("event_type"),
                        discord_message_id=_primary_discord_message_id(sent_messages),
                    )
                except Exception as exc:
                    app.log.error("%s channel reply save error: %s", workflow, exc, exc_info=True)
                app._safe_create_task(
                    _post_conversation_memory(
                        _channel_user_msg_id, _channel_asst_msg_id,
                        question, _stored_assistant_content(content),
                        message.channel.id, message.author.id,
                        workflow, message.author.display_name,
                    ),
                    name=f"{workflow}_memory",
                )
            except Exception as e:
                app.log.error("%s channel error: %s", workflow, e)
                app._log_prompt_failure(
                    question=raw_question,
                    workflow=workflow,
                    failure_type="exception",
                    failure_stage="on_message_channel",
                    channel=message.channel,
                    author=message.author,
                    discord_message_id=message.id,
                    detail=str(e),
                )
                if mentioned:
                    await message.reply("Hit an error. Try again in a moment.")
        return

    await app.bot.process_commands(message)


__all__ = ["route_message"]
