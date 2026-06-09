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
        except (TypeError, ValueError):
            state = {}
        state["updated_at"] = row["updated_at"]
        statuses[row["job_name"]] = state
    return statuses


__all__ = ["save_runtime_job_status", "list_runtime_job_status"]
