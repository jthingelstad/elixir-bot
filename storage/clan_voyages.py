"""Clan Voyage screenshot capture storage."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import _canon_tag, _json_or_none, _rowdicts, _utcnow, managed_connection
from storage.roster import resolve_member

DEFAULT_CLAN_TAG = "#J2RGCRVG"
DEFAULT_EVENT_NAME = "Clan Voyage"


def _loads(value, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _parse_iso(value: str | None) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _fmt_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


def _infer_event_end_at(observed_at: str, event_end_at: str | None = None, ends_in_text: str | None = None) -> str | None:
    explicit = _parse_iso(event_end_at)
    if explicit is not None:
        return _fmt_iso(explicit)
    observed = _parse_iso(observed_at)
    if observed is None:
        return None
    text = (ends_in_text or "").lower()
    if not text:
        return None
    units = {
        "d": "days",
        "day": "days",
        "days": "days",
        "h": "hours",
        "hr": "hours",
        "hrs": "hours",
        "hour": "hours",
        "hours": "hours",
        "m": "minutes",
        "min": "minutes",
        "mins": "minutes",
        "minute": "minutes",
        "minutes": "minutes",
    }
    delta = {"days": 0, "hours": 0, "minutes": 0}
    for amount, unit in re.findall(r"(\d+)\s*(days?|d|hrs?|hours?|h|mins?|minutes?|m)\b", text):
        key = units.get(unit)
        if key:
            delta[key] += int(amount)
    if not any(delta.values()):
        return None
    return _fmt_iso(observed + timedelta(**delta))


def _clean_event_name(value: str | None) -> str:
    return " ".join((value or DEFAULT_EVENT_NAME).split()) or DEFAULT_EVENT_NAME


def _season_key(value: str | None, *, event_end_at: str | None, observed_at: str) -> str:
    clean = " ".join((value or "").split())
    if clean:
        return clean
    basis = event_end_at or observed_at
    if len(basis or "") >= 7:
        return basis[:7]
    return _utcnow()[:7]


def _voyage_key(clan_tag: str, event_name: str, season_key: str) -> str:
    event_slug = re.sub(r"[^a-z0-9]+", "_", event_name.lower()).strip("_") or "clan_voyage"
    return f"clan_voyage:{_canon_tag(clan_tag)}:{event_slug}:{season_key}"


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _as_confidence(value, default: float = 0.75) -> float:
    try:
        return max(0.1, min(0.99, float(value)))
    except (TypeError, ValueError):
        return default


def _member_id_for_tag(player_tag: str | None, *, conn) -> int | None:
    tag = _canon_tag(player_tag)
    if not tag:
        return None
    row = conn.execute("SELECT member_id FROM members WHERE player_tag = ?", (tag,)).fetchone()
    return int(row["member_id"]) if row else None


def _resolve_visible_member(name: str, player_tag: str | None = None, *, conn) -> tuple[str | None, int | None, float]:
    tag = _canon_tag(player_tag)
    if tag:
        member_id = _member_id_for_tag(tag, conn=conn)
        return tag, member_id, 0.99 if member_id else 0.7

    matches = resolve_member(name, status="active", limit=3, conn=conn)
    if not matches:
        return None, None, 0.0
    first = matches[0]
    second_score = int(matches[1].get("match_score") or 0) if len(matches) > 1 else 0
    first_score = int(first.get("match_score") or 0)
    source = first.get("match_source") or ""
    confident = (
        first_score >= 900
        or source in {"current_name_exact", "alias_exact"}
        or (first_score >= 775 and first_score - second_score >= 150)
    )
    if not confident:
        return None, None, min(0.65, first_score / 1000)
    return first.get("player_tag"), first.get("member_id"), min(0.95, first_score / 1000)


def _entry_payload(entry: dict, source_message_id: str | int | None) -> dict | None:
    rank = _as_int(entry.get("rank") or entry.get("place"))
    points = _as_int(entry.get("points") or entry.get("score") or entry.get("crowns"))
    name = " ".join(str(entry.get("player_name") or entry.get("name") or entry.get("player_name_raw") or "").split())
    if rank is None or points is None or not name:
        return None
    return {
        "rank": rank,
        "player_name_raw": name,
        "player_tag": entry.get("player_tag") or entry.get("tag"),
        "role_label": entry.get("role") or entry.get("role_label"),
        "points": points,
        "confidence": _as_confidence(entry.get("confidence")),
        "source_message_id": str(source_message_id) if source_message_id is not None else None,
        "source_image_index": _as_int(entry.get("source_image_index") or entry.get("image_index")),
        "raw": entry,
    }


def _decode_voyage(row: dict, *, conn=None, include_entries: bool = False) -> dict:
    for key in ("source_message_ids_json", "image_metadata_json", "raw_observation_json"):
        fallback = [] if key != "raw_observation_json" else {}
        row[key] = _loads(row.get(key), fallback)
    row["completed"] = bool(row.get("completed"))
    if include_entries and conn is not None:
        row["entries"] = get_clan_voyage_entries(row["voyage_id"], conn=conn)
    return row


@managed_connection
def upsert_clan_voyage_capture(
    *,
    source_message_id: str | int,
    channel_id: str | int | None = None,
    channel_name: str | None = None,
    author_discord_user_id: str | int | None = None,
    author_display_name: str | None = None,
    observed_at: str | None = None,
    clan_tag: str | None = None,
    clan_name: str | None = None,
    event_name: str | None = None,
    season_key: str | None = None,
    event_end_at: str | None = None,
    ends_in_text: str | None = None,
    completed: bool | None = None,
    status: str | None = None,
    entries: list[dict] | None = None,
    image_count: int = 0,
    image_metadata=None,
    raw_observation=None,
    conn=None,
) -> dict | None:
    """Insert or update a Clan Voyage capture from screenshot extraction."""
    observed = observed_at or _utcnow()
    inferred_end = _infer_event_end_at(observed, event_end_at=event_end_at, ends_in_text=ends_in_text)
    clean_event_name = _clean_event_name(event_name)
    clean_clan_tag = _canon_tag(clan_tag or DEFAULT_CLAN_TAG)
    clean_season_key = _season_key(season_key, event_end_at=inferred_end, observed_at=observed)
    voyage_key = _voyage_key(clean_clan_tag, clean_event_name, clean_season_key)
    incoming_entries = [
        payload for payload in (
            _entry_payload(entry, source_message_id) for entry in (entries or [])
        )
        if payload is not None
    ]
    if not incoming_entries and not raw_observation:
        return None

    now = _utcnow()
    existing = conn.execute(
        "SELECT * FROM clan_voyages WHERE voyage_key = ?",
        (voyage_key,),
    ).fetchone()
    previous_sources = []
    previous_image_metadata = []
    previous_image_count = 0
    if existing:
        previous = dict(existing)
        previous_sources = _loads(previous.get("source_message_ids_json"), [])
        previous_image_metadata = _loads(previous.get("image_metadata_json"), [])
        previous_image_count = int(previous.get("image_count") or 0)

    source_id = str(source_message_id)
    source_ids = list(dict.fromkeys([*previous_sources, source_id]))
    image_rows = []
    for item in image_metadata or []:
        row = dict(item) if isinstance(item, dict) else {"value": item}
        row.setdefault("source_message_id", source_id)
        image_rows.append(row)
    if source_id in previous_sources:
        combined_image_count = max(previous_image_count, int(image_count or 0))
    else:
        combined_image_count = previous_image_count + int(image_count or 0)
    combined_image_metadata = [*previous_image_metadata, *image_rows]

    completed_value = 1 if bool(completed) else 0
    clean_status = (
        status
        or ("completed_partial" if completed_value and incoming_entries else "partial")
    )

    conn.execute(
        """
        INSERT INTO clan_voyages (
            voyage_key, clan_tag, clan_name, event_name, season_key, event_end_at,
            observed_at, completed, status, source_channel_id, source_channel_name,
            source_message_ids_json, image_count, image_metadata_json,
            raw_observation_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(voyage_key) DO UPDATE SET
            clan_name = COALESCE(excluded.clan_name, clan_voyages.clan_name),
            event_end_at = COALESCE(excluded.event_end_at, clan_voyages.event_end_at),
            observed_at = excluded.observed_at,
            completed = MAX(clan_voyages.completed, excluded.completed),
            status = CASE
                WHEN excluded.completed = 1 THEN excluded.status
                WHEN clan_voyages.completed = 1 THEN clan_voyages.status
                ELSE excluded.status
            END,
            source_channel_id = COALESCE(excluded.source_channel_id, clan_voyages.source_channel_id),
            source_channel_name = COALESCE(excluded.source_channel_name, clan_voyages.source_channel_name),
            source_message_ids_json = excluded.source_message_ids_json,
            image_count = excluded.image_count,
            image_metadata_json = excluded.image_metadata_json,
            raw_observation_json = excluded.raw_observation_json,
            updated_at = excluded.updated_at
        """,
        (
            voyage_key,
            clean_clan_tag,
            clan_name,
            clean_event_name,
            clean_season_key,
            inferred_end,
            observed,
            completed_value,
            clean_status,
            str(channel_id) if channel_id is not None else None,
            channel_name,
            _json_or_none(source_ids),
            combined_image_count,
            _json_or_none(combined_image_metadata),
            _json_or_none(raw_observation or {}),
            now,
            now,
        ),
    )
    voyage = conn.execute(
        "SELECT * FROM clan_voyages WHERE voyage_key = ?",
        (voyage_key,),
    ).fetchone()
    if not voyage:
        conn.commit()
        return None
    voyage_id = int(voyage["voyage_id"])

    for entry in incoming_entries:
        resolved_tag, member_id, resolution_confidence = _resolve_visible_member(
            entry["player_name_raw"],
            entry.get("player_tag"),
            conn=conn,
        )
        confidence = min(float(entry["confidence"]), resolution_confidence or float(entry["confidence"]))
        conn.execute(
            """
            INSERT INTO clan_voyage_entries (
                voyage_id, rank, player_name_raw, player_tag, member_id, role_label,
                points, confidence, source_message_id, source_image_index, raw_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(voyage_id, rank) DO UPDATE SET
                player_name_raw = excluded.player_name_raw,
                player_tag = COALESCE(excluded.player_tag, clan_voyage_entries.player_tag),
                member_id = COALESCE(excluded.member_id, clan_voyage_entries.member_id),
                role_label = COALESCE(excluded.role_label, clan_voyage_entries.role_label),
                points = excluded.points,
                confidence = excluded.confidence,
                source_message_id = excluded.source_message_id,
                source_image_index = excluded.source_image_index,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            """,
            (
                voyage_id,
                entry["rank"],
                entry["player_name_raw"],
                resolved_tag,
                member_id,
                entry.get("role_label"),
                entry["points"],
                confidence,
                entry.get("source_message_id"),
                entry.get("source_image_index"),
                _json_or_none(entry.get("raw") or {}),
                now,
                now,
            ),
        )
    conn.commit()
    return get_clan_voyage(voyage_id, include_entries=True, conn=conn)


@managed_connection
def get_clan_voyage(voyage_id: int, *, include_entries: bool = False, conn=None) -> dict | None:
    row = conn.execute("SELECT * FROM clan_voyages WHERE voyage_id = ?", (int(voyage_id),)).fetchone()
    if not row:
        return None
    return _decode_voyage(dict(row), conn=conn, include_entries=include_entries)


@managed_connection
def get_latest_clan_voyage(*, include_entries: bool = False, conn=None) -> dict | None:
    row = conn.execute(
        "SELECT * FROM clan_voyages ORDER BY COALESCE(event_end_at, observed_at) DESC, voyage_id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return _decode_voyage(dict(row), conn=conn, include_entries=include_entries)


@managed_connection
def list_clan_voyages(*, limit: int = 5, include_entries: bool = False, conn=None) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM clan_voyages ORDER BY COALESCE(event_end_at, observed_at) DESC, voyage_id DESC LIMIT ?",
        (max(1, int(limit or 5)),),
    ).fetchall()
    return [_decode_voyage(row, conn=conn, include_entries=include_entries) for row in _rowdicts(rows)]


@managed_connection
def get_clan_voyage_entries(voyage_id: int, *, limit: int | None = None, conn=None) -> list[dict]:
    args: list[object] = [int(voyage_id)]
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        args.append(max(1, int(limit)))
    rows = conn.execute(
        "SELECT * FROM clan_voyage_entries WHERE voyage_id = ? ORDER BY rank ASC" + limit_sql,
        tuple(args),
    ).fetchall()
    decoded = []
    for row in _rowdicts(rows):
        row["raw_json"] = _loads(row.get("raw_json"), {})
        decoded.append(row)
    return decoded


@managed_connection
def get_member_clan_voyage_summary(member_tag: str, *, limit: int = 5, conn=None) -> dict:
    tag = _canon_tag(member_tag)
    row = conn.execute("SELECT member_id, current_name FROM members WHERE player_tag = ?", (tag,)).fetchone()
    if not row:
        return {"player_tag": tag, "entries": [], "summary": "No Clan Voyage history captured."}
    entries = conn.execute(
        """
        SELECT cv.voyage_id, cv.season_key, cv.event_end_at, cv.completed, cv.status,
               cve.rank, cve.points, cve.confidence, cve.player_name_raw
        FROM clan_voyage_entries cve
        JOIN clan_voyages cv ON cv.voyage_id = cve.voyage_id
        WHERE cve.member_id = ?
        ORDER BY COALESCE(cv.event_end_at, cv.observed_at) DESC, cv.voyage_id DESC
        LIMIT ?
        """,
        (row["member_id"], max(1, int(limit or 5))),
    ).fetchall()
    rows = _rowdicts(entries)
    if not rows:
        return {
            "player_tag": tag,
            "name": row["current_name"],
            "entries": [],
            "summary": "No Clan Voyage history captured.",
        }
    best = min(rows, key=lambda item: (int(item["rank"]), -int(item["points"])))
    total_points = sum(int(item["points"] or 0) for item in rows)
    return {
        "player_tag": tag,
        "name": row["current_name"],
        "captures": len(rows),
        "total_points": total_points,
        "best_rank": best["rank"],
        "best_points": best["points"],
        "latest_rank": rows[0]["rank"],
        "latest_points": rows[0]["points"],
        "entries": rows,
        "summary": (
            f"{row['current_name']} has {len(rows)} captured Clan Voyage result(s), "
            f"best rank #{best['rank']} with {best['points']} points."
        ),
    }


@managed_connection
def build_clan_voyage_context(*, limit: int = 3, conn=None) -> str:
    voyages = list_clan_voyages(limit=limit, include_entries=True, conn=conn)
    if not voyages:
        return "=== CLAN VOYAGE HISTORY ===\nNo Clan Voyage screenshots have been captured yet."
    lines = ["=== CLAN VOYAGE HISTORY ==="]
    for voyage in voyages:
        entries = voyage.get("entries") or []
        top = ", ".join(
            f"#{entry['rank']} {entry['player_name_raw']} {entry['points']}"
            for entry in entries[:5]
        )
        visible = f"{len(entries)} visible rank(s)"
        completed = "completed" if voyage.get("completed") else "captured"
        lines.append(
            f"- {voyage.get('season_key')} {voyage.get('event_name') or DEFAULT_EVENT_NAME} {completed}; "
            f"{visible}; top: {top or 'none'}"
        )
    return "\n".join(lines)


__all__ = [
    "build_clan_voyage_context",
    "get_clan_voyage",
    "get_clan_voyage_entries",
    "get_latest_clan_voyage",
    "get_member_clan_voyage_summary",
    "list_clan_voyages",
    "upsert_clan_voyage_capture",
]
