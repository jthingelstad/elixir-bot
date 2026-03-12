from __future__ import annotations

import db


STARTUP_SYSTEM_SIGNALS = [
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
