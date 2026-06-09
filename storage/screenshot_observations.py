from __future__ import annotations

import json

from db import _json_or_none, _rowdicts, _utcnow, managed_connection


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


@managed_connection
def save_arena_relay_screenshot_observation(
    *,
    source_message_id: str | int,
    channel_id: str | int | None = None,
    channel_name: str | None = None,
    author_discord_user_id: str | int | None = None,
    author_display_name: str | None = None,
    observed_at: str | None = None,
    screenshot_type: str | None = None,
    summary: str | None = None,
    content: str | None = None,
    players=None,
    actionable_facts=None,
    uncertainty: str | None = None,
    image_count: int = 0,
    image_metadata=None,
    result=None,
    conn=None,
) -> dict:
    if source_message_id is None:
        raise ValueError("source_message_id is required")
    now = _utcnow()
    observed = observed_at or now
    kind = (screenshot_type or "unknown").strip().lower().replace(" ", "_") or "unknown"
    conn.execute(
        """
        INSERT INTO arena_relay_screenshot_observations (
            source_message_id, channel_id, channel_name, author_discord_user_id,
            author_display_name, observed_at, screenshot_type, summary, content,
            players_json, actionable_facts_json, uncertainty, image_count,
            image_metadata_json, result_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_message_id) DO UPDATE SET
            channel_id = excluded.channel_id,
            channel_name = excluded.channel_name,
            author_discord_user_id = excluded.author_discord_user_id,
            author_display_name = excluded.author_display_name,
            observed_at = excluded.observed_at,
            screenshot_type = excluded.screenshot_type,
            summary = excluded.summary,
            content = excluded.content,
            players_json = excluded.players_json,
            actionable_facts_json = excluded.actionable_facts_json,
            uncertainty = excluded.uncertainty,
            image_count = excluded.image_count,
            image_metadata_json = excluded.image_metadata_json,
            result_json = excluded.result_json,
            updated_at = excluded.updated_at
        """,
        (
            str(source_message_id),
            str(channel_id) if channel_id is not None else None,
            channel_name,
            str(author_discord_user_id) if author_discord_user_id is not None else None,
            author_display_name,
            observed,
            kind,
            summary,
            content,
            _json_or_none(_as_list(players)),
            _json_or_none(_as_list(actionable_facts)),
            uncertainty,
            int(image_count or 0),
            _json_or_none(image_metadata or []),
            _json_or_none(result or {}),
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM arena_relay_screenshot_observations WHERE source_message_id = ?",
        (str(source_message_id),),
    ).fetchone()
    return _decode_observation(dict(row))


@managed_connection
def list_arena_relay_screenshot_observations(*, limit: int = 25, screenshot_type: str | None = None, conn=None) -> list[dict]:
    args = []
    where = ""
    if screenshot_type:
        where = "WHERE screenshot_type = ?"
        args.append(str(screenshot_type).strip().lower().replace(" ", "_"))
    args.append(max(1, int(limit or 25)))
    rows = conn.execute(
        f"SELECT * FROM arena_relay_screenshot_observations {where} ORDER BY observed_at DESC LIMIT ?",
        args,
    ).fetchall()
    return [_decode_observation(row) for row in _rowdicts(rows)]


def _decode_observation(row: dict) -> dict:
    for key in ("players_json", "actionable_facts_json", "image_metadata_json", "result_json"):
        fallback = "{}" if key == "result_json" else "[]"
        try:
            row[key] = json.loads(row.get(key) or fallback)
        except (TypeError, ValueError):
            row[key] = {} if key == "result_json" else []
    return row


__all__ = [
    "save_arena_relay_screenshot_observation",
    "list_arena_relay_screenshot_observations",
]
