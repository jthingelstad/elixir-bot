"""elixir_agent.py — LLM-powered observation and response engine for Elixir.

Uses OpenAI function calling to let the LLM query member history, war
results, and player details on demand.

Personality, clan knowledge, and channel behaviors are loaded from
prompt files in the prompts/ directory.
"""

import json
import logging
import os
import subprocess
import time

from openai import OpenAI

import cr_api
import db
import prompts

log = logging.getLogger("elixir_agent")


def _get_build_hash():
    """Capture the git short hash at import time."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(__file__) or ".",
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


BUILD_HASH = _get_build_hash()

# Lazy client — only initialized when actually needed (allows tests to import without API key)
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=60)
    return _client

MAX_TOOL_ROUNDS = 3
LEADER_WRITE_TOOLS_ENABLED = True
MAX_CONTEXT_MEMBERS_DEFAULT = 30
MAX_CONTEXT_MEMBERS_FULL = 50
TOOL_RESULT_MAX_ITEMS = 8
TOOL_RESULT_MAX_CHARS = 3000


def _build_system_prompt(*sections):
    """Combine prompt sections into a single system prompt."""
    parts = [s for s in sections if s]
    parts.append(f"Your build version: {BUILD_HASH}")
    return "\n\n".join(parts)


def _observe_system():
    return _build_system_prompt(
        prompts.purpose(),
        prompts.knowledge_block(),
        prompts.channel_section("#elixir"),
        "You have tools available to look up member history, war results, and player details. "
        "Use them if you want more context before writing your post.\n\n"
        "The roster data includes each member's most-used cards from recent battles. "
        "Use this to add personality and specificity — mention signature cards, playstyles, "
        "or deck choices when they're relevant to the signal (e.g. a trophy milestone, war update).\n\n"
        "Respond with JSON only (no markdown wrapper):\n"
        '{"event_type": "clan_observation|arena_milestone|donation_milestone|war_update|member_join|member_leave", '
        '"member_tags": [], "member_names": [], "summary": "one sentence", '
        '"content": "full Discord-ready markdown post", "metadata": {}}\n\n'
        "Or respond with exactly: null\n\nif the signals are genuinely not worth posting about.",
    )


def _leader_system():
    return _build_system_prompt(
        prompts.purpose(),
        prompts.knowledge_block(),
        prompts.channel_section("#leader-lounge"),
        "You may be provided with recent conversation history with this leader. "
        "Use it for context — reference earlier questions and answers naturally. "
        "Don't repeat yourself if you already covered a topic recently.\n\n"
        "The roster includes each member's favorite cards and battle activity. "
        "Use this when answering questions about members — you can reference their playstyle, "
        "deck preferences, and card usage patterns to give richer, more specific answers.\n\n"
        "## Sharing to the clan\n"
        "A leader may ask you to share a point, insight, or announcement with the whole clan "
        "(e.g. \"share that with the clan\", \"post that to #elixir\", \"announce that\"). "
        "When they do, use event_type \"leader_share\" and include a \"share_content\" field "
        "with a message crafted for the whole clan in the broadcast channel. The \"content\" field should be "
        "your reply to the leader confirming what you shared. "
        "The share_content should be written for a general clan audience — motivational, clear, "
        "and without referencing the private leader discussion.\n\n"
        "Respond with JSON only (no markdown wrapper):\n"
        '{"event_type": "leader_response", "member_tags": [], "member_names": [], '
        '"summary": "one sentence TL;DR", "content": "full Discord-ready markdown response", "metadata": {}}\n\n'
        "Or, when sharing to the clan:\n"
        '{"event_type": "leader_share", "member_tags": [], "member_names": [], '
        '"summary": "one sentence TL;DR", "content": "reply to the leader confirming the share", '
        '"share_content": "the clan-facing post for the broadcast channel", "metadata": {}}',
    )


def _reception_system():
    return _build_system_prompt(
        prompts.purpose(),
        prompts.channel_section("#reception"),
        "Don't use tools — just answer from the roster provided.\n\n"
        "Respond with JSON only (no markdown wrapper):\n"
        '{"event_type": "reception_response", "content": "your Discord-ready response"}',
    )


def _home_message_system():
    return _build_system_prompt(
        prompts.purpose(),
        prompts.knowledge_block(),
        "Your job: write a short message (2-4 sentences) for the clan's public website home page. "
        "Visible to anyone, including people who aren't in the clan yet.\n\n"
        "Your audience is brand-new visitors who know nothing about the clan or you. "
        "Briefly introduce yourself (Elixir, the clan's AI chronicler) and the clan. "
        "Then give a peek into clan activity — wars, trophies, "
        "donations, milestones, and the cards our members love to play. "
        "Make visitors want to join. Use real details from the data.\n\n"
        "Guidelines:\n"
        "- Write in first person as the clan's AI chronicler\n"
        "- Be fresh — don't repeat what you said in your previous message\n"
        "- You can use simple markdown (**bold**, *italic*) for emphasis\n"
        "- No JSON — just the raw message text",
    )


def _members_message_system():
    return _build_system_prompt(
        prompts.purpose(),
        prompts.knowledge_block(),
        "Your job: write a short message (2-5 sentences) for the clan's Members page. "
        "Only current clan members see this page.\n\n"
        "Your audience is insiders. Be conversational, reference specific members by name, "
        "call out donation leaders, trophy movers, war heroes. Hype internal achievements. "
        "You can see each member's most-played cards — use this to add flavor "
        "(e.g. 'our resident Hog Rider main is on a tear').\n\n"
        "Guidelines:\n"
        "- Write in first person as the clan's AI chronicler\n"
        "- Be fresh — don't repeat what you said in your previous message\n"
        "- You can use simple markdown (**bold**, *italic*) for emphasis\n"
        "- No JSON — just the raw message text",
    )


def _roster_bios_system():
    return _build_system_prompt(
        prompts.purpose(),
        prompts.knowledge_block(),
        "Your job: write a short intro paragraph and per-member bios for the clan roster page.\n\n"
        "Output JSON only (no markdown wrapper):\n"
        '{"intro": "1-2 sentence intro for the roster page", '
        '"members": {"TAG": {"bio": "3-5 sentence member biography", '
        '"highlight": "donations|war|trophies|tenure|general"}}}\n\n'
        "Guidelines:\n"
        "- The intro should welcome visitors and set the tone\n"
        "- Each member gets a bio (3-5 sentences) — a short profile paragraph written in third person. "
        "Cover their role, how long they've been in the clan, notable stats (trophies, donations, war contributions), "
        "and something that makes them stand out. Be specific with real numbers from the data. "
        "Treat Co-Leaders the same as Leaders — refer to both simply as 'leader' (do not say 'co-leader'). "
        "Tone: warm, celebratory, like introducing a teammate to the world.\n"
        "- highlight categories: donations (generous donator), war (strong war contributor), "
        "trophies (high trophy count or recent push), tenure (long-time member), general (default)\n"
        "- Member data may include favorite_cards (top cards from recent battles) and current_deck. "
        "Reference card preferences in bios when available (e.g. 'Known for devastating Hog Rider pushes')\n"
        "- Use the member data, war stats, and donation info to personalize\n"
        "- You have tools available to look up member history and war stats if needed",
    )


def _promote_system():
    return _build_system_prompt(
        prompts.purpose(),
        prompts.knowledge_block(),
        "Your job: generate promotional messages for 5 channels to recruit new players.\n\n"
        "Output JSON only (no markdown wrapper):\n"
        '{"message": {"body": "SMS-friendly, short, include poapkings.com link"}, '
        '"social": {"body": "Twitter/Instagram post with stats and link"}, '
        '"email": {"subject": "...", "body": "detailed recruitment pitch"}, '
        '"discord": {"body": "formatted for Discord servers with emojis"}, '
        '"reddit": {"title": "r/RoyaleRecruit format", "body": "detailed post, NO clan invite link"}}\n\n'
        "Use real clan stats from the data provided. The roster includes members' favorite cards — "
        "mention popular cards and deck diversity to show the clan has active, strategic players. "
        "Keep the tone inviting and authentic.",
    )


def _event_system():
    """System prompt for generating event-driven messages (welcome, join, leave, etc.)."""
    return _build_system_prompt(
        prompts.purpose(),
        prompts.discord(),
        "You are generating a single Discord message in response to an event. "
        "The event details are provided below. Write a message appropriate for the "
        "channel and situation described. Be natural and in character.\n\n"
        "Respond with the message text only — no JSON, no markdown wrapper.",
    )


# ── Tool definitions for OpenAI function calling ────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_member_history",
            "description": "Get a clan member's trophy and donation history over time. Returns snapshots from the database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag (e.g. '#ABC123'). Look this up from the roster context.",
                    },
                    "days": {
                        "type": "integer",
                        "description": "How many days of history to retrieve. Default 30.",
                        "default": 30,
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_war_results",
            "description": "Get recent clan war (River Race) results. Shows our rank, fame, and whether we won.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent war results to retrieve. Default 5.",
                        "default": 5,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_war_stats",
            "description": "Get a specific member's war participation history — fame earned, decks used, which wars they participated in.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag (e.g. '#ABC123').",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_promotion_candidates",
            "description": "Evaluate which members with 'member' role meet the criteria for Elder promotion based on donations, activity, and war participation.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_details",
            "description": "Fetch detailed player stats from the Clash Royale API: best trophies, win/loss record, total donations, cards, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_tag": {
                        "type": "string",
                        "description": "The player tag (e.g. '#ABC123').",
                    },
                },
                "required": ["player_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_war_champ_standings",
            "description": "Get the current War Champ standings for this season — total fame per member across all war races. The top contributor at season end wins War Champ and a free Pass Royale.",
            "parameters": {
                "type": "object",
                "properties": {
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID. If omitted, uses the current/most recent season.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_member_birthday",
            "description": "Set a clan member's birthday (month and day).",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "Player tag (e.g. '#ABC123')",
                    },
                    "month": {
                        "type": "integer",
                        "description": "Birth month (1-12)",
                    },
                    "day": {
                        "type": "integer",
                        "description": "Birth day (1-31)",
                    },
                },
                "required": ["member_tag", "month", "day"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_member_join_date",
            "description": "Set or override a clan member's join date. Use when a leader provides or corrects a member's join date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "Player tag (e.g. '#ABC123')",
                    },
                    "date": {
                        "type": "string",
                        "description": "Join date in YYYY-MM-DD format",
                    },
                },
                "required": ["member_tag", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_perfect_war_participants",
            "description": "Find members who participated in every single war race of a season — perfect attendance. These players earn a free Pass Royale for their dedication.",
            "parameters": {
                "type": "object",
                "properties": {
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID. If omitted, uses the current/most recent season.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_member_profile_url",
            "description": "Set a clan member's profile URL (personal website, social media, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "Player tag (e.g. '#ABC123')",
                    },
                    "url": {
                        "type": "string",
                        "description": "Profile URL (must be https://)",
                    },
                },
                "required": ["member_tag", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_member_poap_address",
            "description": "Set a clan member's POAP wallet address (Ethereum address or ENS name).",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "Player tag (e.g. '#ABC123')",
                    },
                    "poap_address": {
                        "type": "string",
                        "description": "Ethereum address or ENS name for POAP collection",
                    },
                },
                "required": ["member_tag", "poap_address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_member_note",
            "description": "Set a clan member's note (e.g. 'Founder', 'War Machine'). Shows on the roster page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "Player tag (e.g. '#ABC123')",
                    },
                    "note": {
                        "type": "string",
                        "description": "Short note or title for the member",
                    },
                },
                "required": ["member_tag", "note"],
            },
        },
    },
]

TOOL_DEFINITIONS = []
for _tool in TOOLS:
    _name = _tool["function"]["name"]
    _side_effect = "write" if _name.startswith("set_member_") else "read"
    TOOL_DEFINITIONS.append({
        "tool": _tool,
        "name": _name,
        "side_effect": _side_effect,
    })

TOOL_DEFINITIONS_BY_NAME = {
    d["name"]: d for d in TOOL_DEFINITIONS
}

READ_TOOLS = [d["tool"] for d in TOOL_DEFINITIONS if d["side_effect"] == "read"]
WRITE_TOOLS = [d["tool"] for d in TOOL_DEFINITIONS if d["side_effect"] == "write"]
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS

TOOLSETS_BY_WORKFLOW = {
    "observe": READ_TOOLS,
    "leader": ALL_TOOLS,
    "reception": [],
    "roster_bios": READ_TOOLS,
}

RESPONSE_SCHEMAS_BY_WORKFLOW = {
    "observation": {"required": ["event_type", "summary", "content"]},
    "leader": {"required": ["event_type", "summary", "content"]},
    "reception": {"required": ["event_type", "content"]},
    "roster_bios": {"required": ["intro", "members"]},
}


def _execute_tool(name, arguments):
    """Execute a tool call and return the result as a string."""
    try:
        if name == "get_member_history":
            result = db.get_member_history(
                arguments["member_tag"],
                days=arguments.get("days", 30),
            )
        elif name == "get_war_results":
            result = db.get_war_history(n=arguments.get("count", 5))
        elif name == "get_member_war_stats":
            result = db.get_member_war_stats(arguments["member_tag"])
        elif name == "get_promotion_candidates":
            result = db.get_promotion_candidates()
        elif name == "get_player_details":
            result = cr_api.get_player(arguments["player_tag"])
        elif name == "get_war_champ_standings":
            result = db.get_war_champ_standings(
                season_id=arguments.get("season_id"),
            )
        elif name == "get_perfect_war_participants":
            result = db.get_perfect_war_participants(
                season_id=arguments.get("season_id"),
            )
        elif name == "set_member_birthday":
            db.set_member_birthday(
                arguments["member_tag"], name=None,
                month=arguments["month"], day=arguments["day"],
            )
            result = {"success": True}
        elif name == "set_member_join_date":
            db.set_member_join_date(
                arguments["member_tag"], name=None,
                joined_date=arguments["date"],
            )
            result = {"success": True}
        elif name == "set_member_profile_url":
            db.set_member_profile_url(
                arguments["member_tag"], name=None,
                url=arguments["url"],
            )
            result = {"success": True}
        elif name == "set_member_poap_address":
            db.set_member_poap_address(
                arguments["member_tag"], name=None,
                poap_address=arguments["poap_address"],
            )
            result = {"success": True}
        elif name == "set_member_note":
            db.set_member_note(
                arguments["member_tag"], name=None,
                note=arguments["note"],
            )
            result = {"success": True}
        else:
            result = {"error": f"Unknown tool: {name}"}

        return json.dumps(result, default=str)
    except Exception as e:
        log.error("Tool execution error (%s): %s", name, e)
        return json.dumps({"error": str(e)})


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

    if workflow == "observation":
        allowed = {
            "clan_observation", "arena_milestone", "donation_milestone",
            "war_update", "member_join", "member_leave",
        }
        et = parsed_obj.get("event_type")
        if et not in allowed:
            return False, f"invalid event_type for observation: {et}"
    elif workflow == "leader":
        et = parsed_obj.get("event_type")
        if et == "leader_response":
            pass
        elif et == "leader_share":
            if "share_content" not in parsed_obj:
                return False, "missing required field for leader_share: share_content"
        else:
            return False, f"invalid event_type for leader: {et}"
    elif workflow == "reception":
        if parsed_obj.get("event_type") != "reception_response":
            return False, f"invalid event_type for reception: {parsed_obj.get('event_type')}"
    elif workflow == "roster_bios":
        if not isinstance(parsed_obj.get("members"), dict):
            return False, "members must be an object map"

    return True, None


def _tool_names(tool_defs):
    return {t["function"]["name"] for t in (tool_defs or [])}


def _estimate_message_chars(messages):
    """Cheap prompt-size proxy for telemetry."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += len(json.dumps(content, default=str))
        elif content is not None:
            total += len(str(content))
    return total


def _build_tool_result_envelope(name, raw_result):
    """Normalize tool output into a compact envelope for model context."""
    try:
        parsed = json.loads(raw_result)
    except Exception:
        parsed = {"error": "tool_result_not_json", "raw": str(raw_result)[:500]}

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
                     allowed_tools=None, response_schema=None, strict_json=True):
    """Run a chat completion with tool-calling loop.

    conversation_history: optional list of {role, content} dicts to inject
        between the system prompt and the current user message (for leader Q&A memory).
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

    enable_write_tools = workflow == "leader" and LEADER_WRITE_TOOLS_ENABLED

    tools_called = []
    denied_tool_count = 0
    validation_failure_count = 0
    completion_latencies_ms = []
    completion_chars = 0

    def _create_completion(call_messages):
        start = time.perf_counter()
        kwargs = {
            "model": "gpt-4o",
            "messages": call_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": 60,
        }
        if allowed_tools:
            kwargs["tools"] = allowed_tools
            kwargs["tool_choice"] = "auto"
        resp = _get_client().chat.completions.create(**kwargs)
        completion_latencies_ms.append(round((time.perf_counter() - start) * 1000, 2))
        return resp

    def _parse_and_validate(content, repair_allowed):
        nonlocal validation_failure_count
        try:
            parsed = _parse_json_response(content) if strict_json else _parse_response(content or "null")
        except Exception as e:
            validation_failure_count += 1
            if not repair_allowed:
                log.warning("validation_failure workflow=%s reason=parse_error detail=%s", workflow, e)
                return None
            return "__REPAIR__", f"Invalid JSON. Error: {e}"

        ok, error = _validate_response(workflow, parsed, response_schema=response_schema)
        if ok:
            return parsed
        validation_failure_count += 1
        if not repair_allowed:
            log.warning("validation_failure workflow=%s reason=schema_error detail=%s", workflow, error)
            return None
        return "__REPAIR__", f"Schema validation failed: {error}"

    for _round in range(MAX_TOOL_ROUNDS + 1):
        try:
            resp = _create_completion(messages)
        except Exception as e:
            log.error("OpenAI API error: %s", e)
            return None

        choice = resp.choices[0]

        # If no tool calls, we have the final answer
        if not choice.message.tool_calls:
            completion_chars += len(choice.message.content or "")
            parsed = _parse_and_validate(choice.message.content or "null", repair_allowed=True)
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
                    return None

                repaired = repair_resp.choices[0].message.content or "null"
                completion_chars += len(repaired)
                parsed = _parse_and_validate(repaired, repair_allowed=False)

            prompt_chars = _estimate_message_chars(messages)
            log.info(
                "agent_loop workflow=%s tool_rounds=%d tools_called=%s denied_tools=%d "
                "validation_failures=%d prompt_chars=%d completion_chars=%d completion_latencies_ms=%s",
                workflow, _round, tools_called, denied_tool_count, validation_failure_count,
                prompt_chars, completion_chars, completion_latencies_ms,
            )
            return parsed

        # Process tool calls
        messages.append(choice.message)
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
        parsed = _parse_and_validate(resp.choices[0].message.content or "null", repair_allowed=False)
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


def _format_recent_posts(recent_posts):
    """Format recent post history for inclusion in LLM context."""
    if not recent_posts:
        return ""
    lines = []
    for p in recent_posts:
        ts = p.get("recorded_at", "")
        content = p.get("content", "")
        lines.append(f"  [{ts}] {content[:200]}")
    return "\n=== YOUR RECENT POSTS IN #elixir ===\n" + "\n".join(lines) + "\n"


def observe_and_post(clan_data, war_data, signals=None, recent_posts=None):
    """Observation with signals from heartbeat. Returns dict or None.

    signals: list of signal dicts from heartbeat.tick(), or None for legacy mode.
    recent_posts: list of recent conversation dicts from db.get_conversation_history().
    """
    context = _clan_context(clan_data, war_data, max_members=MAX_CONTEXT_MEMBERS_DEFAULT)

    if signals:
        signals_text = json.dumps(signals, indent=2, default=str)
        user_msg = (
            f"=== HEARTBEAT SIGNALS ===\n{signals_text}\n\n"
            f"{context}"
        )
    else:
        user_msg = context

    user_msg += _format_recent_posts(recent_posts)

    return _chat_with_tools(
        _observe_system(), user_msg,
        workflow="observation",
        allowed_tools=TOOLSETS_BY_WORKFLOW["observe"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["observation"],
        strict_json=True,
    )


def respond_to_leader(question, author_name, clan_data, war_data,
                      conversation_history=None):
    """Leader Q&A with tool access and conversation memory. Returns dict or None."""
    context = _clan_context(clan_data, war_data, max_members=MAX_CONTEXT_MEMBERS_DEFAULT)
    user_msg = f"Leader '{author_name}' asks: {question}\n\n{context}"
    return _chat_with_tools(
        _leader_system(), user_msg,
        conversation_history=conversation_history,
        workflow="leader",
        allowed_tools=TOOLSETS_BY_WORKFLOW["leader"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["leader"],
        strict_json=True,
    )


def respond_in_reception(question, author_name, clan_data):
    """Onboarding Q&A in #reception. No tools needed. Returns dict or None."""
    members = clan_data.get("memberList", clan_data.get("members", []))
    roster = "\n".join(
        f"  {m.get('name', '?')} ({m.get('tag', '?')})"
        for m in members
    ) or "  (roster unavailable)"
    user_msg = (
        f"New member '{author_name}' asks: {question}\n\n"
        f"=== CLAN ROSTER ===\n{roster}"
    )
    return _chat_with_tools(
        _reception_system(), user_msg,
        temperature=0.7, max_tokens=400,
        workflow="reception",
        allowed_tools=TOOLSETS_BY_WORKFLOW["reception"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["reception"],
        strict_json=True,
    )


# ── Event-driven message generation ──────────────────────────────────────────

def generate_message(event, context, recent_posts=None):
    """Generate a single Discord message for an event using the LLM.

    event: short description of what happened (e.g. "new_member_discord_join")
    context: string with all relevant details for the LLM
    recent_posts: optional list of recent post dicts to avoid repetition.

    Returns message text, or None on failure.
    """
    user_msg = f"Event: {event}\n\n{context}"
    user_msg += _format_recent_posts(recent_posts)
    messages = [
        {"role": "system", "content": _event_system()},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=300,
            timeout=60,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text or text.lower() == "null":
            return None
        return text
    except Exception as e:
        log.error("generate_message error (%s): %s", event, e)
        return None


# ── Site content generation for poapkings.com ────────────────────────────────

def generate_home_message(clan_data, war_data, previous_message, roster_data=None):
    """Generate a message for the poapkings.com home page. Returns text or None."""
    context = _clan_context(
        clan_data, war_data,
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    prev_text = f"Your previous message: {previous_message}" if previous_message else "(none yet)"
    user_msg = f"{context}\n\n{prev_text}\n\nWrite your next message for the home page."

    messages = [
        {"role": "system", "content": _home_message_system()},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.9,
            max_tokens=300,
            timeout=60,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text or text.lower() == "null":
            return None
        return text
    except Exception as e:
        log.error("Home message API error: %s", e)
        return None


def generate_members_message(clan_data, war_data, previous_message, roster_data=None):
    """Generate a message for the poapkings.com members page. Returns text or None."""
    context = _clan_context(
        clan_data, war_data,
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    prev_text = f"Your previous message: {previous_message}" if previous_message else "(none yet)"
    user_msg = f"{context}\n\n{prev_text}\n\nWrite your next message for the members page."

    messages = [
        {"role": "system", "content": _members_message_system()},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.9,
            max_tokens=400,
            timeout=60,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text or text.lower() == "null":
            return None
        return text
    except Exception as e:
        log.error("Members message API error: %s", e)
        return None


def generate_roster_bios(clan_data, war_data, roster_data=None):
    """Generate roster intro and per-member bios. Returns dict or None."""
    context = _clan_context(
        clan_data, war_data,
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    members = clan_data.get("memberList", clan_data.get("members", []))
    member_tags = [m.get("tag", "") for m in members]
    user_msg = (
        f"{context}\n\n"
        f"Generate an intro and bio for each member.\n"
        f"Member tags to cover: {', '.join(member_tags)}"
    )
    return _chat_with_tools(
        _roster_bios_system(), user_msg,
        temperature=0.8, max_tokens=2000,
        workflow="roster_bios",
        allowed_tools=TOOLSETS_BY_WORKFLOW["roster_bios"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["roster_bios"],
        strict_json=True,
    )


def generate_promote_content(clan_data, roster_data=None):
    """Generate promotional messages for 5 channels. Returns dict or None."""
    context = _clan_context(
        clan_data, {},
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    user_msg = f"{context}\n\nGenerate promotional messages for all 5 channels."

    messages = [
        {"role": "system", "content": _promote_system()},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.8,
            max_tokens=1500,
            timeout=60,
        )
        return _parse_response(resp.choices[0].message.content or "null")
    except Exception as e:
        log.error("Promote API error: %s", e)
        return None
