"""cr_knowledge.py — Clash Royale + clan knowledge loaded from prompt files.

Provides game knowledge for LLM prompts and configurable thresholds
for signal detection. All static content comes from prompts/ directory.
"""

import prompts

# Trophy thresholds loaded from CLAN.md
_thresholds = prompts.thresholds()
_MILESTONE_INTERVAL = _thresholds.get("trophy_milestone_interval", 1000)
_MILESTONE_MAX = _thresholds.get("trophy_milestone_max", 15000)
TROPHY_MILESTONES = list(range(_MILESTONE_INTERVAL, _MILESTONE_MAX + 1, _MILESTONE_INTERVAL))

# Inactivity threshold (days)
INACTIVITY_DAYS = _thresholds.get("inactivity_days", 3)

# Donation highlight hour (Chicago time, 24h)
DONATION_HIGHLIGHT_HOUR = _thresholds.get("donation_highlight_hour", 20)

# Days of the week when River Race battles happen (0=Monday, 6=Sunday).
WAR_BATTLE_DAYS = {3, 4, 5, 6}  # Thursday, Friday, Saturday, Sunday
WAR_TRAINING_DAYS = {0, 1, 2}  # Monday, Tuesday, Wednesday


def is_war_battle_day(weekday):
    """Check if a given weekday (0=Monday) is a war battle day."""
    return weekday in WAR_BATTLE_DAYS


def crossed_milestone(old_trophies, new_trophies):
    """Return the milestone crossed, or None."""
    for m in TROPHY_MILESTONES:
        if old_trophies < m <= new_trophies:
            return m
    return None
