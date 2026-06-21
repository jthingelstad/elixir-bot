"""Battle telemetry ingest (§5.3 tier 1).

Battles are NOT event-sourced. They are retention-managed telemetry written
directly to a projection table in elixir-v5.db, deduped on the same identity key
as the legacy member_battle_facts unique constraint. This is the tier that proves
high-volume facts stay out of the append-only log.
"""
from __future__ import annotations

from event_core.domain.player import canon_tag

BATTLE_TELEMETRY_DDL = """
CREATE TABLE IF NOT EXISTS battle_telemetry (
    player_tag     TEXT NOT NULL,
    battle_time    TEXT NOT NULL,
    battle_type    TEXT,
    opponent_tag   TEXT,
    crowns_for     INTEGER,
    crowns_against INTEGER,
    game_mode_id   INTEGER,
    game_mode_name TEXT,
    trophy_change  INTEGER,
    event_tag      TEXT,
    observed_at    TEXT,
    PRIMARY KEY (player_tag, battle_time, battle_type, opponent_tag, crowns_for, crowns_against)
);
"""


def extract_battles(player_tag: str, battle_log: list[dict]) -> list[dict]:
    tag = canon_tag(player_tag)
    out = []
    for b in battle_log or []:
        team = b.get("team") or [{}]
        opp = b.get("opponent") or [{}]
        t0 = team[0] if team else {}
        o0 = opp[0] if opp else {}
        gm = b.get("gameMode") or {}
        out.append(
            {
                "player_tag": tag,
                "battle_time": b.get("battleTime"),
                "battle_type": b.get("type"),
                "opponent_tag": o0.get("tag"),
                "crowns_for": t0.get("crowns"),
                "crowns_against": o0.get("crowns"),
                "game_mode_id": gm.get("id"),
                "game_mode_name": gm.get("name"),
                "trophy_change": t0.get("trophyChange"),
                "event_tag": b.get("eventTag"),
            }
        )
    return out
