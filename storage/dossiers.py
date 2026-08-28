"""What Elixir knows about people, and what it means to do about them.

Agentic Loop v2, Phase 5. Two small stores that share one idea: Elixir's memory
of a member should include the things that are true about them as a person and
not just the numbers the API returns.

- **Dossiers** — one short body per member: "phone broke, said he'd be back",
  "asks for deck help most weeks", "third stint with us", plus one shared active
  focus carried by an evidence-grounded workflow. Reflection owns the
  member-authored body; the focus is a bounded intention, not a member fact or a
  report archive. Both are injected by the chassis for members in a turn's scope.
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

from db import _canon_tag, managed_connection

log = logging.getLogger("elixir")

# A dossier is a paragraph, not a file. The plan's budget is ~500 tokens; this
# caps the characters that can reach a prompt regardless of what wrote the row,
# because the writer is a model and "keep it short" is not an enforcement.
DOSSIER_MAX_CHARS = 2000
ACTIVE_FOCUS_MAX_CHARS = 600


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
    tag = _canon_tag(player_tag)
    body = (body or "").strip()[:DOSSIER_MAX_CHARS]
    if not tag or not body:
        return False
    conn.execute(
        "INSERT INTO member_dossiers (player_tag, body, updated_at, updated_by, "
        "source_intent_key) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(player_tag) DO UPDATE SET body = excluded.body, "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by, "
        "source_intent_key = excluded.source_intent_key",
        (tag, body, _utcnow(), updated_by, source_intent_key),
    )
    return True


@managed_connection
def set_active_focus(
    player_tag: str,
    focus: str,
    *,
    source: str,
    period: str | None = None,
    conn=None,
) -> bool:
    """Set the dossier's shared active focus without replacing its human context.

    A focus may originate in a weekly report today and another grounded workflow
    tomorrow. Keeping it as a dossier field lets every scoped consumer see the
    same carried intention while retaining the reflection-written body and its
    provenance.
    """
    tag = _canon_tag(player_tag)
    text = (focus or "").strip()[:ACTIVE_FOCUS_MAX_CHARS]
    writer = (source or "").strip()
    if not tag or not text or not writer:
        return False
    now = _utcnow()
    conn.execute(
        "INSERT INTO member_dossiers (player_tag, body, updated_at, updated_by, "
        "source_intent_key, active_focus, active_focus_source, active_focus_period, "
        "active_focus_updated_at) VALUES (?, '', ?, ?, NULL, ?, ?, ?, ?) "
        "ON CONFLICT(player_tag) DO UPDATE SET active_focus = excluded.active_focus, "
        "active_focus_source = excluded.active_focus_source, "
        "active_focus_period = excluded.active_focus_period, "
        "active_focus_updated_at = excluded.active_focus_updated_at",
        (tag, now, writer, text, writer, (period or "").strip() or None, now),
    )
    return True


@managed_connection
def dossier_for(player_tag: str, *, conn=None) -> dict | None:
    """Structured private dossier state for one member."""
    tag = _canon_tag(player_tag)
    if not tag:
        return None
    row = conn.execute(
        "SELECT player_tag, body, updated_at, updated_by, source_intent_key, "
        "active_focus, active_focus_source, active_focus_period, active_focus_updated_at "
        "FROM member_dossiers WHERE player_tag = ?",
        (tag,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["body"] = (item.get("body") or "")[:DOSSIER_MAX_CHARS]
    item["active_focus"] = (item.get("active_focus") or "")[:ACTIVE_FOCUS_MAX_CHARS]
    return item


def _prompt_dossier(row) -> str:
    body = (row["body"] or "").strip()
    focus = (row["active_focus"] or "").strip()
    parts: list[str] = []
    if focus:
        period = (row["active_focus_period"] or "").strip()
        source = (row["active_focus_source"] or "").strip()
        origin = ", ".join(value for value in (source, period) if value)
        label = f"Active focus ({origin})" if origin else "Active focus"
        parts.append(f"{label}: {focus}")
    if body:
        parts.append(body)
    return "\n\n".join(parts)[:DOSSIER_MAX_CHARS]


@managed_connection
def dossiers_for(player_tags, *, conn=None) -> dict:
    """Prompt-safe shared dossier context for members in a turn's scope."""
    tags = [_canon_tag(t) for t in (player_tags or [])]
    tags = [tag for tag in tags if tag]
    if not tags:
        return {}
    rows = conn.execute(
        f"SELECT player_tag, body, active_focus, active_focus_source, active_focus_period "
        f"FROM member_dossiers "
        f"WHERE player_tag IN ({','.join('?' for _ in tags)})",
        tuple(tags),
    ).fetchall()
    return {r["player_tag"]: text for r in rows if (text := _prompt_dossier(r))}


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
        "WHERE status = 'pending' AND datetime(due_at) <= datetime(?) "
        "ORDER BY datetime(due_at), followup_id LIMIT ?",
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
    "ACTIVE_FOCUS_MAX_CHARS",
    "DOSSIER_MAX_CHARS",
    "dossier_for",
    "dossiers_for",
    "due_followups",
    "mark_followup_fired",
    "schedule_followup",
    "set_active_focus",
    "upsert_dossier",
]
