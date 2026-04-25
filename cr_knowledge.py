"""cr_knowledge.py — Clash Royale + clan knowledge loaded from prompt files.

Provides game knowledge for LLM prompts and configurable thresholds
for signal detection. All static content comes from prompts/ directory.
"""

import prompts

_thresholds = prompts.thresholds()

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


# Cards required to advance from level N to level N+1, by rarity.
# Index N in the list is the count needed to go from level (N+1) to (N+2).
# i.e. CARDS_REQUIRED_BY_RARITY["common"][0] is the cost of the first upgrade
# (level 1 → 2). The last entry is the cost of the final upgrade before max.
#
# Source: https://clashroyale.fandom.com/wiki/Cards (Cards Required Per Level
# table). The CR API does not expose this — values are public game data,
# stable across the lifetime of a level cap.
# verified: 2026-04-25
CARDS_REQUIRED_BY_RARITY = {
    "common": [2, 4, 10, 20, 50, 100, 200, 400, 800, 1000, 2000, 5000, 5000, 5000, 5000],
    "rare": [2, 4, 10, 20, 50, 100, 200, 400, 800, 1000, 1500, 2000, 2000],
    "epic": [4, 10, 20, 50, 100, 200, 400, 800, 1000, 1250, 1500],
    "legendary": [2, 4, 10, 20, 40, 80, 100, 100],
    "champion": [5, 10, 20, 50, 100],
}


def cards_required_to_upgrade(rarity, current_level):
    """Cards needed to advance from current_level to current_level+1.

    Returns None if the rarity is unknown or the card is at or beyond max
    (no further upgrade exists). current_level is 1-indexed.
    """
    if not isinstance(current_level, int) or current_level < 1:
        return None
    table = CARDS_REQUIRED_BY_RARITY.get((rarity or "").lower())
    if not table:
        return None
    idx = current_level - 1
    if idx >= len(table):
        return None
    return table[idx]


def is_ready_to_upgrade(rarity, current_level, count):
    """True iff the player has stockpiled enough cards to advance one level.

    Maxed cards return False (no upgrade is available). Unknown rarity also
    returns False — better to be silent than wrong.
    """
    needed = cards_required_to_upgrade(rarity, current_level)
    if needed is None:
        return False
    return isinstance(count, int) and count >= needed
