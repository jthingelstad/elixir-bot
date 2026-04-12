"""Evaluate the intent router against LLM-generated user questions.

Round 1: ask Claude to generate a diverse batch of realistic clan-member questions
across clan/deck/trophy-road/general categories. Run each through the router.
Tally: route distribution, low-confidence cases, fallbacks, suspicious choices.

Run with:  python scripts/eval_intent_router.py [--rounds N] [--per-round N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Load .env so CLAUDE_API_KEY is available without a manual export.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agent.core import _create_chat_completion, _chat_model_name
from agent.intent_router import classify_intent
from runtime.intent_registry import ROUTE_KEYS

CATEGORIES = [
    ("clan_ops", "leader-style operations questions: kicks, promotions, who joined, contributors, war participation, roster review"),
    ("clan_member", "regular member questions about other members or the clan: who is doing well, donations, wars left, who's new"),
    ("own_deck", "questions about the speaker's own deck: review, suggest, what's in it, war decks, swap suggestions"),
    ("other_deck", "questions about another player's deck (by name or @mention)"),
    ("trophy_road", "trophy road, arenas, leagues, climbing, pathing of evolution unlocks, rewards"),
    ("general_cr", "general Clash Royale gameplay: card matchups, archetypes, meta, tips, mechanics"),
    ("help_meta", "what can the bot do, how to use it, what commands exist"),
    ("chat_noise", "casual chat: thanks, lol, agreement, off-topic — bot should usually NOT respond"),
    ("status_ops", "operator system/status/schedule questions about the bot itself"),
    ("ambiguous", "tricky or borderline phrasings that could route multiple ways"),
]


def generate_questions(per_category: int = 5, model: str | None = None, seed_round: int = 1) -> list[dict]:
    """Ask the LLM to generate a batch of realistic user questions, tagged by category."""
    category_lines = "\n".join(f"- **{key}**: {desc}" for key, desc in CATEGORIES)
    prompt = (
        f"You are generating a realistic test set of Discord messages a Clash Royale clan "
        f"member or operator might post in a channel where the Elixir bot is listening.\n\n"
        f"Generate exactly {per_category} questions in EACH of the categories below. Vary the "
        f"phrasing significantly — short and long, casual and formal, with and without typos, "
        f"with and without @mentions, with quantifiers ('three new decks'), with implicit "
        f"references ('mine', 'theirs'). Avoid repeating phrasings from earlier rounds.\n\n"
        f"This is round {seed_round}; lean into phrasings you haven't generated before.\n\n"
        f"=== CATEGORIES ===\n{category_lines}\n\n"
        "Return ONLY a JSON array (no markdown wrapper, no commentary). Each element:\n"
        '  {"category": "<category_key>", "question": "<message text>"}'
    )
    resp = _create_chat_completion(
        workflow="eval_question_gen",
        messages=[
            {"role": "system", "content": "You produce realistic test data. Output strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        model=model or _chat_model_name(),
        temperature=1.0,
        max_tokens=4096,
    )
    text = (resp.choices[0].message.content or "").strip()
    # Strip accidental code fences if the model adds them
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"  !! generation returned invalid JSON: {exc}")
        print(f"  raw preview: {text[:300]}")
        return []
    if not isinstance(items, list):
        return []
    valid_categories = {key for key, _ in CATEGORIES}
    return [
        {"category": q["category"], "question": q["question"]}
        for q in items
        if isinstance(q, dict)
        and q.get("category") in valid_categories
        and isinstance(q.get("question"), str)
        and q["question"].strip()
    ]


# Categories → which routes are reasonable for them (used for sanity scoring).
EXPECTED_ROUTES = {
    "clan_ops": {"kick_risk", "top_war_contributors", "roster_join_dates", "clan_status", "llm_chat"},
    "clan_member": {"llm_chat", "roster_join_dates", "top_war_contributors"},
    "own_deck": {"deck_display", "deck_review", "deck_suggest"},
    "other_deck": {"deck_display", "deck_review", "deck_suggest", "llm_chat"},
    "trophy_road": {"llm_chat"},
    "general_cr": {"llm_chat"},
    "help_meta": {"help"},
    "chat_noise": {"not_for_bot", "llm_chat"},
    "status_ops": {"status_report", "schedule_report", "clan_status", "llm_chat"},
    "ambiguous": set(ROUTE_KEYS),  # anything goes for these
}


def evaluate_batch(questions: list[dict], *, workflow: str = "interactive") -> list[dict]:
    """Classify each question and return rich rows for analysis."""
    rows = []
    for i, item in enumerate(questions, 1):
        intent = classify_intent(
            item["question"],
            workflow=workflow,
            mentioned=True,
            allows_open_channel_reply=False,
        )
        expected = EXPECTED_ROUTES.get(item["category"], set(ROUTE_KEYS))
        sane = intent.get("route") in expected
        rows.append({
            "i": i,
            "category": item["category"],
            "question": item["question"],
            "route": intent.get("route"),
            "mode": intent.get("mode"),
            "target_member": intent.get("target_member"),
            "confidence": intent.get("confidence"),
            "rationale": intent.get("rationale"),
            "fallback_reason": intent.get("fallback_reason"),
            "latency_ms": intent.get("latency_ms"),
            "sane": sane,
        })
    return rows


def report(rows: list[dict], round_num: int) -> dict:
    print(f"\n{'='*70}")
    print(f"ROUND {round_num} REPORT — {len(rows)} questions")
    print('='*70)

    route_counts = Counter(r["route"] for r in rows)
    print("\nRoute distribution:")
    for route, n in route_counts.most_common():
        print(f"  {route:25s} {n:>4}")

    fallbacks = [r for r in rows if r.get("fallback_reason")]
    print(f"\nFallback rate: {len(fallbacks)}/{len(rows)} = {len(fallbacks)/len(rows)*100:.1f}%")
    for r in fallbacks[:5]:
        print(f"  - {r['fallback_reason']}: {r['question'][:80]!r}")

    insane = [r for r in rows if not r["sane"]]
    print(f"\nOut-of-expected-set classifications: {len(insane)}/{len(rows)}")
    for r in insane:
        print(f"  cat={r['category']:13s} → route={r['route']:22s} q={r['question'][:80]!r}")
        print(f"      rationale: {r['rationale']}")

    by_cat_correct = defaultdict(lambda: [0, 0])
    for r in rows:
        by_cat_correct[r["category"]][0] += 1 if r["sane"] else 0
        by_cat_correct[r["category"]][1] += 1
    print("\nCoverage by category (in-expected-set / total):")
    for cat, (ok, n) in sorted(by_cat_correct.items()):
        pct = ok / n * 100 if n else 0
        print(f"  {cat:13s} {ok:>3} / {n:<3}  {pct:5.1f}%")

    low_conf = [r for r in rows if (r.get("confidence") or 0) < 0.6]
    print(f"\nLow-confidence (<0.6) classifications: {len(low_conf)}")
    for r in low_conf[:8]:
        print(f"  conf={r.get('confidence'):.2f} {r['category']:13s} → {r['route']:22s} q={r['question'][:70]!r}")

    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms")]
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies)//2]
        p95 = latencies[int(len(latencies)*0.95)]
        print(f"\nLatency: p50={p50:.0f}ms  p95={p95:.0f}ms  max={max(latencies):.0f}ms")

    return {
        "total": len(rows),
        "fallback_rate": len(fallbacks) / len(rows),
        "insane_rate": len(insane) / len(rows),
        "by_cat": dict(by_cat_correct),
        "route_counts": dict(route_counts),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--per-round", type=int, default=5, help="questions per category per round")
    parser.add_argument("--out", default="scripts/intent_router_eval_results.json")
    args = parser.parse_args()

    if not os.getenv("CLAUDE_API_KEY"):
        print("ERROR: CLAUDE_API_KEY not set in env (and not in loaded .env)")
        sys.exit(1)

    all_rows = []
    summaries = []
    for round_num in range(1, args.rounds + 1):
        print(f"\n— Generating round {round_num} ({args.per_round} per category × {len(CATEGORIES)} cats) —")
        questions = generate_questions(per_category=args.per_round, seed_round=round_num)
        if not questions:
            print(f"  round {round_num} produced no questions, skipping")
            continue
        print(f"  generated {len(questions)} questions")
        print(f"— Classifying round {round_num} —")
        rows = evaluate_batch(questions)
        for r in rows:
            r["round"] = round_num
        summary = report(rows, round_num)
        all_rows.extend(rows)
        summaries.append(summary)

    if all_rows:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"rows": all_rows, "summaries": summaries}, indent=2))
        print(f"\nFull results written to {out_path}")

        print("\n" + "="*70)
        print("OVERALL")
        print("="*70)
        total = len(all_rows)
        fb = sum(1 for r in all_rows if r.get("fallback_reason"))
        insane = sum(1 for r in all_rows if not r["sane"])
        print(f"Total questions: {total}")
        print(f"Fallback rate: {fb/total*100:.1f}%")
        print(f"Out-of-expected-set: {insane/total*100:.1f}%")


if __name__ == "__main__":
    main()
