from __future__ import annotations

import json
import sqlite3

from db import _json_or_none, _rowdicts, _utcnow, managed_connection


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


def build_api_sentinel_observations(
    endpoint: str, entity_key: str | None, payload
) -> list[dict]:
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
                    _sample_payload(
                        endpoint=endpoint, entity_key=entity_key, badge=badge
                    ),
                )

        progress = item.get("progress")
        if isinstance(progress, dict):
            for progress_key, progress_value in progress.items():
                add(
                    "progress_key",
                    "player.progress",
                    progress_key,
                    _sample_payload(
                        endpoint=endpoint, entity_key=entity_key, value=progress_value
                    ),
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


def _ensure_first_entity_key(conn: sqlite3.Connection) -> None:
    """Compatibility assertion; db.schema owns this column and its backfill."""
    from db.schema import require_columns

    require_columns(conn, "api_sentinel_observations", {"first_entity_key"})


def _insert_or_touch_observation(
    conn: sqlite3.Connection, observation: dict, now: str
) -> dict | None:
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
        # Touch: entity_key/sample_json track the LATEST observer (by design).
        # first_entity_key is deliberately NOT updated — it preserves who was
        # first seen wearing this (the game-level stream attributes a new event
        # badge to that member; entity_key alone drifts to whoever polled last).
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
            sentinel_type, scope, name, endpoint, entity_key, first_entity_key,
            first_seen_at, last_seen_at, sample_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation["sentinel_type"],
            observation["scope"],
            observation["name"],
            observation.get("endpoint"),
            observation.get("entity_key"),
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


def _record_api_sentinel_observations(
    conn: sqlite3.Connection,
    endpoint: str,
    entity_key: str | None,
    payload,
) -> list[dict]:
    now = _utcnow()
    _ensure_first_entity_key(conn)
    new_observations = []
    for observation in build_api_sentinel_observations(endpoint, entity_key, payload):
        inserted = _insert_or_touch_observation(conn, observation, now)
        if inserted:
            new_observations.append(inserted)
    return new_observations


@managed_connection
def record_api_payload_sentinel_observations(
    endpoint: str,
    entity_key: str | None,
    payload,
    conn=None,
) -> list[dict]:
    observations = _record_api_sentinel_observations(
        conn,
        endpoint,
        entity_key,
        payload,
    )
    conn.commit()
    return observations


@managed_connection
def bootstrap_api_sentinel_baseline(conn=None) -> dict:
    existing = conn.execute(
        "SELECT COUNT(*) AS count FROM api_sentinel_observations"
    ).fetchone()["count"]
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
    return {
        "bootstrapped": True,
        "payloads": len(rows),
        "observations": observation_count,
    }


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
