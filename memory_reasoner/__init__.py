from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from memory_store import list_memories, search_memories


@dataclass
class MemorySummary:
    facts: list[dict]
    leadership_notes: list[dict]
    assistant_inferences: list[dict]
    system_notes: list[dict]
    open_questions: list[str]


def package_prompt_context(*, facts: Optional[list[dict]] = None, memories: Optional[list[dict]] = None) -> dict:
    facts = facts or []
    memories = memories or []
    leadership = [m for m in memories if m.get("source_type") == "leader_note"]
    inferences = [m for m in memories if m.get("source_type") == "elixir_inference"]
    system_notes = [m for m in memories if m.get("source_type") == "system"]
    return {
        "facts": facts,
        "leadership_memories": leadership,
        "assistant_inferences": inferences,
        "system_notes": system_notes,
    }


def summarize_memories(*, facts: Optional[list[dict]] = None, memories: Optional[list[dict]] = None) -> MemorySummary:
    packet = package_prompt_context(facts=facts, memories=memories)
    open_questions = []
    for inference in packet["assistant_inferences"]:
        conf = float(inference.get("confidence") or 0.0)
        if conf < 0.6:
            open_questions.append(
                f"Inference {inference.get('memory_id')} has low confidence ({conf:.2f}) and needs confirmation."
            )
    return MemorySummary(
        facts=packet["facts"],
        leadership_notes=packet["leadership_memories"],
        assistant_inferences=packet["assistant_inferences"],
        system_notes=packet["system_notes"],
        open_questions=open_questions,
    )


def summarize_member_memories(member_id: int, *, viewer_scope: str = "leadership", conn=None) -> MemorySummary:
    memories = list_memories(viewer_scope=viewer_scope, filters={"member_id": member_id}, conn=conn)
    return summarize_memories(memories=memories)


def summarize_war_week_memories(war_week_id: str, *, viewer_scope: str = "leadership", conn=None) -> MemorySummary:
    memories = list_memories(viewer_scope=viewer_scope, filters={"war_week_id": war_week_id}, conn=conn)
    return summarize_memories(memories=memories)


def summarize_war_season_memories(war_season_id: str, *, viewer_scope: str = "leadership", conn=None) -> MemorySummary:
    memories = list_memories(viewer_scope=viewer_scope, filters={"war_season_id": war_season_id}, conn=conn)
    return summarize_memories(memories=memories)


def summarize_topic_date_range(topic: str, *, created_after: Optional[str] = None, created_before: Optional[str] = None,
                               viewer_scope: str = "leadership", conn=None) -> MemorySummary:
    results = search_memories(
        topic,
        viewer_scope=viewer_scope,
        filters={"created_after": created_after, "created_before": created_before},
        conn=conn,
    )
    return summarize_memories(memories=[r.memory for r in results])


def format_memory_for_response(memory: dict) -> str:
    source = memory.get("source_type")
    if source == "leader_note":
        return f"Leadership noted: {memory.get('summary') or memory.get('body')}"
    if source == "elixir_inference":
        conf = float(memory.get("confidence") or 0.0)
        label = "low" if conf < 0.5 else "moderate" if conf < 0.8 else "high"
        return f"Elixir inferred with {label} confidence ({conf:.2f}) that {memory.get('summary') or memory.get('body')}"
    return f"System record indicates: {memory.get('summary') or memory.get('body')}"


__all__ = [
    "MemorySummary",
    "package_prompt_context",
    "summarize_memories",
    "summarize_member_memories",
    "summarize_war_week_memories",
    "summarize_war_season_memories",
    "summarize_topic_date_range",
    "format_memory_for_response",
]
