"""Persistence for the awareness loop: thoughts, posts, and standing watches.

- ``awareness_thoughts`` — one row per loop turn: the read it reviewed, the
  plan it produced, and whether it chose silence.
- ``awareness_posts`` — the durable member-facing delivery history.
- ``watches`` — standing concerns Elixir is keeping an eye on (a durable home
  for the ``flag_member_watch`` surface).

Schema is versioned centrally in ``db.schema``. This module never issues DDL.
Writers follow the ``managed_connection`` borrow/own pattern and never commit a
borrowed connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from db import managed_connection
from db.schema import require_columns

log = logging.getLogger("elixir")

AWARENESS_EVENT_STREAMS = {
    "player": "player_events",
    "clan": "clan_events",
    "war": "war_events",
    "game": "game_events",
}
AWARENESS_CURSOR_PREFIX = "awareness:events:"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_plan(plan) -> tuple[str, str | None]:
    """Classify a brain tick result → ``(outcome, reason)``.

    ``outcome`` is one of ``"posted"``, ``"silence"``, or ``"failed"``.

    A valid brain response ALWAYS carries a ``posts`` key (empty for a
    deliberate silence). ``None``, a ``{"_error": ...}`` payload, or any dict
    missing ``posts`` is a **failure**, never silence — the harness must not
    paint a broken tick (truncation, timeout, schema error, max tool rounds) as
    a brain that thoughtfully chose to stay quiet. That distinction is
    safety-critical the moment the loop can post for real.
    """
    if not isinstance(plan, dict) or "_error" in plan or "posts" not in plan:
        reason = None
        if isinstance(plan, dict):
            err = plan.get("_error")
            if isinstance(err, dict):
                # _chat_with_tools._failure_payload shape: kind/phase/detail.
                parts = [str(err[k]) for k in ("kind", "phase") if err.get(k)]
                head = " @ ".join(parts) if parts else None
                detail = err.get("detail")
                reason = f"{head}: {detail}" if head and detail else (head or detail)
            elif err:
                reason = str(err)
        return "failed", reason or "no structured output"
    if plan.get("posts"):
        return "posted", plan.get("skipped_reason")
    return "silence", plan.get("skipped_reason")


def ensure_awareness_schema(conn: sqlite3.Connection) -> None:
    """Compatibility name: validate; db.get_connection owns all evolution."""
    require_columns(conn, "awareness_thoughts", {"loop_number", "tool_trace_json"})
    require_columns(conn, "awareness_posts", {"lane", "posted_at", "content_preview"})
    require_columns(
        conn,
        "awareness_delivery_intents",
        {"intent_key", "lane", "content", "covers_json", "status", "attempts"},
    )
    require_columns(conn, "watches", {"watch_id", "status"})


def ensure_event_cursors(conn: sqlite3.Connection) -> tuple[dict[str, int], bool]:
    """Return durable awareness positions, initializing the cutover once.

    Existing installations already have successful awareness thoughts but no
    event-id cursors. For that one-time bridge, seed each stream to the greatest
    row that existed when the last successful thought was recorded. Rows created
    later remain pending even when their observed timestamp is old. A genuinely
    fresh installation starts at zero: without a successful thought there is no
    evidence that any stream row has been reviewed, so skipping to the head would
    recreate the silent-loss behavior this cursor is meant to remove.

    The caller owns the transaction. ``build_read`` commits initialization only
    when it owns its connection; borrowed connections follow the normal project
    discipline.
    """
    ensure_awareness_schema(conn)
    last_success = last_tick_at(conn=conn)
    positions: dict[str, int] = {}
    initialized = False
    for stream, table in AWARENESS_EVENT_STREAMS.items():
        consumer_key = f"{AWARENESS_CURSOR_PREFIX}{stream}"
        row = conn.execute(
            "SELECT cursor_int FROM stream_cursors "
            "WHERE consumer_key = ? AND scope_key = ''",
            (consumer_key,),
        ).fetchone()
        if row is not None and row[0] is not None:
            positions[stream] = int(row[0])
            continue

        if last_success:
            position = int(
                conn.execute(
                    # Timestamps have one-second precision. Strictly-before may
                    # replay a same-second row, but <= can silently consume a row
                    # inserted just after the thought in that same second.
                    f"SELECT COALESCE(MAX(event_id), 0) FROM {table} WHERE created_at < ?",
                    (last_success,),
                ).fetchone()[0]
            )
        else:
            position = 0
        conn.execute(
            "INSERT INTO stream_cursors "
            "(consumer_key, scope_key, cursor_int, updated_at, metadata_json) "
            "VALUES (?, '', ?, ?, ?) "
            "ON CONFLICT(consumer_key, scope_key) DO UPDATE SET "
            "cursor_int = excluded.cursor_int, updated_at = excluded.updated_at, "
            "metadata_json = excluded.metadata_json",
            (
                consumer_key,
                position,
                _utcnow(),
                json.dumps(
                    {
                        "initialized_from": "last_success"
                        if last_success
                        else "stream_start"
                    }
                ),
            ),
        )
        positions[stream] = position
        initialized = True
    return positions, initialized


@managed_connection
def event_cursor_positions(*, conn: sqlite3.Connection = None) -> dict[str, int]:
    positions, _ = ensure_event_cursors(conn)
    return positions


@managed_connection
def advance_event_cursors(
    positions: dict[str, int], *, conn: sqlite3.Connection = None
) -> None:
    """Advance only known awareness streams; positions never move backward."""
    now = _utcnow()
    for stream, position in (positions or {}).items():
        if stream not in AWARENESS_EVENT_STREAMS:
            continue
        consumer_key = f"{AWARENESS_CURSOR_PREFIX}{stream}"
        conn.execute(
            "INSERT INTO stream_cursors "
            "(consumer_key, scope_key, cursor_int, updated_at) VALUES (?, '', ?, ?) "
            "ON CONFLICT(consumer_key, scope_key) DO UPDATE SET "
            "cursor_int = MAX(COALESCE(stream_cursors.cursor_int, 0), excluded.cursor_int), "
            "updated_at = excluded.updated_at",
            (consumer_key, max(0, int(position or 0)), now),
        )


@managed_connection
def last_tick_at(*, conn: sqlite3.Connection = None) -> str | None:
    """The timestamp of the most recent *successful* thought — the cursor for
    "since the last tick". None on the very first run (no prior thought), so the
    caller falls back to a bounded window instead of the whole history.

    Only ticks that succeeded advance the cursor: a deliberate silence
    (``chose_silence = 1``) or a real post (``post_count > 0``). A **failed**
    tick (LLM error, send failure, or an uncovered hard-post floor) MUST NOT
    move the cursor — otherwise its delta signals are lost forever and the
    "fail-hard, catch up next loop" contract silently breaks. Failed thoughts
    are still persisted (for #thinking / Observatory), they just don't count
    here."""
    ensure_awareness_schema(conn)
    row = conn.execute(
        "SELECT MAX(at) FROM awareness_thoughts WHERE chose_silence = 1 OR post_count > 0"
    ).fetchone()
    return row[0] if row and row[0] else None


@managed_connection
def persist_thought(
    read: dict,
    plan: dict,
    *,
    model: str | None = None,
    tool_trace: list | None = None,
    cursor_positions: dict[str, int] | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Record one awareness turn. Returns ``{"thought_id", "loop_number"}``.

    ``loop_number`` is a stable, human-friendly sequence (Awareness Loop #N)
    for debugging and cross-reference — the UUID stays as the durable key. The
    full prompt + response of every round is captured separately at the LLM
    layer (``llm_calls`` rows), inspectable in the Observatory."""
    ensure_awareness_schema(conn)
    plan = plan or {}
    outcome, reason = classify_plan(plan)
    post_count = len(plan.get("posts") or [])
    # chose_silence means the brain DELIBERATELY chose to stay quiet — never a
    # failed tick. A failure is recorded with a ⚠️ marker and chose_silence=0.
    chose_silence = 1 if outcome == "silence" else 0
    if outcome == "failed":
        skipped_reason = f"⚠️ tick failed: {reason}"
    else:
        skipped_reason = reason
    thought_id = uuid.uuid4().hex
    loop_number = (
        conn.execute(
            "SELECT COALESCE(MAX(loop_number), 0) + 1 FROM awareness_thoughts"
        ).fetchone()
        or [1]
    )[0]
    conn.execute(
        "INSERT INTO awareness_thoughts (thought_id, loop_number, at, read_json, "
        "plan_json, tool_trace_json, chose_silence, post_count, skipped_reason, "
        "model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            thought_id,
            loop_number,
            _utcnow(),
            json.dumps(read, default=str),
            json.dumps(plan, default=str),
            json.dumps(tool_trace or [], default=str),
            chose_silence,
            post_count,
            skipped_reason,
            model,
        ),
    )
    # Thought + cursor acknowledgement are one transaction. Defensively ignore
    # positions on a failed plan even if a caller accidentally supplies them.
    if outcome != "failed" and cursor_positions:
        advance_event_cursors(cursor_positions, conn=conn)
    return {"thought_id": thought_id, "loop_number": loop_number}


@managed_connection
def record_awareness_post(
    *,
    lane: str,
    content: str,
    covers: list | None = None,
    message_id: str | int | None = None,
    loop_number: int | None = None,
    post: dict | None = None,
    evidence: dict | None = None,
    intent_key: str | None = None,
    conn: sqlite3.Connection = None,
) -> None:
    """Record a delivered brain post in its purpose-built durable ledger.

    Best-effort: a persistence failure must not turn an already-delivered
    Discord post into a retry that double-posts it.
    """
    try:
        now = _utcnow()
        preview = (content or "")[:800]
        conn.execute(
            "INSERT OR IGNORE INTO awareness_posts "
            "(lane, content_preview, covers_json, loop_number, posted_at, "
            "discord_message_id, intent_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                lane,
                preview,
                json.dumps(list(covers or [])),
                loop_number,
                now,
                str(message_id) if message_id is not None else None,
                intent_key,
            ),
        )
        if post is not None:
            from engine.editor import (
                judge_delivered_post,
                record_active_awareness_quality,
            )

            record_active_awareness_quality(
                conn,
                post=post,
                evidence=evidence or {},
                lane=lane,
                content=content,
                discord_message_id=message_id,
            )
            # Observe-and-learn editorial critic: judge the post that just
            # shipped, record the verdict (editor_verdicts), and feed a lesson
            # on a downgrade. Self-isolated/fail-open — never blocks or alters
            # the delivered post; gated by ELIXIR_EDITOR_GATE.
            judge_delivered_post(
                conn,
                post=post,
                evidence=evidence or {},
                lane=lane,
                content=content,
                discord_message_id=message_id,
                loop_number=loop_number,
                intent_key=intent_key,
            )
    except Exception as exc:
        log.exception("record_awareness_post: failed to record %s post", lane)
        # The post is already visible in Discord, so this remains fail-soft to
        # avoid a retry duplicate. The incident ledger makes the lost local
        # receipt visible to health monitoring and operators.
        from storage.incidents import record_incident

        record_incident(
            "awareness.record_post",
            exc,
            context={"lane": lane, "discord_message_id": message_id},
            conn=conn,
        )


def delivery_intent_key(post: dict, content: str) -> str:
    """Stable post identity: covered moments first, content only as fallback."""
    covers = sorted(
        {str(value) for value in (post.get("covers_signal_keys") or []) if value}
    )
    identity = {
        "lane": str(post.get("channel") or ""),
        "covers": covers,
        "content": None if covers else content,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "awareness:" + hashlib.sha256(canonical.encode()).hexdigest()


@managed_connection
def prepare_delivery_intents(
    posts: list[dict],
    *,
    required_signal_keys: set[str] | None = None,
    conn: sqlite3.Connection = None,
) -> list[dict]:
    """Persist a whole post plan before any Discord side effect."""
    ensure_awareness_schema(conn)
    now = _utcnow()
    required = {str(value) for value in (required_signal_keys or set()) if value}
    # ``sending`` is a short lease, not a terminal limbo.  A fresh lease fails
    # closed because Discord may already have accepted the message; after the
    # lease expires we choose the system's documented at-least-once guarantee
    # and retry.  Without this recovery, a process crash in the send/finalize
    # window wedges the entire awareness outbox forever.
    conn.execute(
        """UPDATE awareness_delivery_intents
           SET status = 'pending', updated_at = ?,
               last_error = 'delivery lease expired; retrying at least once'
           WHERE status = 'sending'
             AND datetime(updated_at) < datetime('now', '-15 minutes')""",
        (now,),
    )
    seen = set()
    for post in posts:
        content = post.get("_delivery_content") or ""
        intent_key = delivery_intent_key(post, content)
        if intent_key in seen:
            continue
        seen.add(intent_key)
        covers = sorted(
            {str(value) for value in (post.get("covers_signal_keys") or []) if value}
        )
        conn.execute(
            """INSERT OR IGNORE INTO awareness_delivery_intents
                   (intent_key, lane, content, covers_json, post_json,
                    status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                intent_key,
                post.get("channel"),
                content,
                json.dumps(covers),
                json.dumps(post, default=str),
                now,
                now,
            ),
        )
        # A receipt is stronger evidence than a stale outbox state. This closes
        # the crash window between recording a sent post and marking fulfilled.
        conn.execute(
            """UPDATE awareness_delivery_intents
               SET status = 'fulfilled', fulfilled_at = COALESCE(fulfilled_at, ?),
                   updated_at = ?
               WHERE intent_key = ? AND EXISTS (
                   SELECT 1 FROM awareness_posts ap
                   WHERE ap.intent_key = awareness_delivery_intents.intent_key
               )""",
            (now, now, intent_key),
        )
    params = list(seen)
    current_clause = ""
    if params:
        current_clause = " OR intent_key IN ({})".format(",".join("?" for _ in params))
    fulfilled_clause = (
        " OR (status = 'fulfilled' "
        "AND datetime(fulfilled_at) >= datetime('now', '-6 hours'))"
        if required
        else ""
    )
    rows = conn.execute(
        "SELECT * FROM awareness_delivery_intents "
        "WHERE (status IN ('pending', 'sending') "
        "AND datetime(created_at) >= datetime('now', '-6 hours'))"
        + current_clause
        + fulfilled_clause
        + " "
        "ORDER BY created_at, intent_key",
        params,
    ).fetchall()
    prepared = []
    for row in rows:
        persisted_post = json.loads(row["post_json"])
        covers = {
            str(value)
            for value in (persisted_post.get("covers_signal_keys") or [])
            if value
        }
        if (
            row["status"] == "fulfilled"
            and row["intent_key"] not in seen
            and not required.intersection(covers)
        ):
            continue
        persisted_post["_delivery_content"] = row["content"]
        prepared.append(
            {
                "intent_key": row["intent_key"],
                "status": row["status"],
                "post": persisted_post,
                "from_current_plan": row["intent_key"] in seen,
            }
        )
    return prepared


@managed_connection
def mark_delivery_sending(intent_key: str, *, conn: sqlite3.Connection = None) -> bool:
    now = _utcnow()
    cur = conn.execute(
        """UPDATE awareness_delivery_intents
           SET status = 'sending', attempts = attempts + 1,
               last_attempt_at = ?, updated_at = ?, last_error = NULL
           WHERE intent_key = ? AND status = 'pending'""",
        (now, now, intent_key),
    )
    return cur.rowcount == 1


@managed_connection
def mark_delivery_pending(
    intent_key: str, error: str, *, conn: sqlite3.Connection = None
) -> None:
    conn.execute(
        """UPDATE awareness_delivery_intents
           SET status = 'pending', updated_at = ?, last_error = ?
           WHERE intent_key = ? AND status = 'sending'""",
        (_utcnow(), str(error)[:2000], intent_key),
    )


@managed_connection
def mark_delivery_fulfilled(
    intent_key: str,
    message_id: str | int,
    *,
    loop_number: int | None = None,
    conn: sqlite3.Connection = None,
) -> None:
    now = _utcnow()
    conn.execute(
        """UPDATE awareness_delivery_intents
           SET status = 'fulfilled', fulfilled_at = ?, updated_at = ?,
               discord_message_id = ?, loop_number = COALESCE(?, loop_number),
               last_error = NULL
           WHERE intent_key = ?""",
        (now, now, str(message_id), loop_number, intent_key),
    )


@managed_connection
def attach_awareness_posts_to_loop(
    loop_number: int,
    *,
    since: str,
    conn: sqlite3.Connection = None,
) -> int:
    """Link receipts written during this loop to its persisted thought.

    Delivery necessarily happens before the thought is committed, so the
    receipt cannot know the future loop number when inserted. The awareness
    job is single-instance; linking still-null receipts created after this
    read began is deterministic and crash-safe.
    """
    cur = conn.execute(
        "UPDATE awareness_posts SET loop_number = ? "
        "WHERE loop_number IS NULL AND datetime(posted_at) >= datetime(?)",
        (int(loop_number), since),
    )
    conn.execute(
        """UPDATE awareness_delivery_intents
           SET loop_number = COALESCE(loop_number, ?)
           WHERE intent_key IN (
               SELECT intent_key FROM awareness_posts
               WHERE loop_number = ? AND intent_key IS NOT NULL
           )""",
        (int(loop_number), int(loop_number)),
    )
    return max(0, cur.rowcount)


@managed_connection
def list_recent_thoughts(
    limit: int = 20,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    ensure_awareness_schema(conn)
    rows = conn.execute(
        "SELECT thought_id, loop_number, at, chose_silence, post_count, "
        "skipped_reason, model FROM awareness_thoughts "
        "ORDER BY loop_number DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


@managed_connection
def open_watches(
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    ensure_awareness_schema(conn)
    rows = conn.execute(
        "SELECT watch_id, opened_at, subject_tag, subject_label, reason, status, "
        "expires_at, last_seen_at, resolved_at FROM watches "
        "WHERE status = 'open' ORDER BY opened_at DESC",
    ).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "ensure_awareness_schema",
    "persist_thought",
    "record_awareness_post",
    "delivery_intent_key",
    "prepare_delivery_intents",
    "mark_delivery_sending",
    "mark_delivery_pending",
    "mark_delivery_fulfilled",
    "attach_awareness_posts_to_loop",
    "list_recent_thoughts",
    "open_watches",
    "last_tick_at",
    "event_cursor_positions",
    "ensure_event_cursors",
    "advance_event_cursors",
    "classify_plan",
]
