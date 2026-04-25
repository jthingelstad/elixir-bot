# ── Tool definitions for Anthropic Claude function calling ─────────────────
#
# Consolidated domain-aligned tools:
#   Member domain:  resolve_member, get_member, get_member_war_detail
#   River Race:     get_river_race, get_war_season, get_war_member_standings
#   Clan domain:    get_clan_roster, get_clan_health, get_clan_trends
#   Cards:          lookup_cards (catalog), get_member_card_profile (digest),
#                   lookup_member_cards (filtered slice)
#   Utility:        update_member, save_clan_memory

TOOLS = [
    # ── MEMBER DOMAIN ──────────────────────────────────────────────────────

    {
        "name": "resolve_member",
        "description": (
            "Resolve a clan member from a player name, alias, Discord handle, or player tag "
            "and return the best matching candidates. Matching is case-insensitive and "
            "diacritic-folded, so 'jose' matches 'José' and 'pokemon' matches 'Pokémon' — "
            "pass the user's literal query rather than trying to normalize it yourself. "
            "If multiple candidates come back with similar scores, ask the user which "
            "one they meant instead of guessing."
        ),
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
            "- form: recent form aggregates (wins/losses, streak, hot/mixed/slumping) — NOT individual battles\n"
            "- battles: chronological list of this member's recent individual battles — outcome, crowns, "
            "trophy change, opponent name/tag/clan, slim own/opponent deck, and battle_time per row. "
            "Uses local DB, goes deeper than the ~25-battle CR API battlelog. Control with "
            "battles_limit (default 10, max 100) and battles_scope "
            "(overall_10 / competitive_10 / ladder_ranked_10 / war_10). Use this for 'tell me about my last N battles' "
            "or 'what happened in my recent matches'. "
            "When the user asks about a relative window ('tonight', 'today', 'this morning'), do NOT assume a timezone — "
            "members are international. Instead, infer the session the user means by looking for a natural gap in battle_time "
            "(e.g. several hours between clusters = a break between sessions) and only discuss the most recent cluster. "
            "If the cluster boundary is ambiguous, ask the user to narrow the window rather than guessing.\n"
            "- war: current-day war deck status + season participation summary\n"
            "- trend: trophy/activity trend with window comparison\n"
            "- deck: current deck + signature cards (most-used from battle logs)\n"
            "- losses: top opponent cards seen in recent losses + crown deficit + loss-streak context (uses scope param to pick mode: war_10/ladder_ranked_10/competitive_10/overall_10)\n"
            "- history: trophy and donation history from snapshots\n"
            "- memories: stored memories/observations about this member\n"
            "- chests: upcoming chest cycle (live API)\n"
            "- awards: the member's trophy case — every season-wide award they've earned "
            "(War Champ, Iron King, Donation Champ, Rookie MVP, War Participant), with rank, "
            "season, and metric. The awards table is the authoritative record of clan achievements.\n\n"
            "For 'tell me about X', use default includes. "
            "For 'tell me about my last N battles' / 'what happened in my recent matches', include=['battles']. "
            "For 'what deck does X use', include=['deck']. "
            "For deck-review work, include=['deck','losses'] — for card collection data, use get_member_card_profile or lookup_member_cards. "
            "For leadership evaluation, include=['profile', 'war', 'history', 'memories']. "
            "For 'has X won anything' / 'what has X earned', include=['profile', 'awards']. "
            "For card-collection questions ('what should I upgrade', 'review my cards', 'do I have X'), do NOT use this tool — use get_member_card_profile or lookup_member_cards instead."
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
                    "items": {
                        "type": "string",
                        "enum": [
                            "profile", "form", "battles", "war", "trend",
                            "deck", "losses", "history", "memories",
                            "chests", "awards",
                        ],
                    },
                    "description": (
                        "Which aspects to include. Default: ['profile', 'form']. "
                        "For card-collection data, use get_member_card_profile or lookup_member_cards."
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
                "losses_limit": {
                    "type": "integer",
                    "description": "How many recent battles to scan for the 'losses' include. Default 30.",
                    "default": 30,
                },
                "battles_limit": {
                    "type": "integer",
                    "description": (
                        "How many recent battles to return for the 'battles' include. "
                        "Default 10, max 100. If the user asks for more (e.g. 'last 200 "
                        "battles'), pass the actual number — the call will return up to "
                        "the cap and surface a `capped_at` field so you can tell the user."
                    ),
                    "default": 10,
                },
                "battles_scope": {
                    "type": "string",
                    "description": "Battle scope filter for the 'battles' include: overall_10 (all modes), competitive_10, ladder_ranked_10, war_10. Default: overall_10.",
                    "default": "overall_10",
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
            "- vs_clan_avg: compare this member's war contribution to the clan average\n"
            "- war_decks: reconstruct the four river-race war decks from recent battle history. "
            "Returns status (insufficient_data/partial/reconstructed), confidence (high/medium/low), "
            "the four decks, and gaps. Use this for any war-deck review or war-deck swap question. "
            "The CR API does NOT directly expose the four war decks — this aspect infers them."
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
                    "enum": ["summary", "attendance", "battles", "missed_days", "vs_clan_avg", "war_decks"],
                },
            },
            "required": ["member_tag"],
        },
    },

    # ── RIVER RACE DOMAIN ──────────────────────────────────────────────────

    {
        "name": "get_river_race",
        "description": (
            "Get current River Race data. Use 'aspect' to select the view.\n\n"
            "Aspects:\n"
            "- standings: competing clan rankings with fame, names, and our position "
            "(default — use for 'who are we racing', 'how do we compare', rival clans)\n"
            "- engagement: live war-day member participation — deck usage, top fame earners, "
            "who hasn't battled yet (use for 'who still needs to battle', 'how are we doing today')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "description": "Which view to retrieve. Default: standings.",
                    "default": "standings",
                    "enum": ["standings", "engagement"],
                },
            },
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
            "- promotion_candidates: members eligible for Elder promotion (tenure >= 21d, meaningful donations, active in last 7d, war participation), plus current elders who have dropped below the donation threshold for two consecutive weeks (`demotion_candidates`). Composition includes the 3-per-10 elder cap and `elder_cap_reached` so the agent does not nudge promotions past the cap."
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
                    "description": (
                        "Floor (in days) for the inactivity flag (at_risk). Per-member threshold is "
                        "max(this floor, trophies/1000 * 1.4) — a 5k-trophy player keeps the floor, a "
                        "10k-trophy player gets 14d, a 12.5k-trophy player gets 17.5d. Default 7."
                    ),
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

    {
        "name": "get_member_card_profile",
        "description": (
            "Get a compact card-collection digest for a clan member. Always small "
            "(~3KB) — use this as the FIRST call for any broad card question: "
            "'how am I doing on cards', 'what should I upgrade', 'review my collection', "
            "'do I have legendaries'. Returns:\n"
            "- king_tower_level: capped at king_tower_max (currently 16) — use THIS, not "
            "experience_level, when comparing card levels to the player's tower.\n"
            "- experience_level: the player's account-wide CR experience level (CR API "
            "`expLevel`). Often runs 30-75+ for active players; do NOT use this when "
            "comparing card levels — it will overstate gaps.\n"
            "- totals: owned, max_level, level_13_plus, level_14_plus\n"
            "- by_rarity: per-rarity counts of owned/ready/maxed\n"
            "- modes: evo_unlocked, hero_unlocked, supports_evo, supports_hero counts\n"
            "- ready_to_upgrade_top: top 5 cards the player can upgrade RIGHT NOW (has enough copies)\n"
            "- closest_to_max_top: top 5 cards closest to maxLevel\n"
            "- biggest_king_tower_gaps_top: top 5 cards furthest below the player's King "
            "Tower level. Each entry's `king_tower_gap` is computed against king_tower_level "
            "(capped), not experience_level.\n\n"
            "After reading this digest, only call lookup_member_cards if the user "
            "wants specific cards or a specific slice the digest doesn't cover."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "member_tag": {
                    "type": "string",
                    "description": "Player tag, in-game name, alias, or Discord handle.",
                },
            },
            "required": ["member_tag"],
        },
    },
    {
        "name": "lookup_member_cards",
        "description": (
            "Targeted lookup over a member's card collection. Returns a small focused "
            "list (≤20 by default) matching the filter, with each card carrying count, "
            "cards_required_for_next_level, ready_to_upgrade, and king_tower_gap.\n\n"
            "FILTER IS REQUIRED. If the user's question is ambiguous about which "
            "scope they mean (e.g. 'my cards' could be current deck, war decks, full "
            "collection, by rarity), ask one clarifying question before calling this — "
            "do not guess.\n\n"
            "Filter options (combine freely):\n"
            "- deck=true — current Trophy Road deck (8 cards)\n"
            "- mode=war — inferred war decks (CAVEAT: not authoritative; CR API does not expose them)\n"
            "- rarity=common|rare|epic|legendary|champion — by rarity\n"
            "- name=<str> — substring match on card name (e.g. 'fireball')\n"
            "- ready_to_upgrade=true — has enough copies to level up RIGHT NOW\n"
            "- near_ready=true — at least halfway to a level-up but not yet ready\n"
            "- near_max=true — 1-2 levels from max\n"
            "- maxed=true — at max level\n"
            "- evo_unlocked=true | hero_unlocked=true | has_special_mode=true"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "member_tag": {
                    "type": "string",
                    "description": "Player tag, in-game name, alias, or Discord handle.",
                },
                "filter": {
                    "type": "object",
                    "description": "Filter map. Required — see tool description for options.",
                    "properties": {
                        "deck": {"type": "boolean"},
                        "mode": {"type": "string", "enum": ["war"]},
                        "rarity": {"type": "string"},
                        "name": {"type": "string"},
                        "ready_to_upgrade": {"type": "boolean"},
                        "near_ready": {"type": "boolean"},
                        "near_max": {"type": "boolean"},
                        "maxed": {"type": "boolean"},
                        "evo_unlocked": {"type": "boolean"},
                        "hero_unlocked": {"type": "boolean"},
                        "has_special_mode": {"type": "boolean"},
                    },
                },
                "limit": {
                    "type": "integer",
                    "description": "Max cards to return. Default 20, max 50.",
                    "default": 20,
                },
            },
            "required": ["member_tag", "filter"],
        },
    },

    # ── UTILITY ────────────────────────────────────────────────────────────

    {
        "name": "get_clan_intel_report",
        "description": (
            "Build a scouting/threat analysis for a competing clan in OUR current river race. "
            "Returns roster metrics (trophies, activity, role breakdown), war engagement (fame, "
            "deck usage, engagement %), and a 1-5 threat rating. Use this for the scheduled "
            "Clan Wars Intel Report and for scouting questions like 'how dangerous is clan #X' "
            "when #X is racing us.\n\n"
            "Requires that clan_tag be one of our 4 opponents in the current river race. "
            "For arbitrary external clans not in our current race, use cr_api(aspect='clan') instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "clan_tag": {
                    "type": "string",
                    "description": "CR clan tag (#-prefixed) of a competitor in our current river race.",
                },
            },
            "required": ["clan_tag"],
        },
    },
    {
        "name": "cr_api",
        "description": (
            "Bridge to the live Clash Royale public API. Use when the user asks about ANY "
            "player, clan, or tournament by CR tag — e.g. 'tell me about player #ABC', "
            "'how is clan #XYZ', 'scout the clan I just lost to'.\n\n"
            "For OUR clan and OUR members, prefer local tools (get_member, get_clan_roster, "
            "get_clan_health, get_river_race) — local data is deeper and covers longer history. "
            "For CARD data, use lookup_cards, NOT this tool.\n\n"
            "Aspects:\n"
            "- player: profile, trophies, clan, current deck, favourite card\n"
            "- player_battles: recent battle log with opponent tags preserved (chain into "
            "aspect='player' or 'clan' to scout opponents). Optional mode filter: "
            "ladder / war / tournament / challenge / path_of_legends. "
            "For OUR clan members, prefer get_member include=['battles'] — deeper history, "
            "no live API call, and opponent tags cross-reference our roster.\n"
            "- player_chests: upcoming chest cycle\n"
            "- clan: profile + member summary (counts, averages). Rejects OUR clan.\n"
            "- clan_members: top-N members with tags, roles, trophies, donations\n"
            "- clan_war: current river race for ANY clan (standings, top participants)\n"
            "- clan_war_log: historical river race results\n"
            "- tournament: profile + top members by score\n\n"
            "If the user asks about something the CR API does not expose — battle IDs, match IDs, "
            "historical clan rosters, deck tags — say so plainly. Do not improvise a workaround."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "enum": [
                        "player", "player_battles", "player_chests",
                        "clan", "clan_members", "clan_war", "clan_war_log",
                        "tournament",
                    ],
                    "description": "Which CR API slice to fetch.",
                },
                "tag": {
                    "type": "string",
                    "description": "CR tag (#-prefixed, e.g. '#J2RGCRVG'). Required for every aspect.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Number of items to return for list-shaped aspects. "
                        "player_battles: default 15, max 25. "
                        "clan_members / tournament: default 15, max 30."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["ladder", "war", "tournament", "challenge", "path_of_legends"],
                    "description": "Optional client-side filter for aspect='player_battles'.",
                },
            },
            "required": ["aspect", "tag"],
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
    {
        "name": "flag_member_watch",
        "description": (
            "Flag a member for leadership attention with a short reason. Use when you notice a "
            "pattern the next tick or a human leader should look at — extended silence, activity "
            "drop-off, rank slide, war no-show. Persists as a leadership-scoped memory tagged "
            "'watch-list'. Prefer this over save_clan_memory when the point is 'keep an eye on "
            "this member'; use save_clan_memory when the point is durable knowledge."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "member_tag": {
                    "type": "string",
                    "description": "Player tag, in-game name, or Discord handle (e.g. '#ABC123', 'Vijay', '@jamie').",
                },
                "reason": {
                    "type": "string",
                    "description": "One-sentence reason for the flag. Factual and specific — cite what changed.",
                },
                "expires_at": {
                    "type": "string",
                    "description": "Optional ISO date or ISO datetime after which the flag should be ignored (e.g. '2026-04-30'). Omit for an open-ended flag.",
                },
            },
            "required": ["member_tag", "reason"],
        },
    },
    {
        "name": "record_leadership_followup",
        "description": (
            "Queue an operational suggestion for the leadership channel. Use when you detect a "
            "pattern that calls for a leader action — a rank swing, a recurring no-show, a "
            "compliance gap. Persists as a leadership-scoped memory tagged 'followup'. Keep the "
            "recommendation concrete (who, what, when) so a human can act on it without re-doing "
            "the analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Short label for the followup (e.g. 'Week 3 no-shows', 'promotion review for Gareth').",
                },
                "recommendation": {
                    "type": "string",
                    "description": "Concrete leadership action to consider, with enough context to act on it.",
                },
                "member_tag": {
                    "type": "string",
                    "description": "Player tag, name, or Discord handle if the followup is scoped to a specific member. Optional.",
                },
            },
            "required": ["topic", "recommendation"],
        },
    },
    {
        "name": "schedule_revisit",
        "description": (
            "Tell your future self to look at this signal again at time `at`. Use when a "
            "situation is mid-arc and a later tick should reconsider — watch a win streak "
            "through battle day, check on a silent member by Friday, recheck race pace 6 hours "
            "before reset. At the due time the revisit appears in a later Situation under "
            "`due_revisits`; you decide then whether to post, flag, or let it expire. Not a "
            "guaranteed post — just a reminder. Counts against the per-tick write budget."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "signal_key": {
                    "type": "string",
                    "description": "The `signal_key` of the signal this revisit tracks. Copy it verbatim from `signals_by_lane` or `hard_post_signals`.",
                },
                "at": {
                    "type": "string",
                    "description": "When to surface this revisit, as an ISO-8601 timestamp (e.g. '2026-04-18T18:00:00Z' or '2026-04-18T13:00:00-05:00').",
                },
                "rationale": {
                    "type": "string",
                    "description": "One-sentence reason for the revisit so future-you knows why it was scheduled.",
                },
            },
            "required": ["signal_key", "at", "rationale"],
        },
    },
    {
        "name": "get_season_awards",
        "description": (
            "Get the current standings for the four season-end awards in one "
            "call: War Champ (top fame), Iron King (perfect war attendance — "
            "4/4 decks every required battle day, post-victory days excluded), "
            "Donation Champ (top season donations), Rookie MVP (top fame among "
            "members who joined this season).\n\n"
            "Mid-season this is who would win if the season ended now; after "
            "season-close it's the final podium. Use for 'who's leading war "
            "this season?', 'is anyone on track for Iron King?', 'who's the "
            "rookie to watch?', and similar questions — read the standings "
            "rather than re-deriving from raw fame or donation rows.\n\n"
            "Returns: {season_id, war_champ, iron_kings, donation_champs, "
            "rookie_mvps}. Each entry has rank, tag, name, metric_value, "
            "metric_unit, metadata. For historical award grants (past seasons, "
            "leaderboards, single-player trophy cases) use get_awards instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "season_id": {
                    "type": "integer",
                    "description": "Optional season ID. If omitted, uses the current season.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_awards",
        "description": (
            "Query the clan awards record — the authoritative history of every "
            "season-wide clan accomplishment. Award types: war_champ, iron_king, "
            "donation_champ, rookie_mvp, war_participant.\n\n"
            "Modes:\n"
            "- list (default): filtered list of matching award grants. Combine any "
            "of member_tag, award_type, season_id, rank. Use for 'who won S130 War "
            "Champ?', 'list all iron kings this year', 'show S131 awards'.\n"
            "- leaderboard: aggregate count per member for a given award_type + "
            "rank. Use for 'who has won X the most' questions. Requires award_type.\n\n"
            "For a single player's full trophy case prefer get_member with "
            "include=['awards']."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["list", "leaderboard"],
                    "default": "list",
                    "description": "Query mode. Default: list.",
                },
                "member_tag": {
                    "type": "string",
                    "description": "Optional player tag / name / alias / Discord handle filter (list mode).",
                },
                "award_type": {
                    "type": "string",
                    "description": "Optional award type filter. Required for leaderboard mode. One of: war_champ, iron_king, donation_champ, rookie_mvp, war_participant.",
                },
                "season_id": {
                    "type": "integer",
                    "description": "Optional season filter (list mode).",
                },
                "rank": {
                    "type": "integer",
                    "description": "Optional rank filter (1/2/3). Default for leaderboard mode is 1.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return. Default 100 for list, 20 for leaderboard.",
                    "default": 100,
                },
            },
        },
    },
]
