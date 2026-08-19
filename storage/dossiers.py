"""What Elixir knows about people, and what it means to do about them.

Agentic Loop v2, Phase 5. Two small stores that share one idea: Elixir's memory
of a member should include the things that are true about them as a person and
not just the numbers the API returns.

- **Dossiers** — one short body per member: "phone broke, said he'd be back",
  "asks for deck help most weeks", "third stint with us". Written only by the
  nightly reflection, injected by the chassis for members in a turn's scope.
- **Follow-ups** — an intention with a due date. "Ask canavar how the phone is."
  When one comes due the engine tick emits a `followup_due` event and it travels
  the ordinary wake path, so a carried intention is not a second scheduler.

**Both hold member data and model-authored text.** They live in the clan database
and never in git; the repo is public. Dossier text reaching a prompt is also
model-authored text re-entering a model, so it is length-capped on the way out.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from db import managed_connection

log = logging.getLogger("elixir")

# A dossier is a paragraph, not a file. The plan's budget is ~500 tokens; this
# caps the characters that can reach a prompt regardless of what wrote the row,
# because the writer is a model and "keep it short" is not an enforcement.
DOSSIER_MAX_CHARS = 2000


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@managed_connection
def upsert_dossier(
    player_tag: str,
    body: str,
    *,
    updated_by: str,
    source_intent_key: str | None = None,
    conn=None,
) -> bool:
    """Replace a member's dossier. Returns True if written."""
    body = (body or "").strip()[:DOSSIER_MAX_CHARS]
    if not player_tag or not body:
        return False
    conn.execute(
        "INSERT INTO member_dossiers (player_tag, body, updated_at, updated_by, "
        "source_intent_key) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(player_tag) DO UPDATE SET body = excluded.body, "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by, "
        "source_intent_key = excluded.source_intent_key",
        (player_tag, body, _utcnow(), updated_by, source_intent_key),
    )
    return True


@managed_connection
def dossiers_for(player_tags, *, conn=None) -> dict:
    """``{player_tag: body}`` for the members in a turn's scope."""
    tags = [str(t) for t in (player_tags or []) if t]
    if not tags:
        return {}
    rows = conn.execute(
        f"SELECT player_tag, body FROM member_dossiers "
        f"WHERE player_tag IN ({','.join('?' for _ in tags)})",
        tuple(tags),
    ).fetchall()
    return {r["player_tag"]: (r["body"] or "")[:DOSSIER_MAX_CHARS] for r in rows}


@managed_connection
def schedule_followup(
    *, due_at: str, why: str, player_tag: str | None = None, created_by: str, conn=None
) -> int | None:
    """Carry an intention forward to a date. Returns the followup id."""
    why = (why or "").strip()
    if not due_at or not why:
        return None
    cur = conn.execute(
        "INSERT INTO scheduled_followups (due_at, why, player_tag, created_at, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (due_at, why[:600], player_tag, _utcnow(), created_by),
    )
    return int(cur.lastrowid)


@managed_connection
def due_followups(*, now: str | None = None, limit: int = 10, conn=None) -> list[dict]:
    """Pending follow-ups whose time has come."""
    rows = conn.execute(
        "SELECT followup_id, due_at, why, player_tag FROM scheduled_followups "
        "WHERE status = 'pending' AND due_at <= ? ORDER BY due_at LIMIT ?",
        (now or _utcnow(), int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


@managed_connection
def mark_followup_fired(followup_id: int, *, conn=None) -> None:
    """Fired means EMITTED, not delivered.

    The emitted event carries the ordinary guarantees from here — it sits past
    the awareness cursor until something covers it, and a failed responder turn
    leaves it for the daily deliberation. Keeping the row pending as well would
    give one intention two independent retry mechanisms, which is how a gentle
    check-in becomes a member being asked the same question four times.
    """
    conn.execute(
        "UPDATE scheduled_followups SET status = 'fired', fired_at = ? WHERE followup_id = ?",
        (_utcnow(), int(followup_id)),
    )


__all__ = [
    "DOSSIER_MAX_CHARS",
    "dossiers_for",
    "due_followups",
    "mark_followup_fired",
    "schedule_followup",
    "upsert_dossier",
]
