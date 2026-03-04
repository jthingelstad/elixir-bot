"""cr_knowledge.py — Static Clash Royale + POAP KINGS clan knowledge.

Injected into the LLM system prompt so Elixir understands game mechanics
and clan policies that aren't available from the CR API.
"""

# Trophy thresholds worth celebrating when a member crosses them.
# Every 1,000 trophies is a milestone worth noting.
TROPHY_MILESTONES = list(range(1000, 15001, 1000))

# Days of the week when River Race battles happen (0=Monday, 6=Sunday).
WAR_BATTLE_DAYS = {3, 4, 5, 6}  # Thursday, Friday, Saturday, Sunday
WAR_TRAINING_DAYS = {0, 1, 2}  # Monday, Tuesday, Wednesday

_KNOWLEDGE = """
=== CLASH ROYALE GAME KNOWLEDGE ===

WAR SCHEDULE (River Race):
- River Race runs weekly. Each week is one "race" within a longer war season (4-5 weeks).
- Seasons are identified as SEASON-WEEK (e.g., "130-1" = season 130, week 1).
- The season_id in the API corresponds to the season number. section_index is the week (0-indexed).
- Battle days are THURSDAY through SUNDAY. Members should use their war decks on these days.
- Monday through Wednesday are training/preparation days — no war battles.
- Each member gets 4 battle decks per day during battle days — one deck per battle.
- Players MUST use all 4 decks each battle day. Thank players who have used theirs. Nudge those who haven't.
- The clan that reaches the finish line (fame goal) first wins the race.
- At the end of the war season, the clan with the most war trophies in their group is the war champion.

TROPHY MILESTONES WORTH CELEBRATING:
- Every 1,000 trophies is a milestone (1k, 2k, 3k, ... up to 15k).
- Arena names come from the API — celebrate when a member reaches a new arena.
- Reaching 10,000+ trophies puts a player in the upper echelon and is a major milestone.

ROLES:
- member → elder → coLeader → leader
- Elder: can kick members (once per 20 min) and invite/accept players.
- Co-Leader: can manage the clan and start wars.
- Leader: full control.

=== POAP KINGS CLAN RULES ===

CLAN: POAP KINGS (#J2RGCRVG)
- Join requirement: 2,000+ trophies.
- War participation is encouraged but not required. No pressure — real life comes first.
- Elders donate regularly to support the clan. Help your clanmates and earn your title.
- Active members expected. Idle accounts are removed to keep the clan healthy.
- Push ladder, join Clan Wars when you can, donate to clanmates — that's how you earn Elder.

CLAN COMPOSITION:
- For every 10 members: ~1 leader/co-leader, 2-3 elders, the rest members.
- We can't all be elders — the role has meaning and responsibility.
- Promotions should maintain this balance.

WAR CHAMP:
- The top Clan Wars contributor each season is crowned War Champ.
- War Champ receives a free Pass Royale.
- War seasons last 4-5 weeks. Weekly standings are shared to keep competition alive.

PERFECT WAR PARTICIPATION:
- A player who uses all 4 decks every single battle day for an entire war season earns a free Pass Royale.
- This is a major achievement — acknowledge and celebrate it at season end.
- Track this across all war races in a season.

DONATIONS:
- Donation standings are highlighted once per day, towards end of day.
- Consistent donations over time is what drives Elder promotion — not just one big week.

POAP (Proof of Attendance):
- Each season, active members receive a POAP — a collectible badge that marks the moment.
- Your proof of Arena Push.

PROMOTION GUIDELINES (for evaluating Elder candidates):
- Consistent donations: roughly 50+ per week shows reliable support. Consistency matters more than volume.
- War participation: uses all 4 battle decks during Thu-Sun battle days when they can.
- Active: seen in the last 7 days, not just logging in but playing ladder or wars.
- Tenure: been in the clan for at least 2 weeks.
- Clan composition: check if we have room for another elder (target 2-3 per 10 members).
- No single metric is disqualifying — look at the overall picture.
- Real life comes first. Occasional inactivity is fine if the member is otherwise solid.
""".strip()


def get_knowledge_block():
    """Return the full knowledge block for injection into the LLM system prompt."""
    return _KNOWLEDGE


def is_war_battle_day(weekday):
    """Check if a given weekday (0=Monday) is a war battle day."""
    return weekday in WAR_BATTLE_DAYS


def crossed_milestone(old_trophies, new_trophies):
    """Return the milestone crossed, or None."""
    for m in TROPHY_MILESTONES:
        if old_trophies < m <= new_trophies:
            return m
    return None
