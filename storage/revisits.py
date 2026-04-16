"""Self-scheduled revisits for the awareness loop.

The awareness agent can call ``schedule_revisit`` during a tick to tell its
future self "look at this signal again at time T." Due revisits flow into a
later tick's Situation under ``due_revisits`` so the agent can decide what
to do with them — post a follow-up, update its model, let it expire. The
runtime does not auto-post revisits; they are reminders, not actions.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

# Import the db module (not individual names) so this module's namespace does
# not re-export db internals. ``db/__init__.py:__export_public`` would
# otherwise copy anything here starting with a single underscore back into db,
# clobbering the card_catalog module's ``_utcnow`` override.
import db as _db
from db import managed_connection


def _normalize_due_at(value: str) -> str:
    """Accept ISO-8601 or ``YYYY-MM-DDTHH:MM:SS`` strings; return canonical form."""
    text = (value or "").strip()
    if not text:
        raise ValueError("due_at is required")
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"invalid due_at: {value!r} ({exc})") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


@managed_connection
def schedule_revisit(
    *,
    signal_key: str,
    due_at: str,
    rationale: Optional[str] = None,
    created_by_workflow: str = "awareness",
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Persist a revisit. If (signal_key, due_at) already exists, the existing
    row is returned unchanged — repeat calls are idempotent.
    """
    key = (signal_key or "").strip()
    if not key:
        raise ValueError("signal_key is required")
    normalized_due = _normalize_due_at(due_at)
    now = _db._utcnow()
    conn.execute(
        "INSERT OR IGNORE INTO revisits "
        "(signal_key, created_by_workflow, due_at, rationale, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (key, created_by_workflow or "awareness", normalized_due, (rationale or "").strip() or None, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT revisit_id, signal_key, created_by_workflow, due_at, rationale, "
        "revisited_at, created_at FROM revisits "
        "WHERE signal_key = ? AND due_at = ?",
        (key, normalized_due),
    ).fetchone()
    return dict(row) if row else {}


@managed_connection
def list_due_revisits(
    now: Optional[str] = None,
    *,
    limit: int = 20,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Return revisits whose ``due_at <= now`` and have not yet been revisited.

    ``now`` defaults to the current UTC time. Ordered oldest-first so the most
    overdue revisit surfaces first.
    """
    cutoff = _normalize_due_at(now) if now else _db._utcnow()
    rows = conn.execute(
        "SELECT revisit_id, signal_key, created_by_workflow, due_at, rationale, "
        "created_at FROM revisits "
        "WHERE revisited_at IS NULL AND due_at <= ? "
        "ORDER BY due_at ASC LIMIT ?",
        (cutoff, int(limit)),
    ).fetchall()
    return _db._rowdicts(rows)


@managed_connection
def mark_revisited(
    signal_keys,
    *,
    now: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Mark every pending revisit whose ``signal_key`` is in ``signal_keys`` as
    revisited. Returns the number of rows updated.
    """
    keys = sorted({(k or "").strip() for k in (signal_keys or []) if k})
    if not keys:
        return 0
    stamp = now or _db._utcnow()
    placeholders = ",".join("?" for _ in keys)
    cursor = conn.execute(
        f"UPDATE revisits SET revisited_at = ? "
        f"WHERE revisited_at IS NULL AND signal_key IN ({placeholders})",
        (stamp, *keys),
    )
    conn.commit()
    return cursor.rowcount or 0


@managed_connection
def list_pending_revisits(
    *,
    limit: int = 50,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """All pending (not-yet-revisited) revisits regardless of due_at — used by
    admin tooling, not the tick flow.
    """
    rows = conn.execute(
        "SELECT revisit_id, signal_key, created_by_workflow, due_at, rationale, "
        "created_at FROM revisits "
        "WHERE revisited_at IS NULL "
        "ORDER BY due_at ASC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return _db._rowdicts(rows)
