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
import runtime_status

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
CLANOPS_WRITE_TOOLS_ENABLED = os.getenv("CLANOPS_WRITE_TOOLS_ENABLED", "1") != "0"
MAX_CONTEXT_MEMBERS_DEFAULT = 30
MAX_CONTEXT_MEMBERS_FULL = 50
TOOL_RESULT_MAX_ITEMS = 50
TOOL_RESULT_MAX_CHARS = 12000


def _build_system_prompt(*sections):
    """Combine prompt sections into a single system prompt."""
    parts = [s for s in sections if s]
    parts.append(f"Your build version: {BUILD_HASH}")
    return "\n\n".join(parts)


def _create_chat_completion(*, workflow, messages, model="gpt-4o", temperature=0.7, max_tokens=800, timeout=60, tools=None, tool_choice=None):
    started = time.perf_counter()
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    try:
        resp = _get_client().chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        runtime_status.record_openai_call(
            workflow,
            ok=True,
            model=model,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        )
        return resp
    except Exception as exc:
        runtime_status.record_openai_call(
            workflow,
            ok=False,
            model=model,
            error=exc,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        raise


def _observe_system():
    announcements = prompts.discord_singleton_channel("announcements")
    return _build_system_prompt(
        prompts.purpose(),
        prompts.knowledge_block(),
        prompts.channel_section(announcements["name"]),
        "You have tools available to look up the full roster, member profiles, recent form, deck data, and war status. "
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


def _interactive_system(channel_name, proactive=False):
    proactive_block = (
        "You are observing an ongoing channel conversation. Only reply if you can add clear value. "
        "If you do not have something genuinely useful to add, respond with exactly null.\n\n"
        if proactive
        else ""
    )
    return _build_system_prompt(
        prompts.purpose(),
        prompts.knowledge_block(),
        prompts.channel_section(channel_name),
        "This is an interactive read-only channel. "
        "You may answer questions, explain, analyze, summarize, and help members or leaders interpret clan data. "
        "Do not use write tools. Do not recommend or direct promotions, demotions, or kicks here.\n\n"
        "You have read-only tools for member resolution, the full roster, member profiles, current decks, signature cards, recent form, war status, and battle analytics. "
        "Resolve members by name or Discord handle instead of guessing.\n\n"
        "A user may ask you to share something with the clan. When they do, use event_type \"channel_share\" and include a \"share_content\" field. "
        "If they specify a target like #arena-relay, include \"share_channel\" with that exact channel name. Otherwise default to the primary announcements channel.\n\n"
        f"{proactive_block}"
        "Respond with JSON only (no markdown wrapper):\n"
        '{"event_type": "channel_response", "member_tags": [], "member_names": [], '
        '"summary": "one sentence TL;DR", "content": "full Discord-ready markdown response", "metadata": {}}\n\n'
        "Or, when sharing to the clan:\n"
        '{"event_type": "channel_share", "member_tags": [], "member_names": [], '
        '"summary": "one sentence TL;DR", "content": "reply in the current channel", '
        '"share_content": "the clan-facing post for the target channel", "share_channel": "#arena-relay", "metadata": {}}',
    )


def _clanops_system(channel_name, proactive=False):
    proactive_block = (
        "You are observing a private clan operations discussion. Only interject when you have concrete value to add. "
        "If you do not have a strong, relevant contribution, respond with exactly null.\n\n"
        if proactive
        else ""
    )
    return _build_system_prompt(
        prompts.purpose(),
        prompts.knowledge_block(),
        prompts.channel_section(channel_name),
        "This is a private clan operations channel. "
        "This is the right place to discuss promotions, demotions, kicks, roster corrections, and leadership decisions. "
        "You may use both read and write tools here when necessary.\n\n"
        "Use tools to ground factual claims. Be direct, concrete, and operational. "
        "If a member is referenced by name or Discord handle, resolve them first instead of guessing.\n\n"
        "A user may ask you to share something with the clan. When they do, use event_type \"channel_share\" and include a \"share_content\" field. "
        "If they specify a target like #arena-relay, include \"share_channel\" with that exact channel name. Otherwise default to the primary announcements channel.\n\n"
        f"{proactive_block}"
        "Respond with JSON only (no markdown wrapper):\n"
        '{"event_type": "channel_response", "member_tags": [], "member_names": [], '
        '"summary": "one sentence TL;DR", "content": "full Discord-ready markdown response", "metadata": {}}\n\n'
        "Or, when sharing to the clan:\n"
        '{"event_type": "channel_share", "member_tags": [], "member_names": [], '
        '"summary": "one sentence TL;DR", "content": "reply in the current channel", '
        '"share_content": "the clan-facing post for the target channel", "share_channel": "#arena-relay", "metadata": {}}',
    )


def _reception_system():
    onboarding = prompts.discord_singleton_channel("onboarding")
    return _build_system_prompt(
        prompts.purpose(),
        prompts.channel_section(onboarding["name"]),
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
            "name": "resolve_member",
            "description": "Resolve a clan member from a player name, alias, Discord handle, or player tag and return the best matching candidates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Player name, alias, Discord handle, or player tag.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of candidates to return. Default 5.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clan_roster_summary",
            "description": "Get a high-level roster summary including member count, open slots, average level, and average trophies.",
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
            "name": "list_clan_members",
            "description": "List the current clan members with their role, level, trophies, rank, join date, and Discord linkage when available.",
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
            "name": "list_longest_tenure_members",
            "description": "List the longest-tenured active clan members using the tracked join dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of members to return. Default 10.",
                        "default": 10,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_joins",
            "description": "List members who joined recently along with their recent form and current-season war contribution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "How many days back to consider recent joins. Default 30.",
                        "default": 30,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_profile",
            "description": "Get a normalized current profile for a clan member including join date, role, level, trophies, notes, recent form, and Discord linkage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_overview",
            "description": "Get a combined member overview with current profile, recent form, deck info, and current war status in one response.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_recent_form",
            "description": "Get a member's recent form summary such as wins/losses, streak, and whether they are hot, mixed, or slumping.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Recent form scope. Default competitive_10.",
                        "default": "competitive_10",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_current_deck",
            "description": "Get the member's latest known current deck from stored player profile data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_next_chests",
            "description": "Fetch a member's upcoming chest cycle directly from the Clash Royale API.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_signature_cards",
            "description": "Get the member's most-used cards from recent battle logs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                    "mode_scope": {
                        "type": "string",
                        "description": "Mode scope for card usage. Default overall.",
                        "default": "overall",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_history",
            "description": "Get a clan member's trophy and donation history over time from the stored state snapshots.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
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
            "name": "get_member_war_stats",
            "description": "Get a specific member's war participation history — fame earned, decks used, and race context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_war_status",
            "description": "Get a member's current-day war deck status and current-season participation summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID. If omitted, uses the current/most recent season.",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_war_attendance",
            "description": "Get a member's war attendance summary for the current season and the last 4 weeks, including participation rate and races missed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID. If omitted, uses the current/most recent season.",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_war_battle_record",
            "description": "Get a member's war-battle win/loss/draw record for the selected season using stored battle-log facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID. If omitted, uses the current/most recent season.",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_war_status",
            "description": "Get the current clan war state, latest known season/week, and current race rank.",
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
            "name": "get_war_season_summary",
            "description": "Get a season-level war summary including races, fame-per-member, top contributors, and members with no war participation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID. If omitted, uses the current/most recent season.",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "How many top contributors to include. Default 5.",
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
            "name": "get_war_deck_status_today",
            "description": "List who has used all, some, or none of their war decks today.",
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
            "name": "get_members_without_war_participation",
            "description": "List active members who have not used any war decks in the selected season.",
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
            "name": "get_war_battle_win_rates",
            "description": "List the active members with the highest war-battle win rates this season.",
            "parameters": {
                "type": "object",
                "properties": {
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID. If omitted, uses the current/most recent season.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of members to return. Default 10.",
                        "default": 10,
                    },
                    "min_battles": {
                        "type": "integer",
                        "description": "Minimum war battles required to be included. Default 1.",
                        "default": 1,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clan_boat_battle_record",
            "description": "Get the clan's aggregate boat-battle win/loss/draw record over the most recent N war races.",
            "parameters": {
                "type": "object",
                "properties": {
                    "wars": {
                        "type": "integer",
                        "description": "How many recent war races to include. Default 3.",
                        "default": 3,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_war_score_trend",
            "description": "Summarize whether the clan's war score/rating trend has moved up or down over the selected recent window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "How many days back to compare. Default 30.",
                        "default": 30,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_fame_per_member_to_previous_season",
            "description": "Compare this season's fame-per-member to the previous season.",
            "parameters": {
                "type": "object",
                "properties": {
                    "season_id": {
                        "type": "integer",
                        "description": "Optional current season ID. If omitted, uses the current/most recent season.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_war_champ_standings",
            "description": "Get the current War Champ standings for this season — total fame per member across all war races.",
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
            "name": "get_recent_role_changes",
            "description": "List recent member promotions or demotions by comparing tracked role snapshots.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "How many days back to inspect for role changes. Default 30.",
                        "default": 30,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_member_missed_war_days",
            "description": "List the war days a member missed in the selected season based on tracked daily war status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID. If omitted, uses the current/most recent season.",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_member_war_to_clan_average",
            "description": "Compare one member's war contribution to the clan average for the selected season.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID. If omitted, uses the current/most recent season.",
                    },
                },
                "required": ["member_tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trending_war_contributors",
            "description": "List the members whose recent war contribution is trending upward relative to their earlier season performance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID. If omitted, uses the current/most recent season.",
                    },
                    "recent_races": {
                        "type": "integer",
                        "description": "How many most recent races to treat as the trend window. Default 2.",
                        "default": 2,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of members to return. Default 5.",
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
            "name": "get_members_at_risk",
            "description": "List members currently flagged by configurable participation/activity thresholds, including the reasons they were flagged.",
            "parameters": {
                "type": "object",
                "properties": {
                    "inactivity_days": {
                        "type": "integer",
                        "description": "Flag members inactive for at least this many days. Default 7.",
                        "default": 7,
                    },
                    "min_donations_week": {
                        "type": "integer",
                        "description": "Flag members below this weekly donation count. Default 20.",
                        "default": 20,
                    },
                    "require_war_participation": {
                        "type": "boolean",
                        "description": "Whether to include war participation as a risk criterion. Default false.",
                        "default": False,
                    },
                    "min_war_races": {
                        "type": "integer",
                        "description": "Minimum race participation if war participation is required. Default 1.",
                        "default": 1,
                    },
                    "tenure_grace_days": {
                        "type": "integer",
                        "description": "Ignore very new members younger than this many days. Default 14.",
                        "default": 14,
                    },
                    "season_id": {
                        "type": "integer",
                        "description": "Optional season ID for war participation checks.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_members_on_losing_streak",
            "description": "List active members on a current losing streak so leaders can spot who may need support.",
            "parameters": {
                "type": "object",
                "properties": {
                    "min_streak": {
                        "type": "integer",
                        "description": "Minimum current losing streak to include. Default 3.",
                        "default": 3,
                    },
                    "scope": {
                        "type": "string",
                        "description": "Recent form scope. Default competitive_10.",
                        "default": "competitive_10",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trophy_drops",
            "description": "Get members with notable trophy drops over the last N days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Window in days. Default 7.",
                        "default": 7,
                    },
                    "min_drop": {
                        "type": "integer",
                        "description": "Minimum trophy drop to include. Default 100.",
                        "default": 100,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_details",
            "description": "Fetch fresh player stats directly from the Clash Royale API when raw live details are needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_tag": {
                        "type": "string",
                        "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                    },
                },
                "required": ["player_tag"],
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
                        "description": "Player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie')",
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
                        "description": "Player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie')",
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
                        "description": "Player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie')",
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
            "name": "set_member_note",
            "description": "Set a clan member's note (e.g. 'Founder', 'War Machine'). Shows on the roster page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "member_tag": {
                        "type": "string",
                        "description": "Player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie')",
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
    "interactive": READ_TOOLS,
    "interactive_proactive": READ_TOOLS,
    "clanops": ALL_TOOLS,
    "clanops_proactive": ALL_TOOLS,
    "reception": [],
    "roster_bios": READ_TOOLS,
}

RESPONSE_SCHEMAS_BY_WORKFLOW = {
    "observation": {"required": ["event_type", "summary", "content"]},
    "interactive": {"required": ["event_type", "summary", "content"]},
    "interactive_proactive": {"required": ["event_type", "summary", "content"]},
    "clanops": {"required": ["event_type", "summary", "content"]},
    "clanops_proactive": {"required": ["event_type", "summary", "content"]},
    "reception": {"required": ["event_type", "content"]},
    "roster_bios": {"required": ["intro", "members"]},
}


def _refresh_member_cache(member_tag, include_battles=False):
    """Refresh stored player profile and optionally battle log for a member."""
    player = cr_api.get_player(member_tag)
    if player:
        db.snapshot_player_profile(player)
    if include_battles:
        battles = cr_api.get_player_battle_log(member_tag)
        if battles:
            db.snapshot_player_battlelog(member_tag, battles)


def _resolve_member_tag(value):
    """Accept a tag, name, alias, or Discord handle and return a canonical player tag."""
    query = (value or "").strip()
    if not query:
        raise ValueError("member reference is required")
    if query.startswith("#"):
        return query

    matches = db.resolve_member(query, limit=5)
    if not matches:
        raise ValueError(f"Could not resolve member reference: {query}")
    exactish = [m for m in matches if m.get("match_score", 0) >= 850]
    if len(exactish) == 1:
        return exactish[0]["player_tag"]
    if len(matches) == 1:
        return matches[0]["player_tag"]
    top = matches[0]
    second = matches[1]
    if (top.get("match_score", 0) - second.get("match_score", 0)) >= 100:
        return top["player_tag"]
    choices = ", ".join(m.get("member_ref_with_handle") or m.get("current_name") or m["player_tag"] for m in matches[:3])
    raise ValueError(f"Ambiguous member reference '{query}'. Top matches: {choices}")


def _execute_tool(name, arguments):
    """Execute a tool call and return the result as a string."""
    try:
        if name == "resolve_member":
            result = db.resolve_member(
                arguments["query"],
                limit=arguments.get("limit", 5),
            )
        elif name == "get_clan_roster_summary":
            result = db.get_clan_roster_summary()
        elif name == "list_clan_members":
            result = db.list_members()
        elif name == "list_longest_tenure_members":
            result = db.list_longest_tenure_members(
                limit=arguments.get("limit", 10),
            )
        elif name == "list_recent_joins":
            result = db.list_recent_joins(
                days=arguments.get("days", 30),
            )
        elif name == "get_member_profile":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=True)
            result = db.get_member_profile(member_tag)
        elif name == "get_member_overview":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=True)
            result = db.get_member_overview(member_tag)
        elif name == "get_member_recent_form":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=True)
            result = db.get_member_recent_form(
                member_tag,
                scope=arguments.get("scope", "competitive_10"),
            )
        elif name == "get_member_current_deck":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=False)
            result = db.get_member_current_deck(member_tag)
        elif name == "get_member_next_chests":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = cr_api.get_player_chests(member_tag)
        elif name == "get_member_signature_cards":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=True)
            result = db.get_member_signature_cards(
                member_tag,
                mode_scope=arguments.get("mode_scope", "overall"),
            )
        elif name == "get_member_history":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.get_member_history(
                member_tag,
                days=arguments.get("days", 30),
            )
        elif name == "get_member_war_stats":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.get_member_war_stats(member_tag)
        elif name == "get_member_war_status":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.get_member_war_status(
                member_tag,
                season_id=arguments.get("season_id"),
            )
        elif name == "get_member_war_attendance":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.get_member_war_attendance(
                member_tag,
                season_id=arguments.get("season_id"),
            )
        elif name == "get_member_war_battle_record":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=True)
            result = db.get_member_war_battle_record(
                member_tag,
                season_id=arguments.get("season_id"),
            )
        elif name == "get_current_war_status":
            result = db.get_current_war_status()
        elif name == "get_war_season_summary":
            result = db.get_war_season_summary(
                season_id=arguments.get("season_id"),
                top_n=arguments.get("top_n", 5),
            )
        elif name == "get_war_deck_status_today":
            result = db.get_war_deck_status_today()
        elif name == "get_members_without_war_participation":
            result = db.get_members_without_war_participation(
                season_id=arguments.get("season_id"),
            )
        elif name == "get_trending_war_contributors":
            result = db.get_trending_war_contributors(
                season_id=arguments.get("season_id"),
                recent_races=arguments.get("recent_races", 2),
                limit=arguments.get("limit", 5),
            )
        elif name == "get_promotion_candidates":
            result = db.get_promotion_candidates()
        elif name == "compare_member_war_to_clan_average":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.compare_member_war_to_clan_average(
                member_tag,
                season_id=arguments.get("season_id"),
            )
        elif name == "get_members_at_risk":
            result = db.get_members_at_risk(
                inactivity_days=arguments.get("inactivity_days", 7),
                min_donations_week=arguments.get("min_donations_week", 20),
                require_war_participation=arguments.get("require_war_participation", False),
                min_war_races=arguments.get("min_war_races", 1),
                tenure_grace_days=arguments.get("tenure_grace_days", 14),
                season_id=arguments.get("season_id"),
            )
        elif name == "get_members_on_losing_streak":
            result = db.get_members_on_losing_streak(
                min_streak=arguments.get("min_streak", 3),
                scope=arguments.get("scope", "competitive_10"),
            )
        elif name == "get_trophy_drops":
            result = db.get_trophy_drops(
                days=arguments.get("days", 7),
                min_drop=arguments.get("min_drop", 100),
            )
        elif name == "get_player_details":
            player_tag = _resolve_member_tag(arguments["player_tag"])
            result = cr_api.get_player(player_tag)
        elif name == "get_war_champ_standings":
            result = db.get_war_champ_standings(
                season_id=arguments.get("season_id"),
            )
        elif name == "get_war_battle_win_rates":
            result = db.get_war_battle_win_rates(
                season_id=arguments.get("season_id"),
                limit=arguments.get("limit", 10),
                min_battles=arguments.get("min_battles", 1),
            )
        elif name == "get_clan_boat_battle_record":
            result = db.get_clan_boat_battle_record(
                wars=arguments.get("wars", 3),
            )
        elif name == "get_war_score_trend":
            result = db.get_war_score_trend(
                days=arguments.get("days", 30),
            )
        elif name == "compare_fame_per_member_to_previous_season":
            result = db.compare_fame_per_member_to_previous_season(
                season_id=arguments.get("season_id"),
            )
        elif name == "get_recent_role_changes":
            result = db.get_recent_role_changes(
                days=arguments.get("days", 30),
            )
        elif name == "get_member_missed_war_days":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.get_member_missed_war_days(
                member_tag,
                season_id=arguments.get("season_id"),
            )
        elif name == "get_perfect_war_participants":
            result = db.get_perfect_war_participants(
                season_id=arguments.get("season_id"),
            )
        elif name == "set_member_birthday":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            db.set_member_birthday(
                member_tag, name=None,
                month=arguments["month"], day=arguments["day"],
            )
            result = {"success": True}
        elif name == "set_member_join_date":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            db.set_member_join_date(
                member_tag, name=None,
                joined_date=arguments["date"],
            )
            result = {"success": True}
        elif name == "set_member_profile_url":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            db.set_member_profile_url(
                member_tag, name=None,
                url=arguments["url"],
            )
            result = {"success": True}
        elif name == "set_member_note":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            db.set_member_note(
                member_tag, name=None,
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
        if workflow in {"interactive_proactive", "clanops_proactive"}:
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
    elif workflow == "reception":
        if parsed_obj.get("event_type") != "reception_response":
            return False, f"invalid event_type for reception: {parsed_obj.get('event_type')}"
    elif workflow in {"interactive", "interactive_proactive", "clanops", "clanops_proactive"}:
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

    enable_write_tools = workflow in {"clanops", "clanops_proactive"} and CLANOPS_WRITE_TOOLS_ENABLED

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
            model="gpt-4o",
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=60,
            tools=allowed_tools if allowed_tools else None,
            tool_choice="auto" if allowed_tools else None,
        )
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

    return ("\n\n" + "\n\n".join(sections)) if sections else ""


def observe_and_post(clan_data, war_data, signals=None, recent_posts=None, memory_context=None):
    """Observation with signals from heartbeat. Returns dict or None.

    signals: list of signal dicts from heartbeat.tick(), or None when no detector output is being passed.
    recent_posts: list of recent message dicts from db.list_channel_messages().
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
    user_msg += _format_memory_context(memory_context)

    return _chat_with_tools(
        _observe_system(), user_msg,
        workflow="observation",
        allowed_tools=TOOLSETS_BY_WORKFLOW["observe"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["observation"],
        strict_json=True,
    )


def respond_in_reception(question, author_name, clan_data, memory_context=None):
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
    user_msg += _format_memory_context(memory_context)
    return _chat_with_tools(
        _reception_system(), user_msg,
        temperature=0.7, max_tokens=400,
        workflow="reception",
        allowed_tools=TOOLSETS_BY_WORKFLOW["reception"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["reception"],
        strict_json=True,
    )


def respond_in_channel(question, author_name, channel_name, workflow, clan_data, war_data,
                       conversation_history=None, memory_context=None, proactive=False):
    """Channel Q&A for interactive/clanops workflows."""
    if workflow not in {"interactive", "clanops"}:
        raise ValueError(f"unsupported channel workflow: {workflow}")
    context = _clan_context(clan_data, war_data, max_members=MAX_CONTEXT_MEMBERS_DEFAULT)
    speaker = "Observed message from" if proactive else "Message from"
    user_msg = f"{speaker} '{author_name}' in {channel_name}: {question}\n\n{context}"
    user_msg += _format_memory_context(memory_context)
    workflow_key = f"{workflow}_proactive" if proactive else workflow
    system_prompt = (
        _interactive_system(channel_name, proactive=proactive)
        if workflow == "interactive"
        else _clanops_system(channel_name, proactive=proactive)
    )
    return _chat_with_tools(
        system_prompt,
        user_msg,
        conversation_history=conversation_history,
        workflow=workflow_key,
        allowed_tools=TOOLSETS_BY_WORKFLOW[workflow_key],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW[workflow_key],
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
        resp = _create_chat_completion(
            workflow=f"event:{event}",
            messages=messages,
            model="gpt-4o",
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
        resp = _create_chat_completion(
            workflow="site_home_message",
            messages=messages,
            model="gpt-4o",
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
        resp = _create_chat_completion(
            workflow="site_members_message",
            messages=messages,
            model="gpt-4o",
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
        resp = _create_chat_completion(
            workflow="site_promote_content",
            messages=messages,
            model="gpt-4o",
            temperature=0.8,
            max_tokens=1500,
            timeout=60,
        )
        return _parse_response(resp.choices[0].message.content or "null")
    except Exception as e:
        log.error("Promote API error: %s", e)
        return None
