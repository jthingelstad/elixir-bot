import json
import time

from agent.core import (
    CLANOPS_WRITE_TOOLS_ENABLED,
    MAX_CONTEXT_MEMBERS_DEFAULT,
    MAX_TOOL_ROUNDS,
    TOOL_RESULT_MAX_CHARS,
    TOOL_RESULT_MAX_ITEMS,
    _create_chat_completion,
    log,
)
from agent.tool_policy import (
    ALL_TOOLS,
    RESPONSE_SCHEMAS_BY_WORKFLOW,
    TOOL_DEFINITIONS_BY_NAME,
    TOOLSETS_BY_WORKFLOW,
)
from agent.tool_exec import _execute_tool


def _preview_text(value, limit=500):
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            text = repr(value)
    return text[:limit]


def _failure_payload(kind, detail=None, *, response_text=None, parsed_obj=None, phase=None):
    payload = {"kind": kind}
    if detail is not None:
        payload["detail"] = str(detail)
    if phase:
        payload["phase"] = phase
    if response_text is not None:
        payload["response_text"] = response_text
    if parsed_obj is not None:
        payload["raw_json"] = parsed_obj
    preview_source = parsed_obj if parsed_obj is not None else response_text
    payload["result_preview"] = _preview_text(preview_source)
    return {"_error": payload}

def _parse_response(text):
    """Parse LLM JSON response, handling markdown fences.

    Falls back to wrapping plain text as {"content": text} when JSON parsing
    fails but the response looks like a real answer.
    """
    text = text.strip()
    if text.lower() == "null":
        return None
    try:
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        return json.loads(cleaned.strip())
    except Exception:
        if text:
            log.warning("LLM returned plain text instead of JSON, wrapping: %s", text[:120])
            return {"content": text, "summary": "agent response"}
        return None


def _parse_json_response(text):
    """Parse strict JSON-only model responses."""
    text = (text or "").strip()
    if not text:
        return None
    if text.lower() == "null":
        return None
    cleaned = text
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned.strip())


def _validate_response(workflow, parsed_obj, response_schema=None):
    """Validate parsed model responses against workflow contracts."""
    schema = response_schema or RESPONSE_SCHEMAS_BY_WORKFLOW.get(workflow)
    if parsed_obj is None:
        if workflow == "observation":
            return True, None
        if schema:
            return False, "null response is not allowed for this workflow"
        return True, None
    if not isinstance(parsed_obj, dict):
        return False, "response must be a JSON object"
    if not schema:
        return True, None

    for key in schema.get("required", []):
        if key not in parsed_obj:
            return False, f"missing required field: {key}"

    if "content" in parsed_obj:
        content = parsed_obj.get("content")
        if isinstance(content, list):
            if not content:
                return False, "content list must not be empty"
            if not all(isinstance(item, str) and item.strip() for item in content):
                return False, "content list items must be non-empty strings"
        elif content is not None and not isinstance(content, str):
            return False, "content must be a string or list of strings"

    if workflow == "observation":
        allowed = {
            "clan_observation", "arena_milestone", "donation_milestone",
            "war_update", "member_join", "member_leave",
        }
        et = parsed_obj.get("event_type")
        if et not in allowed:
            return False, f"invalid event_type for observation: {et}"
    elif workflow in {"channel_update", "channel_update_leadership"}:
        et = parsed_obj.get("event_type")
        if not isinstance(et, str) or not et.strip():
            return False, f"invalid event_type for {workflow}: {et}"
    elif workflow == "reception":
        if parsed_obj.get("event_type") != "reception_response":
            return False, f"invalid event_type for reception: {parsed_obj.get('event_type')}"
    elif workflow in {"interactive", "clanops"}:
        et = parsed_obj.get("event_type")
        if et == "channel_response":
            pass
        elif et == "channel_share":
            if "share_content" not in parsed_obj:
                return False, "missing required field for channel_share: share_content"
        else:
            return False, f"invalid event_type for {workflow}: {et}"
    elif workflow == "roster_bios":
        if not isinstance(parsed_obj.get("members"), dict):
            return False, "members must be an object map"

    return True, None


def _tool_names(tool_defs):
    return {t["function"]["name"] for t in (tool_defs or [])}


def _normalize_message(message):
    if isinstance(message, dict):
        return message
    if hasattr(message, "model_dump"):
        dumped = message.model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped

    normalized = {
        "role": getattr(message, "role", None),
        "content": getattr(message, "content", None),
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        normalized_calls = []
        for tool_call in tool_calls:
            if hasattr(tool_call, "model_dump"):
                normalized_calls.append(tool_call.model_dump(exclude_none=True))
                continue
            fn = getattr(tool_call, "function", None)
            normalized_calls.append(
                {
                    "id": getattr(tool_call, "id", None),
                    "type": getattr(tool_call, "type", "function"),
                    "function": {
                        "name": getattr(fn, "name", None),
                        "arguments": getattr(fn, "arguments", None),
                    },
                }
            )
        normalized["tool_calls"] = normalized_calls
    return {key: value for key, value in normalized.items() if value is not None}


def _estimate_message_chars(messages):
    """Cheap prompt-size proxy for telemetry."""
    total = 0
    for m in messages:
        normalized = _normalize_message(m)
        content = normalized.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += len(json.dumps(content, default=str))
        elif content is not None:
            total += len(str(content))
    return total


def _strip_context_image_fields(value):
    """Remove image asset fields before tool data is added to model context."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in {"iconurls", "icon_url", "iconurl", "image_url", "imageurl"}:
                continue
            cleaned[key] = _strip_context_image_fields(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_context_image_fields(item) for item in value]
    return value


def _build_tool_result_envelope(name, raw_result):
    """Normalize tool output into a compact envelope for model context."""
    try:
        parsed = json.loads(raw_result)
    except Exception:
        parsed = {"error": "tool_result_not_json", "raw": str(raw_result)[:500]}
    parsed = _strip_context_image_fields(parsed)

    envelope = {
        "ok": True,
        "error": None,
        "truncated": False,
        "meta": {"tool": name},
        "data": parsed,
    }

    if isinstance(parsed, dict) and "error" in parsed:
        envelope["ok"] = False
        envelope["error"] = parsed.get("error")

    if isinstance(parsed, list):
        original_count = len(parsed)
        if original_count > TOOL_RESULT_MAX_ITEMS:
            envelope["data"] = parsed[:TOOL_RESULT_MAX_ITEMS]
            envelope["truncated"] = True
            envelope["meta"]["original_count"] = original_count
            envelope["meta"]["returned_count"] = TOOL_RESULT_MAX_ITEMS

    serialized = json.dumps(envelope, default=str)
    if len(serialized) > TOOL_RESULT_MAX_CHARS:
        envelope["truncated"] = True
        envelope["meta"]["char_limit"] = TOOL_RESULT_MAX_CHARS
        envelope["meta"]["char_size"] = len(serialized)
        data_s = json.dumps(envelope.get("data"), default=str)
        envelope["data"] = data_s[:TOOL_RESULT_MAX_CHARS // 2] + "...[truncated]"
        if envelope["ok"] and envelope["error"] is None:
            envelope["error"] = "tool_result_truncated_for_context"

    return json.dumps(envelope, default=str)


def _chat_with_tools(system_prompt, user_message, conversation_history=None,
                     temperature=0.7, max_tokens=800, workflow="generic",
                     allowed_tools=None, response_schema=None, strict_json=True,
                     return_errors=False):
    """Run a chat completion with tool-calling loop.

    conversation_history: optional list of {role, content} dicts to inject
        between the system prompt and the current user message.
    Returns the final parsed response dict, or None.
    """
    messages = [
        {"role": "system", "content": system_prompt},
    ]
    # Inject prior conversation turns if provided
    if conversation_history:
        for turn in conversation_history:
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_message})

    if allowed_tools is None:
        allowed_tools = TOOLSETS_BY_WORKFLOW.get(workflow, ALL_TOOLS)
    allowed_tool_names = _tool_names(allowed_tools)

    enable_write_tools = workflow == "clanops" and CLANOPS_WRITE_TOOLS_ENABLED

    tools_called = []
    denied_tool_count = 0
    validation_failure_count = 0
    completion_latencies_ms = []
    completion_chars = 0

    def _create_completion(call_messages):
        start = time.perf_counter()
        resp = _create_chat_completion(
            workflow=workflow,
            messages=call_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=60,
            tools=allowed_tools if allowed_tools else None,
            tool_choice="auto" if allowed_tools else None,
        )
        completion_latencies_ms.append(round((time.perf_counter() - start) * 1000, 2))
        return resp

    def _parse_and_validate(content, repair_allowed, phase):
        nonlocal validation_failure_count
        try:
            parsed = _parse_json_response(content) if strict_json else _parse_response(content or "null")
        except Exception as e:
            validation_failure_count += 1
            if not repair_allowed:
                log.warning("validation_failure workflow=%s reason=parse_error detail=%s", workflow, e)
                if return_errors:
                    return _failure_payload("parse_error", e, response_text=content, phase=phase)
                return None
            return "__REPAIR__", f"Invalid JSON. Error: {e}"

        ok, error = _validate_response(workflow, parsed, response_schema=response_schema)
        if ok:
            return parsed
        validation_failure_count += 1
        if not repair_allowed:
            log.warning("validation_failure workflow=%s reason=schema_error detail=%s", workflow, error)
            if return_errors:
                return _failure_payload("schema_error", error, response_text=content, parsed_obj=parsed, phase=phase)
            return None
        return "__REPAIR__", f"Schema validation failed: {error}"

    for _round in range(MAX_TOOL_ROUNDS + 1):
        try:
            resp = _create_completion(messages)
        except Exception as e:
            log.error("OpenAI API error: %s", e)
            if return_errors:
                return _failure_payload("openai_api_error", e, phase="initial_completion")
            return None

        choice = resp.choices[0]

        # If no tool calls, we have the final answer
        if not choice.message.tool_calls:
            completion_chars += len(choice.message.content or "")
            parsed = _parse_and_validate(choice.message.content or "null", repair_allowed=True, phase="initial_response")
            if isinstance(parsed, tuple) and parsed[0] == "__REPAIR__":
                messages.append({"role": "assistant", "content": choice.message.content or ""})
                messages.append({
                    "role": "system",
                    "content": (
                        "Your previous response was invalid for this workflow. "
                        f"{parsed[1]} Return JSON only that satisfies the required schema."
                    ),
                })
                try:
                    repair_resp = _create_completion(messages)
                except Exception as e:
                    log.error("OpenAI API repair error: %s", e)
                    if return_errors:
                        return _failure_payload("openai_api_error", e, phase="repair_completion")
                    return None

                repaired = repair_resp.choices[0].message.content or "null"
                completion_chars += len(repaired)
                parsed = _parse_and_validate(repaired, repair_allowed=False, phase="repair_response")

            prompt_chars = _estimate_message_chars(messages)
            log.info(
                "agent_loop workflow=%s tool_rounds=%d tools_called=%s denied_tools=%d "
                "validation_failures=%d prompt_chars=%d completion_chars=%d completion_latencies_ms=%s",
                workflow, _round, tools_called, denied_tool_count, validation_failure_count,
                prompt_chars, completion_chars, completion_latencies_ms,
            )
            return parsed

        # Process tool calls
        messages.append(_normalize_message(choice.message))
        for tool_call in choice.message.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            allowed = fn_name in allowed_tool_names
            if not allowed:
                denied_tool_count += 1
                log.warning(
                    "tool_denied workflow=%s tool=%s reason=not_allowed_for_workflow",
                    workflow, fn_name,
                )
                result = json.dumps({
                    "error": "tool_not_allowed",
                    "tool": fn_name,
                    "workflow": workflow,
                })
            else:
                side_effect = TOOL_DEFINITIONS_BY_NAME.get(fn_name, {}).get("side_effect", "read")
                if side_effect == "write" and not enable_write_tools:
                    denied_tool_count += 1
                    log.warning(
                        "tool_denied workflow=%s tool=%s reason=write_policy_disabled",
                        workflow, fn_name,
                    )
                    result = json.dumps({
                        "error": "tool_write_disabled",
                        "tool": fn_name,
                        "workflow": workflow,
                    })
                else:
                    log.info("Tool call workflow=%s: %s(%s)", workflow, fn_name, fn_args)
                    tools_called.append(fn_name)
                    result = _build_tool_result_envelope(
                        fn_name,
                        _execute_tool(fn_name, fn_args),
                    )
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    # If we hit max rounds, try to get a final answer without tools
    log.warning("Hit max tool rounds (%d), requesting final answer", MAX_TOOL_ROUNDS)
    try:
        resp = _create_completion(messages)
        completion_chars += len(resp.choices[0].message.content or "")
        parsed = _parse_and_validate(resp.choices[0].message.content or "null", repair_allowed=False, phase="final_response")
        prompt_chars = _estimate_message_chars(messages)
        log.info(
            "agent_loop workflow=%s tool_rounds=%d tools_called=%s denied_tools=%d "
            "validation_failures=%d prompt_chars=%d completion_chars=%d completion_latencies_ms=%s",
            workflow, MAX_TOOL_ROUNDS, tools_called, denied_tool_count, validation_failure_count,
            prompt_chars, completion_chars, completion_latencies_ms,
        )
        return parsed
    except Exception as e:
        log.error("Final answer error: %s", e)
        if return_errors:
            return _failure_payload("openai_api_error", e, phase="final_completion")
        return None


def _clan_context(clan_data, war_data, roster_data=None, max_members=MAX_CONTEXT_MEMBERS_DEFAULT):
    """Format clan data into a concise context string for the LLM.

    roster_data: optional enriched roster dict (from build_roster_data with
        include_cards=True). When provided, favorite cards are included per member.
    """
    # Build a lookup of enriched roster data (cards, etc.) by tag
    roster_by_tag = {}
    if roster_data:
        for rm in roster_data.get("members", []):
            roster_by_tag[rm.get("tag", "")] = rm
            roster_by_tag["#" + rm.get("tag", "")] = rm

    members = clan_data.get("memberList", clan_data.get("members", []))
    member_summary = []
    sorted_members = sorted(members, key=lambda x: x.get("clanRank", x.get("clan_rank", 99)))
    limited_members = sorted_members[:max_members]
    for m in limited_members:
        arena = m.get("arena", {})
        arena_name = arena.get("name", str(arena)) if isinstance(arena, dict) else str(arena)
        line = (
            f"  {m.get('name','?')} ({m.get('tag','?')}) | rank #{m.get('clanRank', m.get('clan_rank','?'))} | "
            f"{m.get('trophies',0):,} trophies | {m.get('donations',0)} donations | "
            f"role: {m.get('role','member')} | arena: {arena_name} | "
            f"last_seen: {m.get('lastSeen', m.get('last_seen','?'))}"
        )
        # Append card data from enriched roster if available
        tag = m.get("tag", "")
        enriched = roster_by_tag.get(tag, {})
        fav_cards = enriched.get("favorite_cards", [])
        if fav_cards:
            card_str = ", ".join(f"{c['name']} ({c['usage_pct']}%)" for c in fav_cards[:5])
            line += f" | top cards: {card_str}"
        member_summary.append(line)

    omitted_count = max(0, len(sorted_members) - len(limited_members))
    if omitted_count:
        member_summary.append(f"  ... {omitted_count} more members omitted for context budget")

    war_summary = "No active war data."
    if war_data and war_data.get("state") not in (None, "notInWar"):
        parts = war_data.get("clan", {}).get("participants", [])
        fame = war_data.get("clan", {}).get("fame", 0)
        used = [p["name"] for p in parts if p.get("decksUsedToday", 0) > 0]
        unused = [p["name"] for p in parts if p.get("decksUsedToday", 0) == 0]
        war_summary = (
            f"River Race state: {war_data.get('state')} | fame: {fame:,} | "
            f"battled today: {', '.join(used) or 'nobody'} | "
            f"not yet: {', '.join(unused) or 'everyone done'}"
        )
    return (
        f"=== CLAN ROSTER ===\n" + "\n".join(member_summary)
        + f"\n\n=== WAR STATUS ===\n{war_summary}"
    )


def _format_recent_posts(recent_posts, channel_label="this channel"):
    """Format recent assistant post history for inclusion in LLM context."""
    if not recent_posts:
        return ""
    lines = []
    for p in recent_posts:
        ts = p.get("recorded_at", "")
        content = p.get("content", "")
        lines.append(f"  [{ts}] {content[:200]}")
    return f"\n=== YOUR RECENT POSTS IN {channel_label} ===\n" + "\n".join(lines) + "\n"


def _format_memory_context(memory_context):
    if not memory_context:
        return ""
    sections = []
    user_ctx = memory_context.get("discord_user") or {}
    user_facts = user_ctx.get("facts") or []
    user_episodes = user_ctx.get("episodes") or []
    if user_facts or user_episodes:
        lines = []
        for fact in user_facts[:5]:
            lines.append(f"  fact: {fact.get('fact_type')} = {fact.get('fact_value')}")
        for episode in user_episodes[:5]:
            lines.append(f"  episode: {episode.get('summary')}")
        sections.append("=== USER MEMORY ===\n" + "\n".join(lines))

    member_ctx = memory_context.get("member") or {}
    member_facts = member_ctx.get("facts") or []
    member_episodes = member_ctx.get("episodes") or []
    if member_facts or member_episodes:
        lines = []
        for fact in member_facts[:5]:
            lines.append(f"  fact: {fact.get('fact_type')} = {fact.get('fact_value')}")
        for episode in member_episodes[:5]:
            lines.append(f"  episode: {episode.get('summary')}")
        sections.append("=== MEMBER MEMORY ===\n" + "\n".join(lines))

    channel_ctx = memory_context.get("channel") or {}
    channel_state = channel_ctx.get("state") or {}
    channel_episodes = channel_ctx.get("episodes") or []
    if channel_state or channel_episodes:
        lines = []
        if channel_state.get("last_summary"):
            lines.append(f"  last_elixir_summary: {channel_state.get('last_summary')}")
        for episode in channel_episodes[:5]:
            lines.append(f"  episode: {episode.get('summary')}")
        sections.append("=== CHANNEL MEMORY ===\n" + "\n".join(lines))

    durable_memories = memory_context.get("durable_memories") or []
    if durable_memories:
        lines = []
        for memory in durable_memories[:5]:
            summary = (memory.get("summary") or memory.get("body") or "").strip()
            if not summary:
                continue
            scope = memory.get("scope") or "unknown"
            lines.append(f"  [{scope}] {summary}")
        if lines:
            sections.append("=== DURABLE MEMORY ===\n" + "\n".join(lines))

    return ("\n\n" + "\n\n".join(sections)) if sections else ""



__all__ = [
    "_parse_response",
    "_parse_json_response",
    "_validate_response",
    "_tool_names",
    "_estimate_message_chars",
    "_strip_context_image_fields",
    "_build_tool_result_envelope",
    "_chat_with_tools",
    "_clan_context",
    "_format_recent_posts",
    "_format_memory_context",
]
