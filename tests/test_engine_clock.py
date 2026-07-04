"""War clock (architecture §16.1–§16.2): colosseum detection, season length
discovery, battle-time war-key resolution."""
from __future__ import annotations

from datetime import datetime, timezone

from engine.clock import parse_battle_time, resolve_war_keys, war_clock

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _race(period_type="warDay", period_index=24, section_index=3, fame=4000):
    return {"periodType": period_type, "periodIndex": period_index,
            "sectionIndex": section_index, "clan": {"tag": "#J2RGCRVG", "fame": fame}}


def test_colosseum_detection_and_finish_line():
    # Colosseum has NO finish line — fame accrues across all four battle days
    # (verified live 2026-07-03: 20,600 fame on day 2, race still on). The
    # spec's 5,000 constant was wrong; race_finished must never fire here.
    clock = war_clock(_race("colosseum", 31, 4, fame=4800), NOW, season_id=133)
    assert clock.is_colosseum_week is True
    assert clock.finish_line is None
    assert clock.race_finished is False
    deep = war_clock(_race("colosseum", 31, 4, fame=20600), NOW, season_id=133)
    assert deep.race_finished is False and deep.pace_status == "colosseum"


def test_normal_week_finish_line_10000():
    clock = war_clock(_race("warDay", 24, 3, fame=10100), NOW, season_id=133)
    assert clock.finish_line == 10000 and clock.race_finished is True


def test_training_day_fields():
    clock = war_clock(_race("training", 21, 3), NOW, season_id=133)
    assert clock.phase == "training"
    assert clock.war_day_index is None
    assert clock.pace_status == "training"
    assert clock.battle_days_remaining == 4


def test_war_day_index_math():
    # periodIndex 24 → day_index 24 % 7 = 3 → first battle day (war_day_index 0)
    clock = war_clock(_race("warDay", 24, 3), NOW, season_id=133)
    assert clock.day_index == 3 and clock.war_day_index == 0
    assert clock.battle_days_remaining == 3
    last = war_clock(_race("warDay", 27, 3), NOW, season_id=133)
    assert last.war_day_index == 3 and last.battle_days_remaining == 0


def test_section_from_period_index():
    clock = war_clock(_race("warDay", 24, 3), NOW, season_id=133)
    assert clock.section_index == 3
    assert clock.period_index // 7 == clock.section_index


def test_resolve_war_keys_same_day():
    clock = war_clock(_race("warDay", 24, 3), NOW, season_id=133)
    keys = resolve_war_keys("20260701T110000.000Z", clock, NOW)
    assert keys == (133, 3, 0)


def test_resolve_war_keys_yesterday_lands_previous_day():
    # now is battle day 1 (periodIndex 25); a battle from yesterday (war-date -1)
    clock = war_clock(_race("warDay", 25, 3), NOW, season_id=133)
    keys = resolve_war_keys("20260630T110000.000Z", clock, NOW)
    assert keys == (133, 3, 0)  # previous war day, same section


def test_resolve_war_keys_training_day_has_no_war_day():
    clock = war_clock(_race("training", 21, 3), NOW, season_id=133)
    season, section, war_day = resolve_war_keys("20260701T110000.000Z", clock, NOW)
    assert war_day is None  # training-day time maps to the week, never a war day
    assert (season, section) == (133, 3)


def test_resolve_war_keys_no_clock():
    assert resolve_war_keys("20260701T110000.000Z", None, NOW) == (None, None, None)


def test_parse_battle_time_cr_compact():
    dt = parse_battle_time("20260701T110000.000Z")
    assert dt is not None and dt.hour == 11 and dt.tzinfo is not None


def test_period_anchor_beats_fixed_boundary():
    # Carried learning (pre-v5.1 issue #20): CR's reset hour skews per season;
    # the observed period-start anchors the 24h day. Anchor at 09:37Z means
    # 2h later there are ~22h left — not the fixed-10:00Z reading.
    from datetime import datetime, timedelta, timezone

    anchor = datetime(2026, 7, 4, 9, 37, tzinfo=timezone.utc)
    now = anchor + timedelta(hours=2)
    clock = war_clock(_race("warDay", 24, 3, fame=500), now, season_id=133,
                      period_anchor=anchor)
    assert 21.5 <= clock.hours_left_in_period <= 22.5
    # Stale anchor (past its day) falls back to the nominal boundary
    stale = war_clock(_race("warDay", 24, 3, fame=500),
                      anchor + timedelta(hours=30), season_id=133,
                      period_anchor=anchor)
    assert stale.hours_left_in_period > 0
