"""One execution chassis: every Elixir turn differs only by data.

Agentic Loop v2, Phase 1 (docs/plans/agentic-loop.md).

Today Elixir is 16 entry points, 25 system-prompt builders and 27 workflow
specs, each hand-assembling its own prompt, its own context, its own tool
surface and its own delivery. That duplication is why a lesson learned from a
bad post reaches the awareness brain and nothing else, and why "the composer
succeeded but the caller mishandled delivery" is a bug category at all.

The chassis makes those cross-cutting concerns exist exactly once:

  assemble_system   identity + knowledge + policy + job file + surface guidance
  assemble_context  the seed, plus lessons and recent posts injected for FREE
  run_turn          one tool loop, one write-policy gate, one staging area
  Episode           what woke it, what it read, what it posted, what it cost

An :class:`Attention` is the data. ``job`` names a file in ``prompts/jobs/``,
which is the ONLY per-purpose prose in the turn; ``surfaces`` decides which
posting tools are live, which is why the same code welcomes a member to Discord
and mails a recap without a second builder that "omits the Discord blocks".

**Posting is a tool call, and staging is deliberate.** ``post_to_discord``
validates immediately and stages; the responder delivers the staged plan through
``runtime.awareness.deliver.deliver_posts`` after the turn. The model gets its
rejection feedback in-loop, and delivery still goes through the one durable path
that owns the hard-post floor, the outbox, idempotency and the clan-chat
sibling. A second delivery path is what the v4 architecture died of.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field

log = logging.getLogger("elixir")

# Surfaces an attention can be given. A posting tool is offered only when its
# surface is present, so "can this turn speak here?" is data on the attention
# rather than a per-workflow toolset constant somebody has to remember to edit.
SURFACE_DISCORD_ANNOUNCEMENTS = "discord:announcements"
SURFACE_DISCORD_ELIXIR = "discord:elixir"
SURFACE_CLAN_CHAT = "clan_chat"

_DISCORD_SURFACES = {
    SURFACE_DISCORD_ANNOUNCEMENTS: "announcements",
    SURFACE_DISCORD_ELIXIR: "elixir",
}


@dataclass(frozen=True)
class Scope:
    """What this turn is allowed to be about.

    Drives context assembly: dossiers and profiles for ``member_tags``, recent
    posts for ``lanes``. It is not a security boundary — it is the answer to
    "what should I have in front of me", which is the whole reason a scoped turn
    costs 25K tokens instead of 300K.
    """

    member_tags: tuple[str, ...] = ()
    lanes: tuple[str, ...] = ()
    signal_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Budget:
    """What this turn may spend."""

    model_family: str = "lightweight"
    max_tool_rounds: int = 6
    max_tokens: int = 2000


@dataclass(frozen=True)
class Attention:
    """One unit of cognition: what woke it, what it may see, what it may say."""

    job: str
    trigger: dict = field(default_factory=dict)
    scope: Scope = field(default_factory=Scope)
    surfaces: frozenset = frozenset()
    budget: Budget = field(default_factory=Budget)
    # Signal keys this turn MUST cover. Checked after the turn by the caller,
    # against what actually reached the outbox — never against what the model
    # said it did.
    floor: frozenset = frozenset()
    workflow: str = "wake_response"


# ---------------------------------------------------------------------------
# Staging — posting is a tool call
# ---------------------------------------------------------------------------


class _Staging:
    """Posts accepted during one turn, in ``deliver_posts`` plan shape."""

    def __init__(self, attention: Attention):
        self.attention = attention
        self.posts: list[dict] = []
        self.rejections: list[str] = []

    def stage_discord(self, *, lane: str, content: str, covers: list[str]) -> dict:
        post = {
            "channel": lane,
            "content": content,
            "covers_signal_keys": sorted({str(k) for k in (covers or []) if k}),
            "leads_with": _LEADS_WITH_BY_LANE.get(lane, "clan_event"),
            "summary": "",
            "member_tags": list(self.attention.scope.member_tags),
            "member_names": [],
        }
        self.posts.append(post)
        return post

    def attach_clan_chat(self, content: str) -> dict | None:
        """The in-game line rides on its Discord sibling — that is the shape
        ``deliver_posts`` and the join copy-policy both expect. Composing it in
        the same turn is what keeps the two surfaces telling one story."""
        if not self.posts:
            return None
        post = self.posts[-1]
        post["clan_chat"] = [content]
        post.setdefault("relay_reason", "")
        return post


_LEADS_WITH_BY_LANE = {"announcements": "clan_event", "elixir": "milestone"}

# The staging area for the turn currently executing on this thread. A ContextVar
# rather than a parameter because the tool executor's signature is shared by
# ~60 tools and is not the place to thread chassis state.
_ACTIVE_STAGING: contextvars.ContextVar = contextvars.ContextVar(
    "elixir_chassis_staging", default=None
)


def active_staging() -> _Staging | None:
    return _ACTIVE_STAGING.get()


# ---------------------------------------------------------------------------
# Assembly — one recipe, parameterized by the attention
# ---------------------------------------------------------------------------


def assemble_system(attention: Attention) -> str:
    """The one system-prompt recipe.

    Identity, knowledge (which carries GAME.md — its absence is why the
    2026-08-04 experiment misread a war-league change as a demotion), policy,
    the job file, the lane rules for whichever surfaces are live, and the
    formatting guidance those surfaces need.
    """
    import prompts
    from agent.core import _build_system_prompt
    from agent.prompt_builders import _discord_emoji_guidance, _discord_formatting_guidance

    blocks = [
        prompts.identity_block(),
        prompts.knowledge_block(),
        prompts.policy(),
        prompts.job_prompt(attention.job),
    ]

    discord_lanes = [
        lane for surface, lane in _DISCORD_SURFACES.items() if surface in attention.surfaces
    ]
    for lane in discord_lanes:
        blocks.append(prompts.lane_prompt(lane))
    if SURFACE_CLAN_CHAT in attention.surfaces:
        blocks.append(prompts.lane_prompt("clan-chat"))
    if discord_lanes:
        blocks.append(_discord_formatting_guidance())
        blocks.append(_discord_emoji_guidance())
    return _build_system_prompt(*blocks)


def assemble_context(attention: Attention, seed: dict) -> dict:
    """The seed, plus what every turn should always have and no caller should
    have to remember.

    Editorial lessons are the point. They already flow into the awareness
    brain's read and nowhere else, so a lesson learned from a bad welcome never
    reaches deck review or #ask-elixir. Injecting them here means improving them
    improves every chassis turn at once — and because prompt files and memory
    both hot-load, a lesson written tonight shapes tomorrow morning's posts with
    no deploy.
    """
    context = dict(seed or {})
    context["lessons"] = _editorial_lessons()
    if attention.scope.lanes:
        context["recent_posts"] = _recent_posts(attention.scope.lanes)
    return context


def _editorial_lessons(limit: int = 12) -> list[dict]:
    try:
        from storage.contextual_memory import list_memories

        rows = list_memories(
            viewer_scope="system_internal",
            include_system_internal=True,
            filters={"tags": ["editorial"]},
            limit=limit,
        )
        return [
            {"title": r.get("title"), "body": r.get("body")} for r in rows if isinstance(r, dict)
        ]
    except Exception:
        # A turn without lessons is worse than one with, but far better than a
        # turn that does not happen. The floor still guarantees the post.
        log.debug("chassis: editorial lessons unavailable", exc_info=True)
        return []


def _recent_posts(lanes, limit: int = 5) -> list[dict]:
    """What Elixir already said in these lanes. Every composer needs this to
    avoid repeating itself; today each one fetches it separately or not at all."""
    try:
        import db

        conn = db.get_connection()
        try:
            placeholders = ",".join("?" for _ in lanes)
            rows = conn.execute(
                f"SELECT lane, created_at, substr(content, 1, 400) AS content "
                f"FROM awareness_delivery_intents WHERE lane IN ({placeholders}) "
                f"AND status = 'fulfilled' ORDER BY created_at DESC LIMIT ?",
                (*lanes, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        log.debug("chassis: recent posts unavailable", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------


def surface_tools(attention: Attention) -> list[dict]:
    """The posting tools this attention may call. Absent surface, absent tool —
    a turn cannot speak somewhere it was not sent."""
    from agent.tool_defs import TOOLS_BY_NAME

    names = []
    if any(surface in attention.surfaces for surface in _DISCORD_SURFACES):
        names.append("post_to_discord")
    if SURFACE_CLAN_CHAT in attention.surfaces:
        names.append("post_to_clan_chat")
    return [TOOLS_BY_NAME[name] for name in names if name in TOOLS_BY_NAME]


def run_turn(attention: Attention, seed: dict, *, read_tools=None, on_event=None) -> dict:
    """Execute one chassis turn. Returns an episode dict.

    The episode is the record the reflection loop reads: what woke this, what it
    posted, what it cost, what it refused. It is returned rather than persisted
    here so the caller owns the transaction — the same discipline the awareness
    loop uses for its thought.
    """
    import json

    from agent.tool_policy import INTERACTIVE_READ_TOOLS
    from agent.workflows import _chat_with_tools

    context = assemble_context(attention, seed)
    system = assemble_system(attention)
    tools = list(read_tools if read_tools is not None else INTERACTIVE_READ_TOOLS)
    tools.extend(surface_tools(attention))

    staging = _Staging(attention)
    token = _ACTIVE_STAGING.set(staging)
    tool_stats: dict = {}
    try:
        result = _chat_with_tools(
            system,
            json.dumps(context, indent=1, default=str),
            workflow=attention.workflow,
            allowed_tools=tools,
            max_tokens=attention.budget.max_tokens,
            strict_json=False,
            return_errors=True,
            tool_stats=tool_stats,
            on_event=on_event,
        )
    except Exception as exc:
        log.exception("chassis: turn raised (job=%s)", attention.job)
        result = {"_error": {"kind": "exception", "detail": str(exc)}}
    finally:
        _ACTIVE_STAGING.reset(token)

    error = result.get("_error") if isinstance(result, dict) else None
    return {
        "job": attention.job,
        "workflow": attention.workflow,
        "model_family": attention.budget.model_family,
        "trigger": attention.trigger,
        "scope": {
            "member_tags": list(attention.scope.member_tags),
            "lanes": list(attention.scope.lanes),
            "signal_keys": list(attention.scope.signal_keys),
        },
        "posts": staging.posts,
        "rejections": staging.rejections,
        "tool_trace": tool_stats.get("tool_trace") or [],
        "error": error,
        "lessons_injected": len(context.get("lessons") or []),
    }


__all__ = [
    "SURFACE_CLAN_CHAT",
    "SURFACE_DISCORD_ANNOUNCEMENTS",
    "SURFACE_DISCORD_ELIXIR",
    "Attention",
    "Budget",
    "Scope",
    "active_staging",
    "assemble_context",
    "assemble_system",
    "run_turn",
    "surface_tools",
]
