"""The Editor — Elixir's editorial-memory feeders + the verdict-table schema.

Reduced 2026-07-10: the inline gate/critic/judge, the living-rubric builder, the
daily feedback sweep, and the weekly self-review were retired when the awareness
brain became the sole proactive poster (it composes with depth natively — there
is no per-post gate anymore). See RELEASES / the awareness transition.

What remains is live:
- ``ensure_editor_schema`` — the ``editor_verdicts`` table, still read by the
  Observatory editorial view for historical verdicts;
- the auto-feeders that turn human actions into editorial rubric memories:
  ``record_deleted_post`` (an admin deletes an Elixir post) and
  ``record_copy_edit_pair`` (a leader rewrites action-card copy). These populate
  the Observatory editorial page.
"""

from __future__ import annotations

import logging

log = logging.getLogger("elixir.engine.editor")

EDITOR_VERDICTS_DDL = """
CREATE TABLE IF NOT EXISTS editor_verdicts (
    verdict_id INTEGER PRIMARY KEY,
    intent_id INTEGER NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass','revise','fallback','error')),
    dimensions_json TEXT,
    original_copy TEXT, final_copy TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_editor_verdicts_intent ON editor_verdicts(intent_id);
"""


def ensure_editor_schema(conn) -> None:
    conn.executescript(EDITOR_VERDICTS_DDL)


# ------------------------------------------------- editorial rubric memories

def _editorial_memory_exists(conn, event_key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM memories WHERE source_event_key = ?", (event_key,)
    ).fetchone() is not None


def _add_editorial_memory(conn, *, title: str, body: str, kind_tag: str,
                          event_key: str, confidence: float, created_by: str,
                          extra_tags: tuple[str, ...] = (),
                          source_type: str = "inference") -> int | None:
    """One rubric entry, deduped on its event key. Returns memory_id or None."""
    import memory_store

    if _editorial_memory_exists(conn, event_key):
        return None
    et, _, eid = event_key.partition(":")
    mem = memory_store.create_memory(
        body=body, source_type=source_type,
        is_inference=(source_type == "inference"), confidence=confidence,
        created_by=created_by, scope="leadership", title=title,
        event_type=et, event_id=eid or None, conn=conn,
    )
    memory_store.attach_tags(
        mem["memory_id"], ["editorial", kind_tag, *extra_tags],
        actor=created_by, conn=conn,
    )
    return mem["memory_id"]


def record_deleted_post(conn, discord_message_id: str, channel_name: str,
                        content: str) -> int | None:
    """Deletion feeder: an admin deleting an Elixir post in one of its lanes is
    the strongest anti-pattern signal we get."""
    intent = conn.execute(
        "SELECT intent_type, payload_json FROM communication_intents "
        "WHERE discord_message_id = ?",
        (str(discord_message_id),),
    ).fetchone()
    facts_note = ""
    if intent:
        facts_note = (f"\n\nIntent type: {intent['intent_type']}\n"
                      f"Facts it was composed from: {intent['payload_json'][:500]}")
    mid = _add_editorial_memory(
        conn,
        title="Anti-pattern: post deleted by an admin",
        body=(f"An admin deleted this Elixir post in #{channel_name}:\n\n"
              f"{(content or '(content unavailable)')[:600]}{facts_note}"),
        kind_tag="anti-pattern",
        event_key=f"editorial_deletion:{discord_message_id}",
        confidence=0.75,
        created_by="editor-deletion",
    )
    conn.commit()
    return mid


def record_copy_edit_pair(conn, action_id: int, before: str, after: str) -> int | None:
    """Copy-edit feeder: a leader rewriting action-card copy is a paired
    before/after exemplar — the after is what good looks like."""
    before, after = (before or "").strip(), (after or "").strip()
    if not before or not after or before == after:
        return None
    mid = _add_editorial_memory(
        conn,
        title="Exemplar pair: leader copy-edit on an action card",
        body=(f"A leader rewrote Elixir's action-card copy — the AFTER is the "
              f"standard to emulate.\n\nBEFORE (Elixir): {before[:400]}\n\n"
              f"AFTER (leader): {after[:400]}"),
        kind_tag="exemplar",
        event_key=f"editorial_copy_edit:{action_id}",
        confidence=0.8,
        created_by="editor-copy-edit",
    )
    conn.commit()
    return mid
