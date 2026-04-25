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
    AWARENESS_WRITE_BUDGET_PER_TICK,
    AWARENESS_WRITE_TOOL_NAMES,
    EXTERNAL_LOOKUP_TOOL_NAMES,
    MAX_ROUNDS_BY_WORKFLOW,
    RESPONSE_SCHEMAS_BY_WORKFLOW,
    TOOL_DEFINITIONS_BY_NAME,
    TOOLSETS_BY_WORKFLOW,
)

EXTERNAL_LOOKUP_CAP = 5
from agent.tool_exec import _execute_tool
from anthropic import APIError, APIConnectionError


def _preview_text(value, limit=500):
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
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
    except (json.JSONDecodeError, ValueError, IndexError):
        if text:
            log.warning("LLM returned plain text instead of JSON, wrapping: %s", text[:120])
            return {"content": text, "summary": "agent response"}
        return None


def _parse_json_response(text):
    """Parse strict JSON-only model responses.

    Handles bare JSON, markdown-fenced JSON (even with preamble text),
    and JSON embedded after conversational preamble.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.lower() == "null":
        return None
    # Fast path: bare JSON
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    # Extract from markdown code fence (handles preamble before ```)
    fence = text.find("```")
    if fence != -1:
        inner = text[fence + 3:]
        end = inner.find("```")
        if end != -1:
            inner = inner[:end]
        if inner.startswith("json"):
            inner = inner[4:]
        return json.loads(inner.strip())
    # Fallback: find first JSON object in the text
    brace = text.find("{")
    if brace != -1:
        obj, _ = json.JSONDecoder().raw_decode(text, brace)
        return obj
    return json.loads(text)


def _validate_response(workflow, parsed_obj, response_schema=None):
    """Validate parsed model responses against workflow contracts."""
    schema = response_schema or RESPONSE_SCHEMAS_BY_WORKFLOW.get(workflow)
    if parsed_obj is None:
        if workflow in {"observation", "channel_update", "channel_update_leadership"}:
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
    return {t["name"] for t in (tool_defs or [])}


def _normalize_message(message):
    """Normalize an LLM response message into a dict for the messages array.

    Produces Anthropic-native format:
    - Plain text assistant: {"role": "assistant", "content": "text"}
    - Tool-calling assistant: {"role": "assistant", "content": [blocks...]}
    """
    if isinstance(message, dict):
        return message

    role = getattr(message, "role", "assistant")
    content = getattr(message, "content", None)
    tool_calls = getattr(message, "tool_calls", None)

    if tool_calls:
        blocks = []
        if content:
            blocks.append({"type": "text", "text": content})
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            args_str = getattr(fn, "arguments", "{}") if fn else "{}"
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except (json.JSONDecodeError, ValueError):
                args = {}
            blocks.append({
                "type": "tool_use",
                "id": getattr(tc, "id", ""),
                "name": getattr(fn, "name", "") if fn else "",
                "input": args,
            })
        return {"role": role, "content": blocks}

    return {"role": role, "content": content or ""}


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


def _drop_largest_list(data):
    """Replace the largest list nested anywhere in data with a marker.

    Returns (modified_data, dropped_path) or (data, None) if no list found.
    The marker preserves shape so the model can see what was dropped and how big.
    """
    if not isinstance(data, dict):
        return data, None

    candidates = []
    stack = [(data, ())]
    while stack:
        obj, path = stack.pop()
        if isinstance(obj, list):
            candidates.append((path, len(obj), len(json.dumps(obj, default=str))))
            continue
        if isinstance(obj, dict):
            for key, value in obj.items():
                stack.append((value, path + (key,)))

    if not candidates:
        return data, None

    candidates.sort(key=lambda c: c[2], reverse=True)
    path, count, _size = candidates[0]

    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = {
        "dropped": True,
        "original_count": count,
        "reason": "context_size",
        "hint": "Result trimmed to fit context. Use a more specific tool or filter to retrieve these items.",
    }
    return data, path


def _build_tool_result_envelope(name, raw_result):
    """Normalize tool output into a compact envelope for model context."""
    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError, ValueError):
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
            log.warning(
                "tool_result_truncated tool=%s reason=item_limit original=%d returned=%d",
                name, original_count, TOOL_RESULT_MAX_ITEMS,
            )

    serialized = json.dumps(envelope, default=str)
    original_size = len(serialized)
    if original_size > TOOL_RESULT_MAX_CHARS:
        envelope["truncated"] = True
        envelope["meta"]["char_limit"] = TOOL_RESULT_MAX_CHARS
        envelope["meta"]["original_size"] = original_size

        dropped_paths = []
        data = envelope.get("data")
        if isinstance(data, dict):
            while len(json.dumps(envelope, default=str)) > TOOL_RESULT_MAX_CHARS:
                data, path = _drop_largest_list(data)
                if path is None:
                    break
                dropped_paths.append(".".join(str(p) for p in path))
                envelope["data"] = data

        if len(json.dumps(envelope, default=str)) > TOOL_RESULT_MAX_CHARS:
            envelope["data"] = {
                "dropped": True,
                "reason": "context_size_after_field_trim",
                "hint": "Result too large even after dropping arrays. Narrow the query.",
            }

        if dropped_paths:
            envelope["meta"]["dropped_fields"] = dropped_paths
        log.warning(
            "tool_result_truncated tool=%s reason=char_limit char_size=%d char_limit=%d dropped=%s",
            name, original_size, TOOL_RESULT_MAX_CHARS, dropped_paths or "data_replaced",
        )

    return json.dumps(envelope, default=str)


def _tool_result_succeeded(envelope_json: str) -> bool:
    try:
        envelope = json.loads(envelope_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(envelope, dict):
        return False
    return bool(envelope.get("ok")) and envelope.get("error") is None


def _chat_with_tools(system_prompt, user_message, conversation_history=None,
                     temperature=0.7, max_tokens=4096, workflow="generic",
                     allowed_tools=None, response_schema=None, strict_json=True,
                     return_errors=False, tool_stats=None):
    """Run a chat completion with tool-calling loop.

    conversation_history: optional list of {role, content} dicts to inject
        between the system prompt and the current user message.
    tool_stats: optional dict the caller provides; populated in-place with
        ``write_calls_issued``, ``write_calls_succeeded``, ``write_calls_denied``
        so the caller can persist the awareness-loop write budget usage.
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

    # Clanops writes are gated by the long-standing env var. Awareness writes
    # are gated by a per-tick budget enforced in the tool-call loop below.
    is_clanops_write_ok = workflow == "clanops" and CLANOPS_WRITE_TOOLS_ENABLED
    is_awareness_write_ok = workflow == "awareness"

    max_tool_rounds = MAX_ROUNDS_BY_WORKFLOW.get(workflow, MAX_TOOL_ROUNDS)

    tools_called = []
    denied_tool_count = 0
    validation_failure_count = 0
    external_lookup_calls = 0
    completion_latencies_ms = []
    completion_chars = 0

    # Write-call tallies live on the caller-provided tool_stats dict so they
    # survive every return path below without needing a finalizer.
    if tool_stats is None:
        tool_stats = {}
    tool_stats.setdefault("write_calls_issued", 0)
    tool_stats.setdefault("write_calls_succeeded", 0)
    tool_stats.setdefault("write_calls_denied", 0)

    def _create_completion(call_messages, *, allow_tools=True):
        start = time.perf_counter()
        use_tools = allow_tools and bool(allowed_tools)
        resp = _create_chat_completion(
            workflow=workflow,
            messages=call_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=60,
            tools=allowed_tools if use_tools else None,
            tool_choice="auto" if use_tools else None,
        )
        completion_latencies_ms.append(round((time.perf_counter() - start) * 1000, 2))
        return resp

    def _parse_and_validate(content, repair_allowed, phase):
        nonlocal validation_failure_count
        try:
            parsed = _parse_json_response(content) if strict_json else _parse_response(content or "null")
        except (json.JSONDecodeError, ValueError) as e:
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

    def _is_truncated(choice):
        return getattr(choice, "stop_reason", None) == "max_tokens"

    def _truncation_failure(phase, content):
        log.warning("llm_truncated workflow=%s phase=%s max_tokens=%d", workflow, phase, max_tokens)
        if return_errors:
            return _failure_payload(
                "truncation",
                f"LLM response truncated by max_tokens={max_tokens}",
                response_text=content,
                phase=phase,
            )
        return None

    def _log_agent_loop(round_num):
        prompt_chars = _estimate_message_chars(messages)
        log.info(
            "agent_loop workflow=%s tool_rounds=%d tools_called=%s denied_tools=%d "
            "validation_failures=%d prompt_chars=%d completion_chars=%d completion_latencies_ms=%s",
            workflow, round_num, tools_called, denied_tool_count, validation_failure_count,
            prompt_chars, completion_chars, completion_latencies_ms,
        )

    for _round in range(max_tool_rounds + 1):
        try:
            resp = _create_completion(messages)
        except (APIError, APIConnectionError) as e:
            log.error("LLM API error: %s", e)
            if return_errors:
                return _failure_payload("llm_api_error", e, phase="initial_completion")
            return None

        choice = resp.choices[0]

        # If no tool calls, we have the final answer
        if not choice.message.tool_calls:
            initial_content = choice.message.content or ""
            completion_chars += len(initial_content)
            if _is_truncated(choice):
                _log_agent_loop(_round)
                return _truncation_failure("initial_response", initial_content)
            parsed = _parse_and_validate(initial_content or "null", repair_allowed=True, phase="initial_response")
            if isinstance(parsed, tuple) and parsed[0] == "__REPAIR__":
                messages.append({"role": "assistant", "content": initial_content})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your previous response was invalid: {parsed[1]}. "
                        "Reply NOW with valid JSON only that satisfies the required schema. "
                        "Do not call any more tools. Do not return `null` or an empty string. "
                        "You already have everything you need from the tool results above — "
                        "use them to write a real `content` string that answers the user. "
                        "If the user's question turned out to have a false premise (e.g. they "
                        "asked about a card they don't own, or asked 'is X maxed' when X isn't), "
                        "still return a valid response object whose `content` explains plainly "
                        "what you found. Never abstain by returning null — abstaining only "
                        "produces a fallback error message visible to the user."
                    ),
                })
                try:
                    # Repair attempts must not be allowed to escape into more
                    # tool calls — the model's already had its tool budget and
                    # we just want a clean JSON answer from the data it has.
                    repair_resp = _create_completion(messages, allow_tools=False)
                except (APIError, APIConnectionError) as e:
                    log.error("LLM API repair error: %s", e)
                    if return_errors:
                        return _failure_payload("llm_api_error", e, phase="repair_completion")
                    return None

                repair_choice = repair_resp.choices[0]
                repaired = repair_choice.message.content or ""
                completion_chars += len(repaired)
                if _is_truncated(repair_choice):
                    _log_agent_loop(_round)
                    return _truncation_failure("repair_response", repaired)
                parsed = _parse_and_validate(repaired or "null", repair_allowed=False, phase="repair_response")

            _log_agent_loop(_round)
            return parsed

        # Process tool calls
        messages.append(_normalize_message(choice.message))
        tool_result_blocks = []
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
            elif fn_name in EXTERNAL_LOOKUP_TOOL_NAMES and external_lookup_calls >= EXTERNAL_LOOKUP_CAP:
                denied_tool_count += 1
                log.warning(
                    "tool_denied workflow=%s tool=%s reason=external_lookup_cap calls=%d",
                    workflow, fn_name, external_lookup_calls,
                )
                result = json.dumps({
                    "error": "external_lookup_cap_reached",
                    "tool": fn_name,
                    "cap": EXTERNAL_LOOKUP_CAP,
                    "hint": "External CR API lookups are capped per turn. Summarize with what you already have.",
                })
            else:
                side_effect = TOOL_DEFINITIONS_BY_NAME.get(fn_name, {}).get("side_effect", "read")
                is_awareness_write = (
                    is_awareness_write_ok and fn_name in AWARENESS_WRITE_TOOL_NAMES
                )
                write_allowed = (
                    side_effect != "write"
                    or is_clanops_write_ok
                    or is_awareness_write
                )
                if not write_allowed:
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
                elif is_awareness_write and tool_stats["write_calls_issued"] >= AWARENESS_WRITE_BUDGET_PER_TICK:
                    tool_stats["write_calls_denied"] += 1
                    denied_tool_count += 1
                    log.warning(
                        "tool_denied workflow=%s tool=%s reason=awareness_write_budget issued=%d cap=%d",
                        workflow, fn_name, tool_stats["write_calls_issued"], AWARENESS_WRITE_BUDGET_PER_TICK,
                    )
                    result = json.dumps({
                        "error": "awareness_write_budget_reached",
                        "tool": fn_name,
                        "cap": AWARENESS_WRITE_BUDGET_PER_TICK,
                        "hint": (
                            "You have already used your write budget for this tick. "
                            "Skip further writes and finalize your post plan."
                        ),
                    })
                else:
                    log.info("Tool call workflow=%s: %s(%s)", workflow, fn_name, fn_args)
                    tools_called.append(fn_name)
                    if fn_name in EXTERNAL_LOOKUP_TOOL_NAMES:
                        external_lookup_calls += 1
                    if is_awareness_write:
                        tool_stats["write_calls_issued"] += 1
                    result = _build_tool_result_envelope(
                        fn_name,
                        _execute_tool(fn_name, fn_args, workflow=workflow),
                    )
                    if is_awareness_write and _tool_result_succeeded(result):
                        tool_stats["write_calls_succeeded"] += 1
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tool_call.id,
                "content": result,
            })
        messages.append({"role": "user", "content": tool_result_blocks})

    # If we hit max rounds, nudge the model to produce a final JSON answer with no more tools
    log.warning("Hit max tool rounds (%d), requesting final answer", max_tool_rounds)
    messages.append({
        "role": "user",
        "content": (
            "You have used all available tool rounds. Do not request any more tools. "
            "Reply now with the final JSON response that satisfies the required schema."
        ),
    })
    try:
        resp = _create_completion(messages, allow_tools=False)
        final_choice = resp.choices[0]
        final_content = final_choice.message.content or ""
        completion_chars += len(final_content)
        if _is_truncated(final_choice):
            _log_agent_loop(max_tool_rounds)
            return _truncation_failure("final_response", final_content)
        if not final_content.strip():
            log.warning("empty_final_response workflow=%s tools_called=%s", workflow, tools_called)
            _log_agent_loop(max_tool_rounds)
            if return_errors:
                return _failure_payload(
                    "empty_response",
                    "LLM returned empty final answer after max tool rounds",
                    response_text=final_content,
                    phase="final_response",
                )
            return None
        parsed = _parse_and_validate(final_content, repair_allowed=False, phase="final_response")
        _log_agent_loop(max_tool_rounds)
        return parsed
    except (APIError, APIConnectionError) as e:
        log.error("Final answer error: %s", e)
        if return_errors:
            return _failure_payload("llm_api_error", e, phase="final_completion")
        return None


def _clan_context(clan_data, war_data, roster_data=None, max_members=MAX_CONTEXT_MEMBERS_DEFAULT,
                   include_war=True):
    """Format clan data into a concise context string for the LLM.

    roster_data: optional enriched roster dict (from build_roster_data with
        include_cards=True). When provided, favorite cards are included per member.
    include_war: whether to append war status section. Set False for non-war
        contexts (Trophy Road observations, card discussions) to reduce noise.
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

    result = f"=== CLAN ROSTER ===\n" + "\n".join(member_summary)

    if include_war:
        deck_summary = "No active war data."
        if war_data and war_data.get("state") not in (None, "notInWar"):
            parts = war_data.get("clan", {}).get("participants", [])
            used = [p["name"] for p in parts if p.get("decksUsedToday", 0) > 0]
            unused = [p["name"] for p in parts if p.get("decksUsedToday", 0) == 0]
            deck_summary = (
                f"battled today: {', '.join(used) or 'nobody'} | "
                f"not yet: {', '.join(unused) or 'everyone done'}"
            )
        result += f"\n\n=== WAR DECKS TODAY ===\n{deck_summary}"

    return result


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
