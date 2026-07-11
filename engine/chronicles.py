"""Season chronicles — ranked-and-profiles.md D7 (ratified 2026-07-04).

Every bounded event that closes (war season, ranked season) leaves ONE
durable chronicle memory: the season's arc in grounded prose, tagged
`chronicle` + the season id, kind synthesis. The structured tables stay the
queryable skeleton; the chronicle is the narrative layer that ranked
retrieval + FTS surface when anyone asks about past seasons.

Grounding discipline: the prose is assembled deterministically from the
season's rows — every number and name comes from a query. There is NO LLM
call in the close path: close_season runs inside the engine tick's write
transaction, and holding that lock across a network call is the exact hazard
the 2026-07-04 delivery fix removed. (The spec's "LLM with deterministic
fallback" is therefore inverted by design here: deterministic at close;
`compose_chronicle` is importable for richer offline rewrites later.)
Failure never propagates — a lost chronicle must never lose a season close.
"""

from __future__ import annotations

import logging

log = logging.getLogger("engine.chronicles")


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _name(conn, tag):
    row = conn.execute(
        "SELECT current_name FROM players WHERE player_tag = ?", (tag,)
    ).fetchone()
    return (row[0] if row else None) or tag


def _war_chronicle(conn, season_id: int) -> str | None:
    season = conn.execute(
        "SELECT * FROM war_seasons WHERE season_id = ?", (season_id,)
    ).fetchone()
    if season is None:
        return None
    weeks = _rows(conn, """
        SELECT section_index, period_type, our_rank, our_fame
        FROM war_weeks WHERE season_id = ? ORDER BY section_index""", (season_id,))
    standings = _rows(conn, """
        SELECT player_tag, SUM(COALESCE(fame,0)) AS points
        FROM war_participation WHERE season_id = ?
        GROUP BY player_tag HAVING points > 0 ORDER BY points DESC LIMIT 5""",
        (season_id,))
    awards = _rows(conn, """
        SELECT award_type, player_tag, rank, metric_value, metric_unit
        FROM awards WHERE season_id = ? AND award_type != 'war_participant'
        ORDER BY award_type, rank""", (season_id,))
    joins = _rows(conn, """
        SELECT cm.player_tag FROM clan_memberships cm
        JOIN war_seasons s ON s.season_id = ?
        WHERE cm.joined_at >= s.started_at
          AND (s.ended_at IS NULL OR cm.joined_at <= s.ended_at)""", (season_id,))

    ranked_weeks = [w for w in weeks if w["our_rank"] is not None]
    firsts = sum(1 for w in ranked_weeks if w["our_rank"] == 1)
    head = f"War season {season_id}: {len(weeks)} weeks"
    if season["final_rank"] is not None:
        head += f", final rank #{season['final_rank']}"
    parts = [head + "."]
    if ranked_weeks:
        placings = ", ".join(
            f"week {w['section_index']} #{w['our_rank']} ({w['our_fame']} fame)"
            for w in ranked_weeks)
        parts.append(f"Weekly placings: {placings}.")
        if firsts == len(ranked_weeks) and firsts > 1:
            parts.append(f"Every ranked week finished first — {firsts} straight.")
    if season["war_champ_tag"]:
        champ = _name(conn, season["war_champ_tag"])
        top_points = standings[0]["points"] if standings else None
        parts.append(f"War Champ: {champ}"
                     + (f" with {top_points} season points" if top_points else "") + ".")
        fp = season["free_pass_tag"]
        if fp and fp != season["war_champ_tag"]:
            parts.append(f"The Free Pass rotated to {_name(conn, fp)} "
                         "(no repeat seasons).")
        elif fp:
            parts.append("They keep the Free Pass (no rotation due).")
        if len(standings) > 1:
            others = ", ".join(f"{_name(conn, s['player_tag'])} ({s['points']})"
                               for s in standings[1:4])
            parts.append(f"Top contributors behind them: {others}.")
    elif standings:  # season not yet closed (or champ unknown): keep the leader
        tops = ", ".join(f"{_name(conn, s['player_tag'])} ({s['points']})"
                         for s in standings[:4])
        parts.append(f"Top contributors: {tops}.")
    extras = [a for a in awards if a["award_type"] not in ("war_champ", "free_pass")]
    if extras:
        by_type: dict[str, list] = {}
        for a in extras:
            by_type.setdefault(a["award_type"], []).append(a)
        bits = []
        for t, rows_ in by_type.items():
            names = ", ".join(_name(conn, a["player_tag"]) for a in rows_[:3])
            bits.append(f"{t.replace('_', ' ')}: {names}")
        parts.append("Season honors — " + "; ".join(bits) + ".")
    if joins:
        names = ", ".join(_name(conn, j["player_tag"]) for j in joins[:5])
        parts.append(f"Joined during the season: {names}.")
    return " ".join(parts)


def _ranked_chronicle(conn, season_id: str) -> str | None:
    from engine.pol_seasons import podium

    season = conn.execute(
        "SELECT * FROM pol_seasons WHERE pol_season_id = ?", (season_id,)
    ).fetchone()
    if season is None:
        return None
    pod = podium(conn, season_id)
    players = _rows(conn, """
        SELECT r.player_tag, r.league, r.rating, r.battles, r.wins
        FROM pol_season_results r
        JOIN clan_memberships cm ON cm.player_tag = r.player_tag AND cm.left_at IS NULL
        WHERE r.pol_season_id = ? AND r.battles > 0
        ORDER BY r.league DESC, COALESCE(r.rating,0) DESC""", (season_id,))
    if not players:
        return (f"Ranked season {season_id} closed with no clan members "
                "active in Ranked.")
    parts = [f"Ranked season {season_id}: {len(players)} members played Ranked."]
    if pod:
        from engine.normalize import ranked_league_name

        lead = pod[0]
        line = (f"{lead['name']} led the clan — {ranked_league_name(lead['league'])}"
                + (f" at {lead['rating']} rating" if lead["rating"] else "")
                + (f", {lead['wins']}W/{lead['battles'] - lead['wins']}L"
                   if lead["battles"] else "") + ".")
        parts.append(line)
        if len(pod) > 1:
            rest = "; ".join(
                f"#{e['rank']} {e['name']} ({e['league_name']}"
                + (f", {e['rating']}" if e["rating"] else "") + ")"
                for e in pod[1:])
            parts.append(f"Podium: {rest}.")
    total_battles = sum(p["battles"] or 0 for p in players)
    total_wins = sum(p["wins"] or 0 for p in players)
    parts.append(f"Clan Ranked volume: {total_battles} battles, "
                 f"{total_wins} wins across the season.")
    return " ".join(parts)


def compose_chronicle(conn, kind: str, season_id) -> str | None:
    """Deterministic season prose — every fact from a row. Pure read."""
    if kind == "war":
        return _war_chronicle(conn, int(season_id))
    if kind == "ranked":
        return _ranked_chronicle(conn, str(season_id))
    raise ValueError(f"unknown chronicle kind: {kind}")


def write_season_chronicle(conn, kind: str, season_id, observed_at: str) -> int | None:
    """Compose + persist the chronicle memory. Idempotent (one chronicle per
    (kind, season): a prior chronicle memory short-circuits). Never raises."""
    try:
        tag_key = f"{kind}-season-{season_id}"
        existing = conn.execute(
            """SELECT 1 FROM memories m JOIN memory_tags t ON t.memory_id = m.memory_id
               WHERE t.tag = ? LIMIT 1""",
            (tag_key,),
        ).fetchone()
        if existing:
            return None
        body = compose_chronicle(conn, kind, season_id)
        if not body:
            return None
        import memory_store

        label = "War" if kind == "war" else "Ranked"
        mem = memory_store.create_memory(
            body=body,
            source_type="elixir_synthesis",
            is_inference=False,
            confidence=0.95,
            created_by="engine.chronicles",
            scope="public",
            title=f"{label} season {season_id} — chronicle",
            summary=body[:200],
            metadata={"kind": kind, "season_id": str(season_id)},
            conn=conn,
        )
        memory_store.attach_tags(
            mem["memory_id"], ["chronicle", tag_key],
            actor="engine.chronicles", conn=conn,
        )
        log.info("chronicle written: %s season %s (memory %s)",
                 kind, season_id, mem["memory_id"])
        return mem["memory_id"]
    except Exception as exc:
        log.exception("chronicle failed for %s season %s", kind, season_id)
        from storage.incidents import record_incident
        record_incident("chronicles.write", exc,
                        context={"kind": kind, "season_id": season_id}, conn=conn)
        return None
