"""The Observatory chat panel — "work with Elixir".

Wired to the same agent path as Discord channel Q&A
(elixir_agent.respond_in_channel) with the clanops workflow (leadership
tools — this surface is admin-only by the Tailscale gate). Messages persist
in the same conversation store as Discord under scope 'webadmin:admin', so
the panel has memory across sessions and restarts.
"""

from __future__ import annotations

import asyncio
import logging

from storage import messages as messages_store

log = logging.getLogger("elixir.webapp")

CHAT_SCOPE = "webadmin:admin"
CHANNEL_NAME = "#observatory"

_TASKS: set = set()


def list_messages(limit: int = 50) -> list[dict]:
    msgs = messages_store.list_thread_messages(CHAT_SCOPE, limit=limit)
    return [
        {
            "role": m.get("role"),
            "content": m.get("content") or "",
            "recorded_at": m.get("recorded_at"),
        }
        for m in msgs
    ]


def _save(author_type: str, content: str) -> None:
    messages_store.save_message(
        CHAT_SCOPE,
        author_type,
        content,
        channel_name=CHANNEL_NAME,
        channel_kind="webadmin",
        workflow="clanops",
    )


def _run_agent(question: str, login: str, prior: list[dict]) -> str:
    import db
    import elixir_agent

    try:  # ranked, query-aware memory context (memory.md §2.3)
        memory_context = db.build_memory_context(
            channel_id=CHANNEL_NAME,
            viewer_scope="leadership",
            query=question,
        )
    except Exception:
        log.debug("Observatory chat memory context unavailable", exc_info=True)
        memory_context = None
    result = elixir_agent.respond_in_channel(
        question=question,
        author_name=login or "Jamie (web)",
        channel_name=CHANNEL_NAME,
        workflow="clanops",
        clan_data={},
        war_data=None,
        conversation_history=prior,
        memory_context=memory_context,
    )
    if isinstance(result, dict):
        return result.get("content", result.get("summary", "")) or "(no reply)"
    return str(result) if result else "(no reply)"


async def post_message(question: str, login: str) -> None:
    """Store the user message, then run the agent in a background task so the
    web request returns immediately (the panel polls for the reply)."""
    prior = await asyncio.to_thread(list_messages, 20)
    await asyncio.to_thread(_save, "user", question)

    async def _respond():
        try:
            reply = await asyncio.to_thread(_run_agent, question, login, prior)
        except Exception as exc:  # keep the panel alive on agent failure
            log.exception("webadmin chat agent failed")
            reply = f"(agent error: {exc})"
        await asyncio.to_thread(_save, "assistant", reply)

    task = asyncio.create_task(_respond())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
