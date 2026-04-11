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


async def route_message(message):
    import runtime.app as app

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

    if workflow == "clanops" and app._is_legacy_clanops_command_text(raw_question):
        if not mentioned or not app.parse_admin_command(raw_question, require_prefix=True):
            hint_content = app._build_clanops_command_hint()
            await asyncio.to_thread(
                db.save_message,
                conversation_scope,
                "user",
                raw_question,
                channel_id=message.channel.id,
                channel_name=getattr(message.channel, "name", None),
                channel_kind=str(message.channel.type),
                discord_user_id=message.author.id,
                username=message.author.name,
                display_name=message.author.display_name,
                workflow="clanops",
                discord_message_id=message.id,
            )
            await app._reply_text(message, hint_content)
            await asyncio.to_thread(
                db.save_message,
                conversation_scope,
                "assistant",
                hint_content,
                channel_id=message.channel.id,
                channel_name=getattr(message.channel, "name", None),
                channel_kind=str(message.channel.type),
                discord_user_id=message.author.id,
                username=message.author.name,
                display_name=message.author.display_name,
                workflow="clanops",
                event_type="clanops_command_hint",
            )
            return

    if workflow in {"clanops", "interactive"} and app._is_roster_join_dates_request(raw_question):
        app.log.info(
            "message_route route=roster_join_dates_report channel_id=%s author_id=%s mentioned=%s subagent=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            subagent,
            workflow,
            raw_question,
            message.content,
        )
        roster_content = await asyncio.to_thread(app._build_roster_join_dates_report)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await app._reply_text(message, roster_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            roster_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="roster_join_dates_report",
        )
        return

    if workflow == "interactive" and app._is_help_request(raw_question):
        app.log.info(
            "message_route route=interactive_help channel_id=%s author_id=%s mentioned=%s subagent=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            subagent,
            workflow,
            raw_question,
            message.content,
        )
        help_content = await asyncio.to_thread(app._build_help_report, "interactive")
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="interactive",
            discord_message_id=message.id,
        )
        await app._reply_text(message, help_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            help_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="interactive",
            event_type="interactive_help",
        )
        return

    deck_target = None
    if workflow in {"clanops", "interactive"} and app._is_member_deck_request(raw_question):
        deck_target = await asyncio.to_thread(app._extract_member_deck_target, raw_question, message)
    if workflow in {"clanops", "interactive"} and deck_target:
        app.log.info(
            "message_route route=member_deck_report channel_id=%s author_id=%s mentioned=%s subagent=%s workflow=%s deck_target=%r raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            subagent,
            workflow,
            deck_target,
            raw_question,
            message.content,
        )
        deck_content = await asyncio.to_thread(app._build_member_deck_report, deck_target)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await app._reply_text(message, deck_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            deck_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="member_deck_report",
        )
        return

    if workflow == "clanops" and app._is_kick_risk_request(raw_question):
        app.log.info(
            "message_route route=kick_risk_report channel_id=%s author_id=%s mentioned=%s subagent=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            subagent,
            workflow,
            raw_question,
            message.content,
        )
        kick_risk_content = await asyncio.to_thread(app._build_kick_risk_report)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await app._reply_text(message, kick_risk_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            kick_risk_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="kick_risk_report",
        )
        return

    if workflow == "clanops" and app._is_top_war_contributors_request(raw_question):
        app.log.info(
            "message_route route=top_war_contributors_report channel_id=%s author_id=%s mentioned=%s subagent=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            subagent,
            workflow,
            raw_question,
            message.content,
        )
        top_war_content = await asyncio.to_thread(app._build_top_war_contributors_report)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await app._reply_text(message, top_war_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            top_war_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="top_war_contributors_report",
        )
        return

    admin_command = app.parse_admin_command(raw_question, require_prefix=True) if workflow == "clanops" and mentioned else None
    if admin_command:
        if admin_command.get("kind") == "command" and app.admin_command_requires_leader(admin_command) and not app._has_leader_role(message.author):
            denial = "Leader role required for this command."
            await asyncio.to_thread(
                db.save_message,
                conversation_scope,
                "user",
                raw_question,
                channel_id=message.channel.id,
                channel_name=getattr(message.channel, "name", None),
                channel_kind=str(message.channel.type),
                discord_user_id=message.author.id,
                username=message.author.name,
                display_name=message.author.display_name,
                workflow="clanops",
                discord_message_id=message.id,
            )
            await app._reply_text(message, denial)
            await asyncio.to_thread(
                db.save_message,
                conversation_scope,
                "assistant",
                denial,
                channel_id=message.channel.id,
                channel_name=getattr(message.channel, "name", None),
                channel_kind=str(message.channel.type),
                discord_user_id=message.author.id,
                username=message.author.name,
                display_name=message.author.display_name,
                workflow="clanops",
                event_type="clanops_admin_denied",
            )
            return
        route_key = (admin_command.get("key") or admin_command.get("command") or "admin").replace(".", "_").replace("-", "_")
        route = f"clanops_admin_{route_key}"
        if admin_command.get("preview"):
            route += "_preview"
        app.log.info(
            "message_route route=%s channel_id=%s author_id=%s mentioned=%s subagent=%s workflow=%s raw_question=%r original=%r",
            route,
            message.channel.id,
            message.author.id,
            mentioned,
            subagent,
            workflow,
            raw_question,
            message.content,
        )
        admin_content = await app.dispatch_admin_command(
            admin_command,
        )
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            discord_message_id=message.id,
        )
        await app._reply_text(message, admin_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            admin_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            event_type=route,
        )
        return

    clan_status_mode = app._clan_status_mode(raw_question) if workflow == "clanops" else None
    if workflow == "clanops" and (app._is_status_request(raw_question) or app._is_schedule_request(raw_question) or clan_status_mode):
        route = (
            "clan_status_report" if clan_status_mode == "full"
            else "clan_status_short_report" if clan_status_mode == "short"
            else "schedule_report" if app._is_schedule_request(raw_question)
            else "status_report"
        )
        app.log.info(
            "message_route route=%s channel_id=%s author_id=%s mentioned=%s subagent=%s workflow=%s raw_question=%r original=%r",
            route,
            message.channel.id,
            message.author.id,
            mentioned,
            subagent,
            workflow,
            raw_question,
            message.content,
        )
        clan = {}
        war = {}
        if clan_status_mode:
            try:
                clan, war = await app._load_live_clan_context()
            except Exception as exc:
                app.log.warning("Clan status refresh failed: %s", exc)
        if clan_status_mode == "full":
            report_builder = app._build_clan_status_report
            report_args = (clan, war)
            event_type = "clan_status_report"
        elif clan_status_mode == "short":
            report_builder = app._build_clan_status_short_report
            report_args = (clan, war)
            event_type = "clan_status_short_report"
        elif app._is_schedule_request(raw_question):
            report_builder = app._build_schedule_report
            report_args = ()
            event_type = "schedule_report"
        else:
            report_builder = app._build_status_report
            report_args = ()
            event_type = "status_report"
        status_content = await asyncio.to_thread(report_builder, *report_args)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            discord_message_id=message.id,
        )
        await app._reply_text(message, status_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            status_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            event_type=event_type,
        )
        return

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
                _reception_user_msg_id = await asyncio.to_thread(
                    db.save_message,
                    scope,
                    "user",
                    question,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                    workflow="reception",
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
                        scope,
                        "assistant",
                        _stored_assistant_content(content),
                        channel_id=message.channel.id,
                        channel_name=getattr(message.channel, "name", None),
                        channel_kind=str(message.channel.type),
                        workflow="reception",
                        event_type=result.get("event_type"),
                        discord_message_id=_primary_discord_message_id(sent_messages),
                    )
                except Exception as exc:
                    app.log.error("reception reply save error: %s", exc, exc_info=True)
                asyncio.get_event_loop().create_task(
                    _post_conversation_memory(
                        _reception_user_msg_id, _reception_asst_msg_id,
                        question, _stored_assistant_content(content),
                        message.channel.id, message.author.id,
                        "reception", message.author.display_name,
                    )
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

                _channel_user_msg_id = await asyncio.to_thread(
                    db.save_message,
                    conversation_scope,
                    "user",
                    question,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                    workflow=workflow,
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
                        conversation_scope,
                        "assistant",
                        _stored_assistant_content(content),
                        channel_id=message.channel.id,
                        channel_name=getattr(message.channel, "name", None),
                        channel_kind=str(message.channel.type),
                        discord_user_id=message.author.id,
                        username=message.author.name,
                        display_name=message.author.display_name,
                        workflow=workflow,
                        event_type=result.get("event_type"),
                        discord_message_id=_primary_discord_message_id(sent_messages),
                    )
                except Exception as exc:
                    app.log.error("%s channel reply save error: %s", workflow, exc, exc_info=True)
                asyncio.get_event_loop().create_task(
                    _post_conversation_memory(
                        _channel_user_msg_id, _channel_asst_msg_id,
                        question, _stored_assistant_content(content),
                        message.channel.id, message.author.id,
                        workflow, message.author.display_name,
                    )
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
