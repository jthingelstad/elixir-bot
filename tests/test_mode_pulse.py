"""The awareness read surfaces per-mode battle activity (mode_pulse) so the brain
sees ongoing ranked / 2v2 / event grinds, not just one-off pol_promotion events.
Covers runtime/awareness/read.py:_mode_pulse (a compaction of the ~14K
get_clan_game_mode_summary down to the mode mix + slim top ranked members)."""
from __future__ import annotations

import json
from unittest.mock import patch

from runtime.awareness import read as read_mod


_SUMMARY = {
    "window_days": 7,
    "by_group": [
        {"mode_group": "ranked", "label": "Ranked", "members_active": 8,
         "battles": 254, "wins": 125, "losses": 129, "win_rate": 0.49, "trophy_delta": 540},
        {"mode_group": "two_v_two", "label": "2v2", "members_active": 14,
         "battles": 332, "wins": 161, "losses": 171, "win_rate": 0.485, "trophy_delta": 0},
    ],
    "ranked_activity": [
        # Full member-reference bloat that must be dropped in the compact view.
        {"member_ref": "Fullboat", "name": "Fullboat", "ranked_battles": 57,
         "wins": 33, "losses": 24, "win_rate": 0.579, "trophy_delta": 990,
         "max_league_seen": 5, "donation_rank_week": 4, "elder_eligible": None,
         "war_points_rank_season": 2, "tag": "#X"},
    ],
    # Extra heavy keys the compact block must NOT carry through.
    "by_game_mode": [{"x": "y"}] * 50,
    "leaderboards": {"big": "blob"},
}


def test_mode_pulse_compacts_summary_and_surfaces_ranked():
    with patch.object(read_mod.db, "get_clan_game_mode_summary", return_value=_SUMMARY):
        mp = read_mod._mode_pulse(conn=None)

    assert mp["window_days"] == 7
    modes = {m["mode"] for m in mp["mode_mix"]}
    assert "Ranked" in modes and "2v2" in modes
    # mode rows keep only the summary fields, not the raw group internals.
    ranked_mode = next(m for m in mp["mode_mix"] if m["mode"] == "Ranked")
    assert ranked_mode["members_active"] == 8 and ranked_mode["battles"] == 254

    # ranked_top is slimmed — identity + ranked stats, no member-reference bloat.
    top = mp["ranked_top"][0]
    assert top["member_ref"] == "Fullboat" and top["ranked_battles"] == 57
    assert top["league"] == 5 and top["win_rate"] == 0.579
    assert "donation_rank_week" not in top and "war_points_rank_season" not in top

    # The compact block is small (the raw summary is ~14K); no heavy keys leak.
    blob = json.dumps(mp, default=str)
    assert len(blob) < 3000
    assert "by_game_mode" not in mp and "leaderboards" not in mp


def test_mode_pulse_degrades_to_empty_shape_on_error():
    # _mode_pulse raising is caught by build_read's _load → default shape + _degraded.
    with patch.object(read_mod.db, "get_clan_game_mode_summary", side_effect=RuntimeError("boom")):
        r = read_mod.build_read()
    assert r["mode_pulse"] == {"mode_mix": [], "ranked_top": [], "window_days": read_mod._MODE_PULSE_DAYS}
    assert "mode_pulse" in r.get("_degraded", [])
