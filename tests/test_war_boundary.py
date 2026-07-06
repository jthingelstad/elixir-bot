"""Season/week boundary integrity (#166).

The river-race API emits a post-battle 'reset' snapshot at a section's end —
same season/section, but every clan's fame collapses to 0 — just before the
next season/section appears. Persisting it let the rollover finalize a finished
week from zeros, which corrupted Season 133's Colosseum standings (recorded as
#3 / 0 fame instead of #1 / 42,600) and posted a wrong #river-race recap.

These guard the three fixes: monotonic participation writes, the race-baseline
reset guard (merge_baseline), and the finalizer/close_season hardening.
"""
from __future__ import annotations

from engine.emitters import emit
from engine.emitters.war import merge_baseline

TAG = "#J2RGCRVG"
RIVAL = "#RIVAL01"


def _race(season, section, our_fame, *, period_type="colosseum", period_index=22,
          rival_fame=700):
    """A projected race aspect (project_race_aspect shape). Participant fame
    mirrors our clan fame split across two members for the monotonic checks."""
    a, b = our_fame * 3 // 5, our_fame - (our_fame * 3 // 5)
    return {
        "season_id": season, "section_index": section,
        "period_index": period_index, "period_type": period_type,
        "our_tag": TAG, "our_fame": our_fame,
        "clans": {
            TAG: {"name": "POAP KINGS", "fame": our_fame},
            RIVAL: {"name": "Rival", "fame": rival_fame},
        },
        "participants": {
            "#AAA": {"name": "Al", "fame": a, "repair_points": 0,
                     "boat_attacks": 0, "decks_used": 16, "decks_used_today": 4},
            "#BBB": {"name": "Bo", "fame": b, "repair_points": 0,
                     "boat_attacks": 0, "decks_used": 15, "decks_used_today": 4},
        },
    }


def _emit(conn, payload, at):
    return emit(conn, "riverrace", TAG, "race", payload, at)


# --- merge_baseline (pure) -------------------------------------------------

def test_merge_keeps_peak_on_same_section_reset():
    old = _race(133, 4, 42600)
    new = _race(133, 4, 0, rival_fame=0)
    assert merge_baseline(old, new) is old  # reset suppressed, peak kept


def test_merge_passes_through_new_season():
    old = _race(133, 4, 42600)
    new = _race(134, 0, 0, period_type="training")
    assert merge_baseline(old, new) is new  # real rollover survives


def test_merge_passes_through_new_section():
    old = _race(133, 3, 32600)
    new = _race(133, 4, 0, period_type="training")  # next week starts at 0
    assert merge_baseline(old, new) is new


def test_merge_passes_through_normal_progress():
    old = _race(133, 4, 20000)
    new = _race(133, 4, 42600)
    assert merge_baseline(old, new) is new


# --- full boundary through emit() ------------------------------------------

def test_reset_then_rollover_finalizes_from_peak(engine_conn):
    c = engine_conn
    # first sight is silent (§8); participation begins accruing next observation
    _emit(c, _race(133, 4, 20000), "2026-07-06T03:00:00Z")
    # peak Colosseum observation
    _emit(c, _race(133, 4, 42600), "2026-07-06T04:00:00Z")
    # post-battle reset snapshot — must NOT wipe participation or baseline
    _emit(c, _race(133, 4, 0, rival_fame=0), "2026-07-06T10:00:00Z")
    part = c.execute(
        "SELECT SUM(fame) f, SUM(decks_used) d FROM war_participation "
        "WHERE season_id=133 AND section_index=4").fetchone()
    assert part["f"] == 42600, "reset snapshot wiped Colosseum participation"
    assert part["d"] == 31
    # season rollover to 134 — finalize the finished Colosseum week
    _emit(c, _race(134, 0, 0, period_type="training"), "2026-07-06T10:20:00Z")
    wk = c.execute(
        "SELECT our_rank, our_fame FROM war_weeks WHERE season_id=133 AND section_index=4"
    ).fetchone()
    assert wk["our_fame"] == 42600, "week finalized from the reset, not the peak"
    assert wk["our_rank"] == 1
    season = c.execute(
        "SELECT final_rank FROM war_seasons WHERE season_id=133").fetchone()
    assert season["final_rank"] == 1, "season recorded a bogus finish rank"


def test_monotonic_participation_never_decreases(engine_conn):
    c = engine_conn
    _emit(c, _race(133, 4, 20000), "2026-07-06T03:00:00Z")  # first sight (silent)
    _emit(c, _race(133, 4, 42600), "2026-07-06T04:00:00Z")
    before = c.execute(
        "SELECT fame, decks_used FROM war_participation "
        "WHERE season_id=133 AND section_index=4 AND player_tag='#AAA'").fetchone()
    # a stale/zero re-observation of the same section
    _emit(c, _race(133, 4, 0, rival_fame=0), "2026-07-06T10:00:00Z")
    after = c.execute(
        "SELECT fame, decks_used FROM war_participation "
        "WHERE season_id=133 AND section_index=4 AND player_tag='#AAA'").fetchone()
    assert after["fame"] == before["fame"]
    assert after["decks_used"] == before["decks_used"]
