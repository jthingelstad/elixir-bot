"""Evaluate the new card tools (get_member_card_profile, lookup_member_cards)
via multi-turn conversations through the interactive workflow.

This is the card-focused twin of eval_deck_conversations.py. It runs each
turn through `respond_in_channel(workflow="interactive")` — the same path
that handles #ask-elixir card questions in production, where the original
null-response failures from 2026-04-24 happened.

Question buckets target the specific paths we care about:

  1. **broad** — digest territory: "review my cards", "tell me about my
     collection". Should fire `get_member_card_profile`.
  2. **upgrade** — "what should I upgrade", "what's ready to level up".
     Should hit `lookup_member_cards(filter={ready_to_upgrade: true})` or
     surface ready-list from the digest.
  3. **rarity** — "what legendaries do I have", "show me my epics".
     Should hit `lookup_member_cards(filter={rarity: ...})`.
  4. **single_card** — "is my fireball maxed", "what level is my hog rider".
     Should hit `lookup_member_cards(filter={name: ...})`.
  5. **ambiguous** — bare "my cards", "tell me what I have". Should either
     fire the digest OR ask a clarifying question.
  6. **meta** — "what info do you have about my cards", "what details can
     you tell me about my cards" (the actual question that broke
     shimmeringhost on 2026-04-24).

Every turn runs on Haiku (the model production uses for the interactive
workflow). The eval reports tool-call patterns, null/empty responses,
clarification rate, and whether the deprecated include=['cards'] path
fires (which would be a regression).

Run with:  python scripts/eval_card_conversations.py [--members N] [--turns N]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db
import elixir_agent
from agent import tool_exec
from agent.core import _create_chat_completion, _lightweight_model_name
from agent.intent_router import classify_intent
from cr_api import CLAN_TAG


# ── Member selection ──────────────────────────────────────────────────────


def pick_members(target_count: int) -> list[dict]:
    """Pick a stratified sample of active members.

    Stratifies by ready-to-upgrade richness so the eval covers both
    upgrade-rich players (where ready_to_upgrade=true returns lots) and
    upgrade-poor players (where it returns few or none).
    """
    members = db.list_members("active")
    if not members:
        return []

    # Bucket by whether the member has any cards ready to upgrade right now.
    # Uses the new digest directly so we exercise it in the picker too.
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for m in members:
        try:
            profile = db.get_member_card_profile(m["player_tag"])
        except Exception:
            profile = None
        if not profile:
            bucket = "no_snapshot"
        else:
            ready_n = len(profile.get("ready_to_upgrade_top") or [])
            bucket = "many_ready" if ready_n >= 3 else "few_ready" if ready_n >= 1 else "none_ready"
        m["card_bucket"] = bucket
        m["king_tower"] = profile.get("king_tower_level") if profile else None
        by_bucket[bucket].append(m)

    picks: list[dict] = []
    remaining = target_count
    for bucket in ("many_ready", "few_ready", "none_ready", "no_snapshot"):
        pool = by_bucket.get(bucket) or []
        if not pool:
            continue
        picks.append(random.choice(pool))
        remaining -= 1
        if remaining <= 0:
            return picks

    leftover = [m for m in members if m not in picks]
    random.shuffle(leftover)
    picks.extend(leftover[:remaining])
    return picks


# ── Script generation ─────────────────────────────────────────────────────


_BUCKET_HINTS = {
    "broad": (
        "Broad/digest-style question about the player's whole card collection. "
        "Examples: 'review my cards', 'how am I doing on cards', "
        "'tell me about my collection', 'what do my cards look like'. "
        "Should NOT mention a specific card or rarity."
    ),
    "upgrade": (
        "Upgrade-focused question. Examples: 'what should I upgrade next', "
        "'any cards ready to level up', 'what's close to maxing'. "
        "Should signal upgrade intent without naming a specific card."
    ),
    "rarity": (
        "Question scoped to one rarity tier. Examples: 'what legendaries do I have', "
        "'show me my epics', 'how many champions am I missing'."
    ),
    "single_card": (
        "Question about exactly one card. Pick a real card name (Knight, Fireball, "
        "Hog Rider, P.E.K.K.A, Log, etc.). Examples: 'is my fireball maxed', "
        "'what level is my hog rider', 'how close am I to leveling up the log'."
    ),
    "ambiguous": (
        "Deliberately ambiguous question that could mean current deck OR full collection "
        "OR war decks. Examples: 'my cards', 'tell me what I have', 'cards', "
        "'show me what I've got'. Keep it short and underspecified."
    ),
    "meta": (
        "Meta question about what the bot can tell them about their cards. "
        "Examples: 'what info do you have about my cards', "
        "'what can you tell me about my cards', 'what details do you track for my cards'. "
        "This is the question shape that broke things on 2026-04-24."
    ),
}


def generate_card_script(member: dict) -> list[tuple[str, str]]:
    """Return a 6-turn script: one question from each bucket, in randomized order.

    Each entry is (bucket, question_text).
    """
    name = member.get("current_name") or member["player_tag"]
    tag = member["player_tag"]

    # Randomize order so successive runs don't always test buckets in the same
    # context; multi-turn coherence matters for follow-ups.
    buckets = list(_BUCKET_HINTS.keys())
    random.shuffle(buckets)

    prompt = (
        f"You are generating realistic Discord messages a clan member would send to "
        f"the Elixir bot about their cards.\n\n"
        f"**Member:** {name} ({tag}). King Tower level "
        f"{member.get('king_tower') or 'unknown'}. Card-bucket: {member.get('card_bucket', '?')}.\n\n"
        f"Generate one message for EACH of these buckets, in this exact order:\n\n"
        + "\n".join(f"{i+1}. **{b}** — {_BUCKET_HINTS[b]}" for i, b in enumerate(buckets))
        + "\n\n**Instructions:**\n"
        f"- Write the messages as if {name} is speaking. Don't reference themselves "
        f"by name (use 'my', 'I', etc.).\n"
        f"- Vary phrasing across casual/formal, short/long, with/without typos.\n"
        f"- Keep each message under 25 words.\n"
        f"- Do NOT prefix the message with the bucket name.\n\n"
        f"Return ONLY a JSON array of {len(buckets)} strings in the bucket order above. "
        f"No wrapper, no commentary."
    )
    resp = _create_chat_completion(
        workflow="eval_card_script_gen",
        messages=[
            {"role": "system", "content": "You produce realistic test data. Output strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        model=_lightweight_model_name(),
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
    pairs = [
        (bucket, msg.strip())
        for bucket, msg in zip(buckets, items)
        if isinstance(msg, str) and msg.strip()
    ]
    return pairs


# ── Tool-call capture ─────────────────────────────────────────────────────


_tool_calls_for_turn: list[tuple[str, dict]] = []
_original_execute_tool = tool_exec._execute_tool


def _capturing_execute_tool(name, arguments, *args, **kwargs):
    _tool_calls_for_turn.append((name, dict(arguments) if isinstance(arguments, dict) else arguments))
    return _original_execute_tool(name, arguments, *args, **kwargs)


def install_tool_capture() -> None:
    from agent import chat as agent_chat
    tool_exec._execute_tool = _capturing_execute_tool
    agent_chat._execute_tool = _capturing_execute_tool
    if hasattr(elixir_agent, "_execute_tool"):
        elixir_agent._execute_tool = _capturing_execute_tool


def reset_tool_capture() -> list[tuple[str, dict]]:
    calls = list(_tool_calls_for_turn)
    _tool_calls_for_turn.clear()
    return calls


# ── Pipeline execution ────────────────────────────────────────────────────


def _connect_db():
    return sqlite3.connect("elixir.db")


def _build_clan_war_context() -> tuple[dict, dict]:
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    try:
        members = [dict(r) for r in conn.execute(
            "SELECT player_tag AS tag, current_name AS name FROM members WHERE status='active'"
        )]
    finally:
        conn.close()
    clan = {"tag": CLAN_TAG, "name": "POAP KINGS", "memberList": members, "members": members}
    war = {}
    return clan, war


_QUESTION_MARK_TAIL = ("?", "?")


def _looks_like_clarifying_question(text: str) -> bool:
    """Heuristic: response is a clarification if it ends with a question mark
    OR contains a question that asks the user to disambiguate scope."""
    if not text:
        return False
    stripped = text.strip()
    if stripped.endswith(_QUESTION_MARK_TAIL):
        return True
    # Common clarification patterns the prompt encourages.
    lowered = stripped.lower()
    return any(
        phrase in lowered for phrase in (
            "do you mean", "which would you", "which scope", "current deck or",
            "full collection or", "war decks or", "did you want",
        )
    ) and "?" in stripped


def run_turn(
    member: dict,
    bucket: str,
    question: str,
    conversation_history: list[dict],
    clan: dict,
    war: dict,
) -> dict:
    """Run a single conversation turn through the interactive workflow."""
    reset_tool_capture()
    author = member.get("current_name") or member["player_tag"]

    row: dict = {"bucket": bucket, "question": question}

    # Light intent-router check so we know what route the question got — useful
    # for catching cases where a card question gets misrouted to deck_review.
    try:
        intent = classify_intent(
            question, workflow="interactive", mentioned=True,
            allows_open_channel_reply=False,
            conversation_history=conversation_history,
        )
        row["route"] = intent.get("route")
        row["intent_mode"] = intent.get("mode")
    except Exception as exc:
        row["intent_error"] = str(exc)
        row["route"] = None

    try:
        result = elixir_agent.respond_in_channel(
            question=question,
            author_name=author,
            channel_name="#ask-elixir",
            workflow="interactive",
            clan_data=clan,
            war_data=war,
            conversation_history=conversation_history,
            memory_context=None,
        )
    except Exception as exc:
        row["error"] = f"respond_in_channel raised: {exc}"
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
    if isinstance(content, list):
        content = "\n\n".join(str(s) for s in content if s)
    row["content"] = content
    row["content_len"] = len(content)
    row["event_type"] = result.get("event_type")
    row["clarifying"] = _looks_like_clarifying_question(content)

    # Flag the regression we're guarding against.
    row["used_deprecated_cards_include"] = any(
        name == "get_member" and "cards" in (args.get("include") or [])
        for name, args in row["tool_calls"] or []
    )
    row["used_card_profile"] = any(name == "get_member_card_profile" for name, _ in row["tool_calls"] or [])
    row["used_lookup_member_cards"] = any(name == "lookup_member_cards" for name, _ in row["tool_calls"] or [])

    return row


# ── Reporting ─────────────────────────────────────────────────────────────


def print_member_report(member: dict, turns: list[dict]) -> None:
    name = member.get("current_name") or member["player_tag"]
    print(f"\n── {name} ({member['player_tag']}) "
          f"[king_tower={member.get('king_tower')}, bucket={member.get('card_bucket')}] ──")
    for i, t in enumerate(turns, 1):
        flag = "!" if t.get("error") else (
            "·" if t.get("event_type") == "agent_error" else " "
        )
        tools = [name for name, _ in t.get("tool_calls") or []]
        tools_str = f" tools={','.join(tools)}" if tools else " tools=-"
        markers = []
        if t.get("clarifying"):
            markers.append("CLARIFY")
        if t.get("used_card_profile"):
            markers.append("profile")
        if t.get("used_lookup_member_cards"):
            markers.append("lookup")
        if t.get("used_deprecated_cards_include"):
            markers.append("DEPRECATED!")
        marker_str = f" [{','.join(markers)}]" if markers else ""
        clen = t.get("content_len") or 0
        print(f"  [{i}]{flag} {t.get('bucket'):11s}{tools_str:60s} len={clen:>4}{marker_str}")
        print(f"       Q: {t['question'][:120]}")
        if t.get("error"):
            print(f"       ERROR: {t['error']}")
        elif t.get("content"):
            preview = t["content"][:200].replace("\n", " ")
            print(f"       A: {preview}")


def print_summary(all_turns: list[tuple[dict, dict]]) -> None:
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total = len(all_turns)
    errors = sum(1 for _, t in all_turns if t.get("error"))
    null_responses = sum(
        1 for _, t in all_turns
        if t.get("event_type") == "agent_error" or (not t.get("error") and not t.get("content"))
    )
    deprecated = [(m, t) for m, t in all_turns if t.get("used_deprecated_cards_include")]
    clarifying = [(m, t) for m, t in all_turns if t.get("clarifying")]
    used_profile = sum(1 for _, t in all_turns if t.get("used_card_profile"))
    used_lookup = sum(1 for _, t in all_turns if t.get("used_lookup_member_cards"))

    print(f"Total turns: {total}")
    print(f"Errors: {errors}")
    print(f"Null/empty responses: {null_responses}  ← target: 0")
    print(f"Deprecated include=['cards']: {len(deprecated)}  ← target: 0 (regression check)")
    print(f"Card profile fired: {used_profile}/{total} turns")
    print(f"lookup_member_cards fired: {used_lookup}/{total} turns")
    print(f"Clarifying questions: {len(clarifying)}/{total}")

    if errors:
        print("\nError details:")
        for m, t in all_turns:
            if t.get("error"):
                name = m.get("current_name") or m.get("player_tag", "?")
                print(f"  {name} [{t.get('bucket')}]: {t['error']}")

    # Per-bucket breakdown.
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for _m, t in all_turns:
        by_bucket[t.get("bucket") or "?"].append(t)
    print("\nPer-bucket tool routing:")
    for bucket in ("broad", "upgrade", "rarity", "single_card", "ambiguous", "meta"):
        rows = by_bucket.get(bucket, [])
        if not rows:
            continue
        n = len(rows)
        prof = sum(1 for r in rows if r.get("used_card_profile"))
        look = sum(1 for r in rows if r.get("used_lookup_member_cards"))
        clar = sum(1 for r in rows if r.get("clarifying"))
        empty = sum(1 for r in rows if not r.get("error") and not r.get("content"))
        print(f"  {bucket:12s} n={n:>2}  profile={prof:>2}  lookup={look:>2}  clarify={clar:>2}  empty={empty:>2}")

    # Tool tally including deprecated-path detection.
    tools = Counter()
    for _m, t in all_turns:
        for name, _args in t.get("tool_calls") or []:
            tools[name] += 1
    print("\nTool call tally:")
    for name, n in tools.most_common():
        marker = " ← REGRESSION" if name == "get_member" and any(
            "cards" in (args.get("include") or [])
            for _m, t in all_turns
            for tname, args in (t.get("tool_calls") or [])
            if tname == name
        ) else ""
        print(f"  {name:30s} {n:>4}{marker}")

    if deprecated:
        print("\nDeprecated-path callers:")
        for m, t in deprecated:
            name = m.get("current_name") or m.get("player_tag", "?")
            print(f"  {name} [{t.get('bucket')}]: {t['question'][:80]}")


# ── Main ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--members", type=int, default=4, help="Number of members to test")
    parser.add_argument("--turns", type=int, default=6, help="Turns per member (max 6, one per bucket)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for member selection")
    parser.add_argument("--out", default="scripts/card_conversations_eval_results.json")
    args = parser.parse_args()

    if not os.getenv("CLAUDE_API_KEY"):
        print("ERROR: CLAUDE_API_KEY not set")
        sys.exit(1)

    if args.seed is not None:
        random.seed(args.seed)

    install_tool_capture()
    clan, war = _build_clan_war_context()

    members = pick_members(args.members)
    if not members:
        print("No active members found in DB; nothing to evaluate.")
        sys.exit(1)

    print(f"Selected {len(members)} members:")
    for m in members:
        print(f"  {m.get('current_name') or m['player_tag']:30s} "
              f"({m['player_tag']})  king_tower={m.get('king_tower')}  "
              f"bucket={m.get('card_bucket')}")

    all_rows = []
    all_turns: list[tuple[dict, dict]] = []

    for member in members:
        name = member.get("current_name") or member["player_tag"]
        print(f"\n— Generating script for {name} —")
        script = generate_card_script(member)
        if not script:
            print("    !! no script generated, skipping member")
            continue
        print(f"    {len(script)} questions:")
        for bucket, msg in script:
            print(f"    • [{bucket}] {msg}")

        conversation_history: list[dict] = []
        turns: list[dict] = []
        for i, (bucket, question) in enumerate(script[: args.turns], 1):
            print(f"    running turn {i}/{min(len(script), args.turns)} ({bucket})…")
            turn = run_turn(member, bucket, question, conversation_history, clan, war)
            turns.append(turn)
            all_turns.append((member, turn))

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
                "king_tower": member.get("king_tower"),
                "card_bucket": member.get("card_bucket"),
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
