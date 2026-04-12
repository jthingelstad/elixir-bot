"""Evaluate deck analysis features via multi-turn conversations.

Picks real clan members stratified by war participation (regular/occasional/
rare/never). For each, the LLM generates a 3-turn conversation script tuned
to the member's profile (war players get war-deck scripts, non-war players
get regular-deck scripts). Each turn is run through the real deck pipeline
(classify_intent → respond_in_deck_review or _build_member_deck_report),
feeding conversation_history forward between turns. Tool calls are captured
by wrapping _execute_tool.

Run with:  python scripts/eval_deck_conversations.py [--members N]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db
import elixir_agent
from agent import tool_exec
from agent.core import _create_chat_completion, _chat_model_name
from agent.intent_router import classify_intent
from runtime.helpers._members import (
    _build_member_deck_report,
    _build_member_war_decks_report,
)
from storage.war_analytics import war_player_types_by_tag


# ── Member selection ──────────────────────────────────────────────────────


def pick_members(target_count: int) -> list[dict]:
    """Pick a stratified sample of active members across war_player_type."""
    members = db.list_members("active")
    if not members:
        return []
    tags = [m["player_tag"] for m in members]
    conn = db.get_connection()
    try:
        types_by_tag = war_player_types_by_tag(conn, tags)
    finally:
        conn.close()

    by_type: dict[str, list[dict]] = defaultdict(list)
    for m in members:
        war_type = types_by_tag.get(m["player_tag"], "never")
        m["war_player_type"] = war_type
        # Note whether they have a current deck snapshot so test scripts can pick
        # between "review existing" and "build from scratch" paths.
        deck = db.get_member_current_deck(m["player_tag"])
        m["has_current_deck"] = bool(deck and deck.get("cards"))
        by_type[war_type].append(m)

    # Stratified pick: at least one from each present category.
    picks: list[dict] = []
    remaining = target_count
    for war_type in ("regular", "occasional", "rare", "never"):
        bucket = by_type.get(war_type) or []
        if not bucket:
            continue
        picked = random.choice(bucket)
        picks.append(picked)
        remaining -= 1
        if remaining <= 0:
            return picks

    # Fill the rest randomly from any bucket we haven't drained.
    pool = [m for m in members if m not in picks]
    random.shuffle(pool)
    picks.extend(pool[:remaining])
    return picks


# ── Script generation ─────────────────────────────────────────────────────


def generate_conversation_script(member: dict) -> list[str]:
    """Ask the LLM to produce a 3-turn deck conversation for this member profile."""
    war_type = member["war_player_type"]
    has_deck = member["has_current_deck"]
    name = member.get("current_name") or member["player_tag"]
    tag = member["player_tag"]

    profile_hint = {
        "regular": "plays clan war regularly — has 4 war decks in the battle log and cares about tuning them",
        "occasional": "plays clan war sometimes — knows war decks but isn't obsessive",
        "rare": "rarely plays clan war — mostly ladder/trophy road",
        "never": "doesn't play clan war — only plays ladder and events",
    }[war_type]

    deck_status = (
        "has a current ladder deck stored"
        if has_deck else "has no current deck data (new or lapsed player)"
    )

    prompt = (
        f"You are generating a realistic 3-turn Discord conversation that a clan "
        f"member might have with the Elixir bot about their decks.\n\n"
        f"**Member profile:**\n"
        f"- Name: {name} ({tag})\n"
        f"- War participation: {war_type} — {profile_hint}\n"
        f"- Deck data: {deck_status}\n\n"
        f"**Instructions:**\n"
        f"- Write 3 sequential messages the member would send, varying in phrasing.\n"
        f"- They address the bot directly (so each message would trigger the deck workflow).\n"
        f"- The 3 turns should build on each other — e.g., show deck → review → tweak; "
        f"or build decks → adjust one → change cost profile.\n"
        f"- Mix war decks and regular decks depending on profile. A 'never' war player should NOT "
        f"ask for war decks. A 'regular' war player probably should.\n"
        f"- Don't use the member's own name in the message — they're speaking as themselves "
        f"(so 'my deck', 'my war decks', etc.).\n"
        f"- Keep each message conversational and concise (under 30 words).\n\n"
        "Return ONLY a JSON array of 3 strings, no wrapper, no commentary."
    )
    resp = _create_chat_completion(
        workflow="eval_deck_script_gen",
        messages=[
            {"role": "system", "content": "You produce realistic test data. Output strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        model=_chat_model_name(),
        temperature=1.0,
        max_tokens=1024,
    )
    text = (resp.choices[0].message.content or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"    !! script generation returned invalid JSON: {exc}")
        return []
    return [s for s in items if isinstance(s, str) and s.strip()]


# ── Tool-call capture ─────────────────────────────────────────────────────


_tool_calls_for_turn: list[tuple[str, dict]] = []
_original_execute_tool = tool_exec._execute_tool


def _capturing_execute_tool(name, arguments, *args, **kwargs):
    _tool_calls_for_turn.append((name, arguments))
    return _original_execute_tool(name, arguments, *args, **kwargs)


def install_tool_capture() -> None:
    # agent/chat.py does `from agent.tool_exec import _execute_tool`, which
    # rebinds the name in the chat module. We have to patch the name where it's
    # actually called from.
    from agent import chat as agent_chat
    tool_exec._execute_tool = _capturing_execute_tool
    agent_chat._execute_tool = _capturing_execute_tool
    if hasattr(elixir_agent, "_execute_tool"):
        elixir_agent._execute_tool = _capturing_execute_tool


def reset_tool_capture() -> list[tuple[str, dict]]:
    calls = list(_tool_calls_for_turn)
    _tool_calls_for_turn.clear()
    return calls


# ── Turn execution ────────────────────────────────────────────────────────


def run_turn(member: dict, question: str, conversation_history: list[dict]) -> dict:
    """Run a single conversation turn through the real deck pipeline."""
    reset_tool_capture()
    intent = classify_intent(
        question, workflow="interactive", mentioned=True, allows_open_channel_reply=False,
        conversation_history=conversation_history,
    )
    route = intent.get("route")
    mode = intent.get("mode")

    row: dict = {
        "question": question,
        "route": route,
        "mode": mode,
        "confidence": intent.get("confidence"),
        "rationale": intent.get("rationale"),
    }

    if route == "deck_display":
        try:
            if mode == "war":
                content = _build_member_war_decks_report(member["player_tag"])
                row["event_type"] = "deck_display_war"
            else:
                content = _build_member_deck_report(member["player_tag"])
                row["event_type"] = "deck_display"
            row["content"] = content
            row["content_len"] = len(content or "")
        except Exception as exc:
            row["error"] = f"deck_display raised: {exc}"
        row["tool_calls"] = []
        return row

    if route not in ("deck_review", "deck_suggest"):
        row["skipped"] = f"route={route} not a deck route"
        row["tool_calls"] = []
        return row

    subject = "review" if route == "deck_review" else "suggest"
    try:
        result = elixir_agent.respond_in_deck_review(
            question=question,
            author_name=member.get("current_name") or member["player_tag"],
            channel_name="#eval-decks",
            mode=mode or "regular",
            subject=subject,
            target_member_tag=member["player_tag"],
            target_member_name=member.get("current_name"),
            conversation_history=conversation_history,
            memory_context=None,
        )
    except Exception as exc:
        row["error"] = f"respond_in_deck_review raised: {exc}"
        row["tool_calls"] = reset_tool_capture()
        return row

    row["tool_calls"] = reset_tool_capture()

    if not isinstance(result, dict):
        row["error"] = f"result not dict: {type(result).__name__}"
        return row
    if result.get("_error"):
        row["error"] = f"agent _error: {result['_error']}"
        row["event_type"] = "agent_error"
        return row

    content = result.get("content") or ""
    # The agent may return content as a list of strings (multi-message split
    # for Discord's 2000-char limit). Normalise to a single string for eval.
    if isinstance(content, list):
        content = "\n\n".join(str(s) for s in content if s)
    row["content"] = content
    row["content_len"] = len(content)
    row["event_type"] = result.get("event_type")
    row["summary"] = result.get("summary")

    # War-specific validation: suggest mode should produce 4 decks.
    if subject == "suggest" and (mode or "regular") == "war":
        proposed = result.get("proposed_decks")
        row["proposed_decks_count"] = len(proposed) if isinstance(proposed, list) else 0

    return row


# ── Reporting ─────────────────────────────────────────────────────────────


def print_member_report(member: dict, turns: list[dict]) -> None:
    name = member.get("current_name") or member["player_tag"]
    print(f"\n── {name} ({member['player_tag']}) "
          f"[war={member['war_player_type']}, has_deck={member['has_current_deck']}] ──")
    for i, t in enumerate(turns, 1):
        flag = "!" if t.get("error") else ("·" if t.get("skipped") else " ")
        route_label = f"{t.get('route')}"
        if t.get("mode"):
            route_label += f"/{t['mode']}"
        tool_names = [c[0] for c in t.get("tool_calls") or []]
        tool_summary = f" tools={','.join(tool_names)}" if tool_names else ""
        clen = t.get("content_len")
        clen_str = f" len={clen}" if clen else ""
        print(f"  [{i}]{flag} route={route_label:20s}{tool_summary}{clen_str}")
        print(f"       Q: {t['question'][:100]}")
        if t.get("error"):
            print(f"       ERROR: {t['error']}")
        elif t.get("skipped"):
            print(f"       SKIPPED: {t['skipped']}")
        elif t.get("content"):
            preview = t["content"][:180].replace("\n", " ")
            print(f"       A: {preview}")
        if t.get("proposed_decks_count") is not None:
            marker = "OK" if t["proposed_decks_count"] == 4 else "MISMATCH"
            print(f"       war_decks: {t['proposed_decks_count']} [{marker}]")


def print_summary(all_turns: list[tuple[dict, dict]]) -> None:
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total = len(all_turns)
    errors = sum(1 for _, t in all_turns if t.get("error"))
    skipped = sum(1 for _, t in all_turns if t.get("skipped"))
    routes = Counter(t.get("route") for _, t in all_turns)
    tools = Counter()
    for _, t in all_turns:
        for name, _args in t.get("tool_calls") or []:
            tools[name] += 1

    print(f"Total turns: {total}")
    print(f"Errors: {errors}")
    print(f"Off-topic routes (skipped): {skipped}")
    print(f"\nRoute distribution:")
    for route, n in routes.most_common():
        print(f"  {route:22s} {n:>4}")
    print(f"\nTool call tally:")
    for name, n in tools.most_common():
        print(f"  {name:25s} {n:>4}")

    war_suggest_turns = [
        t for _, t in all_turns
        if t.get("route") == "deck_suggest" and t.get("mode") == "war"
        and t.get("proposed_decks_count") is not None
    ]
    if war_suggest_turns:
        mismatches = [t for t in war_suggest_turns if t["proposed_decks_count"] != 4]
        print(f"\nWar deck_suggest validation:")
        print(f"  {len(war_suggest_turns) - len(mismatches)}/{len(war_suggest_turns)} produced exactly 4 decks")
        for t in mismatches:
            print(f"  MISMATCH: got {t['proposed_decks_count']} decks for Q: {t['question'][:80]!r}")


# ── Main ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--members", type=int, default=6, help="Number of members to test")
    parser.add_argument("--turns", type=int, default=3, help="Turns per member")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for member selection")
    parser.add_argument("--out", default="scripts/deck_conversations_eval_results.json")
    args = parser.parse_args()

    if not os.getenv("CLAUDE_API_KEY"):
        print("ERROR: CLAUDE_API_KEY not set")
        sys.exit(1)

    if args.seed is not None:
        random.seed(args.seed)

    install_tool_capture()

    members = pick_members(args.members)
    if not members:
        print("No active members found in DB; nothing to evaluate.")
        sys.exit(1)

    print(f"Selected {len(members)} members:")
    for m in members:
        print(f"  {m.get('current_name') or m['player_tag']:30s} "
              f"({m['player_tag']})  war={m['war_player_type']:10s} "
              f"deck_data={'yes' if m['has_current_deck'] else 'no'}")

    all_rows = []
    all_turns: list[tuple[dict, dict]] = []

    for member in members:
        print(f"\n— Generating script for {member.get('current_name') or member['player_tag']} —")
        script = generate_conversation_script(member)
        if not script:
            print("    !! no script generated, skipping member")
            continue
        print(f"    {len(script)} turns:")
        for s in script:
            print(f"    • {s}")

        conversation_history: list[dict] = []
        turns: list[dict] = []
        for i, question in enumerate(script[: args.turns], 1):
            print(f"    running turn {i}/{min(len(script), args.turns)}…")
            turn = run_turn(member, question, conversation_history)
            turns.append(turn)
            all_turns.append((member, turn))

            # Build conversation history for the next turn. Use the shape the
            # deck workflow expects — a list of {role, content} dicts, newest
            # last (db.list_thread_messages returns ASC by time).
            conversation_history.append({"role": "user", "content": question})
            assistant_content = turn.get("content")
            if assistant_content:
                if isinstance(assistant_content, list):
                    assistant_content = "\n\n".join(str(s) for s in assistant_content if s)
                conversation_history.append({"role": "assistant", "content": assistant_content})

        print_member_report(member, turns)
        all_rows.append({
            "member": {
                "tag": member["player_tag"],
                "name": member.get("current_name"),
                "war_player_type": member["war_player_type"],
                "has_current_deck": member["has_current_deck"],
            },
            "turns": turns,
        })

    print_summary(all_turns)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(all_rows, indent=2, default=str))
    print(f"\nFull results → {out_path}")


if __name__ == "__main__":
    main()
