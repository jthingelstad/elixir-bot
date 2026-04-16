import json
import re
import sqlite3

import db
from anthropic import APIError, APIConnectionError

from agent import app as _app
from agent.core import (
    MAX_CONTEXT_MEMBERS_DEFAULT,
    MAX_CONTEXT_MEMBERS_FULL,
    _create_chat_completion,
    log,
)
from agent.chat import _clan_context, _format_memory_context, _format_recent_posts, _parse_json_response, _parse_response
from agent.prompts import (
    _awareness_system,
    _channel_subagent_system,
    _clanops_system,
    _deck_review_system,
    _event_system,
    _help_system,
    _home_message_system,
    _intel_report_system,
    _interactive_system,
    _members_message_system,
    _memory_synthesis_system,
    _observe_system,
    _quiz_explain_system,
    _promote_system,
    _reception_system,
    _roster_bios_system,
    _tournament_recap_system,
    _weekly_digest_system,
)
from agent.tool_policy import RESPONSE_SCHEMAS_BY_WORKFLOW, TOOLSETS_BY_WORKFLOW


def _chat_with_tools(*args, **kwargs):
    return _app._chat_with_tools(*args, **kwargs)


def _clan_trend_prompt_context(days=30, window_days=7):
    try:
        return db.build_clan_trend_summary_context(days=days, window_days=window_days) or ""
    except sqlite3.Error as exc:
        log.warning("Clan trend summary context unavailable: %s", exc)
        return ""


def _war_status_prompt_context():
    """Build a compact war status context string with competing clan standings."""
    try:
        status = db.get_current_war_status()
    except Exception as exc:
        log.warning("War status context unavailable: %s", exc)
        return ""
    if not status or status.get("state") in (None, "notInWar"):
        return ""
    lines = ["=== RIVER RACE STATUS ==="]
    phase_display = status.get("phase_display") or status.get("phase", "unknown")
    season_week = status.get("season_week_label") or ""
    if season_week:
        lines.append(f"{season_week} | {phase_display}")
    else:
        lines.append(phase_display)
    if status.get("colosseum_week"):
        lines.append("Colosseum week (100 trophy stakes)")
    if status.get("final_battle_day_active"):
        lines.append("FINAL BATTLE DAY")
    elif status.get("final_practice_day_active"):
        lines.append("Final practice day")
    standings = status.get("race_standings") or []
    if standings:
        lines.append("Race standings:")
        for clan in standings:
            marker = " (us)" if clan.get("is_us") else ""
            lines.append(
                f"  {clan['rank']}. {clan.get('clan_name', '?')}{marker} | "
                f"{clan.get('fame', 0):,} fame"
            )
    return "\n".join(lines)


_WAR_MENTION_PATTERNS = tuple(re.compile(pat, re.IGNORECASE) for pat in (
    r"\bwar\b",
    r"\briver race\b",
    r"\brace\b",
    r"\bfame\b",
    r"\bboat\b",
    r"\bdeck.{0,5}(usage|status|today)\b",
    r"\bbattle day\b",
    r"\bpractice day\b",
    r"\bcolosseum\b",
))


def _mentions_war(text):
    """Return True if the text references River Race / war concepts."""
    if not text:
        return False
    return any(pat.search(text) for pat in _WAR_MENTION_PATTERNS)


_LIGHTWEIGHT_ASK_ELIXIR_PATTERNS = tuple(re.compile(pat, re.IGNORECASE) for pat in (
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
))


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
    members = roster_data.get("members", []) if roster_data else clan_data.get("memberList", clan_data.get("members", []))
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

        signature_cards = ((overview.get("signature_cards") or {}).get("cards") or [])
        if signature_cards:
            top_cards = ", ".join(card.get("name", "") for card in signature_cards[:3] if card.get("name"))
            if top_cards:
                line.append(f"signature_cards: {top_cards}")

        current_deck = ((overview.get("current_deck") or {}).get("cards") or [])
        if current_deck:
            deck_cards = ", ".join(card.get("name", "") for card in current_deck[:4] if card.get("name"))
            if deck_cards:
                line.append(f"current_deck_sample: {deck_cards}")

        war_status = overview.get("war_status") or {}
        war_season = war_status.get("season") or {}
        if war_season:
            line.append(
                "current_season_war: "
                f"{war_season.get('races_played') or 0} races, "
                f"{war_season.get('total_fame') or 0:,} fame, "
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
    avg_level = round(
        sum((member.get("expLevel") or 0) for member in members) / len(members),
        1,
    ) if members else 0

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
        key=lambda member: (member.get("trophies") or 0, -(member.get("clanRank") or 999)),
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
        member for member in sorted(
            members,
            key=lambda member: (member.get("donations") or 0, member.get("trophies") or 0),
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
                favorite_cards = ", ".join(card.get("name", "") for card in (member.get("favorite_cards") or [])[:2] if card.get("name"))
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
        season_summary = db.get_war_season_summary(top_n=5)
    except sqlite3.Error:
        season_summary = None
    if season_summary:
        lines.append("\n=== WAR SEASON LEADERS ===")
        for member in (season_summary.get("top_contributors") or [])[:5]:
            lines.append(
                f"- {member.get('member_name') or member.get('name')} | "
                f"{member.get('total_fame', 0):,} fame | races: {member.get('races_participated', 0)}"
            )

    return "\n".join(lines)

def observe_and_post(clan_data, war_data, signals=None, recent_posts=None, memory_context=None):
    """Observation with signals from heartbeat. Returns dict or None.

    signals: list of signal dicts from heartbeat.tick(), or None when no detector output is being passed.
    recent_posts: list of recent message dicts from db.list_channel_messages().
    """
    from runtime.channel_subagents import is_war_signal

    # Only include war context when signals are war-related
    has_war_signals = signals and any(is_war_signal(s) for s in signals)
    context = _clan_context(clan_data, war_data, max_members=MAX_CONTEXT_MEMBERS_DEFAULT,
                            include_war=has_war_signals)

    if signals:
        signals_text = json.dumps(signals, indent=2, default=str)
        user_msg = (
            f"=== HEARTBEAT SIGNALS ===\n{signals_text}\n\n"
            f"{context}"
        )
    else:
        user_msg = context

    # Add war status context (with competing clan standings) for war signals
    if has_war_signals:
        war_ctx = _war_status_prompt_context()
        if war_ctx:
            user_msg += f"\n\n{war_ctx}"

    user_msg += _format_recent_posts(recent_posts)
    user_msg += _format_memory_context(memory_context)

    return _chat_with_tools(
        _observe_system(), user_msg,
        workflow="observation",
        allowed_tools=TOOLSETS_BY_WORKFLOW["observe"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["observation"],
        strict_json=True,
    )


def generate_channel_update(channel_name, subagent_key, context, *,
                            recent_posts=None, memory_context=None, leadership=False):
    """Generate a proactive update for a specific channel-named subagent."""
    user_msg = context or ""
    user_msg += _format_recent_posts(recent_posts, channel_label=channel_name)
    user_msg += _format_memory_context(memory_context)
    workflow = "channel_update_leadership" if leadership else "channel_update"
    return _chat_with_tools(
        _channel_subagent_system(channel_name, leadership=leadership),
        user_msg,
        workflow=workflow,
        allowed_tools=TOOLSETS_BY_WORKFLOW[workflow],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW[workflow],
        strict_json=True,
    )


def run_memory_synthesis(context: dict):
    """Run one weekly memory-synthesis agent turn.

    ``context`` carries the week's memories, posts, live clan state, and
    prior synthesis arcs. The agent returns a structured plan:
    ``{"arc_memories": [...], "stale_memory_ids": [...], "contradictions": [...], "digest": "..."}``.

    The job function (``_memory_synthesis_cycle``) is responsible for
    persisting arc memories, marking stale entries expired, and posting the
    digest to #leader-lounge. This agent call just produces the plan.
    """
    public_context = {k: v for k, v in (context or {}).items() if not k.startswith("_")}
    user_msg = (
        "Here is the week's memory context. Decide which arcs belong in the "
        "clan's long-term memory, which stored memories are stale, which "
        "stored memories contradict the live clan state, and write a short "
        "digest for #leader-lounge. Follow the output schema in your system "
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
    )


def run_awareness_tick(situation: dict, *, tool_stats: dict | None = None):
    """Run one awareness-loop turn. Receives the assembled Situation, returns
    a structured post plan: ``{"posts": [...], "skipped_reason": "..."}``.

    Phase 4 of the unified agentic awareness loop. Replaces N per-signal
    ``generate_channel_update`` calls with one agent turn that sees the full
    situation and decides what (if anything) to say where.

    ``tool_stats`` is optional; when provided, it is populated in-place with
    ``write_calls_issued``, ``write_calls_succeeded``, and ``write_calls_denied``
    so the caller can persist the awareness write budget usage in
    ``awareness_ticks``.
    """
    # Strip `_`-prefixed internal fields (e.g., _raw_signal_count, _clan_tag)
    # before serializing — the agent does not need runtime bookkeeping.
    public_situation = {k: v for k, v in (situation or {}).items() if not k.startswith("_")}
    user_msg = (
        "Here is the current Situation. Decide what, if anything, to post and "
        "where, following the lane rules in your system prompt. Silence is an "
        "allowed outcome. Hard-post-floor signals (in `hard_post_signals`) "
        "must be addressed.\n\n"
        f"```json\n{json.dumps(public_situation, indent=2, default=str)}\n```\n"
    )
    return _chat_with_tools(
        _awareness_system(),
        user_msg,
        workflow="awareness",
        allowed_tools=TOOLSETS_BY_WORKFLOW["awareness"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["awareness"],
        strict_json=True,
        tool_stats=tool_stats,
    )


def respond_in_reception(question, author_name, clan_data, memory_context=None):
    """Onboarding Q&A in #reception. No tools needed. Returns dict or None."""
    members = clan_data.get("memberList", clan_data.get("members", []))
    roster = "\n".join(
        f"  {m.get('name', '?')} ({m.get('tag', '?')})"
        for m in members
    ) or "  (roster unavailable)"
    user_msg = (
        f"New member '{author_name}' asks: {question}\n\n"
        f"=== CLAN ROSTER ===\n{roster}"
    )
    user_msg += _format_memory_context(memory_context)
    return _chat_with_tools(
        _reception_system(), user_msg,
        temperature=0.7, max_tokens=400,
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
        return f"These cards appear in more than one deck (no-overlap rule): {', '.join(duplicates)}."
    return None


def respond_in_deck_review(question, author_name, channel_name, *, mode, subject,
                           target_member_tag=None, target_member_name=None,
                           conversation_history=None, memory_context=None):
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
        "Follow the deck-review workflow guidance precisely. Ground every claim in tool calls."
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
                    log.warning("verified-costs pre-fetch failed for %s: %s", target_member_tag, exc)
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
            current_deck = db.get_member_current_deck(target_member_tag)
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
                    log.warning("verified-costs pre-fetch failed for %s: %s", target_member_tag, exc)
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

    base_user_msg += _format_memory_context(memory_context)
    system_prompt = _deck_review_system(channel_name, mode=mode, subject=subject)
    validate = mode == "war" and subject == "suggest"
    max_attempts = 3 if validate else 1
    history = list(conversation_history or [])
    user_msg = base_user_msg
    last_result = None
    for attempt in range(max_attempts):
        result = _chat_with_tools(
            system_prompt,
            user_msg,
            conversation_history=history,
            workflow="deck_review",
            allowed_tools=TOOLSETS_BY_WORKFLOW["deck_review"],
            response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["deck_review"],
            strict_json=True,
            return_errors=True,
        )
        last_result = result
        if not validate:
            return result
        error = _validate_war_deck_suggestion(result)
        if error is None:
            return result
        log.warning(
            "deck_review war-suggest validation failed (attempt %d/%d): %s",
            attempt + 1, max_attempts, error,
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


def respond_to_help_request(question, *, author_name, channel_name, role,
                             memory_context=None, conversation_history=None):
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
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    try:
        resp = _create_chat_completion(
            workflow="help",
            messages=messages,
            temperature=0.7,
            max_tokens=600,
            timeout=60,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return None
        return {"event_type": "help_response", "content": text, "summary": text[:200]}
    except (APIError, APIConnectionError) as exc:
        log.warning("respond_to_help_request_failed: %s", exc)
        return None


def respond_in_channel(question, author_name, channel_name, workflow, clan_data, war_data,
                       conversation_history=None, memory_context=None):
    """Channel Q&A for interactive/clanops workflows."""
    if workflow not in {"interactive", "clanops"}:
        raise ValueError(f"unsupported channel workflow: {workflow}")
    lightweight_turn = workflow == "interactive" and _is_lightweight_ask_elixir_turn(channel_name, question)

    # Determine whether war context is relevant for this conversation
    channel_lower = (channel_name or "").strip().lower()
    war_relevant = (
        workflow == "clanops"
        or channel_lower in ("#war-talk", "#river-race")
        or _mentions_war(question)
    )

    context = "" if lightweight_turn else _clan_context(
        clan_data, war_data, max_members=MAX_CONTEXT_MEMBERS_DEFAULT,
        include_war=war_relevant,
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
    if trend_context:
        user_msg += f"\n\n{trend_context}"
    # Add war status context (with competing clan standings) when relevant
    if not lightweight_turn and war_relevant:
        war_ctx = _war_status_prompt_context()
        if war_ctx:
            user_msg += f"\n\n{war_ctx}"
    user_msg += _format_memory_context(memory_context)
    system_prompt = (
        _interactive_system(channel_name)
        if workflow == "interactive"
        else _clanops_system(channel_name)
    )
    return _chat_with_tools(
        system_prompt,
        user_msg,
        conversation_history=conversation_history,
        workflow=workflow,
        allowed_tools=TOOLSETS_BY_WORKFLOW[workflow],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW[workflow],
        strict_json=True,
        return_errors=True,
    )


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
        _event_system(), user_msg,
        workflow=f"event:{event}", temperature=0.7, max_tokens=300,
        error_label=f"generate_message({event})",
    )


def explain_quiz_answer(*, question_text: str, correct_answer: str, context: str) -> str | None:
    """Write a 1-2 sentence tactical explanation for a quiz answer.

    The deterministic quiz scaffold has already picked the cards, computed
    the math, and flagged the correct option. This call only narrates *why
    the answer is correct* and what it means in play. Routes to the
    lightweight model via ``event:quiz_explain``. Returns text or None on
    failure; callers must have a templated fallback ready.
    """
    user_msg = (
        f"QUESTION: {question_text}\n"
        f"CORRECT ANSWER: {correct_answer}\n\n"
        f"CONTEXT:\n{context}\n\n"
        "Write the explanation."
    )
    messages = [
        {"role": "system", "content": _quiz_explain_system()},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _create_chat_completion(
            workflow="event:quiz_explain",
            messages=messages,
            temperature=0.7,
            max_tokens=200,
            timeout=30,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except (APIError, APIConnectionError) as exc:
        log.warning("explain_quiz_answer API error: %s", exc)
        return None
    if not raw:
        return None
    # The prompt asks for JSON with an "explanation" key. Haiku sometimes
    # wraps its answer in ```json ... ``` fences — use the shared parser
    # that already handles that case, falling through to raw text if all
    # else fails.
    try:
        parsed = _parse_json_response(raw)
        if isinstance(parsed, dict):
            text = (parsed.get("explanation") or "").strip()
            if text:
                return text
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return raw


# ── Site content generation for poapkings.com ────────────────────────────────

def _generate_simple_message(system_prompt, user_msg, *, workflow, temperature=0.8,
                              max_tokens=300, error_label="LLM"):
    """Shared pattern: system+user message -> text or None."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _create_chat_completion(
            workflow=workflow,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=60,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text or text.lower() == "null":
            return None
        return text
    except (APIError, APIConnectionError) as e:
        log.error("%s API error: %s", error_label, e)
        return None


def generate_home_message(clan_data, war_data, previous_message, roster_data=None):
    """Generate a message for the poapkings.com home page. Returns text or None."""
    context = _clan_context(
        clan_data, war_data,
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    prev_text = f"Your previous message: {previous_message}" if previous_message else "(none yet)"
    user_msg = f"{context}\n\n{prev_text}\n\nWrite your next message for the home page."
    return _generate_simple_message(
        _home_message_system(), user_msg,
        workflow="site_home_message", temperature=0.9, max_tokens=300,
        error_label="Home message",
    )


def generate_members_message(clan_data, war_data, previous_message, roster_data=None):
    """Generate a message for the poapkings.com members page. Returns text or None."""
    context = _clan_context(
        clan_data, war_data,
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    prev_text = f"Your previous message: {previous_message}" if previous_message else "(none yet)"
    user_msg = f"{context}\n\n{prev_text}\n\nWrite your next message for the members page."
    return _generate_simple_message(
        _members_message_system(), user_msg,
        workflow="site_members_message", temperature=0.9, max_tokens=400,
        error_label="Members message",
    )


def generate_roster_bios(clan_data, war_data, roster_data=None):
    """Generate roster intro and per-member bios. Returns dict or None."""
    context = _clan_context(
        clan_data, war_data,
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    member_profile_context = _roster_bio_context(clan_data, roster_data=roster_data)
    members = clan_data.get("memberList", clan_data.get("members", []))
    member_tags = [m.get("tag", "") for m in members]
    user_msg = (
        f"{context}\n\n"
        f"{member_profile_context}\n\n"
        f"Generate an intro and bio for each member.\n"
        f"Member tags to cover: {', '.join(member_tags)}"
    )
    return _chat_with_tools(
        _roster_bios_system(), user_msg,
        temperature=0.8, max_tokens=2000,
        workflow="roster_bios",
        allowed_tools=TOOLSETS_BY_WORKFLOW["roster_bios"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["roster_bios"],
        strict_json=True,
    )


def generate_promote_content(clan_data, war_data=None, roster_data=None):
    """Generate promotional messages for 5 channels. Returns dict or None."""
    context = _clan_context(
        clan_data, war_data or {},
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
        {"role": "system", "content": _promote_system(required_trophies=required_trophies)},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _create_chat_completion(
            workflow="site_promote_content",
            messages=messages,
            temperature=0.8,
            max_tokens=1500,
            timeout=60,
        )
        return _parse_response(resp.choices[0].message.content or "null")
    except (APIError, APIConnectionError) as e:
        log.error("Promote API error: %s", e)
        return None


def generate_weekly_digest(summary_context, previous_message=""):
    """Generate a long-form weekly clan recap for Discord. Returns text or None."""
    prev_text = f"Your previous weekly recap: {previous_message}" if previous_message else "(no previous recap provided)"
    user_msg = f"{summary_context}\n\n{prev_text}\n\nWrite this week's clan recap."
    return _generate_simple_message(
        _weekly_digest_system(), user_msg,
        workflow="weekly_digest", temperature=0.8, max_tokens=1200,
        error_label="Weekly digest",
    )


def generate_tournament_recap(recap_context):
    """Generate a narrative tournament recap for Discord. Returns text or None."""
    user_msg = f"{recap_context}\n\nWrite this tournament's recap."
    return _generate_simple_message(
        _tournament_recap_system(), user_msg,
        workflow="tournament_recap", temperature=0.8, max_tokens=1200,
        error_label="Tournament recap",
    )


def generate_intel_report(our_tag, competitor_tags, *, season_id=None, memory_context=None):
    """Run the Clan Wars Intel Report workflow.

    The LLM fetches intel on each competitor via cr_api + get_clan_intel_report,
    then composes a Discord-ready multi-message post. Returns the parsed
    response dict (with `content` as an array of message strings) or None.
    """
    season_line = f"Season {season_id} is beginning." if season_id is not None else "A new river race has started."
    opponents_line = ", ".join(f"#{t.lstrip('#').upper()}" for t in competitor_tags) or "(none listed)"
    user_msg = (
        f"{season_line}\n"
        f"Our clan: #{our_tag.lstrip('#').upper()}\n"
        f"Current river race opponents: {opponents_line}\n\n"
        "Use get_clan_intel_report on each opponent to gather threat analysis, "
        "then compose the Clan Wars Intel Report for #river-race."
    )
    user_msg += _format_memory_context(memory_context)
    return _chat_with_tools(
        _intel_report_system(),
        user_msg,
        workflow="intel_report",
        allowed_tools=TOOLSETS_BY_WORKFLOW["intel_report"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["intel_report"],
        strict_json=True,
        max_tokens=4096,
    )


__all__ = [
    "observe_and_post",
    "generate_channel_update",
    "generate_intel_report",
    "run_memory_synthesis",
    "run_awareness_tick",
    "respond_in_reception",
    "respond_in_channel",
    "respond_in_deck_review",
    "respond_to_help_request",
    "generate_message",
    "explain_quiz_answer",
    "generate_home_message",
    "generate_members_message",
    "generate_roster_bios",
    "generate_promote_content",
    "generate_tournament_recap",
    "generate_weekly_digest",
]
