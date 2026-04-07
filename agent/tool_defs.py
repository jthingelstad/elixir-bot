# ── Tool definitions for Anthropic Claude function calling ─────────────────

TOOLS = [
    {
        "name": "resolve_member",
        "description": "Resolve a clan member from a player name, alias, Discord handle, or player tag and return the best matching candidates.",
        "input_schema": {
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
    {
        "name": "get_clan_roster_summary",
        "description": "Get a high-level roster summary including member count, open slots, average level, and average trophies.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_clan_members",
        "description": "List the current clan members with their role, level, trophies, rank, join date, and Discord linkage when available.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_longest_tenure_members",
        "description": "List the longest-tenured active clan members using the tracked join dates.",
        "input_schema": {
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
    {
        "name": "list_recent_joins",
        "description": "List members who joined recently along with their recent form and current-season war contribution.",
        "input_schema": {
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
    {
        "name": "get_member_profile",
        "description": "Get a normalized current profile for a clan member including join date, role, level, trophies, generated member bio/highlight, recent form, longer-term player stats, signature cards, and Discord linkage.",
        "input_schema": {
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
    {
        "name": "get_member_overview",
        "description": "Get a combined member overview with current profile, generated member bio, recent form, deck info, signature cards, and current war status in one response.",
        "input_schema": {
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
    {
        "name": "get_member_recent_form",
        "description": "Get a member's recent form summary such as wins/losses, streak, and whether they are hot, mixed, or slumping.",
        "input_schema": {
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
    {
        "name": "get_member_current_deck",
        "description": "Get the member's latest known current deck from stored player profile data.",
        "input_schema": {
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
    {
        "name": "get_member_card_collection",
        "description": "Get a member's tracked Clash Royale card collection with card levels, rarity summaries, and strongest cards. Use this for collection-wide questions such as unlocked legendary or champion cards, not just the current deck.",
        "input_schema": {
            "type": "object",
            "properties": {
                "member_tag": {
                    "type": "string",
                    "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of main collection cards to return. Default 60.",
                    "default": 60,
                },
                "min_level": {
                    "type": "integer",
                    "description": "Optional minimum displayed card level filter.",
                },
                "rarity": {
                    "type": "string",
                    "description": "Optional rarity filter such as common, rare, epic, legendary, or champion.",
                },
                "include_support": {
                    "type": "boolean",
                    "description": "Whether to include support cards such as tower troops. Default true.",
                    "default": True,
                },
            },
            "required": ["member_tag"],
        },
    },
    {
        "name": "get_member_next_chests",
        "description": "Fetch a member's upcoming chest cycle directly from the Clash Royale API.",
        "input_schema": {
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
    {
        "name": "get_member_signature_cards",
        "description": "Get the member's most-used cards from recent battle logs.",
        "input_schema": {
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
    {
        "name": "get_members_with_most_level_16_cards",
        "description": "Rank active members by how many level 16 cards they currently have in their tracked card collection.",
        "input_schema": {
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
    {
        "name": "get_member_history",
        "description": "Get a clan member's trophy and donation history over time from the stored state snapshots.",
        "input_schema": {
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
    {
        "name": "compare_member_trend_windows",
        "description": "Compare a member's recent trophy movement and battle activity to the previous same-length window using the long-term trend tables.",
        "input_schema": {
            "type": "object",
            "properties": {
                "member_tag": {
                    "type": "string",
                    "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                },
                "window_days": {
                    "type": "integer",
                    "description": "How many recent days to compare against the prior same-length window. Default 7.",
                    "default": 7,
                },
            },
            "required": ["member_tag"],
        },
    },
    {
        "name": "get_member_trend_summary",
        "description": "Get a compact prompt-ready member trend summary covering recent trophies, battle activity, and current-vs-previous window movement.",
        "input_schema": {
            "type": "object",
            "properties": {
                "member_tag": {
                    "type": "string",
                    "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                },
                "days": {
                    "type": "integer",
                    "description": "How many total days of trend context to summarize. Default 30.",
                    "default": 30,
                },
                "window_days": {
                    "type": "integer",
                    "description": "How many recent days to compare against the prior same-length window. Default 7.",
                    "default": 7,
                },
            },
            "required": ["member_tag"],
        },
    },
    {
        "name": "compare_clan_trend_windows",
        "description": "Compare clan member count, clan score, total member trophies, and battle activity across the recent window versus the previous same-length window.",
        "input_schema": {
            "type": "object",
            "properties": {
                "window_days": {
                    "type": "integer",
                    "description": "How many recent days to compare against the prior same-length window. Default 7.",
                    "default": 7,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_clan_trend_summary",
        "description": "Get a compact prompt-ready clan trend summary covering member count, clan score, total member trophies, and recent battle activity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many total days of trend context to summarize. Default 30.",
                    "default": 30,
                },
                "window_days": {
                    "type": "integer",
                    "description": "How many recent days to compare against the prior same-length window. Default 7.",
                    "default": 7,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_member_war_stats",
        "description": "Get a specific member's war participation history \u2014 fame earned, decks used, and race context.",
        "input_schema": {
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
    {
        "name": "get_member_war_status",
        "description": "Get a member's current-day war deck status and current-season participation summary.",
        "input_schema": {
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
    {
        "name": "get_member_war_attendance",
        "description": "Get a member's war attendance summary for the current season and the last 4 weeks, including participation rate and races missed.",
        "input_schema": {
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
    {
        "name": "get_member_war_battle_record",
        "description": "Get a member's war-battle win/loss/draw record for the selected season using stored battle-log facts.",
        "input_schema": {
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
    {
        "name": "get_current_war_status",
        "description": "Get the current clan war state, season/week, live race rank, user-facing phase labels like phase_display and battle_day_number, and final-day flags like final_practice_day_active and final_battle_day_active. Prefer those labels and flags over raw period_index when describing war progress.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_current_war_day_state",
        "description": "Get the live current war-day state for the active practice or battle day, including phase/day labels, week, time left in the current war day, engagement counts, who used all/some/none of their decks, the top fame earners today, and the full tracked participant list.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_war_season_summary",
        "description": "Get a season-level war summary including races, fame-per-member, top contributors, and members with no war participation.",
        "input_schema": {
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
    {
        "name": "get_war_deck_status_today",
        "description": "Get the current war-day deck and engagement snapshot: who used all, some, or none of their decks, how many members are engaged or finished, the live phase/day label, week, time left, current rank/score context, and the top fame earners today.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_members_without_war_participation",
        "description": "List active members who have not used any war decks in the selected season.",
        "input_schema": {
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
    {
        "name": "get_war_battle_win_rates",
        "description": "List the active members with the highest war-battle win rates this season.",
        "input_schema": {
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
    {
        "name": "get_clan_boat_battle_record",
        "description": "Get the clan's aggregate boat-battle win/loss/draw record over the most recent N war races.",
        "input_schema": {
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
    {
        "name": "get_war_score_trend",
        "description": "Summarize whether the clan's war score/rating trend has moved up or down over the selected recent window.",
        "input_schema": {
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
    {
        "name": "compare_fame_per_member_to_previous_season",
        "description": "Compare this season's fame-per-member to the previous season.",
        "input_schema": {
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
    {
        "name": "get_war_champ_standings",
        "description": "Get the current War Champ standings for this season \u2014 total fame per member across all war races.",
        "input_schema": {
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
    {
        "name": "get_recent_role_changes",
        "description": "List recent member promotions or demotions by comparing tracked role snapshots.",
        "input_schema": {
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
    {
        "name": "get_member_missed_war_days",
        "description": "List the war days a member missed in the selected season based on tracked daily war status.",
        "input_schema": {
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
    {
        "name": "compare_member_war_to_clan_average",
        "description": "Compare one member's war contribution to the clan average for the selected season.",
        "input_schema": {
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
    {
        "name": "get_trending_war_contributors",
        "description": "List the members whose recent war contribution is trending upward relative to their earlier season performance.",
        "input_schema": {
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
    {
        "name": "get_promotion_candidates",
        "description": "Evaluate which members with 'member' role meet the criteria for Elder promotion based on donations, activity, and war participation.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_members_at_risk",
        "description": "List members currently flagged by configurable participation/activity thresholds, including the reasons they were flagged.",
        "input_schema": {
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
    {
        "name": "get_members_on_hot_streak",
        "description": "List active members on a current winning streak so the clan can see who is cooking right now.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_streak": {
                    "type": "integer",
                    "description": "Minimum current winning streak to include. Default 4.",
                    "default": 4,
                },
                "scope": {
                    "type": "string",
                    "description": "Recent form scope. Default ladder_ranked_10.",
                    "default": "ladder_ranked_10",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_members_on_losing_streak",
        "description": "List active members on a current losing streak so leaders can spot who may need support.",
        "input_schema": {
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
    {
        "name": "get_trophy_drops",
        "description": "Get members with notable trophy drops over the last N days.",
        "input_schema": {
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
    {
        "name": "get_player_details",
        "description": "Fetch fresh player stats directly from the Clash Royale API when raw live details are needed.",
        "input_schema": {
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
    {
        "name": "set_member_birthday",
        "description": "Set a clan member's birthday (month and day).",
        "input_schema": {
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
    {
        "name": "set_member_join_date",
        "description": "Set or override a clan member's join date. Use when a leader provides or corrects a member's join date.",
        "input_schema": {
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
    {
        "name": "get_perfect_war_participants",
        "description": "Find members who participated in every single war race of a season \u2014 perfect attendance. These players earn a free Pass Royale for their dedication.",
        "input_schema": {
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
    {
        "name": "set_member_profile_url",
        "description": "Set a clan member's profile URL (personal website, social media, etc.).",
        "input_schema": {
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
    {
        "name": "set_member_note",
        "description": "Set a clan member's note (e.g. 'Founder', 'War Machine'). Shows on the roster page.",
        "input_schema": {
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
]
