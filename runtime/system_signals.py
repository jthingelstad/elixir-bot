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
        "signal_key": "capability_weekly_clan_recap_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Weekly Clan Recap",
            "message": (
                "Elixir is getting a longer weekly recap for the clan. "
                "Starting next week, I will post a must-read summary of river race storylines, clan momentum, and standout player progress."
            ),
            "details": [
                "The recap will be a longer 3-5 paragraph post built for active clan members.",
                "It will focus on the week's clan story, River Race progress, and individual highlights that mattered.",
                "This week's announcement is a preview so everyone knows the recap is coming next week.",
            ],
            "audience": "clan",
            "capability_area": "weekly_recap",
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
