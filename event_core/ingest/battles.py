"""Battle telemetry ingest (§5.3 tier 1).

Battles are NOT event-sourced. They are retention-managed telemetry written
directly to a projection table in elixir-v5.db, deduped on the same identity key
as the legacy member_battle_facts unique constraint. This is the tier that proves
high-volume facts stay out of the append-only log.

Mode classification reuses the canonical storage.game_modes.classify_battle_mode
(pure, no DB) to guarantee parity; the outcome resolver mirrors
storage.player._resolve_battle_outcome. Both should re-home into event_core when
storage/ is decommissioned (Phase 4).
"""
from __future__ import annotations

from storage.game_modes import classify_battle_mode

from event_core.domain.player import canon_tag

_COMPETITIVE = {"ladder", "ranked", "war", "special_event", "tournament", "two_v_two"}


def _resolve_outcome(battle: dict, team0: dict, opp0: dict) -> str | None:
    boat = battle.get("boatBattleWon")
    if isinstance(boat, bool):
        return "W" if boat else "L"
    tc = team0.get("trophyChange") if team0 else None
    if isinstance(tc, (int, float)):
        return "W" if tc > 0 else ("L" if tc < 0 else "D")
    cf = team0.get("crowns") if team0 else None
    ca = opp0.get("crowns") if opp0 else None
    if cf is None or ca is None:
        return None
    return "W" if cf > ca else ("L" if cf < ca else "D")


def extract_battles(player_tag: str, battle_log: list[dict]) -> list[dict]:
    tag = canon_tag(player_tag)
    out = []
    for b in battle_log or []:
        team = b.get("team") or [{}]
        opp = b.get("opponent") or [{}]
        t0 = team[0] if team else {}
        o0 = opp[0] if opp else {}
        gm = b.get("gameMode") or {}
        arena = b.get("arena") or {}
        mode_group = classify_battle_mode(
            battle_type=b.get("type"),
            game_mode_id=gm.get("id"),
            game_mode_name=gm.get("name"),
            deck_selection=b.get("deckSelection"),
            event_tag=b.get("eventTag"),
            tournament_tag=b.get("tournamentTag"),
            is_hosted_match=b.get("isHostedMatch"),
            team_size=len(b.get("team") or []),
            opponent_size=len(b.get("opponent") or []),
        )
        out.append(
            {
                "player_tag": tag,
                "battle_time": b.get("battleTime"),
                # Identity (PRIMARY KEY) columns must be NON-NULL: SQLite treats
                # NULLs in a PK as distinct, so a NULL opponent_tag/crowns (boat
                # battles, PvE) would defeat INSERT OR IGNORE and re-insert every
                # poll. Coalesce to stable sentinels so dedup works.
                "battle_type": b.get("type") or "unknown",
                "opponent_tag": o0.get("tag") or "",
                "crowns_for": t0.get("crowns") if t0.get("crowns") is not None else -1,
                "crowns_against": o0.get("crowns") if o0.get("crowns") is not None else -1,
                "game_mode_id": gm.get("id"),
                "game_mode_name": gm.get("name"),
                "mode_group": mode_group,
                "outcome": _resolve_outcome(b, t0, o0),
                "is_war": int(mode_group == "war"),
                "is_ladder": int(mode_group == "ladder"),
                "is_ranked": int(mode_group == "ranked"),
                "is_competitive": int(mode_group in _COMPETITIVE),
                "is_special_event": int(mode_group == "special_event"),
                "trophy_change": t0.get("trophyChange"),
                "starting_trophies": t0.get("startingTrophies"),
                "deck_selection": b.get("deckSelection"),
                "arena_id": arena.get("id") if isinstance(arena, dict) else None,
                "arena_name": arena.get("name") if isinstance(arena, dict) else None,
                "league_number": b.get("leagueNumber"),
                "is_hosted_match": int(b["isHostedMatch"]) if isinstance(b.get("isHostedMatch"), bool) else None,
                "tournament_tag": b.get("tournamentTag"),
                "event_tag": b.get("eventTag"),
            }
        )
    return out


# Column order for the telemetry table / inserts (identity first).
BATTLE_COLUMNS = (
    "player_tag", "battle_time", "battle_type", "opponent_tag", "crowns_for",
    "crowns_against", "game_mode_id", "game_mode_name", "mode_group", "outcome",
    "is_war", "is_ladder", "is_ranked", "is_competitive", "is_special_event",
    "trophy_change", "starting_trophies", "deck_selection", "arena_id",
    "arena_name", "league_number", "is_hosted_match", "tournament_tag", "event_tag",
)

_INT_COLS = {
    "crowns_for", "crowns_against", "game_mode_id", "is_war", "is_ladder",
    "is_ranked", "is_competitive", "is_special_event", "trophy_change",
    "starting_trophies", "arena_id", "league_number", "is_hosted_match",
}

_BATTLE_DDL_COLS = ",\n    ".join(
    f"{c} {'INTEGER' if c in _INT_COLS else 'TEXT'}" for c in BATTLE_COLUMNS
)

BATTLE_TELEMETRY_DDL = f"""
CREATE TABLE IF NOT EXISTS battle_telemetry (
    {_BATTLE_DDL_COLS},
    observed_at TEXT,
    PRIMARY KEY (player_tag, battle_time, battle_type, opponent_tag, crowns_for, crowns_against)
);
"""

_BATTLE_INSERT = (
    f"INSERT OR IGNORE INTO battle_telemetry({','.join(BATTLE_COLUMNS)},observed_at) "
    f"VALUES({','.join('?' for _ in BATTLE_COLUMNS)},?)"
)


def write_battle_telemetry(conn, player_tag: str, battle_log: list[dict], observed_at: str) -> int:
    """Idempotently write a player's battlelog into battle_telemetry (tier 1).

    The single battle-ingest path shared by backfill and live ingest. Returns the
    number of newly-inserted rows (dedup via the identity primary key).
    """
    conn.execute(BATTLE_TELEMETRY_DDL)
    inserted = 0
    for bt in extract_battles(player_tag, battle_log):
        cur = conn.execute(_BATTLE_INSERT, [bt[c] for c in BATTLE_COLUMNS] + [observed_at])
        inserted += cur.rowcount
    conn.commit()
    return inserted
