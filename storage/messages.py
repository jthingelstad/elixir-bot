import json
from datetime import datetime, timedelta, timezone

from db import (
    CONVERSATION_MAX_PER_SCOPE,
    CONVERSATION_RETENTION_DAYS,
    _canon_tag,
    _ensure_channel,
    _ensure_member,
    _ensure_thread,
    _json_or_none,
    _normalize_scope,
    _rowdicts,
    _utcnow,
    get_connection,
)
from storage.identity import save_memory_episode, save_memory_fact, upsert_discord_user

# -- Signal and announcement logs ------------------------------------------

def was_signal_sent(signal_type, date_str, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        return conn.execute("SELECT 1 FROM signal_log WHERE signal_type = ? AND signal_date = ?", (signal_type, date_str)).fetchone() is not None
    finally:
        if close:
            conn.close()


def was_signal_sent_any_date(signal_type, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        return conn.execute("SELECT 1 FROM signal_log WHERE signal_type = ?", (signal_type,)).fetchone() is not None
    finally:
        if close:
            conn.close()


def mark_signal_sent(signal_type, date_str, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO signal_log (signal_type, signal_date) VALUES (?, ?)", (signal_type, date_str))
        conn.commit()
    finally:
        if close:
            conn.close()


def queue_system_signal(signal_key, signal_type, payload, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO system_signals (signal_key, signal_type, created_at, payload_json) VALUES (?, ?, ?, ?)",
            (signal_key, signal_type, _utcnow(), _json_or_none(payload) or "{}"),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def list_pending_system_signals(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT signal_key, signal_type, created_at, payload_json "
            "FROM system_signals WHERE announced_at IS NULL "
            "ORDER BY created_at ASC, system_signal_id ASC"
        ).fetchall()
        signals = []
        for row in rows:
            payload = {}
            if row["payload_json"]:
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
            item = dict(payload)
            item.setdefault("type", row["signal_type"])
            item["signal_key"] = row["signal_key"]
            item["signal_type"] = row["signal_type"]
            item["created_at"] = row["created_at"]
            item["signal_log_type"] = f"system_signal::{row['signal_key']}"
            signals.append(item)
        return signals
    finally:
        if close:
            conn.close()


def mark_system_signal_announced(signal_key, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "UPDATE system_signals SET announced_at = ? WHERE signal_key = ? AND announced_at IS NULL",
            (_utcnow(), signal_key),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def mark_announcement_sent(date_str, announcement_type, target_tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO cake_day_announcements (announcement_date, announcement_type, target_tag) VALUES (?, ?, ?)",
            (date_str, announcement_type, _canon_tag(target_tag) if target_tag else None),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def was_announcement_sent(date_str, announcement_type, target_tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM cake_day_announcements WHERE announcement_date = ? AND announcement_type = ? AND target_tag IS ?",
            (date_str, announcement_type, _canon_tag(target_tag) if target_tag else None),
        ).fetchone()
        return row is not None
    finally:
        if close:
            conn.close()


# -- Messaging --------------------------------------------------------------

def save_message(scope, author_type, content, summary=None, channel_id=None, channel_name=None,
                 channel_kind=None, discord_user_id=None, username=None, display_name=None,
                 member_tag=None, workflow=None, event_type=None, discord_message_id=None,
                 raw_json=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = None
        if member_tag:
            member_id = _ensure_member(conn, member_tag)
        if discord_user_id is not None:
            upsert_discord_user(discord_user_id, username=username, display_name=display_name, conn=conn)
            if member_id is None:
                link = conn.execute(
                    "SELECT member_id FROM discord_links WHERE discord_user_id = ? AND is_primary = 1",
                    (str(discord_user_id),),
                ).fetchone()
                if link:
                    member_id = link["member_id"]
        _ensure_channel(conn, channel_id, channel_name=channel_name, channel_kind=channel_kind)
        thread_id = _ensure_thread(
            conn,
            scope,
            channel_id=str(channel_id) if channel_id is not None else None,
            discord_user_id=str(discord_user_id) if discord_user_id is not None else None,
            member_id=member_id,
        )
        now = _utcnow()
        summary = summary if summary is not None else (content[:200] if content else "")
        conn.execute(
            "INSERT INTO messages (discord_message_id, thread_id, channel_id, discord_user_id, member_id, author_type, workflow, event_type, content, summary, created_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        message_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "UPDATE conversation_threads SET last_active_at = ? WHERE thread_id = ?",
            (now, thread_id),
        )
        if channel_id is not None and author_type == "assistant":
            conn.execute(
                "INSERT INTO channel_state (channel_id, last_elixir_post_at, last_summary) VALUES (?, ?, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET last_elixir_post_at = excluded.last_elixir_post_at, last_summary = excluded.last_summary",
                (str(channel_id), now, summary),
            )
        if discord_user_id is not None:
            importance = 2 if workflow in {"leader", "reception"} else 1
            save_memory_episode(
                "discord_user",
                str(discord_user_id),
                workflow or author_type,
                summary,
                importance=importance,
                source_message_ids=[message_id],
                conn=conn,
            )
            if author_type == "user":
                save_memory_fact(
                    "discord_user",
                    str(discord_user_id),
                    "last_user_summary",
                    summary,
                    confidence=0.6,
                    source_message_id=message_id,
                    conn=conn,
                )
        if member_id is not None:
            importance = 2 if workflow in {"leader", "reception"} else 1
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
    finally:
        if close:
            conn.close()


def list_thread_messages(scope, limit=10, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        scope_type, scope_key = _normalize_scope(scope)
        row = conn.execute(
            "SELECT thread_id FROM conversation_threads WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        ).fetchone()
        if not row:
            return []
        rows = conn.execute(
            "SELECT author_type, content, summary, created_at FROM messages WHERE thread_id = ? ORDER BY created_at DESC, message_id DESC LIMIT ?",
            (row["thread_id"], limit),
        ).fetchall()
        out = []
        for msg in reversed(rows):
            role = "assistant" if msg["author_type"] == "assistant" else "user"
            out.append({
                "role": role,
                "content": msg["content"],
                "author_name": None,
                "recorded_at": msg["created_at"],
            })
        return out
    finally:
        if close:
            conn.close()


def list_channel_messages(channel_id, limit=10, author_type=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        where = ["channel_id = ?"]
        params = [str(channel_id)]
        if author_type:
            where.append("author_type = ?")
            params.append(author_type)
        params.append(limit)
        rows = conn.execute(
            "SELECT author_type, content, summary, created_at "
            f"FROM messages WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, message_id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        out = []
        for msg in reversed(rows):
            role = "assistant" if msg["author_type"] == "assistant" else "user"
            out.append({
                "role": role,
                "content": msg["content"],
                "author_name": None,
                "recorded_at": msg["created_at"],
            })
        return out
    finally:
        if close:
            conn.close()


def record_prompt_failure(question, failure_type, failure_stage, *, workflow=None, channel_id=None,
                          channel_name=None, discord_user_id=None, discord_message_id=None,
                          detail=None, result_preview=None, openai_last_error=None,
                          openai_last_model=None, openai_last_call_at=None, raw_json=None,
                          conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO prompt_failures (recorded_at, workflow, failure_type, failure_stage, channel_id, channel_name, discord_user_id, discord_message_id, question, detail, result_preview, openai_last_error, openai_last_model, openai_last_call_at, raw_json) "
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
                openai_last_error,
                openai_last_model,
                openai_last_call_at,
                _json_or_none(raw_json),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        if close:
            conn.close()


def list_prompt_failures(limit=20, workflow=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if workflow:
            rows = conn.execute(
                "SELECT failure_id, recorded_at, workflow, failure_type, failure_stage, channel_id, channel_name, discord_user_id, discord_message_id, question, detail, result_preview, openai_last_error, openai_last_model, openai_last_call_at, raw_json "
                "FROM prompt_failures WHERE workflow = ? ORDER BY recorded_at DESC, failure_id DESC LIMIT ?",
                (workflow, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT failure_id, recorded_at, workflow, failure_type, failure_stage, channel_id, channel_name, discord_user_id, discord_message_id, question, detail, result_preview, openai_last_error, openai_last_model, openai_last_call_at, raw_json "
                "FROM prompt_failures ORDER BY recorded_at DESC, failure_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


def purge_old_conversations(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=CONVERSATION_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        conn.commit()
    finally:
        if close:
            conn.close()
