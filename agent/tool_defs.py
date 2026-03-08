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
            "description": "Get the current clan war state, season/week, live race rank, and user-facing phase labels like phase_display and battle_day_number. Prefer phase_display or battle_day_number over raw period_index when describing battle days.",
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
