"""Delivery — the at-least-once intent consumer (runtime.md §5).

Carried semantics: compose THEN send THEN mark fulfilled (never
fulfil-before-send); a failed send marks the intent failed and stops THAT
LANE this tick (lane ordering is per lane — one broken lane must not block
the others, cold review 2026-07-04 #2); intents past their 6-hour expiry
drop as 'expired' (a 6-hour-old celebration reads as bot lag, not delight).

Commit discipline (cold review 2026-07-04 #1): every terminal status UPDATE
commits IMMEDIATELY, before the next compose/send. Composes are LLM calls and
sends are network round-trips — holding the write transaction across them
starves every other writer for minutes, and an escaping exception would roll
back 'fulfilled' marks for messages that were actually posted → duplicate
posts next tick.

send_fn(lane, copy) -> message_id and compose_fn(intent_row) -> str|None are
injected so the offline rehearsal passes stubs (engine.offline).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from engine.recognition import compose as _compose
from engine.recognition.scorer import parse_utc

log = logging.getLogger("elixir.engine.delivery")

MAX_INTENT_AGE_HOURS = 6   # carried from discord_consumer.py:36


def raise_intent(conn, recognition_key: str | None, intent_type: str, lane: str,
                 scope: str, payload: dict, now: str) -> int:
    """Insert a pending intent; expires 6h from raise (runtime.md §5)."""
    anchor = parse_utc(now)
    expires = (anchor + timedelta(hours=MAX_INTENT_AGE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        """INSERT INTO communication_intents
               (recognition_key, intent_type, lane, scope, payload_json,
                status, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (recognition_key, intent_type, lane, scope,
         json.dumps(payload, default=str), now, expires),
    )
    return cur.lastrowid


def consume(conn, send_fn, compose_fn, now: str, *, editor_gate=None) -> dict:
    """Deliver pending/failed intents oldest-first, per lane. Returns counters.

    editor_gate(conn, intent, copy, compose_fn, now) -> str is the Editor's
    inline gate (engine.editor.gate) — injected by the LIVE tick only, so the
    offline rehearsal and tests never touch the critic. It judges composed
    copy (never the deterministic fallback, which is grounded by construction)
    and is fail-open by contract: it returns original/revised/fallback copy
    and never raises past its own guard."""
    counters = {"delivered": 0, "expired": 0, "failed": 0, "skipped": 0}
    now_dt = parse_utc(now)
    rows = conn.execute(
        """SELECT * FROM communication_intents
           WHERE status IN ('pending', 'failed')
           ORDER BY intent_id ASC"""
    ).fetchall()

    # Group by lane: fail-stop is PER LANE — a broken lane's remainder waits
    # for the next tick; every other lane still delivers (cold review #2).
    lanes: dict[str, list] = {}
    for intent in rows:
        lanes.setdefault(intent["lane"] or "", []).append(intent)

    for lane, intents in lanes.items():
        for intent in intents:
            expires = parse_utc(intent["expires_at"])
            if expires is not None and now_dt is not None and now_dt > expires:
                conn.execute(
                    """UPDATE communication_intents
                       SET status = 'expired', last_error = 'stale_backlog'
                       WHERE intent_id = ?""",
                    (intent["intent_id"],),
                )
                conn.commit()  # terminal state lands before any network work
                counters["expired"] += 1
                log.info("intent %s expired (stale_backlog)", intent["intent_id"])
                continue
            copy = None
            try:
                copy = compose_fn(intent)
            except Exception:
                log.exception("compose failed for intent %s; using fallback", intent["intent_id"])
            if not copy or _compose.looks_like_meta(copy):
                copy = _compose.render_intent(intent)   # deterministic fallback (§7 guard)
            elif editor_gate is not None:
                try:
                    copy = editor_gate(conn, intent, copy, compose_fn, now)
                except Exception:  # belt over the gate's own fail-open braces
                    log.exception("editor gate raised for intent %s; sending original",
                                  intent["intent_id"])
            try:
                message_id = send_fn(intent["lane"], copy)
            except Exception as exc:
                # Fail-stop THIS lane; other lanes proceed. Retry next tick.
                conn.execute(
                    """UPDATE communication_intents
                       SET status = 'failed', attempts = attempts + 1, last_error = ?
                       WHERE intent_id = ?""",
                    (str(exc)[:500], intent["intent_id"]),
                )
                conn.commit()
                counters["failed"] += 1
                log.warning("send failed for intent %s; stopping lane %r: %s",
                            intent["intent_id"], lane, exc)
                break
            conn.execute(
                """UPDATE communication_intents
                   SET status = 'fulfilled', fulfilled_at = ?, discord_message_id = ?,
                       attempts = attempts + 1
                   WHERE intent_id = ?""",
                (now, str(message_id) if message_id is not None else None, intent["intent_id"]),
            )
            # Commit the fulfilled mark IMMEDIATELY: the message is live on
            # Discord; a later rollback would re-send it (cold review #1).
            conn.commit()
            counters["delivered"] += 1
    return counters
