"""Golden-pair emitter tests (migration.md Phase 8): (previous_state,
observed_state) → exact event list. Spec: events.md §3–§4, architecture §8."""
from __future__ import annotations

import json

from engine.emitters import emit
from engine.emitters.clan import emit_calendar, project_clan_aspects
from engine.emitters.player import project_player_aspects

NOW = "2026-07-01T12:00:00Z"
LATER = "2026-07-01T13:00:00Z"
TAG = "#AAA111"


def _profile(level=44, wins=4990, best=6890, badges=None, arena=(54000012, "Spooky Town")):
    return {
        "tag": TAG, "name": "Alice", "expLevel": level, "wins": wins,
        "bestTrophies": best, "trophies": best - 100,
        "arena": {"id": arena[0], "name": arena[1]},
        "badges": [{"name": n, "level": lv} for n, lv in (badges or {}).items()],
    }


def _events(conn, table="player_events"):
    return [
        (r["event_type"], r["dedup_key"])
        for r in conn.execute(f"SELECT event_type, dedup_key FROM {table} ORDER BY event_id")
    ]


def _emit_profile(conn, payload, at):
    return emit(conn, "player", TAG, "profile",
                project_player_aspects(payload)["profile"], at)


def test_first_sight_emits_nothing(engine_conn):
    assert _emit_profile(engine_conn, _profile(), NOW) == 0
    assert _events(engine_conn) == []
    row = engine_conn.execute(
        "SELECT observed_at, prev_observed_at FROM state_baselines "
        "WHERE entity_kind='player' AND entity_tag=? AND aspect='profile'", (TAG,)
    ).fetchone()
    assert row["observed_at"] == NOW and row["prev_observed_at"] is None


def test_career_wins_golden_pair(engine_conn):
    # expLevel is retired (no level_up); career_wins_milestone is the profile-
    # aspect ladder event. Account progression now rides collection_level_
    # milestone (cards aspect) — see test_collection_level_milestone.py.
    _emit_profile(engine_conn, _profile(level=44, wins=4990), NOW)
    n = _emit_profile(engine_conn, _profile(level=45, wins=5100), LATER)
    events = _events(engine_conn)
    assert ("career_wins_milestone", f"career_wins_milestone:{TAG}:5000") in events
    assert all(e[0] != "level_up" for e in events)
    assert n == len(events)
    # timing honesty: estimated, window bounded by prev observation
    row = engine_conn.execute(
        "SELECT timing, window_start FROM player_events LIMIT 1").fetchone()
    assert row["timing"] == "estimated" and row["window_start"] == NOW


def test_unchanged_payload_emits_nothing(engine_conn):
    _emit_profile(engine_conn, _profile(), NOW)
    assert _emit_profile(engine_conn, _profile(), LATER) == 0
    assert _events(engine_conn) == []


def test_best_trophies_peak_every_100(engine_conn):
    _emit_profile(engine_conn, _profile(best=6890), NOW)
    _emit_profile(engine_conn, _profile(best=7010), LATER)
    events = _events(engine_conn)
    assert ("best_trophies_peak", f"best_trophies_peak:{TAG}:6900") in events
    assert ("best_trophies_peak", f"best_trophies_peak:{TAG}:7000") in events


def test_arena_changed_emitted_with_prev(engine_conn):
    _emit_profile(engine_conn, _profile(arena=(54000012, "Spooky Town")), NOW)
    _emit_profile(engine_conn, _profile(arena=(54000013, "Rascal's Hideout")), LATER)
    rows = [r for r in _events(engine_conn) if r[0] == "arena_changed"]
    assert rows == [("arena_changed", f"arena_changed:{TAG}:54000013")]


def test_card_unlock_and_level_milestone(engine_conn):
    def cards_payload(cards):
        return {"tag": TAG, "cards": cards, "badges": []}

    base = [{"id": 1, "name": "Knight", "rarity": "common",
             "level": 13, "maxLevel": 14}]
    emit(engine_conn, "player", TAG, "cards",
         project_player_aspects(cards_payload(base))["cards"], NOW)
    grown = base + [{"id": 2, "name": "Mighty Miner", "rarity": "champion",
                     "level": 1, "maxLevel": 4}]
    grown[0] = dict(base[0], level=14)  # display level 15... depends on scale
    emit(engine_conn, "player", TAG, "cards",
         project_player_aspects(cards_payload(grown))["cards"], LATER)
    events = _events(engine_conn)
    unlocks = [e for e in events if e[0] == "card_unlocked"]
    assert unlocks == [("card_unlocked", f"card_unlocked:{TAG}:2")]
    payload = json.loads(engine_conn.execute(
        "SELECT payload_json FROM player_events WHERE event_type='card_unlocked'"
    ).fetchone()[0])
    assert payload["rarity"] == "champion"


def _roster(members):
    return {"tag": "#J2RGCRVG", "name": "POAP KINGS", "clanScore": 60000,
            "clanWarTrophies": 900,
            "memberList": [
                {"tag": t, "name": n, "role": r, "trophies": 5000,
                 "donations": d} for t, n, r, d in members]}


def _emit_roster(conn, payload, at):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, "
        "is_home) VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)", (at,))
    return emit(conn, "clan", "#J2RGCRVG", "roster",
                project_clan_aspects(payload)["roster"], at)


def test_roster_join_leave_role_change_maintain_memberships(engine_conn):
    _emit_roster(engine_conn, _roster([("#A", "Al", "member", 0),
                                       ("#B", "Bo", "elder", 0)]), NOW)
    # First-sight roster is silent (the emitter never runs — no identity
    # upserts, no membership rows); production's initial roster state comes
    # from the T1/T5 transforms. Simulate that carried state:
    for tag, name in (("#A", "Al"), ("#B", "Bo")):
        engine_conn.execute(
            "INSERT OR IGNORE INTO players (player_tag, current_name, "
            "first_seen_at, last_seen_at) VALUES (?, ?, '2026-03-01', ?)",
            (tag, name, NOW))
        engine_conn.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
            "VALUES (?, '2026-03-01', 'transform')", (tag,))
    engine_conn.commit()
    # join #C, leave #B, promote #A
    _emit_roster(engine_conn, _roster([("#A", "Al", "elder", 0),
                                       ("#C", "Cy", "member", 0)]), LATER)
    events = _events(engine_conn, "clan_events")
    types = [e[0] for e in events]
    assert "member_joined" in types and "member_left" in types and "role_changed" in types
    open_tags = {r[0] for r in engine_conn.execute(
        "SELECT player_tag FROM clan_memberships WHERE left_at IS NULL")}
    assert open_tags == {"#A", "#C"}
    closed = engine_conn.execute(
        "SELECT left_at FROM clan_memberships WHERE player_tag='#B'").fetchone()
    assert closed["left_at"] is not None
    direction = json.loads(engine_conn.execute(
        "SELECT payload_json FROM clan_events WHERE event_type='role_changed'"
    ).fetchone()[0])["direction"]
    assert direction == "promoted"


def test_weekly_donation_leader_reset_top3(engine_conn):
    _emit_roster(engine_conn, _roster([("#A", "Al", "member", 300),
                                       ("#B", "Bo", "member", 200),
                                       ("#C", "Cy", "member", 100),
                                       ("#D", "Dy", "member", 50)]), NOW)
    # Monday reset: everyone collapses to ~0
    _emit_roster(engine_conn, _roster([("#A", "Al", "member", 0),
                                       ("#B", "Bo", "member", 0),
                                       ("#C", "Cy", "member", 0),
                                       ("#D", "Dy", "member", 0)]), LATER)
    row = engine_conn.execute(
        "SELECT payload_json FROM clan_events WHERE event_type='weekly_donation_leader'"
    ).fetchone()
    assert row is not None, "reset should emit the donation leader"
    payload = json.loads(row[0])
    leaders = payload["leaders"]
    assert [x["tag"] for x in leaders] == ["#A", "#B", "#C"]  # top-3, ordered
    assert leaders[0]["donations"] == 300


def test_calendar_birthday_and_anniversary(engine_conn):
    engine_conn.execute(
        "INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)", (NOW,))
    engine_conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES ('#A', 'Al', ?, ?)", (NOW, NOW))
    engine_conn.execute(
        "INSERT INTO player_metadata (player_tag, birth_month, birth_day) "
        "VALUES ('#A', 7, 1)")
    engine_conn.execute(
        "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
        "VALUES ('#A', '2026-04-01', 'test')")
    engine_conn.commit()
    n = emit_calendar(engine_conn, "2026-07-01")
    events = _events(engine_conn, "clan_events")
    types = [e[0] for e in events]
    assert "member_birthday" in types
    # joined 04-01 → 07-01 is 3 months → join_anniversary (months %3 == 0)
    assert "join_anniversary" in types
    assert n == len(events)
    # idempotent: same day re-run adds nothing
    assert emit_calendar(engine_conn, "2026-07-01") == 0
