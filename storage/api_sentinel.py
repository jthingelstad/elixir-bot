from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict

from db import _json_or_none, _rowdicts, _utcnow, managed_connection


SCHEMA_SENTINEL_SIGNAL_TYPE = "api_schema_sentinel"
EVENT_SENTINEL_SIGNAL_TYPE = "api_event_sentinel"
_ANNOUNCED_SCHEMA_TYPES = {"badge_name", "progress_key", "battle_game_mode", "schema_path"}
_CONTENT_ITEM_LIMIT = 12


def _json_kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _clip(value: str | None, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _flatten_schema_paths(value, prefix: str = ""):
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            yield path, child
            # Player progress keys are dynamic game-mode identifiers. Record
            # them as progress_key observations instead of generic paths.
            if path == "progress" or path.startswith("progress."):
                continue
            yield from _flatten_schema_paths(child, path)
    elif isinstance(value, list):
        list_path = f"{prefix}[]" if prefix else "[]"
        yield list_path, value[:1] if value else []
        for child in value[:5]:
            yield from _flatten_schema_paths(child, list_path)


def _sample_payload(**values) -> dict:
    return {key: value for key, value in values.items() if value is not None}


def _observation_key(observation: dict) -> tuple[str, str, str]:
    return (
        observation["sentinel_type"],
        observation["scope"],
        observation["name"],
    )


def build_api_sentinel_observations(endpoint: str, entity_key: str | None, payload) -> list[dict]:
    endpoint = (endpoint or "unknown").strip() or "unknown"
    entity_key = (entity_key or "global").strip() or "global"
    observations: dict[tuple[str, str, str], dict] = {}

    def add(sentinel_type: str, scope: str, name, sample: dict | None = None) -> None:
        normalized = str(name or "").strip()
        if not normalized:
            return
        observation = {
            "sentinel_type": sentinel_type,
            "scope": scope,
            "name": normalized,
            "endpoint": endpoint,
            "entity_key": entity_key,
            "sample": sample or {},
        }
        observations.setdefault(_observation_key(observation), observation)

    for path, child in _flatten_schema_paths(payload):
        if endpoint == "events":
            continue
        add(
            "schema_path",
            endpoint,
            path,
            _sample_payload(
                path=path,
                json_type=_json_kind(child),
                endpoint=endpoint,
                entity_key=entity_key,
            ),
        )

    for item in _iter_dicts(payload):
        badges = item.get("badges")
        if isinstance(badges, list):
            for badge in badges:
                if not isinstance(badge, dict):
                    continue
                add(
                    "badge_name",
                    "player.badges",
                    badge.get("name"),
                    _sample_payload(endpoint=endpoint, entity_key=entity_key, badge=badge),
                )

        progress = item.get("progress")
        if isinstance(progress, dict):
            for progress_key, progress_value in progress.items():
                add(
                    "progress_key",
                    "player.progress",
                    progress_key,
                    _sample_payload(endpoint=endpoint, entity_key=entity_key, value=progress_value),
                )

        game_mode = item.get("gameMode")
        if isinstance(game_mode, dict):
            mode_id = game_mode.get("id")
            mode_name = game_mode.get("name")
            add(
                "battle_game_mode",
                "battlelog.gameMode",
                mode_id or mode_name,
                _sample_payload(
                    endpoint=endpoint,
                    entity_key=entity_key,
                    id=mode_id,
                    name=mode_name,
                    battle_type=item.get("type"),
                    event_tag=item.get("eventTag"),
                ),
            )

    if endpoint == "events":
        event_items = (
            payload
            if isinstance(payload, list)
            else (payload.get("items") if isinstance(payload, dict) else [])
        )
        for event in event_items or []:
            if not isinstance(event, dict):
                continue
            event_tag = event.get("eventTag")
            title = event.get("title")
            add(
                "event",
                "events",
                event_tag or title,
                _sample_payload(
                    endpoint=endpoint,
                    entity_key=entity_key,
                    eventTag=event_tag,
                    title=title,
                    description=event.get("description"),
                ),
            )

    return list(observations.values())


def _insert_or_touch_observation(conn: sqlite3.Connection, observation: dict, now: str) -> dict | None:
    row = conn.execute(
        """
        SELECT observation_id
        FROM api_sentinel_observations
        WHERE sentinel_type = ? AND scope = ? AND name = ?
        """,
        (
            observation["sentinel_type"],
            observation["scope"],
            observation["name"],
        ),
    ).fetchone()
    sample_json = _json_or_none(observation.get("sample") or {})
    if row:
        conn.execute(
            """
            UPDATE api_sentinel_observations
            SET last_seen_at = ?, endpoint = ?, entity_key = ?, sample_json = ?, updated_at = ?
            WHERE observation_id = ?
            """,
            (
                now,
                observation.get("endpoint"),
                observation.get("entity_key"),
                sample_json,
                now,
                row["observation_id"],
            ),
        )
        return None

    conn.execute(
        """
        INSERT INTO api_sentinel_observations (
            sentinel_type, scope, name, endpoint, entity_key, first_seen_at,
            last_seen_at, sample_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation["sentinel_type"],
            observation["scope"],
            observation["name"],
            observation.get("endpoint"),
            observation.get("entity_key"),
            now,
            now,
            sample_json,
            now,
            now,
        ),
    )
    inserted = dict(observation)
    inserted["first_seen_at"] = now
    inserted["last_seen_at"] = now
    return inserted


def _signal_key(signal_type: str, observations: list[dict], now: str) -> str:
    basis = "|".join(
        f"{obs.get('sentinel_type')}:{obs.get('scope')}:{obs.get('name')}"
        for obs in sorted(
            observations,
            key=lambda item: (
                item.get("sentinel_type"),
                item.get("scope"),
                item.get("name"),
            ),
        )
    )
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    compact_time = now.replace("-", "").replace(":", "").replace("T", "")[:12]
    return f"{signal_type}:{compact_time}:{digest}"


def _observation_payload(observation: dict) -> dict:
    return {
        "sentinel_type": observation.get("sentinel_type"),
        "scope": observation.get("scope"),
        "name": observation.get("name"),
        "endpoint": observation.get("endpoint"),
        "entity_key": observation.get("entity_key"),
        "first_seen_at": observation.get("first_seen_at"),
        "sample": observation.get("sample") or {},
    }


def _format_event_observation(observation: dict) -> str:
    sample = observation.get("sample") or {}
    title = sample.get("title") or observation.get("name")
    tag = sample.get("eventTag") or observation.get("name")
    description = sample.get("description")
    suffix = f" - {_clip(description, 120)}" if description else " - no description"
    return f"- {title} (`{tag}`){suffix}"


def _format_schema_observation(observation: dict) -> str:
    sentinel_type = observation.get("sentinel_type")
    sample = observation.get("sample") or {}
    endpoint = observation.get("endpoint") or "unknown"
    entity = observation.get("entity_key") or "global"
    name = observation.get("name") or "unknown"
    if sentinel_type == "badge_name":
        return f"- Badge `{name}` on `{endpoint}` (`{entity}`)"
    if sentinel_type == "progress_key":
        return f"- Progress key `{name}` on `{endpoint}` (`{entity}`)"
    if sentinel_type == "battle_game_mode":
        mode_name = sample.get("name") or name
        event_tag = sample.get("event_tag")
        suffix = f", eventTag `{event_tag}`" if event_tag else ""
        return f"- Battle game mode `{mode_name}` (`{name}`{suffix})"
    return f"- Path `{name}` on `{endpoint}` (`{entity}`)"


def _content_list(lines: list[str]) -> list[str]:
    if len(lines) <= _CONTENT_ITEM_LIMIT:
        return lines
    shown = lines[:_CONTENT_ITEM_LIMIT]
    shown.append(f"- ...and {len(lines) - _CONTENT_ITEM_LIMIT} more")
    return shown


def _event_signal_payload(signal_key: str, observations: list[dict]) -> dict:
    lines = [_format_event_observation(obs) for obs in observations]
    content = [
        "**CR event sentinel**",
        "",
        "New live game mode/event entries appeared in `/events`:",
        *_content_list(lines),
        "",
        "Stored by `eventTag` so battle logs can be interpreted when these modes show up.",
    ]
    return {
        "type": EVENT_SENTINEL_SIGNAL_TYPE,
        "audience": "leadership",
        "title": "CR event sentinel",
        "summary": f"{len(observations)} first-seen CR event(s)",
        "discord_content": "\n".join(content),
        "observations": [_observation_payload(obs) for obs in observations],
        "signal_key": signal_key,
    }


def _schema_signal_payload(signal_key: str, observations: list[dict]) -> dict:
    grouped = defaultdict(list)
    for observation in observations:
        grouped[observation.get("sentinel_type")].append(observation)
    lines = []
    for sentinel_type in ("badge_name", "progress_key", "battle_game_mode", "schema_path"):
        for observation in grouped.get(sentinel_type, []):
            lines.append(_format_schema_observation(observation))
    content = [
        "**CR API schema sentinel**",
        "",
        "First-seen Clash Royale API observations:",
        *_content_list(lines),
        "",
        "Stored in `api_sentinel_observations` for future drift checks and Elixir context.",
    ]
    return {
        "type": SCHEMA_SENTINEL_SIGNAL_TYPE,
        "audience": "leadership",
        "title": "CR API schema sentinel",
        "summary": f"{len(observations)} first-seen CR API schema observation(s)",
        "discord_content": "\n".join(content),
        "observations": [_observation_payload(obs) for obs in observations],
        "signal_key": signal_key,
    }


def _queue_signal(conn: sqlite3.Connection, signal_key: str, signal_type: str, payload: dict, now: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO system_signals (signal_key, signal_type, created_at, payload_json)
        VALUES (?, ?, ?, ?)
        """,
        (signal_key, signal_type, now, _json_or_none(payload) or "{}"),
    )


def _mark_observations_announced(
    conn: sqlite3.Connection,
    signal_key: str,
    observations: list[dict],
    now: str,
) -> None:
    for observation in observations:
        conn.execute(
            """
            UPDATE api_sentinel_observations
            SET announced_signal_key = ?, updated_at = ?
            WHERE sentinel_type = ? AND scope = ? AND name = ? AND announced_signal_key IS NULL
            """,
            (
                signal_key,
                now,
                observation.get("sentinel_type"),
                observation.get("scope"),
                observation.get("name"),
            ),
        )


def _queue_api_sentinel_signals(conn: sqlite3.Connection, observations: list[dict], now: str) -> list[str]:
    signal_keys = []
    event_observations = [obs for obs in observations if obs.get("sentinel_type") == "event"]
    schema_observations = [
        obs
        for obs in observations
        if obs.get("sentinel_type") in _ANNOUNCED_SCHEMA_TYPES and obs.get("endpoint") != "events"
    ]
    if event_observations:
        signal_key = _signal_key(EVENT_SENTINEL_SIGNAL_TYPE, event_observations, now)
        payload = _event_signal_payload(signal_key, event_observations)
        _queue_signal(conn, signal_key, EVENT_SENTINEL_SIGNAL_TYPE, payload, now)
        _mark_observations_announced(conn, signal_key, event_observations, now)
        signal_keys.append(signal_key)
    if schema_observations:
        signal_key = _signal_key(SCHEMA_SENTINEL_SIGNAL_TYPE, schema_observations, now)
        payload = _schema_signal_payload(signal_key, schema_observations)
        _queue_signal(conn, signal_key, SCHEMA_SENTINEL_SIGNAL_TYPE, payload, now)
        _mark_observations_announced(conn, signal_key, schema_observations, now)
        signal_keys.append(signal_key)
    return signal_keys


def _record_api_sentinel_observations(
    conn: sqlite3.Connection,
    endpoint: str,
    entity_key: str | None,
    payload,
    *,
    announce: bool,
) -> list[dict]:
    now = _utcnow()
    new_observations = []
    for observation in build_api_sentinel_observations(endpoint, entity_key, payload):
        inserted = _insert_or_touch_observation(conn, observation, now)
        if inserted:
            new_observations.append(inserted)
    if announce and new_observations:
        _queue_api_sentinel_signals(conn, new_observations, now)
    return new_observations


@managed_connection
def record_api_payload_sentinel_observations(
    endpoint: str,
    entity_key: str | None,
    payload,
    *,
    announce: bool = True,
    conn=None,
) -> list[dict]:
    observations = _record_api_sentinel_observations(
        conn,
        endpoint,
        entity_key,
        payload,
        announce=announce,
    )
    conn.commit()
    return observations


@managed_connection
def bootstrap_api_sentinel_baseline(conn=None) -> dict:
    existing = conn.execute("SELECT COUNT(*) AS count FROM api_sentinel_observations").fetchone()["count"]
    if existing:
        return {"bootstrapped": False, "payloads": 0, "observations": 0}

    rows = conn.execute(
        """
        SELECT endpoint, entity_key, payload_json
        FROM raw_api_payloads
        ORDER BY fetched_at ASC, payload_id ASC
        """
    ).fetchall()
    observation_count = 0
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        observation_count += len(
            _record_api_sentinel_observations(
                conn,
                row["endpoint"],
                row["entity_key"],
                payload,
                announce=False,
            )
        )
    conn.commit()
    return {"bootstrapped": True, "payloads": len(rows), "observations": observation_count}


@managed_connection
def list_api_sentinel_observations(
    *,
    sentinel_type: str | None = None,
    limit: int = 50,
    conn=None,
) -> list[dict]:
    params: list[object] = []
    where = ""
    if sentinel_type:
        where = "WHERE sentinel_type = ?"
        params.append(sentinel_type)
    params.append(max(1, min(int(limit or 50), 500)))
    rows = conn.execute(
        f"""
        SELECT observation_id, sentinel_type, scope, name, endpoint, entity_key,
               first_seen_at, last_seen_at, sample_json, announced_signal_key,
               created_at, updated_at
        FROM api_sentinel_observations
        {where}
        ORDER BY first_seen_at DESC, observation_id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    result = _rowdicts(rows)
    for item in result:
        try:
            item["sample"] = json.loads(item.pop("sample_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["sample"] = {}
    return result
