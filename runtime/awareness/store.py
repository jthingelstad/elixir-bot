"""Persistence for the awareness loop: the train of thought + standing watches.

Two v5.1 tables, created lazily on first use (no migration runner in v5.1 —
same discipline as ``storage/incidents.py``):

- ``awareness_thoughts`` — one row per loop turn: the read it reviewed, the
  plan it produced, and whether it chose silence. (The ``shadow`` column is a
  retained legacy field — shadow mode was removed, the brain is fully live.)
- ``watches`` — standing concerns Elixir is keeping an eye on (a durable home
  for the ``flag_member_watch`` surface).

Writers follow the ``managed_connection`` borrow/own pattern and never commit a
borrowed connection.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from db import managed_connection

log = logging.getLogger("elixir")

AWARENESS_DDL = """
CREATE TABLE IF NOT EXISTS awareness_thoughts (
    thought_id TEXT PRIMARY KEY,
    loop_number INTEGER,
    at TEXT NOT NULL,
    read_json TEXT,
    plan_json TEXT,
    tool_trace_json TEXT,
    chose_silence INTEGER NOT NULL DEFAULT 0,
    post_count INTEGER NOT NULL DEFAULT 0,
    skipped_reason TEXT,
    model TEXT,
    shadow INTEGER NOT NULL DEFAULT 0  -- legacy: shadow mode removed; always 0 (live)
);
CREATE INDEX IF NOT EXISTS idx_awareness_thoughts_at ON awareness_thoughts(at DESC);
CREATE INDEX IF NOT EXISTS idx_awareness_thoughts_loop ON awareness_thoughts(loop_number DESC);

CREATE TABLE IF NOT EXISTS watches (
    watch_id TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL,
    subject_tag TEXT,
    subject_label TEXT,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    expires_at TEXT,
    last_seen_at TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_watches_status ON watches(status, opened_at DESC);
"""

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
    have = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='awareness_thoughts'"
    ).fetchone()
    if not have:
        conn.executescript(AWARENESS_DDL)
        conn.commit()
        return
    # Best-effort forward-add for columns introduced after the table existed
    # (no migration runner in v5.1 — same lazy discipline as the DDL itself).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(awareness_thoughts)").fetchall()}
    added_loop_number = "loop_number" not in cols
    for col, decl in (("loop_number", "INTEGER"), ("tool_trace_json", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE awareness_thoughts ADD COLUMN {col} {decl}")
    if added_loop_number:
        # Backfill pre-existing rows from insertion order (rowid) so the loop
        # sequence is continuous and new loops don't restart at #1.
        conn.execute(
            "UPDATE awareness_thoughts SET loop_number = rowid WHERE loop_number IS NULL"
        )
    conn.commit()


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
                json.dumps({"initialized_from": "last_success" if last_success else "stream_start"}),
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
    loop_number = (conn.execute(
        "SELECT COALESCE(MAX(loop_number), 0) + 1 FROM awareness_thoughts"
    ).fetchone() or [1])[0]
    conn.execute(
        # `shadow` is a retained legacy column (shadow mode was removed — the
        # brain is fully live); always 0. Kept so historical rows still read.
        "INSERT INTO awareness_thoughts (thought_id, loop_number, at, read_json, "
        "plan_json, tool_trace_json, chose_silence, post_count, skipped_reason, "
        "model, shadow) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
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
    conn: sqlite3.Connection = None,
) -> None:
    """Record a delivered brain post as a **fulfilled** ``communication_intents``
    row so the next read's ``channel_memory`` sees what was just said and the
    brain doesn't repeat itself.

    We reuse the engine's intents table (no new store): once the engine stops
    raising intents, this becomes the ONLY writer, and ``read._channel_memory``
    reads the ``payload_json`` content preview back out. Best-effort — a failure
    here must not fail an already-delivered Discord post, so it swallows and
    logs rather than raising."""
    now = _utcnow()
    preview = (content or "")[:800]
    payload = json.dumps(
        {
            "content": preview,
            "covers_signal_keys": list(covers or []),
            "loop_number": loop_number,
        },
        default=str,
    )
    try:
        conn.execute(
            "INSERT INTO communication_intents (recognition_key, intent_type, lane, "
            "scope, payload_json, status, attempts, created_at, expires_at, "
            "fulfilled_at, discord_message_id) "
            "VALUES (NULL, 'awareness:post', ?, 'public', ?, 'fulfilled', 1, ?, ?, ?, ?)",
            (lane, payload, now, now, now,
             str(message_id) if message_id is not None else None),
        )
    except sqlite3.Error:
        log.exception("record_awareness_post: failed to record %s post", lane)


@managed_connection
def list_recent_thoughts(
    limit: int = 20,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    ensure_awareness_schema(conn)
    rows = conn.execute(
        "SELECT thought_id, loop_number, at, chose_silence, post_count, "
        "skipped_reason, model, shadow FROM awareness_thoughts "
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
    "list_recent_thoughts",
    "open_watches",
    "last_tick_at",
    "event_cursor_positions",
    "ensure_event_cursors",
    "advance_event_cursors",
    "classify_plan",
]
