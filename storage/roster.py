from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional


_NON_FOLD_CHARS = re.compile(r"[^a-z0-9 ]+")
_FOLD_WHITESPACE = re.compile(r"\s+")


def _hours_since_iso(value: Optional[str]) -> Optional[float]:
    """Hours between an ISO/compact timestamp and now (UTC), or None if
    unparseable. Used for freshness/staleness stamps (QA L4)."""
    if not value:
        return None
    from engine.normalize import parse_cr_time
    dt = parse_cr_time(value)
    if dt is None:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)

# v5.1: "active member" = has an open clan_memberships row (§7); the old
# members.status column is gone. `m` must alias players in the outer query.
_ACTIVE = (
    "EXISTS (SELECT 1 FROM clan_memberships cm "
    "WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL)"
)


def _fold_for_search(value: str) -> str:
    """Aggressively normalize a name for fuzzy matching.

    NFKD compatibility decomposition unwinds fullwidth Latin (Ｓ→S),
    superscripts/subscripts (²⁸→28), ligatures (ﬁ→fi), and similar
    compatibility characters. Combining marks are stripped (José→jose),
    then anything that isn't a letter/digit/space is dropped so emoji,
    hearts, lightning bolts, hyphens, and the variation-selector tail on
    emoji all fold away. Whitespace is collapsed.

    "²⁸"→"28", "Ｓｈａｆｉｔｈ Ｎｉｈａｌ♥️"→"shafith nihal", "L-Drxgo⚡"→"ldrxgo",
    "José"→"jose". Used on both the query and the stored fields inside
    resolve_member so searches are unicode-tolerant.
    """
    nfkd = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in nfkd if unicodedata.category(ch) != "Mn").lower()
    stripped = _NON_FOLD_CHARS.sub("", stripped)
    return _FOLD_WHITESPACE.sub(" ", stripped).strip()


def pick_best_match(matches: list[dict]) -> dict | None:
    """Pick the single best candidate from a resolve_member result list.

    Accepts a high-confidence exact match (score >= 850) when there's only one,
    or the top match when it outscores second place by 100 points, or the only
    candidate when there's one. Returns None when the result is genuinely
    ambiguous so callers can present disambiguation to the user.
    """
    if not matches:
        return None
    exactish = [m for m in matches if m.get("match_score", 0) >= 850]
    if len(exactish) == 1:
        return exactish[0]
    if len(matches) == 1:
        return matches[0]
    top, second = matches[0], matches[1]
    if (top.get("match_score", 0) - second.get("match_score", 0)) >= 100:
        return top
    return None

from db import (
    _canon_tag,
    _current_joined_at,
    _ensure_member,
    _get_current_membership,
    _rowdicts,
    _utcnow,
    chicago_date_for_utc_timestamp,
    chicago_today,
    managed_connection,
)
from storage._enrichment import _member_reference_fields
from storage.cards import (
    get_member_card_collection,
    get_member_current_deck,
    get_member_signature_cards,
)


def _ensure_last_seen_api_column(conn) -> None:
    """Lazy ALTER: add player_current_state.last_seen_api on live DBs cut before
    the column existed (v5.1 has no forward-migration runner — fresh builds get
    it from schema_v51 NEW_DDL). Idempotent; safe to call every snapshot."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(player_current_state)")}
    if "last_seen_api" not in cols:
        conn.execute("ALTER TABLE player_current_state ADD COLUMN last_seen_api TEXT")


@managed_connection
def snapshot_members(member_list: list[dict], conn: Optional[sqlite3.Connection] = None, *, create_if_missing: bool = True) -> int:
    """Upsert players + player_current_state + player_daily_metrics + open
    memberships from a live clan roster payload.

    v5.1 note: this no longer interferes with join detection — the engine's
    roster emitter diffs against its own state_baselines row, not against
    these read models, so interactive flows may snapshot freely (the old
    'observed'→'active' promotion dance is gone with the status column).
    """
    observed_at = _utcnow()
    today = chicago_date_for_utc_timestamp(observed_at) or chicago_today()
    _ensure_last_seen_api_column(conn)
    seen_tags = set()
    for member in member_list:
        tag = _canon_tag(member.get("tag"))
        if not tag:
            continue
        seen_tags.add(tag)
        name = member.get("name") or ""
        if not create_if_missing:
            existing = conn.execute("SELECT player_tag FROM players WHERE player_tag = ?", (tag,)).fetchone()
            if not existing:
                continue
        _ensure_member(conn, tag, name=name)
        arena = member.get("arena") or {}
        arena_id = arena.get("id") if isinstance(arena, dict) else None
        arena_name = arena.get("name") if isinstance(arena, dict) else str(arena or "")
        state = {
            "observed_at": observed_at,
            "role": member.get("role", "member"),
            "exp_level": member.get("expLevel", member.get("exp_level")),
            "trophies": member.get("trophies", 0),
            "best_trophies": member.get("bestTrophies", member.get("best_trophies")),
            "clan_rank": member.get("clanRank", member.get("clan_rank")),
            "previous_clan_rank": member.get("previousClanRank"),
            "donations_week": member.get("donations", 0),
            "donations_received_week": member.get("donationsReceived", member.get("donations_received", 0)),
            "arena_id": arena_id,
            "arena_name": arena_name,
            # Ingested for roster-badge awareness only — so Elixir knows when a
            # member is wearing the in-game "idle" flag. NOT an engagement signal:
            # battling remains the kick clock (architecture §13.6).
            "last_seen_api": member.get("lastSeen") or member.get("last_seen"),
        }
        conn.execute(
            "INSERT INTO player_current_state (player_tag, observed_at, role, exp_level, trophies, best_trophies, clan_rank, previous_clan_rank, donations_week, donations_received_week, arena_id, arena_name, last_seen_api) "
            "VALUES (:player_tag, :observed_at, :role, :exp_level, :trophies, :best_trophies, :clan_rank, :previous_clan_rank, :donations_week, :donations_received_week, :arena_id, :arena_name, :last_seen_api) "
            "ON CONFLICT(player_tag) DO UPDATE SET observed_at = excluded.observed_at, role = excluded.role, exp_level = excluded.exp_level, trophies = excluded.trophies, best_trophies = excluded.best_trophies, clan_rank = excluded.clan_rank, previous_clan_rank = excluded.previous_clan_rank, donations_week = excluded.donations_week, donations_received_week = excluded.donations_received_week, arena_id = excluded.arena_id, arena_name = excluded.arena_name, last_seen_api = excluded.last_seen_api",
            {"player_tag": tag, **state},
        )
        conn.execute(
            "INSERT INTO player_daily_metrics (player_tag, metric_date, exp_level, trophies, best_trophies, clan_rank, donations_week, donations_received_week, last_seen_api) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(player_tag, metric_date) DO UPDATE SET exp_level = excluded.exp_level, trophies = excluded.trophies, best_trophies = excluded.best_trophies, clan_rank = excluded.clan_rank, donations_week = excluded.donations_week, donations_received_week = excluded.donations_received_week, last_seen_api = excluded.last_seen_api",
            (tag, today, state["exp_level"], state["trophies"], state["best_trophies"], state["clan_rank"], state["donations_week"], state["donations_received_week"], state["last_seen_api"]),
        )
        if create_if_missing and not _get_current_membership(conn, tag):
            # clan_tag defaults to the home clan and FKs clans — guaranteed on
            # migrated DBs (T3) but not fresh ones; ensure defensively.
            conn.execute(
                "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
                "VALUES ('#J2RGCRVG', 'POAP KINGS', ?, ?, 1)",
                (today, today),
            )
            conn.execute(
                "INSERT INTO clan_memberships (player_tag, joined_at, left_at, join_source, leave_source) VALUES (?, ?, NULL, ?, NULL)",
                (tag, today, "clan_api_snapshot"),
            )
    conn.commit()
    return len(seen_tags)


@managed_connection
def snapshot_clan_daily_metrics(clan_data: Optional[dict], conn: Optional[sqlite3.Connection] = None, observed_at: Optional[str] = None) -> str:
    observed_at = observed_at or _utcnow()
    metric_date = chicago_date_for_utc_timestamp(observed_at) or chicago_today()
    clan_tag = _canon_tag((clan_data or {}).get("tag")) or "#J2RGCRVG"
    clan_name = (clan_data or {}).get("name") or "POAP KINGS"
    member_list = (clan_data or {}).get("memberList") or []
    member_count = (clan_data or {}).get("members")
    if not isinstance(member_count, int):
        member_count = len(member_list)
    total_member_trophies = sum((member.get("trophies") or 0) for member in member_list)
    avg_member_trophies = round(total_member_trophies / member_count, 2) if member_count else 0.0
    top_member_trophies = max((member.get("trophies") or 0) for member in member_list) if member_list else 0
    weekly_donations_total = sum((member.get("donations") or 0) for member in member_list)
    joins_today = conn.execute(
        "SELECT COUNT(*) AS cnt FROM clan_memberships WHERE joined_at = ?",
        (metric_date,),
    ).fetchone()["cnt"]
    leaves_today = conn.execute(
        "SELECT COUNT(*) AS cnt FROM clan_memberships WHERE left_at = ?",
        (metric_date,),
    ).fetchone()["cnt"]
    conn.execute(
        "INSERT INTO clan_daily_metrics (metric_date, clan_tag, clan_name, member_count, open_slots, clan_score, clan_war_trophies, required_trophies, donations_per_week_requirement, weekly_donations_total, total_member_trophies, avg_member_trophies, top_member_trophies, joins_today, leaves_today, net_member_change, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(clan_tag, metric_date) DO UPDATE SET clan_name = excluded.clan_name, member_count = excluded.member_count, open_slots = excluded.open_slots, clan_score = excluded.clan_score, clan_war_trophies = excluded.clan_war_trophies, required_trophies = excluded.required_trophies, donations_per_week_requirement = excluded.donations_per_week_requirement, weekly_donations_total = excluded.weekly_donations_total, total_member_trophies = excluded.total_member_trophies, avg_member_trophies = excluded.avg_member_trophies, top_member_trophies = excluded.top_member_trophies, joins_today = excluded.joins_today, leaves_today = excluded.leaves_today, net_member_change = excluded.net_member_change, observed_at = excluded.observed_at",
        (
            metric_date,
            clan_tag,
            clan_name,
            member_count,
            max(0, 50 - member_count),
            (clan_data or {}).get("clanScore"),
            (clan_data or {}).get("clanWarTrophies"),
            (clan_data or {}).get("requiredTrophies"),
            (clan_data or {}).get("donationsPerWeek"),
            weekly_donations_total,
            total_member_trophies,
            avg_member_trophies,
            top_member_trophies,
            joins_today,
            leaves_today,
            joins_today - leaves_today,
            observed_at,
        ),
    )
    conn.commit()
    return metric_date


@managed_connection
def list_clan_daily_metrics(days: int = 30, clan_tag: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    cutoff = (datetime.fromisoformat(chicago_today()) - timedelta(days=max(days - 1, 0))).date().isoformat()
    where = ["metric_date >= ?"]
    params = [cutoff]
    if clan_tag:
        where.append("clan_tag = ?")
        params.append(_canon_tag(clan_tag))
    rows = conn.execute(
        "SELECT metric_date, clan_tag, clan_name, member_count, open_slots, clan_score, clan_war_trophies, required_trophies, donations_per_week_requirement, weekly_donations_total, total_member_trophies, avg_member_trophies, top_member_trophies, joins_today, leaves_today, net_member_change, observed_at "
        f"FROM clan_daily_metrics WHERE {' AND '.join(where)} "
        "ORDER BY metric_date ASC, clan_tag ASC",
        tuple(params),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_active_roster_map(conn: Optional[sqlite3.Connection] = None) -> dict[str, str]:
    rows = conn.execute(
        f"SELECT m.player_tag, m.current_name FROM players m WHERE {_ACTIVE} "
        "ORDER BY m.current_name COLLATE NOCASE"
    ).fetchall()
    return {r["player_tag"]: r["current_name"] for r in rows}


@managed_connection
def get_member_history(tag: str, days: int = 30, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Role/trophy history from first-class events + daily metrics
    (schema.md §9: history stops diffing snapshot pairs)."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    tag = _canon_tag(tag)
    daily = conn.execute(
        "SELECT d.metric_date, d.trophies, d.best_trophies, d.donations_week AS donations, "
        "d.donations_received_week AS donations_received, d.clan_rank "
        "FROM player_daily_metrics d WHERE d.player_tag = ? AND d.metric_date >= ? "
        "ORDER BY d.metric_date ASC",
        (tag, cutoff[:10]),
    ).fetchall()
    events = conn.execute(
        "SELECT event_type, payload_json, observed_at FROM clan_events "
        "WHERE subject_tag = ? AND event_type IN ('role_changed', 'member_joined', 'member_left') "
        "AND observed_at >= ? ORDER BY observed_at ASC",
        (tag, cutoff),
    ).fetchall()
    name_row = conn.execute("SELECT current_name FROM players WHERE player_tag = ?", (tag,)).fetchone()
    name = name_row["current_name"] if name_row else None
    out = []
    for d in daily:
        item = dict(d)
        item.update({"tag": tag, "name": name, "recorded_at": d["metric_date"],
                     "role": None, "arena_id": None, "arena_name": None, "last_seen": None})
        out.append(item)
    for e in events:
        out.append({
            "tag": tag, "name": name, "recorded_at": e["observed_at"],
            "event_type": e["event_type"], "event_payload": e["payload_json"],
        })
    out.sort(key=lambda item: str(item.get("recorded_at") or ""))
    return out


@managed_connection
def resolve_member(query: str, status: Optional[str] = "active", limit: int = 5, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    query_lower = _fold_for_search(query)
    query_handle = query_lower.lstrip("@")
    # Always try the query as a player tag — _canon_tag uppercases and prepends
    # '#'. Real player tags are 8-10 chars in a restricted Supercell alphabet,
    # so non-tag queries won't false-match against any actual player_tag.
    query_tag = _canon_tag(query)

    status_predicate = f"(? IS NULL OR {_ACTIVE})"
    rows = conn.execute(
        "SELECT m.player_tag AS member_id, m.player_tag, m.current_name, "
        f"CASE WHEN {_ACTIVE} THEN 'active' ELSE 'observed' END AS status, "
        # QA L3: carry the state snapshot time so the mutable stats below
        # (trophies/rank/role) aren't read as live — for authoritative current
        # stats callers should still go to get_member.
        "cs.role, cs.trophies, cs.clan_rank, cs.observed_at, "
        "dl.discord_user_id, du.username AS discord_username, du.display_name AS discord_display_name "
        "FROM players m "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        "LEFT JOIN discord_links dl ON dl.player_tag = m.player_tag AND dl.is_primary = 1 "
        "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
        f"WHERE {status_predicate} "
        "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE",
        (status,),
    ).fetchall()
    aliases = {}
    for row in conn.execute(
        "SELECT player_tag, alias FROM player_aliases"
    ).fetchall():
        aliases.setdefault(row["player_tag"], []).append(row["alias"])

    candidates = []
    for row in rows:
        member = dict(row)
        member["joined_date"] = _current_joined_at(conn, row["player_tag"])
        member["in_discord"] = 1 if row["discord_user_id"] else 0
        member_aliases = aliases.get(row["player_tag"], [])
        score = 0
        source = None

        name = _fold_for_search(member.get("current_name") or "")
        discord_username = _fold_for_search(member.get("discord_username") or "")
        discord_display = _fold_for_search(member.get("discord_display_name") or "")
        alias_lowers = [_fold_for_search(a) for a in member_aliases]

        if query_tag and member["player_tag"] == query_tag:
            score, source = 1000, "player_tag_exact"
        elif name == query_lower:
            score, source = 950, "current_name_exact"
        elif query_lower in alias_lowers:
            score, source = 900, "alias_exact"
        elif discord_username == query_handle:
            score, source = 875, "discord_username_exact"
        elif discord_display == query_lower or discord_display == query_handle:
            score, source = 850, "discord_display_exact"
        elif name.startswith(query_lower):
            score, source = 775, "current_name_prefix"
        elif any(a.startswith(query_lower) for a in alias_lowers):
            score, source = 750, "alias_prefix"
        elif discord_username.startswith(query_handle) and query_handle:
            score, source = 725, "discord_username_prefix"
        elif query_lower in name:
            score, source = 650, "current_name_contains"
        elif any(query_lower in a for a in alias_lowers):
            score, source = 625, "alias_contains"
        elif query_handle and query_handle in discord_username:
            score, source = 600, "discord_username_contains"
        elif query_lower and query_lower in discord_display:
            score, source = 575, "discord_display_contains"

        if score:
            member["match_score"] = score
            member["match_source"] = source
            member["aliases"] = member_aliases
            candidates.append(_member_reference_fields(conn, row["player_tag"], member))

    candidates.sort(
        key=lambda item: (
            -item["match_score"],
            item.get("clan_rank") if item.get("clan_rank") is not None else 999,
            (item.get("current_name") or "").lower(),
        )
    )
    return candidates[:limit]


@managed_connection
def list_members(status: str = "active", conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    predicate = _ACTIVE if status == "active" else "1=1"
    rows = conn.execute(
        "SELECT m.player_tag AS member_id, m.player_tag, m.current_name, "
        f"CASE WHEN {_ACTIVE} THEN 'active' ELSE 'observed' END AS status, "
        "cs.role, cs.trophies, "
        "cs.best_trophies, cs.clan_rank, cs.donations_week, cs.donations_received_week, cs.arena_name, "
        "md.note, md.profile_url, md.cr_account_age_days, md.cr_account_age_years, md.cr_account_age_updated_at, "
        "md.cr_games_per_day, md.cr_games_per_day_window_days, md.cr_games_per_day_updated_at, "
        "md.cr_collection_level, md.cr_collection_level_badge_tier, md.cr_collection_level_badge_max_tier, md.cr_collection_level_updated_at, "
        "md.cr_clan_war_wins, md.cr_battle_wins, md.cr_clan_donations, md.cr_banner_count, md.cr_emote_count, md.cr_profile_badges_updated_at, "
        "dl.discord_user_id, du.username AS discord_username, du.display_name AS discord_display_name "
        "FROM players m "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        "LEFT JOIN player_metadata md ON md.player_tag = m.player_tag "
        "LEFT JOIN discord_links dl ON dl.player_tag = m.player_tag AND dl.is_primary = 1 "
        "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
        f"WHERE {predicate} "
        "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE",
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["joined_date"] = _current_joined_at(conn, row["player_tag"])
        item["in_discord"] = 1 if row["discord_user_id"] else 0
        result.append(_member_reference_fields(conn, row["player_tag"], item))
    return result


@managed_connection
def get_clan_roster_summary(conn: Optional[sqlite3.Connection] = None) -> dict:
    from storage.war import get_current_war_status
    row = conn.execute(
        # Collection Level is the CR 2026 progression metric (expLevel is dead).
        # AVG ignores NULLs — averages only members whose collection level is synced.
        "SELECT COUNT(*) AS active_members, "
        "ROUND(AVG(md.cr_collection_level), 0) AS avg_collection_level, "
        # QA L10: the average only covers members whose collection level is synced;
        # count them so a low denominator isn't mistaken for a clan-wide average.
        "COUNT(md.cr_collection_level) AS collection_level_sample_size, "
        "ROUND(AVG(COALESCE(cs.trophies, 0)), 0) AS avg_trophies, "
        "SUM(COALESCE(cs.donations_week, 0)) AS donations_week_total, "
        "MAX(COALESCE(cs.trophies, 0)) AS top_trophies "
        "FROM players m "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        "LEFT JOIN player_metadata md ON md.player_tag = m.player_tag "
        f"WHERE {_ACTIVE}"
    ).fetchone()
    war = get_current_war_status(conn=conn)
    result = dict(row)
    result["open_slots"] = max(0, 50 - (result["active_members"] or 0))
    # QA L10: flag when the collection-level average omits unsynced members.
    active = result.get("active_members") or 0
    sample = result.get("collection_level_sample_size") or 0
    if sample < active:
        result["avg_collection_level_note"] = (
            f"Averaged over {sample} of {active} members with a synced collection level; "
            "unsynced members are excluded."
        )
    if war:
        result["current_war"] = war
    return result


@managed_connection
def get_member_profile(tag: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    row = conn.execute(
        "SELECT m.player_tag AS member_id, m.player_tag, COALESCE(m.display_name, m.current_name) AS member_name, "
        f"CASE WHEN {_ACTIVE} THEN 'active' ELSE 'observed' END AS status, "
        "cs.observed_at, cs.role, cs.trophies, cs.best_trophies, cs.clan_rank, "
        "cs.donations_week, cs.donations_received_week, cs.arena_name, "
        "md.birth_month, md.birth_day, md.cr_account_age_days, md.cr_account_age_years, md.cr_account_age_updated_at, "
        "md.cr_games_per_day, md.cr_games_per_day_window_days, md.cr_games_per_day_updated_at, "
        "md.cr_collection_level, md.cr_collection_level_badge_tier, md.cr_collection_level_badge_max_tier, md.cr_collection_level_updated_at, "
        "md.cr_clan_war_wins, md.cr_battle_wins, md.cr_clan_donations, md.cr_banner_count, md.cr_emote_count, md.cr_profile_badges_updated_at, "
        "md.profile_url, md.note, md.email, md.email_verified_at, md.email_source, "
        "md.generated_bio AS bio, md.generated_highlight AS profile_highlight, md.generated_profile_updated_at, "
        "dl.discord_user_id, du.username AS discord_username, du.display_name AS discord_display_name, du.last_seen_at AS discord_last_seen_at "
        "FROM players m "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        "LEFT JOIN player_metadata md ON md.player_tag = m.player_tag "
        "LEFT JOIN discord_links dl ON dl.player_tag = m.player_tag AND dl.is_primary = 1 "
        "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
        "WHERE m.player_tag = ?",
        (_canon_tag(tag),),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    # Career stats: the profile baseline (state_baselines) replaces
    # player_profile_snapshots; fields default None when never deep-polled.
    result.update(_career_fields_from_baseline(conn, row["player_tag"]))
    result["joined_date"] = _current_joined_at(conn, row["player_tag"])
    result["membership_summary"] = get_member_membership_summary(tag, conn=conn)
    result["in_discord"] = 1 if row["discord_user_id"] else 0
    _member_reference_fields(conn, row["player_tag"], result)
    recent_form = get_member_recent_form(tag, conn=conn)
    if recent_form:
        # QA L4: stamp how old the recent-form snapshot is so a stale form (a
        # departed / inactive member whose battles stopped days ago) isn't read
        # as current. Age from computed_at; flag stale past 48h.
        age_hours = _hours_since_iso(recent_form.get("computed_at"))
        if age_hours is not None:
            recent_form["form_age_hours"] = round(age_hours, 1)
            recent_form["form_stale"] = age_hours > 48
        result["recent_form"] = recent_form
    deck = get_member_current_deck(tag, conn=conn)
    if deck:
        result["current_deck"] = deck
    cards = get_member_signature_cards(tag, conn=conn)
    if cards:
        result["signature_cards"] = cards
    collection = get_member_card_collection(tag, limit=12, conn=conn)
    if collection:
        result["card_collection_summary"] = collection.get("summary")
    return result


_CAREER_FIELD_DEFAULTS = {
    "player_profile_at": None,
    "career_wins": None,
    "career_losses": None,
    "career_battle_count": None,
    "career_total_donations": None,
    "war_day_wins": None,
    "challenge_max_wins": None,
    "three_crown_wins": None,
    "current_favourite_card_name": None,
    "current_path_of_legend_season_result_json": None,
    "last_path_of_legend_season_result_json": None,
    "best_path_of_legend_season_result_json": None,
    "progress_json": None,
}


def _career_fields_from_baseline(conn, player_tag: str) -> dict:
    import json as _json

    out = dict(_CAREER_FIELD_DEFAULTS)
    row = conn.execute(
        "SELECT payload_json, observed_at FROM state_baselines "
        "WHERE entity_kind = 'player' AND entity_tag = ? AND aspect = 'profile'",
        (player_tag,),
    ).fetchone()
    if not row:
        return out
    try:
        payload = _json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return out
    out["player_profile_at"] = row["observed_at"]
    out["career_wins"] = payload.get("wins")
    out["career_losses"] = payload.get("losses")
    out["career_battle_count"] = payload.get("battle_count", payload.get("battleCount"))
    out["career_total_donations"] = payload.get("total_donations", payload.get("totalDonations"))
    out["war_day_wins"] = payload.get("war_day_wins", payload.get("warDayWins"))
    out["challenge_max_wins"] = payload.get("challenge_max_wins", payload.get("challengeMaxWins"))
    out["three_crown_wins"] = payload.get("three_crown_wins", payload.get("threeCrownWins"))
    out["current_favourite_card_name"] = payload.get("favourite_card", payload.get("currentFavouriteCard", {}).get("name") if isinstance(payload.get("currentFavouriteCard"), dict) else None)
    return out


@managed_connection
def get_member_membership_summary(tag: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    row = conn.execute(
        "SELECT player_tag, current_name FROM players WHERE player_tag = ?",
        (_canon_tag(tag),),
    ).fetchone()
    if not row:
        return None
    memberships = conn.execute(
        """
        SELECT membership_id, joined_at, left_at, join_source, leave_source
        FROM clan_memberships
        WHERE player_tag = ?
        ORDER BY joined_at ASC, membership_id ASC
        """,
        (row["player_tag"],),
    ).fetchall()
    if not memberships:
        return {
            "player_tag": row["player_tag"],
            "member_name": row["current_name"],
            "join_count": 0,
            "prior_stints": 0,
            "is_returning": False,
            "current_joined_at": None,
            "first_joined_at": None,
            "last_left_at": None,
            "memberships": [],
        }
    current = _get_current_membership(conn, row["player_tag"])
    current_membership_id = current["membership_id"] if current else None
    prior_stints = sum(
        1
        for membership in memberships
        if membership["left_at"] or (
            current_membership_id is not None
            and membership["membership_id"] != current_membership_id
        )
    )
    last_left = None
    for membership in memberships:
        if membership["left_at"]:
            last_left = membership["left_at"]
    items = [dict(membership) for membership in memberships]
    return {
        "player_tag": row["player_tag"],
        "member_name": row["current_name"],
        "join_count": len(items),
        "prior_stints": prior_stints,
        "is_returning": prior_stints > 0,
        "current_joined_at": current["joined_at"] if current else None,
        "first_joined_at": items[0]["joined_at"],
        "last_left_at": last_left,
        "memberships": items,
    }


@managed_connection
def get_member_overview(tag: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    from storage.war import get_member_war_status

    profile = get_member_profile(tag, conn=conn)
    if not profile:
        return None
    overview = dict(profile)
    overview["war_status"] = get_member_war_status(tag, conn=conn)
    return overview


@managed_connection
def list_longest_tenure_members(limit: int = 10, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    rows = conn.execute(
        "SELECT m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, cs.role, cs.trophies, cs.clan_rank "
        "FROM players m "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        f"WHERE {_ACTIVE}"
    ).fetchall()
    result = []
    for row in rows:
        joined_date = _current_joined_at(conn, row["tag"])
        if not joined_date:
            continue
        joined_day = joined_date[:10]
        try:
            tenure_days = (today - datetime.strptime(joined_day, "%Y-%m-%d").date()).days
        except ValueError:
            tenure_days = None
        item = dict(row)
        item["joined_date"] = joined_day
        item["tenure_days"] = tenure_days
        result.append(_member_reference_fields(conn, row["tag"], item))
    result.sort(
        key=lambda item: (
            item["joined_date"],
            (item.get("name") or "").lower(),
        )
    )
    return result[:limit]


@managed_connection
def list_recent_joins(days: int = 30, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    from storage.war import get_current_season_id
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days))
    season_id = get_current_season_id(conn=conn)
    rows = conn.execute(
        "SELECT m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, cs.role, cs.trophies, cs.clan_rank "
        "FROM players m "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        f"WHERE {_ACTIVE}"
    ).fetchall()
    result = []
    for row in rows:
        joined_date = _current_joined_at(conn, row["tag"])
        if not joined_date:
            continue
        joined_day = joined_date[:10]
        try:
            joined_dt = datetime.strptime(joined_day, "%Y-%m-%d").date()
        except ValueError:
            continue
        if joined_dt < cutoff:
            continue
        item = dict(row)
        item["joined_date"] = joined_day
        form = conn.execute(
            "SELECT wins, losses, sample_size, form_label FROM player_recent_form WHERE player_tag = ? AND scope = 'competitive_10'",
            (row["tag"],),
        ).fetchone()
        if form:
            item["recent_form"] = dict(form)
        if season_id is not None:
            war = conn.execute(
                "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_fame "
                "FROM war_participation wp "
                "WHERE wp.season_id = ? AND wp.player_tag = ?",
                (season_id, row["tag"]),
            ).fetchone()
            item["current_season_war"] = dict(war)
        result.append(_member_reference_fields(conn, row["tag"], item))
    result.sort(
        key=lambda item: (
            item["joined_date"],
            (item.get("name") or "").lower(),
        ),
        reverse=True,
    )
    return result


@managed_connection
def get_member_recent_form(tag: str, scope: str = "competitive_10", conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    row = conn.execute(
        "SELECT f.player_tag, f.scope, f.sample_size, f.wins, f.losses, f.draws, f.current_streak, "
        "f.current_streak_type, f.win_rate, f.avg_crown_diff, f.avg_trophy_change, f.form_label, f.summary, f.computed_at "
        "FROM player_recent_form f "
        "WHERE f.player_tag = ? AND f.scope = ?",
        (_canon_tag(tag), scope),
    ).fetchone()
    return dict(row) if row else None


def _streak_row(conn, row, scope: str) -> dict:
    """Shape a streak row (QA M11/L12): stamp the battle scope, carry computed_at
    freshness, and — when the streak has hit the recent-form sample ceiling —
    note it may actually be longer than the number shown."""
    item = dict(_member_reference_fields(conn, row["tag"], dict(row)), scope=scope)
    streak = row["current_streak"] or 0
    sample = row["sample_size"] or 0
    if sample and streak >= sample:
        item["streak_note"] = (
            f"Streak equals the {sample}-battle recent-form window — the true streak "
            "may be longer than shown."
        )
    return item


@managed_connection
def get_members_on_losing_streak(min_streak: int = 3, scope: str = "competitive_10", conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        "SELECT m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, cs.clan_rank, cs.role, "
        "f.current_streak, f.current_streak_type, f.wins, f.losses, f.sample_size, f.form_label, f.summary, f.computed_at "
        "FROM player_recent_form f "
        "JOIN players m ON m.player_tag = f.player_tag "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        f"WHERE {_ACTIVE} AND f.scope = ? AND f.current_streak_type = 'L' AND f.current_streak >= ? "
        "ORDER BY f.current_streak DESC, cs.clan_rank ASC, m.current_name COLLATE NOCASE",
        (scope, min_streak),
    ).fetchall()
    # QA M11: hot_streaks defaults to ladder_ranked_10 but losing_streaks to
    # competitive_10 (a different battle universe) — stamp the scope so the two
    # lists aren't read as symmetric.
    # QA L12: computed_at surfaces staleness; the streak can't exceed sample_size
    # (the recent-form window), so a "10" streak may actually be longer.
    return [_streak_row(conn, row, scope) for row in rows]


@managed_connection
def get_members_on_hot_streak(min_streak: int = 4, scope: str = "ladder_ranked_10", conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        "SELECT m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, cs.clan_rank, cs.role, "
        "f.current_streak, f.current_streak_type, f.wins, f.losses, f.draws, f.sample_size, "
        "f.form_label, f.summary, f.avg_trophy_change, f.computed_at "
        "FROM player_recent_form f "
        "JOIN players m ON m.player_tag = f.player_tag "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        f"WHERE {_ACTIVE} AND f.scope = ? AND f.current_streak_type = 'W' AND f.current_streak >= ? "
        "ORDER BY f.current_streak DESC, COALESCE(f.avg_trophy_change, 0) DESC, cs.clan_rank ASC, m.current_name COLLATE NOCASE",
        (scope, min_streak),
    ).fetchall()
    # QA M11: stamp scope (see get_members_on_losing_streak) — this list is
    # ladder+ranked, not the same universe as the competitive losing-streak list.
    # QA L12: computed_at + sample-cap note (see get_members_on_losing_streak).
    return [_streak_row(conn, row, scope) for row in rows]


@managed_connection
def get_weekly_digest_summary(days: int = 7, conn: Optional[sqlite3.Connection] = None) -> dict:
    from storage.war import get_current_season_id
    from storage.war_analytics import get_trending_war_contributors, get_war_score_trend
    from storage.war_status import get_trophy_changes, get_war_season_summary

    roster = get_clan_roster_summary(conn=conn)
    season_id = get_current_season_id(conn=conn)
    cutoff_ts = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_race = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y%m%dT%H%M%S.000Z")

    top_donors = conn.execute(
        "SELECT m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, cs.role, cs.clan_rank, cs.donations_week "
        "FROM players m "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        f"WHERE {_ACTIVE} AND COALESCE(cs.donations_week, 0) > 0 "
        "ORDER BY COALESCE(cs.donations_week, 0) DESC, cs.clan_rank ASC, m.current_name COLLATE NOCASE "
        "LIMIT 5"
    ).fetchall()
    donors = [_member_reference_fields(conn, row["tag"], dict(row)) for row in top_donors]

    race_rows = conn.execute(
        "SELECT season_id, section_index, our_rank, trophy_change, our_fame, finish_time, created_date "
        "FROM war_weeks WHERE created_date >= ? "
        "ORDER BY created_date DESC LIMIT 3",
        (cutoff_race,),
    ).fetchall()
    recent_war_races = []
    for row in race_rows:
        standings = _rowdicts(conn.execute(
            "SELECT wwc.rank, c.name, wwc.clan_tag AS tag, wwc.fame, NULL AS trophy_change "
            "FROM war_week_clans wwc LEFT JOIN clans c ON c.clan_tag = wwc.clan_tag "
            "WHERE wwc.season_id = ? AND wwc.section_index = ? "
            "ORDER BY COALESCE(wwc.rank, 99) ASC LIMIT 3",
            (row["season_id"], row["section_index"]),
        ).fetchall())
        top_participants = conn.execute(
            "SELECT wp.player_tag AS tag, COALESCE(p.display_name, p.current_name) AS name, wp.fame, wp.repair_points, wp.decks_used "
            "FROM war_participation wp LEFT JOIN players p ON p.player_tag = wp.player_tag "
            "WHERE wp.season_id = ? AND wp.section_index = ? "
            "ORDER BY COALESCE(wp.fame, 0) DESC, COALESCE(wp.decks_used, 0) DESC, name COLLATE NOCASE "
            "LIMIT 3",
            (row["season_id"], row["section_index"]),
        ).fetchall()
        participants = [_member_reference_fields(conn, p["tag"], dict(p)) for p in top_participants]
        recent_war_races.append({
            "season_id": row["season_id"],
            "week": (row["section_index"] + 1) if row["section_index"] is not None else None,
            "section_index": row["section_index"],
            "created_date": row["created_date"],
            "our_rank": row["our_rank"],
            "trophy_change": row["trophy_change"],
            "our_fame": row["our_fame"],
            "total_clans": len(standings) or None,
            "finish_time": row["finish_time"],
            "top_participants": participants,
            "standings_preview": standings,
        })

    trophy_changes = get_trophy_changes(since_hours=max(24, days * 24), conn=conn)
    trophy_risers = [item for item in trophy_changes if (item.get("change") or 0) > 0][:5]
    trophy_drops = [item for item in trophy_changes if (item.get("change") or 0) < 0][:3]

    # Progression from daily metrics (player_profile_snapshots retired).
    progression = []
    active_members = conn.execute(
        f"SELECT m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name FROM players m WHERE {_ACTIVE}"
    ).fetchall()
    for row in active_members:
        metrics = conn.execute(
            "SELECT metric_date, trophies, best_trophies "
            "FROM player_daily_metrics WHERE player_tag = ? AND metric_date >= ? "
            "ORDER BY metric_date ASC",
            (row["tag"], cutoff_ts[:10]),
        ).fetchall()
        if len(metrics) < 2:
            continue
        first, latest = metrics[0], metrics[-1]
        item = {
            "tag": row["tag"],
            "name": row["name"],
            "wins_gain": 0,
            "trophies_change": (latest["trophies"] or 0) - (first["trophies"] or 0),
            "best_trophies_gain": (latest["best_trophies"] or 0) - (first["best_trophies"] or 0),
            "pol_league_gain": 0,
            "pol_trophies_change": 0,
            "favorite_card": None,
        }
        if any(item[key] for key in ("trophies_change", "best_trophies_gain")):
            progression.append(_member_reference_fields(conn, row["tag"], item))
    progression.sort(
        key=lambda item: (
            -(item.get("best_trophies_gain") or 0),
            -(item.get("trophies_change") or 0),
            (item.get("name") or "").lower(),
        )
    )

    recent_joins = list_recent_joins(days=days, conn=conn)[:5]
    hot_streaks = get_members_on_hot_streak(min_streak=4, conn=conn)[:5]
    war_score_trend = get_war_score_trend(days=days, conn=conn)
    season_summary = get_war_season_summary(season_id=season_id, top_n=5, conn=conn) if season_id is not None else None
    recent_race_count = len(recent_war_races)
    trending_war = get_trending_war_contributors(
        season_id=season_id,
        recent_races=max(1, min(3, recent_race_count)) if recent_race_count else 1,
        limit=5,
        conn=conn,
    ) if season_id is not None else {"members": []}

    from storage.awards import get_season_awards_standings
    season_awards = get_season_awards_standings(season_id=season_id, conn=conn) if season_id is not None else None

    return {
        "window_days": days,
        "roster": roster,
        "season_id": season_id,
        "top_donors": donors,
        "recent_war_races": recent_war_races,
        "war_score_trend": war_score_trend,
        "war_season_summary": season_summary,
        "trending_war_contributors": trending_war,
        "trophy_risers": trophy_risers,
        "trophy_drops": trophy_drops,
        "progression_highlights": progression[:8],
        "hot_streaks": hot_streaks,
        "recent_joins": recent_joins,
        "season_awards": season_awards,
    }
