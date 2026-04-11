"""Tournament tracking storage layer.

Handles registration, polling, battle capture, and recap context building
for clan-hosted private tournaments.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import (
    _canon_tag,
    _ensure_member,
    _json_or_none,
    _utcnow,
    managed_connection,
)
from storage.player import _normalize_cards_for_storage


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _api_status_to_internal(api_status: str) -> str:
    return {
        "inPreparation": "in_preparation",
        "inProgress": "in_progress",
        "ended": "ended",
    }.get(api_status, api_status)


def _member_id_for_tag(conn, tag: str):
    row = conn.execute(
        "SELECT member_id FROM members WHERE player_tag = ?", (_canon_tag(tag),)
    ).fetchone()
    return row["member_id"] if row else None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@managed_connection
def register_tournament(tournament_tag: str, api_data: dict, conn: Optional[sqlite3.Connection] = None) -> int:
    """Create a tournaments row from a /tournaments/{tag} API response.

    Returns the tournament_id.
    """
    tag = _canon_tag(tournament_tag)
    api_status = api_data.get("status") or ""
    internal_status = _api_status_to_internal(api_status)

    game_mode = api_data.get("gameMode") or {}
    creator_tag = _canon_tag(api_data.get("creatorTag") or "")
    creator_name = None
    members_list = api_data.get("membersList") or []
    for m in members_list:
        if _canon_tag(m.get("tag") or "") == creator_tag:
            creator_name = m.get("name")
            break

    conn.execute(
        """INSERT OR IGNORE INTO tournaments (
            tournament_tag, name, description, type, status,
            creator_tag, creator_name,
            game_mode_id, game_mode_name, deck_selection,
            level_cap, max_capacity,
            duration_seconds, preparation_duration_seconds,
            created_time, started_time, ended_time,
            watching_started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tag,
            api_data.get("name"),
            api_data.get("description"),
            api_data.get("type"),
            internal_status,
            creator_tag,
            creator_name,
            game_mode.get("id"),
            game_mode.get("name"),
            None,  # deck_selection filled on first battle
            api_data.get("levelCap"),
            api_data.get("maxCapacity"),
            api_data.get("duration"),
            api_data.get("preparationDuration"),
            api_data.get("createdTime"),
            api_data.get("startedTime"),
            api_data.get("endedTime"),
            _utcnow(),
        ),
    )

    row = conn.execute(
        "SELECT tournament_id FROM tournaments WHERE tournament_tag = ?", (tag,)
    ).fetchone()
    tournament_id = row["tournament_id"]

    # Seed initial participants
    now = _utcnow()
    for m in members_list:
        p_tag = _canon_tag(m.get("tag") or "")
        if not p_tag:
            continue
        member_id = _member_id_for_tag(conn, p_tag)
        conn.execute(
            """INSERT OR IGNORE INTO tournament_participants
               (tournament_id, player_tag, player_name, member_id, clan_tag,
                first_seen_at, last_seen_at, final_score, final_rank)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tournament_id,
                p_tag,
                m.get("name"),
                member_id,
                (m.get("clan") or {}).get("tag"),
                now,
                now,
                m.get("score"),
                m.get("rank"),
            ),
        )

    conn.commit()
    return tournament_id


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

@managed_connection
def poll_tournament(tournament_tag: str, api_data: dict, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Update tournament state from a fresh API response.

    Returns dict with:
      - participants: list of participant dicts (for battle log fetching)
      - live_signals: list of signal dicts to deliver
    """
    tag = _canon_tag(tournament_tag)
    now = _utcnow()
    api_status = api_data.get("status") or ""
    new_status = _api_status_to_internal(api_status)

    row = conn.execute(
        "SELECT tournament_id, status, poll_count FROM tournaments WHERE tournament_tag = ?",
        (tag,),
    ).fetchone()
    if not row:
        return {"participants": [], "live_signals": []}

    tournament_id = row["tournament_id"]
    old_status = row["status"]
    poll_count = row["poll_count"]

    # Track previous leader for lead-change detection
    prev_leader = conn.execute(
        "SELECT player_tag, player_name, final_score FROM tournament_participants WHERE tournament_id = ? ORDER BY final_rank ASC LIMIT 1",
        (tournament_id,),
    ).fetchone()

    # Update tournament row
    conn.execute(
        """UPDATE tournaments SET
            status = ?, poll_count = ?, last_poll_at = ?,
            started_time = COALESCE(started_time, ?),
            ended_time = COALESCE(ended_time, ?)
        WHERE tournament_id = ?""",
        (
            new_status,
            poll_count + 1,
            now,
            api_data.get("startedTime"),
            api_data.get("endedTime"),
            tournament_id,
        ),
    )

    # Upsert participants
    members_list = api_data.get("membersList") or []
    for m in members_list:
        p_tag = _canon_tag(m.get("tag") or "")
        if not p_tag:
            continue
        member_id = _member_id_for_tag(conn, p_tag)
        existing = conn.execute(
            "SELECT participant_id FROM tournament_participants WHERE tournament_id = ? AND player_tag = ?",
            (tournament_id, p_tag),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE tournament_participants SET
                    player_name = ?, last_seen_at = ?,
                    final_score = ?, final_rank = ?,
                    member_id = COALESCE(member_id, ?)
                WHERE participant_id = ?""",
                (m.get("name"), now, m.get("score"), m.get("rank"), member_id, existing["participant_id"]),
            )
        else:
            conn.execute(
                """INSERT INTO tournament_participants
                   (tournament_id, player_tag, player_name, member_id, clan_tag,
                    first_seen_at, last_seen_at, final_score, final_rank)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tournament_id,
                    p_tag,
                    m.get("name"),
                    member_id,
                    (m.get("clan") or {}).get("tag"),
                    now,
                    now,
                    m.get("score"),
                    m.get("rank"),
                ),
            )

    conn.commit()

    # Build participant list for battle log fetching
    participants = [
        {"player_tag": _canon_tag(m.get("tag") or ""), "player_name": m.get("name")}
        for m in members_list
        if m.get("tag")
    ]

    # Detect live signals
    live_signals = []

    tournament_name = api_data.get("name") or tag
    participant_count = len(members_list)

    if old_status != new_status:
        if new_status == "in_progress":
            live_signals.append({
                "type": "tournament_started",
                "signal_key": f"tournament_started|{tag}",
                "tournament_tag": tag,
                "tournament_name": tournament_name,
                "participant_count": participant_count,
                "game_mode_id": (api_data.get("gameMode") or {}).get("id"),
                "deck_selection": api_data.get("deckSelection"),
            })
        elif new_status == "ended":
            top3 = sorted(members_list, key=lambda m: m.get("rank") or 999)[:3]
            live_signals.append({
                "type": "tournament_ended",
                "signal_key": f"tournament_ended|{tag}",
                "tournament_tag": tag,
                "tournament_name": tournament_name,
                "participant_count": participant_count,
                "winner_name": top3[0].get("name") if top3 else None,
                "winner_score": top3[0].get("score") if top3 else None,
                "top3": [{"name": m.get("name"), "score": m.get("score"), "rank": m.get("rank")} for m in top3],
            })

    # Lead change detection (only during in_progress)
    if new_status == "in_progress" and members_list:
        new_leader = sorted(members_list, key=lambda m: m.get("rank") or 999)[0]
        new_leader_tag = _canon_tag(new_leader.get("tag") or "")
        new_leader_score = new_leader.get("score") or 0
        if (
            prev_leader
            and new_leader_tag != prev_leader["player_tag"]
            and new_leader_score > 0
        ):
            live_signals.append({
                "type": "tournament_lead_change",
                "signal_key": f"tournament_lead_change|{tag}|{new_leader_tag}|{new_leader_score}",
                "tournament_tag": tag,
                "tournament_name": tournament_name,
                "new_leader_name": new_leader.get("name"),
                "new_leader_score": new_leader_score,
                "previous_leader_name": prev_leader["player_name"],
            })

    return {"participants": participants, "live_signals": live_signals}


# ---------------------------------------------------------------------------
# Battle capture
# ---------------------------------------------------------------------------

@managed_connection
def store_tournament_battle(tournament_id: int, battle: dict, conn: Optional[sqlite3.Connection] = None) -> bool:
    """Store a single tournament battle with dedup.

    Canonicalizes player order (player1_tag < player2_tag lexicographically)
    so the same match stored from either player's log deduplicates.

    Returns True if a new battle was inserted, False if it was a duplicate.
    """
    team = (battle.get("team") or [{}])[0]
    opp = (battle.get("opponent") or [{}])[0]
    if not team or not opp:
        return False

    tag_a = _canon_tag(team.get("tag") or "")
    tag_b = _canon_tag(opp.get("tag") or "")
    if not tag_a or not tag_b:
        return False

    # Determine winner from crowns
    crowns_a = team.get("crowns")
    crowns_b = opp.get("crowns")
    winner_tag = None
    if isinstance(crowns_a, int) and isinstance(crowns_b, int):
        if crowns_a > crowns_b:
            winner_tag = tag_a
        elif crowns_b > crowns_a:
            winner_tag = tag_b

    deck_a = _json_or_none(_normalize_cards_for_storage(team.get("cards") or []))
    deck_b = _json_or_none(_normalize_cards_for_storage(opp.get("cards") or []))

    # Canonicalize order: player1_tag is always the lexicographically smaller tag
    if tag_a <= tag_b:
        p1_tag, p1_name, p1_mid, p1_crowns, p1_deck = tag_a, team.get("name"), _member_id_for_tag(conn, tag_a), crowns_a, deck_a
        p2_tag, p2_name, p2_mid, p2_crowns, p2_deck = tag_b, opp.get("name"), _member_id_for_tag(conn, tag_b), crowns_b, deck_b
    else:
        p1_tag, p1_name, p1_mid, p1_crowns, p1_deck = tag_b, opp.get("name"), _member_id_for_tag(conn, tag_b), crowns_b, deck_b
        p2_tag, p2_name, p2_mid, p2_crowns, p2_deck = tag_a, team.get("name"), _member_id_for_tag(conn, tag_a), crowns_a, deck_a

    arena = battle.get("arena") or {}
    game_mode = battle.get("gameMode") or {}

    cursor = conn.execute(
        """INSERT OR IGNORE INTO tournament_battles (
            tournament_id, battle_time,
            player1_tag, player1_name, player1_member_id, player1_crowns, player1_deck_json,
            player2_tag, player2_name, player2_member_id, player2_crowns, player2_deck_json,
            winner_tag, deck_selection, game_mode_id, arena_name, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tournament_id,
            battle.get("battleTime"),
            p1_tag, p1_name, p1_mid, p1_crowns, p1_deck,
            p2_tag, p2_name, p2_mid, p2_crowns, p2_deck,
            winner_tag,
            battle.get("deckSelection"),
            game_mode.get("id"),
            arena.get("name") if isinstance(arena, dict) else None,
            _json_or_none(battle),
        ),
    )

    inserted = cursor.rowcount > 0
    if inserted:
        conn.execute(
            "UPDATE tournaments SET battles_captured = battles_captured + 1 WHERE tournament_id = ?",
            (tournament_id,),
        )
        # Set deck_selection on tournament if not yet known
        deck_sel = battle.get("deckSelection")
        if deck_sel:
            conn.execute(
                "UPDATE tournaments SET deck_selection = COALESCE(deck_selection, ?) WHERE tournament_id = ?",
                (deck_sel, tournament_id),
            )

    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------

@managed_connection
def finalize_tournament(tournament_tag: str, api_data: dict, conn: Optional[sqlite3.Connection] = None) -> None:
    """Mark a tournament as ended and store final snapshot."""
    tag = _canon_tag(tournament_tag)
    conn.execute(
        """UPDATE tournaments SET
            status = 'ended',
            watching_ended_at = ?,
            ended_time = COALESCE(ended_time, ?),
            raw_final_json = ?
        WHERE tournament_tag = ?""",
        (_utcnow(), api_data.get("endedTime"), _json_or_none(api_data), tag),
    )
    # Update final scores/ranks from the API
    for m in api_data.get("membersList") or []:
        p_tag = _canon_tag(m.get("tag") or "")
        if not p_tag:
            continue
        conn.execute(
            """UPDATE tournament_participants SET
                final_score = ?, final_rank = ?, player_name = ?
            WHERE tournament_id = (SELECT tournament_id FROM tournaments WHERE tournament_tag = ?)
              AND player_tag = ?""",
            (m.get("score"), m.get("rank"), m.get("name"), tag, p_tag),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

@managed_connection
def get_active_tournament(conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """Return the currently watched tournament or None."""
    row = conn.execute(
        """SELECT * FROM tournaments
           WHERE status IN ('watching', 'in_preparation', 'in_progress')
           ORDER BY tournament_id DESC LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


@managed_connection
def get_tournament_by_tag(tournament_tag: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """Return a tournament row by tag or None."""
    tag = _canon_tag(tournament_tag)
    row = conn.execute(
        "SELECT * FROM tournaments WHERE tournament_tag = ?", (tag,)
    ).fetchone()
    return dict(row) if row else None


@managed_connection
def get_tournament_participants(tournament_id: int, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Return all participants for a tournament, ordered by rank."""
    rows = conn.execute(
        """SELECT * FROM tournament_participants
           WHERE tournament_id = ?
           ORDER BY COALESCE(final_rank, 999) ASC""",
        (tournament_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@managed_connection
def get_tournament_battles(tournament_id: int, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Return all battles for a tournament, ordered by time."""
    rows = conn.execute(
        """SELECT * FROM tournament_battles
           WHERE tournament_id = ?
           ORDER BY battle_time ASC""",
        (tournament_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@managed_connection
def get_recent_tournaments_for_recap(days: int = 7, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Return recent ended tournaments with summary data for weekly recap integration."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """SELECT t.*,
            (SELECT COUNT(*) FROM tournament_participants WHERE tournament_id = t.tournament_id) AS participant_count
        FROM tournaments t
        WHERE t.status = 'ended' AND t.watching_ended_at >= ?
        ORDER BY t.tournament_id DESC""",
        (cutoff,),
    ).fetchall()

    results = []
    for row in rows:
        t = dict(row)
        tid = t["tournament_id"]

        # Get winner
        winner_row = conn.execute(
            "SELECT player_name, final_score FROM tournament_participants WHERE tournament_id = ? AND final_rank = 1",
            (tid,),
        ).fetchone()

        # Get top 3 cards
        card_stats = get_tournament_card_stats(tid, conn=conn)
        top3_cards = ", ".join(c["name"] for c in (card_stats.get("cards") or [])[:3])

        deck_label = {
            "draftCompetitive": "Triple Draft",
            "collection": "Bring Your Own Deck",
            "draft": "Draft",
        }.get(t.get("deck_selection") or "", t.get("deck_selection") or "")

        results.append({
            "name": t["name"],
            "tournament_tag": t["tournament_tag"],
            "deck_selection": deck_label,
            "participant_count": t["participant_count"],
            "battles_captured": t.get("battles_captured", 0),
            "winner_name": winner_row["player_name"] if winner_row else None,
            "winner_score": winner_row["final_score"] if winner_row else None,
            "top_cards": top3_cards or None,
        })

    return results


# ---------------------------------------------------------------------------
# Card analysis
# ---------------------------------------------------------------------------

@managed_connection
def get_tournament_card_stats(tournament_id: int, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Aggregate card usage across all tournament battles.

    Returns dict with:
      - cards: list of {name, id, pick_count, win_count, win_rate, players}
      - player_tendencies: dict of player_name -> list of {name, pick_count, win_count}
    """
    battles = get_tournament_battles(tournament_id, conn=conn)

    card_stats = {}   # card_name -> {picks, wins, players}
    player_cards = {} # player_name -> {card_name -> {picks, wins}}

    for battle in battles:
        for side in (1, 2):
            tag_key = f"player{side}_tag"
            name_key = f"player{side}_name"
            deck_key = f"player{side}_deck_json"

            player_name = battle[name_key] or battle[tag_key]
            deck_json = battle[deck_key]
            if not deck_json:
                continue

            cards = json.loads(deck_json)
            is_winner = battle["winner_tag"] == battle[tag_key]

            if player_name not in player_cards:
                player_cards[player_name] = {}

            for card in cards:
                cname = card.get("name") or f"id:{card.get('id')}"
                cid = card.get("id")

                if cname not in card_stats:
                    card_stats[cname] = {"name": cname, "id": cid, "picks": 0, "wins": 0, "players": set()}
                card_stats[cname]["picks"] += 1
                card_stats[cname]["players"].add(player_name)
                if is_winner:
                    card_stats[cname]["wins"] += 1

                if cname not in player_cards[player_name]:
                    player_cards[player_name][cname] = {"picks": 0, "wins": 0}
                player_cards[player_name][cname]["picks"] += 1
                if is_winner:
                    player_cards[player_name][cname]["wins"] += 1

    # Build sorted card list
    card_list = []
    for cname, stats in sorted(card_stats.items(), key=lambda x: x[1]["picks"], reverse=True):
        win_rate = stats["wins"] / stats["picks"] if stats["picks"] > 0 else 0
        card_list.append({
            "name": cname,
            "id": stats["id"],
            "pick_count": stats["picks"],
            "win_count": stats["wins"],
            "win_rate": round(win_rate, 2),
            "player_count": len(stats["players"]),
        })

    # Build player tendencies
    player_tendencies = {}
    for player_name, cards in player_cards.items():
        tendency_list = sorted(cards.items(), key=lambda x: x[1]["picks"], reverse=True)
        player_tendencies[player_name] = [
            {"name": cname, "pick_count": stats["picks"], "win_count": stats["wins"]}
            for cname, stats in tendency_list
        ]

    return {"cards": card_list, "player_tendencies": player_tendencies}


# ---------------------------------------------------------------------------
# Recap context
# ---------------------------------------------------------------------------

@managed_connection
def build_tournament_recap_context(tournament_tag: str, conn: Optional[sqlite3.Connection] = None) -> str:
    """Build structured text context for LLM recap generation.

    Returns a multi-section string with tournament metadata, standings,
    card analysis, head-to-head matchups, and notable moments.
    """
    tournament = get_tournament_by_tag(tournament_tag, conn=conn)
    if not tournament:
        return ""

    tid = tournament["tournament_id"]
    participants = get_tournament_participants(tid, conn=conn)
    battles = get_tournament_battles(tid, conn=conn)
    card_stats = get_tournament_card_stats(tid, conn=conn)

    sections = []

    # --- Tournament metadata ---
    deck_label = {
        "draftCompetitive": "Triple Draft",
        "collection": "Bring Your Own Deck",
        "draft": "Draft",
    }.get(tournament.get("deck_selection") or "", tournament.get("deck_selection") or "Unknown")

    duration_hrs = (tournament.get("duration_seconds") or 0) / 3600
    sections.append(
        f"=== TOURNAMENT ===\n"
        f"Name: {tournament['name']}\n"
        f"Format: {deck_label}\n"
        f"Duration: {duration_hrs:.0f} hours\n"
        f"Participants: {len(participants)}\n"
        f"Total battles captured: {len(battles)}\n"
        f"Creator: {tournament.get('creator_name') or tournament.get('creator_tag')}"
    )

    # --- Final standings ---
    standings_lines = []
    for p in participants:
        clan_member = " (clan)" if p.get("member_id") else ""
        standings_lines.append(
            f"{p.get('final_rank', '?')}. {p['player_name']} — {p.get('final_score', 0)} wins{clan_member}"
        )
    sections.append("=== FINAL STANDINGS ===\n" + "\n".join(standings_lines))

    # --- Card analysis: most picked ---
    top_cards = card_stats["cards"][:15]
    if top_cards:
        card_lines = []
        for c in top_cards:
            card_lines.append(
                f"- {c['name']}: {c['pick_count']} picks, "
                f"{c['win_count']} wins ({c['win_rate']:.0%} win rate), "
                f"picked by {c['player_count']} players"
            )
        sections.append("=== MOST PICKED CARDS ===\n" + "\n".join(card_lines))

    # --- Player card tendencies ---
    tendency_lines = []
    for player_name, cards in card_stats["player_tendencies"].items():
        top3 = cards[:3]
        if top3:
            favs = ", ".join(f"{c['name']} ({c['pick_count']}x)" for c in top3)
            tendency_lines.append(f"- {player_name}: {favs}")
    if tendency_lines:
        sections.append("=== PLAYER CARD TENDENCIES ===\n" + "\n".join(tendency_lines))

    # --- Head-to-head matchups ---
    h2h = {}  # (p1_name, p2_name) -> {"p1_wins": 0, "p2_wins": 0, "battles": []}
    for b in battles:
        p1 = b["player1_name"] or b["player1_tag"]
        p2 = b["player2_name"] or b["player2_tag"]
        key = (p1, p2)
        if key not in h2h:
            h2h[key] = {"p1_wins": 0, "p2_wins": 0, "battles": []}
        if b["winner_tag"] == b["player1_tag"]:
            h2h[key]["p1_wins"] += 1
        elif b["winner_tag"] == b["player2_tag"]:
            h2h[key]["p2_wins"] += 1

        # Extract card names for battle summary
        p1_cards = [c.get("name") for c in json.loads(b["player1_deck_json"] or "[]")]
        p2_cards = [c.get("name") for c in json.loads(b["player2_deck_json"] or "[]")]
        winner_name = None
        if b["winner_tag"] == b["player1_tag"]:
            winner_name = p1
        elif b["winner_tag"] == b["player2_tag"]:
            winner_name = p2
        h2h[key]["battles"].append({
            "time": b["battle_time"],
            "p1_crowns": b["player1_crowns"],
            "p2_crowns": b["player2_crowns"],
            "winner": winner_name,
            "p1_cards": p1_cards,
            "p2_cards": p2_cards,
        })

    if h2h:
        h2h_lines = []
        for (p1, p2), record in h2h.items():
            h2h_lines.append(f"\n{p1} vs {p2}: {record['p1_wins']}-{record['p2_wins']}")
            for b in record["battles"]:
                winner = b["winner"] or "Draw"
                p1_deck = ", ".join(b["p1_cards"][:4]) + "..." if len(b["p1_cards"]) > 4 else ", ".join(b["p1_cards"])
                p2_deck = ", ".join(b["p2_cards"][:4]) + "..." if len(b["p2_cards"]) > 4 else ", ".join(b["p2_cards"])
                h2h_lines.append(
                    f"  {b['p1_crowns']}-{b['p2_crowns']} ({winner} wins) | "
                    f"{p1}: [{p1_deck}] vs {p2}: [{p2_deck}]"
                )
        sections.append("=== HEAD-TO-HEAD MATCHUPS ===" + "\n".join(h2h_lines))

    # --- Notable moments ---
    notable = []
    for b in battles:
        p1_crowns = b.get("player1_crowns") or 0
        p2_crowns = b.get("player2_crowns") or 0
        if p1_crowns == 3 or p2_crowns == 3:
            winner = b["player1_name"] if p1_crowns == 3 else b["player2_name"]
            loser = b["player2_name"] if p1_crowns == 3 else b["player1_name"]
            notable.append(f"- {winner} three-crowned {loser}")
    if notable:
        sections.append("=== NOTABLE MOMENTS ===\n" + "\n".join(notable))

    return "\n\n".join(sections)


__all__ = [
    "register_tournament",
    "poll_tournament",
    "store_tournament_battle",
    "finalize_tournament",
    "get_active_tournament",
    "get_tournament_by_tag",
    "get_tournament_participants",
    "get_tournament_battles",
    "get_recent_tournaments_for_recap",
    "get_tournament_card_stats",
    "build_tournament_recap_context",
]
