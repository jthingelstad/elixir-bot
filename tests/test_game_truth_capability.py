from capabilities.game_truth import (
    deterministic_correction,
    get_game_truth,
    live_war_claim_facts,
)
from engine.game_rules import (
    river_race_completed_from_score,
    river_race_finish_line,
)


def test_colosseum_truth_is_one_contract():
    live = {"period": {"is_colosseum_week": True, "period_type": "colosseum"}}
    truth = get_game_truth(live_war=live)
    mechanics = truth["mechanics"]
    assert mechanics["finish_line"] is None
    assert mechanics["every_battle_counts_for_standings"] is True
    assert river_race_finish_line("colosseum") is None
    assert (
        river_race_completed_from_score("colosseum", 40200, active_battle_phase=True)
        is False
    )


def test_live_claim_facts_and_fallback_are_grounded():
    facts = live_war_claim_facts(
        {"current_state": {"colosseum_week": True, "period_type": "colosseum"}}
    )
    assert facts["is_colosseum_week"] is True
    assert facts["finish_line"] is None
    assert "no finish line" in deterministic_correction(facts).lower()
