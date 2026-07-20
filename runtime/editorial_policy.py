"""Dark-launched editorial policy shared by composition and delivery."""

from __future__ import annotations

import os

POSITIVE_WAR_MESSAGING_FLAG = "ELIXIR_POSITIVE_WAR_MESSAGING"

POSITIVE_WAR_MESSAGING_GUIDANCE = """
PUBLIC WAR PARTICIPATION — POSITIVE RECOGNITION ONLY (dark-launch override):
- Proactive member-facing war copy recognizes the members who participated. Lead with how
  many played, who completed all four decks, their points, the race they moved, or another
  concrete contribution.
- Do not count, name, call out, remind, pressure, or create urgency around members who have
  not played or have decks left. Do not say "untouched," "no-shows," "only partial," "last
  chance/shot," or "get/play/use your decks." The game clock may frame achievements and the
  live race, never a participation nag.
- This rule overrides any generic GAME.md or CLAN.md suggestion to encourage participation.
  Nonparticipation remains private operational evidence for the deterministic management
  system; it is not material for an awareness post.
- Prefer the canonical war capability's `engagement.participation_recognition` facts. If the
  available positive facts do not make a worthwhile post, stay silent.
""".strip()


def positive_war_messaging_enabled() -> bool:
    """Return whether participant-positive proactive war copy is dark-launched."""
    return os.getenv(POSITIVE_WAR_MESSAGING_FLAG, "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


__all__ = [
    "POSITIVE_WAR_MESSAGING_FLAG",
    "POSITIVE_WAR_MESSAGING_GUIDANCE",
    "positive_war_messaging_enabled",
]
