from __future__ import annotations

import db


STARTUP_SYSTEM_SIGNALS = [
    {
        "signal_key": "release_three_lane_elixir_v3",
        "signal_type": "capability_unlock",
        "payload": {
            "title": 'Achievement Unlocked: v3 "Three-Lane Elixir"',
            "message": (
                "Elixir has entered a new form: Three-Lane Elixir. "
                "Instead of one crowded stream trying to cover everything, I now work through focused lanes with clearer purpose, stronger context, and less signal pileup."
            ),
            "details": [
                "River Race coordination, player progression, and clan-event celebrations now have distinct lanes instead of fighting for space in one mixed feed.",
                "Ask Elixir is now its own open conversation channel, so clanmates can talk directly with Elixir without needing every update mixed into the same room.",
                "This should make Elixir feel sharper, easier to follow, and more useful across the server while still staying one shared Elixir mind.",
            ],
            "audience": "clan",
            "capability_area": "three_lane_elixir",
        },
    },
    {
        "signal_key": "capability_memory_system_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Stronger Memory",
            "message": (
                "Elixir now carries a stronger memory system for clan continuity. "
                "I can keep better track of ongoing context, recent interactions, and "
                "leadership notes instead of treating every conversation like a reset."
            ),
            "details": [
                "Conversation memory now carries more continuity across chats.",
                "Leadership can inspect stored memory with /elixir memory show.",
                "This makes follow-up questions and ongoing clan operations more consistent.",
            ],
            "audience": "clan",
            "capability_area": "memory",
        },
    },
    {
        "signal_key": "capability_battle_pulse_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Battle Pulse",
            "message": (
                "Elixir now tracks fresh ladder and Path of Legend momentum, not just river race activity. "
                "I can surface hot streaks, trophy pushes, and ranked climbs when clanmates are cooking."
            ),
            "details": [
                "Elixir can now react to ladder and Path of Legend heaters.",
                "Big trophy pushes and ranked promotions can now get called out to the clan.",
                "Battle Pulse only reacts to fresh battle activity, so it should feel timely instead of noisy.",
            ],
            "audience": "clan",
            "capability_area": "battle_pulse",
        },
    },
    {
        "signal_key": "capability_badge_and_achievement_celebrations_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Badge Celebrations",
            "message": (
                "Elixir can now celebrate more of the real Clash Royale milestone moments that matter to the clan. "
                "Badge tier-ups, achievement star gains, and big profile unlocks can now show up as proper callouts instead of staying buried inside player profiles."
            ),
            "details": [
                "Years Played badge jumps can now be surfaced as real clan celebration moments.",
                "Achievement star gains and notable badge unlocks can now feed the channel-update workflow.",
                "The goal is to make meaningful player progression feel visible and worth reacting to.",
            ],
            "audience": "clan",
            "capability_area": "badge_celebrations",
        },
    },
    {
        "signal_key": "capability_player_profile_depth_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Deeper Player Profiles",
            "message": (
                "Elixir now carries a richer player-profile layer behind the scenes. "
                "That includes derived Clash Royale account age and a recent games-per-day activity read, so member profiles can talk about more than trophies and role."
            ),
            "details": [
                "Profiles can now carry how long a member has been playing Clash Royale based on Years Played badge data.",
                "Recent battle activity can now be summarized into a games-per-day style signal instead of raw battle history only.",
                "This gives roster bios and site experiences more texture when talking about grinders, veterans, and long-time players.",
            ],
            "audience": "clan",
            "capability_area": "player_profile_depth",
        },
    },
    {
        "signal_key": "capability_weekly_clan_recap_v2",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Weekly Clan Recap",
            "message": (
                "A new weekly clan recap is now part of the rhythm. "
                "Every Monday, Elixir will drop a bigger must-read summary built to capture the week's story, the clan's momentum, and the players who moved it."
            ),
            "details": [
                "Expect a fuller read than a normal announcement, with the biggest clan beats pulled into one place.",
                "River Race movement, trophy trends, hot streaks, and standout player progress will all feed into the recap.",
                "If you want the best single snapshot of how POAP KINGS is doing each week, this will be the post to watch for.",
            ],
            "audience": "clan",
            "capability_area": "weekly_recap",
        },
    },
    {
        "signal_key": "capability_long_term_trends_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Long-Term Trend Tracking",
            "message": (
                "Elixir is now building a real long-term trend layer for POAP KINGS. "
                "That means daily tracking for player and clan performance, not just isolated snapshots, and it sets us up with a real time-series foundation for understanding how the clan is evolving over time."
            ),
            "details": [
                "Daily player trophy movement and clan-wide performance trends are now being captured into a growing time-series record.",
                "Battle activity, wins, losses, and mode-based trends are being organized so Elixir can spot real momentum instead of reacting to one-off spikes.",
                "This is the foundation for future charts, stronger weekly summaries, and sharper player and clan sentiment grounded in long-term data.",
            ],
            "audience": "clan",
            "capability_area": "long_term_trends",
        },
    },
    {
        "signal_key": "capability_roster_showcase_depth_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Deeper Roster Showcase",
            "message": (
                "The POAP KINGS website roster now has a deeper stat layer available behind the scenes. "
                "Badge highlights, mastery standouts, achievement progress, account age, and recent activity data can now flow into the website payload instead of stopping at basic roster facts."
            ),
            "details": [
                "Roster payloads can now carry curated badge highlights instead of only bare member basics.",
                "Top mastery progress and achievement progress are now available for richer site presentation.",
                "This opens the door to deeper player cards, richer bios, and more personality on poapkings.com.",
            ],
            "audience": "clan",
            "capability_area": "roster_showcase",
        },
    },
    {
        "signal_key": "capability_poap_kings_integration_v2",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Formal POAP KINGS Integration",
            "message": (
                "Elixir now has a formal POAP KINGS integration layer behind the scenes. "
                "That is a big internal cleanup win: the website publishing flow is now explicit, cleaner, and easier to reason about instead of living in a confusing fake-generic publish path."
            ),
            "details": [
                "POAP KINGS website publishing now lives in a dedicated integration instead of being mixed into Elixir core.",
                "The site update flow is cleaner for leadership to operate and easier to extend with future POAP KINGS-specific features.",
                "This kind of cleanup is mostly invisible day to day, but it makes Elixir more reliable and gives us a better foundation for future website and POAP work.",
            ],
            "audience": "clan",
            "capability_area": "poap_kings_integration",
        },
    },
    {
        "signal_key": "capability_war_awareness_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: War Awareness",
            "message": (
                "Elixir just got a massive River Race intelligence upgrade. "
                "I now track war periods as live game-driven phases instead of relying on calendar assumptions, "
                "which means I can follow each practice day and battle day with much stronger awareness of what is actually happening in the race."
            ),
            "details": [
                "Elixir now knows the active war phase, which day of practice or battle we are in, which week of the season it is, and how much time is left in the current war day.",
                "Battle-day tracking is much deeper now: Elixir can follow who has used all, some, or none of their decks, who is leading the day in fame, and who still needs a nudge.",
                "War storytelling is much stronger now too, with day-by-day battle recaps, richer weekly war recaps, and season-level war context built from tracked participation over time.",
            ],
            "audience": "clan",
            "capability_area": "war_awareness",
        },
    },
    {
        "signal_key": "capability_card_modes_and_war_completion_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Sharper Card And War Intel",
            "message": (
                "Elixir now reads some of the most important Clash Royale edge cases much more cleanly. "
                "I understand Heroes, Evo cards, and combo Hero + Evo cards without the old 'Evolution Level' confusion, "
                "war signals are now tracked through a stronger clock-aligned pipeline, and I know when POAP KINGS has already completed the week's race so I stop asking for extra drive after the job is done."
            ),
            "discord_content": (
                "**Achievement Unlocked: Sharper Card And War Intel**\n\n"
                "Elixir now reads some of the most important Clash Royale edge cases much more cleanly. "
                "I understand Heroes, Evo cards, and combo Hero + Evo cards without the old 'Evolution Level' confusion, "
                "war signals are now tracked through a stronger clock-aligned pipeline, and I know when POAP KINGS has already completed the week's race so I stop asking for extra drive after the job is done.\n\n"
                "- Deck and profile card language now uses player-facing card modes like Hero, Evo, and Hero + Evo instead of raw evolution-level wording.\n"
                "- River Race detection is now more reliable and more clock-based, so important war updates should not get lost between snapshot checks.\n"
                "- When the live war API shows that POAP KINGS has already finished the race, Elixir will shift into completion and recognition mode instead of pushing for more win-drive messaging."
            ),
            "details": [
                "Deck and profile card language now uses player-facing card modes like Hero, Evo, and Hero + Evo instead of raw evolution-level wording.",
                "River Race detection is now more reliable and more clock-based, so important war updates should not get lost between snapshot checks.",
                "When the live war API shows that POAP KINGS has already finished the race, Elixir will shift into completion and recognition mode instead of pushing for more win-drive messaging.",
            ],
            "audience": "clan",
            "capability_area": "war_and_card_intel",
        },
    },
    {
        "signal_key": "capability_subagent_behavior_upgrade_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Sharper Channel Instincts",
            "message": (
                "Elixir now behaves more differently from channel to channel in the ways that matter. "
                "The lane prompts behind the scenes are much more complete, so I should feel more natural in each room instead of sounding too similar everywhere."
            ),
            "discord_content": (
                "**Achievement Unlocked: Sharper Channel Instincts**\n\n"
                "Elixir now behaves more differently from channel to channel in the ways that matter. "
                "The lane prompts behind the scenes are much more complete, so I should feel more natural in each room instead of sounding too similar everywhere.\n\n"
                "- `#river-race`, `#war-talk`, `#general`, and `#ask-elixir` now have much clearer personalities, so war command, tactical help, matter-of-fact answers, and exploratory conversation should feel more distinct.\n"
                "- `#reception` and `#promote-the-clan` are now more intentionally recruiter-minded, which should make onboarding and member-driven recruiting feel smoother.\n"
                "- `#player-progress`, `#clan-events`, `#announcements`, and `#leader-lounge` now have stronger guidance for what counts as worth saying and how those updates should sound."
            ),
            "details": [
                "River Race command, tactical war help, matter-of-fact general answers, and exploratory Ask Elixir conversation now have much clearer boundaries.",
                "Reception and recruiting lanes are more intentionally recruiter-minded, which should make onboarding and member-driven recruiting feel smoother.",
                "Celebration, announcement, and leadership lanes now have stronger guidance for what counts as worth saying and how those updates should sound.",
            ],
            "audience": "clan",
            "capability_area": "subagent_behavior",
        },
    },
    {
        "signal_key": "capability_ask_elixir_reaction_feedback_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Ask Elixir Feedback Reactions",
            "message": (
                "Ask Elixir now has a faster feedback loop built right into the channel. "
                "If an answer helps, you can drop a thumbs-up. If it misses, you can drop a thumbs-down and Elixir will know it needs another shot."
            ),
            "discord_content": (
                "**Achievement Unlocked: Ask Elixir Feedback Reactions**\n\n"
                "Ask Elixir now has a faster feedback loop built right into the channel. "
                "If an answer helps, you can drop a thumbs-up. If it misses, you can drop a thumbs-down and Elixir will know it needs another shot.\n\n"
                "- In `#ask-elixir`, you can now react to substantial Elixir answers with `👍` and `👎` so feedback is one tap away.\n"
                "- A `👎` does not auto-reanswer, but it does tell Elixir the answer missed and prompts a quick invitation to try again or clarify what felt off.\n"
                "- That feedback now feeds into Elixir's review loop behind the scenes, so Ask Elixir can keep getting sharper over time."
            ),
            "details": [
                "In #ask-elixir, members can now react to substantial Elixir answers with thumbs-up and thumbs-down so feedback is one tap away.",
                "A thumbs-down does not auto-reanswer, but it does tell Elixir the answer missed and prompts a quick invitation to try again or clarify what felt off.",
                "That feedback now feeds into Elixir's review loop behind the scenes, so Ask Elixir can keep getting sharper over time.",
            ],
            "audience": "clan",
            "capability_area": "ask_elixir_feedback",
        },
    },
    {
        "signal_key": "feature_custom_emoji_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Custom Elixir Emoji",
            "message": (
                "POAP KINGS now has 19 custom Elixir-themed emoji available across the server. "
                "Use them in your messages, reactions, and anywhere Discord lets you drop emoji. "
                "They're all Elixir-flavored and ready for battle."
            ),
            "details": [
                "19 custom Elixir emoji are now registered as server emoji and available to everyone.",
                (
                    "The full set: :elixir: :elixir_angry: :elixir_celebrate: :elixir_cheers: "
                    ":elixir_elixir: :elixir_evil_laugh: :elixir_facepalm: :elixir_fireball: "
                    ":elixir_gg: :elixir_happy: :elixir_hype: :elixir_rage: :elixir_shield: "
                    ":elixir_skelly: :elixir_sleepy: :elixir_spell: :elixir_thinking: "
                    ":elixir_time: :elixir_trophy:"
                ),
                (
                    "IMPORTANT: You MUST use many of these emoji inline throughout your announcement message. "
                    "Show them off by weaving them naturally into the text and showcase the variety. "
                    "Use the actual :emoji_name: syntax so they render in Discord."
                ),
            ],
            "audience": "clan",
            "capability_area": "custom_emoji",
        },
    },
    {
        "signal_key": "capability_signal_quality_and_colosseum_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Smarter Signals & Colosseum Awareness",
            "message": (
                "Elixir just got a lot sharper about what is worth saying out loud and when to stay quiet. "
                "Player progress updates are now filtered to meaningful milestones instead of flooding the channel with every small step. "
                "And Elixir now fully understands Colosseum week — the final, highest-stakes week of every River Race season."
            ),
            "discord_content": (
                "**Achievement Unlocked: Smarter Signals & Colosseum Awareness**\n\n"
                "Elixir just got a lot sharper about what is worth saying out loud and when to stay quiet. "
                "Player progress updates are now filtered to meaningful milestones instead of flooding the channel with every small step. "
                "And Elixir now fully understands Colosseum week.\n\n"
                "**Signal quality:**\n"
                "- Card mastery celebrations now start at level 5 instead of level 1 — the early grind is quiet, the real milestones get the spotlight.\n"
                "- New card unlocks only fire for Epic, Legendary, and Champion cards. Common and Rare unlocks stay quiet.\n"
                "- Player level-ups now celebrate every 5th level instead of every single one.\n"
                "- Card upgrades now start at level 15 instead of 14.\n\n"
                "**Colosseum week:**\n"
                "- Elixir now recognizes the final week of every River Race season as Colosseum week.\n"
                "- 100 trophies are on the line in Colosseum — more than all other weeks combined.\n"
                "- No boat defenses, no boat battles. Elixir knows this and will not ask about them during Colosseum week.\n\n"
                "**Behind the scenes:**\n"
                "- Weekly database maintenance now runs Sunday at 2:00 AM CT with a cleanup report posted to leadership.\n"
                "- Member departures now distinguish likely kicks from voluntary leaves — no more warm farewells for inactive members who were removed."
            ),
            "details": [
                "Card mastery celebrations now start at level 5 instead of level 1 — the early grind is quiet, the real milestones get the spotlight.",
                "New card unlocks only fire for Epic, Legendary, and Champion cards. Common and Rare unlocks stay quiet.",
                "Player level-ups now celebrate every 5th level instead of every single one.",
                "Elixir now recognizes the final week of every River Race season as Colosseum week — 100 trophies on the line, no boat defenses, no boat battles.",
                "Weekly database maintenance now runs Sunday at 2:00 AM CT with a report posted to leadership.",
                "Member departures now distinguish likely kicks from voluntary leaves.",
            ],
            "audience": "clan",
            "capability_area": "signal_quality",
        },
    },
    {
        "signal_key": "capability_tournament_tracking_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Tournament Tracking",
            "message": (
                "Elixir can now watch clan-hosted private tournaments from start to finish. "
                "When a tournament tag is registered, Elixir tracks standings, captures every battle and card draft, "
                "and posts a full narrative recap when the tournament ends."
            ),
            "discord_content": (
                "**Achievement Unlocked: Tournament Tracking** :elixir:\n\n"
                "POAP KINGS tournaments just got an upgrade. Elixir can now track private clan tournaments from start to finish — "
                "standings, battles, cards, and all.\n\n"
                "**How it works:**\n"
                "- Leadership registers a tournament tag and Elixir starts watching — polling every 5 minutes for the life of the event.\n"
                "- Live updates drop in #clan-events as the tournament unfolds — who takes the lead, who gets dethroned.\n"
                "- Every battle is captured: both players' full drafted decks, crowns, and the outcome.\n\n"
                "**When the tournament ends:**\n"
                "- Elixir posts a full recap right here — the story of the tournament, not just the stats.\n"
                "- Card analysis breaks down the draft meta: which cards were picked most, which had the best win rates, "
                "and which players had signature draft tendencies.\n"
                "- Head-to-head matchup records show who faced who and what they brought to the table.\n\n"
                "This is **Elixir v3.1 \"Tournament Arc\"** — the first feature built specifically for POAP KINGS clan events."
            ),
            "details": [
                "Register a tournament tag and Elixir starts a dedicated polling job for the life of the event.",
                "Live updates post to #clan-events: tournament started, lead changes, tournament ended.",
                "Every battle captured with full card decks for both players, crowns, and winner.",
                "Tournament recap generated by Elixir with card draft analysis, player tendencies, and head-to-head records.",
                "Weekly recaps now include tournament results when a tournament happened that week.",
                "Tournament watch survives bot restarts — Elixir resumes tracking automatically.",
            ],
            "audience": "clan",
            "capability_area": "tournament_tracking",
        },
    },
    {
        "signal_key": "capability_anthropic_claude_migration_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: New Brain",
            "message": (
                "Elixir has migrated from OpenAI GPT to Anthropic Claude. "
                "This is a full intelligence upgrade — new models, native prompt caching, and a faster signal pipeline."
            ),
            "discord_content": (
                "**Achievement Unlocked: New Brain** :elixir:\n\n"
                "Elixir just got a brain transplant. Starting today, every conversation, every signal, "
                "and every piece of content Elixir produces is powered by **Anthropic Claude** instead of OpenAI GPT.\n\n"
                "**What changed:**\n"
                "- Chat, content, and promotion workflows now run on **Claude Sonnet** — "
                "stronger at staying in character, following instructions, and writing within guardrails.\n"
                "- Signal detection (the heartbeat that watches the clan 24/7) now runs on **Claude Haiku** — "
                "faster and more efficient for high-volume classification.\n"
                "- Native **prompt caching** means Elixir's large system prompts and 47 tool definitions "
                "are cached across calls, cutting latency and cost on every request.\n\n"
                "**What you'll notice:**\n"
                "- Elixir's voice may feel slightly different as the new models settle in. Same soul, new neurons.\n"
                "- Responses should be more consistent with Elixir's personality across channels.\n"
                "- Structured answers (war status, member lookups, roster data) should have fewer formatting hiccups.\n\n"
                "This is **Elixir v4.0 \"New Brain\"** — same Elixir, sharper mind."
            ),
            "details": [
                "Full migration from OpenAI GPT to Anthropic Claude models.",
                "Claude Sonnet powers chat, content, and promotion workflows.",
                "Claude Haiku powers observation and signal detection for speed and efficiency.",
                "Native prompt caching on system prompts and tool definitions reduces latency and cost.",
                "Tool definitions and message handling rewritten for Anthropic's native API format.",
                "Telemetry system generalized to provider-neutral naming.",
            ],
            "audience": "clan",
            "capability_area": "intelligence_migration",
        },
    },
    {
        "signal_key": "feature_card_quiz_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Card Quiz",
            "message": (
                "Elixir now has a card training quiz in #card-quiz. "
                "Members can test their Clash Royale card knowledge with interactive quizzes and a daily question with streak tracking."
            ),
            "discord_content": (
                "**Achievement Unlocked: Card Quiz** :elixir_hype:\n\n"
                "POAP KINGS just got a new training ground. **#card-quiz** is live — "
                "a dedicated channel where you can sharpen your Clash Royale card knowledge with real quizzes.\n\n"
                "**What's in there:**\n"
                "- `/elixir quiz start` — take a quick quiz (1-10 questions). "
                "Elixir will test you on elixir costs, rarities, card types, Evo/Hero modes, and Champions — "
                "all with card images pulled straight from the game.\n"
                "- **Daily question** — a new question drops every morning. "
                "Answer it to start building a streak. Come back tomorrow to keep it alive.\n"
                "- `/elixir quiz stats` — check your accuracy and streak.\n"
                "- `/elixir quiz leaderboard` — see who's running the longest daily streak in the clan.\n\n"
                "**Why it matters:**\n"
                "Knowing your cards is knowing your matchups. "
                "Whether it's the elixir cost of a counter or which cards have Evo, "
                "the quiz is built to make that knowledge stick.\n\n"
                "Head to **#card-quiz** and see what you know. :elixir_trophy:"
            ),
            "details": [
                "New #card-quiz channel with interactive quizzes and a daily question.",
                "Six question types covering elixir cost, rarity, card type, Evo/Hero capability, and Champion identification.",
                "All questions generated from a synced Clash Royale card catalog with card images.",
                "Daily streak tracking for consecutive correct answers on the daily question.",
                "Elixir now has a lookup_cards tool for accurate card data in #ask-elixir conversations.",
            ],
            "audience": "clan",
            "capability_area": "card_quiz",
        },
    },
    {
        "signal_key": "capability_race_command_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Race Command",
            "message": (
                "Elixir's River Race coverage just got a major upgrade. "
                "I now track the full competitive field — not just POAP KINGS — and carry the weight of our unbroken 1st-place streak in how I talk about the race."
            ),
            "discord_content": (
                "**Achievement Unlocked: Race Command** :elixir:\n\n"
                "Elixir's River Race coverage just leveled up. "
                "I'm not just watching POAP KINGS anymore — I'm watching the entire field.\n\n"
                "**What's new in #river-race:**\n"
                "- I now call out competing clans by name. "
                "Who's closest, who's making a move, and who's barely showing up — it all makes the updates now.\n"
                "- The fame gap sets the tone. "
                "A 200-fame lead feels different than a 5,000-fame lead, and the posts will reflect that.\n"
                "- Day transitions are now one clean message instead of two back-to-back posts. "
                "When a battle day ends and a new one starts, you get one cohesive update covering both.\n\n"
                "**The streak:**\n"
                "- POAP KINGS has finished 1st in every single river race since our very first one. "
                "That streak is now part of how I frame the race — "
                "not as a stat I announce every post, but as the floor I stand on.\n\n"
                "**Under the hood:**\n"
                "- Elixir's tool layer was consolidated from 51 tools down to 15 domain-aligned tools — "
                "cleaner, faster, and easier to reason about.\n\n"
                "This is **Elixir v4.2 \"Race Command\"** — sharper eyes on the river."
            ),
            "details": [
                "River Race updates now reference competing clans by name with fame-gap framing.",
                "Day transition signals (end of day + start of day) are merged into one cohesive post.",
                "The clan's unbroken 1st-place streak is now tracked as a durable identity memory.",
                "LLM tool layer consolidated from 51 to 15 domain-aligned tools.",
            ],
            "audience": "clan",
            "capability_area": "race_command",
        },
    },
]


def queue_startup_system_signals(*, conn=None) -> None:
    for signal in STARTUP_SYSTEM_SIGNALS:
        db.queue_system_signal(
            signal["signal_key"],
            signal["signal_type"],
            signal["payload"],
            conn=conn,
        )


__all__ = [
    "STARTUP_SYSTEM_SIGNALS",
    "queue_startup_system_signals",
]
