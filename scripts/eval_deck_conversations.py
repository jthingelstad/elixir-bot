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

Two claims are checked regardless of routing, because they are the guarantees the
feature is built on:
  * no borrowed win rate is attached to a recommended deck (clan deck win rates are
    skill-confounded and do not transfer between members)
  * the structured war-set result contains four 8-card decks with 32 distinct cards

Usage:
    python scripts/eval_deck_conversations.py --members 3 --strict
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

_CAPTURED: list[dict[str, Any]] = []
_ORIG = tool_exec._execute_tool


@dataclass(frozen=True)
class Thresholds:
    max_error_count: int = 0
    max_empty_response_count: int = 0
    min_required_tool_view_rate: float = 1.0
    max_win_rate_leak_count: int = 0
    min_honesty_pass_rate: float = 1.0
    min_war_set_card_uniqueness_rate: float = 1.0


_HONESTY_REFUSAL = re.compile(
    r"(?:\b(?:no|not|isn't|is not)\b[^.!?\n]{0,48}\bcard\b|"
    r"\bcard\b[^.!?\n]{0,48}\b(?:doesn't exist|does not exist|isn't real|is not real|"
    r"not found)\b|\bnothing by that name\b|\bmade[- ]?up\b|"
    r"\b(?:can't|cannot|couldn't|could not)\b[^.!?\n]{0,48}\b(?:build|find|recommend)\b)",
    re.I,
)


def _capturing_execute_tool(name, arguments, *args, **kwargs):
    result = _ORIG(name, arguments, *args, **kwargs)
    try:
        parsed_result = json.loads(result) if isinstance(result, str) else result
    except json.JSONDecodeError:
        parsed_result = result
    _CAPTURED.append(
        {
            "name": name,
            "arguments": dict(arguments) if isinstance(arguments, dict) else arguments,
            "result": parsed_result,
        }
    )
    return result


def install_tool_capture() -> None:
    # The dispatched symbol is `_execute_tool`, bound into three modules at import
    # time. Patching only one (or the un-underscored name) silently captures nothing
    # while the turns still succeed — which reads as "the model used no tools".
    from agent import chat as agent_chat

    tool_exec._execute_tool = _capturing_execute_tool
    agent_chat._execute_tool = _capturing_execute_tool
    if hasattr(elixir_agent, "_execute_tool"):
        elixir_agent._execute_tool = _capturing_execute_tool


def reset_tool_capture() -> list[dict[str, Any]]:
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
        "WHERE EXISTS (SELECT 1 FROM clan_memberships cm WHERE cm.player_tag = p.player_tag "
        "AND cm.left_at IS NULL) "
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


def _tool_payload(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value and set(value) & {"ok", "error", "meta"}:
        return value["data"]
    return value


def _tool_calls(trace: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return [(item.get("name", ""), item.get("arguments")) for item in trace]


def _tool_error_count(trace: list[dict[str, Any]]) -> int:
    count = 0
    for item in trace:
        result = item.get("result")
        if isinstance(result, dict) and result.get("error") and not result.get("capability"):
            count += 1
    return count


def _rate(passing: int, total: int) -> float:
    return round(passing / total, 3) if total else 0.0


def _metric(value: Any, threshold: dict[str, Any], passed: bool, definition: str) -> dict:
    return {
        "definition": definition,
        "threshold": threshold,
        "value": value,
        "passed": bool(passed),
    }


def _required_tool_view_pass(turn: dict[str, Any]) -> bool:
    bucket = turn.get("bucket")
    calls = turn.get("tool_calls") or []
    if bucket in {"war_set", "anchored", "discover"}:
        return any(
            name == "get_deck_recommendations" and (arguments or {}).get("view") == bucket
            for name, arguments in calls
        )
    if bucket == "upgrade":
        return any(
            (name == "get_deck_recommendations" and (arguments or {}).get("view") == "upgrades")
            or (
                name == "get_member_cards"
                and (arguments or {}).get("view") == "lookup"
                and ((arguments or {}).get("filter") or {}).get("ready_to_upgrade") is True
            )
            for name, arguments in calls
        )
    return True


def _war_set_check(trace: list[dict[str, Any]]) -> dict[str, Any]:
    results = [
        _tool_payload(item.get("result"))
        for item in trace
        if item.get("name") == "get_deck_recommendations"
        and (item.get("arguments") or {}).get("view") == "war_set"
    ]
    if not results:
        return {"passed": False, "reason": "missing_structured_war_set_result", "duplicates": []}

    duplicate_names: set[str] = set()
    failures: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            failures.append("unparseable_structured_war_set_result")
            continue
        decks = result.get("decks")
        if result.get("available") is not True or not isinstance(decks, list):
            failures.append(f"unavailable_war_set:{result.get('error') or 'unknown'}")
            continue
        if len(decks) != 4:
            failures.append(f"expected_4_decks_got_{len(decks)}")
        seen: Counter[str] = Counter()
        for index, deck in enumerate(decks, start=1):
            cards = deck.get("cards") if isinstance(deck, dict) else None
            if not isinstance(cards, list) or len(cards) != 8:
                failures.append(
                    f"deck_{index}_expected_8_cards_got_"
                    f"{len(cards) if isinstance(cards, list) else 'unparseable'}"
                )
                continue
            names = []
            for card in cards:
                name = card.get("name") if isinstance(card, dict) else card
                if not isinstance(name, str) or not name.strip():
                    failures.append(f"deck_{index}_has_unparseable_card")
                    continue
                names.append(name.strip().casefold())
            seen.update(names)
        duplicate_names.update(name for name, count in seen.items() if count > 1)
        if len(seen) != 32:
            failures.append(f"expected_32_distinct_cards_got_{len(seen)}")

    if duplicate_names:
        failures.append("card_reused_across_war_decks")
    return {
        "passed": not failures,
        "reason": ";".join(failures) if failures else None,
        "duplicates": sorted(duplicate_names),
    }


def _honesty_check(turn: dict[str, Any]) -> dict[str, Any]:
    trace = turn.get("tool_trace") or []
    negative_evidence = False
    successful_deck_result = False
    for item in trace:
        payload = _tool_payload(item.get("result"))
        if item.get("name") == "lookup_cards":
            negative_evidence |= payload == [] or (
                isinstance(payload, dict)
                and not payload.get("cards")
                and not payload.get("results")
                and not payload.get("items")
            )
        if item.get("name") == "get_deck_recommendations" and isinstance(payload, dict):
            negative_evidence |= payload.get("error") in {"unknown_card", "card_not_owned"}
            successful_deck_result |= payload.get("available") is True
    refused = bool(_HONESTY_REFUSAL.search(turn.get("content") or ""))
    return {
        "passed": bool(
            not turn.get("error") and turn.get("content") and refused and not successful_deck_result
        ),
        "negative_tool_evidence": negative_evidence,
        "refusal_language": refused,
        "successful_deck_result": successful_deck_result,
    }


def score_results(results: list[dict[str, Any]], thresholds: Thresholds | None = None) -> dict:
    thresholds = thresholds or Thresholds()
    rows = [turn for member in results for turn in member.get("turns", [])]
    required = [
        turn
        for turn in rows
        if turn.get("bucket") in {"war_set", "anchored", "discover", "upgrade"}
    ]
    honesty = [turn for turn in rows if turn.get("bucket") == "honesty"]
    war_sets = [turn for turn in rows if turn.get("bucket") == "war_set"]

    required_passes = sum(_required_tool_view_pass(turn) for turn in required)
    honesty_passes = sum(bool((turn.get("honesty_check") or {}).get("passed")) for turn in honesty)
    war_set_passes = sum(bool((turn.get("war_set_check") or {}).get("passed")) for turn in war_sets)
    error_count = sum(
        bool(turn.get("error")) + int(turn.get("tool_error_count") or 0) for turn in rows
    )
    empty_count = sum(not (turn.get("content") or "").strip() for turn in rows)
    win_rate_count = sum(bool(turn.get("win_rate_in_answer")) for turn in rows)
    required_rate = _rate(required_passes, len(required))
    honesty_rate = _rate(honesty_passes, len(honesty))
    war_set_rate = _rate(war_set_passes, len(war_sets))

    metrics = {
        "error_count": _metric(
            error_count,
            {"<=": thresholds.max_error_count},
            error_count <= thresholds.max_error_count,
            "Turn exceptions plus tool executions that returned an infrastructure or invocation error rather than a capability result.",
        ),
        "empty_response_count": _metric(
            empty_count,
            {"<=": thresholds.max_empty_response_count},
            empty_count <= thresholds.max_empty_response_count,
            "Turns whose response content is empty or whitespace-only.",
        ),
        "required_tool_view_rate": _metric(
            required_rate,
            {">=": thresholds.min_required_tool_view_rate},
            bool(required) and required_rate >= thresholds.min_required_tool_view_rate,
            "War-set, anchored, and discover turns using get_deck_recommendations with the matching view, plus upgrade turns using upgrades or ready-to-upgrade lookup.",
        ),
        "win_rate_leak_count": _metric(
            win_rate_count,
            {"<=": thresholds.max_win_rate_leak_count},
            win_rate_count <= thresholds.max_win_rate_leak_count,
            "Responses that attach a numeric win rate or win-rate percentage to the answer.",
        ),
        "honesty_pass_rate": _metric(
            honesty_rate,
            {">=": thresholds.min_honesty_pass_rate},
            bool(honesty) and honesty_rate >= thresholds.min_honesty_pass_rate,
            "Honesty turns with explicit refusal language and no successful deck recommendation; negative structured tool evidence is recorded when available.",
        ),
        "war_set_card_uniqueness_rate": _metric(
            war_set_rate,
            {">=": thresholds.min_war_set_card_uniqueness_rate},
            bool(war_sets) and war_set_rate >= thresholds.min_war_set_card_uniqueness_rate,
            "War-set turns whose structured result has exactly four 8-card decks and 32 distinct normalized card names.",
        ),
    }
    return {
        "passed": all(metric["passed"] for metric in metrics.values()),
        "metrics": metrics,
        "counts": {
            "turns": len(rows),
            "required_tool_view_turns": len(required),
            "honesty_turns": len(honesty),
            "war_set_turns": len(war_sets),
        },
    }


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
        trace = reset_tool_capture()
        row["tool_calls"] = _tool_calls(trace)
        row["tool_trace"] = trace
        row["tool_error_count"] = _tool_error_count(trace)
        row["content"] = ""
        row["content_len"] = 0
        row["win_rate_in_answer"] = False
        if bucket == "war_set":
            row["war_set_check"] = _war_set_check(trace)
        if bucket == "honesty":
            row["honesty_check"] = _honesty_check(row)
        return row
    trace = reset_tool_capture()
    calls = _tool_calls(trace)
    row["tool_calls"] = calls
    row["tool_trace"] = trace
    row["tool_error_count"] = _tool_error_count(trace)
    row["content"] = text or ""
    row["content_len"] = len(text or "")
    names = {n for n, _ in calls}
    row["used_deck_reco"] = "get_deck_recommendations" in names
    row["used_member_cards"] = "get_member_cards" in names
    row["views"] = [a.get("view") for n, a in calls if n == "get_deck_recommendations"]
    # The guarantee checks — these matter more than which tool fired.
    row["win_rate_in_answer"] = bool(_WIN_RATE.search(text or ""))
    if bucket == "war_set":
        row["war_set_check"] = _war_set_check(trace)
    if bucket == "honesty":
        row["honesty_check"] = _honesty_check(row)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--members", type=int, default=3)
    ap.add_argument("--out", default="scripts/deck_conversations_eval_results.json")
    ap.add_argument("--strict", action="store_true", help="Exit non-zero when thresholds fail")
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
    summary = score_results(out)
    artifact = {
        "harness": "eval_deck_conversations",
        "summary": summary,
        "results": out,
    }
    for name, metric in summary["metrics"].items():
        status = "PASS" if metric["passed"] else "FAIL"
        print(f"  {status} {name}: {metric['value']} threshold={metric['threshold']}")
    print(f"  overall: {'PASS' if summary['passed'] else 'FAIL'}")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str))
    print(f"\nresults -> {args.out}")
    return 2 if args.strict and not summary["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
