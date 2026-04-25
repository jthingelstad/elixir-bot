"""Tests for cr_knowledge.py — game + clan knowledge loaded from prompt files."""

import cr_knowledge
import prompts


def test_war_schedule_present():
    """Knowledge block mentions Thu-Sun battle days."""
    block = prompts.knowledge_block()
    assert "THURSDAY" in block.upper()
    assert "SUNDAY" in block.upper()
    assert "battle days" in block.lower()


def test_clan_rules_present():
    """Knowledge block includes POAP KINGS rules."""
    block = prompts.knowledge_block()
    assert "POAP KINGS" in block
    assert "Elder" in block
    assert "War Champ" in block
    assert "Pass Royale" in block
    assert "2,000" in block


def test_promotion_guidelines_present():
    """Knowledge block includes promotion criteria."""
    block = prompts.knowledge_block()
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


def test_inactivity_threshold():
    """Inactivity threshold is loaded from CLAN.md."""
    assert cr_knowledge.INACTIVITY_DAYS == 3


def test_donation_highlight_hour():
    """Donation highlight hour is loaded from CLAN.md."""
    assert cr_knowledge.DONATION_HIGHLIGHT_HOUR == 20


def test_deck_usage_knowledge():
    """Knowledge block mentions 4 decks per day."""
    block = prompts.knowledge_block()
    assert "4 battle decks" in block or "4 decks" in block


def test_clan_composition_in_policy():
    """Clan composition rules live in POLICY.md (loaded by leadership lanes only)."""
    block = prompts.policy()
    assert "2-3 elders" in block
    assert "composition" in block.lower()


def test_donation_consistency_knowledge():
    """Knowledge block mentions donation consistency."""
    block = prompts.knowledge_block()
    assert "consistent" in block.lower()


def test_perfect_participation_knowledge():
    """Knowledge block mentions perfect war participation reward."""
    block = prompts.knowledge_block()
    assert "perfect" in block.lower()
    assert "Pass Royale" in block


def test_season_naming_knowledge():
    """Knowledge block mentions SEASON-WEEK naming convention."""
    block = prompts.knowledge_block()
    assert "SEASON-WEEK" in block
    assert "130-1" in block
