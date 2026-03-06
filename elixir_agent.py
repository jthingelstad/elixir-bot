"""elixir_agent.py — LLM-powered observation and response engine for Elixir.

Uses OpenAI function calling to let the LLM query member history, war
results, and player details on demand.

Personality, clan knowledge, and channel behaviors are loaded from
prompt files in the prompts/ directory.
"""

import json
import logging
import os

from openai import OpenAI

import cr_api
import db
import prompts

log = logging.getLogger("elixir_agent")

# Lazy client — only initialized when actually needed (allows tests to import without API key)
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client

MAX_TOOL_ROUNDS = 3


def _build_system_prompt(*sections):
    """Combine prompt sections into a single system prompt."""
    return "\n\n".join(s for s in sections if s)


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
        "Your audience is the public. Give a peek into clan activity — wars, trophies, "
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


def _chat_with_tools(system_prompt, user_message, conversation_history=None,
                     temperature=0.7, max_tokens=800):
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

    for _round in range(MAX_TOOL_ROUNDS + 1):
        try:
            resp = _get_client().chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            log.error("OpenAI API error: %s", e)
            return None

        choice = resp.choices[0]

        # If no tool calls, we have the final answer
        if not choice.message.tool_calls:
            return _parse_response(choice.message.content or "null")

        # Process tool calls
        messages.append(choice.message)
        for tool_call in choice.message.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            log.info("Tool call: %s(%s)", fn_name, fn_args)
            result = _execute_tool(fn_name, fn_args)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    # If we hit max rounds, try to get a final answer without tools
    log.warning("Hit max tool rounds (%d), requesting final answer", MAX_TOOL_ROUNDS)
    try:
        resp = _get_client().chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _parse_response(resp.choices[0].message.content or "null")
    except Exception as e:
        log.error("Final answer error: %s", e)
        return None


def _clan_context(clan_data, war_data, roster_data=None):
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
    for m in sorted(members, key=lambda x: x.get("clanRank", x.get("clan_rank", 99))):
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


def observe_and_post(clan_data, war_data, signals=None):
    """Observation with signals from heartbeat. Returns dict or None.

    signals: list of signal dicts from heartbeat.tick(), or None for legacy mode.
    """
    context = _clan_context(clan_data, war_data)

    if signals:
        signals_text = json.dumps(signals, indent=2, default=str)
        user_msg = (
            f"=== HEARTBEAT SIGNALS ===\n{signals_text}\n\n"
            f"{context}"
        )
    else:
        user_msg = context

    return _chat_with_tools(_observe_system(), user_msg)


def respond_to_leader(question, author_name, clan_data, war_data,
                      conversation_history=None):
    """Leader Q&A with tool access and conversation memory. Returns dict or None."""
    context = _clan_context(clan_data, war_data)
    user_msg = f"Leader '{author_name}' asks: {question}\n\n{context}"
    return _chat_with_tools(_leader_system(), user_msg,
                            conversation_history=conversation_history)


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
    return _chat_with_tools(_reception_system(), user_msg,
                            temperature=0.7, max_tokens=400)


# ── Event-driven message generation ──────────────────────────────────────────

def generate_message(event, context):
    """Generate a single Discord message for an event using the LLM.

    event: short description of what happened (e.g. "new_member_discord_join")
    context: string with all relevant details for the LLM

    Returns message text, or None on failure.
    """
    user_msg = f"Event: {event}\n\n{context}"
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
    context = _clan_context(clan_data, war_data, roster_data=roster_data)
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
    context = _clan_context(clan_data, war_data, roster_data=roster_data)
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
    context = _clan_context(clan_data, war_data, roster_data=roster_data)
    members = clan_data.get("memberList", clan_data.get("members", []))
    member_tags = [m.get("tag", "") for m in members]
    user_msg = (
        f"{context}\n\n"
        f"Generate an intro and bio for each member.\n"
        f"Member tags to cover: {', '.join(member_tags)}"
    )
    return _chat_with_tools(_roster_bios_system(), user_msg,
                            temperature=0.8, max_tokens=2000)


def generate_promote_content(clan_data, roster_data=None):
    """Generate promotional messages for 5 channels. Returns dict or None."""
    context = _clan_context(clan_data, {}, roster_data=roster_data)
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
        )
        return _parse_response(resp.choices[0].message.content or "null")
    except Exception as e:
        log.error("Promote API error: %s", e)
        return None
