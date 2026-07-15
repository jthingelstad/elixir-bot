"""The shared recognition ledger — one real moment, one post (recognition.md §5).

Every recognized moment claims a deterministic key BEFORE any intent is
raised; the PRIMARY KEY makes the first claim win and the second back off
(architecture §10). Claims without intents are legitimate: suppressed moments
still claim, so a re-scan can't resurrect them. Durable — never purged
(schema.md §7.1).
"""

from __future__ import annotations

import json
import logging

from engine.db import utcnow

log = logging.getLogger("elixir.engine.recognition")


def claim(
    conn, recognition_key: str, stream: str, event_refs: list, score: int
) -> bool:
    """Claim a moment. Returns True iff WE claimed it (first claimant wins)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO recognition_ledger
               (recognition_key, stream, event_refs_json, score, claimed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            recognition_key,
            stream,
            json.dumps({"refs": list(event_refs)}),
            score,
            utcnow(),
        ),
    )
    return cur.rowcount == 1


def attach_intent(conn, recognition_key: str, intent_id: int) -> None:
    conn.execute(
        "UPDATE recognition_ledger SET intent_id = ? WHERE recognition_key = ?",
        (intent_id, recognition_key),
    )


def record_suppression(
    conn, recognition_key: str, reason: str, trace: dict | None = None
) -> None:
    """Record why a claimed moment did NOT post (recognition.md §1: the engine
    can always answer "why didn't you post X?"). The reason + scoring trace
    live inside the claim row's event_refs_json."""
    row = conn.execute(
        "SELECT event_refs_json FROM recognition_ledger WHERE recognition_key = ?",
        (recognition_key,),
    ).fetchone()
    if row is None:
        return
    try:
        blob = json.loads(row[0]) if row[0] else {}
    except (TypeError, ValueError):
        blob = {}
    blob["suppressed"] = {"reason": reason, **(trace or {})}
    conn.execute(
        "UPDATE recognition_ledger SET event_refs_json = ? WHERE recognition_key = ?",
        (json.dumps(blob), recognition_key),
    )
    log.info("recognition suppressed %s: %s", recognition_key, reason)


def last_highlight_at(conn, subject_tag: str) -> str | None:
    """When the subject last got a raised/fulfilled public highlight. The
    accrual window cuts off here — already-celebrated evidence never
    double-counts (recognition.md §3 step 3). Subject identity rides in the
    intent payload (delivery.raise_intent stores subject_tag)."""
    row = conn.execute(
        """SELECT MAX(created_at) FROM communication_intents
           WHERE scope = 'public'
             AND intent_type LIKE 'celebrate:%'
             AND status IN ('pending', 'fulfilled')
             AND json_extract(payload_json, '$.subject_tag') = ?""",
        (subject_tag,),
    ).fetchone()
    return row[0] if row and row[0] else None
