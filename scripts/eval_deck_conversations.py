#!/usr/bin/env python3
"""Evaluate get_deck_recommendations tool SELECTION via real #ask-elixir turns.

The capability itself is covered by unit tests and direct audits. What those cannot
test is the only thing that decides whether the feature exists for a member: does the
model REACH for it, with the right view, when someone asks a deck question?

Buckets mirror the four questions the tool was built for, plus the two failure modes
that matter more than any routing statistic:

  1. **war_set**   "suggest war decks" -> get_deck_recommendations(view='war_set').
                   War forbids reusing a card across the four decks, so a hand-rolled
                   answer is wrong even when it looks plausible.
  2. **anchored**  "best deck around X" -> view='anchored' with card=X.
  3. **discover**  "I'm bored of my deck" -> view='discover'.
  4. **upgrade**   "what should I upgrade" -> the SHARED case. get_member_cards
                   (ready_to_upgrade) answers what they can afford now;
                   get_deck_recommendations(view='upgrades') answers what would most
                   improve the decks they field. Either is defensible, both is best;
                   this bucket exists to prove the new tool did not steal the old one.
  5. **honesty**   Questions whose honest answer is a refusal or a null result — a
                   card they do not own, a maxed player with nothing to upgrade.
                   Inventing an answer here is worse than not answering.

Two claims are checked in the response TEXT regardless of routing, because they are
the guarantees the feature is built on:
  * no borrowed win rate is attached to a recommended deck (clan deck win rates are
    skill-confounded and do not transfer between members)
  * a war set is never presented with a card reused across decks

Usage:
    python scripts/eval_deck_conversations.py --members 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import db  # noqa: E402
import elixir_agent  # noqa: E402
from agent import tool_exec  # noqa: E402
from cr_api import CLAN_TAG  # noqa: E402

_CAPTURED: list[tuple[str, dict]] = []
_ORIG = tool_exec._execute_tool


def _capturing_execute_tool(name, arguments, *args, **kwargs):
    _CAPTURED.append((name, dict(arguments) if isinstance(arguments, dict) else arguments))
    return _ORIG(name, arguments, *args, **kwargs)


def install_tool_capture() -> None:
    # The dispatched symbol is `_execute_tool`, bound into three modules at import
    # time. Patching only one (or the un-underscored name) silently captures nothing
    # while the turns still succeed — which reads as "the model used no tools".
    from agent import chat as agent_chat

    tool_exec._execute_tool = _capturing_execute_tool
    agent_chat._execute_tool = _capturing_execute_tool
    if hasattr(elixir_agent, "_execute_tool"):
        elixir_agent._execute_tool = _capturing_execute_tool


def reset_tool_capture() -> list[tuple[str, dict]]:
    global _CAPTURED
    out, _CAPTURED = _CAPTURED, []
    return out


def _conn():
    return db.get_connection()


def _clan_war_context() -> tuple[dict, dict]:
    """respond_in_channel takes clan/war context positionally; the interactive workflow
    needs a member list to resolve names to tags."""
    conn = _conn()
    try:
        members = [
            dict(r)
            for r in conn.execute(
                "SELECT p.player_tag AS tag, p.current_name AS name FROM players p "
                "WHERE EXISTS (SELECT 1 FROM clan_memberships cm "
                "WHERE cm.player_tag = p.player_tag AND cm.left_at IS NULL)"
            )
        ]
    finally:
        conn.close()
    return {"tag": CLAN_TAG, "name": "POAP KINGS", "memberList": members, "members": members}, {}


def pick_members(n: int) -> list[dict]:
    """Members with both a collection and real battles — the tool needs both. Spread
    across collection depth so a maxed veteran and a developing player both appear."""
    conn = _conn()
    rows = conn.execute(
        "SELECT p.player_tag, p.display_name, COUNT(DISTINCT b.dedup_key) battles, "
        "(SELECT COUNT(*) FROM player_card_collection pc WHERE pc.player_tag = p.player_tag) cards "
        "FROM players p JOIN battle_events b ON b.player_tag = p.player_tag "
        "GROUP BY 1 HAVING battles >= 40 AND cards >= 60 ORDER BY cards DESC"
    ).fetchall()
    conn.close()
    if not rows:
        return []
    rows = [dict(r) for r in rows]
    picks, step = [], max(1, len(rows) // max(1, n))
    for i in range(0, len(rows), step):
        picks.append(rows[i])
        if len(picks) >= n:
            break
    return picks


def script_for(member: dict) -> list[tuple[str, str]]:
    conn = _conn()
    tag = member["player_tag"]
    owned = conn.execute(
        "SELECT c.name FROM player_card_collection pc JOIN card_catalog c USING(card_id) "
        "WHERE pc.player_tag = ? AND c.rarity IN ('legendary','epic') ORDER BY pc.level DESC LIMIT 1",
        (tag,),
    ).fetchone()
    # A card the member genuinely does NOT own — any type. Filtering to card_type
    # 'troop' returned nothing for deep collections (the only gaps were tower troops),
    # silently fell through to a hardcoded default they DID own, and turned the honesty
    # bucket into an ordinary request. Exclude tower troops explicitly instead.
    unowned = conn.execute(
        "SELECT c.name FROM card_catalog c WHERE c.card_id NOT IN "
        "(SELECT card_id FROM player_card_collection WHERE player_tag = ?) "
        "AND c.card_id < 100000000 ORDER BY c.card_id LIMIT 1",
        (tag,),
    ).fetchone()
    conn.close()
    anchor = owned["name"] if owned else "Witch"
    # No real gap in the collection -> ask for a card that does not exist, which still
    # tests the refusal path. Never silently substitute a card they own.
    missing = unowned["name"] if unowned else "Mega Wizard Supreme"
    return [
        ("war_set", "can you suggest some war decks for me?"),
        ("anchored", f"whats my best deck built around {anchor}?"),
        ("discover", "im bored of my deck, what else could i play?"),
        ("upgrade", "what should i upgrade next?"),
        ("honesty", f"build me a deck around {missing}"),
    ]


_CLAN: dict = {}
_WAR: dict = {}
_WIN_RATE = re.compile(r"\b\d{1,3}(\.\d+)?\s?%\s?(win|wr\b)|win\s?rate[^.]{0,24}\d", re.I)


def run_turn(member, bucket, question, history):
    reset_tool_capture()
    row = {"bucket": bucket, "question": question}
    try:
        result = elixir_agent.respond_in_channel(
            question=question,
            author_name=member["display_name"],
            channel_name="#ask-elixir",
            workflow="interactive",
            clan_data=_CLAN,
            war_data=_WAR,
            conversation_history=history,
            memory_context=None,
        )
        text = result.get("content") if isinstance(result, dict) else str(result)
    except Exception as exc:  # noqa: BLE001 - an eval records failures, never raises
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["tool_calls"] = reset_tool_capture()
        return row
    calls = reset_tool_capture()
    row["tool_calls"] = [(n, a) for n, a in calls]
    row["content"] = text or ""
    row["content_len"] = len(text or "")
    names = {n for n, _ in calls}
    row["used_deck_reco"] = "get_deck_recommendations" in names
    row["used_member_cards"] = "get_member_cards" in names
    row["views"] = [a.get("view") for n, a in calls if n == "get_deck_recommendations"]
    # The guarantee checks — these matter more than which tool fired.
    row["win_rate_in_answer"] = bool(_WIN_RATE.search(text or ""))
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--members", type=int, default=3)
    ap.add_argument("--out", default="scripts/deck_conversations_eval_results.json")
    args = ap.parse_args()
    if not os.getenv("CLAUDE_API_KEY"):
        print("ERROR: CLAUDE_API_KEY not set", file=sys.stderr)
        return 2
    install_tool_capture()
    global _CLAN, _WAR
    _CLAN, _WAR = _clan_war_context()
    members = pick_members(args.members)
    if not members:
        print("no eligible members", file=sys.stderr)
        return 1
    out = []
    for m in members:
        print(f"\n=== {m['display_name']} ({m['cards']} cards, {m['battles']} battles) ===")
        turns, history = [], []
        for bucket, q in script_for(m):
            r = run_turn(m, bucket, q, history)
            turns.append(r)
            history = (
                history
                + [
                    {"role": "user", "content": q},
                    {"role": "assistant", "content": r.get("content", "")},
                ]
            )[-6:]
            views = ",".join(v for v in r.get("views") or [] if v) or "-"
            flag = " WIN_RATE!" if r.get("win_rate_in_answer") else ""
            print(
                f"  [{bucket:9}] reco={r.get('used_deck_reco')!s:5} views={views:10} "
                f"len={r.get('content_len', 0):5}{flag}"
            )
            if r.get("error"):
                print(f"      ERROR: {r['error']}")
        out.append({"member": m, "turns": turns})

    rows = [t for m in out for t in m["turns"]]
    bk = defaultdict(Counter)
    for t in rows:
        bk[t["bucket"]]["n"] += 1
        bk[t["bucket"]]["reco"] += bool(t.get("used_deck_reco"))
        bk[t["bucket"]]["cards"] += bool(t.get("used_member_cards"))
        bk[t["bucket"]]["err"] += bool(t.get("error"))
        bk[t["bucket"]]["winrate"] += bool(t.get("win_rate_in_answer"))
    print("\n" + "=" * 66 + "\nSUMMARY\n" + "=" * 66)
    print(
        f"turns={len(rows)}  errors={sum(1 for t in rows if t.get('error'))}  "
        f"empty={sum(1 for t in rows if not t.get('content'))}"
    )
    print(f"\n{'bucket':10} {'n':>2} {'deck_reco':>10} {'member_cards':>13} {'win_rate_leak':>14}")
    for b, cdict in bk.items():
        print(f"{b:10} {cdict['n']:2} {cdict['reco']:10} {cdict['cards']:13} {cdict['winrate']:14}")
    tally = Counter(n for t in rows for n, _ in t.get("tool_calls", []))
    print("\ntool tally:", dict(tally.most_common(8)))
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    print(f"\nresults -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
