"""Elder math, ratified 2026-08-04 in #leaders.

Three changes, each traceable to a decision:

1. **Doing nothing scores zero.** Mid-rank percentile is a RANKING device and
   the elder score uses it as a CREDIT MAGNITUDE, so a tied block of
   non-participants was being paid its midpoint. Measured on the live roster:
   27 of 41 members had zero ranked battles and every one received 0.329, worth
   up to 0.086 of final score — 1.7× the swap margin that decides seats.

2. **Finishing a war day counts extra.** Jamie: *"All four is more than half the
   decks and should impact calculation."* Straight decks÷available scored two
   decks as exactly half a day's work.

3. **Tenure wins close calls.** Jamie: *"clan tenure should weigh in for ties or
   close calls. Tenure wins."* Inside the swap deadband the seat goes to the
   longer-tenured player rather than automatically to the incumbent.

The governing principle behind all of it, from the same conversation: *"it is
important that Elder is based on things any player can do... we don't promote on
war points, we promote on war decks."*
"""

from __future__ import annotations

import pytest

from engine.management import (
    FULL_DAY_BONUS,
    SWAP_MARGIN,
    _elder_band,
    _elder_scores,
    _participation_percentile,
    _percentile,
    _war_day_credit,
)

# --------------------------------------------------------------- zero anchor


def test_non_participation_scores_zero_not_the_tied_midpoint():
    """The bug: 27 members tied at zero each received 0.329."""
    values = [0.0] * 27 + [5.0, 50.0, 200.0]
    assert _participation_percentile(0.0, values) == 0.0
    # ...while the plain ranking percentile still hands the tied block its
    # midpoint, which is correct FOR RANKING and wrong as a credit.
    assert _percentile(0.0, values) > 0.4


def test_participants_are_ranked_against_each_other():
    """With most of the roster at zero, a whole-roster percentile would hand the
    very first battle ~0.67 — a bigger cliff than the gap between a 1-battle
    member and a 221-battle one."""
    values = [0.0] * 27 + [1.0, 100.0, 221.0]
    assert _participation_percentile(1.0, values) == pytest.approx(1 / 6)
    assert _participation_percentile(221.0, values) == pytest.approx(5 / 6)


def test_a_roster_where_nobody_participates_pays_nobody():
    assert _participation_percentile(0.0, [0.0, 0.0, 0.0]) == 0.0


# ------------------------------------------------------------ war day credit


def test_all_four_decks_is_worth_more_than_twice_two():
    """The ratified sentence, as arithmetic."""
    assert _war_day_credit(4, 4) == 1.0
    assert _war_day_credit(2, 4) == pytest.approx(0.375)
    assert _war_day_credit(4, 4) > 2 * _war_day_credit(2, 4)


def test_the_credit_is_monotonic_and_bounded():
    credits = [_war_day_credit(used, 4) for used in range(5)]
    assert credits == sorted(credits), "more decks is never worth less"
    assert credits[0] == 0.0 and credits[-1] == 1.0


def test_an_absent_day_earns_nothing():
    assert _war_day_credit(0, 4) == 0.0


def test_a_day_with_no_decks_available_is_not_counted_against_anyone():
    """A war day the member could not play is not a day they skipped."""
    assert _war_day_credit(0, 0) == 0.0


def test_overuse_cannot_exceed_a_full_day():
    """Defensive: a bad row must not hand out more than one day of credit."""
    assert _war_day_credit(9, 4) == 1.0


def test_the_bonus_is_a_share_of_the_day_not_an_addition():
    """The range has to stay 0-1 or every percentile downstream shifts."""
    proportional = (1 - FULL_DAY_BONUS) * 0.5
    assert _war_day_credit(2, 4) == pytest.approx(proportional)


def test_four_half_days_do_not_equal_two_full_days(engine_conn):
    """Why the average is per-day rather than summed decks ÷ summed available:
    summing first makes 8 decks look like 8 decks however they were played."""
    from engine.db import ensure_player
    from engine.management import _war_rate

    ensure_player(engine_conn, "#HALF", "Halfway", "2026-07-01T00:00:00Z")
    ensure_player(engine_conn, "#FULL", "Finisher", "2026-07-01T00:00:00Z")

    def _day(tag, idx, used):
        engine_conn.execute(
            "INSERT INTO war_attendance_days (season_id, section_index, war_day_index, "
            "player_tag, observed_at, decks_used, decks_available) "
            "VALUES (134, 3, ?, ?, ?, ?, 4)",
            (idx, tag, f"2026-08-0{idx + 1}T10:00:00Z", used),
        )

    for idx in range(4):
        _day("#HALF", idx, 2)
    for idx in range(2):
        _day("#FULL", idx, 4)
    engine_conn.commit()

    now = "2026-08-05T00:00:00Z"
    half = _war_rate(engine_conn, "#HALF", now)
    full = _war_rate(engine_conn, "#FULL", now)
    assert half == pytest.approx(0.375)
    assert full == pytest.approx(1.0)
    assert full > half, "eight decks across four half-days is not two finished days"


# ------------------------------------------------------------------- tenure


def test_tenure_decides_a_close_call(engine_conn):
    """Inside the deadband the seat goes to the longer-tenured player.

    Rewritten 2026-08-05. The first version of this test asserted substrings of
    `inspect.getsource(_elder_band)` — it would have passed on a reformat that
    broke the rule and failed on a harmless rename. It tested the source text,
    not the behaviour, which is the exact class of test this audit was about.
    """
    from tests.test_engine_management import ANCHOR, ANCHOR_NOW, _seed_ranked

    for i in range(6):
        _seed_ranked(engine_conn, f"#F{i}", war_used=4, war_avail=16, war_days=2, donations=100)
    # A near-tie inside SWAP_MARGIN, where the CHALLENGER is longer-tenured.
    _seed_ranked(
        engine_conn, "#CHAL", tenure=400, war_used=12, war_avail=16, war_days=3, donations=300
    )
    _seed_ranked(
        engine_conn,
        "#INC",
        role="elder",
        tenure=60,
        war_used=12,
        war_avail=16,
        war_days=3,
        donations=295,
    )
    _seed_ranked(
        engine_conn, "#E2", role="elder", war_used=13, war_avail=16, war_days=3, donations=350
    )
    _seed_ranked(
        engine_conn, "#E3", role="elder", war_used=14, war_avail=16, war_days=3, donations=380
    )

    scores = _elder_scores(engine_conn, ANCHOR_NOW, ANCHOR)
    margin = scores["#CHAL"]["score"] - scores["#INC"]["score"]
    assert 0 < margin < SWAP_MARGIN, f"test needs a near-tie inside the deadband, got {margin:.3f}"

    band = _elder_band(engine_conn, scores, ANCHOR_NOW)
    assert "#CHAL" in band["promotable"], "the longer-tenured challenger takes the close call"
    assert "#INC" in band["demotable"], "and the shorter-tenured incumbent yields the seat"


def test_tenure_never_invents_a_promotion_for_a_challenger_who_is_behind(engine_conn):
    """Tenure breaks a tie; it does not manufacture one.

    A challenger BEHIND on score loses no matter how long they have been here —
    otherwise "tenure wins" would quietly become "tenure outranks participation",
    which is the opposite of the rule Elder is built on.
    """
    from tests.test_engine_management import ANCHOR, ANCHOR_NOW, _seed_ranked

    for i in range(6):
        _seed_ranked(engine_conn, f"#F{i}", war_used=4, war_avail=16, war_days=2, donations=100)
    # The challenger is the LONGEST-tenured member on the roster and still behind.
    _seed_ranked(
        engine_conn, "#OLD", tenure=900, war_used=9, war_avail=16, war_days=3, donations=200
    )
    for tag, used, don in (("#E1", 13, 350), ("#E2", 14, 380), ("#E3", 15, 400)):
        _seed_ranked(
            engine_conn,
            tag,
            role="elder",
            tenure=60,
            war_used=used,
            war_avail=16,
            war_days=3,
            donations=don,
        )

    scores = _elder_scores(engine_conn, ANCHOR_NOW, ANCHOR)
    assert scores["#OLD"]["score"] < scores["#E1"]["score"], "test needs the veteran behind"

    band = _elder_band(engine_conn, scores, ANCHOR_NOW)
    assert "#OLD" not in band["promotable"], "900 days of tenure must not buy a seat"
