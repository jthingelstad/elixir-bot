"""Tests for cr_knowledge.py — static game + clan knowledge."""

import cr_knowledge


def test_war_schedule_present():
    """Knowledge block mentions Thu-Sun battle days."""
    block = cr_knowledge.get_knowledge_block()
    assert "THURSDAY" in block.upper()
    assert "SUNDAY" in block.upper()
    assert "battle days" in block.lower()


def test_clan_rules_present():
    """Knowledge block includes POAP KINGS rules."""
    block = cr_knowledge.get_knowledge_block()
    assert "POAP KINGS" in block
    assert "Elder" in block
    assert "War Champ" in block
    assert "Pass Royale" in block
    assert "2,000" in block


def test_promotion_guidelines_present():
    """Knowledge block includes promotion criteria."""
    block = cr_knowledge.get_knowledge_block()
    assert "donation" in block.lower()
    assert "war participation" in block.lower()
    assert "active" in block.lower()


def test_is_war_battle_day():
    """Thursday (3) through Sunday (6) are battle days."""
    assert cr_knowledge.is_war_battle_day(3)  # Thursday
    assert cr_knowledge.is_war_battle_day(4)  # Friday
    assert cr_knowledge.is_war_battle_day(5)  # Saturday
    assert cr_knowledge.is_war_battle_day(6)  # Sunday
    assert not cr_knowledge.is_war_battle_day(0)  # Monday
    assert not cr_knowledge.is_war_battle_day(1)  # Tuesday
    assert not cr_knowledge.is_war_battle_day(2)  # Wednesday


def test_milestones_every_1k():
    """Milestones are at every 1,000 trophies."""
    assert 1000 in cr_knowledge.TROPHY_MILESTONES
    assert 5000 in cr_knowledge.TROPHY_MILESTONES
    assert 10000 in cr_knowledge.TROPHY_MILESTONES
    assert 15000 in cr_knowledge.TROPHY_MILESTONES
    # Every entry is a multiple of 1000
    for m in cr_knowledge.TROPHY_MILESTONES:
        assert m % 1000 == 0


def test_crossed_milestone():
    """Correctly detects milestone crossings."""
    assert cr_knowledge.crossed_milestone(9850, 10023) == 10000
    assert cr_knowledge.crossed_milestone(4900, 5100) == 5000
    assert cr_knowledge.crossed_milestone(5100, 5900) is None  # no crossing
    assert cr_knowledge.crossed_milestone(11900, 12100) == 12000
    assert cr_knowledge.crossed_milestone(900, 1100) == 1000
    assert cr_knowledge.crossed_milestone(2500, 3100) == 3000
