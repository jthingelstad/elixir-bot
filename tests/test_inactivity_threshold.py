"""Trophy-scaled inactivity threshold for at-risk member flagging.

Rule: per-member threshold = max(floor_days, trophies/1000 * 1.4). The floor
guards against very-low-trophy players being flagged immediately; the
trophy term gives high-trophy members proportionally more rope.
"""

from storage.war_analytics import _effective_inactivity_threshold


def test_threshold_floor_holds_for_low_trophy_member():
    # 3000 trophies × 1.4/1000 = 4.2 → below 7 floor, so floor wins.
    assert _effective_inactivity_threshold(3000, floor_days=7) == 7.0


def test_threshold_floor_holds_when_trophies_missing_or_zero():
    assert _effective_inactivity_threshold(None, floor_days=7) == 7.0
    assert _effective_inactivity_threshold(0, floor_days=7) == 7.0


def test_threshold_uses_trophy_scaling_at_5k():
    # 5000 trophies × 1.4/1000 = 7.0 → exactly the floor.
    assert _effective_inactivity_threshold(5000, floor_days=7) == 7.0


def test_threshold_scales_above_floor_for_10k_member():
    # 10000 trophies × 1.4/1000 = 14.0 → 14 days.
    assert _effective_inactivity_threshold(10000, floor_days=7) == 14.0


def test_threshold_scales_for_12500_trophy_member():
    # 12500 × 1.4/1000 = 17.5
    assert _effective_inactivity_threshold(12500, floor_days=7) == 17.5


def test_floor_param_can_be_raised():
    # If a caller passes a higher floor, that wins for low-trophy players.
    assert _effective_inactivity_threshold(2000, floor_days=10) == 10.0
    # And does not lower a higher trophy-scaled threshold.
    assert _effective_inactivity_threshold(15000, floor_days=10) == 21.0
