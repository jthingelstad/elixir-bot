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
