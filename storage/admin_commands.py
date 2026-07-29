"""Durable usage telemetry for Discord application commands."""

from __future__ import annotations

import sqlite3
from typing import Optional

from db import _rowdicts, _utcnow, managed_connection


@managed_connection
def record_admin_command_invocation(
    command_key: str,
    event_type: str,
    *,
    discord_user_id: str | int | None,
    channel_id: str | int | None,
    write_requested: bool,
    accepted: bool,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Record one command attempt without persisting arguments or reply text."""
    cursor = conn.execute(
        "INSERT INTO admin_command_invocations "
        "(command_key, event_type, discord_user_id, channel_id, write_requested, "
        "accepted, invoked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            str(command_key),
            str(event_type),
            str(discord_user_id) if discord_user_id is not None else None,
            str(channel_id) if channel_id is not None else None,
            1 if write_requested else 0,
            1 if accepted else 0,
            _utcnow(),
        ),
    )
    return int(cursor.lastrowid)


@managed_connection
def list_admin_command_usage(
    *,
    accepted_only: bool = True,
    since: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Aggregate durable command use for later surface-retirement decisions."""
    where: list[str] = []
    params: list[object] = []
    if accepted_only:
        where.append("accepted = 1")
    if since:
        where.append("invoked_at >= ?")
        params.append(str(since))
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        "SELECT command_key, event_type, COUNT(*) AS invocation_count, "
        "MAX(invoked_at) AS last_invoked_at "
        f"FROM admin_command_invocations {clause} "
        "GROUP BY command_key, event_type ORDER BY invocation_count DESC, command_key",
        params,
    ).fetchall()
    return _rowdicts(rows)
