"""Names Elixir publishes go through the sanitizer, not the raw column.

`preferred_display_name` exists because member names are member-controlled text:
it reads the materialized `players.display_name`, folds unicode look-alikes
("²⁸" -> "28") and runs an injection guard, falling back to the tag when there
is nothing safe to say.

Several publish paths read `players.current_name` directly instead, so the
sanitizer was bypassed exactly where the name reaches other people. The sharpest
case was a single season-close payload where war_champ / rookie_mvp /
donation_champ came through the safe path while free_pass and iron_king came
through a raw `_name()` helper — one announcement, two naming conventions. And
`podium()` feeds `pol_season_podium`, which is a HARD-POST event: that name goes
to #announcements whether or not anything else reads it.

These tests seed a member whose raw name needs folding and assert each publish
path emits the safe form.
"""

from __future__ import annotations

import pytest

import db
from engine.awards import _name as award_name
from engine.chronicles import _name as chronicle_name
from engine.pol_seasons import podium

RAW = "²⁸"  # the real roster case: superscript digits
SAFE = "28"
TAG = "#SUP28"


@pytest.fixture
def name_db(tmp_path, monkeypatch):
    path = str(tmp_path / "names.db")
    original = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: original(path))
    conn = original(path)
    try:
        conn.execute(
            "INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
            "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-30', 1)"
        )
        conn.execute(
            "INSERT INTO players (player_tag, current_name, display_name, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, '2026-05-01', '2026-07-30')",
            (TAG, RAW, SAFE),
        )
        conn.execute(
            "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, join_source) "
            "VALUES (?, '#J2RGCRVG', '2026-05-01', 'test')",
            (TAG,),
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


def test_an_award_name_is_the_safe_form(name_db):
    """engine/awards._name feeds free_pass and iron_king in the season-close
    payload — the two that used the raw column while their siblings did not."""
    assert award_name(name_db, TAG) == SAFE


def test_a_chronicle_name_is_the_safe_form(name_db):
    assert chronicle_name(name_db, TAG) == SAFE


def test_the_ranked_podium_publishes_the_safe_form(name_db):
    """pol_season_podium is a hard post: this name reaches #announcements."""
    name_db.execute("INSERT OR IGNORE INTO pol_seasons (pol_season_id) VALUES ('202607')")
    name_db.execute(
        "INSERT INTO pol_season_results (pol_season_id, player_tag, league, rating, "
        "battles, wins, observed_at) "
        "VALUES ('202607', ?, 9, 5000, 20, 15, '2026-07-30T00:00:00')",
        (TAG,),
    )
    name_db.commit()

    rows = podium(name_db, "202607")

    assert rows, "the podium query returned nothing — the fixture is wrong, not the code"
    assert rows[0]["name"] == SAFE, f"the podium published a raw name: {rows[0]['name']!r}"
    assert rows[0]["tag"] == TAG


def test_an_unresolvable_member_falls_back_to_the_tag_not_a_raw_name(name_db):
    """The contract preferred_display_name keeps: when there is nothing safe to
    say, say the tag. Never invent a name and never emit the raw one."""
    assert chronicle_name(name_db, "#GHOST") == "#GHOST"
