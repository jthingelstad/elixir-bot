"""Step-4 projection refreshers (runtime.md §2 step 4; schema.md §6).

Every function is incremental — it refreshes only what a poll touched. Reads
never scan streams at query time; these materializations are the read side.

Form/rollup semantics are ported from the proven storage layer
(`storage/player.py::_recompute_member_recent_form` /
`_recompute_member_daily_battle_rollups`) re-keyed to tags and re-sourced to
`battle_events` — labels, scopes, and completeness accounting carry verbatim
so the Phase-5 tool port reads identical shapes.
"""

from __future__ import annotations

import json

from db import chicago_date_for_cr_timestamp, chicago_day_bounds_utc

from engine.db import canon_tag, utcnow

# Scope predicates carried verbatim from storage/player.py::_LOSSES_SCOPE_PREDICATES —
# the read tools (get_member form/losses aspects) select by these exact keys.
FORM_SCOPES = {
    "overall_10": "1=1",
    "competitive_10": "is_competitive = 1",
    "ladder_ranked_10": "(is_ladder = 1 OR is_ranked = 1)",
    "ladder_10": "is_ladder = 1",
    "ranked_10": "is_ranked = 1",
    "event_10": "is_special_event = 1",
    "tournament_10": "(battle_type = 'tournament' OR tournament_tag IS NOT NULL)",
    "two_v_two_10": "(battle_type = 'clanMate2v2' OR game_mode_id IN (72000014, 72000051) OR game_mode_name = 'TeamVsTeam')",
    "friendly_10": "(battle_type IN ('clanMate', 'friendly', 'unknown') OR is_hosted_match = 1)",
    "war_10": "is_war = 1",
}
FORM_SAMPLE = 10


def _form_label(wins: int, losses: int, sample_size: int) -> str:
    # Carried from db.__init__._build_form_label.
    if sample_size == 0:
        return "inactive"
    win_rate = wins / sample_size
    if sample_size >= 5 and wins >= sample_size - 2:
        return "hot"
    if win_rate >= 0.6:
        return "strong"
    if win_rate <= 0.3 and losses >= 4:
        return "cold"
    if losses > wins:
        return "slumping"
    return "mixed"


def _form_summary(wins: int, losses: int, draws: int, sample_size: int, label: str) -> str:
    if sample_size == 0:
        return "No recent battles recorded."
    return f"{wins}-{losses}-{draws} over the last {sample_size} battles ({label})."


# ------------------------------------------------------------- player state

def refresh_player_state(conn, player_tag, profile_payload, roster_entry, observed_at):
    """Upsert player_current_state. Roster owns role/clan_rank/donations;
    profile owns exp/best/arena/ranked/deck. Either source may be None —
    missing fields keep their previous value (COALESCE on update)."""
    tag = canon_tag(player_tag)
    p = profile_payload or {}
    r = roster_entry or {}
    arena = p.get("arena") or {}
    _ranked_now = p.get("currentPathOfLegendSeasonResult") or {}
    ranked_league = _ranked_now.get("leagueNumber")
    # Rating comes from the RANKED result, not leagueStatistics.currentSeason
    # (that's trophy-road trophies — rehearsal 2026-07-04 found 14,000 stored
    # as a "rating" while the real UC rating was 1,867).
    ranked_trophies = _ranked_now.get("trophies")
    deck = p.get("currentDeck")
    # NOTE: `r` is the PROJECTED roster entry (engine/emitters/clan.py
    # project_clan_aspects — snake_case keys), not a raw CR memberList item.
    # Live incident 2026-07-04: camelCase lookups here meant exp_level /
    # clan_rank / donations_received never populated from roster refreshes.
    # Raw-key fallbacks kept for direct callers passing CR-shaped dicts.
    vals = {
        "role": r.get("role"),
        "exp_level": p.get("expLevel") or r.get("exp_level") or r.get("expLevel"),
        "trophies": r.get("trophies") if r.get("trophies") is not None else p.get("trophies"),
        "best_trophies": p.get("bestTrophies"),
        "clan_rank": r.get("clan_rank") or r.get("clanRank"),
        "previous_clan_rank": r.get("previous_clan_rank") or r.get("previousClanRank"),
        "donations_week": r.get("donations"),
        "donations_received_week": r.get("donations_received") or r.get("donationsReceived"),
        "arena_id": arena.get("id") if arena else (r.get("arena") or {}).get("id"),
        "arena_name": arena.get("name") if arena else (r.get("arena") or {}).get("name"),
        "ranked_league": ranked_league,
        "ranked_trophies": ranked_trophies,
        "current_deck_json": json.dumps(deck, ensure_ascii=False) if deck else None,
    }
    cols = ", ".join(vals)
    placeholders = ", ".join("?" for _ in vals)
    updates = ", ".join(
        f"{c} = COALESCE(excluded.{c}, player_current_state.{c})" for c in vals
    )
    conn.execute(
        f"""INSERT INTO player_current_state (player_tag, observed_at, {cols})
            VALUES (?, ?, {placeholders})
            ON CONFLICT(player_tag) DO UPDATE SET
                observed_at = excluded.observed_at, {updates}""",
        (tag, observed_at, *vals.values()),
    )


def refresh_card_collection(conn, player_tag, cards_payload, observed_at):
    """Upsert player_card_collection from a profile's cards[] array."""
    tag = canon_tag(player_tag)
    for card in cards_payload or []:
        card_id = card.get("id")
        if card_id is None:
            continue
        conn.execute(
            """INSERT INTO player_card_collection
                   (player_tag, card_id, level, count, star_level, evolution_level, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(player_tag, card_id) DO UPDATE SET
                   level = excluded.level, count = excluded.count,
                   star_level = excluded.star_level,
                   evolution_level = excluded.evolution_level,
                   observed_at = excluded.observed_at""",
            (
                tag, card_id, card.get("level"), card.get("count"),
                card.get("starLevel"), card.get("evolutionLevel"), observed_at,
            ),
        )


# --------------------------------------------------------------------- form

def refresh_form(conn, player_tag, now=None):
    """Recompute player_recent_form for every carried scope (last 10 battles
    per scope, from battle_events). schema.md §6.2: pre-materialized, never
    per-call."""
    tag = canon_tag(player_tag)
    computed_at = now or utcnow()
    for scope, predicate in FORM_SCOPES.items():
        rows = conn.execute(
            f"""SELECT outcome, crowns_for, crowns_against, trophy_change
                FROM battle_events WHERE player_tag = ? AND {predicate}
                ORDER BY battle_time DESC LIMIT {FORM_SAMPLE}""",
            (tag,),
        ).fetchall()
        sample_size = len(rows)
        wins = sum(1 for r in rows if r["outcome"] == "W")
        losses = sum(1 for r in rows if r["outcome"] == "L")
        draws = sum(1 for r in rows if r["outcome"] == "D")
        streak_type = rows[0]["outcome"] if rows and rows[0]["outcome"] else None
        streak = 0
        for row in rows:
            if streak_type and row["outcome"] == streak_type:
                streak += 1
            else:
                break
        diffs = [
            (r["crowns_for"] or 0) - (r["crowns_against"] or 0)
            for r in rows
            if r["crowns_for"] is not None and r["crowns_against"] is not None
        ]
        changes = [r["trophy_change"] for r in rows if r["trophy_change"] is not None]
        label = _form_label(wins, losses, sample_size)
        conn.execute(
            """INSERT INTO player_recent_form (player_tag, scope, computed_at,
                   sample_size, wins, losses, draws, current_streak,
                   current_streak_type, win_rate, avg_crown_diff,
                   avg_trophy_change, form_label, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(player_tag, scope) DO UPDATE SET
                   computed_at = excluded.computed_at,
                   sample_size = excluded.sample_size, wins = excluded.wins,
                   losses = excluded.losses, draws = excluded.draws,
                   current_streak = excluded.current_streak,
                   current_streak_type = excluded.current_streak_type,
                   win_rate = excluded.win_rate,
                   avg_crown_diff = excluded.avg_crown_diff,
                   avg_trophy_change = excluded.avg_trophy_change,
                   form_label = excluded.form_label, summary = excluded.summary""",
            (
                tag, scope, computed_at, sample_size, wins, losses, draws,
                streak, streak_type,
                round(wins / sample_size, 4) if sample_size else 0,
                round(sum(diffs) / len(diffs), 2) if diffs else None,
                round(sum(changes) / len(changes), 2) if changes else None,
                label, _form_summary(wins, losses, draws, sample_size, label),
            ),
        )


# ------------------------------------------------------------------ rollups

def refresh_rollups(conn, player_tag, date_chicago, expected_battle_delta=None):
    """Upsert player_daily_battle_rollups (+ the day's player_daily_metrics
    row) for one Chicago day from battle_events.

    expected_battle_delta: the profile battles-count delta across the day —
    the tick computes it from baseline history when available (the old
    player_profile_snapshots source is gone); None means unknown, matching
    the carried NULL semantics.
    """
    tag = canon_tag(player_tag)
    start_utc, end_utc = chicago_day_bounds_utc(date_chicago)
    compact = lambda s: s.replace("-", "").replace(":", "")  # noqa: E731  (CR compact time)
    rows = conn.execute(
        """SELECT battle_time, mode_group, game_mode_id, game_mode_name, outcome,
                  crowns_for, crowns_against, trophy_change
           FROM battle_events
           WHERE player_tag = ?
             AND REPLACE(REPLACE(battle_time, '.000Z', ''), 'Z', '') >= ?
             AND REPLACE(REPLACE(battle_time, '.000Z', ''), 'Z', '') < ?""",
        (tag, compact(start_utc), compact(end_utc)),
    ).fetchall()
    conn.execute(
        "DELETE FROM player_daily_battle_rollups WHERE player_tag = ? AND battle_date = ?",
        (tag, date_chicago),
    )
    _refresh_daily_metrics(conn, tag, date_chicago)
    if not rows:
        return
    buckets: dict = {}
    for row in rows:
        key = (row["mode_group"], row["game_mode_id"])
        b = buckets.setdefault(
            key,
            {
                "game_mode_name": row["game_mode_name"], "battles": 0, "wins": 0,
                "losses": 0, "draws": 0, "crowns_for": 0, "crowns_against": 0,
                "trophy_change_total": 0, "first": None, "last": None,
            },
        )
        b["battles"] += 1
        if row["outcome"] == "W":
            b["wins"] += 1
        elif row["outcome"] == "L":
            b["losses"] += 1
        elif row["outcome"] == "D":
            b["draws"] += 1
        b["crowns_for"] += int(row["crowns_for"] or 0)
        b["crowns_against"] += int(row["crowns_against"] or 0)
        b["trophy_change_total"] += int(row["trophy_change"] or 0)
        bt = row["battle_time"]
        b["first"] = bt if b["first"] is None or bt < b["first"] else b["first"]
        b["last"] = bt if b["last"] is None or bt > b["last"] else b["last"]

    captured = len(rows)
    completeness = None
    is_complete = 0
    if expected_battle_delta is not None:
        completeness = min(1.0, round(captured / max(expected_battle_delta, 1), 4))
        is_complete = 1 if captured >= expected_battle_delta else 0
    aggregated_at = utcnow()
    for (mode_group, game_mode_id), b in buckets.items():
        conn.execute(
            """INSERT INTO player_daily_battle_rollups (player_tag, battle_date,
                   mode_group, game_mode_id, game_mode_name, battles, wins, losses,
                   draws, crowns_for, crowns_against, trophy_change_total,
                   first_battle_at, last_battle_at, captured_battles,
                   expected_battle_delta, completeness_ratio, is_complete,
                   last_aggregated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tag, date_chicago, mode_group, game_mode_id, b["game_mode_name"],
                b["battles"], b["wins"], b["losses"], b["draws"], b["crowns_for"],
                b["crowns_against"], b["trophy_change_total"], b["first"], b["last"],
                captured, expected_battle_delta, completeness, is_complete,
                aggregated_at,
            ),
        )


def _refresh_daily_metrics(conn, tag, date_chicago):
    """Upsert the day's player_daily_metrics row from player_current_state.
    last_seen_api carries the in-game lastSeen for roster-badge awareness — it
    is recorded, NOT used as an engagement signal (battling stays the kick
    clock; architecture §13.6)."""
    state = conn.execute(
        "SELECT * FROM player_current_state WHERE player_tag = ?", (tag,)
    ).fetchone()
    if not state:
        return
    last_seen_api = state["last_seen_api"] if "last_seen_api" in state.keys() else None
    conn.execute(
        """INSERT INTO player_daily_metrics (player_tag, metric_date, exp_level,
               trophies, best_trophies, clan_rank, donations_week,
               donations_received_week, last_seen_api)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(player_tag, metric_date) DO UPDATE SET
               exp_level = excluded.exp_level, trophies = excluded.trophies,
               best_trophies = excluded.best_trophies, clan_rank = excluded.clan_rank,
               donations_week = excluded.donations_week,
               donations_received_week = excluded.donations_received_week,
               last_seen_api = COALESCE(excluded.last_seen_api, player_daily_metrics.last_seen_api)""",
        (
            tag, date_chicago, state["exp_level"], state["trophies"],
            state["best_trophies"], state["clan_rank"], state["donations_week"],
            state["donations_received_week"], last_seen_api,
        ),
    )


def refresh_clan_rollups(conn, clan_payload, date_chicago, observed_at):
    """Upsert the day's clan_daily_metrics row from a clan payload (raw_json
    column is gone — L1 owns raw). joins/leaves counters are maintained by the
    roster emitter's events; here they carry forward within the day."""
    c = clan_payload or {}
    members = c.get("memberList") or []
    trophies = [m.get("trophies") or 0 for m in members]
    conn.execute(
        """INSERT INTO clan_daily_metrics (metric_date, clan_tag, clan_name,
               member_count, open_slots, clan_score, clan_war_trophies,
               required_trophies, donations_per_week_requirement,
               weekly_donations_total, total_member_trophies,
               avg_member_trophies, top_member_trophies, observed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(clan_tag, metric_date) DO UPDATE SET
               clan_name = excluded.clan_name,
               member_count = excluded.member_count,
               open_slots = excluded.open_slots,
               clan_score = excluded.clan_score,
               clan_war_trophies = excluded.clan_war_trophies,
               required_trophies = excluded.required_trophies,
               donations_per_week_requirement = excluded.donations_per_week_requirement,
               weekly_donations_total = excluded.weekly_donations_total,
               total_member_trophies = excluded.total_member_trophies,
               avg_member_trophies = excluded.avg_member_trophies,
               top_member_trophies = excluded.top_member_trophies,
               observed_at = excluded.observed_at""",
        (
            date_chicago, canon_tag(c.get("tag") or ""), c.get("name"),
            len(members), max(0, 50 - len(members)), c.get("clanScore"),
            c.get("clanWarTrophies"), c.get("requiredTrophies"),
            c.get("donationsPerWeek"), sum(m.get("donations") or 0 for m in members),
            sum(trophies), round(sum(trophies) / len(trophies), 2) if trophies else None,
            max(trophies) if trophies else None, observed_at,
        ),
    )


# -------------------------------------------------- management metric inputs

def refresh_management_inputs(conn, player_tag, now=None):
    """Recompute member_management's evidence/metric columns (schema.md §6.3).
    Evaluator *states* are management.py's job (runtime.md §2 step 5)."""
    tag = canon_tag(player_tag)
    now = now or utcnow()
    membership = conn.execute(
        """SELECT joined_at FROM clan_memberships
           WHERE player_tag = ? AND left_at IS NULL ORDER BY joined_at DESC LIMIT 1""",
        (tag,),
    ).fetchone()
    if not membership:
        return  # management rows exist only for members (open membership)
    tenure_days = conn.execute(
        "SELECT CAST(julianday(?) - julianday(?) AS INTEGER)",
        (now[:10], membership["joined_at"][:10]),
    ).fetchone()[0]
    role_row = conn.execute(
        "SELECT role FROM player_current_state WHERE player_tag = ?", (tag,)
    ).fetchone()
    donations = conn.execute(
        """SELECT AVG(donations_week) FROM (
               SELECT donations_week FROM player_daily_metrics
               WHERE player_tag = ? AND strftime('%w', metric_date) = '0'
                 AND donations_week IS NOT NULL
               ORDER BY metric_date DESC LIMIT 4)""",
        (tag,),
    ).fetchone()[0]
    fame = conn.execute(
        """SELECT AVG(season_fame) FROM (
               SELECT SUM(fame) AS season_fame FROM war_participation
               WHERE player_tag = ?
                 AND season_id IN (SELECT season_id FROM war_seasons
                                   WHERE ended_at IS NOT NULL
                                   ORDER BY season_id DESC LIMIT 3)
               GROUP BY season_id)""",
        (tag,),
    ).fetchone()[0]
    attendance = conn.execute(
        """SELECT CAST(SUM(decks_used) AS REAL) / NULLIF(SUM(decks_available), 0)
           FROM war_attendance_days
           WHERE player_tag = ? AND observed_at >= strftime('%Y-%m-%dT%H:%M:%S', ?, '-28 days')""",
        (tag, now),
    ).fetchone()[0]
    battle_days = conn.execute(
        "SELECT battle_time FROM battle_events WHERE player_tag = ? "
        "AND observed_at >= strftime('%Y-%m-%dT%H:%M:%S', ?, '-28 days')",
        (tag, now),
    ).fetchall()
    days = {
        chicago_date_for_cr_timestamp(r["battle_time"])
        for r in battle_days
        if chicago_date_for_cr_timestamp(r["battle_time"])
    }
    conn.execute(
        """INSERT INTO member_management (player_tag, computed_at, week_anchor,
               tenure_days, role, donations_4wk_avg, war_fame_3season_avg,
               war_attendance_rate, battle_days_last_28)
           VALUES (?, ?, COALESCE((SELECT week_anchor FROM member_management
                                   WHERE player_tag = ?), ?), ?, ?, ?, ?, ?, ?)
           ON CONFLICT(player_tag) DO UPDATE SET
               computed_at = excluded.computed_at,
               tenure_days = excluded.tenure_days, role = excluded.role,
               donations_4wk_avg = excluded.donations_4wk_avg,
               war_fame_3season_avg = excluded.war_fame_3season_avg,
               war_attendance_rate = excluded.war_attendance_rate,
               battle_days_last_28 = excluded.battle_days_last_28""",
        (
            tag, now, tag, now[:10], tenure_days,
            role_row["role"] if role_row else None,
            round(donations, 2) if donations is not None else None,
            round(fame, 2) if fame is not None else None,
            round(attendance, 4) if attendance is not None else None,
            len(days),
        ),
    )
