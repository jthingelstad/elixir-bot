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


