"""Scorer constants are law (recognition.md §2–§3): every value asserted
verbatim; accrual, coalescing, cohort machinery."""
from __future__ import annotations

import json

from engine.recognition import scorer
from engine.recognition.scorer import Candidate, base_score, cohort_waves, decide, sort_key

NOW = "2026-07-01T12:00:00Z"


def test_base_scores_verbatim():
    expected = {
        "ultimate_champion_reached": (120, True),
        "pol_global_rank_attained": (110, True),
        "card_level_milestone": (95, True),
        "career_wins_milestone": (85, True),
        "collection_level_milestone": (80, True),
        "badge_earned": (55, False),
        "pol_promotion": (45, False),
        "best_trophies_peak": (40, False),
        "ranked_pulse": (30, False),
    }
    for event_type, (score, bypass) in expected.items():
        assert base_score(event_type, {}) == (score, bypass), event_type


def test_threshold_and_window_constants():
    from datetime import timedelta

    assert scorer.HIGHLIGHT_THRESHOLD == 80
    assert scorer.ACCRUAL_WINDOW == timedelta(days=14)


def test_card_unlocked_dynamic():
    assert base_score("card_unlocked", {"rarity": "champion"}) == (90, True)
    assert base_score("card_unlocked", {"rarity": "legendary"}) == (65, False)
    assert base_score("card_unlocked", {"rarity": "epic"}) == (0, False)


def test_trophy_push_formula_edges():
    assert base_score("trophy_push", {"trophy_delta": 100}) == (25, False)
    assert base_score("trophy_push", {"trophy_delta": 150}) == (30, False)
    assert base_score("trophy_push", {"trophy_delta": 300}) == (45, False)  # cap
    assert base_score("trophy_push", {"trophy_delta": 1000}) == (45, False)


def test_arena_up_ratified_constant():
    assert base_score("arena_up", {}) == (85, True)


def test_celebrate_priority_table_verbatim():
    p = scorer.CELEBRATE_PRIORITY
    assert p["ultimate_champion_reached"] == 100
    assert p["pol_global_rank_attained"] == 95
    assert p["arena_up"] == 90
    assert p["card_level_milestone"] == 80
    assert p["card_unlocked"] == 75
    assert p["badge_earned"] == 70
    assert p["collection_level_milestone"] == 65
    assert p["career_wins_milestone"] == 60
    assert p["pol_promotion"] == 50
    assert p["best_trophies_peak"] == 40
    assert p["ranked_pulse"] == 15
    assert p["trophy_push"] == 10


def _cand(event_type, key=None, tag="#A", at=NOW, payload=None, arrival=0):
    return Candidate(key=key or f"{event_type}:{tag}:x", event_type=event_type,
                     subject_tag=tag, occurred_at=at, payload=payload or {},
                     arrival=arrival)


def test_same_tick_coalescing_picks_by_priority():
    group = [_cand("best_trophies_peak", arrival=0),
             _cand("card_level_milestone", arrival=1),
             _cand("collection_level_milestone", arrival=2)]
    selected = max(group, key=sort_key)
    assert selected.event_type == "card_level_milestone"


def test_decide_bypass_posts_regardless(engine_conn):
    selected = _cand("collection_level_milestone")  # 80, bypass
    post, score, trace = decide(engine_conn, "#A", selected, [selected], None)
    assert post is True and score >= 80
    assert isinstance(trace, dict)


def test_decide_accrues_below_threshold(engine_conn):
    selected = _cand("best_trophies_peak")  # 40, no bypass
    post, score, trace = decide(engine_conn, "#A", selected, [selected], None)
    assert post is False and score == 40


def test_decide_sums_stored_evidence(engine_conn):
    # a stored 45-point pol_promotion inside the window
    engine_conn.execute(
        "INSERT INTO player_events (dedup_key, event_type, player_tag, observed_at,"
        " payload_json, created_at) VALUES (?, 'pol_promotion', '#A', ?, ?, ?)",
        ("pol_promotion:#A:6", "2026-06-25T12:00:00Z", json.dumps({"league": 6}), NOW))
    engine_conn.commit()
    selected = _cand("best_trophies_peak")  # 40 + 45 = 85 ≥ 80
    post, score, _ = decide(engine_conn, "#A", selected, [selected], None)
    assert post is True and score == 85


def test_decide_accrues_suppressed_ranked_pulse_ledger(engine_conn):
    for day in ("20260629", "20260630"):
        engine_conn.execute(
            """INSERT INTO recognition_ledger
                 (recognition_key, stream, event_refs_json, score, claimed_at)
               VALUES (?, 'battle', ?, 30, ?)""",
            (
                f"ranked_pulse:#A:{day}",
                json.dumps({"refs": [f"battle_events:#A:{day}"]}),
                f"{day[:4]}-{day[4:6]}-{day[6:8]}T12:00:00Z",
            ),
        )
    engine_conn.commit()

    selected = _cand("ranked_pulse", key="ranked_pulse:#A:20260701")
    post, score, trace = decide(engine_conn, "#A", selected, [selected], None)
    assert post is True
    assert score == 90
    assert len(trace["recognition_evidence"]) == 3


def test_decide_cutoff_at_last_highlight(engine_conn):
    engine_conn.execute(
        "INSERT INTO player_events (dedup_key, event_type, player_tag, observed_at,"
        " payload_json, created_at) VALUES (?, 'pol_promotion', '#A', ?, ?, ?)",
        ("pol_promotion:#A:6", "2026-06-25T12:00:00Z", json.dumps({}), NOW))
    engine_conn.commit()
    # last posted highlight AFTER that evidence → evidence excluded
    post, score, _ = decide(engine_conn, "#A", _cand("best_trophies_peak"),
                            [_cand("best_trophies_peak")], "2026-06-30T00:00:00Z")
    assert post is False and score == 40


def test_cohort_wave_three_members_same_day():
    cands = [_cand("badge_earned", key=f"badge_earned:#{i}:x", tag=f"#{i}")
             for i in "ABC"]
    waves = cohort_waves(cands)
    assert len(waves) == 1
    key = next(iter(waves))
    assert key.startswith("cohort_wave:badge_earned:")
    assert len(waves[key]) == 3


def test_no_cohort_below_three_or_wrong_type():
    assert cohort_waves([_cand("badge_earned", tag="#A"),
                         _cand("badge_earned", tag="#B")]) == {}
    assert cohort_waves([_cand("collection_level_milestone", tag=f"#{i}") for i in "ABC"]) == {}
