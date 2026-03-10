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
