"""Grounded-copy sweep (#167/#168/#169/#139): badge humanizer, badge_earned
fallback, and cohort-wave fallback that names members + the concrete milestone
instead of leaking raw keys or posting a bare count."""
from __future__ import annotations

import json

from engine.normalize import humanize_badge, mastery_card
from engine.recognition.compose import render_intent


def _row(intent_type: str, payload: dict):
    """Minimal render_intent input — it only reads payload_json + intent_type."""
    return {"payload_json": json.dumps(payload), "intent_type": intent_type, "scope": "public"}


# --- badge humanizer (#167) --------------------------------------------------

def test_mastery_card_splits_camelcase():
    assert mastery_card("MasteryRonin") == "Ronin"
    assert mastery_card("MasterySuspiciousBush") == "Suspicious Bush"
    assert mastery_card("MasteryMovingCannon") == "Moving Cannon"
    assert mastery_card("CrazyArenaBadge1") is None  # not a mastery badge
    assert mastery_card(None) is None


def test_humanize_badge_never_leaks_raw_key():
    assert humanize_badge("MasteryRonin") == "Card Mastery: Ronin"
    assert humanize_badge("CrazyArenaBadge1") == "Crazy Arena Badge 1"
    assert humanize_badge("Classic12Wins") == "Classic Challenge 12 wins"
    assert humanize_badge("SomeUnknownBadgeX") == "Some Unknown Badge X"
    assert humanize_badge(None) == "a new badge"
    # the invariant: no output is a bare camelCase key
    for v in ("MasteryTesla", "MasteryRascals", "TopLeague", "CrazyArenaBadge1"):
        assert " " in humanize_badge(v) or ":" in humanize_badge(v)


# --- badge_earned deterministic fallback (#167) ------------------------------

def test_badge_earned_fallback_reads_as_mastery_milestone():
    out = render_intent(_row("celebrate:badge_earned",
                             {"event_type": "badge_earned", "player_name": "Th15_Guy",
                              "badge_name": "MasteryRonin"}))
    assert "MasteryRonin" not in out
    assert "Ronin" in out and "Card Mastery" in out


def test_badge_earned_fallback_non_mastery_uses_label():
    out = render_intent(_row("celebrate:badge_earned",
                             {"event_type": "badge_earned", "player_name": "pax",
                              "badge_name": "CrazyArenaBadge1"}))
    assert "CrazyArenaBadge1" not in out
    assert "Crazy Arena Badge 1" in out


# --- cohort-wave fallback (#169/#139) ----------------------------------------

def _wave(wave_type, members):
    return _row("cohort:cohort_wave",
                {"event_type": f"cohort_wave:{wave_type}", "wave_type": wave_type,
                 "members": members, "member_count": len(members)})


def test_cohort_fallback_names_members_and_milestone():
    out = render_intent(_wave("card_unlocked", [
        {"tag": "#A", "name": "sniperhendo"},
        {"tag": "#B", "name": "Sandeep"},
        {"tag": "#C", "name": "shimmeringhost"}]))
    assert "sniperhendo" in out and "Sandeep" in out and "shimmeringhost" in out
    assert "unlocked a new card" in out
    assert "Multiple members" not in out  # never the old content-free line


def test_cohort_fallback_uses_per_member_detail_when_present():
    out = render_intent(_wave("card_level_milestone", [
        {"tag": "#A", "name": "Vijay", "detail": "Goblins → 16"},
        {"tag": "#B", "name": "Ditaka", "detail": "Fireball → 16"},
        {"tag": "#C", "name": "Aaqib Javed", "detail": "Flying Machine → 16"}]))
    assert "Vijay (Goblins → 16)" in out
    assert "Ditaka (Fireball → 16)" in out
    assert "Aaqib Javed (Flying Machine → 16)" in out


def test_cohort_fallback_specific_or_silent_never_bare_count():
    # No names available → a warm line, but never a content-free tally.
    out = render_intent(_wave("badge_earned", [{"tag": "#A"}, {"tag": "#B"}, {"tag": "#C"}]))
    assert "Badge wave" in out
    assert "Multiple members hit the same milestone" not in out
