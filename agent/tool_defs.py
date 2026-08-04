# ── Tool definitions for Anthropic Claude function calling ─────────────────
#
# Shared, non-overlapping tool block (14 tools):
#   Member domain:  resolve_member, get_member, get_member_war_detail
#   River Race:     get_river_race
#   Clan domain:    get_clan_roster
#   Cards:          lookup_cards (catalog), get_member_cards (member collection)
#   Elixir state:   get_elixir_state
#   Utility:        cr_api, save_clan_memory, record_leadership_followup,
#                   get_awards, lookup_reference

TOOLS = [
    # ── BATTLE INTELLIGENCE (computed, no model) ───────────────────────────
    {
        "name": "get_deck_recommendations",
        "description": (
            "FORWARD-LOOKING deck recommendations, bound by what a member OWNS and can "
            "field at level. Distinct from get_deck_intelligence, which reports how their "
            "OBSERVED decks have performed — this one suggests decks they have never played.\n\n"
            "Views (all require 'member_tag'):\n"
            "- upgrades: 'what can I do to improve my deck?' — which cards to upgrade, "
            "ranked by how much they are actually played x how far from max they are. "
            "Returns all_played_cards_maxed=true when there is genuinely nothing to do; "
            "say that plainly rather than inventing an upgrade.\n"
            "- discover: 'what decks should I consider?' — for a member in a rut or one "
            "whose main deck is already maxed. Includes decks nobody in the clan plays, "
            "plus a current-meta snapshot when one has been refreshed.\n"
            "- build: EXACTLY the decks the member asked for — pass 'anchors' (one card per "
            "deck wanted) and 'count'. THE view for 'build me two decks, one around X and "
            "one around Y'. Decks may share cards, which is correct everywhere except war.\n"
            "- war_set: FOUR war decks using 32 DISTINCT cards (war forbids reusing a card "
            "across decks). ONLY for an explicit war request — 'war decks', 'river race', "
            "'my four decks'. A member who says 'a deck' or names a number other than four "
            "is NOT asking for a war set, and the no-overlap rule makes each individual "
            "deck worse, so never reach for this view to be helpful.\n"
            "- anchored: best decks built around ONE card. Requires 'card'. For several "
            "cards use 'build'.\n\n"
            "Every deck comes back with 'role_coverage' (which card fills each slot of the "
            "deck formula, air answers split into troops vs spells, and a 'gaps' list) and "
            "per-card 'roles'. EXPLAIN THE DECK FROM THESE rather than from your own "
            "knowledge of the cards — the point is that the member learns what a deck needs, "
            "not just what to copy. Read 'gaps' out honestly; an empty list means the deck is "
            "structurally sound, and role_coverage.unknown means the cards are not enriched "
            "yet, so say nothing about structure rather than guessing.\n\n"
            "'your_field' is what this member ACTUALLY runs into — archetype shares and "
            "THEIR OWN record against each. Use it to ground a suggestion in their "
            "situation ('you meet beatdown in a quarter of your games, and you are 25-44 "
            "into bait'). Do NOT turn it into a claim that one archetype beats another: "
            "adjusted for who plays what, archetype matchup is worth about 3 points "
            "against 22 for card levels, so a member's own lopsided record is about them "
            "and their deck, never proof an archetype is strong.\n\n"
            "Card levels: EVERY card maxes at 16, so a level is a single number. Say "
            '"Wall Breakers are level 15" or "one off max" or "maxed" — never '
            '"15/16", and never mention a second scale. Use levels_from_max for how '
            "far off they are.\n\n"
            "SOUND LIKE A PLAYER. Call decks by their archetype the way the game's players "
            "do — 'your Hog cycle', 'Royal Hogs bridge spam', 'LavaLoon', 'log bait', "
            "'X-Bow siege' — never by reciting eight card names, which is the clearest tell "
            "of a tool that does not play. Say what a card is FOR using its role: win "
            "condition, tank answer, air answer, splash, cycle card, small spell.\n\n"
            "IMPORTANT: this tool deliberately reports NO win rates for a deck. Clan deck "
            "win rates are skill-confounded and do not transfer between members, so ranking "
            "is level readiness and structural soundness. 'fielded_by_members' is context "
            "only — never present it as evidence a deck is good.\n\n"
            "'you_play_this' true means these are the EXACT eight cards they already run. "
            "Say so in the first line — 'this is the deck you are already on' — then either "
            "explain why it is still the best answer to what they asked, or offer something "
            "different. Softening that to 'close to your existing deck' wastes their time "
            "and reads as though you did not check. 'you_play_this_archetype' is the looser "
            "signal: same archetype, different eight cards.\n\n"
            "Each deck's 'copy_link' loads it into the game — give the raw URL. When "
            "'link_omits_forms' is non-empty, name those cards and say the link brings them "
            "in as base cards, because the share format cannot carry Evo or Hero.\n\n"
            "When 'link_slot_risk' is non-empty the paste may arrive ONE CARD SHORT. All "
            "three special slots (Evo / Hero / Wild) are already spoken for, and the game "
            "auto-equips evolutions the player owns as it reads the deck — so an "
            "evolution for one of the named cards can take the last slot and leave a gap. "
            "Tell them to check the deck after pasting and un-equip that evolution if a "
            "card is missing. This is observed behaviour, not a guess: a Champion came "
            "through as an empty slot exactly this way."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["upgrades", "discover", "build", "war_set", "anchored"],
                    "description": "Which recommendation view to return.",
                },
                "member_tag": {"type": "string", "description": "Member tag or name."},
                "card": {"type": "string", "description": "Anchor card for the 'anchored' view."},
                "anchors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "For 'build': one card per deck wanted, in the order the member "
                        "named them. Each returned deck is built around its anchor."
                    ),
                },
                "count": {
                    "type": "integer",
                    "description": (
                        "For 'build': how many decks the member asked for. Defaults to the "
                        "number of anchors. Never inflate this to four."
                    ),
                },
                "require": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "win_condition",
                            "big_spell",
                            "small_spell",
                            "spell",
                            "reset",
                            "knockback",
                            "pull",
                            "air_defender",
                            "heavy_air",
                            "tank_answer",
                            "splash",
                            "swarm",
                            "building",
                            "tank",
                            "cycle",
                        ],
                    },
                    "description": (
                        "Deck PROPERTIES the member asked for, for 'build' and "
                        "'anchored': win_condition, big_spell, small_spell, spell, "
                        "reset, knockback, pull, air_defender, heavy_air, tank_answer, splash, "
                        "swarm, building, tank, cycle. THE way to answer 'a deck with a "
                        "reset card and a big spell' — pass require=['reset','big_spell'] "
                        "and KEEP the anchor. Do not guess a specific card that has the "
                        "property and anchor on that instead: a member asking to fix the "
                        "spell gap in his Ronin deck got back a deck with no Ronin, while "
                        "88 decks he could build had both."
                    ),
                },
                "limit": {"type": "integer", "description": "Max decks to return (default 6)."},
            },
            "required": ["view", "member_tag"],
        },
    },
    {
        "name": "read_deck_link",
        "description": (
            "Read a Clash Royale deck that a member PASTED into chat. The game's "
            "'Copy Deck' button produces a link like 'https://link.clashroyale.com/en?"
            "clashroyale://copyDeck?deck=26000007;28000015;...&tt=...' — usually inside a "
            "sentence such as '<name> wants to share a Clash Royale deck: <link>'. Pass "
            "the member's whole message as 'link' and this resolves the eight cards, "
            "their elixir costs, the roles each one fills, the deck's role_coverage and "
            "gaps, and the tower troop.\n\n"
            "USE THIS whenever a message contains a deck link — never try to read the "
            "card ids yourself.\n\n"
            "CRITICAL: the share format carries BASE CARDS ONLY. It cannot express "
            "Evolution or Hero forms — a deck shared by a player running Evo Witch comes "
            "through as plain Witch. Never tell a member which of their cards are "
            "evolved based on a pasted link; ask if it matters to the advice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "link": {
                    "type": "string",
                    "description": "The member's message text containing the deck link.",
                },
                "member_tag": {
                    "type": "string",
                    "description": (
                        "Optional. Fills in this member's own card levels. A pasted deck "
                        "may be someone else's, so unowned cards are reported as unowned."
                    ),
                },
            },
            "required": ["link"],
        },
    },
    {
        "name": "get_battle_intelligence",
        "description": (
            "Computed battle intelligence from BOTH sides of Elixir's observed 1v1 "
            "battles — opponent card data IS available here (unlike get_deck_intelligence, "
            "which is own-deck only). Form-aware: 'Evo Knight' and 'Knight' are distinct.\n\n"
            "Views:\n"
            "- card: a card's win rate when a member PLAYS it and when they FACE it. "
            "Requires 'card'; optional 'member_tag' (omit for clan-wide). Use for "
            "'how do I do against X' / 'is X worth it' questions.\n"
            "- nemesis: the opponent card-forms a member (or the clan) does worst against.\n"
            "- battle: a member's recent battles with their computed read (margin, how "
            "close, elixir discipline, deck-level gap), under 'recent_battles'. "
            "Requires 'member_tag'. NOTE the key: 'battles' is a COUNT in the other "
            "views, never a list.\n"
            "- member_summary: a member's computed rollup (record, stomps/squeakers, "
            "discipline, best/worst cards). Requires 'member_tag'.\n"
            "- deck: a member's observed decks with rules archetype, avg elixir, and W/L. "
            "Requires 'member_tag'.\n"
            "- newcomer: THE view for a member_joined — who this player is on their "
            "first day, when there is no history yet: Collection Level, the deck they walked in "
            "with (named archetype), Evolutions unlocked, collection depth, peak trophies. "
            "Use it to write a welcome that shows you actually looked.\n"
            "- coaching: THE view for 'how have I been playing?' / 'what should I work on?' — "
            "an aggregated read of a member's recent battles: record, what decided their games "
            "(decisive_factors), their own record against each opposing archetype, their deck's "
            "role coverage, and the structural mechanism behind any matchup they lose. Requires "
            "'member_tag'. Ground coaching advice in THIS, not in single battles.\n"
            "'battles' is the SAMPLE size and 'battles_in_window' the real total — when "
            "sample_truncated is true, never state 'battles' as how many games they played.\n"
            "Deck structure lives in primary_deck_shape.role_coverage, which names the CARD "
            "filling each slot. There are no scalar answer-counts any more; do not ask for one.\n\n"
            "Time: pass 'days' for a window ('this week' = 7); every view reports the "
            "window it used. To answer 'am I improving?', call twice with different windows "
            "and compare. member_summary also returns clan_standing (percentile vs active "
            "members with 20+ battles) for 'am I above average?'.\n"
            "Routing: card LEVELS and upgrade questions belong to get_member_cards; war-week "
            "participation belongs to get_member_war_detail; this tool has no 2v2 data at all "
            "(1v1 only) — say so plainly rather than answering from 1v1.\n\n"
            "Statistical floor: win-rate claims need n>=30; below that the view returns "
            "insufficient_sample with the real n, never a weak number. Ranked (Path of "
            "Legends) suppresses level_gap (levels are normalized)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": [
                        "card",
                        "nemesis",
                        "battle",
                        "member_summary",
                        "deck",
                        "coaching",
                        "newcomer",
                    ],
                    "default": "battle",
                },
                "member_tag": {
                    "type": "string",
                    "description": "Player tag, name, alias, or Discord handle. Required for "
                    "battle/member_summary/deck; optional for card/nemesis (omit for clan-wide).",
                },
                "card": {
                    "type": "string",
                    "description": "Card name for the 'card' view (e.g. 'Bats', 'Royal Hogs').",
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "competitive", "war", "ranked", "ladder"],
                    "default": "all",
                    "description": "Filter by battle type. 'war' and 'ranked' isolate those "
                    "modes; 'competitive' is broad (includes ladder) so prefer the specific "
                    "value when the member names a mode.",
                },
                "days": {
                    "type": "integer",
                    "description": "Only battles from the last N days (1-365). Omit for "
                    "all-time. Use it for 'this week', 'lately', or to compare two windows "
                    "when someone asks whether they are improving.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Means different things per view. For 'battle' it is how many "
                        "battles come back. For 'coaching' it is the SAMPLE SIZE the "
                        "aggregate is computed over, so a small limit narrows the read "
                        "rather than shortening a list — leave it alone unless you mean "
                        "to. Default 20."
                    ),
                    "default": 20,
                },
            },
            "required": ["view"],
        },
    },
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
            "- profile: join date, role, level, trophies, bio, Discord link, CR account age, activity rate, badge-backed profile metrics such as Collection Level and Clan War Wins\n"
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
            "- losses: what is BEATING them — top opponent cards seen in recent losses, crown deficit, loss streak, "
            "plus `margin` (how close each loss was: one-crown games, near-misses where the opponent's last "
            "tower finished under 500 HP) and `elixir` (elixir wasted vs the opponent's in the same battle; "
            "LOWER IS BETTER). Uses scope to pick mode: war_10/ladder_ranked_10/competitive_10/overall_10\n"
            "- wins: what is WORKING — cards they BEAT, crown surplus, win streak, plus `margin` "
            "(three-crown wins, wins where no tower was lost, narrow wins that were nearly losses) "
            "and the same `elixir` comparison. Use this for praise, coaching on strengths, or "
            "'what should I keep doing' — pair with losses for a full read\n"
            "- history: trophy and donation history from snapshots\n"
            "- ranked: Ranked / Path of Legend status and recent Ranked activity, separate from Trophy Road\n"
            "- mode_activity: 7/30-day activity by mode family (Trophy Road, Ranked, Events, War, etc.)\n"
            "- memories: stored memories/observations about this member\n"
            "- chests: upcoming chest cycle (live API)\n"
            "- awards: the member's trophy case — every season-wide award they've earned "
            "(War Champ, Iron King, Donation Champ, Rookie MVP, War Participant), with rank, "
            "season, and metric. The awards table is the authoritative record of clan achievements.\n\n"
            "For 'tell me about X', use default includes. "
            "For 'tell me about my last N battles' / 'what happened in my recent matches', include=['battles']. "
            "For 'what deck does X use', include=['deck']. "
            "For deck-review work, include=['deck','losses'] — for card collection data, use get_member_cards. "
            "For leadership evaluation, include=['profile', 'war', 'history', 'memories']. "
            "For 'has X won anything' / 'what has X earned', include=['profile', 'awards']. "
            "For card-collection questions ('what should I upgrade', 'review my cards', 'do I have X'), do NOT use this tool — use get_member_cards instead."
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
                            "profile",
                            "form",
                            "battles",
                            "war",
                            "trend",
                            "deck",
                            "losses",
                            "wins",
                            "history",
                            "ranked",
                            "mode_activity",
                            "memories",
                            "chests",
                            "awards",
                        ],
                    },
                    "description": (
                        "Which aspects to include. Default: ['profile', 'form']. "
                        "For card-collection data, use get_member_cards."
                    ),
                    "default": ["profile", "form"],
                },
                "scope": {
                    "type": "string",
                    "description": "Recent form/loss scope. Options include overall_10, competitive_10, ladder_ranked_10, ladder_10, ranked_10, event_10, tournament_10, two_v_two_10, friendly_10, war_10. Default: competitive_10.",
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
                    "description": "Battle scope filter for the 'battles' include: overall_10, competitive_10, ladder_ranked_10, ladder_10, ranked_10, event_10, tournament_10, two_v_two_10, friendly_10, war_10. Default: overall_10.",
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
            "- summary: points earned, decks used, race context for current season\n"
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
                    "enum": [
                        "summary",
                        "attendance",
                        "battles",
                        "missed_days",
                        "vs_clan_avg",
                        "war_decks",
                    ],
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
            "- engagement: live war-day member participation — deck usage, top points earners, "
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
            "- standings: members ranked by a war metric. Use the `metric` param: "
            "'points' (default — War Champ leaderboard; 'fame' is accepted as a "
            "back-compat alias), 'win_rate' (highest war-battle "
            "win rates), or 'attendance' (active members with zero war participation). "
            "Each member entry is enriched with war_player_type "
            "(regular/occasional/rare/never). Use for end-of-week race recaps and "
            "'who is contributing most/least'. The points metric includes the "
            "current in-progress week's points (per-player `finalized_points` and "
            "`in_progress_points` are exposed) and bundles a top-3 `rookie_mvps` "
            "list so a single 'war champ standings' question covers the rookie "
            "race too. The response carries a `freshness` block with `as_of` "
            "and `current_week_included` — quote those when answering 'right "
            "now' questions so players see how fresh the read is.\n"
            "- win_rates: members with highest war-battle win rates (no enrichment)\n"
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
                        "summary",
                        "standings",
                        "win_rates",
                        "boat_battles",
                        "score_trend",
                        "season_comparison",
                        "trending",
                        "perfect_attendance",
                        "no_participation",
                    ],
                },
                "metric": {
                    "type": "string",
                    "description": "Ranking metric for aspect='standings'. Default: points ('fame' accepted as a back-compat alias).",
                    "default": "points",
                    "enum": ["points", "fame", "win_rate", "attendance"],
                },
                "season_id": {
                    "type": "integer",
                    "description": "Optional season ID. If omitted, uses the current/most recent season.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of members to return (for rankings). Default 10. "
                        "For full-roster standings (aspect='standings') pass a higher value, e.g. 30."
                    ),
                    "default": 10,
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
            "- max_cards: members ranked by level 16 card count\n"
            "- card_owners: clan-wide owners of ONE card (args: card_name, "
            "maxed_only default true) — use for 'who has X maxed?' instead of "
            "per-member lookups\n"
            "- donations: top donors THIS WEEK (compact; use for any weekly "
            "donation question — the full list may truncate)\n"
            "- trends: compare clan metrics (member count, clan score, total trophies, "
            "battle activity) across recent window vs prior same-length window. Uses "
            "`window_days` (default 7) for the comparison window and `days` (default 30) "
            "for the trend-summary context. A default trend summary may already be in "
            "your context — use this for custom windows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "description": "Which roster view to retrieve. Default: list.",
                    "default": "list",
                    "enum": [
                        "list",
                        "summary",
                        "recent_joins",
                        "longest_tenure",
                        "role_changes",
                        "max_cards",
                        "card_owners",
                        "donations",
                        "trends",
                    ],
                },
                "card_name": {
                    "type": "string",
                    "description": "For card_owners: the card to look up (e.g. 'Balloon').",
                },
                "maxed_only": {
                    "type": "boolean",
                    "description": "For card_owners: only owners at display level 16. Default true.",
                    "default": True,
                },
                "days": {
                    "type": "integer",
                    "description": (
                        "Window in days. recent_joins / role_changes: lookback for "
                        "the listing. trends: total days of trend-summary context. "
                        "Default 30."
                    ),
                    "default": 30,
                },
                "window_days": {
                    "type": "integer",
                    "description": "Comparison window in days for aspect='trends'. Default 7.",
                    "default": 7,
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
            "- promotion_candidates: Elder role review using a smoothed rolling donation leaderboard. Active battle play and recent war participation are required; there is no fixed donation-count floor. The Elder cap is a maximum, not a target; demotions use hysteresis to avoid role flapping. Returns promotion candidates, demotion candidates, the ranked leaderboard, and composition/cap fields."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "description": "Which health analysis to run. Default: at_risk.",
                    "default": "at_risk",
                    "enum": [
                        "at_risk",
                        "hot_streaks",
                        "losing_streaks",
                        "trophy_drops",
                        "promotion_candidates",
                    ],
                },
                # ACCEPTED AND IGNORED. The trophy-scaled formula that used to be
                # documented here — max(floor, trophies/1000 * 1.4), with worked
                # examples — was deleted in the July kick redesign; the engine now
                # uses a flat KICK_AT_RISK_DAYS and storage.war_analytics deletes
                # these arguments on arrival. Describing a retired rule to the
                # model is worse than omitting it: it would repeat the numbers to
                # members as fact. This tool is not currently advertised, which is
                # the only reason that did not happen.
                "inactivity_days": {
                    "type": "integer",
                    "description": (
                        "Deprecated and ignored — the engine owns the inactivity "
                        "threshold and it is flat, not trophy-scaled."
                    ),
                },
                "min_donations_week": {
                    "type": "integer",
                    "description": "Deprecated and ignored — the engine owns this threshold.",
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
        "name": "get_clan_game_modes",
        "description": (
            "Summarize how the clan is playing across Clash Royale game modes. Use this for "
            "questions about Ranked, Trophy Road, events, tournaments, 2v2, friendly play, "
            "side-mode progress such as Merge Tactics, or overall mode mix.\n\n"
            "Aspects:\n"
            "- summary: mode mix across the requested window\n"
            "- ranked: Ranked / Path of Legend roster activity and current profile standings\n"
            "- side_modes: Player.progress mode-season keys and top side-mode progress\n"
            "- events: event and special-mode battle activity from battle logs"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "description": "Which view to retrieve. Default: summary.",
                    "default": "summary",
                    "enum": ["summary", "ranked", "side_modes", "events", "duos"],
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days. Default 30.",
                    "default": 30,
                },
                "mode_group": {
                    "type": "string",
                    "description": "Optional mode-group filter.",
                    "enum": [
                        "ladder",
                        "ranked",
                        "war",
                        "special_event",
                        "tournament",
                        "two_v_two",
                        "friendly",
                        "side_mode",
                        "other",
                    ],
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows per ranked/top list. Default 10.",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_elixir_state",
        "description": (
            "Inspect Elixir's internal operating state: the normalized game-event stream, "
            "awareness decisions and confirmed posts, and leader-action cards. "
            "Use this when leaders ask what Elixir is monitoring, "
            "which recommendations are open, why something was posted or skipped, or what Elixir "
            "would do next. Leadership-only aspects are blocked outside leadership workflows. "
            "Public workflows can only read public event-stream views."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "description": "Which internal-state view to retrieve. Default: operational_summary.",
                    "default": "operational_summary",
                    "enum": [
                        "operational_summary",
                        "event_summary",
                        "recent_events",
                        "game_modes",
                        "season_window",
                        "war_season",
                        "leader_actions",
                        "awareness_activity",
                    ],
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Event-stream scope: public, leadership, or all. "
                        "Non-leadership workflows are forced to public."
                    ),
                    "enum": ["public", "leadership", "all"],
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days for recent_events. Default 7, max 90.",
                    "default": 7,
                },
                "windows": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Lookback windows in days for event_summary. Default [7, 28, 56, 90].",
                },
                "event_type": {
                    "type": "string",
                    "description": "Optional event type filter for recent_events.",
                },
                "subject_key": {
                    "type": "string",
                    "description": "Optional subject key filter for event views.",
                },
                "status": {
                    "type": "string",
                    "enum": ["proposed", "done", "rejected", "deferred", "all"],
                    "description": "Optional leader-action status filter; defaults to proposed.",
                },
                "war_view": {
                    "type": "string",
                    "enum": ["snapshot", "summary", "history"],
                    "default": "snapshot",
                    "description": "For aspect='war_season': current snapshot, compact summary, or recent history.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows to return. Default 25, max 100.",
                    "default": 25,
                },
            },
            "required": [],
        },
    },
    # ── CARD DOMAIN ────────────────────────────────────────────────────────
    {
        "name": "get_deck_intelligence",
        "description": (
            "Get structured deck and clan-local metagame intelligence derived from "
            "Elixir's observed battle history. This is the FIRST tool for deck strategy, "
            "deck stability, archetypes, variants, recent substitutions, performance, or "
            "balance-impact questions.\n\n"
            "Views:\n"
            "- member: requires member_tag; returns current/primary deck, archetype, win "
            "conditions, observed W/L, variants, recent card swaps, stability, and upgrade "
            "bottlenecks.\n"
            "- clan: clan-local archetype, win-condition, and primary-card spread. This is "
            "POAP KINGS usage, NOT global ladder meta.\n"
            "- card_impact (leadership only): requires changes (preferred) or cards; shows which "
            "clan members actually use those cards and their observed results. Preserve each "
            "change's direction, source URL/date, effective date, and WIP/final status. An optional "
            "member_tag narrows the impact to one member. The tool does not infer the balance "
            "change itself.\n\n"
            "Evidence boundary: Elixir stores our members' own decks but not opponent deck "
            "lists. Never turn this result into claims about specific opponent cards or global meta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["member", "clan", "card_impact"],
                    "default": "member",
                },
                "member_tag": {
                    "type": "string",
                    "description": "Player tag, name, alias, or Discord handle; required for member view and optional for a leadership card_impact query.",
                },
                "cards": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Named changed/affected cards; required for card_impact view.",
                },
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "card": {"type": "string"},
                            "direction": {
                                "type": "string",
                                "enum": ["buff", "nerf", "rework", "mixed", "unknown"],
                            },
                            "status": {
                                "type": "string",
                                "description": "For example wip, proposed, final, stale, or superseded.",
                            },
                            "source_url": {"type": "string"},
                            "published_at": {"type": "string"},
                            "effective_at": {"type": "string"},
                        },
                        "required": ["card", "direction", "source_url", "published_at"],
                    },
                    "description": "Sourced balance items for card_impact. Preferred over bare cards.",
                },
                "days": {
                    "type": "integer",
                    "description": "Observed battle window, 1-180 days. Default 30.",
                    "default": 30,
                },
                "scope": {
                    "type": "string",
                    "enum": [
                        "all",
                        "competitive",
                        "ladder_ranked",
                        "ladder",
                        "ranked",
                        "war",
                    ],
                    "description": "Battle family to analyze. Default competitive.",
                    "default": "competitive",
                },
            },
            "required": [],
        },
    },
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
                "role": {
                    "type": "string",
                    "enum": [
                        "win_condition",
                        "tank",
                        "mini_tank",
                        "support",
                        "swarm",
                        "building",
                        "spawner",
                        "spell",
                        "champion",
                    ],
                    "description": (
                        "Filter by what the card is FOR. THE answer to 'what cards are "
                        "win conditions?', 'what tanks are there?', 'which spells do I "
                        "have?' — questions that are about the role, not the name."
                    ),
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
        "name": "get_member_cards",
        "description": (
            "Get a clan member's card collection. Use view='profile' (default) for "
            "broad questions such as 'how am I doing on cards', 'what should I upgrade', "
            "or 'review my collection'. The compact profile returns:\n"
            "- king_tower_level: the player's King Tower Level (1-16, capped at "
            "king_tower_max), computed from their card collection — use THIS when "
            "comparing card levels to the player's tower.\n"
            "- totals: owned, max_level, level_13_plus, level_14_plus\n"
            "- by_rarity: per-rarity counts of owned/ready/maxed\n"
            "- modes: evo_unlocked, hero_unlocked, supports_evo, supports_hero counts\n"
            "- ready_to_upgrade_top: top 5 cards the player can upgrade RIGHT NOW (has enough copies)\n"
            "- closest_to_max_top: top 5 cards closest to maxLevel\n"
            "- biggest_king_tower_gaps_top: top 5 cards furthest below the player's King "
            "Tower level. Each entry's `king_tower_gap` is computed against king_tower_level "
            "(capped), not experience_level.\n\n"
            "Use view='lookup' with a required filter for a focused list: deck=true, "
            "mode=war, rarity, name, ready_to_upgrade, near_ready, near_max, maxed, "
            "evo_unlocked, hero_unlocked, or has_special_mode. Ask a clarifying "
            "question if the intended slice is ambiguous."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "member_tag": {
                    "type": "string",
                    "description": "Player tag, in-game name, alias, or Discord handle.",
                },
                "view": {
                    "type": "string",
                    "enum": ["profile", "lookup"],
                    "default": "profile",
                    "description": "Compact collection profile or filtered card lookup.",
                },
                "filter": {
                    "type": "object",
                    "description": "Required for view='lookup'. Combine supported filters as needed.",
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
                    "description": "Maximum cards for view='lookup'. Default 20, max 50.",
                    "default": 20,
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
            "Fetch live Clash Royale API data for an external player, clan, river race, "
            "tournament, event, or Ranked leaderboard. Prefer local tools for POAP KINGS "
            "and its members; use lookup_cards for card facts. player_battles can be "
            "filtered by mode. List views accept limit. Tagless views are events and "
            "leaderboards/rankings. If the API does not expose a requested fact, say so "
            "plainly instead of improvising."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aspect": {
                    "type": "string",
                    "enum": [
                        "player",
                        "player_battles",
                        "player_chests",
                        "clan",
                        "clan_members",
                        "clan_war",
                        "clan_war_log",
                        "tournament",
                        "events",
                        "pathoflegend_location_rankings",
                        "pathoflegend_season_rankings",
                        "leaderboards",
                        "leaderboard",
                    ],
                    "description": "Which CR API slice to fetch.",
                },
                "tag": {
                    "type": "string",
                    "description": "CR tag (#-prefixed, e.g. '#J2RGCRVG'). Required except for aspect='events'.",
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
                    "enum": [
                        "ladder",
                        "ranked",
                        "war",
                        "tournament",
                        "challenge",
                        "path_of_legends",
                        "event",
                        "two_v_two",
                        "friendly",
                    ],
                    "description": "Optional client-side filter for aspect='player_battles'.",
                },
                "location_id": {
                    "type": "string",
                    "description": "Location id for aspect='pathoflegend_location_rankings'. Default: global.",
                },
                "season_id": {
                    "type": "string",
                    "description": "Season id such as 2026-06 for aspect='pathoflegend_season_rankings'.",
                },
                "leaderboard_id": {
                    "type": "integer",
                    "description": "Integer leaderboard id for aspect='leaderboard'.",
                },
            },
            "required": ["aspect"],
        },
    },
    {
        "name": "update_member",
        "description": (
            "Set metadata for a clan member. Use 'field' to specify what to update.\n\n"
            "Fields:\n"
            '- birthday: set birth month and day (value: {"month": 3, "day": 15})\n'
            '- join_date: set or override join date (value: "2024-01-15")\n'
            '- profile_url: set profile URL (value: "https://...")\n'
            '- note: set a short note or title (value: "War Machine")\n'
            "- nickname: pin a readable name Elixir prefers over the game name "
            'everywhere (value: "Ellipsis"; empty value clears it)'
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
                    "enum": [
                        "birthday",
                        "join_date",
                        "profile_url",
                        "note",
                        "nickname",
                    ],
                },
                "value": {
                    "description": 'The value to set. For birthday: {"month": M, "day": D}. For join_date: "YYYY-MM-DD". For profile_url: "https://...". For note: short text. For nickname: a short readable name (e.g. "Ellipsis").',
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
            "Record private member state for future awareness ticks. Use for an observed pattern "
            "to watch — extended silence, activity drop-off, rank slide, war no-show — or for an "
            "explicit leave hold when a member told leaders they will be away. This tool never "
            "raises a #actions card and never asks a leader to decide anything. Use "
            "record_leadership_followup for a member-review decision; use raise_clan_chat_relay "
            "for paste-ready in-game copy."
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
                "away_until": {
                    "type": "string",
                    "description": (
                        "Optional ISO date/datetime. Set ONLY when the member has TOLD leaders they'll "
                        "be away (a leave of absence) — e.g. 'travelling, back after the 20th'. This "
                        "records a leave HOLD that grants inactivity grace until the date: the kick "
                        "clock is paused, no removal card fires. Do NOT set this for a member who is "
                        "merely inactive with no word — that is a normal 'reason' watch, and their kick "
                        "clock keeps running. A hold is an approved absence, not an observed one."
                    ),
                },
            },
            "required": ["member_tag", "reason"],
        },
    },
    {
        "name": "raise_clan_chat_relay",
        "description": (
            "Raise an in-game clan-chat relay card to #actions so a human leader can paste a "
            "short note into the game's clan chat — the ONE surface that reaches every member, "
            "not just the Discord subset. Use to acknowledge something members should see in "
            "game: most importantly, confirming a leave of absence a leader just told you about "
            "('X and Y are away for a week'). The workflow for an away/LOA notice is TWO steps — "
            "first call flag_member_watch with away_until for EACH member (records the leave "
            "hold that pauses their kick clock), THEN call this once to relay a warm "
            "acknowledgement to clan chat. This tool never records the leave hold itself and is "
            "not a substitute for flag_member_watch. "
            "Copy rules (enforced — invalid copy is rejected, not posted): <=200 characters, "
            "plain text only (no markdown, links, or @mentions), and avoid the in-game chat "
            "filter — write 'and' not '&', and never '+<number>'. Name the members and keep it "
            "warm (e.g. 'Noted 1spaceO2 and pigsareus are on leave for the week — see you when "
            "you're back!')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "copy": {
                    "type": "string",
                    "description": (
                        "The exact clan-chat message to relay: <=200 chars, plain text. Name "
                        "the members. This is what a leader pastes into clan chat verbatim."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short internal rationale for the card (why it's being relayed). Not "
                        "shown in clan chat."
                    ),
                },
                "member_tag": {
                    "type": "string",
                    "description": (
                        "Optional player tag/name/handle when the relay is about one specific "
                        "member (links the card to them)."
                    ),
                },
            },
            "required": ["copy"],
        },
    },
    {
        "name": "record_leadership_followup",
        "description": (
            "Record an operational observation as a durable leadership-scoped memory tagged "
            "'followup'. Use when you detect a pattern worth remembering — a rank swing, a "
            "recurring no-show, a compliance gap. "
            "This is the ONLY awareness write tool that can open a member-review action card. "
            "IMPORTANT — this is a NOTE, not an escalation. On its own it reaches no human: "
            "it does not post anywhere and does not raise a card. To actually ask leadership "
            "for something, either post to the leader-lounge lane (#leaders), or pass "
            "action_type + member_tag so it becomes a #actions card a leader can decide. "
            "The result tells you which happened via 'escalated'. "
            "Keep the recommendation concrete (who, what, when) so a human "
            "can act on it without re-doing the analysis. "
            "ATOMIC — each call is ONE decision a leader can act on or decline on its own. Never "
            "bundle multiple members or multiple actions into one followup: three kick reviews are "
            "three calls, a kick and a promotion are two calls. If you catch yourself writing 'and' "
            "or a list of members into a single recommendation, split it. "
            "Set revisit_at plus signal_key when future awareness should reconsider the same signal."
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
                "action_type": {
                    "type": "string",
                    "enum": [
                        "kick_recommendation",
                        "promotion_recommendation",
                        "demotion_recommendation",
                    ],
                    "description": "Set this (WITH member_tag) for a member kick/promotion/demotion review, and it becomes a #actions card a leader can decide — the only way this tool reaches a human. Omit it and the followup is recorded as a memory only; nothing is posted and no leader is asked anything.",
                },
                "revisit_at": {
                    "type": "string",
                    "description": "Optional ISO-8601 time when awareness should reconsider this followup.",
                },
                "signal_key": {
                    "type": "string",
                    "description": "Required with revisit_at; copy the source signal_key verbatim.",
                },
                "away_until": {
                    "type": "string",
                    "description": (
                        "Set this (WITH member_tag) when a leader says a member is AWAY — a "
                        "leave of absence. It records a hold that PAUSES that member's "
                        "inactivity/kick clock until the given date, so they are not flagged "
                        "for being idle while they are away. ISO date or datetime, e.g. "
                        "'2026-08-03'. Use it only for a member who told leaders they would "
                        "be gone; someone merely quiet is not on hold."
                    ),
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
                    "description": "The `signal_key` of the signal this revisit tracks. Copy it verbatim from `signals_by_category` or `hard_post_signals`.",
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
        "name": "get_game_mode_performance",
        "description": (
            "Performance in ONE named game mode — the member's own record and the "
            "clan leaderboard for it.\n\n"
            "Use this whenever someone asks about a specific mode by name: "
            "'how am I doing in C.H.A.O.S Draft League', 'chaos mode leaderboard', "
            "'who's best at Crazy Arena', 'my Showdown record'. Do NOT answer such "
            "questions from the grouped Events/Challenges rollups in get_member or "
            "the clan mode windows — those bucket every special event together and "
            "cannot separate one mode from another.\n\n"
            "`mode` is matched loosely against how members and the game name modes, "
            "so 'chaos', 'C.H.A.O.S Draft League' and 'Chaos_1v1_Draft' all work. "
            "When nothing matches, the result has resolved=false and "
            "available_modes — offer those real names instead of telling the member "
            "the mode does not exist.\n\n"
            "Pass member_tag to include that member's record and their rank. The "
            "leaderboard needs at least 3 battles in the window to list a member.\n\n"
            "OMIT `mode` entirely to list every mode the clan actually plays, with "
            "battle counts and clan win rates — use that for 'what game modes do you "
            "track?', 'what modes can I ask about?', or when a member is browsing "
            "rather than asking about one mode. Members cannot ask about a mode "
            "whose name they do not know, and the raw names are unguessable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": (
                        "Mode name as the member said it (e.g. 'chaos', 'Crazy Arena'). "
                        "Omit to list every tracked mode."
                    ),
                },
                "member_tag": {
                    "type": "string",
                    "description": "Optional player tag to include a personal record and rank.",
                },
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days (default 90).",
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
            "rank. Use for 'who has won X the most' questions. Requires award_type.\n"
            "- current_standings: live standings for the four season-end awards in "
            "one call — War Champ (top points), Iron King (perfect war attendance — "
            "4/4 decks every required battle day, post-victory days excluded), "
            "Donation Champ (top season donations), Rookie MVP (top points among "
            "members who joined this season). Mid-season the three competitive "
            "awards (War Champ, Donation Champ, Rookie MVP) show who would win "
            "if the season ended now; Iron King is NOT a one-winner race — it's "
            "recognition for every member still on perfect attendance, so frame "
            "iron_kings as 'these players are amazing / still on track' and "
            "celebrate the whole list, not 'who is leading Iron King'. After "
            "season-close everything is the final podium / honor roll. Use for "
            "'who's leading war this season?', 'who's still on Iron King "
            "track?', 'who's the rookie to watch?'. Returns {season_id, "
            "war_champ, iron_kings, donation_champs, rookie_mvps, freshness}; "
            "each entry has rank, tag, name, metric_value, metric_unit, "
            "metadata. The `freshness` block includes `as_of` and "
            "`current_week_included` — quote those when answering 'right now' "
            "questions so players see how fresh the read is. War Champ and "
            "Rookie MVP points include the current in-progress week. Honors "
            "season_id; ignores member_tag / award_type / rank / limit "
            "filters.\n\n"
            "For a single player's full trophy case prefer get_member with "
            "include=['awards']."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["list", "leaderboard", "current_standings"],
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
    {
        "name": "lookup_reference",
        "description": (
            "Resolve one of Elixir's own shorthand reference codes and return its "
            "full record. Elixir emits these codes itself, so when a leader mentions "
            "one in chat ('look at R137', 'why did L60 stay quiet?'), "
            "call this tool to pull the real record BEFORE answering — never guess "
            "what a code means or invent its contents.\n\n"
            "Reference kinds (the leading letter selects the kind, case-insensitive):\n"
            "- R<n> — a leader-action recommendation: a kick / promotion / demotion / "
            "relay card Elixir raised to the leadership action board for a human to "
            "decide. Returns action_type, status (proposed/done/rejected), the target "
            "member (name + tag), objective, rationale, the in-game clan-chat copy, and "
            "the decision (who decided, when, any note) plus outcome if decided.\n"
            "- L<n> — an awareness loop: one hourly deliberation tick. Returns whether "
            "it posted or stayed silent, its reasoning, what it posted (channel + "
            "summary + members), and read health (errors / degraded blocks / hard-post "
            "signal count).\n"
            "- M<n> — a stored clan memory: a durable note Elixir wrote. Returns the "
            "memory kind, title, body, summary, scope, subject member, status "
            "(active/archived), author, and tags.\n\n"
            "Accepts the code with the letter ('R137', 'L60', 'M340') or a bare "
            "number plus an explicit `kind`. Returns {error, hint} when the code "
            "doesn't resolve."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": (
                        "The reference code, e.g. 'R137', 'L60', or 'M340'. The "
                        "leading letter selects the kind (R = leader action, L = loop, "
                        "M = memory). A bare number is allowed when "
                        "`kind` is given."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["leader_action", "loop", "memory"],
                    "description": (
                        "Explicit kind, only needed when `reference` is a bare number "
                        "with no letter prefix."
                    ),
                },
            },
            "required": ["reference"],
        },
    },
]

# Definitions kept above the export preserve direct-executor compatibility while
# the LLM sees one canonical owner per question. Only this 17-tool block is ever
# offered to shared workflows.
_SHARED_TOOL_NAMES = (
    "resolve_member",
    "get_member",
    "get_member_war_detail",
    "get_river_race",
    "get_clan_roster",
    "get_elixir_state",
    "get_deck_intelligence",
    "get_deck_recommendations",
    "read_deck_link",
    "get_battle_intelligence",
    "lookup_cards",
    "get_member_cards",
    "cr_api",
    "save_clan_memory",
    "record_leadership_followup",
    "get_game_mode_performance",
    "get_awards",
    "lookup_reference",
)
_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}
TOOLS = [_TOOLS_BY_NAME[name] for name in _SHARED_TOOL_NAMES]

__all__ = ["TOOLS"]
