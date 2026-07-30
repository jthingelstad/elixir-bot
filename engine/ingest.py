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
from engine.normalize import canonical_utc_timestamp
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


def _deck_json(participant: dict) -> str | None:
    """Slim a participant's 8 cards to (id, name, level).

    Applied to BOTH sides. The opponent's deck is the only record of what a
    member actually lost to -- their tag can be re-scouted later, but the deck
    they brought to this specific battle cannot be recovered from any endpoint
    once it ages out of the battle log.
    """
    cards = participant.get("cards") or []
    if not cards:
        return None
    deck = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        slim = {"id": c.get("id"), "name": c.get("name"), "level": c.get("level")}
        # Only ~12% of cards played are evolved, so carry the key only when it
        # applies -- but carry it. An Evo Knight is a different threat from a
        # plain one, and collapsing them makes "what beat you" advice wrong.
        if c.get("evolutionLevel"):
            slim["evolution_level"] = c["evolutionLevel"]
        if c.get("starLevel"):
            slim["star_level"] = c["starLevel"]
        # Duel rounds mark which cards were actually played; absent elsewhere.
        if c.get("used") is not None:
            slim["used"] = bool(c["used"])
        deck.append(slim)
    return json.dumps(deck, separators=(",", ":")) if deck else None


def _rounds_json(participant: dict) -> str | None:
    """Per-round decks for a war duel (2-3 rounds under one battle).

    A duel's top-level `cards` is every round's deck concatenated, so the
    individual decks are only recoverable here. Keeps crowns, tower HP and
    elixir leaked per round alongside the deck.
    """
    rounds = participant.get("rounds") or []
    if not rounds:
        return None
    out = []
    for r in rounds:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "crowns": r.get("crowns"),
                "elixir_leaked": r.get("elixirLeaked"),
                "princess_towers_hp": r.get("princessTowersHitPoints"),
                # Key is `cards`, matching the shape war_analytics'
                # _extract_deck_candidates already expects for duel rounds.
                "cards": json.loads(_deck_json(r) or "[]"),
            }
        )
    return json.dumps(out, separators=(",", ":")) if out else None


def _support_cards_json(participant: dict) -> str | None:
    """The tower troop. 100% coverage and never stored before v17 -- 6.9% of
    use is non-default (Dagger Duchess, Royal Chef, Cannoneer), so it is real
    deck identity rather than a constant."""
    cards = participant.get("supportCards") or []
    slim = [
        {"id": c.get("id"), "name": c.get("name"), "level": c.get("level")}
        for c in cards
        if isinstance(c, dict)
    ]
    return json.dumps(slim, separators=(",", ":")) if slim else None


def _towers_json(participant: dict) -> str | None:
    hp = participant.get("princessTowersHitPoints")
    return json.dumps(hp, separators=(",", ":")) if isinstance(hp, list) else None


def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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
                    canon_tag(tm.get("tag")) if isinstance(tm, dict) and tm.get("tag") else None
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
                "battle_time": canonical_utc_timestamp(b.get("battleTime")),
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
                "deck_json": _deck_json(t0),
                "opponent_deck_json": _deck_json(o0),
                # --- v17: the rest of the battle record ----------------------
                "support_cards_json": _support_cards_json(t0),
                "elixir_leaked": _float_or_none(t0.get("elixirLeaked")),
                "king_tower_hp": _int_or_none(t0.get("kingTowerHitPoints")),
                "princess_towers_hp_json": _towers_json(t0),
                "global_rank": _int_or_none(t0.get("globalRank")),
                # Their clan AT BATTLE TIME. Not the same as their clan today,
                # which is what every other table records.
                "clan_tag": (t0.get("clan") or {}).get("tag"),
                "rounds_json": _rounds_json(t0),
                "opponent_name": o0.get("name"),
                "opponent_clan_tag": (o0.get("clan") or {}).get("tag"),
                "opponent_clan_name": (o0.get("clan") or {}).get("name"),
                "opponent_clan_badge_id": _int_or_none((o0.get("clan") or {}).get("badgeId")),
                "opponent_support_cards_json": _support_cards_json(o0),
                "opponent_elixir_leaked": _float_or_none(o0.get("elixirLeaked")),
                "opponent_king_tower_hp": _int_or_none(o0.get("kingTowerHitPoints")),
                "opponent_princess_towers_hp_json": _towers_json(o0),
                "opponent_global_rank": _int_or_none(o0.get("globalRank")),
                "opponent_starting_trophies": _int_or_none(o0.get("startingTrophies")),
                "opponent_trophy_change": _int_or_none(o0.get("trophyChange")),
                "opponent_rounds_json": _rounds_json(o0),
                "modifiers_json": (
                    json.dumps(b["modifiers"], separators=(",", ":"))
                    if b.get("modifiers")
                    else None
                ),
                "boat_battle_side": b.get("boatBattleSide"),
                "boat_battle_won": (
                    int(b["boatBattleWon"]) if isinstance(b.get("boatBattleWon"), bool) else None
                ),
                "new_towers_destroyed": _int_or_none(b.get("newTowersDestroyed")),
                "prev_towers_destroyed": _int_or_none(b.get("prevTowersDestroyed")),
                "remaining_towers": _int_or_none(b.get("remainingTowers")),
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
    "opponent_deck_json",
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
    # v17 -- the rest of the battle. Every one of these is also enriched on
    # dedup below, so a thinner first observation is never made permanent.
    "support_cards_json",
    "elixir_leaked",
    "king_tower_hp",
    "princess_towers_hp_json",
    "global_rank",
    "clan_tag",
    "rounds_json",
    "opponent_name",
    "opponent_clan_tag",
    "opponent_clan_name",
    "opponent_clan_badge_id",
    "opponent_support_cards_json",
    "opponent_elixir_leaked",
    "opponent_king_tower_hp",
    "opponent_princess_towers_hp_json",
    "opponent_global_rank",
    "opponent_starting_trophies",
    "opponent_trophy_change",
    "opponent_rounds_json",
    "modifiers_json",
    "boat_battle_side",
    "boat_battle_won",
    "new_towers_destroyed",
    "prev_towers_destroyed",
    "remaining_towers",
)

_INSERT_SQL = (
    f"INSERT OR IGNORE INTO battle_events ({','.join(_INSERT_COLUMNS)}) "
    f"VALUES ({','.join('?' for _ in _INSERT_COLUMNS)})"
)

# Columns the enrich-on-dedup UPDATE fills when a thinner earlier observation
# already claimed the dedup key. Derived from one list so the two paths cannot
# drift: a column added to the INSERT but forgotten here would silently never
# backfill (which is exactly how deck_json stayed NULL on re-polled battles).
_ENRICH_COLUMNS = (
    (
        "teammate_tag",
        "league_number",
        "deck_json",
        "arena_id",
        "arena_name",
    )
    + _INSERT_COLUMNS[
        _INSERT_COLUMNS.index("opponent_deck_json") : _INSERT_COLUMNS.index("arena_id")
    ]
    + _INSERT_COLUMNS[_INSERT_COLUMNS.index("support_cards_json") :]
)

_ENRICH_SQL = (
    "UPDATE battle_events SET "
    "season_id = COALESCE(season_id, ?), "
    "section_index = COALESCE(section_index, ?), "
    "war_day_index = COALESCE(war_day_index, ?), "
    + ", ".join(f"{c} = COALESCE({c}, ?)" for c in _ENRICH_COLUMNS)
    + " WHERE dedup_key = ?"
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
        # The key embeds battle_time, so it is only stable while battle_time's
        # FORMAT is stable. Normalizing that column to ISO-Z (schema v25) silently
        # changed every key: the same battle re-polled after the change hashed to
        # a new key, INSERT OR IGNORE saw no collision, and 1,348 duplicate rows
        # landed before anyone noticed. Derive it from the same canonical value
        # that gets stored, so the key and the column can never disagree again.
        dedup_key = (
            f"{bt['player_tag']}:{canonical_utc_timestamp(bt['battle_time'])}:{bt['opponent_tag']}"
        )
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
        if cur.rowcount == 0:
            # The same battle can arrive first through an interactive tool
            # refresh, where no current war clock is available, and later
            # through the scheduled engine tick.  Deduplication must not make
            # that first, thinner observation permanent: enrich only missing
            # facts while preserving the original native event row.
            conn.execute(
                _ENRICH_SQL,
                (
                    season_id,
                    section_index,
                    war_day_index,
                    *(row[c] for c in _ENRICH_COLUMNS),
                    dedup_key,
                ),
            )
    return inserted
