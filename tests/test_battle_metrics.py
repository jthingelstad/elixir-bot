"""Unit tests for the pure per-battle metrics (Battle Intelligence F1)."""

import json

from engine.battle_metrics import (
    closeness_band,
    discipline_delta,
    hp_margin,
    level_gap,
)


def _pj(*hps):
    return json.dumps(list(hps))


def test_hp_margin_reproduces_a_clean_win():
    # member kept king + 2 princess (standing 3), opponent lost a princess (standing 2)
    m = hp_margin(2000, _pj(1500, 1500), 2000, _pj(1500))
    # (3-2)*3000 + ((2000+3000) - (2000+1500)) = 3000 + 1500
    assert m == 4500


def test_hp_margin_destroyed_king_is_zero_not_null():
    # opponent king destroyed (0) and both princess gone ([]): standing 0
    m = hp_margin(2000, _pj(1000, 1000), 0, _pj())
    assert m == (3 - 0) * 3000 + ((2000 + 2000) - 0)
    assert m == 13000


def test_hp_margin_null_when_tower_fields_absent():
    assert hp_margin(None, _pj(1, 1), 2000, _pj(1)) is None
    assert hp_margin(2000, None, 2000, _pj(1)) is None
    assert hp_margin(2000, _pj(1), 2000, None) is None


def test_closeness_bands_match_locked_cuts():
    assert closeness_band(100) == 3  # squeaker
    assert closeness_band(-4199) == 3
    assert closeness_band(4200) == 2
    assert closeness_band(5799) == 2
    assert closeness_band(5800) == 1
    assert closeness_band(7699) == 1
    assert closeness_band(7700) == 0  # stomp
    assert closeness_band(20000) == 0
    assert closeness_band(None) is None


def test_level_gap_deck_scoped():
    member = [{"level": 14}, {"level": 12}]  # avg 13
    opp = [{"level": 11}, {"level": 11}]  # avg 11
    assert level_gap(member, opp) == 2.0


def test_level_gap_null_for_ranked():
    member = [{"level": 14}]
    opp = [{"level": 11}]
    assert level_gap(member, opp, is_ranked=True) is None


def test_level_gap_null_when_a_side_missing():
    assert level_gap([{"level": 11}], None) is None
    assert level_gap([{"level": 11}], []) is None


def test_discipline_delta():
    assert discipline_delta(5.0, 2.0) == 3.0
    assert discipline_delta(None, 2.0) is None
    assert discipline_delta(5.0, None) is None
