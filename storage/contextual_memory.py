from __future__ import annotations

from datetime import datetime, timezone

import pytz

from db import _canon_tag, get_connection
from memory_store import archive_memory, attach_tags, create_memory, list_memories, update_memory

CHICAGO = pytz.timezone("America/Chicago")


def _weekly_event_token(now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(CHICAGO)
    return current.strftime("%G-W%V")


def _memory_summary(text: str, limit: int = 220) -> str:
    clean = " ".join((text or "").strip().split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def upsert_summary_memory(
    *,
    event_type: str,
    event_id: str,
    title: str,
    body: str,
    scope: str,
    created_by: str = "elixir",
    tags: list[str] | None = None,
    metadata: dict | None = None,
    war_season_id: str | None = None,
    war_week_id: str | None = None,
    member_tag: str | None = None,
    member_id: int | None = None,
    conn=None,
) -> dict | None:
    text = (body or "").strip()
    if not text:
        return None

    close = conn is None
    conn = conn or get_connection()
    try:
        existing = list_memories(
            viewer_scope="system_internal",
            include_system_internal=True,
            filters={"event_type": event_type, "event_id": event_id},
            limit=1,
            conn=conn,
        )
        summary = _memory_summary(text)
        payload_metadata = dict(metadata or {})
        payload_metadata.setdefault("event_id", event_id)

        member_kwargs: dict = {}
        if member_tag is not None:
            member_kwargs["member_tag"] = member_tag
        if member_id is not None:
            member_kwargs["member_id"] = member_id

        if existing:
            memory = existing[0]
            updated = update_memory(
                memory["memory_id"],
                actor=created_by,
                title=title,
                body=text,
                summary=summary,
                metadata=payload_metadata,
                **member_kwargs,
                conn=conn,
            )
            if tags:
                attach_tags(updated["memory_id"], tags, actor=created_by, conn=conn)
            return updated

        created = create_memory(
            title=title,
            body=text,
            summary=summary,
            source_type="system",
            is_inference=False,
            confidence=1.0,
            created_by=created_by,
            scope=scope,
            event_type=event_type,
            event_id=event_id,
            war_season_id=war_season_id,
            war_week_id=war_week_id,
            metadata=payload_metadata,
            **member_kwargs,
            conn=conn,
        )
        if tags:
            attach_tags(created["memory_id"], tags, actor=created_by, conn=conn)
        return created
    finally:
        if close:
            conn.close()


def upsert_weekly_summary_memory(
    *,
    event_type: str,
    title: str,
    body: str,
    scope: str,
    created_by: str = "elixir:weekly-job",
    tags: list[str] | None = None,
    metadata: dict | None = None,
    now: datetime | None = None,
    conn=None,
) -> dict | None:
    event_id = _weekly_event_token(now=now)
    payload_metadata = dict(metadata or {})
    payload_metadata.setdefault("event_token", event_id)
    return upsert_summary_memory(
        event_type=event_type,
        event_id=event_id,
        title=title,
        body=body,
        scope=scope,
        created_by=created_by,
        tags=tags,
        metadata=payload_metadata,
        conn=conn,
    )


def upsert_war_recap_memory(
    *,
    signals: list[dict],
    body: str,
    channel_id: int | str | None = None,
    workflow: str = "observation",
    created_by: str = "elixir:observation",
    conn=None,
) -> dict | None:
    signals = signals or []
    text = (body or "").strip()
    if not text:
        return None

    by_type = {signal.get("type"): signal for signal in signals if signal.get("type")}
    metadata = {"workflow": workflow}
    if channel_id is not None:
        metadata["channel_id"] = str(channel_id)

    if "war_season_complete" in by_type:
        signal = by_type["war_season_complete"]
        season_id = signal.get("season_id")
        if season_id is None:
            return None
        return upsert_summary_memory(
            event_type="war_season_recap",
            event_id=str(season_id),
            title=f"War Season {season_id} Recap",
            body=text,
            scope="public",
            created_by=created_by,
            tags=["war", "season-recap", f"season-{season_id}"],
            metadata=metadata,
            war_season_id=str(season_id),
            conn=conn,
        )

    if "war_week_complete" in by_type or "war_completed" in by_type:
        signal = by_type.get("war_week_complete") or by_type.get("war_completed") or {}
        season_id = signal.get("season_id")
        week = signal.get("week")
        if week is None and signal.get("section_index") is not None:
            week = int(signal["section_index"]) + 1
        if season_id is None or week is None:
            return None
        week_token = f"{season_id}:{week}"
        return upsert_summary_memory(
            event_type="war_week_recap",
            event_id=week_token,
            title=f"War Season {season_id} Week {week} Recap",
            body=text,
            scope="public",
            created_by=created_by,
            tags=["war", "week-recap", f"season-{season_id}", f"week-{week}"],
            metadata=metadata,
            war_season_id=str(season_id),
            war_week_id=week_token,
            conn=conn,
        )

    if "war_battle_day_complete" in by_type:
        signal = by_type["war_battle_day_complete"]
        season_id = signal.get("season_id")
        week = signal.get("week")
        day_number = signal.get("day_number")
        if season_id is None or week is None or day_number is None:
            return None
        event_id = f"{season_id}:{week}:{day_number}"
        return upsert_summary_memory(
            event_type="war_battle_day_recap",
            event_id=event_id,
            title=f"War Season {season_id} Week {week} Battle Day {day_number} Recap",
            body=text,
            scope="public",
            created_by=created_by,
            tags=["war", "battle-day-recap", f"season-{season_id}", f"week-{week}", f"day-{day_number}"],
            metadata=metadata,
            war_season_id=str(season_id),
            war_week_id=f"{season_id}:{week}",
            conn=conn,
        )

    return None


def upsert_member_note_memory(
    *,
    member_tag: str,
    member_label: str,
    note: str,
    created_by: str = "leader:admin",
    metadata: dict | None = None,
    conn=None,
) -> dict | None:
    tag = _canon_tag(member_tag)
    text = (note or "").strip()
    if not tag or not text:
        return None

    close = conn is None
    conn = conn or get_connection()
    try:
        member_row = conn.execute(
            "SELECT m.member_id, m.current_name, cs.role "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.player_tag = ?",
            (tag,),
        ).fetchone()
        title = f"Leader Note: {member_label}"
        payload_metadata = dict(metadata or {})
        payload_metadata.setdefault("member_label", member_label)
        memory = upsert_summary_memory(
            event_type="member_note",
            event_id=tag,
            title=title,
            body=text,
            scope="leadership",
            created_by=created_by,
            tags=["leader-note", "member-note"],
            metadata=payload_metadata,
            conn=conn,
        )
        if memory and member_row:
            memory = update_memory(
                memory["memory_id"],
                actor=created_by,
                member_id=member_row["member_id"],
                member_tag=tag,
                role=member_row["role"],
                conn=conn,
            )
        return memory
    finally:
        if close:
            conn.close()


def archive_member_note_memory(
    *,
    member_tag: str,
    actor: str = "leader:admin",
    conn=None,
) -> dict | None:
    tag = _canon_tag(member_tag)
    if not tag:
        return None

    close = conn is None
    conn = conn or get_connection()
    try:
        existing = list_memories(
            viewer_scope="system_internal",
            include_system_internal=True,
            include_archived=True,
            filters={"event_type": "member_note", "event_id": tag},
            limit=1,
            conn=conn,
        )
        if not existing:
            return None
        memory = existing[0]
        if memory.get("status") == "archived":
            return memory
        return archive_memory(memory["memory_id"], actor=actor, conn=conn)
    finally:
        if close:
            conn.close()


__all__ = [
    "archive_member_note_memory",
    "upsert_member_note_memory",
    "upsert_summary_memory",
    "upsert_weekly_summary_memory",
    "upsert_war_recap_memory",
]
