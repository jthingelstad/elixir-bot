"""Preferred nicknames — deterministic cleaning applied everywhere, with a
stored exceptions layer (LLM residual + leader override)."""

from __future__ import annotations

import json

import pytest

from engine.emitters import emit
from engine.emitters.clan import project_clan_aspects
from engine.nicknames import ensure_nickname, generate_nickname, needs_nickname
from storage._formatting import preferred_display_name, stored_nickname

NOW = "2026-07-06T12:00:00Z"


# --- needs_nickname: only true when callable_name can't leave a readable name


@pytest.mark.parametrize(
    "name,flagged",
    [
        ("...", True),
        ("♥♥", True),
        ("", True),
        (None, True),
        ("。。。", True),
        ("²⁸", False),
        ("Sebastián", False),
        ("Ｓｈａｆｉｔｈ Ｎｉｈａｌ♥️", False),
        ("L-Drxgo⚡", False),
        ("kiruba⚜️", False),
        ("King Thing", False),
    ],
)
def test_needs_nickname(name, flagged):
    assert needs_nickname(name) is flagged


# --- generation: deterministic residual, LLM seam, placeholder fallback


def test_generate_ellipsis_is_deterministic_no_llm():
    def boom(_):  # must NOT be called for "..."
        raise AssertionError("LLM should not fire for the ellipsis residual")

    assert generate_nickname("...", llm_fn=boom) == ("Ellipsis", "generated")


def test_generate_uses_llm_for_other_residuals():
    assert generate_nickname("♥♥", llm_fn=lambda n: "Hearts") == ("Hearts", "generated")


def test_generate_falls_back_to_placeholder_on_llm_failure():
    nick, source = generate_nickname("♜♜", llm_fn=lambda n: None)
    assert source == "placeholder" and nick


def test_generate_sanitizes_llm_output():
    # model returns quotes + emoji + overlong; sanitizer folds/trims it
    nick, source = generate_nickname("☠", llm_fn=lambda n: '  "Skull ☠"  ')
    assert nick == "Skull" and source == "generated"


# --- preferred_display_name resolution order (stored > callable_name > raw)


def _player(conn, tag, name):
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, "
        "last_seen_at) VALUES (?, ?, ?, ?)",
        (tag, name, NOW, NOW),
    )


def test_display_prefers_stored_then_callable(engine_conn):
    c = engine_conn
    _player(c, "#A", "²⁸")
    _player(c, "#B", "...")
    c.commit()
    # no stored nickname → live callable_name fold
    assert preferred_display_name(c, "#A") == "28"
    # residual with a stored nickname → the nickname wins
    from db import set_member_nickname

    set_member_nickname("#B", "Ellipsis", source="generated", conn=c)
    assert preferred_display_name(c, "#B") == "Ellipsis"
    # raw_name path (no DB row)
    assert preferred_display_name(c, "#Z", "Ｓｈａｆｉｔｈ Ｎｉｈａｌ♥️") == "Shafith Nihal"


# --- ensure_nickname lifecycle


def test_ensure_stores_residual_and_is_idempotent(engine_conn):
    c = engine_conn
    _player(c, "#R", "...")
    c.commit()
    assert ensure_nickname(c, "#R", "...", NOW) == "Ellipsis"
    assert stored_nickname(c, "#R") == "Ellipsis"
    # idempotent: a second sight does not regenerate (stub would raise)
    assert ensure_nickname(c, "#R", "...", NOW, llm_fn=lambda n: "X") == "Ellipsis"


def test_ensure_never_overwrites_leader_override(engine_conn):
    c = engine_conn
    _player(c, "#L", "...")
    c.commit()
    from db import set_member_nickname

    set_member_nickname("#L", "Dots", source="leader", conn=c)
    assert ensure_nickname(c, "#L", "...", NOW) == "Dots"
    assert stored_nickname(c, "#L") == "Dots"


def test_ensure_clears_generated_on_rename_to_readable(engine_conn):
    c = engine_conn
    _player(c, "#C", "...")
    c.commit()
    ensure_nickname(c, "#C", "...", NOW)
    assert stored_nickname(c, "#C") == "Ellipsis"
    # member renamed to a clean name → generated nickname dropped, callable_name takes over
    c.execute("UPDATE players SET current_name='John' WHERE player_tag='#C'")
    ensure_nickname(c, "#C", "John", NOW)
    assert stored_nickname(c, "#C") is None
    assert preferred_display_name(c, "#C") == "John"


def test_ensure_clean_name_is_noop(engine_conn):
    c = engine_conn
    _player(c, "#K", "King Thing")
    c.commit()
    assert ensure_nickname(c, "#K", "King Thing", NOW) is None
    assert stored_nickname(c, "#K") is None


# --- first-sight through the roster emitter (end to end, no LLM for "...")


def _roster(members):
    return {
        "tag": "#J2RGCRVG",
        "name": "POAP KINGS",
        "clanScore": 60000,
        "clanWarTrophies": 900,
        "memberList": [
            {"tag": t, "name": n, "role": "member", "trophies": 5000, "donations": 0}
            for t, n in members
        ],
    }


def _emit_roster(conn, members, at):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, "
        "last_seen_at, is_home) VALUES ('#J2RGCRVG','POAP KINGS','2026-02-04',?,1)",
        (at,),
    )
    return emit(
        conn,
        "clan",
        "#J2RGCRVG",
        "roster",
        project_clan_aspects(_roster(members))["roster"],
        at,
    )


def test_residual_member_named_before_member_joined_posts(engine_conn):
    c = engine_conn
    # first roster is the silent baseline; the join is the event path
    _emit_roster(c, [("#A", "Al")], "2026-07-06T10:00:00Z")
    _emit_roster(c, [("#A", "Al"), ("#DOTS", "...")], "2026-07-06T10:10:00Z")
    # nickname persisted at first-sight, before the member_joined payload is built
    assert stored_nickname(c, "#DOTS") == "Ellipsis"
    payload = json.loads(
        c.execute(
            "SELECT payload_json FROM clan_events WHERE event_type='member_joined' "
            "AND subject_tag='#DOTS'"
        ).fetchone()["payload_json"]
    )
    assert payload["name"] == "Ellipsis"
