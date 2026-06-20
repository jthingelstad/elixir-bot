"""Normalized game event stream storage.

This is Elixir's canonical compact observation ledger: it records what Elixir
detected before projects, cases, intents, or delivery decide what to do with
those observations. Delivery outcomes, cases, and projects are separate layers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import db as _db
from db import EVENT_STREAM_RETENTION_DAYS, managed_connection
from signal_keys import signal_source_key
from storage.game_modes import mode_group_label

EVENT_STREAM_WINDOWS = (7, 28, 56, 90)

# Event-class discriminator. The signal-grain stream feeds prompt context; the
# high-volume battle-grain telemetry accumulates for rollups/queries and is
# excluded from the prompt-facing helpers by default (they default to
# event_class="signal") so battle volume never bloats awareness context.
DEFAULT_EVENT_CLASS = "signal"
BATTLE_EVENT_CLASS = "battle"
BATTLE_SOURCE_SYSTEM = "battle_log"

_LEADERSHIP_SIGNAL_TYPES = {
    "inactive_members",
    "api_event_sentinel",
    "api_schema_sentinel",
}

__all__ = [
    "EVENT_STREAM_WINDOWS",
    "DEFAULT_EVENT_CLASS",
    "BATTLE_EVENT_CLASS",
    "event_key_for_signal",
    "battle_event_key",
    "record_game_event",
    "record_signal_events",
    "record_battle_event",
    "list_recent_events",
    "list_subject_events",
    "summarize_events_by_window",
    "summarize_battle_modes",
]


def _json_dumps(value) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str, ensure_ascii=False)


def _payload_hash(payload) -> str:
    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _short_hash(payload) -> str:
    return _payload_hash(payload)[:16]


def _clean_text(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _canon_source(value: str | None, default: str) -> str:
    text = _clean_text(value)
    return text or default


def _normalize_time(value: str | None) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _event_type_from_signal(signal: dict) -> str:
    return _clean_text((signal or {}).get("type") or (signal or {}).get("signal_type")) or "signal"


def _signal_payload(signal: dict) -> dict:
    return dict(signal or {})


def event_key_for_signal(
    signal: dict,
    source_system: str,
    source_detector: str | None = None,
) -> str:
    """Return the deterministic event key for one signal observation."""
    source = _canon_source(source_system, "unknown")
    detector = _canon_source(source_detector, "")
    signal_key = signal_source_key(signal)
    basis = "|".join([source, detector, signal_key, _event_type_from_signal(signal)])
    return f"game_event:{hashlib.sha256(basis.encode('utf-8')).hexdigest()}"


def _scope_from_signal(signal: dict) -> str:
    signal = signal or {}
    payload = signal.get("payload") if isinstance(signal.get("payload"), dict) else {}
    scope = _clean_text(signal.get("scope"))
    if scope:
        return scope
    audience = (_clean_text(signal.get("audience") or payload.get("audience")) or "").lower()
    if audience == "leadership":
        return "leadership"
    if _event_type_from_signal(signal) in _LEADERSHIP_SIGNAL_TYPES:
        return "leadership"
    if audience == "system_internal":
        return "system_internal"
    return "public"


def _first_signal_value(signal: dict, keys: Iterable[str]) -> str | None:
    for key in keys:
        value = _clean_text((signal or {}).get(key))
        if value:
            return value
    return None


def _subject_from_signal(signal: dict) -> tuple[str | None, str | None]:
    signal = signal or {}
    tag = _first_signal_value(signal, ("tag", "player_tag", "member_tag", "target_player_tag"))
    if tag:
        return "member", _db._canon_tag(tag)
    nested_members = signal.get("members")
    if isinstance(nested_members, list) and len(nested_members) == 1:
        member = nested_members[0] if isinstance(nested_members[0], dict) else {}
        nested_tag = _first_signal_value(member, ("tag", "player_tag", "member_tag"))
        if nested_tag:
            return "member", _db._canon_tag(nested_tag)
    season_id = _clean_text(signal.get("season_id"))
    if _event_type_from_signal(signal).startswith("war_") or season_id:
        week = _clean_text(signal.get("week") or signal.get("section_index"))
        key = f"season:{season_id or 'unknown'}"
        if week:
            key = f"{key}:week:{week}"
        return "war", key
    clan_tag = _clean_text(signal.get("clan_tag"))
    if clan_tag:
        return "clan", _db._canon_tag(clan_tag)
    if _event_type_from_signal(signal) in {"capability_unlock"}:
        return "system", signal_source_key(signal)
    return None, None


def _war_week_from_signal(signal: dict) -> str | None:
    value = _first_signal_value(signal or {}, ("week", "section_index", "war_week"))
    return value


def _row_to_event(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["payload_json"] = json.loads(item.get("payload_json") or "{}")
    return item


@managed_connection
def record_game_event(
    *,
    event_type: str,
    source_system: str,
    source_detector: str | None = None,
    source_signal_key: str | None = None,
    source_signal_type: str | None = None,
    observed_at: str | None = None,
    occurred_at: str | None = None,
    scope: str = "public",
    subject_type: str | None = None,
    subject_key: str | None = None,
    season_id: str | int | None = None,
    war_week: str | int | None = None,
    event_class: str = DEFAULT_EVENT_CLASS,
    game_mode: str | None = None,
    payload: Optional[dict] = None,
    event_key: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Insert one normalized event idempotently and return the stored row."""
    normalized_payload = payload or {}
    normalized_event_type = _canon_source(event_type, "signal")
    normalized_source = _canon_source(source_system, "unknown")
    normalized_key = _clean_text(event_key)
    if not normalized_key:
        basis = "|".join([
            normalized_source,
            _clean_text(source_detector) or "",
            _clean_text(source_signal_key) or "",
            normalized_event_type,
            _short_hash(normalized_payload),
        ])
        normalized_key = f"game_event:{hashlib.sha256(basis.encode('utf-8')).hexdigest()}"
    payload_json = _json_dumps(normalized_payload)
    now = _db._utcnow()
    observed = _normalize_time(observed_at) or now
    occurred = _normalize_time(occurred_at)
    conn.execute(
        """
        INSERT OR IGNORE INTO game_event_stream (
            event_key, event_type, source_system, source_detector,
            source_signal_key, source_signal_type, observed_at, occurred_at,
            scope, subject_type, subject_key, season_id, war_week,
            event_class, game_mode,
            payload_json, payload_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalized_key,
            normalized_event_type,
            normalized_source,
            _clean_text(source_detector),
            _clean_text(source_signal_key),
            _clean_text(source_signal_type),
            observed,
            occurred,
            _canon_source(scope, "public"),
            _clean_text(subject_type),
            _clean_text(subject_key),
            _clean_text(season_id),
            _clean_text(war_week),
            _canon_source(event_class, DEFAULT_EVENT_CLASS),
            _clean_text(game_mode),
            payload_json,
            hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM game_event_stream WHERE event_key = ?",
        (normalized_key,),
    ).fetchone()
    return _row_to_event(row) if row else {}


@managed_connection
def record_signal_events(
    signals: list[dict] | tuple[dict, ...] | None,
    *,
    source_system: str,
    source_detector: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Record signal dicts into the event stream and annotate mutable signals."""
    events: list[dict] = []
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        payload = _signal_payload(signal)
        event_type = _event_type_from_signal(signal)
        source_signal_key = signal_source_key(signal)
        source_signal_type = _clean_text(signal.get("signal_type")) or event_type
        subject_type, subject_key = _subject_from_signal(signal)
        event_key = event_key_for_signal(signal, source_system, source_detector)
        event = record_game_event(
            event_type=event_type,
            source_system=source_system,
            source_detector=source_detector,
            source_signal_key=source_signal_key,
            source_signal_type=source_signal_type,
            observed_at=_clean_text(signal.get("observed_at")),
            occurred_at=_clean_text(signal.get("occurred_at") or signal.get("created_at")),
            scope=_scope_from_signal(signal),
            subject_type=subject_type,
            subject_key=subject_key,
            season_id=signal.get("season_id"),
            war_week=_war_week_from_signal(signal),
            payload=payload,
            event_key=event_key,
            conn=conn,
        )
        if event:
            signal.setdefault("event_key", event.get("event_key"))
            signal.setdefault("event_id", event.get("event_id"))
            events.append(event)
    return events


def battle_event_key(
    member_tag: str,
    battle_time: str | None,
    battle_type: str | None = None,
    opponent_tag: str | None = None,
    crowns_for=None,
    crowns_against=None,
) -> str:
    """Deterministic event key for one battle.

    Mirrors the ``member_battle_facts`` dedupe tuple (member, time, type,
    opponent, crowns) so the live ingest path and the historical backfill
    produce identical keys and ``INSERT OR IGNORE`` stays idempotent.
    """
    basis = "|".join([
        "battle",
        _db._canon_tag(member_tag) or "",
        _clean_text(battle_time) or "",
        _clean_text(battle_type) or "",
        _db._canon_tag(opponent_tag) or "",
        "" if crowns_for is None else str(crowns_for),
        "" if crowns_against is None else str(crowns_against),
    ])
    return f"game_event:{hashlib.sha256(basis.encode('utf-8')).hexdigest()}"


@managed_connection
def record_battle_event(
    *,
    member_tag: str,
    battle_time: str | None,
    mode_group: str,
    battle_type: str | None = None,
    game_mode_name: str | None = None,
    outcome: str | None = None,
    crowns_for=None,
    crowns_against=None,
    trophy_change=None,
    league_number=None,
    arena_name: str | None = None,
    opponent_name: str | None = None,
    opponent_tag: str | None = None,
    opponent_clan_tag: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Project one battle into the stream as battle-grain telemetry.

    ``observed_at``/``occurred_at`` are set to the battle time so windowed
    queries place historical battles correctly. The caller supplies
    ``mode_group`` (Elixir's stable mode family from
    ``storage.game_modes.classify_battle_mode``) so the event's game mode
    matches the battle fact's classification exactly.
    """
    tag = _db._canon_tag(member_tag)
    when = _clean_text(battle_time)
    if not tag or not when:
        return {}
    parsed = _db._parse_cr_time(battle_time)
    occurred = parsed.strftime("%Y-%m-%dT%H:%M:%S") if parsed else _normalize_time(battle_time)
    mode = _clean_text(mode_group) or "other"
    payload = {
        "mode_group": mode,
        "game_mode_name": _clean_text(game_mode_name),
        "battle_type": _clean_text(battle_type),
        "outcome": _clean_text(outcome),
        "crowns_for": crowns_for,
        "crowns_against": crowns_against,
        "trophy_change": trophy_change,
        "league_number": league_number,
        "arena": _clean_text(arena_name),
        "opponent_name": _clean_text(opponent_name),
        "opponent_tag": _db._canon_tag(opponent_tag) if opponent_tag else None,
        "opponent_clan_tag": _db._canon_tag(opponent_clan_tag) if opponent_clan_tag else None,
    }
    return record_game_event(
        event_type="battle_played",
        source_system=BATTLE_SOURCE_SYSTEM,
        source_detector=mode,
        observed_at=occurred,
        occurred_at=occurred,
        scope="public",
        subject_type="member",
        subject_key=tag,
        event_class=BATTLE_EVENT_CLASS,
        game_mode=mode,
        payload=payload,
        event_key=battle_event_key(tag, battle_time, battle_type, opponent_tag, crowns_for, crowns_against),
        conn=conn,
    )


def _event_filters(
    *,
    since: str | None = None,
    days: int | None = None,
    scope: str | None = None,
    event_type: str | None = None,
    subject_type: str | None = None,
    subject_key: str | None = None,
    event_class: str | None = None,
) -> tuple[str, list]:
    clauses = []
    args: list = []
    if since:
        clauses.append("observed_at >= ?")
        args.append(_normalize_time(since) or since)
    elif days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).replace(tzinfo=None)
        clauses.append("observed_at >= ?")
        args.append(cutoff.strftime("%Y-%m-%dT%H:%M:%S"))
    if event_class:
        clauses.append("event_class = ?")
        args.append(event_class)
    if scope:
        clauses.append("scope = ?")
        args.append(scope)
    if event_type:
        clauses.append("event_type = ?")
        args.append(event_type)
    if subject_type:
        clauses.append("subject_type = ?")
        args.append(subject_type)
    if subject_key:
        clauses.append("subject_key = ?")
        args.append(subject_key)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, args


@managed_connection
def list_recent_events(
    *,
    days: int = EVENT_STREAM_RETENTION_DAYS,
    since: str | None = None,
    scope: str | None = None,
    event_type: str | None = None,
    subject_type: str | None = None,
    subject_key: str | None = None,
    event_class: str | None = DEFAULT_EVENT_CLASS,
    limit: int = 100,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    where, args = _event_filters(
        since=since,
        days=days,
        scope=scope,
        event_type=event_type,
        subject_type=subject_type,
        subject_key=subject_key,
        event_class=event_class,
    )
    rows = conn.execute(
        f"SELECT * FROM game_event_stream {where} "
        "ORDER BY observed_at DESC, event_id DESC LIMIT ?",
        (*args, max(1, int(limit or 100))),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


@managed_connection
def list_subject_events(
    subject_type: str,
    subject_key: str,
    *,
    days: int = EVENT_STREAM_RETENTION_DAYS,
    limit: int = 100,
    scope: str | None = None,
    event_class: str | None = DEFAULT_EVENT_CLASS,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    return list_recent_events(
        days=days,
        scope=scope,
        subject_type=subject_type,
        subject_key=subject_key,
        event_class=event_class,
        limit=limit,
        conn=conn,
    )


@managed_connection
def summarize_events_by_window(
    *,
    windows: tuple[int, ...] = EVENT_STREAM_WINDOWS,
    scope: str | None = None,
    subject_type: str | None = None,
    subject_key: str | None = None,
    event_class: str | None = DEFAULT_EVENT_CLASS,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Return compact event counts by type/scope for standard lookback windows."""
    now_dt = datetime.fromisoformat((now or _db._utcnow()).replace("Z", "+00:00"))
    if now_dt.tzinfo is not None:
        now_dt = now_dt.astimezone(timezone.utc).replace(tzinfo=None)
    summary: dict[str, dict] = {}
    for days in windows:
        cutoff = (now_dt - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%S")
        where, args = _event_filters(
            since=cutoff,
            scope=scope,
            subject_type=subject_type,
            subject_key=subject_key,
            event_class=event_class,
        )
        total = conn.execute(
            f"SELECT COUNT(*) AS count FROM game_event_stream {where}",
            args,
        ).fetchone()["count"]
        by_type_rows = conn.execute(
            f"SELECT event_type, COUNT(*) AS count FROM game_event_stream {where} "
            "GROUP BY event_type ORDER BY count DESC, event_type ASC",
            args,
        ).fetchall()
        by_scope_rows = conn.execute(
            f"SELECT scope, COUNT(*) AS count FROM game_event_stream {where} "
            "GROUP BY scope ORDER BY count DESC, scope ASC",
            args,
        ).fetchall()
        summary[f"{int(days)}d"] = {
            "days": int(days),
            "total": int(total or 0),
            "by_type": {row["event_type"]: int(row["count"] or 0) for row in by_type_rows},
            "by_scope": {row["scope"]: int(row["count"] or 0) for row in by_scope_rows},
        }
    return summary


def _win_rate(wins: int, decided: int) -> float | None:
    return round(wins / decided, 3) if decided else None


@managed_connection
def summarize_battle_modes(
    *,
    windows: tuple[int, ...] = (7, 28),
    now: str | None = None,
    top_members: int = 3,
    min_battles: int = 3,
    subject_key: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Per-mode battle activity derived from the battle-grain stream.

    For each lookback window, returns a per-game-mode summary (battles, active
    members, W/L, win rate) plus the most active members in that mode. This is
    what lets Elixir observe Path of Legends, 2v2, and event activity instead of
    only Trophy Road — no detector signal required. Battles are public, so this
    block is public-scoped. Pass ``subject_key`` (a player tag) to scope the
    summary to one member's per-mode activity (e.g. for a player highlight).
    """
    now_dt = datetime.fromisoformat((now or _db._utcnow()).replace("Z", "+00:00"))
    if now_dt.tzinfo is not None:
        now_dt = now_dt.astimezone(timezone.utc).replace(tzinfo=None)
    member_filter = _db._canon_tag(subject_key) if subject_key else None
    result: dict[str, dict] = {}
    for days in windows:
        cutoff = (now_dt - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%S")
        where = [
            "g.event_class = 'battle'",
            "g.observed_at >= ?",
            "g.subject_key IS NOT NULL",
        ]
        params: list = [cutoff]
        if member_filter:
            where.append("g.subject_key = ?")
            params.append(member_filter)
        rows = conn.execute(
            f"""
            SELECT g.game_mode AS game_mode,
                   g.subject_key AS tag,
                   m.current_name AS name,
                   COUNT(*) AS battles,
                   SUM(CASE WHEN json_extract(g.payload_json, '$.outcome') = 'W' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN json_extract(g.payload_json, '$.outcome') = 'L' THEN 1 ELSE 0 END) AS losses
            FROM game_event_stream g
            LEFT JOIN members m ON m.player_tag = g.subject_key
            WHERE {' AND '.join(where)}
            GROUP BY g.game_mode, g.subject_key
            """,
            tuple(params),
        ).fetchall()
        modes: dict[str, dict] = {}
        for row in rows:
            mode = row["game_mode"] or "other"
            bucket = modes.setdefault(mode, {"battles": 0, "wins": 0, "losses": 0, "members": []})
            wins = int(row["wins"] or 0)
            losses = int(row["losses"] or 0)
            battles = int(row["battles"] or 0)
            bucket["battles"] += battles
            bucket["wins"] += wins
            bucket["losses"] += losses
            bucket["members"].append({
                "tag": row["tag"],
                "name": row["name"],
                "battles": battles,
                "wins": wins,
                "losses": losses,
            })
        summary_modes: dict[str, dict] = {}
        for mode, bucket in modes.items():
            if bucket["battles"] < max(1, int(min_battles)):
                continue
            members = sorted(
                bucket["members"], key=lambda member: (-member["battles"], -member["wins"])
            )[: max(0, int(top_members))]
            for member in members:
                member["win_rate"] = _win_rate(member["wins"], member["wins"] + member["losses"])
            summary_modes[mode] = {
                "label": mode_group_label(mode),
                "battles": bucket["battles"],
                "active_members": len(bucket["members"]),
                "wins": bucket["wins"],
                "losses": bucket["losses"],
                "win_rate": _win_rate(bucket["wins"], bucket["wins"] + bucket["losses"]),
                "top_members": members,
            }
        ordered = dict(sorted(summary_modes.items(), key=lambda kv: -kv[1]["battles"]))
        result[f"{int(days)}d"] = {"days": int(days), "modes": ordered}
    return result
