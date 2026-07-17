from __future__ import annotations

import json

from db import _json_or_none, _utcnow, managed_connection


@managed_connection
def save_runtime_job_status(job_name: str, state: dict, *, conn=None) -> dict:
    name = (job_name or "").strip()
    if not name:
        raise ValueError("job_name is required")
    payload = dict(state or {})
    updated_at = _utcnow()
    conn.execute(
        """
        INSERT INTO runtime_job_status (job_name, status_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(job_name) DO UPDATE SET
            status_json = excluded.status_json,
            updated_at = excluded.updated_at
        """,
        (name, _json_or_none(payload), updated_at),
    )
    conn.commit()
    return {"job_name": name, **payload, "updated_at": updated_at}


@managed_connection
def list_runtime_job_status(*, conn=None) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT job_name, status_json, updated_at FROM runtime_job_status ORDER BY job_name"
    ).fetchall()
    statuses: dict[str, dict] = {}
    for row in rows:
        try:
            state = json.loads(row["status_json"] or "{}")
        except TypeError, ValueError:
            state = {}
        state["updated_at"] = row["updated_at"]
        statuses[row["job_name"]] = state
    return statuses


@managed_connection
def get_awareness_loop_by_number(loop_number, *, conn=None) -> dict | None:
    """Compact read of one awareness-loop tick (``awareness_thoughts`` by
    ``loop_number``) for the agent's reference-lookup tool (an "L<n>" reference).
    Projects the heavy ``read_json`` / ``plan_json`` down to the decision, the
    reasoning, and what it posted — not the full read/plan blobs."""
    row = conn.execute(
        "SELECT loop_number, at, chose_silence, post_count, skipped_reason, model, "
        "read_json, plan_json FROM awareness_thoughts WHERE loop_number = ?",
        (int(loop_number),),
    ).fetchone()
    if not row:
        return None

    def _loads(value):
        try:
            return json.loads(value or "{}")
        except TypeError, ValueError:
            return {}

    read = _loads(row["read_json"])
    plan = _loads(row["plan_json"])
    posts = [
        {
            "channel": p.get("channel"),
            "leads_with": p.get("leads_with"),
            "summary": p.get("summary"),
            "members": p.get("member_names") or [],
        }
        for p in (plan.get("posts") or [])
    ]
    chose_silence = bool(row["chose_silence"])
    return {
        "loop": f"L{row['loop_number']}",
        "loop_number": row["loop_number"],
        "at": row["at"],
        "decision": "silence" if chose_silence else "posted",
        "post_count": row["post_count"],
        "reasoning": row["skipped_reason"] or plan.get("skipped_reason"),
        "posts": posts,
        "model": row["model"],
        "read_health": {
            "error": read.get("_error"),
            "degraded": read.get("_degraded") or [],
            "hard_post_signal_count": len(read.get("hard_post_signals") or []),
        },
    }


@managed_connection
def get_awareness_activity(*, limit: int = 20, conn=None) -> dict:
    """Current proactive decisions and confirmed deliveries.

    This is the production replacement for the retired communication-intent
    queue: thoughts explain post-vs-silence, posts are send-confirmed receipts.
    """
    cap = max(1, min(int(limit or 20), 100))
    thoughts = conn.execute(
        "SELECT loop_number, at, chose_silence, post_count, skipped_reason, model "
        "FROM awareness_thoughts ORDER BY loop_number DESC LIMIT ?",
        (cap,),
    ).fetchall()
    posts = conn.execute(
        "SELECT post_id, lane, content_preview, covers_json, loop_number, posted_at, "
        "discord_message_id FROM awareness_posts ORDER BY posted_at DESC, post_id DESC LIMIT ?",
        (cap,),
    ).fetchall()
    return {
        "thoughts": [dict(row) for row in thoughts],
        "posts": [dict(row) for row in posts],
    }


__all__ = [
    "save_runtime_job_status",
    "list_runtime_job_status",
    "get_awareness_loop_by_number",
    "get_awareness_activity",
]
