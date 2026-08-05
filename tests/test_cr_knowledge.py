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
    # NOT a literal floor. This used to assert "2,000" — a test enforcing the
    # very staleness that told a 7,053-trophy joiner they were "well clear of
    # our 2,000-trophy entry line". Assert the RULE is stated; its value comes
    # from clan_daily_metrics via the <<REQUIRED_TROPHIES>> token.
    assert "Join requirement:" in block


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
    """The elder band lives in POLICY.md (loaded by leadership lanes only), and
    the prose must match the engine constants. The old assertion here pinned a
    '2-3 elders per 10' cap that the 2026-07-05 band replaced, so the test kept
    passing while the file told the model something false — assert against
    engine.management now, so drift fails instead of persisting."""
    from engine.management import ELDER_BAND_CEIL, ELDER_BAND_FLOOR

    block = prompts.policy()
    band = f"{round(ELDER_BAND_FLOOR * 100)}-{round(ELDER_BAND_CEIL * 100)}%"
    assert band in block
    assert "leadership included" in block


def test_policy_describes_war_as_the_primary_elder_path():
    """POLICY.md said donations were the primary path long after war became the
    heavier term, and that text reached leaders on 2026-08-02. The weights must
    be quoted from the engine, not restated from memory."""
    from engine.management import SCORE_W_DONATION, SCORE_W_WAR

    block = prompts.policy()
    assert f"{SCORE_W_WAR:.2f} x competitive" in block
    assert f"{SCORE_W_DONATION:.2f} x donation%" in block


def test_policy_does_not_cite_retired_tool_fields():
    """`elder_cap_reached` and `recommended` were fields POLICY.md told the model
    to trust; neither exists any more, and `evaluate_elder_eligibility` is dead
    code. Naming them taught the model to look for data it can never get."""
    block = prompts.policy()
    for gone in ("elder_cap_reached", "evaluate_elder_eligibility"):
        assert gone not in block


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
