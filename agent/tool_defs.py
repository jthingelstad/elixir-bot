# ── Tool definitions for Anthropic Claude function calling ─────────────────
#
# Consolidated domain-aligned tools (15 total):
#   Member domain:  resolve_member, get_member, get_member_war_detail
#   River Race:     get_river_race, get_war_season, get_war_member_standings
#   Clan domain:    get_clan_roster, get_clan_health, get_clan_trends
#   Cards:          lookup_cards
#   Utility:        get_player_details, update_member, save_clan_memory

TOOLS = [
    # ── MEMBER DOMAIN ──────────────────────────────────────────────────────

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
        "name": "get_member",
        "description": (
            "Get comprehensive information about a clan member. Use 'include' to select "
            "which aspects to return. Defaults to profile + form.\n\n"
            "Include options:\n"
            "- profile: join date, role, level, trophies, bio, Discord link, CR account age, activity rate\n"
            "- form: recent form (wins/losses, streak, hot/mixed/slumping)\n"
            "- war: current-day war deck status + season participation summary\n"
            "- trend: trophy/activity trend with window comparison\n"
            "- deck: current deck + signature cards (most-used from battle logs)\n"
            "- cards: full card collection with levels, rarity summaries, strongest cards\n"
            "- history: trophy and donation history from snapshots\n"
            "- memories: stored memories/observations about this member\n"
            "- chests: upcoming chest cycle (live API)\n\n"
            "For 'tell me about X', use default includes. "
            "For 'what deck does X use', include=['deck']. "
            "For leadership evaluation, include=['profile', 'war', 'history', 'memories']."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "member_tag": {
                    "type": "string",
                    "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                },
                "include": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Which aspects to include. Options: profile, form, war, trend, deck, "
                        "cards, history, memories, chests. Default: ['profile', 'form']."
                    ),
                    "default": ["profile", "form"],
                },
                "scope": {
                    "type": "string",
                    "description": "Recent form scope (for 'form' include). Default: competitive_10.",
                    "default": "competitive_10",
                },
                "days": {
                    "type": "integer",
                    "description": "History/trend window in days. Default 30.",
                    "default": 30,
                },
                "rarity": {
                    "type": "string",
                    "description": "Card rarity filter (for 'cards' include): common, rare, epic, legendary, champion.",
                },
                "min_level": {
                    "type": "integer",
                    "description": "Minimum displayed card level filter (for 'cards' include).",
                },
            },
            "required": ["member_tag"],
        },
    },
    {
        "name": "get_member_war_detail",
        "description": (
            "Get detailed River Race / war performance data for a specific member. "
            "Every response includes the member's war_player_type (regular/occasional/rare/never) "
            "based on historical participation.\n\n"
            "Aspects:\n"
            "- summary: fame earned, decks used, race context for current season\n"
            "- attendance: participation rate, races played/missed, last 4 weeks\n"
            "- battles: war-battle win/loss/draw record for the season\n"
            "- missed_days: which specific war days were missed\n"
            "- vs_clan_avg: compare this member's war contribution to the clan average"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "member_tag": {
                    "type": "string",
                    "description": "The player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie').",
                },
                "aspect": {
                    "type": "string",
                    "description": "Which war detail to retrieve. Default: summary.",
                    "default": "summary",
                    "enum": ["summary", "attendance", "battles", "missed_days", "vs_clan_avg"],
                },
            },
            "required": ["member_tag"],
        },
    },

    # ── RIVER RACE DOMAIN ──────────────────────────────────────────────────

    {
        "name": "get_river_race",
        "description": (
            "Get the current River Race state including live war-day engagement, deck usage, "
            "top fame earners, and competing clan standings (names, fame, ranks). "
            "Basic war phase/day/rank may already be in your context — use this for detailed "
            "live data including who has battled and who the competing clans are."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_war_season",
        "description": (
            "Get season-level River Race analytics. Use 'aspect' to select the view.\n\n"
            "Aspects:\n"
            "- summary: season overview with races, fame/member, top contributors, non-participants\n"
            "- standings: War Champ leaderboard (total fame per member across all races)\n"
            "- win_rates: members with highest war-battle win rates\n"
            "- boat_battles: aggregate boat-battle win/loss/draw record\n"
            "- score_trend: war score/rating direction over time\n"
            "- season_comparison: fame-per-member vs previous season\n"
            "- trending: members whose war contribution is trending up\n"
            "- perfect_attendance: members with perfect race attendance\n"
            "- no_participation: active members with zero war participation"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "description": "Which season analytic to retrieve. Default: summary.",
                    "default": "summary",
                    "enum": [
                        "summary", "standings", "win_rates", "boat_battles",
                        "score_trend", "season_comparison", "trending",
                        "perfect_attendance", "no_participation",
                    ],
                },
                "season_id": {
                    "type": "integer",
                    "description": "Optional season ID. If omitted, uses the current/most recent season.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of members to return (for rankings). Default 10.",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_war_member_standings",
        "description": (
            "Get war performance for all active members ranked by a metric. "
            "Shows each member's war_player_type (regular/occasional/rare/never). "
            "Use for end-of-week race recaps, 'who is contributing most/least', and "
            "distinguishing active war participants from those who never play."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": "Ranking metric. Default: fame.",
                    "default": "fame",
                    "enum": ["fame", "win_rate", "attendance"],
                },
                "season_id": {
                    "type": "integer",
                    "description": "Optional season ID. If omitted, uses the current/most recent season.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of members to return. Default 30.",
                    "default": 30,
                },
            },
            "required": [],
        },
    },

    # ── CLAN DOMAIN ────────────────────────────────────────────────────────

    {
        "name": "get_clan_roster",
        "description": (
            "Get information about the clan roster. Use 'aspect' to select the view.\n\n"
            "Aspects:\n"
            "- list: full roster with roles, levels, trophies, ranks, join dates, Discord linkage\n"
            "- summary: member count, open slots, average level, average trophies\n"
            "- recent_joins: members who joined recently with form and war contribution\n"
            "- longest_tenure: longest-tenured active members\n"
            "- role_changes: recent promotions or demotions\n"
            "- max_cards: members ranked by level 16 card count"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "description": "Which roster view to retrieve. Default: list.",
                    "default": "list",
                    "enum": ["list", "summary", "recent_joins", "longest_tenure", "role_changes", "max_cards"],
                },
                "days": {
                    "type": "integer",
                    "description": "How many days back for recent_joins or role_changes. Default 30.",
                    "default": 30,
                },
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
        "name": "get_clan_health",
        "description": (
            "Assess clan health and member status. Results include CR account age and "
            "war_player_type for context.\n\n"
            "Aspects:\n"
            "- at_risk: members flagged by inactivity, low donations, or low war participation\n"
            "- hot_streaks: members on a current winning streak\n"
            "- losing_streaks: members on a current losing streak\n"
            "- trophy_drops: members with notable trophy drops\n"
            "- promotion_candidates: members with 'member' role who meet Elder promotion criteria"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "description": "Which health analysis to run. Default: at_risk.",
                    "default": "at_risk",
                    "enum": ["at_risk", "hot_streaks", "losing_streaks", "trophy_drops", "promotion_candidates"],
                },
                "inactivity_days": {
                    "type": "integer",
                    "description": "Flag members inactive for at least this many days (at_risk). Default 7.",
                    "default": 7,
                },
                "min_donations_week": {
                    "type": "integer",
                    "description": "Flag members below this weekly donation count (at_risk). Default 20.",
                    "default": 20,
                },
                "min_streak": {
                    "type": "integer",
                    "description": "Minimum streak length to include (hot_streaks/losing_streaks). Default 3.",
                    "default": 3,
                },
                "min_drop": {
                    "type": "integer",
                    "description": "Minimum trophy drop to include (trophy_drops). Default 100.",
                    "default": 100,
                },
                "days": {
                    "type": "integer",
                    "description": "Window in days for trophy_drops. Default 7.",
                    "default": 7,
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
        "name": "get_clan_trends",
        "description": (
            "Compare clan metrics (member count, clan score, total trophies, battle activity) "
            "across recent window versus previous same-length window. "
            "A default trend summary may already be in your context — use this for custom windows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window_days": {
                    "type": "integer",
                    "description": "How many recent days to compare against the prior same-length window. Default 7.",
                    "default": 7,
                },
                "days": {
                    "type": "integer",
                    "description": "Total days of trend context. Default 30.",
                    "default": 30,
                },
            },
            "required": [],
        },
    },

    # ── CARD DOMAIN ────────────────────────────────────────────────────────

    {
        "name": "lookup_cards",
        "description": "Look up Clash Royale cards from the card catalog. Use this for accurate card data including elixir cost, rarity, type, and evolution/hero capability. Always prefer this over relying on memory when discussing card stats or comparisons.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Card name or partial name to search for (e.g. 'Knight', 'Valk').",
                },
                "rarity": {
                    "type": "string",
                    "description": "Filter by rarity: common, rare, epic, legendary, champion.",
                },
                "min_cost": {
                    "type": "integer",
                    "description": "Minimum elixir cost filter.",
                },
                "max_cost": {
                    "type": "integer",
                    "description": "Maximum elixir cost filter.",
                },
                "card_type": {
                    "type": "string",
                    "description": "Filter by type: troop, building, spell, tower_troop.",
                },
                "has_evolution": {
                    "type": "boolean",
                    "description": "Filter to cards with (true) or without (false) evolution capability.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of cards to return. Default 25.",
                    "default": 25,
                },
            },
            "required": [],
        },
    },

    # ── UTILITY ────────────────────────────────────────────────────────────

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
        "name": "update_member",
        "description": (
            "Set metadata for a clan member. Use 'field' to specify what to update.\n\n"
            "Fields:\n"
            "- birthday: set birth month and day (value: {\"month\": 3, \"day\": 15})\n"
            "- join_date: set or override join date (value: \"2024-01-15\")\n"
            "- profile_url: set profile URL (value: \"https://...\")\n"
            "- note: set a short note or title (value: \"War Machine\")"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "member_tag": {
                    "type": "string",
                    "description": "Player tag, in-game name, alias, or Discord handle (e.g. '#ABC123' or '@jamie')",
                },
                "field": {
                    "type": "string",
                    "description": "Which metadata field to set.",
                    "enum": ["birthday", "join_date", "profile_url", "note"],
                },
                "value": {
                    "description": "The value to set. For birthday: {\"month\": M, \"day\": D}. For join_date: \"YYYY-MM-DD\". For profile_url: \"https://...\". For note: short text.",
                },
            },
            "required": ["member_tag", "field", "value"],
        },
    },
    {
        "name": "save_clan_memory",
        "description": "Save a durable clan memory or leader note that persists across sessions. Use when leadership asks to remember, record, or note something about a member or the clan. Also use proactively when a significant decision is made during conversation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short title for the memory (e.g. 'raquaza is war leader', 'Free Pass Royale reward policy')",
                },
                "body": {
                    "type": "string",
                    "description": "Full text of what to remember",
                },
                "member_tag": {
                    "type": "string",
                    "description": "Player tag, name, or Discord handle if this memory is about a specific member. Optional.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Categorization tags (e.g. ['decision', 'war', 'member-note'])",
                },
            },
            "required": ["title", "body"],
        },
    },
]
