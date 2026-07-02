"""The v5 in-game celebration relay features the member's recent win (not a bare 'congrats')."""

from __future__ import annotations

from runtime.app import _v5_event_clan_chat_context


def test_celebration_relay_features_recent_win():
    spec = {
        "objective": "career_wins_milestone",
        "target_player_name": "Th15_Guy",
        "target_player_tag": "#QLGYYG0Q",
        "copy": "Th15_Guy hit 2,000 career wins. Huge POAP KINGS milestone.",
    }
    meta = {"summary": {"detection_type": "career_wins_milestone", "milestone": 2000}}
    win = {"opponent_tag": "#OPP", "mode": "Ranked", "crowns_for": 3, "crowns_against": 1}

    enriched = _v5_event_clan_chat_context(spec, meta, win)
    # seeded win + agentic tool instruction + anti-invention guard
    assert "#OPP" in enriched
    assert "CR READ TOOLS" in enriched
    assert "do not invent" in enriched


def test_celebration_relay_without_win_stays_plain():
    spec = {"objective": "career_wins_milestone", "target_player_name": "Th15_Guy",
            "target_player_tag": "#QLGYYG0Q", "copy": "…"}
    meta = {"summary": {"detection_type": "career_wins_milestone", "milestone": 2000}}
    plain = _v5_event_clan_chat_context(spec, meta, None)
    assert "Recent win JSON" not in plain
