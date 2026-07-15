"""Battle mirror — runtime.md §2 step 2, schema.md §5.1.

The battle stream is native (§9.1): battles mirror in as-is, dedup-keyed,
with war keys resolved from the battle's OWN battle_time against the season
calendar — never the tick-time clock (late-mirrored battles must land in
their real war day).

Extraction is the verbatim port of event_core/ingest/battles.py
(extract_battles + _resolve_outcome); mode classification stays on the
canonical storage.game_modes.classify_battle_mode (pure, survives the cut).
One addition over battle_telemetry: deck_json (schema.md §5.1 — war-deck
reconstruction stops parsing raw payloads).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from engine.clock import WarClock, resolve_war_keys
from engine.db import canon_tag
from storage.game_modes import classify_battle_mode

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


def _deck_json(t0: dict) -> str | None:
    cards = t0.get("cards") or []
    if not cards:
        return None
    deck = [
        {"id": c.get("id"), "name": c.get("name"), "level": c.get("level")}
        for c in cards
        if isinstance(c, dict)
    ]
    return json.dumps(deck, separators=(",", ":")) if deck else None


def extract_battles(player_tag: str, battle_log: list[dict]) -> list[dict]:
    """Verbatim port of event_core/ingest/battles.extract_battles, plus deck_json.

    Identity columns coalesce NULLs to stable sentinels so the dedup key is
    deterministic (boat battles / PvE have no opponent tag).
    """
    tag = canon_tag(player_tag)
    out = []
    for b in battle_log or []:
        team = b.get("team") or [{}]
        opp = b.get("opponent") or [{}]
        # Admission guarantees the subject is present.  Keep a first-player
        # fallback for direct/unit callers, but attribute crowns, trophy delta,
        # outcome, and deck to the polled player even when CR orders a 2v2 team
        # with that player second.
        t0 = next(
            (
                member
                for member in team
                if isinstance(member, dict) and canon_tag(member.get("tag")) == tag
            ),
            team[0] if team else {},
        )
        o0 = opp[0] if opp else {}
        # D3 (ranked-and-profiles.md): 2v2 carries the duo partner in the team
        # array — but CR does NOT guarantee the polled player is team[0]; the
        # partner is whichever member isn't the subject (live bug 2026-07-05:
        # team[1] recorded players as their own teammate).
        teammate = None
        if len(team) > 1:
            for tm in team:
                tm_tag = (
                    canon_tag(tm.get("tag"))
                    if isinstance(tm, dict) and tm.get("tag")
                    else None
                )
                if tm_tag and tm_tag != tag:
                    teammate = tm_tag
                    break
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
                "battle_type": b.get("type") or "unknown",
                "opponent_tag": o0.get("tag") or "",
                "crowns_for": t0.get("crowns") if t0.get("crowns") is not None else -1,
                "crowns_against": o0.get("crowns")
                if o0.get("crowns") is not None
                else -1,
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
                "deck_json": _deck_json(t0),
                "arena_id": arena.get("id") if isinstance(arena, dict) else None,
                "arena_name": arena.get("name") if isinstance(arena, dict) else None,
                "teammate_tag": teammate,
                "league_number": b.get("leagueNumber"),
                "is_hosted_match": int(b["isHostedMatch"])
                if isinstance(b.get("isHostedMatch"), bool)
                else None,
                "tournament_tag": b.get("tournamentTag"),
                "event_tag": b.get("eventTag"),
            }
        )
    return out


_INSERT_COLUMNS = (
    "dedup_key",
    "player_tag",
    "battle_time",
    "observed_at",
    "battle_type",
    "opponent_tag",
    "crowns_for",
    "crowns_against",
    "game_mode_id",
    "game_mode_name",
    "mode_group",
    "outcome",
    "is_war",
    "is_ladder",
    "is_ranked",
    "is_competitive",
    "is_special_event",
    "trophy_change",
    "starting_trophies",
    "deck_selection",
    "deck_json",
    "arena_id",
    "arena_name",
    "teammate_tag",
    "league_number",
    "is_hosted_match",
    "tournament_tag",
    "event_tag",
    "season_id",
    "section_index",
    "war_day_index",
)

_INSERT_SQL = (
    f"INSERT OR IGNORE INTO battle_events ({','.join(_INSERT_COLUMNS)}) "
    f"VALUES ({','.join('?' for _ in _INSERT_COLUMNS)})"
)


def mirror_battles(
    conn,
    player_tag: str,
    battlelog: list,
    observed_at: str,
    clock: WarClock | None,
    now: datetime | None = None,
) -> int:
    """Mirror a player's battlelog into battle_events. Returns newly-inserted
    row count (dedup key '{player_tag}:{battle_time}:{opponent_tag}',
    schema.md §5.1)."""
    now = now or datetime.now(timezone.utc)
    inserted = 0
    for bt in extract_battles(player_tag, battlelog):
        season_id = section_index = war_day_index = None
        if bt["is_war"]:
            season_id, section_index, war_day_index = resolve_war_keys(
                bt["battle_time"], clock, now
            )
        dedup_key = f"{bt['player_tag']}:{bt['battle_time']}:{bt['opponent_tag']}"
        row = {
            **bt,
            "dedup_key": dedup_key,
            "observed_at": observed_at,
            "season_id": season_id,
            "section_index": section_index,
            "war_day_index": war_day_index,
        }
        cur = conn.execute(_INSERT_SQL, [row[c] for c in _INSERT_COLUMNS])
        inserted += cur.rowcount
    return inserted
