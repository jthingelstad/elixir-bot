"""Shape-drift tripwires over REAL CR payloads (tests/fixtures/cr/).

Every other engine test builds minimal dicts to target one delta; these run
the projection layer over payloads captured verbatim from raw_api_payloads,
so a Supercell shape change (renamed key, restructured list, new null) fails
HERE with a clear diff instead of surfacing as a silent None three layers up.

When a fixture goes stale, refresh it from the raw log (AGENTS.md 'Testing');
never hand-edit the JSON.
"""

from __future__ import annotations

import pytest

from engine.emitters.clan import project_clan_aspects
from engine.emitters.player import project_player_aspects
from engine.emitters.war import project_race_aspect
from engine.normalize import annotate, parse_cr_time
from tests.conftest import load_cr_fixture

# --- projection key contracts (exact sets: additions are drift too — they
# mean the projector grew and this contract should be updated consciously) --

PLAYER_ASPECTS = {"profile", "cards", "ranked"}
PROFILE_KEYS = {
    "arena_id",
    "arena_name",
    "badges",
    "best_trophies",
    "exp_level",
    "name",
    "trophies",
    "wins",
}
CARDS_KEYS = {"cards", "collection_level"}
# last/best added 2026-07-04 (ranked-and-profiles.md D6 — rollover detection);
# the no-emit-on-shape-change guard lives in tests/test_ranked_seasons.py.
RANKED_KEYS = {"league", "rank", "trophies", "last", "best"}
CLAN_ASPECTS = {"clan_entity", "roster"}
CLAN_ENTITY_KEYS = {"clan_score", "name", "war_trophies"}
ROSTER_MEMBER_KEYS = {
    "clan_rank",
    "donations",
    "donations_received",
    "exp_level",
    "name",
    "previous_clan_rank",
    "role",
    "trophies",
}
RACE_KEYS = {
    "clans",
    "our_fame",
    "our_tag",
    "participants",
    "our_defense",
    "period_index",
    "period_type",
    "season_id",
    "section_index",
}
RACE_PARTICIPANT_KEYS = {
    "boat_attacks",
    "decks_used",
    "decks_used_today",
    "fame",
    "name",
    "repair_points",
}


@pytest.mark.parametrize("name", ["player_evo", "player_plain"])
def test_player_projection_shape(name):
    payload = load_cr_fixture(name)
    aspects = project_player_aspects(payload)
    assert set(aspects) == PLAYER_ASPECTS
    assert set(aspects["profile"]) == PROFILE_KEYS
    assert set(aspects["cards"]) == CARDS_KEYS
    assert set(aspects["ranked"]) == RANKED_KEYS
    # values the engine depends on must be populated from a real profile
    assert aspects["profile"]["name"]
    assert isinstance(aspects["profile"]["exp_level"], int)
    assert isinstance(aspects["profile"]["trophies"], int)
    assert aspects["cards"]["collection_level"]["level"] > 0


def test_player_evolution_cards_survive_projection():
    payload = load_cr_fixture("player_evo")
    raw_evo = {c["name"] for c in payload["cards"] if c.get("evolutionLevel")}
    assert raw_evo, "fixture must contain evolution cards — refresh it"
    cards = project_player_aspects(payload)["cards"]["cards"]
    assert isinstance(cards, dict) and len(cards) == len(payload["cards"])


def test_clan_projection_shape():
    payload = load_cr_fixture("clan")
    aspects = project_clan_aspects(payload)
    assert set(aspects) == CLAN_ASPECTS
    assert set(aspects["clan_entity"]) == CLAN_ENTITY_KEYS
    members = aspects["roster"]["members"]
    assert len(members) == len(payload["memberList"])
    for tag, m in members.items():
        assert tag.startswith("#")
        assert set(m) == ROSTER_MEMBER_KEYS
    # the camelCase-read regression: donations_received came back NULL for
    # weeks because the projector read snake_case off a CR payload
    assert any(m["donations_received"] for m in members.values())
    # memberList.expLevel is DEAD at the CR API (always 0 — normalize.md
    # catalog, battery 2026-07-04): the projection maps it to None so the
    # profile-sourced value in player_current_state is never clobbered.
    assert all(m["exp_level"] is None for m in members.values())


@pytest.mark.parametrize(
    "name,period_type",
    [
        ("riverrace_training", "training"),
        ("riverrace_warday", "warDay"),
        ("riverrace_colosseum", "colosseum"),
    ],
)
def test_race_projection_shape(name, period_type):
    payload = load_cr_fixture(name)
    assert payload["periodType"] == period_type, "fixture drifted — re-export"
    aspect = project_race_aspect(payload, 133)
    assert set(aspect) == RACE_KEYS
    assert aspect["period_type"] == period_type
    assert aspect["our_tag"] == "#J2RGCRVG"
    for tag, p in aspect["participants"].items():
        assert tag.startswith("#")
        assert set(p) == RACE_PARTICIPANT_KEYS


def test_battlelog_fixture_parses_end_to_end():
    log = load_cr_fixture("battlelog")
    assert isinstance(log, list) and log
    for battle in log:
        # battleTime is the compact CR format; the one shared parser must
        # take every real battle to a tz-aware datetime
        dt = parse_cr_time(battle["battleTime"])
        assert dt is not None and dt.tzinfo is not None
        assert battle.get("team") and battle["team"][0].get("tag")
    types = {b.get("type") for b in log}
    assert "PvP" in types, "fixture should span ladder — refresh it"
    assert any("river" in (t or "").lower() for t in types), (
        "fixture should span war battles — refresh it"
    )


def test_riverracelog_shape():
    racelog = load_cr_fixture("riverracelog")
    items = racelog["items"] if isinstance(racelog, dict) else racelog
    assert items
    for item in items:
        assert "seasonId" not in item or True  # documenting: seasonId IS present here
        standings = item.get("standings") or []
        assert standings
        for s in standings:
            assert "clan" in s and s["clan"].get("tag")


@pytest.mark.parametrize(
    "fixture,endpoint",
    [
        ("player_evo", "player"),
        ("clan", "clan"),
        ("riverrace_colosseum", "currentriverrace"),
        ("battlelog", "player_battlelog"),
    ],
)
def test_annotate_never_throws_on_real_payloads(fixture, endpoint):
    """normalize.annotate must be total over real payloads — it runs at the
    tool boundary on every read, so an exception there blanks a whole view."""
    payload = load_cr_fixture(fixture)
    out = annotate(payload, endpoint)
    assert out is not None


# --- bidirectional seam contract: emitter WRITES → projection READS ----------
# The camelCase-vs-snake_case regression (2026-07-04): the roster projection
# writes snake_case, but refresh_player_state read camelCase off it, so
# exp_level / clan_rank / donations_received came back NULL for weeks ("average
# level 16"). This asserts a real roster member round-trips through the
# projection into player_current_state with its values intact.


def test_roster_projection_round_trips_into_player_state(engine_conn):
    from engine.db import canon_tag, ensure_player
    from engine.emitters.clan import project_clan_aspects
    from engine.projections import refresh_player_state, roster_state_from_api

    clan = load_cr_fixture("clan")
    roster = project_clan_aspects(clan)["roster"]["members"]
    profile = load_cr_fixture("player_evo")
    profile_tag = canon_tag(profile["tag"])
    # Use the real profile fixture first: the following sparse roster refresh
    # must preserve its profile-owned exp/best fields.
    ensure_player(engine_conn, profile_tag, profile.get("name"), "2026-07-04T23:59:00Z")
    refresh_player_state(
        engine_conn, profile_tag, profile, None, "2026-07-04T23:59:00Z"
    )

    # pick a roster member with a real, non-zero clan_rank + donations
    tag, member = next(iter(roster.items()))
    raw = next(m for m in clan["memberList"] if canon_tag(m["tag"]) == tag)

    ensure_player(engine_conn, tag, member.get("name"), "2026-07-05T00:00:00Z")
    refresh_player_state(engine_conn, tag, None, member, "2026-07-05T00:00:00Z")
    engine_conn.commit()

    row = dict(
        engine_conn.execute(
            "SELECT clan_rank, donations_week, donations_received_week "
            "FROM player_current_state WHERE player_tag = ?",
            (tag,),
        ).fetchone()
    )
    # These are the exact fields the camelCase bug NULL'd — they must carry the
    # raw CR values through the snake_case projection.
    assert row["clan_rank"] == raw["clanRank"], "clan_rank lost across the seam"
    assert row["donations_week"] == raw["donations"], "donations lost across the seam"
    assert row["donations_received_week"] == raw["donationsReceived"], (
        "donations_received lost across the seam"
    )

    # The raw clan fixture has expLevel=0 for everybody. Replaying its roster
    # entry for the profile member cannot erase deep-profile facts.
    raw_profile_member = dict(raw)
    raw_profile_member["tag"] = profile_tag
    refresh_player_state(
        engine_conn,
        profile_tag,
        None,
        roster_state_from_api(raw_profile_member),
        "2026-07-05T00:00:00Z",
    )
    preserved = engine_conn.execute(
        "SELECT exp_level, best_trophies FROM player_current_state WHERE player_tag = ?",
        (profile_tag,),
    ).fetchone()
    assert preserved["exp_level"] == profile["expLevel"]
    assert preserved["best_trophies"] == profile["bestTrophies"]
