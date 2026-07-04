"""Ranked seasons + playstyle profiles (ranked-and-profiles.md, D1–D7).

Covers: the D6 shape-change guard (extending the ranked aspect must not emit
from shape alone), the observed rollover lifecycle (close → results snapshot →
podium intent → pol_champ awards → chronicle → idempotence), season-id math
(first-Monday), the era-aware league names, the UC constant fix (7-league
scheme), teammate extraction (D3), and the deterministic identity labels (D4).
"""
from __future__ import annotations

import json
from datetime import date

import db
from engine import pol_seasons, profiles
from engine.emitters import emit
from engine.ingest import extract_battles
from engine.normalize import ranked_league_name

AT_JUNE = "2026-06-20T10:00:00Z"
AT_CLOSE = "2026-07-06T10:05:00Z"


def _player_payload(tag_suffix: str, name: str, *, league, trophies, rank=None,
                    last=None, best=None) -> dict:
    p = {
        "tag": f"#{tag_suffix}", "name": name, "expLevel": 50, "wins": 100,
        "trophies": 9000, "bestTrophies": 9100,
        "arena": {"id": 54000015, "name": "Legendary Arena"},
        "currentPathOfLegendSeasonResult": {
            "leagueNumber": league, "trophies": trophies, "rank": rank},
    }
    if last is not None:
        p["lastPathOfLegendSeasonResult"] = last
    if best is not None:
        p["bestPathOfLegendSeasonResult"] = best
    return p


def _seed_member(conn, tag: str, name: str):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-06', 1)")
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, '2026-03-01', '2026-07-06')", (tag, name))
    conn.execute(
        "INSERT OR IGNORE INTO clan_memberships (player_tag, joined_at, join_source) "
        "VALUES (?, '2026-03-01', 'test')", (tag,))


def _emit_player(conn, tag_suffix: str, name: str, at: str, **kw) -> int:
    from engine.emitters.player import project_player_aspects

    payload = _player_payload(tag_suffix, name, **kw)
    n = 0
    for aspect, ap in project_player_aspects(payload).items():
        n += emit(conn, "player", f"#{tag_suffix}", aspect, ap, at)
    return n


# ----------------------------------------------------------- season id math

def test_season_id_math():
    assert pol_seasons.first_monday(2026, 7) == date(2026, 7, 6)
    assert pol_seasons.first_monday(2026, 6) == date(2026, 6, 1)
    # July 4 is before July's first Monday → the June season is still open
    assert pol_seasons.season_id_for(date(2026, 7, 4)) == "2026-06"
    # July 6 (the reset day) onward → the July season
    assert pol_seasons.season_id_for(date(2026, 7, 6)) == "2026-07"
    assert pol_seasons.previous_season_id("2026-07") == "2026-06"
    assert pol_seasons.previous_season_id("2026-01") == "2025-12"


# ------------------------------------------------------------ league naming

def test_ranked_league_names_are_era_aware():
    assert ranked_league_name(7) == "Ultimate Champion"
    assert ranked_league_name(1) == "Master 1"
    assert ranked_league_name(10) is None  # no league 10 in the current scheme
    assert ranked_league_name(10, legacy=True) == "Ultimate Champion (Path of Legends era)"
    assert ranked_league_name(None) is None


def test_ultimate_champion_fires_at_league_7():
    """The carried constant said 10 (old PoL scale) — under the 7-league
    rework the event could never fire."""
    conn = db.get_connection()
    try:
        _seed_member(conn, "#UC1", "Climber")
        _emit_player(conn, "UC1", "Climber", AT_JUNE,
                     league=6, trophies=1500, last={"leagueNumber": 5, "trophies": 1400})
        _emit_player(conn, "UC1", "Climber", "2026-06-21T10:00:00Z",
                     league=7, trophies=1600, last={"leagueNumber": 5, "trophies": 1400})
        events = [r["event_type"] for r in conn.execute(
            "SELECT event_type FROM player_events WHERE player_tag = '#UC1'")]
        assert "pol_promotion" in events
        assert "ultimate_champion_reached" in events
    finally:
        conn.close()


# --------------------------------------------------- D6 shape-change guard

def test_shape_change_alone_emits_nothing():
    """A baseline written before the aspect carried last/best must not emit
    (no promotion, no rollover) when only the SHAPE changes."""
    conn = db.get_connection()
    try:
        _seed_member(conn, "#SHAPE", "Shapely")
        # old-shape baseline: flat ranked dict, no last/best keys
        from engine.baselines import set_baseline

        set_baseline(conn, "player", "#SHAPE", "ranked",
                     {"league": 7, "rank": None, "trophies": 1867}, AT_JUNE)
        n = 0
        from engine.emitters.player import project_player_aspects

        payload = _player_payload("SHAPE", "Shapely", league=7, trophies=1867,
                                  last={"leagueNumber": 7, "trophies": 1619},
                                  best={"leagueNumber": 10, "trophies": 1896})
        aspects = project_player_aspects(payload)
        n = emit(conn, "player", "#SHAPE", "ranked", aspects["ranked"], AT_CLOSE)
        assert n == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM player_events WHERE player_tag = '#SHAPE'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pol_seasons WHERE closed = 1"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# -------------------------------------------------------- rollover lifecycle

def _seed_rollups(conn, tag: str, battles: int, wins: int, mode="ranked",
                  dates=("2026-06-10", "2026-06-20")):
    for i, d in enumerate(dates):
        b = battles // len(dates) + (battles % len(dates) if i == 0 else 0)
        w = wins // len(dates) + (wins % len(dates) if i == 0 else 0)
        conn.execute(
            """INSERT INTO player_daily_battle_rollups
                   (player_tag, battle_date, mode_group, battles, wins, losses,
                    last_aggregated_at)
               VALUES (?, ?, ?, ?, ?, ?, '2026-07-01T00:00:00Z')""",
            (tag, d, mode, b, w, b - w))


def test_rollover_closes_season_and_posts_podium():
    conn = db.get_connection()
    try:
        for suffix, name in (("ATT", "Atternam"), ("OLL", "OllieTurtle"), ("MAS", "Masterling")):
            _seed_member(conn, f"#{suffix}", name)
        # June baselines (extended shape) — seeds the open 2026-06 season
        _emit_player(conn, "ATT", "Atternam", "2026-06-19T10:00:00Z",
                     league=7, trophies=1900, last={"leagueNumber": 6, "trophies": 1500})
        _emit_player(conn, "ATT", "Atternam", AT_JUNE,
                     league=7, trophies=1982, last={"leagueNumber": 6, "trophies": 1500})
        _emit_player(conn, "OLL", "OllieTurtle", "2026-06-19T10:00:00Z",
                     league=7, trophies=1800, last={"leagueNumber": 7, "trophies": 1619})
        _emit_player(conn, "OLL", "OllieTurtle", AT_JUNE,
                     league=7, trophies=1867, last={"leagueNumber": 7, "trophies": 1619})
        _emit_player(conn, "MAS", "Masterling", "2026-06-19T10:00:00Z",
                     league=2, trophies=None, last={"leagueNumber": 1, "trophies": None})
        _emit_player(conn, "MAS", "Masterling", AT_JUNE,
                     league=3, trophies=None, last={"leagueNumber": 1, "trophies": None})
        assert conn.execute(
            "SELECT COUNT(*) FROM pol_seasons WHERE pol_season_id = '2026-06' AND closed = 0"
        ).fetchone()[0] == 1
        _seed_rollups(conn, "#ATT", 6, 6)
        _seed_rollups(conn, "#OLL", 28, 16)
        _seed_rollups(conn, "#MAS", 12, 5)

        # Monday July 6: Atternam's profile shows the reset first
        n = _emit_player(conn, "ATT", "Atternam", AT_CLOSE,
                         league=1, trophies=None,
                         last={"leagueNumber": 7, "trophies": 1982},
                         best={"leagueNumber": 7, "trophies": 1982})
        assert n >= 1  # pol_season_closed event

        season = conn.execute(
            "SELECT * FROM pol_seasons WHERE pol_season_id = '2026-06'").fetchone()
        assert season["closed"] == 1 and season["ended_at"] == AT_CLOSE
        # new season born
        assert conn.execute(
            "SELECT COUNT(*) FROM pol_seasons WHERE pol_season_id = '2026-07' AND closed = 0"
        ).fetchone()[0] == 1

        # results: all three snapshotted; Atternam's own row authoritative
        results = {r["player_tag"]: dict(r) for r in conn.execute(
            "SELECT * FROM pol_season_results WHERE pol_season_id = '2026-06'")}
        assert set(results) == {"#ATT", "#OLL", "#MAS"}
        assert results["#ATT"]["league"] == 7 and results["#ATT"]["rating"] == 1982
        assert results["#OLL"]["league"] == 7 and results["#OLL"]["rating"] == 1867
        assert results["#ATT"]["battles"] == 6 and results["#ATT"]["wins"] == 6

        # podium order: league desc, rating desc
        pod = pol_seasons.podium(conn, "2026-06")
        assert [e["tag"] for e in pod] == ["#ATT", "#OLL", "#MAS"]
        assert pod[0]["league_name"] == "Ultimate Champion"

        # ONE podium intent to clan-events
        intents = conn.execute(
            "SELECT * FROM communication_intents WHERE intent_type = 'clan:pol_season_podium'"
        ).fetchall()
        assert len(intents) == 1 and intents[0]["lane"] == "clan-events"
        payload = json.loads(intents[0]["payload_json"])
        assert payload["podium"][0]["name"] == "Atternam"

        # pol_champ awards under the sortable integer season id
        champs = conn.execute(
            "SELECT player_tag, rank FROM awards WHERE award_type = 'pol_champ' "
            "AND season_id = 202606 ORDER BY rank").fetchall()
        assert [(r["player_tag"], r["rank"]) for r in champs] == [
            ("#ATT", 1), ("#OLL", 2), ("#MAS", 3)]

        # chronicle memory written + tagged
        chron = conn.execute(
            """SELECT m.body FROM memories m JOIN memory_tags t ON t.memory_id = m.memory_id
               WHERE t.tag = 'ranked-season-2026-06'""").fetchone()
        assert chron and "Atternam" in chron["body"] and "Ultimate Champion" in chron["body"]

        # OllieTurtle's later rollover: own row upserted, nothing else re-fires
        _emit_player(conn, "OLL", "OllieTurtle", "2026-07-06T11:00:00Z",
                     league=1, trophies=None,
                     last={"leagueNumber": 7, "trophies": 1867})
        assert conn.execute(
            "SELECT COUNT(*) FROM communication_intents WHERE intent_type = 'clan:pol_season_podium'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM awards WHERE award_type = 'pol_champ'"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM player_events WHERE event_type = 'pol_season_closed'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


# ------------------------------------------------------------- D3 teammates

def test_teammate_extracted_from_2v2():
    log = [{
        "type": "clanMate2v2",
        "battleTime": "20260704T120000.000Z",
        "gameMode": {"id": 72000006, "name": "TeamVsTeam"},
        "team": [
            {"tag": "#ME", "crowns": 2, "cards": []},
            {"tag": "#BUDDY", "crowns": 2, "cards": []},
        ],
        "opponent": [{"tag": "#OPP1", "crowns": 1}, {"tag": "#OPP2", "crowns": 1}],
    }]
    rows = extract_battles("#ME", log)
    assert rows[0]["teammate_tag"] == "#BUDDY"
    solo = [{"type": "PvP", "battleTime": "20260704T120100.000Z",
             "gameMode": {"id": 72000000, "name": "Ladder"},
             "team": [{"tag": "#ME", "crowns": 3, "trophyChange": 30}],
             "opponent": [{"tag": "#OPP", "crowns": 0}]}]
    assert extract_battles("#ME", solo)[0]["teammate_tag"] is None


# --------------------------------------------------------- D4 identity labels

def _profile_conn_with(conn, tag: str, mode_battles: dict[str, tuple[int, int]]):
    _seed_member(conn, tag, tag.strip("#"))
    for mode, (battles, wins) in mode_battles.items():
        _seed_rollups(conn, tag, battles, wins, mode=mode,
                      dates=("2026-06-25", "2026-06-28"))


def test_identity_labels_at_thresholds():
    conn = db.get_connection()
    try:
        today = "2026-07-04"
        # Ranked grinder: 20 of 40 ranked (50% share, ≥12 battles)
        _profile_conn_with(conn, "#GRIND", {"ranked": (20, 12), "ladder": (20, 10)})
        p = profiles.player_mode_profile(conn, "#GRIND", today=today)
        assert p["identity"] == "Ranked grinder"
        # threshold edge: largest mode at 34% share → all-rounder
        _profile_conn_with(conn, "#EDGE", {"ladder": (34, 20), "war": (33, 15),
                                           "ranked": (33, 15)})
        p = profiles.player_mode_profile(conn, "#EDGE", today=today)
        assert p["identity"] == "all-rounder"
        # battles floor: 50% share but only 6 battles in the top mode, 11 total
        _profile_conn_with(conn, "#FEW", {"two_v_two": (6, 3), "ladder": (5, 2)})
        p = profiles.player_mode_profile(conn, "#FEW", today=today)
        assert p["identity"] == "all-rounder"
        # quiet: under 8 total
        _profile_conn_with(conn, "#QUIET", {"ladder": (5, 3)})
        p = profiles.player_mode_profile(conn, "#QUIET", today=today)
        assert p["identity"] == "quiet"
        # friendlies never drive identity
        _profile_conn_with(conn, "#SPAR", {"friendly": (30, 15), "ladder": (10, 5)})
        p = profiles.player_mode_profile(conn, "#SPAR", today=today)
        assert p["identity"] == "all-rounder"
    finally:
        conn.close()


def test_playstyle_line_is_grounded():
    conn = db.get_connection()
    try:
        _profile_conn_with(conn, "#LINE", {"two_v_two": (31, 20), "ladder": (9, 4)})
        p = profiles.player_mode_profile(conn, "#LINE", today="2026-07-04")
        line = profiles.playstyle_line(p)
        assert "31" in line and "40" in line and "2v2" in line
    finally:
        conn.close()
