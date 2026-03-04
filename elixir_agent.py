"""elixir_agent.py — LLM-powered observation and response engine for Elixir.

Uses OpenAI function calling to let the LLM query member history, war
results, and player details on demand.
"""

import json
import logging
import os

from openai import OpenAI

import cr_api
import cr_knowledge
import db

log = logging.getLogger("elixir_agent")

# Lazy client — only initialized when actually needed (allows tests to import without API key)
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client

MAX_TOOL_ROUNDS = 3

ELIXIR_PERSONALITY = (
    "You are Elixir 🧪, the sharp-minded chronicler and advisor for POAP KINGS, "
    "a Clash Royale clan. You know the game deeply — arenas, card donations, River Race, "
    "trophy pushing, Elder/Co-Leader promotions. You're confident, direct, and occasionally "
    "witty. You avoid repeating yourself. You always sign off with 🧪."
)

OBSERVE_SYSTEM = (
    ELIXIR_PERSONALITY + "\n\n"
    + cr_knowledge.get_knowledge_block() + "\n\n"
    "Your job: you are given a set of signals detected from the latest heartbeat check. "
    "Each signal represents something that actually changed — a trophy milestone, a new member, "
    "a war result, etc. Weave 2-3 of the most interesting signals into a single, natural "
    "Discord post. Don't just list them — tell a story. If a signal isn't interesting enough, "
    "skip it.\n\n"
    "You have tools available to look up member history, war results, and player details. "
    "Use them if you want more context before writing your post.\n\n"
    "Respond with JSON only (no markdown wrapper):\n"
    '{"event_type": "clan_observation|arena_milestone|donation_milestone|war_update|member_join|member_leave", '
    '"member_tags": [], "member_names": [], "summary": "one sentence", '
    '"content": "full Discord-ready markdown post", "metadata": {}}\n\n'
    "Or respond with exactly: null\n\nif the signals are genuinely not worth posting about."
)

LEADER_SYSTEM = (
    ELIXIR_PERSONALITY + "\n\n"
    + cr_knowledge.get_knowledge_block() + "\n\n"
    "You are answering a question from a clan leader in #leader-lounge. "
    "Base your answer on the clan data provided and use your tools to look up "
    "member history, war stats, or player details as needed. Be direct, give a "
    "concrete recommendation, and explain your reasoning briefly.\n\n"
    "You may be provided with recent conversation history with this leader. "
    "Use it for context — reference earlier questions and answers naturally. "
    "Don't repeat yourself if you already covered a topic recently.\n\n"
    "## Sharing to the clan\n"
    "A leader may ask you to share a point, insight, or announcement with the whole clan "
    "(e.g. \"share that with the clan\", \"post that to #elixir\", \"announce that\"). "
    "When they do, use event_type \"leader_share\" and include a \"share_content\" field "
    "with a message crafted for the whole clan in #elixir. The \"content\" field should be "
    "your reply to the leader confirming what you shared. "
    "The share_content should be written for a general clan audience — motivational, clear, "
    "and without referencing the private leader-lounge discussion.\n\n"
    "Respond with JSON only (no markdown wrapper):\n"
    '{"event_type": "leader_response", "member_tags": [], "member_names": [], '
    '"summary": "one sentence TL;DR", "content": "full Discord-ready markdown response", "metadata": {}}\n\n'
    "Or, when sharing to the clan:\n"
    '{"event_type": "leader_share", "member_tags": [], "member_names": [], '
    '"summary": "one sentence TL;DR", "content": "reply to the leader confirming the share", '
    '"share_content": "the clan-facing post for #elixir", "metadata": {}}'
)

RECEPTION_SYSTEM = (
    ELIXIR_PERSONALITY + "\n\n"
    "You are greeting a new member in the #reception channel of the POAP KINGS Discord server. "
    "They need to change their Discord **server nickname** to match their **Clash Royale in-game name** "
    "exactly so you can verify them and grant access to the rest of the server.\n\n"
    "**How to change nickname:**\n"
    "• Desktop — Right-click your name in the member list → Edit Server Profile → change nickname\n"
    "• Mobile — Tap the server name at the top → Edit Server Profile → change nickname\n\n"
    "The current clan roster is provided below. If they tell you their in-game name, confirm "
    "whether it's in the roster and remind them to set it as their nickname. "
    "If their name isn't in the roster, they may not be in the clan yet — tell them to join "
    "clan tag #J2RGCRVG in Clash Royale first.\n\n"
    "Be friendly, brief, and helpful. Don't use tools — just answer from the roster provided.\n\n"
    "Respond with JSON only (no markdown wrapper):\n"
    '{"event_type": "reception_response", "content": "your Discord-ready response"}'
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
        else:
            result = {"error": f"Unknown tool: {name}"}

        return json.dumps(result, default=str)
    except Exception as e:
        log.error("Tool execution error (%s): %s", name, e)
        return json.dumps({"error": str(e)})


def _parse_response(text):
    """Parse LLM JSON response, handling markdown fences."""
    text = text.strip()
    if text.lower() == "null":
        return None
    try:
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        log.error("Failed to parse agent response: %s\nRaw: %s", e, text)
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


def _clan_context(clan_data, war_data, recent):
    """Format clan data into a concise context string for the LLM."""
    members = clan_data.get("memberList", clan_data.get("members", []))
    member_summary = []
    for m in sorted(members, key=lambda x: x.get("clanRank", x.get("clan_rank", 99))):
        arena = m.get("arena", {})
        arena_name = arena.get("name", str(arena)) if isinstance(arena, dict) else str(arena)
        member_summary.append(
            f"  {m.get('name','?')} ({m.get('tag','?')}) | rank #{m.get('clanRank', m.get('clan_rank','?'))} | "
            f"{m.get('trophies',0):,} trophies | {m.get('donations',0)} donations | "
            f"role: {m.get('role','member')} | arena: {arena_name} | "
            f"last_seen: {m.get('lastSeen', m.get('last_seen','?'))}"
        )
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
    recent_summary = json.dumps(recent, indent=2) if recent else "None yet."
    return (
        f"=== CLAN ROSTER ===\n" + "\n".join(member_summary)
        + f"\n\n=== WAR STATUS ===\n{war_summary}"
        + f"\n\n=== RECENT ELIXIR POSTS (last {len(recent) if recent else 0}) ===\n{recent_summary}"
    )


def observe_and_post(clan_data, war_data, recent_entries, signals=None):
    """Observation with signals from heartbeat. Returns dict or None.

    signals: list of signal dicts from heartbeat.tick(), or None for legacy mode.
    """
    context = _clan_context(clan_data, war_data, recent_entries)

    if signals:
        signals_text = json.dumps(signals, indent=2, default=str)
        user_msg = (
            f"=== HEARTBEAT SIGNALS ===\n{signals_text}\n\n"
            f"{context}"
        )
    else:
        user_msg = context

    return _chat_with_tools(OBSERVE_SYSTEM, user_msg)


def respond_to_leader(question, author_name, clan_data, war_data, recent_entries,
                      conversation_history=None):
    """Leader Q&A with tool access and conversation memory. Returns dict or None."""
    context = _clan_context(clan_data, war_data, recent_entries)
    user_msg = f"Leader '{author_name}' asks: {question}\n\n{context}"
    return _chat_with_tools(LEADER_SYSTEM, user_msg,
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
    return _chat_with_tools(RECEPTION_SYSTEM, user_msg,
                            temperature=0.7, max_tokens=400)
