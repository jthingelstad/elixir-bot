"""Coverage-matrix smoke (schema.md §9): every ported tool aspect returns a
well-shaped, non-error result against a seeded v5.1 fixture DB."""
from __future__ import annotations

import json

import pytest

from agent import tool_exec

NOW = "2026-07-01T12:00:00Z"


@pytest.fixture()
def seeded_db(engine_conn, _isolate_default_sqlite_db):
    c = engine_conn
    c.execute("INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home)"
              " VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)", (NOW,))
    c.execute("INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at)"
              " VALUES ('#A', 'Alice', '2026-03-01', ?)", (NOW,))
    c.execute("INSERT INTO player_aliases (player_tag, alias, source, observed_at)"
              " VALUES ('#A', 'Alice', 'roster', ?)", (NOW,))
    c.execute("INSERT INTO clan_memberships (player_tag, joined_at, join_source)"
              " VALUES ('#A', '2026-03-01', 'test')")
    c.execute("INSERT INTO player_current_state (player_tag, observed_at, role,"
              " exp_level, trophies, best_trophies, donations_week, arena_id, arena_name)"
              " VALUES ('#A', ?, 'member', 45, 5500, 6000, 120, 54000012, 'Spooky Town')",
              (NOW,))
    c.execute("INSERT INTO player_metadata (player_tag) VALUES ('#A')")
    c.execute("INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at,"
              " battle_type, opponent_tag, crowns_for, crowns_against, mode_group,"
              " outcome, is_war, trophy_change, deck_json)"
              " VALUES ('#A:20260630T110000.000Z:#OPP', '#A', '20260630T110000.000Z', ?,"
              " 'PvP', '#OPP', 2, 0, 'ladder', 'W', 0, 30, '[]')", (NOW,))
    c.execute("INSERT INTO player_recent_form (player_tag, scope, computed_at,"
              " sample_size, wins, losses, draws, win_rate)"
              " VALUES ('#A', 'overall_10', ?, 1, 1, 0, 0, 1.0)", (NOW,))
    c.execute("INSERT INTO player_daily_metrics (player_tag, metric_date, trophies,"
              " donations_week) VALUES ('#A', '2026-06-30', 5500, 120)")
    c.execute("INSERT INTO member_management (player_tag, computed_at, week_anchor,"
              " tenure_days, role, kick_state, promote_state)"
              " VALUES ('#A', ?, '2026-06-29', 120, 'member', 'none', 'building')",
              (NOW,))
    c.execute("INSERT INTO war_seasons (season_id, started_at, ended_at, final_rank,"
              " weeks, war_champ_tag, free_pass_tag)"
              " VALUES (132, '2026-06-01', '2026-06-28', 1, 4, '#A', '#A')")
    c.execute("INSERT INTO war_weeks (season_id, section_index, our_rank, our_fame)"
              " VALUES (132, 0, 1, 10000)")
    c.execute("INSERT INTO war_participation (season_id, section_index, player_tag,"
              " fame, decks_used, observed_at) VALUES (132, 0, '#A', 3000, 16, ?)", (NOW,))
    c.execute("INSERT INTO war_attendance_days (season_id, section_index, war_day_index,"
              " player_tag, decks_used, decks_available, observed_at)"
              " VALUES (132, 0, 0, '#A', 4, 4, ?)", (NOW,))
    c.execute("INSERT INTO awards (award_type, season_id, player_tag, rank, awarded_at)"
              " VALUES ('war_champ', 132, '#A', 1, ?)", (NOW,))
    c.execute("INSERT INTO awards (award_type, season_id, player_tag, rank, awarded_at)"
              " VALUES ('free_pass', 132, '#A', 1, ?)", (NOW,))
    c.execute("INSERT INTO player_events (dedup_key, event_type, player_tag, observed_at,"
              " payload_json, created_at) VALUES ('level_up:#A:45', 'level_up', '#A', ?,"
              " '{}', ?)", (NOW, NOW))
    c.execute("INSERT INTO state_baselines (entity_kind, entity_tag, aspect, payload_json,"
              " payload_hash, observed_at) VALUES ('riverrace', '#J2RGCRVG', 'race',"
              " ?, 'h', ?)",
              (json.dumps({"season_id": 133, "section_index": 0, "period_index": 3,
                           "period_type": "warDay", "our_tag": "#J2RGCRVG",
                           "our_fame": 1000, "clans": {}, "participants": {}}), NOW))
    c.commit()
    return c


def _run(name, args):
    result = tool_exec._execute_tool(name, args)
    assert result is not None
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    assert "Traceback" not in text
    assert "Tool execution error" not in text, f"{name}({args}) errored: {text[:300]}"
    return text


CASES = [
    ("resolve_member", {"query": "Alice"}),
    ("get_member", {"member": "#A", "include": ["profile"]}),
    ("get_member", {"member": "#A", "include": ["form"]}),
    ("get_member", {"member": "#A", "include": ["battles"]}),
    ("get_member", {"member": "#A", "include": ["trend"]}),
    ("get_member", {"member": "#A", "include": ["history"]}),
    ("get_member", {"member": "#A", "include": ["awards"]}),
    ("get_member_war_detail", {"member": "#A"}),
    ("get_clan_roster", {"view": "list"}),
    ("get_clan_roster", {"view": "recent_joins"}),
    ("get_clan_roster", {"view": "role_changes"}),
    ("get_clan_health", {"view": "at_risk"}),
    ("get_clan_health", {"view": "promotion_candidates"}),
    ("get_clan_health", {"view": "trophy_drops"}),
    ("get_war_season", {"view": "summary", "season_id": 132}),
    ("get_war_season", {"view": "standings", "season_id": 132}),
    ("get_war_season", {"view": "perfect_attendance", "season_id": 132}),
    ("get_awards", {"view": "list"}),
    ("get_awards", {"view": "leaderboard"}),
    ("get_clan_game_modes", {}),
    ("get_elixir_state", {"view": "event_summary"}),
    ("get_elixir_state", {"view": "recent_events"}),
    ("get_river_race", {"view": "standings"}),
]


@pytest.mark.parametrize("name,args", CASES, ids=[f"{n}:{json.dumps(a)[:40]}" for n, a in CASES])
def test_tool_aspect_smoke(seeded_db, name, args):
    _run(name, args)


def test_get_clan_voyage_removed():
    from agent import tool_defs

    names = json.dumps(getattr(tool_defs, "TOOLS", []) or
                       [t for t in dir(tool_defs)])
    assert "get_clan_voyage" not in names
