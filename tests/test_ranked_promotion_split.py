"""The ranked-promotion split — Agentic Loop v2, wake policy.

Unlike the badge split (two populations, a clean `level is None` binary), ranked
promotions are a GRADIENT, and the clan's interest tracks it. Over 20 days to
2026-08-04, promotions reached a post:

    into leagues 1-3 (Master tiers)      15 events,  3 posted   20%
    into leagues 4-6 (Champion tiers)    10 events,  6 posted   60%
    into league 7   (Ultimate Champion)   2 events,  2 posted  100%

League 4 is where the game itself renames the tier to "Champion", so the split
uses the game's own boundary rather than an invented threshold.

The second finding these lock in: reaching Ultimate Champion used to emit BOTH
`ultimate_champion_reached` AND a `pol_promotion` for the same player at the
identical timestamp.
"""

from __future__ import annotations

import json

import pytest

from engine.normalize import ranked_league_tier


def _promote(conn, tag, prev, new, at="2026-08-04T12:00:00Z"):
    from engine.emitters.player import emit_ranked

    emit_ranked(
        conn,
        tag,
        {"league": prev},
        {"league": new},
        at,
        None,
    )
    conn.commit()
    rows = conn.execute(
        "SELECT event_type, payload_json FROM player_events WHERE player_tag = ? "
        "AND event_type IN ('pol_promotion','champion_league_reached',"
        "'ultimate_champion_reached') ORDER BY event_id",
        (tag,),
    ).fetchall()
    return [(r["event_type"], json.loads(r["payload_json"])) for r in rows]


@pytest.mark.parametrize(
    "league,expected",
    [
        (1, "master"),
        (2, "master"),
        (3, "master"),
        (4, "champion"),
        (6, "champion"),
        (7, "ultimate"),
    ],
)
def test_the_tier_predicate_follows_the_games_own_names(league, expected):
    assert ranked_league_tier(league) == expected


def test_an_unknown_league_is_treated_as_the_grind_tier():
    """Fail toward silence: an unparseable league must not wake the clan."""
    assert ranked_league_tier(None) == "master"
    assert ranked_league_tier("bogus") == "master"


def test_wake_policy_matches_the_measured_posting_rates():
    from engine.event_contracts import wake_policy

    assert wake_policy("pol_promotion")[0] == "digest"
    assert wake_policy("champion_league_reached")[0] == "immediate"
    assert wake_policy("ultimate_champion_reached")[0] == "immediate"


def test_every_ranked_promotion_type_is_in_the_shared_constant():
    """A reader that hardcodes one name drops either the new events or the
    entire pre-split back catalogue."""
    from engine.event_contracts import EVENT_CONTRACTS, RANKED_PROMOTION_EVENT_TYPES

    for event_type in RANKED_PROMOTION_EVENT_TYPES:
        assert event_type in EVENT_CONTRACTS


def test_reaching_ultimate_champion_emits_exactly_one_event(engine_conn):
    """The duplicate this split removed: the same player, the same timestamp,
    counted twice."""
    from engine.db import ensure_player

    ensure_player(engine_conn, "#UC", "Champ", "2026-08-04T00:00:00Z")
    events = _promote(engine_conn, "#UC", 6, 7)
    assert [e[0] for e in events] == ["ultimate_champion_reached"]
    assert events[0][1]["league_tier"] == "ultimate"


def test_a_champion_tier_arrival_is_its_own_event(engine_conn):
    from engine.db import ensure_player

    ensure_player(engine_conn, "#CH", "Climber", "2026-08-04T00:00:00Z")
    events = _promote(engine_conn, "#CH", 3, 4)
    assert [e[0] for e in events] == ["champion_league_reached"]
    assert events[0][1] == {"league": 4, "prev_league": 3, "league_tier": "champion"}


def test_a_master_tier_bump_stays_routine(engine_conn):
    from engine.db import ensure_player

    ensure_player(engine_conn, "#MA", "Grinder", "2026-08-04T00:00:00Z")
    events = _promote(engine_conn, "#MA", 1, 2)
    assert [e[0] for e in events] == ["pol_promotion"]
    assert events[0][1]["league_tier"] == "master"
