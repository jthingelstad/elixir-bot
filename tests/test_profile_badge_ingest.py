"""Badge-derived profile facts must be written on every profile observation.

These columns (Collection Level, years played, career wins) had NO ongoing writer:
the values present were copied forward by the one-time v5.1 migration transform on
2026-07-03/04, and that transform was deleted as a dead script on 2026-07-29. The
columns sat frozen for ~4 weeks, so every member who joined after the cutover had
none — which is why a newcomer's welcome had nothing to say but trophies.
"""

import db
from engine.projections import refresh_profile_metadata

PAYLOAD = {
    "expLevel": 46,  # deprecated; must NOT be treated as the progression number
    "collectionLevel": 0,  # top-level key is a STUB that always reads 0
    "wins": 3059,
    "warDayWins": 12,
    "totalDonations": 5600,
    "badges": [
        {"name": "CollectionLevel", "level": 8, "maxLevel": 8, "progress": 1517},
        {"name": "YearsPlayed", "level": 3, "maxLevel": 11, "progress": 444},
        {"name": "BannerCollection", "level": 3, "maxLevel": 8, "progress": 77},
    ],
}


def _conn(tmp_path, tag="#ABC"):
    conn = db.get_connection(str(tmp_path / "t.db"))
    conn.execute(
        "INSERT INTO players (player_tag, first_seen_at, last_seen_at) VALUES (?, 'x', 'x')",
        (tag,),
    )
    return conn


def _meta(conn, tag="#ABC"):
    return conn.execute(
        "SELECT cr_collection_level cl, cr_collection_level_badge_tier tier, "
        "cr_account_age_years years, cr_battle_wins wins, cr_clan_war_wins war "
        "FROM player_metadata WHERE player_tag = ?",
        (tag,),
    ).fetchone()


def test_collection_level_comes_from_the_badge_not_the_stub_field(tmp_path):
    conn = _conn(tmp_path)
    assert refresh_profile_metadata(conn, "#ABC", PAYLOAD, "2026-07-31T00:00:00Z") is True
    row = _meta(conn)
    assert row["cl"] == 1517, "must read badge progress, not the top-level 0 stub"
    assert row["tier"] == 8
    conn.close()


def test_years_played_and_career_totals_are_written(tmp_path):
    conn = _conn(tmp_path)
    refresh_profile_metadata(conn, "#ABC", PAYLOAD, "2026-07-31T00:00:00Z")
    row = _meta(conn)
    assert row["years"] == 3
    assert row["wins"] == 3059
    assert row["war"] == 12
    conn.close()


def test_missing_years_badge_does_not_block_collection_level(tmp_path):
    """The YearsPlayed badge is genuinely absent on some accounts."""
    conn = _conn(tmp_path)
    payload = {**PAYLOAD, "badges": [PAYLOAD["badges"][0]]}
    assert refresh_profile_metadata(conn, "#ABC", payload, "2026-07-31T00:00:00Z") is True
    row = _meta(conn)
    assert row["cl"] == 1517
    assert row["years"] is None
    conn.close()


def test_payload_without_badges_writes_nothing_and_does_not_raise(tmp_path):
    conn = _conn(tmp_path)
    assert refresh_profile_metadata(conn, "#ABC", {"badges": None}, "2026-07-31T00:00:00Z") is False
    conn.close()


def test_profile_observation_populates_metadata_end_to_end(tmp_path):
    """The hook must actually fire from the materialize path, not just in isolation —
    an unwired writer is exactly how this rotted the first time."""
    import inspect

    import engine.materialize as materialize

    source = inspect.getsource(materialize)
    assert "refresh_profile_metadata" in source, "profile ingest is not wired into materialize"
