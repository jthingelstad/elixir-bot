"""Unified eval: regular requests, deck requests, and cr_api tag lookups.

Drives three buckets through the real Elixir pipeline:

  1. **regular**  — general interactive Q&A (our clan, members, trophies, meta).
                    Routes via `classify_intent` → run `respond_in_channel` for
                    llm_chat / help / status / etc.  Deck-dispatched routes are
                    handed off to bucket 2.
  2. **deck**     — deck review / suggest / display. Uses the same pipeline as
                    `scripts/eval_deck_conversations.py` but keyed off the router.
  3. **cr_api**   — external lookups by CR tag. The prompts embed real non-clan
                    tags (players, clans, tournaments) so the LLM must reach for
                    the new `cr_api` tool to answer. Verifies the tool fires and
                    the envelope routes back cleanly.

Run with:  python scripts/eval_all_requests.py [--rounds N] [--per-bucket N]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from collections import Counter
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
from agent.core import _create_chat_completion, _chat_model_name
from agent.intent_router import classify_intent
from runtime.helpers._members import (
    _build_member_deck_report,
    _build_member_war_decks_report,
)
from cr_api import CLAN_TAG


# ── Real-data tag fixtures ────────────────────────────────────────────────

def _connect_db():
    return sqlite3.connect("elixir.db")


def sample_real_tags() -> dict:
    """Pick real tags from the local DB for use in cr_api prompts.

    Returns: {
        "our_member_tags": [...],       # for "tell me about me" style prompts
        "external_clan_tags": [...],    # for 'how is clan #XYZ?' prompts
        "external_player_tags": [...],  # for 'scout #ABC' prompts
    }
    """
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    try:
        our_members = [
            (r["current_name"] or r["player_tag"], r["player_tag"])
            for r in conn.execute(
                "SELECT current_name, player_tag FROM members "
                "WHERE status='active' ORDER BY RANDOM() LIMIT 5"
            )
        ]
        our_tag_norm = CLAN_TAG.lstrip("#").upper()
        external_clans = [
            (r["clan_name"], r["clan_tag"])
            for r in conn.execute(
                "SELECT DISTINCT clan_tag, clan_name FROM war_period_clan_status "
                "WHERE UPPER(REPLACE(clan_tag,'#','')) != ? "
                "ORDER BY RANDOM() LIMIT 6",
                (our_tag_norm,),
            )
            if r["clan_tag"]
        ]
        external_players = [
            (r["opponent_name"], r["opponent_tag"])
            for r in conn.execute(
                "SELECT DISTINCT opponent_tag, opponent_name "
                "FROM member_battle_facts "
                "WHERE opponent_tag IS NOT NULL AND opponent_tag != '' "
                "AND (opponent_clan_tag IS NULL OR UPPER(REPLACE(opponent_clan_tag,'#','')) != ?) "
                "ORDER BY RANDOM() LIMIT 8",
                (our_tag_norm,),
            )
            if r["opponent_tag"]
        ]
    finally:
        conn.close()
    return {
        "our_member_tags": our_members,
        "external_clan_tags": external_clans,
        "external_player_tags": external_players,
    }


# ── Request generation (LLM) ──────────────────────────────────────────────

REGULAR_HINT = (
    "Realistic questions a clan member or leader would ask Elixir about OUR clan, "
    "OUR roster, OR generic Clash Royale gameplay. Cover clan_status, kick_risk, "
    "top war contributors, a specific OUR-clan member's donations or recent form, "
    "'what arena is X in', meta/matchup questions, and 1-2 help/capabilities asks. "
    "Do NOT include tags from other clans."
)

DECK_HINT = (
    "Realistic deck questions a member would send. Vary: 'review my deck', "
    "'suggest a better deck', 'show me my war decks', 'swap X for Y in my war "
    "deck 2', 'what cards are in {member}'s deck'. Mix regular ladder and war "
    "decks. Some should be first-turn; some can be short follow-ups."
)

CR_API_HINT = (
    "Realistic external-lookup questions that REQUIRE the cr_api tool. "
    "Each question MUST include exactly one CR tag from the fixtures below. "
    "Mix: 'how strong is clan #XYZ', 'what's #XYZ's current river race standing', "
    "'scout player #ABC — what's their deck', 'what's their recent battle log', "
    "'pull up the top members of #XYZ', 'what's in tournament #T's roster'. "
    "At LEAST one should ask for a player's recent battles (aspect chaining)."
)


def generate_requests(bucket: str, count: int, fixtures: dict, round_idx: int) -> list[dict]:
    """Ask the LLM for a batch of realistic questions for a bucket."""
    if bucket == "regular":
        hint = REGULAR_HINT
        fixtures_block = ""
    elif bucket == "deck":
        hint = DECK_HINT
        members = fixtures["our_member_tags"][:3]
        member_lines = "\n".join(f"  - {name} ({tag})" for name, tag in members)
        fixtures_block = f"\n\nUse these real clan members when the phrasing calls for a name:\n{member_lines}"
    elif bucket == "cr_api":
        hint = CR_API_HINT
        clans = fixtures["external_clan_tags"][:4]
        players = fixtures["external_player_tags"][:4]
        clan_lines = "\n".join(f"  - {n} ({t})" for n, t in clans) or "  (none)"
        player_lines = "\n".join(f"  - {n} ({t})" for n, t in players) or "  (none)"
        fixtures_block = (
            f"\n\n=== EXTERNAL CLAN TAGS ===\n{clan_lines}"
            f"\n\n=== EXTERNAL PLAYER TAGS ===\n{player_lines}"
            "\n\nUse ONLY tags from these two lists verbatim (tag must appear in the question)."
        )
    else:
        raise ValueError(bucket)

    prompt = (
        f"You are generating realistic Discord messages a clan member might post "
        f"to Elixir. Round {round_idx}.\n\n"
        f"**Bucket:** {bucket}\n{hint}{fixtures_block}\n\n"
        f"Generate exactly {count} questions. Vary phrasing wildly — short/long, "
        f"casual/formal, with/without typos, some direct questions, some terse commands. "
        f"Avoid obvious repetition.\n\n"
        "Return ONLY a JSON array of strings, no wrapper, no commentary."
    )
    resp = _create_chat_completion(
        workflow=f"eval_allreqs_{bucket}",
        messages=[
            {"role": "system", "content": "You produce realistic test data. Output strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        model=_chat_model_name(),
        temperature=1.0,
        max_tokens=2048,
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
        print(f"  !! {bucket} generation returned invalid JSON: {exc}")
        return []
    return [{"bucket": bucket, "question": s} for s in items if isinstance(s, str) and s.strip()]


# ── Tool-call capture ─────────────────────────────────────────────────────

_tool_calls_for_turn: list[tuple[str, dict]] = []
_original_execute_tool = tool_exec._execute_tool


def _capturing_execute_tool(name, arguments, *args, **kwargs):
    _tool_calls_for_turn.append((name, arguments))
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

def _fake_clan_ctx() -> tuple[dict, dict]:
    """Build a lightweight clan/war context snapshot from local DB."""
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    try:
        members = [dict(r) for r in conn.execute(
            "SELECT player_tag AS tag, current_name AS name FROM members WHERE status='active'"
        )]
    finally:
        conn.close()
    clan = {
        "tag": CLAN_TAG,
        "name": "POAP KINGS",
        "memberList": members,
        "members": members,
    }
    war = {}
    return clan, war


def _resolve_target_member(question: str, intent: dict) -> dict | None:
    """Best-effort member resolution for deck routes: use router-provided target
    or the first active member matched by name substring."""
    target = intent.get("target_member")
    members = db.list_members("active")
    if target:
        low = str(target).lower()
        for m in members:
            name = (m.get("current_name") or "").lower()
            if low in name or m["player_tag"] == target:
                return m
    ql = question.lower()
    if any(w in ql for w in (" my ", "my deck", "my war", "i have", "i'm", "i am")):
        # use a deterministic "self" stand-in — pick a regular-war member
        from storage.war_analytics import war_player_types_by_tag
        tags = [m["player_tag"] for m in members]
        conn = db.get_connection()
        try:
            types_by_tag = war_player_types_by_tag(conn, tags)
        finally:
            conn.close()
        for m in members:
            if types_by_tag.get(m["player_tag"]) == "regular":
                return m
    return members[0] if members else None


def run_request(req: dict, clan: dict, war: dict) -> dict:
    """Route + execute a single request through the real pipeline."""
    question = req["question"]
    reset_tool_capture()
    intent = classify_intent(
        question, workflow="interactive", mentioned=True,
        allows_open_channel_reply=False, conversation_history=[],
    )
    route = intent.get("route")
    mode = intent.get("mode")
    row = {
        "bucket": req["bucket"],
        "question": question,
        "route": route,
        "mode": mode,
        "confidence": intent.get("confidence"),
        "rationale": intent.get("rationale"),
    }

    try:
        if route == "deck_display":
            member = _resolve_target_member(question, intent)
            if not member:
                row["error"] = "no member to resolve for deck_display"
                row["tool_calls"] = reset_tool_capture()
                return row
            if mode == "war":
                content = _build_member_war_decks_report(member["player_tag"])
                row["event_type"] = "deck_display_war"
            else:
                content = _build_member_deck_report(member["player_tag"])
                row["event_type"] = "deck_display"
            row["content"] = content
            row["content_len"] = len(content or "")
            row["resolved_member"] = member.get("current_name")
            row["tool_calls"] = reset_tool_capture()
            return row

        if route in {"deck_review", "deck_suggest"}:
            member = _resolve_target_member(question, intent)
            if not member:
                row["error"] = "no member to resolve for deck_review"
                row["tool_calls"] = reset_tool_capture()
                return row
            subject = "review" if route == "deck_review" else "suggest"
            result = elixir_agent.respond_in_deck_review(
                question=question,
                author_name=member.get("current_name") or member["player_tag"],
                channel_name="#eval",
                mode=mode or "regular",
                subject=subject,
                target_member_tag=member["player_tag"],
                target_member_name=member.get("current_name"),
                conversation_history=[],
                memory_context=None,
            )
            row["resolved_member"] = member.get("current_name")
        elif route == "not_for_bot":
            row["skipped"] = "not_for_bot"
            row["tool_calls"] = reset_tool_capture()
            return row
        else:
            # help, clan_status, kick_risk, top_war_contributors, roster_join_dates,
            # status_report, schedule_report, llm_chat — all delegate to respond_in_channel
            # for the LLM+tools workflow. (The real runtime has dedicated report builders
            # for some routes, but for eval-of-LLM-quality purposes respond_in_channel
            # covers what we care about: routing confidence + tool usage.)
            result = elixir_agent.respond_in_channel(
                question=question,
                author_name="Eval",
                channel_name="#eval",
                workflow="interactive",
                clan_data=clan,
                war_data=war,
                conversation_history=[],
                memory_context=None,
            )
    except Exception as exc:
        row["error"] = f"pipeline raised: {exc}"
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
    row["summary"] = result.get("summary")
    return row


# ── Reporting ─────────────────────────────────────────────────────────────

def print_round_summary(round_idx: int, rows: list[dict]) -> None:
    print(f"\n{'=' * 72}\nROUND {round_idx} SUMMARY\n{'=' * 72}")
    by_bucket = Counter(r["bucket"] for r in rows)
    errors = [r for r in rows if r.get("error")]
    print(f"Total: {len(rows)} | errors: {len(errors)} | by-bucket: {dict(by_bucket)}")

    print("\nRoute distribution (per bucket):")
    per_bucket_routes: dict[str, Counter] = {}
    for r in rows:
        per_bucket_routes.setdefault(r["bucket"], Counter())[r.get("route") or "?"] += 1
    for bucket, ctr in per_bucket_routes.items():
        dist = ", ".join(f"{k}={v}" for k, v in ctr.most_common())
        print(f"  {bucket:8s} → {dist}")

    print("\nTool-call usage (per bucket):")
    per_bucket_tools: dict[str, Counter] = {}
    for r in rows:
        bucket_tools = per_bucket_tools.setdefault(r["bucket"], Counter())
        for name, _ in r.get("tool_calls") or []:
            bucket_tools[name] += 1
    for bucket, ctr in per_bucket_tools.items():
        if not ctr:
            print(f"  {bucket:8s} → (no tools)")
            continue
        print(f"  {bucket:8s} → " + ", ".join(f"{k}={v}" for k, v in ctr.most_common()))

    # cr_api bucket: did cr_api tool fire?
    cr_rows = [r for r in rows if r["bucket"] == "cr_api"]
    cr_fired = [r for r in cr_rows if any(n == "cr_api" for n, _ in (r.get("tool_calls") or []))]
    if cr_rows:
        print(f"\ncr_api bucket: {len(cr_fired)}/{len(cr_rows)} prompts actually triggered cr_api tool")
        for r in cr_rows:
            tool_names = {n for n, _ in (r.get("tool_calls") or [])}
            mark = "OK" if "cr_api" in tool_names else "MISS"
            print(f"  [{mark}] route={r.get('route'):12s} tools={sorted(tool_names) or '-'}  Q: {r['question'][:80]}")

    # deck bucket: did deck route fire?
    deck_rows = [r for r in rows if r["bucket"] == "deck"]
    if deck_rows:
        deck_routed = sum(1 for r in deck_rows if r.get("route") in {"deck_display", "deck_review", "deck_suggest"})
        print(f"\ndeck bucket: {deck_routed}/{len(deck_rows)} prompts routed to a deck_* intent")

    if errors:
        print("\nErrors:")
        for r in errors:
            print(f"  [{r['bucket']}] route={r.get('route')}  Q: {r['question'][:80]}")
            print(f"    → {r['error']}")

    # Short A-previews for each row to eyeball quality
    print("\nPreviews:")
    for r in rows:
        flag = "!" if r.get("error") else ("·" if r.get("skipped") else " ")
        preview = (r.get("content") or r.get("error") or r.get("skipped") or "")
        preview = preview[:160].replace("\n", " ")
        tool_list = ",".join(n for n, _ in r.get("tool_calls") or []) or "-"
        print(f"  [{r['bucket']:7s}]{flag} route={r.get('route'):12s} tools={tool_list}")
        print(f"       Q: {r['question'][:110]}")
        print(f"       A: {preview}")


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--per-bucket", type=int, default=4, help="Questions per bucket per round")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out", default="scripts/eval_all_requests_results.json")
    args = parser.parse_args()

    if not os.getenv("CLAUDE_API_KEY"):
        print("ERROR: CLAUDE_API_KEY not set")
        sys.exit(1)

    if args.seed is not None:
        random.seed(args.seed)

    install_tool_capture()
    fixtures = sample_real_tags()
    clan, war = _fake_clan_ctx()

    print("Fixtures:")
    print(f"  our members: {len(fixtures['our_member_tags'])}")
    print(f"  external clans: {len(fixtures['external_clan_tags'])}")
    print(f"  external players: {len(fixtures['external_player_tags'])}")

    all_rows: list[dict] = []
    for round_idx in range(1, args.rounds + 1):
        print(f"\n── Round {round_idx}/{args.rounds}: generating prompts ──")
        reqs: list[dict] = []
        for bucket in ("regular", "deck", "cr_api"):
            batch = generate_requests(bucket, args.per_bucket, fixtures, round_idx)
            print(f"  {bucket:7s} → {len(batch)} prompts")
            reqs.extend(batch)

        for i, req in enumerate(reqs, 1):
            print(f"  [{i}/{len(reqs)}] ({req['bucket']}) running…", flush=True)
            row = run_request(req, clan, war)
            row["round"] = round_idx
            all_rows.append(row)

        round_rows = [r for r in all_rows if r["round"] == round_idx]
        print_round_summary(round_idx, round_rows)

    # Final rollup across rounds
    if args.rounds > 1:
        print(f"\n{'#' * 72}\nACROSS-ROUNDS ROLLUP\n{'#' * 72}")
        print_round_summary(0, all_rows)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(all_rows, indent=2, default=str))
    print(f"\nFull results → {out_path}")


if __name__ == "__main__":
    main()
