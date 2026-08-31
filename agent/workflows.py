import json
import re
import sqlite3

from anthropic import APIConnectionError, APIError

import db
from agent.chat import (
    _clan_context,
    _extract_reference_codes,
    _format_memory_context,
    _format_recent_posts,
    _parse_response,
)
from agent.core import (
    MAX_CONTEXT_MEMBERS_DEFAULT,
    MAX_CONTEXT_MEMBERS_FULL,
    _create_chat_completion,
    log,
    response_text,
)
from agent.prompt_builders import (
    _ask_elixir_daily_system,
    _awareness_system,
    _channel_lane_system,
    _clan_chat_copy_system,
    _clanops_system,
    _deck_review_system,
    _elder_standing_system,
    _event_system,
    _help_system,
    _intel_report_system,
    _interactive_system,
    _leader_action_feedback_system,
    _leader_note_interpret_system,
    _member_outreach_ask_system,
    _member_report_system,
    _memory_synthesis_system,
    _promote_system,
    _reception_system,
    _reflection_system,
    _screenshot_readout_system,
    _tournament_recap_system,
    _tournament_update_system,
    _war_intel_system,
    _weekly_recap_email_system,
    _weekly_recap_system,
)
from agent.tool_policy import RESPONSE_SCHEMAS_BY_WORKFLOW, TOOLSETS_BY_WORKFLOW
from capabilities import members as member_capability
from capabilities import war as war_capability


def _chat_with_tools(*args, **kwargs):
    # Deliberate late binding through the facade so tests that patch
    # elixir_agent._chat_with_tools intercept workflow-internal calls.
    import elixir_agent

    return elixir_agent._chat_with_tools(*args, **kwargs)


def _clan_trend_prompt_context(days=30, window_days=7):
    try:
        return db.build_clan_trend_summary_context(days=days, window_days=window_days) or ""
    except sqlite3.Error as exc:
        log.warning("Clan trend summary context unavailable: %s", exc)
        return ""


def _war_status_prompt_context():
    """Return the single 'war now' block shared with the get_river_race tool."""
    from agent.war_render import render_war_now

    try:
        snapshot = _war_status_snapshot()
        data = snapshot.get("clock") if snapshot.get("available") else None
    except Exception as exc:
        log.warning("War status context unavailable: %s", exc)
        return ""
    return render_war_now(data) if data else ""


def _war_status_snapshot():
    return war_capability.get_war_intelligence(source=db)


def _repair_channel_game_facts(response: dict, findings: list[dict], facts: dict):
    """One no-tools wording repair; factual contract and response shape stay fixed."""
    system = (
        "Repair a structured Discord response that contradicts canonical Clash Royale "
        "mechanics. Return JSON only. Preserve event_type, summary, metadata, decisions, "
        "member identities, and every field except content/share_content. Change only the "
        "minimum wording needed to agree with CANONICAL FACTS. Do not add tools or claims. "
        "For Colosseum: there is no finish line and every battle across all four battle "
        "days continues to count toward clan and member standings."
    )
    return _chat_with_tools(
        system,
        json.dumps(
            {"canonical_facts": facts, "findings": findings, "response": response},
            indent=2,
            default=str,
        ),
        workflow="game_factual_repair",
        allowed_tools=TOOLSETS_BY_WORKFLOW["game_factual_repair"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["game_factual_repair"],
        strict_json=True,
        return_errors=True,
    )


_WAR_MENTION_PATTERNS = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"\bwar\b",
        r"\briver race\b",
        r"\brace\b",
        r"\bfame\b",
        r"\bboat\b",
        r"\bdeck.{0,5}(usage|status|today)\b",
        r"\bbattle day\b",
        r"\bpractice day\b",
        r"\bcolosseum\b",
    )
)


def _mentions_war(text):
    """Return True if the text references River Race / war concepts."""
    if not text:
        return False
    return any(pat.search(text) for pat in _WAR_MENTION_PATTERNS)


def _with_image_blocks(text: str, image_blocks=None):
    blocks = list(image_blocks or [])
    if not blocks:
        return text
    return [{"type": "text", "text": text}, *blocks]


_LIGHTWEIGHT_ASK_ELIXIR_PATTERNS = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"\bthanks?\b",
        r"\bthank you\b",
        r"\bnice\b",
        r"\bawesome\b",
        r"\bgreat\b",
        r"\bsmart(?:er)?\b",
        r"\bbetter\b",
        r"\bhelpful\b",
        r"\blove that\b",
        r"\byou sure are\b",
    )
)


def _is_lightweight_ask_elixir_turn(channel_name: str, question: str) -> bool:
    if (channel_name or "").strip().lower() != "#ask-elixir":
        return False
    text = (question or "").strip().lower()
    if not text:
        return False
    words = re.findall(r"[a-z0-9']+", text)
    if len(words) > 8:
        return False
    return any(pat.search(text) for pat in _LIGHTWEIGHT_ASK_ELIXIR_PATTERNS)


def _roster_bio_context(clan_data, roster_data=None):
    members = (
        roster_data.get("members", [])
        if roster_data
        else clan_data.get("memberList", clan_data.get("members", []))
    )
    if not members:
        return ""

    lines = ["=== MEMBER PROFILE SNAPSHOT ==="]
    for member in members:
        tag = member.get("tag", "")
        canon_tag = tag if str(tag).startswith("#") else f"#{tag}"
        try:
            overview = db.get_member_overview(canon_tag)
        except sqlite3.Error:
            overview = None
        if not overview:
            continue

        line = [
            f"- {overview.get('member_name') or member.get('name') or canon_tag}",
            f"tag: {overview.get('player_tag') or canon_tag}",
            f"role: {overview.get('role') or member.get('role') or 'member'}",
            f"trophies: {overview.get('trophies') or 0:,}",
        ]
        if overview.get("best_trophies"):
            line.append(f"best_trophies: {overview.get('best_trophies'):,}")
        if overview.get("joined_date"):
            line.append(f"joined_date: {overview.get('joined_date')}")
        if overview.get("donations_week") is not None:
            line.append(f"weekly_donations: {overview.get('donations_week')}")
        if overview.get("career_wins") is not None:
            line.append(
                f"career_record: {overview.get('career_wins') or 0} wins / {overview.get('career_losses') or 0} losses"
            )
        if overview.get("three_crown_wins") is not None:
            line.append(f"three_crown_wins: {overview.get('three_crown_wins') or 0}")
        if overview.get("war_day_wins") is not None:
            line.append(f"war_day_wins: {overview.get('war_day_wins') or 0}")
        if overview.get("current_favourite_card_name"):
            line.append(f"favorite_card: {overview.get('current_favourite_card_name')}")

        recent_form = overview.get("recent_form") or {}
        if recent_form:
            line.append(f"recent_form: {recent_form.get('summary')}")

        signature_cards = (overview.get("signature_cards") or {}).get("cards") or []
        if signature_cards:
            top_cards = ", ".join(
                card.get("name", "") for card in signature_cards[:3] if card.get("name")
            )
            if top_cards:
                line.append(f"signature_cards: {top_cards}")

        current_deck = (overview.get("current_deck") or {}).get("cards") or []
        if current_deck:
            deck_cards = ", ".join(
                card.get("name", "") for card in current_deck[:4] if card.get("name")
            )
            if deck_cards:
                line.append(f"current_deck_sample: {deck_cards}")

        war_status = overview.get("war_status") or {}
        war_season = war_status.get("season") or {}
        if war_season:
            line.append(
                "current_season_war: "
                f"{war_season.get('races_played') or 0} races, "
                f"{war_season.get('total_points') or 0:,} points, "
                f"{war_season.get('total_decks_used') or 0} decks"
            )

        if overview.get("bio"):
            line.append(f"existing_bio: {overview.get('bio')}")
        if overview.get("profile_highlight"):
            line.append(f"existing_highlight: {overview.get('profile_highlight')}")
        if member.get("note"):
            line.append(f"manual_note: {member.get('note')}")
        lines.append(" | ".join(line))

    return "\n".join(lines)


def _promotion_context(clan_data, war_data, roster_data=None):
    members = clan_data.get("memberList", clan_data.get("members", [])) or []
    lines = []

    clan_name = clan_data.get("name", "POAP KINGS")
    clan_tag = clan_data.get("tag", "#J2RGCRVG")
    member_count = clan_data.get("members", len(members))
    required_trophies = clan_data.get("requiredTrophies", 0)
    donations_per_week = clan_data.get("donationsPerWeek", 0)
    clan_score = clan_data.get("clanScore", 0)
    clan_war_trophies = clan_data.get("clanWarTrophies", 0)
    war_league = ((clan_data.get("warLeague") or {}).get("name")) or "Unknown"
    total_trophies = sum((member.get("trophies") or 0) for member in members)
    avg_level = (
        round(
            sum((member.get("expLevel") or 0) for member in members) / len(members),
            1,
        )
        if members
        else 0
    )

    lines.append("=== CLAN SNAPSHOT ===")
    lines.append(f"name: {clan_name}")
    lines.append(f"tag: {clan_tag}")
    lines.append(f"members: {member_count}")
    lines.append(f"required_trophies: {required_trophies}")
    lines.append(f"combined_trophies: {total_trophies}")
    lines.append(f"avg_member_level: {avg_level}")
    lines.append(f"weekly_donations: {donations_per_week}")
    lines.append(f"clan_score: {clan_score}")
    lines.append(f"clan_war_trophies: {clan_war_trophies}")
    lines.append(f"war_league: {war_league}")

    trophy_leaders = sorted(
        members,
        key=lambda member: (
            member.get("trophies") or 0,
            -(member.get("clanRank") or 999),
        ),
        reverse=True,
    )[:5]
    if trophy_leaders:
        lines.append("\n=== TROPHY LEADERS ===")
        for member in trophy_leaders:
            lines.append(
                f"- {member.get('name')} | {member.get('trophies', 0):,} trophies | "
                f"role: {member.get('role', 'member')}"
            )

    donation_leaders = [
        member
        for member in sorted(
            members,
            key=lambda member: (
                member.get("donations") or 0,
                member.get("trophies") or 0,
            ),
            reverse=True,
        )
        if (member.get("donations") or 0) > 0
    ][:5]
    if donation_leaders:
        lines.append("\n=== DONATION LEADERS ===")
        for member in donation_leaders:
            lines.append(
                f"- {member.get('name')} | {member.get('donations', 0)} donations | "
                f"{member.get('trophies', 0):,} trophies"
            )

    if roster_data:
        spotlight_candidates = sorted(
            roster_data.get("members", []),
            key=lambda member: (
                {"Leader": 3, "Co-Leader": 3, "Elder": 2, "Member": 1}.get(member.get("role"), 0),
                len(member.get("favorite_cards") or []),
                member.get("trophies") or 0,
                member.get("donations") or 0,
            ),
            reverse=True,
        )
        if spotlight_candidates:
            lines.append("\n=== MEMBER SPOTLIGHTS WITH SIGNATURE CARDS ===")
            for member in spotlight_candidates[:5]:
                favorite_cards = ", ".join(
                    card.get("name", "")
                    for card in (member.get("favorite_cards") or [])[:2]
                    if card.get("name")
                )
                bio = (member.get("bio") or "").strip()
                bio_preview = bio.split(". ")[0].strip() if bio else ""
                line = f"- {member.get('name')}"
                if favorite_cards:
                    line += f" | signature cards: {favorite_cards}"
                line += (
                    f" | role: {member.get('role')} | "
                    f"{member.get('trophies', 0):,} trophies | donations: {member.get('donations', 0)}"
                )
                if member.get("highlight"):
                    line += f" | highlight: {member.get('highlight')}"
                if bio_preview:
                    line += f" | bio: {bio_preview}"
                lines.append(line)

    try:
        season_summary = war_capability.get_war_season_view(view="summary", limit=5, source=db)[
            "data"
        ]
    except sqlite3.Error:
        season_summary = None
    if season_summary:
        lines.append("\n=== WAR SEASON LEADERS ===")
        for member in (season_summary.get("top_contributors") or [])[:5]:
            lines.append(
                f"- {member.get('member_name') or member.get('name')} | "
                f"{member.get('total_points', 0):,} points | races: {member.get('races_participated', 0)}"
            )

    return "\n".join(lines)


def generate_channel_update(
    channel_name,
    lane_key,
    context,
    *,
    recent_posts=None,
    memory_context=None,
    leadership=False,
):
    """Generate a proactive update for a specific Discord channel lane."""
    user_msg = context or ""
    user_msg += _format_recent_posts(recent_posts, channel_label=channel_name)
    user_msg += _format_memory_context(memory_context)
    workflow = "channel_update_leadership" if leadership else "channel_update"
    return _chat_with_tools(
        _channel_lane_system(channel_name, leadership=leadership),
        user_msg,
        workflow=workflow,
        allowed_tools=TOOLSETS_BY_WORKFLOW[workflow],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW[workflow],
        strict_json=True,
    )


def generate_clan_chat_copy(request: dict):
    """Generate copy intended for Clash Royale in-game clan chat only."""
    public_request = {k: v for k, v in (request or {}).items() if not str(k).startswith("_")}
    user_msg = (
        "Generate Clash Royale in-game clan chat copy for this request. "
        "Follow the system guardrails exactly and return only the JSON object.\n\n"
        f"```json\n{json.dumps(public_request, indent=2, default=str)}\n```"
    )
    return _chat_with_tools(
        _clan_chat_copy_system(),
        user_msg,
        workflow="clan_chat_copy",
        allowed_tools=TOOLSETS_BY_WORKFLOW["clan_chat_copy"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["clan_chat_copy"],
        strict_json=True,
    )


def run_memory_synthesis(context: dict):
    """Run one weekly memory-synthesis agent turn.

    ``context`` carries the week's memories, posts, live clan state, and
    prior synthesis arcs. The agent returns a structured plan:
    ``{"arc_memories": [...], "stale_memory_ids": [...], "contradictions": [...], "digest": "..."}``.

    The job function (``_memory_synthesis_cycle``) is responsible for
    persisting arc memories, marking stale entries expired, and posting the
    digest to #leaders. This agent call just produces the plan.
    """
    public_context = {k: v for k, v in (context or {}).items() if not k.startswith("_")}
    user_msg = (
        "Here is the week's memory context. Decide which arcs belong in the "
        "clan's long-term memory, which stored memories are stale, which "
        "stored memories contradict the live clan state, and write a short "
        "digest for #leaders. Follow the output schema in your system "
        "prompt exactly.\n\n"
        f"```json\n{json.dumps(public_context, indent=2, default=str)}\n```\n"
    )
    return _chat_with_tools(
        _memory_synthesis_system(),
        user_msg,
        workflow="memory_synthesis",
        allowed_tools=TOOLSETS_BY_WORKFLOW["memory_synthesis"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["memory_synthesis"],
        strict_json=True,
        return_errors=True,
        # Ceiling and effort: MODEL_CALL_POLICY["memory_synthesis"]. The retry
        # path below reduces the INPUT context, which cannot help when the
        # constraint is the OUTPUT ceiling; it stays as a backstop only.
    )


def synthesize_leader_action_feedback(context: dict):
    """Synthesize leader action reactions/notes into future leader-action guidance."""
    public_context = {k: v for k, v in (context or {}).items() if not k.startswith("_")}
    user_msg = (
        "Here is recent #leader-actions leader feedback. Synthesize what Elixir should learn "
        "for future cards of this action type. Focus on authoring choices, timing thresholds, "
        "decision standards, and follow-up expectations.\n\n"
        f"```json\n{json.dumps(public_context, indent=2, default=str)}\n```\n"
    )
    return _chat_with_tools(
        _leader_action_feedback_system(),
        user_msg,
        workflow="leader_action_feedback",
        allowed_tools=TOOLSETS_BY_WORKFLOW["leader_action_feedback"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["leader_action_feedback"],
        strict_json=True,
        return_errors=True,
    )


def run_reflection(context: dict):
    """One nightly reflection turn (Agentic Loop v2, Phase 4).

    ``context`` carries the last 24h of delivery intents, the wakes that chose
    silence and why, leadership reactions attributed to specific posts, and the
    lessons already in force. Returns
    ``{"lessons": [...], "notes": "..."}``.

    Persisting is the job function's business, not this call's — and it is where
    the caps live, because a model asked to be useful will always find three
    things to say.
    """
    public_context = {k: v for k, v in (context or {}).items() if not k.startswith("_")}
    user_msg = (
        "Here is the last 24 hours of your own output and the leadership "
        "reactions to it, plus member-authored conversations that may support "
        "dossiers. Use only exact refs from evidence_index in evidence_refs. "
        "An empty lessons list is a correct answer on a quiet "
        "day. Follow the output schema in your system prompt exactly.\n\n"
        f"```json\n{json.dumps(public_context, indent=2, default=str)}\n```\n"
    )
    return _chat_with_tools(
        _reflection_system(),
        user_msg,
        workflow="reflection",
        allowed_tools=TOOLSETS_BY_WORKFLOW["reflection"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["reflection"],
        strict_json=True,
        return_errors=True,
    )


def interpret_leader_note(context: dict):
    """Classify a leader's free-text note on an #actions card into one structured
    effect (timing_hold / invalidate_premise / persist_context / none). Cheap,
    tool-less, strict-JSON. Returns the parsed dict, or ``{"_error": ...}`` on
    failure (the caller then leaves the note as a plain, uninterpreted annotation)."""
    public_context = {k: v for k, v in (context or {}).items() if not k.startswith("_")}
    user_msg = (
        "Classify this leader note into one effect per your instructions.\n\n"
        f"```json\n{json.dumps(public_context, indent=2, default=str)}\n```\n"
    )
    return _chat_with_tools(
        _leader_note_interpret_system(),
        user_msg,
        workflow="leader_note_interpret",
        allowed_tools=TOOLSETS_BY_WORKFLOW["leader_note_interpret"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["leader_note_interpret"],
        strict_json=True,
        return_errors=True,
    )


def generate_ask_elixir_daily(
    read: dict, *, recent_topics: list | None = None, tool_stats: dict | None = None
):
    """Brain-composed #ask-elixir daily post: a feature-discovery invitation that
    rotates through Elixir's capabilities (decks, cards, stats, donations, awards,
    the Elder track, milestones, modes…) — NOT the same war hook every day. Given
    the clan read plus the recent topics to avoid, it spotlights a fresh capability
    area with a real hook and answerable sample questions.

    Returns ``{"post": str, "topic": str|None}`` on success, ``None`` when there
    is genuinely no worthwhile hook (fail-open-to-silence, no filler), or
    ``{"_error": {...}}`` when composition FAILED.

    Those last two were one `None` until 2026-08-06, and the caller marked both
    a job success. The result: this post stopped appearing in #ask-elixir on
    2026-07-26 and nobody heard about it for eleven days, while
    `daily_clan_insight` reported `success_count` rising and "no hook today —
    skipped" every morning. A composer that cannot say "I failed" makes its
    caller's health reporting a lie."""
    public = {k: v for k, v in (read or {}).items() if not k.startswith("_")}
    recent = ", ".join(str(t) for t in (recent_topics or []) if t) or "(none yet)"
    user_msg = (
        "Compose today's #ask-elixir feature-discovery post per your system "
        "prompt. Pick a capability AREA I have NOT covered recently, find one "
        "real, specific hook there (use tools to dig it up), then invite members "
        "with 2-3 answerable questions.\n\n"
        f"RECENT DAILY TOPICS (avoid these areas — pick something different): {recent}\n\n"
        "Here is the current clan situation (the war numbers sit at the top, but "
        "war is overused — reach past it into another area unless there's a "
        "genuinely fresh war angle not in the recent topics):\n\n"
        f"```json\n{json.dumps(public, indent=2, default=str)}\n```\n"
    )
    result = _chat_with_tools(
        _ask_elixir_daily_system(),
        user_msg,
        workflow="ask_elixir_daily",
        allowed_tools=TOOLSETS_BY_WORKFLOW["ask_elixir_daily"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["ask_elixir_daily"],
        strict_json=True,
        return_errors=True,
        tool_stats=tool_stats,
    )
    if not isinstance(result, dict):
        return {"_error": {"kind": "no_response", "detail": "composer returned no result"}}
    if "_error" in result:
        return result
    post = str(result.get("post") or "").strip()
    if not post:
        # The one legitimate silence: the model composed fine and had no hook.
        return None
    return {"post": post, "topic": result.get("topic")}


def run_awareness_tick(situation: dict, *, tool_stats: dict | None = None, on_event=None):
    """Run one awareness-loop turn. Receives the assembled Situation, returns
    a structured post plan: ``{"posts": [...], "skipped_reason": "..."}``.

    Phase 4 of the unified agentic awareness loop. Replaces N per-signal
    ``generate_channel_update`` calls with one agent turn that sees the full
    situation and decides what (if anything) to say where.

    ``tool_stats`` is optional; when provided, it is populated in-place with
    ``write_calls_issued``, ``write_calls_succeeded``, and ``write_calls_denied``
    so the caller can persist the complete call trace with the awareness
    thought.

    The turn runs with the brain's full read + write tool surface
    (``AWARENESS_TOOLS``) — writes are still bounded per tick by
    ``AWARENESS_WRITE_BUDGET_PER_TICK`` in the tool-call loop.
    """
    # Strip `_`-prefixed internal fields (e.g., _raw_signal_count, _clan_tag)
    # before serializing — the agent does not need runtime bookkeeping.
    public_situation = {k: v for k, v in (situation or {}).items() if not k.startswith("_")}
    base_user_msg = (
        "Here is the current Situation. Decide what, if anything, to post and "
        "where, following the lane rules in your system prompt. Silence is an "
        "allowed outcome. Hard-post-floor signals (in `hard_post_signals`) "
        "must be addressed.\n\n"
        # Compact JSON preserves the complete read while avoiding thousands of
        # formatting-only prompt characters on every awareness turn.
        f"```json\n{json.dumps(public_situation, separators=(',', ':'), default=str)}\n```\n"
    )
    allowed_tools = TOOLSETS_BY_WORKFLOW["awareness"]
    reference_codes = _extract_reference_codes(public_situation)

    def _tick(user_msg, max_tokens):
        return _chat_with_tools(
            _awareness_system(),
            user_msg,
            workflow="awareness",
            # The brain deliberates over the whole read (many signals across
            # lanes) and may emit several posts; 4096 truncates mid-response.
            # Give it the same headroom as clanops, more on the retry.
            max_tokens=max_tokens,
            allowed_tools=allowed_tools,
            response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["awareness"],
            strict_json=True,
            # Always surface WHY a tick produced nothing (truncation, schema
            # error, timeout, max tool rounds) as a {"_error": ...} payload so
            # we can retry a transient truncation here and the loop can classify
            # a persistent failure (see the return below).
            return_errors=True,
            tool_stats=tool_stats,
            on_event=on_event,
            reference_codes=reference_codes,
        )

    result = _tick(base_user_msg, 8192)

    # A truncation is over-generation, not a genuine ceiling: the same read
    # usually deliberates fine on a second pass (observed loop #9 truncated at
    # 8192, loop #10 on a near-identical read chose silence cleanly). Retry
    # once with real headroom and an explicit economy nudge before giving up —
    # a hard-post-floor signal shouldn't wait a whole tick on a stochastic
    # runaway. Raising the ceiling alone wouldn't help; a runaway just costs
    # more before hitting it. The retry re-reads the same tools if it needs to.
    # (The full prompt + response of every round is captured at the LLM layer —
    # agent.core records it onto the llm_calls row — so nothing extra is needed
    # here for Observatory visibility.)
    if _is_truncation_error(result):
        log.warning(
            "awareness tick truncated (max_tokens=8192, phase=%s); retrying once with headroom",
            result["_error"].get("phase"),
        )
        if on_event is not None:
            try:
                on_event({"type": "retry", "reason": "truncation", "max_tokens": 16384})
            except Exception:
                log.debug("on_event retry notice raised (ignored)", exc_info=True)
        retry_msg = base_user_msg + (
            "\n\nYour previous attempt ran past the length limit and was discarded. "
            "Be economical: combine redundant beats, cover a milestone backlog in a "
            "single post per channel rather than one post per signal, and keep each "
            "`content` tight. Emit only the posts that clear the bar."
        )
        result = _tick(retry_msg, 16384)

    # Return the result verbatim — including a {"_error": ...} payload on a
    # persistent failure — so the loop's classify_plan can mark the tick failed
    # (not deliberate silence) and surface the failure detail in the diagnostic.
    return result


def _is_truncation_error(result) -> bool:
    """True when a strict workflow returns a truncation failure payload."""
    return (
        isinstance(result, dict)
        and isinstance(result.get("_error"), dict)
        and result["_error"].get("kind") == "truncation"
    )


def repair_awareness_plan(situation: dict, plan: dict, violations: list[str]):
    """One no-tools repair for a plan rejected by deterministic copy policy.

    The repair model may change wording only. Channel choice, coverage keys,
    member identity fields, and relay decisions must survive unchanged. Factual
    values survive except rejected nonparticipation framing; delivery runs the
    deterministic validator again and fails closed if this attempt misses.
    """
    participation_repair = ""
    if any("negative_war_participation" in violation for violation in violations):
        participation_repair = (
            " For a negative_war_participation violation, remove the nag, deficit framing, "
            "nonparticipant count or roll call, and any command to play decks. You may remove "
            "names and numbers from the copy when they belong to that rejected framing, while "
            "preserving structured member identity fields. Keep positive facts already present "
            "about members who played or completed decks. Do not calculate, infer, or introduce "
            "a replacement participation count; if no positive fact remains, make the smallest "
            "grounded recognition statement the existing plan supports."
        )
    system = (
        "You repair a structured Discord post plan that failed deterministic copy policy. "
        "Return JSON only with the same awareness plan shape. Preserve every channel, "
        "covers_signal_keys value, leads_with value, member identity fields, and relay decision. "
        "Preserve every factual number unless a violation-specific instruction explicitly "
        "allows removing it. Change only the minimum wording needed to clear the violations. "
        "Refer to every member with they/them/their or repeat the member name; never guess "
        "gender. If the race is explicitly unranked, remove any claim about its current "
        "numeric rank without inventing a replacement. Colosseum has NO finish line and "
        "every battle across all four battle days continues to count toward clan and member "
        "standings; remove any contrary claim. Do not add posts, facts, or tools."
        + participation_repair
    )
    from capabilities.game_truth import get_game_truth

    live_war = {
        "period": situation.get("time") or {},
        "current_state": (
            ((situation.get("war_season") or {}).get("state") or {}).get("race") or {}
        ),
    }
    truth = {
        "race_ranked": (situation.get("war_season") or {}).get("race_ranked"),
        "canonical_game_truth": get_game_truth(topic="river_race", live_war=live_war),
        "violations": violations,
        "plan": plan,
    }
    return _chat_with_tools(
        system,
        "Repair this plan:\n" + json.dumps(truth, indent=2, default=str),
        workflow="awareness_repair",
        allowed_tools=[],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["awareness_repair"],
        strict_json=True,
        return_errors=True,
        # The repair must echo the FULL plan back as JSON (every post, verbatim
        # except the reworded copy). 4096 truncated a 2-post plan mid-response,
        # dropping a post → deliver.py rejected it as repair.changed_post_count
        # and failed the tick (#177, 2026-07-16). 8192 fits a multi-post plan.
    )


def respond_in_reception(question, author_name, clan_data, memory_context=None):
    """Onboarding Q&A in #welcome. No tools needed. Returns dict or None."""
    members = clan_data.get("memberList", clan_data.get("members", []))
    roster = (
        "\n".join(f"  {m.get('name', '?')} ({m.get('tag', '?')})" for m in members)
        or "  (roster unavailable)"
    )
    user_msg = f"New member '{author_name}' asks: {question}\n\n=== CLAN ROSTER ===\n{roster}"
    user_msg += _format_memory_context(memory_context)
    return _chat_with_tools(
        _reception_system(),
        user_msg,
        temperature=0.7,
        workflow="reception",
        allowed_tools=TOOLSETS_BY_WORKFLOW["reception"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["reception"],
        strict_json=True,
        return_errors=True,
    )


def _validate_war_deck_suggestion(result):
    """Validate war+suggest LLM response: 4 decks of 8 unique cards, 32 unique total.

    Returns None when valid, otherwise an error string describing the violation.
    """
    if not isinstance(result, dict):
        return "Response was not a JSON object."
    # A deterministic fallback is not a model answer to revise. Asking for a revision
    # would spend three more attempts re-deriving decks we already know the model
    # cannot fit inside the response limit, and would end up returning this same
    # object anyway — four wasted calls and four more minutes of member silence.
    if (result.get("metadata") or {}).get("fallback"):
        return None
    decks = result.get("proposed_decks")
    if not isinstance(decks, list) or len(decks) != 4:
        return "proposed_decks must be an array of exactly 4 decks."
    seen_total: dict[str, int] = {}
    for idx, deck in enumerate(decks, start=1):
        if not isinstance(deck, list) or len(deck) != 8:
            return f"Deck {idx} must contain exactly 8 cards (got {len(deck) if isinstance(deck, list) else type(deck).__name__})."
        normalized = []
        for slot in deck:
            if not isinstance(slot, str) or not slot.strip():
                return f"Deck {idx} contains a non-string or empty card slot."
            normalized.append(slot.strip())
        if len(set(normalized)) != 8:
            return f"Deck {idx} has duplicate cards within itself."
        for name in normalized:
            seen_total[name] = seen_total.get(name, 0) + 1
    duplicates = sorted([name for name, count in seen_total.items() if count > 1])
    if duplicates:
        return (
            f"These cards appear in more than one deck (no-overlap rule): {', '.join(duplicates)}."
        )
    return None


def respond_in_deck_review(
    question,
    author_name,
    channel_name,
    *,
    mode,
    subject,
    target_member_tag=None,
    target_member_name=None,
    conversation_history=None,
    memory_context=None,
    image_blocks=None,
):
    """Run the dedicated deck_review workflow.

    mode: 'regular' or 'war'
    subject: 'review' or 'suggest'

    For war+suggest, validates the proposed_decks structured field and asks the
    LLM to revise (up to 2 attempts) when the no-overlap or 32-unique constraint
    is violated.
    """
    target_line = ""
    if target_member_tag:
        target_line = (
            f"\nThe deck review target is member: {target_member_name or target_member_tag} "
            f"({target_member_tag}). Use this tag with the member tools.\n"
        )
    base_user_msg = (
        f"Latest deck-{subject} request from '{author_name}' in {channel_name} "
        f"(mode={mode}, subject={subject}): {question}{target_line}\n\n"
        "Follow the deck-review workflow guidance precisely. Ground every recommendation in tool calls. "
        "If screenshot images are attached, first identify the visible cards/UI from the image, say when any detail is unclear, "
        "and then use tools for player collection, recent losses, and card facts that cannot be trusted from vision alone."
    )

    # For war review/suggest, pre-fetch reconstruction so the LLM sees the
    # status without burning a tool round, and so we can short-circuit the
    # new-player case with a deterministic instruction.
    if mode == "war" and target_member_tag:
        try:
            war_decks = db.reconstruct_member_war_decks(target_member_tag)
        except Exception as exc:
            log.warning("war_decks pre-fetch failed for %s: %s", target_member_tag, exc)
            war_decks = None
        if isinstance(war_decks, dict):
            base_user_msg += (
                "\n\n=== PRE-FETCHED WAR DECK RECONSTRUCTION ===\n"
                f"{json.dumps(war_decks, indent=2)}\n"
                "(Treat this as the result of get_member_war_detail aspect='war_decks'. "
                "Do not call that tool again unless you need a refresh.)\n"
            )
            card_names = {
                card.get("name")
                for deck in war_decks.get("decks") or []
                if isinstance(deck, dict)
                for card in deck.get("cards") or []
                if isinstance(card, dict) and card.get("name")
            }
            if card_names:
                try:
                    catalog = db.lookup_cards(limit=max(len(card_names) * 2, 50))
                except Exception as exc:
                    log.warning(
                        "verified-costs pre-fetch failed for %s: %s",
                        target_member_tag,
                        exc,
                    )
                    catalog = []
                costs = {
                    c["name"]: c.get("elixir_cost")
                    for c in catalog
                    if isinstance(c, dict) and c.get("name") in card_names
                }
                if costs:
                    cost_lines = "\n".join(
                        f"  {name}: {cost if cost is not None else 'n/a (support card)'}"
                        for name, cost in sorted(costs.items())
                    )
                    base_user_msg += (
                        "\n\n=== VERIFIED CARD ELIXIR COSTS (from card catalog) ===\n"
                        f"{cost_lines}\n"
                        "These are the authoritative elixir costs for every card in the reconstructed decks. "
                        "Use ONLY these values when computing averages or totals. Do not use memory.\n"
                    )
            if war_decks.get("status") == "insufficient_data" and subject == "review":
                base_user_msg += (
                    "\nSPECIAL CASE — NEW WAR PLAYER:\n"
                    "This member has no reconstructable war decks. Your reply MUST:\n"
                    "1. Acknowledge warmly that they haven't played war battles yet.\n"
                    "2. Make an explicit offer to build four starter war decks from their card collection.\n"
                    "3. Tell them how to accept — use this exact callout phrase so the next message routes correctly: "
                    "**Reply `build my war decks` and I'll put together a starter kit.**\n"
                    "Do not call the war_decks tool again. Do not reconstruct anything.\n"
                )

    # For regular-mode review/suggest, pre-fetch the current deck and its card
    # catalog entries so the agent doesn't burn 8+ tool calls re-looking up
    # elixir costs it could have had for free.
    if mode == "regular" and target_member_tag and subject == "review":
        try:
            current_deck = (
                member_capability.get_member_intelligence(
                    target_member_tag,
                    facets=("loadout",),
                    source=db,
                ).get("loadout")
                or {}
            ).get("current_deck")
        except Exception as exc:
            log.warning("current_deck pre-fetch failed for %s: %s", target_member_tag, exc)
            current_deck = None
        if isinstance(current_deck, dict) and current_deck.get("cards"):
            deck_card_names = [
                card.get("name")
                for card in current_deck.get("cards") or []
                if isinstance(card, dict) and card.get("name")
            ]
            base_user_msg += (
                "\n\n=== PRE-FETCHED CURRENT DECK ===\n"
                f"{json.dumps(current_deck, indent=2, default=str)}\n"
                "(Treat this as the result of get_member include='deck'. Don't re-fetch "
                "the deck itself unless the user asks about a different time window.)\n"
            )
            if deck_card_names:
                try:
                    catalog = db.lookup_cards(limit=max(len(deck_card_names) * 2, 50))
                except Exception as exc:
                    log.warning(
                        "verified-costs pre-fetch failed for %s: %s",
                        target_member_tag,
                        exc,
                    )
                    catalog = []
                costs = {
                    c["name"]: c.get("elixir_cost")
                    for c in catalog
                    if isinstance(c, dict) and c.get("name") in set(deck_card_names)
                }
                if costs:
                    cost_lines = "\n".join(
                        f"  {name}: {cost if cost is not None else 'n/a (support card)'}"
                        for name, cost in sorted(costs.items())
                    )
                    base_user_msg += (
                        "\n\n=== VERIFIED CARD ELIXIR COSTS (from card catalog) ===\n"
                        f"{cost_lines}\n"
                        "These are the authoritative elixir costs for every card in the current deck. "
                        "Use ONLY these values for averages or totals. Only call lookup_cards for "
                        "cards that aren't in this list (e.g. when proposing a swap target).\n"
                    )

    # Pre-fetch the player's full card collection at light fidelity so the LLM
    # has authoritative levels for every owned card. Without this, on
    # 2026-04-24 Elixir hallucinated swap-target levels by copying the level
    # of the card being swapped out (Royal Ghost L12 → Fireball "L12" when
    # actual was L8). False data calls everything else into question, so we
    # inject the collection rather than rely on the LLM to fetch it.
    if target_member_tag:
        try:
            collection = db.get_member_card_collection(
                target_member_tag,
                include_support=True,
            )
        except Exception as exc:
            log.warning("collection pre-fetch failed for %s: %s", target_member_tag, exc)
            collection = None
        if isinstance(collection, dict):
            owned = list(collection.get("cards") or []) + list(
                collection.get("support_cards") or []
            )
            owned = [
                {
                    "name": c.get("name"),
                    "level": c.get("level"),
                    "rarity": c.get("rarity"),
                    "count": c.get("count"),
                }
                for c in owned
                if isinstance(c, dict) and c.get("name")
            ]
            if owned:
                owned.sort(key=lambda c: (c["name"] or "").lower())
                rows = "\n".join(
                    f"  {c['name']}: L{c['level']} {c['rarity']}"
                    + (f" ({c['count']} copies)" if c.get("count") is not None else "")
                    for c in owned
                )
                base_user_msg += (
                    "\n\n=== YOUR CARD COLLECTION (authoritative levels) ===\n"
                    f"{rows}\n"
                    f"Total: {len(owned)} cards owned.\n"
                    "These are the AUTHORITATIVE levels for every card the player owns. "
                    "When you mention any card in your response — especially as a swap target — "
                    "use ONLY the level shown above. Never invent or infer a card's level. "
                    "If a card you want to suggest does not appear in this list, the player does "
                    "not own it; pick a different candidate. Do NOT call get_member_cards "
                    "to re-confirm levels — this list is already the truth.\n"
                )

    base_user_msg += _format_memory_context(memory_context)
    system_prompt = _deck_review_system(channel_name, mode=mode, subject=subject)
    validate = mode == "war" and subject == "suggest"
    max_attempts = 3 if validate else 1
    history = list(conversation_history or [])
    user_msg = _with_image_blocks(base_user_msg, image_blocks)
    last_result = None

    def _run_deck_turn(turn_user_msg):
        def _run(message, max_tokens):
            return _chat_with_tools(
                system_prompt,
                message,
                conversation_history=history,
                workflow="deck_review",
                allowed_tools=TOOLSETS_BY_WORKFLOW["deck_review"],
                response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["deck_review"],
                strict_json=True,
                return_errors=True,
                max_tokens=max_tokens,
            )

        result = _run(turn_user_msg, 4096)
        if not _is_truncation_error(result):
            return result

        log.warning(
            "deck_review truncated (max_tokens=4096, phase=%s); retrying once with headroom",
            result["_error"].get("phase"),
        )
        retry_nudge = (
            "Your previous attempt reached the response limit and was discarded. "
            "Retry once with concise analysis and recommendations, but keep every required "
            "deck-review JSON field and finish the complete JSON object."
        )
        if isinstance(turn_user_msg, list):
            retry_user_msg = [*turn_user_msg, {"type": "text", "text": retry_nudge}]
        else:
            retry_user_msg = f"{turn_user_msg}\n\n{retry_nudge}"
        retried = _run(retry_user_msg, 8192)
        if not _is_truncation_error(retried):
            return retried
        # Both attempts hit the ceiling. Truncation used to return None all the way up,
        # so the member got NOTHING: on 2026-07-26 a war-deck request burned 24 model
        # calls over three and a half minutes, hit the limit six times, and never
        # produced a reply. He waited, saw silence, and did not ask again for five days.
        # A short honest answer beats that every time, and it keeps the conversation
        # open by naming the narrower question we can definitely answer.
        log.error("deck_review truncated twice (4096 then 8192); returning a spoken fallback")
        return {
            "event_type": "deck_review_response",
            "member_tags": [],
            "member_names": [],
            "summary": "Deck answer exceeded the response limit twice.",
            "content": (
                "That one got away from me — the answer ran past my response limit twice, "
                "so rather than leave you hanging with nothing: ask me for one deck at a "
                "time and I'll get it right. If you were after a full war set, say "
                "**war decks** and I'll do those four on their own."
            ),
            "metadata": {"fallback": "truncated_twice"},
        }

    for attempt in range(max_attempts):
        result = _run_deck_turn(user_msg)
        last_result = result
        if not validate:
            return result
        error = _validate_war_deck_suggestion(result)
        if error is None:
            return result
        log.warning(
            "deck_review war-suggest validation failed (attempt %d/%d): %s",
            attempt + 1,
            max_attempts,
            error,
        )
        if attempt + 1 >= max_attempts:
            break
        # Carry the prior turn forward and ask for a revision.
        history.append({"role": "user", "content": user_msg})
        prior_content = json.dumps(result) if isinstance(result, dict) else str(result)
        history.append({"role": "assistant", "content": prior_content})
        user_msg = (
            f"VALIDATION FAILED on your previous war-deck suggestion: {error}\n"
            "Revise the four decks so all 32 cards are unique and every card is owned by the player. "
            "Return the same JSON shape with the corrected proposed_decks."
        )
    return last_result


def respond_to_help_request(
    question,
    *,
    author_name,
    channel_name,
    role,
    memory_context=None,
    conversation_history=None,
):
    """Generate an in-character answer to a 'what can you do?' style question.

    The capability list comes from the intent registry so adding a new route
    keeps the help reply current automatically.
    """
    from runtime.intent_registry import help_routes_for_workflow

    routes = help_routes_for_workflow("clanops" if role == "clanops" else "interactive")
    capability_lines = []
    for r in routes:
        examples = r.get("examples") or []
        example = f' (e.g. "{examples[0]}")' if examples else ""
        capability_lines.append(f"- {r['label']}: {r['help_summary']}{example}")
    capabilities_block = "\n".join(capability_lines)

    user_msg = (
        f"'{author_name}' just asked in {channel_name}: {question}\n\n"
        "Here is the current list of capabilities available in this channel — pick the most "
        "relevant two or three and weave them into a short, natural reply in your own voice. "
        "Don't list every capability and don't write a manual; the goal is for them to feel "
        "invited to ask, not lectured.\n\n"
        f"=== CAPABILITIES ===\n{capabilities_block}"
    )
    user_msg += _format_memory_context(memory_context)

    system_prompt = _help_system(channel_name, role=role)
    messages = [
        {"role": "user", "content": user_msg},
    ]

    try:
        resp = _create_chat_completion(
            workflow="help",
            system=system_prompt,
            messages=messages,
            temperature=0.7,
        )
        text = (response_text(resp) or "").strip()
        if not text:
            return None
        return {"event_type": "help_response", "content": text, "summary": text[:200]}
    except (APIError, APIConnectionError) as exc:
        log.warning("respond_to_help_request_failed: %s", exc)
        return None


def _action_card_context(limit: int = 5) -> str:
    try:
        actions = db.list_leader_actions(status="proposed", limit=limit) or []
    except Exception as exc:
        log.warning("arena relay action context unavailable: %s", exc)
        return ""
    if not actions:
        return ""
    lines = ["=== RECENT OPEN ARENA-RELAY ACTIONS ==="]
    for action in actions[:limit]:
        prompt_text = " ".join((action.get("prompt_text") or "").split())
        if len(prompt_text) > 180:
            prompt_text = prompt_text[:177] + "..."
        lines.append(
            f"- id={action.get('action_id')} type={action.get('action_type')} "
            f"objective={action.get('objective')} proposed_at={action.get('proposed_at')} "
            f"prompt={prompt_text}"
        )
    return "\n".join(lines)


def analyze_leader_screenshot(
    question, *, author_name, channel_name, memory_context=None, image_blocks=None
):
    """Analyze leader-submitted Clash Royale screenshots posted to #leader-actions."""
    user_msg = (
        f"Leader '{author_name}' posted screenshot evidence in {channel_name}.\n\n"
        f"{question}\n\n"
        "Read the attached screenshot(s) as operational evidence. "
        "Return a crisp #leader-actions readout, not a conversational answer."
    )
    war_ctx = _war_status_prompt_context()
    if war_ctx:
        user_msg += f"\n\n{war_ctx}"
    action_ctx = _action_card_context()
    if action_ctx:
        user_msg += f"\n\n{action_ctx}"
    user_msg += _format_memory_context(memory_context)
    user_msg = _with_image_blocks(user_msg, image_blocks)
    return _chat_with_tools(
        _screenshot_readout_system(channel_name),
        user_msg,
        workflow="screenshot_readout",
        allowed_tools=TOOLSETS_BY_WORKFLOW["screenshot_readout"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["screenshot_readout"],
        strict_json=True,
        return_errors=True,
    )


def respond_in_channel(
    question,
    author_name,
    channel_name,
    workflow,
    clan_data,
    war_data,
    conversation_history=None,
    memory_context=None,
    image_blocks=None,
    author_identity=None,
):
    """Channel Q&A for interactive/clanops workflows.

    author_identity: the asker's linked clan member ({member_name, member_tag})
    when a Discord link exists — "my stats"-style questions resolve to the
    right player instead of a display-name guess (rehearsal 2026-07-04: every
    'my' question dead-ended in "who are you?" without it)."""
    if workflow not in {"interactive", "clanops"}:
        raise ValueError(f"unsupported channel workflow: {workflow}")
    lightweight_turn = workflow == "interactive" and _is_lightweight_ask_elixir_turn(
        channel_name, question
    )

    # Determine whether war context is relevant for this conversation
    channel_lower = (channel_name or "").strip().lower()
    war_relevant = (
        workflow == "clanops" or channel_lower == "#river-race" or _mentions_war(question)
    )

    context = (
        ""
        if lightweight_turn
        else _clan_context(
            clan_data,
            war_data,
            max_members=MAX_CONTEXT_MEMBERS_DEFAULT,
            include_war=war_relevant,
        )
    )
    trend_context = "" if lightweight_turn else _clan_trend_prompt_context()
    if lightweight_turn:
        user_msg = (
            f"Latest message from '{author_name}' in {channel_name}: {question}\n\n"
            "This is a lightweight conversational follow-up in Elixir's direct conversation lane. "
            "Reply to the latest message itself. Keep it short, natural, and present."
        )
    else:
        user_msg = (
            f"Latest user message to answer from '{author_name}' in {channel_name}: {question}\n\n"
            f"{context}"
        )
    if author_identity and (
        author_identity.get("member_name") or author_identity.get("player_tag")
    ):
        user_msg += (
            f"\n\n(Asker identity: '{author_name}' is linked to clan member "
            f"{author_identity.get('member_name')} {author_identity.get('player_tag') or ''} — "
            "questions about 'me'/'my' refer to that member; use the tag directly, "
            "do not ask who they are.)"
        )
    if trend_context:
        user_msg += f"\n\n{trend_context}"
    # Add war status context (with competing clan standings) when relevant
    war_snapshot = None
    if not lightweight_turn and war_relevant:
        try:
            war_snapshot = _war_status_snapshot()
        except Exception as exc:
            log.warning("War status admission snapshot unavailable: %s", exc)
            war_snapshot = None
        from agent.war_render import render_war_now

        war_ctx = (
            render_war_now(war_snapshot.get("clock"))
            if isinstance(war_snapshot, dict)
            and war_snapshot.get("available")
            and war_snapshot.get("clock")
            else ""
        )
        if war_ctx:
            user_msg += f"\n\n{war_ctx}"
    user_msg += _format_memory_context(memory_context)
    if image_blocks:
        user_msg += (
            "\n\n=== SCREENSHOT INPUT ===\n"
            "One or more Clash Royale screenshots are attached to this message. Inspect the visible UI directly. "
            "They may show decks, battle logs, leaderboards, store offers, profiles, collections, rewards, clan chat, or war screens. "
            "Answer the user's question from what is visible, say when text or details are unclear, and use tools only for "
            "structured clan/player context or card facts that would improve the answer."
        )
    user_msg = _with_image_blocks(user_msg, image_blocks)
    system_prompt = (
        _interactive_system(channel_name)
        if workflow == "interactive"
        else _clanops_system(channel_name)
    )
    result = _chat_with_tools(
        system_prompt,
        user_msg,
        conversation_history=conversation_history,
        workflow=workflow,
        max_tokens=8192 if workflow == "clanops" else 4096,
        allowed_tools=TOOLSETS_BY_WORKFLOW[workflow],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW[workflow],
        strict_json=True,
        return_errors=True,
    )
    if war_relevant and isinstance(war_snapshot, dict) and war_snapshot.get("available"):
        from agent.factual_admission import admit_structured_response
        from capabilities.game_truth import live_war_claim_facts

        result, trace = admit_structured_response(
            result,
            live_war_claim_facts(war_snapshot),
            repair_fn=_repair_channel_game_facts,
        )
        if trace.get("decision") != "pass":
            log.warning(
                "game factual admission %s for %s (%d finding(s))",
                trace.get("decision"),
                workflow,
                len(trace.get("findings") or []),
            )
    return result


# ── Event-driven message generation ──────────────────────────────────────────


def generate_message(event, context, recent_posts=None):
    """Generate a single Discord message for an event using the LLM.

    event: short description of what happened (e.g. "new_member_discord_join")
    context: string with all relevant details for the LLM
    recent_posts: optional list of recent post dicts to avoid repetition.

    Returns message text, or None on failure.
    """
    user_msg = f"Event: {event}\n\n{context}"
    user_msg += _format_recent_posts(recent_posts)
    return _generate_simple_message(
        _event_system(),
        user_msg,
        # Event type belongs in the evidence, not in workflow identity. Every
        # one-line event blurb shares one registered policy.
        workflow="event_blurb",
        temperature=0.7,
        error_label=f"generate_message({event})",
    )


# ── Site content generation for poapkings.com ────────────────────────────────


def _generate_simple_message(
    system_prompt,
    user_msg,
    *,
    workflow,
    temperature=0.8,
    max_tokens=None,
    error_label="LLM",
):
    """Shared pattern: system+user message -> text or None.

    max_tokens defaults to the workflow's ceiling in MODEL_CALL_POLICY."""
    messages = [
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _create_chat_completion(
            workflow=workflow,
            system=system_prompt,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = (response_text(resp) or "").strip()
        if not text or text.lower() == "null":
            return None
        return text
    except (APIError, APIConnectionError) as e:
        log.exception("%s API error: %s", error_label, e)
        return None


def generate_promote_content(clan_data, war_data=None, roster_data=None):
    """Generate promotional messages for 5 channels. Returns dict or None."""
    context = _clan_context(
        clan_data,
        war_data or {},
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    promotion_context = _promotion_context(clan_data, war_data or {}, roster_data=roster_data)
    required_trophies = clan_data.get("requiredTrophies", 2000)
    user_msg = (
        f"{context}\n\n"
        f"{promotion_context}\n\n"
        "Generate promotional messages for all 5 channels. Use the promotion snapshot heavily."
    )

    messages = [
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _create_chat_completion(
            workflow="recruiting_copy",
            system=_promote_system(required_trophies=required_trophies),
            messages=messages,
            temperature=0.8,
        )
        return _parse_response(response_text(resp) or "null")
    except (APIError, APIConnectionError) as e:
        log.exception("Promote API error: %s", e)
        return None


def generate_weekly_recap(
    read: dict,
    week_context: str,
    previous_message: str = "",
    *,
    tool_stats: dict | None = None,
):
    """Brain-composed Weekly Clan Recap (rebuilt 2026-07-11). The awareness brain
    reads the whole clan (``read``, incl. what it already posted this week) plus
    an aggregated week fact-base (``week_context``) and writes the weekly
    retrospective in its own voice — grounded, synthesizing the week's arc rather
    than re-listing moments it already posted. Tools available for verification.

    Returns the recap body text, or None when composition fails / there's no week
    to recap (the caller then posts nothing)."""
    public = {k: v for k, v in (read or {}).items() if not k.startswith("_")}
    prev_text = (
        f"Last week's recap (for continuity/callback):\n{previous_message}"
        if previous_message
        else "(no previous recap on file)"
    )
    user_msg = (
        "Write this week's Weekly Clan Recap per your system prompt: the "
        "clan-level story of the week, the human beats (named members, real "
        "numbers), and a short forward look. Ground every claim — especially any "
        "rank or superlative — in the week facts, the read, or a tool result.\n\n"
        f"THE WEEK (aggregated facts):\n{week_context}\n\n"
        f"{prev_text}\n\n"
        "THE READ (live clan state + what I already posted this week):\n"
        f"```json\n{json.dumps(public, indent=2, default=str)}\n```\n"
    )
    result = _chat_with_tools(
        _weekly_recap_system(),
        user_msg,
        workflow="weekly_recap",
        allowed_tools=TOOLSETS_BY_WORKFLOW["weekly_recap"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["weekly_recap"],
        strict_json=True,
        return_errors=True,
        tool_stats=tool_stats,
    )
    if not isinstance(result, dict) or "_error" in result:
        return None
    recap = str(result.get("recap") or "").strip()
    return recap or None


def generate_weekly_recap_email(
    read: dict,
    week_context: str,
    discord_recap: str = "",
    *,
    tool_stats: dict | None = None,
):
    """The emailed Weekly Clan Report — its OWN composition, not a reformat.

    Decoupled from the Discord post (Jamie, 2026-08-03). The Discord recap is
    short, punchy and written under Discord's formatting constraints; email has
    headings, tables and room, and a reader who may not be in Discord at all.
    Reformatting one into the other gave the worst of both.

    The Discord post is passed in only so this can avoid repeating its phrasing —
    it is context, not source material. Facts come from the same week fact-base
    and the same read.

    Returns the email body as Markdown, or None when composition fails (the
    caller then falls back to the reformatted Discord post rather than sending
    nothing)."""
    public = {k: v for k, v in (read or {}).items() if not k.startswith("_")}
    posted = (
        "This morning's Discord post (do not repeat its phrasing; the email is "
        f"the fuller edition):\n{discord_recap}"
        if discord_recap
        else "(nothing posted to Discord this week)"
    )
    user_msg = (
        "Write this week's Weekly Clan Report as an EMAIL, per your system "
        "prompt. Use headings, tables and lists; be expansive and explain why "
        "things happened, not just what. Ground every claim in the week facts, "
        "the read, or a tool result.\n\n"
        f"THE WEEK (aggregated facts):\n{week_context}\n\n"
        f"{posted}\n\n"
        "THE READ (live clan state):\n"
        f"```json\n{json.dumps(public, indent=2, default=str)}\n```\n"
    )
    result = _chat_with_tools(
        _weekly_recap_email_system(),
        user_msg,
        workflow="weekly_recap_email",
        allowed_tools=TOOLSETS_BY_WORKFLOW["weekly_recap_email"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["weekly_recap_email"],
        strict_json=True,
        return_errors=True,
        tool_stats=tool_stats,
    )
    if not isinstance(result, dict) or "_error" in result:
        return None
    email = str(result.get("email") or "").strip()
    return email or None


_MEMBER_REPORT_BLOCKS = (
    "overview",
    "standouts",
    "progress",
    "meta",
    "focus_review",
    "next_focus",
    "closer",
)


def generate_elder_standing(facts: str) -> str:
    """Warm the weekly Elder Standing report from a facts-only brief (built by
    runtime.elder_standing.facts_for_model). Returns the post text, or "" so the
    caller falls back to the deterministic render. No tools; grounded strictly in
    the brief (the caller also validates that every named member is real)."""
    user_msg = (
        f"{facts}\n\nWrite this week's Elder Standing report now — the "
        "sections in order, grounded ONLY in the members and numbers above. "
        "No tables. Name only members listed above."
    )
    return (
        _generate_simple_message(
            _elder_standing_system(),
            user_msg,
            workflow="elder_standing",
            temperature=0.7,
            error_label="Elder standing",
        )
        or ""
    )


def generate_outreach_ask(facts: str) -> str:
    """Compose the warm profile-outreach DM in Elixir's voice from a facts brief
    (the member's name + any known details). Returns the message text, or "" so the
    caller falls back to the deterministic template. No tools; a leader approves the
    draft before it sends."""
    user_msg = (
        f"{facts}\n\nWrite the direct message now — a few warm, natural sentences in "
        "your own voice, grounded only in the facts above. Return only the message text."
    )
    return (
        _generate_simple_message(
            _member_outreach_ask_system(),
            user_msg,
            workflow="member_outreach_ask",
            temperature=0.8,
            error_label="Member outreach ask",
        )
        or ""
    )


def generate_member_report(facts: str) -> dict:
    """Generate the narrative blocks for one member's weekly report from a
    facts-only brief (built by runtime.member_report.facts_for_model). Returns a
    dict of the named blocks plus a ``battle_intros`` map (one per battle type).
    ``next_focus`` is the shared dossier intention and is required by the sending
    job before any email leaves; other missing blocks come back as "" (or an empty
    map) so the renderer can fall back deterministically."""
    user_msg = (
        f"{facts}\n\nWrite this member's personalized weekly report now — the "
        "tagged blocks plus one <battle_intro> per battle type, grounded only in "
        "the facts above."
    )
    text = (
        _generate_simple_message(
            _member_report_system(),
            user_msg,
            workflow="member_report",
            temperature=0.85,
            error_label="Member report",
        )
        or ""
    )
    return _parse_member_report(text)


def _parse_member_report(text: str) -> dict:
    def block(name: str) -> str:
        m = re.search(rf"<{name}>(.*?)</{name}>", text, re.S | re.I)
        return m.group(1).strip() if m else ""

    out = {name: block(name) for name in _MEMBER_REPORT_BLOCKS}
    # Per-type battle intros: repeated <battle_intro type="ladder">…</battle_intro>.
    intros: dict[str, str] = {}
    for m in re.finditer(
        r'<battle_intro\s+type="([^"]+)"\s*>(.*?)</battle_intro>', text, re.S | re.I
    ):
        intros[m.group(1).strip()] = m.group(2).strip()
    out["battle_intros"] = intros
    return out


def generate_tournament_update(signals, *, recent_posts=None, memory_context=None):
    """Generate a live tournament post for a batch of tournament_* signals.

    Runs with a dedicated tournament prompt and a self-contained context —
    just the signals themselves and recent posts in #clan-events, no war
    state and no river-race context. The clan-events lane is still the
    destination, but the prompt and inputs are tournament-only.

    Returns the parsed response dict (with ``content`` as a string) or None.
    """
    user_msg = (
        "Write a tournament post for #clan-events. Base the post on the "
        "signals below — their payload is the only ground truth. Do not "
        "reference war/river-race state, Battle Day N, or any clock outside "
        "the tournament's own start/end times.\n\n"
        "Signals:\n"
        f"```json\n{json.dumps(signals or [], indent=2, default=str)}\n```\n"
    )
    user_msg += _format_recent_posts(recent_posts, channel_label="#clan-events")
    user_msg += _format_memory_context(memory_context)
    return _chat_with_tools(
        _tournament_update_system(),
        user_msg,
        workflow="tournament_update",
        allowed_tools=TOOLSETS_BY_WORKFLOW["tournament_update"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["tournament_update"],
        strict_json=True,
    )


def generate_tournament_recap(recap_context):
    """Generate a narrative tournament recap for Discord. Returns text or None.

    Uses the agent-loop pattern so the LLM can enrich the recap with cr_api
    lookups when needed (player profiles, recent battles). Recap context is
    pre-materialized by db.build_tournament_recap_context, so most recaps will
    not need tool calls at all.
    """
    user_msg = f"{recap_context}\n\nWrite this tournament's recap."
    parsed = _chat_with_tools(
        _tournament_recap_system(),
        user_msg,
        workflow="tournament_recap",
        allowed_tools=TOOLSETS_BY_WORKFLOW["tournament_recap"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["tournament_recap"],
        strict_json=True,
        temperature=0.8,
    )
    if not parsed:
        return None
    content = parsed.get("content")
    if not isinstance(content, str):
        return None
    text = content.strip()
    return text or None


def generate_war_intel_narrative(facts: str) -> dict:
    """Write the Clan Wars Intel Report prose from an assembled facts brief.

    No tools by design: runtime.war_intel already resolved every clan, player and
    number, and the renderer prints them. The model contributes the assessment,
    a threat rating and one paragraph per opponent. Returns {} when the model
    fails or returns unparseable JSON — the renderer falls back to deterministic
    copy rather than dropping the report, because the tables are the substance.
    """
    user_msg = (
        f"{facts}\n\nWrite the Clan Wars Intel Report now: the overall assessment, "
        "then one entry per opponent above with a threat rating and paragraph. "
        "JSON only."
    )
    text = (
        _generate_simple_message(
            _war_intel_system(),
            user_msg,
            workflow="war_intel",
            temperature=0.8,
            error_label="War intel",
        )
        or ""
    )
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S)
    try:
        parsed = json.loads(text)
    except ValueError, TypeError:
        log.warning("war intel: model returned unparseable JSON (%d chars)", len(text))
        return {}
    return parsed if isinstance(parsed, dict) else {}


def generate_intel_report(our_tag, competitor_tags, *, season_id=None, memory_context=None):
    """Run the Clan Wars Intel Report workflow.

    The LLM fetches intel on each competitor via cr_api,
    then composes a Discord-ready multi-message post. Returns the parsed
    response dict (with `content` as an array of message strings) or None.
    """
    season_line = (
        f"Season {season_id} is beginning."
        if season_id is not None
        else "A new river race has started."
    )
    opponents_line = (
        ", ".join(f"#{t.lstrip('#').upper()}" for t in competitor_tags) or "(none listed)"
    )
    user_msg = (
        f"{season_line}\n"
        f"Our clan: #{our_tag.lstrip('#').upper()}\n"
        f"Current river race opponents: {opponents_line}\n\n"
        "Use cr_api aspect='clan' and aspect='clan_war' on each opponent, "
        "then compose the Clan Wars Intel Report for #river-race."
    )
    our_state_lines = []
    try:
        snapshot = war_capability.get_war_season_view(view="snapshot", source=db)["data"]
        if snapshot:
            state = snapshot.get("state") or {}
            health = state.get("participation_health") or {}
            our_state_lines.append(f"summary: {snapshot.get('summary')}")
            if health:
                our_state_lines.append(
                    f"participation today: engaged {health.get('engaged_count', 0)}/"
                    f"{health.get('total_participants', 0)}, untouched {health.get('untouched_count', 0)}"
                )
            risks = state.get("active_risks") or {}
            if risks.get("no_participation_count"):
                our_state_lines.append(
                    f"no-participation this season: {risks['no_participation_count']} member(s)"
                )
            comparison = state.get("prior_cycle_comparison") or {}
            if comparison.get("direction") and comparison.get("direction") != "unknown":
                our_state_lines.append(
                    f"points-per-member vs prior season: {comparison.get('direction')}"
                )
    except Exception:
        log.warning("intel report: our-side war season context failed", exc_info=True)
    if our_state_lines:
        user_msg += (
            "\n\n=== OUR CLAN STATE (background only, for framing our side; the opponent "
            "live opponent intel you fetch via cr_api is the post's focus and ground truth) ===\n"
            + "\n".join(our_state_lines)
            + "\n"
        )
    user_msg += _format_memory_context(memory_context)
    return _chat_with_tools(
        _intel_report_system(),
        user_msg,
        workflow="intel_report",
        allowed_tools=TOOLSETS_BY_WORKFLOW["intel_report"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["intel_report"],
        strict_json=True,
    )


__all__ = [
    "generate_channel_update",
    "generate_clan_chat_copy",
    "generate_intel_report",
    "generate_war_intel_narrative",
    "run_memory_synthesis",
    "run_reflection",
    "run_awareness_tick",
    "repair_awareness_plan",
    "generate_ask_elixir_daily",
    "respond_in_reception",
    "respond_in_channel",
    "respond_in_deck_review",
    "respond_to_help_request",
    "analyze_leader_screenshot",
    "generate_message",
    "generate_promote_content",
    "generate_tournament_recap",
    "generate_tournament_update",
    "generate_weekly_recap",
]
