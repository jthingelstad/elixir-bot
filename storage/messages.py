from __future__ import annotations

import json
import sqlite3
from typing import Optional

from db import (
    CONVERSATION_MAX_PER_SCOPE,
    _ensure_member,
    _ensure_thread,
    _json_or_none,
    _normalize_scope,
    _rowdicts,
    _utcnow,
    managed_connection,
)
from storage.identity import save_memory_episode, upsert_discord_user

# -- System-signal and conversation logs ----------------------------------


@managed_connection
def save_message(
    scope: str,
    author_type: str,
    content: str,
    summary: Optional[str] = None,
    channel_id: Optional[str | int] = None,
    channel_name: Optional[str] = None,
    channel_kind: Optional[str] = None,
    discord_user_id: Optional[str | int] = None,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    member_tag: Optional[str] = None,
    workflow: Optional[str] = None,
    event_type: Optional[str] = None,
    discord_message_id: Optional[str | int] = None,
    raw_json: Optional[dict] = None,
    intent_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    member_id = None
    if member_tag:
        member_id = _ensure_member(conn, member_tag)
    if discord_user_id is not None:
        upsert_discord_user(
            discord_user_id, username=username, display_name=display_name, conn=conn
        )
        if member_id is None:
            link = conn.execute(
                "SELECT player_tag AS member_id FROM discord_links WHERE discord_user_id = ? AND is_primary = 1",
                (str(discord_user_id),),
            ).fetchone()
            if link:
                member_id = link["member_id"]
    thread_id = _ensure_thread(conn, scope)
    now = _utcnow()
    summary = summary if summary is not None else (content[:200] if content else "")
    conn.execute(
        "INSERT INTO messages (discord_message_id, thread_id, channel_id, discord_user_id, member_id, author_type, workflow, event_type, content, summary, created_at, raw_json, intent_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(discord_message_id) if discord_message_id is not None else None,
            thread_id,
            str(channel_id) if channel_id is not None else None,
            str(discord_user_id) if discord_user_id is not None else None,
            member_id,
            author_type,
            workflow,
            event_type,
            content,
            summary,
            now,
            _json_or_none(raw_json),
            int(intent_id) if intent_id is not None else None,
        ),
    )
    message_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    if channel_id is not None and author_type == "assistant":
        conn.execute(
            "INSERT INTO channel_state (channel_id, last_summary) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET last_summary = excluded.last_summary",
            (str(channel_id), summary),
        )
    if discord_user_id is not None:
        importance = 2 if workflow in {"clanops", "reception"} else 1
        save_memory_episode(
            "discord_user",
            str(discord_user_id),
            workflow or author_type,
            summary,
            importance=importance,
            source_message_ids=[message_id],
            conn=conn,
        )
        # Note: last_user_summary is written by _post_conversation_memory
        # after distillation, not here. Writing the truncated content[:200]
        # here would persist verbatim text if distillation later fails.
    if member_id is not None:
        importance = 2 if workflow in {"clanops", "reception"} else 1
        save_memory_episode(
            "member",
            str(member_id),
            workflow or author_type,
            summary,
            importance=importance,
            source_message_ids=[message_id],
            conn=conn,
        )
    if channel_id is not None and author_type == "assistant":
        save_memory_episode(
            "channel",
            str(channel_id),
            workflow or "assistant_post",
            summary,
            importance=1,
            source_message_ids=[message_id],
            conn=conn,
        )
    rows = conn.execute(
        "SELECT message_id FROM messages WHERE thread_id = ? ORDER BY created_at DESC, message_id DESC",
        (thread_id,),
    ).fetchall()
    if len(rows) > CONVERSATION_MAX_PER_SCOPE:
        ids_to_keep = [r["message_id"] for r in rows[:CONVERSATION_MAX_PER_SCOPE]]
        placeholders = ",".join("?" for _ in ids_to_keep)
        conn.execute(
            f"DELETE FROM messages WHERE thread_id = ? AND message_id NOT IN ({placeholders})",
            (thread_id, *ids_to_keep),
        )
    conn.commit()
    return message_id


@managed_connection
def update_message_summary(
    message_id: int, summary: str, conn: Optional[sqlite3.Connection] = None
) -> None:
    """Retroactively update a message's summary and propagate to memory stores."""
    row = conn.execute(
        "SELECT message_id, author_type, discord_user_id, channel_id "
        "FROM messages WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if not row:
        return
    conn.execute(
        "UPDATE messages SET summary = ? WHERE message_id = ?",
        (summary, message_id),
    )
    author_type = row["author_type"]
    channel_id = row["channel_id"]

    # Propagate to channel_state (assistant summary)
    if author_type == "assistant" and channel_id:
        conn.execute(
            "UPDATE channel_state SET last_summary = ? WHERE channel_id = ?",
            (summary, str(channel_id)),
        )

    # Propagate to the memory_episodes entry linked to this message
    msg_id_pattern = f"%{message_id}%"
    episode_row = conn.execute(
        "SELECT episode_id FROM memory_episodes "
        "WHERE source_message_ids_json LIKE ? "
        "ORDER BY created_at DESC LIMIT 1",
        (msg_id_pattern,),
    ).fetchone()
    if episode_row:
        conn.execute(
            "UPDATE memory_episodes SET summary = ? WHERE episode_id = ?",
            (summary, episode_row["episode_id"]),
        )

    conn.commit()


@managed_connection
def get_message_by_discord_message_id(
    discord_message_id: str | int, conn: Optional[sqlite3.Connection] = None
) -> Optional[dict]:
    row = conn.execute(
        "SELECT message_id, discord_message_id, thread_id, channel_id, discord_user_id, member_id, "
        "author_type, workflow, event_type, content, summary, created_at, intent_id "
        "FROM messages WHERE discord_message_id = ?",
        (str(discord_message_id),),
    ).fetchone()
    return dict(row) if row else None


def _previous_user_message_for_assistant(conn, assistant_row):
    if not assistant_row:
        return None
    thread_id = assistant_row.get("thread_id")
    message_id = assistant_row.get("message_id")
    discord_user_id = assistant_row.get("discord_user_id")
    if not thread_id or not message_id:
        return None
    if discord_user_id is not None:
        row = conn.execute(
            "SELECT message_id, content, summary, discord_user_id "
            "FROM messages WHERE thread_id = ? AND author_type = 'user' AND discord_user_id = ? AND message_id < ? "
            "ORDER BY message_id DESC LIMIT 1",
            (thread_id, str(discord_user_id), int(message_id)),
        ).fetchone()
        if row:
            return dict(row)
    row = conn.execute(
        "SELECT message_id, content, summary, discord_user_id "
        "FROM messages WHERE thread_id = ? AND author_type = 'user' AND message_id < ? "
        "ORDER BY message_id DESC LIMIT 1",
        (thread_id, int(message_id)),
    ).fetchone()
    return dict(row) if row else None


def _response_preview(content) -> str:
    text = (content or "").strip()
    return text[:280] if text else ""


@managed_connection
def upsert_prompt_feedback(
    *,
    assistant_discord_message_id: str | int,
    discord_user_id: str | int,
    original_asker_discord_user_id: Optional[str | int] = None,
    workflow: Optional[str] = None,
    channel_id: Optional[str | int] = None,
    channel_name: Optional[str] = None,
    feedback_value: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    feedback_value = (feedback_value or "").strip().lower()
    if feedback_value not in {"up", "down"}:
        raise ValueError(f"invalid feedback value: {feedback_value}")
    assistant = get_message_by_discord_message_id(assistant_discord_message_id, conn=conn)
    if not assistant:
        raise ValueError(
            f"assistant message not found for discord id {assistant_discord_message_id}"
        )
    previous_question = _previous_user_message_for_assistant(conn, assistant)
    question = (previous_question.get("content") if previous_question else "") or ""
    existing = conn.execute(
        "SELECT prompt_feedback_id, feedback_value, removed_at FROM prompt_feedback "
        "WHERE assistant_discord_message_id = ? AND discord_user_id = ?",
        (str(assistant_discord_message_id), str(discord_user_id)),
    ).fetchone()
    now = _utcnow()
    if existing:
        conn.execute(
            "UPDATE prompt_feedback SET assistant_message_id = ?, workflow = ?, channel_id = ?, channel_name = ?, "
            "original_asker_discord_user_id = ?, feedback_value = ?, question = ?, response_preview = ?, "
            "updated_at = ?, removed_at = NULL "
            "WHERE prompt_feedback_id = ?",
            (
                assistant.get("message_id"),
                workflow or assistant.get("workflow"),
                str(channel_id) if channel_id is not None else assistant.get("channel_id"),
                channel_name,
                str(original_asker_discord_user_id)
                if original_asker_discord_user_id is not None
                else assistant.get("discord_user_id"),
                feedback_value,
                question,
                _response_preview(assistant.get("content") or ""),
                now,
                existing["prompt_feedback_id"],
            ),
        )
        prompt_feedback_id = existing["prompt_feedback_id"]
        previous_value = (existing["feedback_value"] or "").strip().lower()
        was_removed = bool(existing["removed_at"])
    else:
        cur = conn.execute(
            "INSERT INTO prompt_feedback (assistant_message_id, assistant_discord_message_id, workflow, channel_id, channel_name, "
            "discord_user_id, original_asker_discord_user_id, feedback_value, question, response_preview, recorded_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                assistant.get("message_id"),
                str(assistant_discord_message_id),
                workflow or assistant.get("workflow"),
                str(channel_id) if channel_id is not None else assistant.get("channel_id"),
                channel_name,
                str(discord_user_id),
                str(original_asker_discord_user_id)
                if original_asker_discord_user_id is not None
                else assistant.get("discord_user_id"),
                feedback_value,
                question,
                _response_preview(assistant.get("content") or ""),
                now,
                now,
            ),
        )
        prompt_feedback_id = cur.lastrowid
        previous_value = None
        was_removed = False
    conn.commit()
    became_active_down = feedback_value == "down" and (previous_value != "down" or was_removed)
    return {
        "prompt_feedback_id": prompt_feedback_id,
        "feedback_value": feedback_value,
        "became_active_down": became_active_down,
        "changed": previous_value != feedback_value or was_removed,
    }


@managed_connection
def clear_prompt_feedback(
    *,
    assistant_discord_message_id: str | int,
    discord_user_id: str | int,
    feedback_value: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    now = _utcnow()
    params = [now, now, str(assistant_discord_message_id), str(discord_user_id)]
    sql = (
        "UPDATE prompt_feedback SET removed_at = ?, updated_at = ? "
        "WHERE assistant_discord_message_id = ? AND discord_user_id = ? AND removed_at IS NULL"
    )
    if feedback_value:
        sql += " AND feedback_value = ?"
        params.append((feedback_value or "").strip().lower())
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur.rowcount


@managed_connection
def mark_prompt_feedback_retry_invited(
    prompt_feedback_id: int,
    *,
    retry_message_id: Optional[str | int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    conn.execute(
        "UPDATE prompt_feedback SET retry_invited_at = ?, retry_invite_message_id = ? WHERE prompt_feedback_id = ?",
        (
            _utcnow(),
            str(retry_message_id) if retry_message_id is not None else None,
            int(prompt_feedback_id),
        ),
    )
    conn.commit()


@managed_connection
def list_prompt_feedback(
    limit: int = 20,
    workflow: Optional[str] = None,
    *,
    include_positive: bool = False,
    active_only: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    where = []
    params = []
    if workflow:
        where.append("workflow = ?")
        params.append(workflow)
    if active_only:
        where.append("removed_at IS NULL")
    if not include_positive:
        where.append("feedback_value = 'down'")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        "SELECT prompt_feedback_id, assistant_message_id, assistant_discord_message_id, workflow, channel_id, channel_name, "
        "discord_user_id, original_asker_discord_user_id, feedback_value, question, response_preview, recorded_at, "
        "updated_at, removed_at, retry_invited_at, retry_invite_message_id "
        f"FROM prompt_feedback {clause} "
        "ORDER BY updated_at DESC, prompt_feedback_id DESC LIMIT ?",
        (*params, max(1, int(limit or 20))),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def list_prompt_review_items(
    limit: int = 20,
    workflow: Optional[str] = None,
    *,
    include_positive: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    failures = list_prompt_failures(limit=max(1, int(limit or 20)), workflow=workflow, conn=conn)
    feedback = list_prompt_feedback(
        limit=max(1, int(limit or 20)),
        workflow=workflow,
        include_positive=include_positive,
        active_only=True,
        conn=conn,
    )
    items = []
    for row in failures:
        item = dict(row)
        item["kind"] = "failure"
        item["sort_at"] = item.get("recorded_at")
        items.append(item)
    for row in feedback:
        item = {
            "kind": "feedback",
            "feedback_id": row["prompt_feedback_id"],
            "recorded_at": row["updated_at"] or row["recorded_at"],
            "workflow": row.get("workflow"),
            "failure_type": f"user_feedback_{row.get('feedback_value')}",
            "failure_stage": "discord_reaction",
            "channel_id": row.get("channel_id"),
            "channel_name": row.get("channel_name"),
            "discord_user_id": row.get("discord_user_id"),
            "discord_message_id": row.get("assistant_discord_message_id"),
            "question": row.get("question") or "",
            "detail": "Original asker reacted with thumbs down."
            if row.get("feedback_value") == "down"
            else "Original asker reacted with thumbs up.",
            "result_preview": row.get("response_preview"),
            "feedback_value": row.get("feedback_value"),
            "original_asker_discord_user_id": row.get("original_asker_discord_user_id"),
            "retry_invited_at": row.get("retry_invited_at"),
            "raw_json": None,
            "sort_at": row.get("updated_at") or row.get("recorded_at"),
        }
        items.append(item)
    items.sort(
        key=lambda item: (
            item.get("sort_at") or "",
            item.get("failure_id") or item.get("feedback_id") or 0,
        ),
        reverse=True,
    )
    return items[: max(1, int(limit or 20))]


@managed_connection
def list_thread_messages(
    scope: str, limit: int = 10, conn: Optional[sqlite3.Connection] = None
) -> list[dict]:
    scope_type, scope_key = _normalize_scope(scope)
    row = conn.execute(
        "SELECT thread_id FROM conversation_threads WHERE scope_type = ? AND scope_key = ?",
        (scope_type, scope_key),
    ).fetchone()
    if not row:
        return []
    rows = conn.execute(
        "SELECT author_type, content, summary, created_at FROM messages "
        "WHERE thread_id = ? AND TRIM(content) <> '' "
        "ORDER BY created_at DESC, message_id DESC LIMIT ?",
        (row["thread_id"], limit),
    ).fetchall()
    out = []
    for msg in reversed(rows):
        role = "assistant" if msg["author_type"] == "assistant" else "user"
        out.append(
            {
                "role": role,
                "content": msg["content"],
                "author_name": None,
                "recorded_at": msg["created_at"],
            }
        )
    return out


@managed_connection
def list_channel_messages(
    channel_id: str | int,
    limit: int = 10,
    author_type: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    where = ["channel_id = ?"]
    params = [str(channel_id)]
    if author_type:
        where.append("author_type = ?")
        params.append(author_type)
    params.append(limit)
    rows = conn.execute(
        "SELECT author_type, content, summary, created_at "
        f"FROM messages WHERE {' AND '.join(where)} "
        "AND TRIM(content) <> '' "
        "ORDER BY created_at DESC, message_id DESC LIMIT ?",
        tuple(params),
    ).fetchall()
    out = []
    for msg in reversed(rows):
        role = "assistant" if msg["author_type"] == "assistant" else "user"
        out.append(
            {
                "role": role,
                "content": msg["content"],
                "author_name": None,
                "recorded_at": msg["created_at"],
            }
        )
    return out


@managed_connection
def record_prompt_failure(
    question: str,
    failure_type: str,
    failure_stage: str,
    *,
    workflow: Optional[str] = None,
    channel_id: Optional[str | int] = None,
    channel_name: Optional[str] = None,
    discord_user_id: Optional[str | int] = None,
    discord_message_id: Optional[str | int] = None,
    detail: Optional[str] = None,
    result_preview: Optional[str] = None,
    llm_last_error: Optional[str] = None,
    llm_last_model: Optional[str] = None,
    llm_last_call_at: Optional[str] = None,
    raw_json: Optional[dict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO prompt_failures (recorded_at, workflow, failure_type, failure_stage, channel_id, channel_name, discord_user_id, discord_message_id, question, detail, result_preview, llm_last_error, llm_last_model, llm_last_call_at, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _utcnow(),
            workflow,
            failure_type,
            failure_stage,
            str(channel_id) if channel_id is not None else None,
            channel_name,
            str(discord_user_id) if discord_user_id is not None else None,
            str(discord_message_id) if discord_message_id is not None else None,
            question or "",
            detail,
            result_preview,
            llm_last_error,
            llm_last_model,
            llm_last_call_at,
            _json_or_none(raw_json),
        ),
    )
    conn.commit()
    return cur.lastrowid


@managed_connection
def list_prompt_failures(
    limit: int = 20,
    workflow: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    if workflow:
        rows = conn.execute(
            "SELECT failure_id, recorded_at, workflow, failure_type, failure_stage, channel_id, channel_name, discord_user_id, discord_message_id, question, detail, result_preview, llm_last_error, llm_last_model, llm_last_call_at, raw_json "
            "FROM prompt_failures WHERE workflow = ? ORDER BY recorded_at DESC, failure_id DESC LIMIT ?",
            (workflow, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT failure_id, recorded_at, workflow, failure_type, failure_stage, channel_id, channel_name, discord_user_id, discord_message_id, question, detail, result_preview, llm_last_error, llm_last_model, llm_last_call_at, raw_json "
            "FROM prompt_failures ORDER BY recorded_at DESC, failure_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return _rowdicts(rows)


def _ensure_llm_blob_columns(conn: sqlite3.Connection) -> None:
    """Compatibility assertion; db.schema owns the blob-column migration."""
    from db.schema import require_columns

    require_columns(conn, "llm_calls", {"prompt_json", "response_json"})


@managed_connection
def _record_llm_call_clan_db(
    workflow: str,
    model: str,
    *,
    ok: bool = True,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    prompt_json: Optional[str] = None,
    response_json: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    _ensure_llm_blob_columns(conn)
    conn.execute(
        "INSERT INTO llm_calls (recorded_at, workflow, model, ok, error, duration_ms, "
        "prompt_tokens, completion_tokens, total_tokens, cache_creation_tokens, cache_read_tokens, "
        "prompt_json, response_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _utcnow(),
            workflow,
            model,
            1 if ok else 0,
            str(error) if error else None,
            duration_ms,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cache_creation_tokens,
            cache_read_tokens,
            prompt_json,
            response_json,
        ),
    )
    conn.commit()


@managed_connection
def get_llm_call(call_id: int, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """Full detail for one LLM call — metadata + the captured prompt/response
    blobs, JSON-decoded. Powers the Observatory per-call drill-down. None when
    the call doesn't exist; blobs are None once pruned.

    Reads the TELEMETRY database (2026-08-03), not the clan one. The decorated
    ``conn`` is still accepted so the db facade's signature is unchanged, and
    deliberately unused — llm_calls no longer lives on that connection."""
    from storage import telemetry

    conn = telemetry.connect()
    row = conn.execute("SELECT * FROM llm_calls WHERE call_id = ?", (int(call_id),)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for raw_key, key in (("prompt_json", "prompt"), ("response_json", "response")):
        try:
            out[key] = json.loads(out.get(raw_key) or "null")
        except TypeError, ValueError:
            out[key] = None
    return out


def record_llm_call(*args, **kwargs) -> None:
    """Record a model call in the TELEMETRY database, not the clan one.

    Moved 2026-08-03. LLM call rows are telemetry about the agent, not facts
    about the clan, and every one of them used to take the clan database's single
    write lock — the same lock whose contention we were trying to measure. The
    clan DB is now about the clan.

    The old clan-DB writer survives as ``_record_llm_call_clan_db`` only so the
    one-time history copy has something to read; nothing calls it in production.
    """
    kwargs.pop("conn", None)  # the clan connection is exactly what we stopped using
    from storage import telemetry

    telemetry.record_llm_call(*args, **kwargs)
