"""The player Pulse — broad-window stream commentary (docs/reference/v5.1/pulse.md).

Three 8-hour windows a day, anchored tiles ending ~04:00/12:00/20:00 CT, each
narrating the player/battle streams' shape for a different third of the
globe's active hours. The job is a cheap 30-minute check: when a window is
complete it builds a facts JSON from rows strictly inside (anchor, anchor+8h],
claims `pulse:player:{window_end}` in the recognition ledger (idempotent),
and raises ONE communication intent on the battle-feed lane — the engine
tick's delivery then composes (Sonnet 5), Editor-gates, and posts it like any
other intent (the single-pipeline rule; a pulse is not a side channel).

Anchor state is restart-proof: it rides in the persisted
runtime_job_status.player_pulse row (`anchor=<iso>` inside last_summary) and
advances by exactly 8h per posted window — drift-free tiling even across
missed checks. After downtime longer than one window, skipped windows advance
silently and only the most recent COMPLETE window posts (a stale pulse reads
as bot lag, not delight; the ledger keys make double-posts impossible
regardless).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("elixir.player_pulse")

JOB_NAME = "player_pulse"
ANCHOR_MARKER = "anchor="
WINDOW_HOURS = 8
LANE = "battle-feed"
INTENT_TYPE = "pulse:player_stream"

# Window boundaries land at these UTC hours = 04:00 / 12:00 / 20:00 CT under
# CDT (UTC-5). Hardcoding CDT means a one-hour seasonal drift under CST —
# accepted: the tiles stay exact 8h regardless; only the label shifts an hour.
GRID_HOURS_UTC = (1, 9, 17)

QUIET_WINDOW_BATTLES = 15   # below this the ask licenses two honest sentences
STANDOUT_MIN_BATTLES = 6
SPOTLIGHT_MIN_SCORE = 3

# CT-hour window labels (timezone texture, spec §3): keyed by window END hour UTC.
WINDOW_LABELS = {
    9: "the overnight window — quiet US hours, Asia-Pacific daytime, EU morning",
    17: "the midday window — US morning, EU afternoon and evening",
    1: "the evening window — US afternoon and evening, EU late night, Asia early morning",
}


# ------------------------------------------------------------------ time math

def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compact(dt: datetime) -> str:
    """battle_time comparison form (CR-compact: 20260705T040000...)."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")


def seed_anchor(now: datetime) -> datetime:
    """The most recent grid boundary <= now. The first pulse then fires one
    full window later, covering exactly one tile."""
    now = now.astimezone(timezone.utc)
    candidates = []
    for day_off in (0, -1):
        day = (now + timedelta(days=day_off)).date()
        for h in GRID_HOURS_UTC:
            b = datetime(day.year, day.month, day.day, h, tzinfo=timezone.utc)
            if b <= now:
                candidates.append(b)
    return max(candidates)


def read_anchor(conn) -> datetime | None:
    """The persisted anchor. Lives in stream_cursors (the engine's consumer-
    position store - the pulse IS a stream consumer). The original design
    piggybacked runtime_job_status.last_summary, but the harness re-creates
    that row on restart (live 2026-07-05: a restart wiped the anchor and the
    re-seed silently skipped the first-ever window)."""
    row = conn.execute(
        "SELECT cursor_text FROM stream_cursors WHERE consumer_key = ? AND scope_key = ?",
        (JOB_NAME, "anchor"),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return _parse_iso(row[0])
    except ValueError:
        return None


def write_anchor(conn, anchor: datetime) -> None:
    conn.execute(
        """INSERT INTO stream_cursors (consumer_key, scope_key, cursor_text, updated_at)
           VALUES (?, 'anchor', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
           ON CONFLICT(consumer_key, scope_key) DO UPDATE SET
               cursor_text = excluded.cursor_text, updated_at = excluded.updated_at""",
        (JOB_NAME, _iso(anchor)),
    )


# ------------------------------------------------------------- facts builders

def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _member_names(conn) -> dict[str, str]:
    return {
        r["player_tag"]: r["current_name"]
        for r in conn.execute(
            """SELECT p.player_tag, p.current_name
               FROM clan_memberships cm JOIN players p USING (player_tag)
               WHERE cm.left_at IS NULL"""
        ).fetchall()
    }


def _battle_aggregates(conn, members: dict, start_c: str, end_c: str) -> dict:
    battles = _rows(conn, """
        SELECT player_tag, battle_time, mode_group, game_mode_name, outcome,
               crowns_for, crowns_against, trophy_change, teammate_tag,
               dedup_key, is_competitive
        FROM battle_events
        WHERE battle_time > ? AND battle_time <= ?""", (start_c, end_c))
    battles = [b for b in battles if b["player_tag"] in members]
    mode_mix: dict[str, int] = {}
    by_hour: dict[str, int] = {}
    per_player: dict[str, dict] = {}
    for b in battles:
        mode_mix[b["mode_group"]] = mode_mix.get(b["mode_group"], 0) + 1
        hour = b["battle_time"][9:11]
        by_hour[hour] = by_hour.get(hour, 0) + 1
        s = per_player.setdefault(b["player_tag"], {"battles": 0, "wins": 0})
        s["battles"] += 1
        s["wins"] += 1 if b["outcome"] == "W" else 0
    return {"battles": battles, "mode_mix": mode_mix, "by_hour": by_hour,
            "per_player": per_player}


def _standouts(conn, members: dict, per_player: dict, limit: int = 3) -> list[dict]:
    ranked = sorted(
        (
            # Explicit wins/losses/record so neither composer nor critic has
            # to interpret notation (live 2026-07-05: the critic misread a
            # correct "5-4" W-L as contradicting "5 wins of 9 battles" and
            # coerced worse copy into fallback).
            {"tag": t, "name": members[t], **s,
             "losses": s["battles"] - s["wins"],
             "record": f"{s['wins']}W-{s['battles'] - s['wins']}L in {s['battles']} battles"}
            for t, s in per_player.items()
            if s["battles"] >= STANDOUT_MIN_BATTLES
        ),
        key=lambda r: (-r["wins"], -r["battles"]),
    )[:limit]
    for r in ranked:
        try:
            from engine.profiles import player_mode_profile

            identity = player_mode_profile(conn, r["tag"]).get("identity")
            if identity and identity not in ("quiet",):
                r["identity"] = identity
        except Exception:
            pass
        r.pop("tag", None)
    return ranked


def _off_peak_carrier(members: dict, agg: dict) -> dict | None:
    """Who kept the lights on in the window's quiet hours (battles in hours
    with <= 2 total). The global-clan story the spec names."""
    quiet_hours = {h for h, n in agg["by_hour"].items() if n <= 2}
    if not quiet_hours:
        return None
    counts: dict[str, int] = {}
    for b in agg["battles"]:
        if b["battle_time"][9:11] in quiet_hours:
            counts[b["player_tag"]] = counts.get(b["player_tag"], 0) + 1
    if not counts:
        return None
    tag = max(counts, key=lambda t: counts[t])
    return {"name": members[tag], "battles_in_quiet_hours": counts[tag]}


def _player_events(conn, members: dict, start: str, end: str, limit: int = 6) -> list[dict]:
    """In-window player moments that recognition did NOT already post — the
    pulse nods at quiet achievements; re-announcing celebrated ones is
    recycled news (the Editor's freshness gate proved this on the smoke run)."""
    posted = set()
    for r in _rows(conn, """
        SELECT recognition_key FROM recognition_ledger
        WHERE intent_id IS NOT NULL
          AND claimed_at >= strftime('%Y-%m-%dT%H:%M:%S', ?, '-1 day')""", (start,)):
        parts = r["recognition_key"].split(":")
        if len(parts) >= 2:
            posted.add((parts[0], parts[1]))
    rows = _rows(conn, """
        SELECT pe.event_type, pe.player_tag, pe.payload_json
        FROM player_events pe
        WHERE pe.observed_at > ? AND pe.observed_at <= ? AND pe.scope = 'public'
        ORDER BY pe.observed_at DESC LIMIT ?""", (start, end, limit))
    rows = [r for r in rows if (r["event_type"], r["player_tag"]) not in posted]
    out = []
    for r in rows:
        if r["player_tag"] not in members:
            continue
        try:
            p = json.loads(r["payload_json"] or "{}")
        except (TypeError, ValueError):
            p = {}
        detail = {k: p[k] for k in ("level", "milestone", "card_name", "league",
                                    "boundary", "badge_name", "arena_name")
                  if p.get(k) is not None}
        out.append({"event": r["event_type"], "name": members[r["player_tag"]],
                    **detail})
    return out


def _ranked_activity(conn, members: dict, per_mode_battles: list[dict]) -> list[dict]:
    ranked_counts: dict[str, dict] = {}
    for b in per_mode_battles:
        if b["mode_group"] != "ranked":
            continue
        s = ranked_counts.setdefault(b["player_tag"], {"battles": 0, "wins": 0})
        s["battles"] += 1
        s["wins"] += 1 if b["outcome"] == "W" else 0
    if not ranked_counts:
        return []
    out = []
    for tag, s in sorted(ranked_counts.items(), key=lambda kv: -kv[1]["battles"])[:3]:
        row = conn.execute(
            "SELECT ranked_league, ranked_trophies FROM player_current_state "
            "WHERE player_tag = ?", (tag,)
        ).fetchone()
        entry = {"name": members[tag], **s}
        if row and row["ranked_league"]:
            entry["league_number"] = row["ranked_league"]
            entry["current_rating"] = row["ranked_trophies"]
        out.append(entry)
    return out


def _arena_up_refs(conn, start: str, end: str) -> set[str]:
    """dedup_keys of battles cited by arena_up claims in this window — those
    battles are deciders (spotlight bonus)."""
    refs: set[str] = set()
    for r in _rows(conn, """
        SELECT event_refs_json FROM recognition_ledger
        WHERE recognition_key LIKE 'arena_up:%'
          AND claimed_at > ? AND claimed_at <= ?""", (start, end)):
        try:
            refs.update(json.loads(r["event_refs_json"] or "{}").get("refs") or [])
        except (TypeError, ValueError):
            continue
    return refs


def spotlight_battle(members: dict, battles: list[dict], decider_refs: set[str]) -> dict | None:
    """The window's single coolest battle, deterministically scored
    (pulse.md §3): sweeps, big swings, arena-up deciders, event-mode wins,
    clan-duo 2v2 wins."""
    best, best_key = None, (0,)
    for b in battles:
        score = 0
        why = []
        win = b["outcome"] == "W"
        cf, ca = b.get("crowns_for") or 0, b.get("crowns_against") or 0
        if win and cf == 3 and ca == 0:
            score += 3
            why.append("three-crown sweep")
        elif win and cf == 3:
            score += 2
            why.append("three crowns")
        swing = abs(b.get("trophy_change") or 0)
        if swing >= 40:
            score += 3
            why.append(f"{'+' if (b.get('trophy_change') or 0) > 0 else '-'}{swing} trophy swing")
        if b["dedup_key"] in decider_refs:
            score += 5
            why.append("the battle that clinched a new arena")
        if win and b["mode_group"] == "special_event":
            score += 2
            why.append(f"event-mode win ({b.get('game_mode_name')})")
        if win and b.get("teammate_tag") in members:
            score += 3
            why.append(f"2v2 win alongside {members[b['teammate_tag']]}")
        if win:
            score += 1
        key = (score, b["battle_time"])  # ties → the later battle
        if score >= SPOTLIGHT_MIN_SCORE and key > best_key:
            best_key = key
            best = {
                "name": members[b["player_tag"]],
                "mode": b.get("game_mode_name") or b["mode_group"],
                "crowns": f"{cf}-{ca}",
                "trophy_change": b.get("trophy_change"),
                "why": ", ".join(why),
            }
            if b.get("teammate_tag") in members:
                best["teammate"] = members[b["teammate_tag"]]
    return best


def _accruing_quietly(conn, members: dict, start: str, end: str, limit: int = 3) -> list[dict]:
    out = []
    for r in _rows(conn, """
        SELECT recognition_key, event_refs_json FROM recognition_ledger
        WHERE claimed_at > ? AND claimed_at <= ? AND intent_id IS NULL
          AND recognition_key NOT LIKE 'pulse:%'
          AND event_refs_json LIKE '%suppressed%'
        ORDER BY claimed_at DESC LIMIT 10""", (start, end)):
        parts = r["recognition_key"].split(":")
        if len(parts) < 2:
            continue
        kind, tag = parts[0], parts[1]
        if tag in members:
            out.append({"name": members[tag], "moment": kind.replace("_", " ")})
        if len(out) >= limit:
            break
    return out


def _recently_featured(conn, limit_pulses: int = 2) -> list[str]:
    """Names fed as feature candidates in the previous pulses (P3 rotation
    fairness — the ask prefers fresh names)."""
    names: list[str] = []
    for r in _rows(conn, """
        SELECT payload_json FROM communication_intents
        WHERE intent_type = ? ORDER BY intent_id DESC LIMIT ?""",
                   (INTENT_TYPE, limit_pulses)):
        try:
            p = json.loads(r["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        for n in p.get("featured_candidates") or []:
            if n not in names:
                names.append(n)
    return names


def build_window_facts(conn, window_start: datetime, window_end: datetime) -> dict:
    """The pulse facts JSON — every value from rows strictly inside
    (window_start, window_end]."""
    start, end = _iso(window_start), _iso(window_end)
    start_c, end_c = _compact(window_start), _compact(window_end)
    members = _member_names(conn)
    agg = _battle_aggregates(conn, members, start_c, end_c)
    standouts = _standouts(conn, members, agg["per_player"])
    events = _player_events(conn, members, start, end)
    spotlight = spotlight_battle(members, agg["battles"],
                                 _arena_up_refs(conn, start, end))
    facts = {
        "event_type": "player_pulse",
        "window_start": start,
        "window_end": end,
        "window_label": WINDOW_LABELS.get(window_end.astimezone(timezone.utc).hour,
                                          "the last eight hours"),
        "battles_total": len(agg["battles"]),
        "active_players": len(agg["per_player"]),
        "mode_mix": dict(sorted(agg["mode_mix"].items(), key=lambda kv: -kv[1])),
        "standouts": standouts,
        "off_peak_carrier": _off_peak_carrier(members, agg),
        "player_moments": events,
        "ranked_activity": _ranked_activity(conn, members, agg["battles"]),
        "battle_spotlight": spotlight,
        "accruing_quietly": _accruing_quietly(conn, members, start, end),
        "recently_featured": _recently_featured(conn),
        "quiet_window": len(agg["battles"]) < QUIET_WINDOW_BATTLES,
    }
    featured = [s["name"] for s in standouts]
    if spotlight:
        featured.append(spotlight["name"])
    featured += [e["name"] for e in events]
    facts["featured_candidates"] = list(dict.fromkeys(featured))[:6]
    return {k: v for k, v in facts.items() if v not in (None, [], {})}


# ------------------------------------------------------------------ the check

def run_check(conn, now: datetime | None = None) -> str:
    """The 30-minute check. Returns the status summary (always carrying
    `anchor=<iso>` so the tiling survives restarts). Commits its own writes."""
    from engine import delivery
    from engine.recognition import ledger

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    anchor = read_anchor(conn)
    if anchor is None:
        anchor = seed_anchor(now)
        write_anchor(conn, anchor)
        conn.commit()
        log.info("player pulse anchor seeded at %s", _iso(anchor))
        return f"seeded {ANCHOR_MARKER}{_iso(anchor)}"

    window = timedelta(hours=WINDOW_HOURS)
    if now < anchor + window:
        return f"waiting {ANCHOR_MARKER}{_iso(anchor)}"

    # Advance over any backlog; only the most recent COMPLETE window posts.
    skipped = 0
    while now >= anchor + 2 * window:
        anchor += window
        skipped += 1
    window_start, window_end = anchor, anchor + window

    key = f"pulse:player:{_iso(window_end)}"
    posted = "already-claimed"
    if ledger.claim(conn, key, "player", [], 0):
        facts = build_window_facts(conn, window_start, window_end)
        intent_id = delivery.raise_intent(
            conn, key, INTENT_TYPE, LANE, "public", facts, _iso(now)
        )
        ledger.attach_intent(conn, key, intent_id)
        posted = f"intent {intent_id} ({facts.get('battles_total', 0)} battles)"
    anchor = window_end
    write_anchor(conn, anchor)
    conn.commit()
    note = f" skipped={skipped}" if skipped else ""
    return f"window {_iso(window_end)} {posted}{note} {ANCHOR_MARKER}{_iso(anchor)}"
