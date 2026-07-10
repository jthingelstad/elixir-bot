#!/usr/bin/env python3
"""Retrospective post-quality eval (confidence plan Pillar 3).

Samples what Elixir ACTUALLY posted (editor_verdicts.final_copy ⋈ the intent
facts that drove it) and grades each post two ways:
  (a) deterministic GAME-KNOWLEDGE accuracy — engine.game_check (no LLM);
  (b) an LLM DEPTH judge — did it look at the subject, voice, member-value
      (best-effort; skipped headless if no API so the harness always runs).

Emits a per-lane scorecard + a flagged list (post + the specific bad/thin
claim), stores a summary row in post_quality_runs (trend), and exposes
`run_eval(days, lane=None) -> dict` for the Phase-5 confidence report.

    python scripts/eval_post_quality.py --days 3
    python scripts/eval_post_quality.py --days 7 --json
    python scripts/eval_post_quality.py --lane battle-feed --no-llm
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import db  # noqa: E402
from engine import game_check  # noqa: E402

_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS post_quality_runs (
    run_id INTEGER PRIMARY KEY,
    run_at TEXT NOT NULL,
    days INTEGER NOT NULL,
    sampled INTEGER NOT NULL,
    game_accuracy_rate REAL,
    avg_depth REAL,
    flagged_count INTEGER NOT NULL,
    detail_json TEXT
)
"""

_DEPTH_SYSTEM = (
    "You grade a Discord clan bot's post for DEPTH and quality (not correctness "
    "— assume the facts are right). Score 1-5: 5 = clearly looked at this "
    "specific member/moment and said something a clanmate would care about; "
    "1 = a generic template that could be about anyone. Reply ONLY compact JSON: "
    '{\"score\": <1-5>, \"reason\": \"<one short clause>\"}'
)


def _depth_judge(copy, intent_type):
    """Best-effort LLM depth score. Returns (score|None, reason). Never raises."""
    try:
        from agent.core import _create_chat_completion, response_text

        resp = _create_chat_completion(
            workflow="post_quality_eval",
            system=_DEPTH_SYSTEM,
            messages=[{"role": "user", "content": f"[{intent_type}] {copy}"}],
            temperature=0.2, max_tokens=120, timeout=30,
        )
        text = (response_text(resp) or "").strip()
        start, end = text.find("{"), text.rfind("}")
        obj = json.loads(text[start:end + 1])
        score = int(obj.get("score"))
        return (score if 1 <= score <= 5 else None), str(obj.get("reason", ""))[:120]
    except Exception as exc:  # headless / no key / parse — skip, never fail the run
        return None, f"(depth skipped: {type(exc).__name__})"


def _sample(conn, *, days, lane):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT ev.verdict, ev.final_copy, ev.at, ci.intent_type, ci.lane,
                  ci.payload_json, ci.discord_message_id
           FROM editor_verdicts ev
           JOIN communication_intents ci ON ci.intent_id = ev.intent_id
           WHERE ev.at >= ? AND ev.final_copy IS NOT NULL
           ORDER BY ev.at DESC""",
        (cutoff,),
    ).fetchall()
    out = []
    for r in rows:
        if lane and r["lane"] != lane:
            continue
        try:
            facts = json.loads(r["payload_json"] or "{}")
        except (TypeError, ValueError):
            facts = {}
        out.append({"copy": r["final_copy"], "intent_type": r["intent_type"],
                    "lane": r["lane"], "facts": facts, "verdict": r["verdict"],
                    "message_id": r["discord_message_id"], "at": r["at"]})
    return out


def run_eval(days: int = 3, lane: str | None = None, *, use_llm: bool = True) -> dict:
    """Grade recent posts; return a scorecard dict. Read-only except the
    post_quality_runs summary row."""
    conn = db.get_connection()
    try:
        conn.execute(_RUNS_DDL)
        posts = _sample(conn, days=days, lane=lane)
        by_lane: dict[str, dict] = {}
        flagged: list[dict] = []
        depths: list[int] = []
        game_ok = 0
        for p in posts:
            findings = game_check.check_post(p["copy"], p["facts"], conn)
            clean = not findings
            game_ok += 1 if clean else 0
            lane_stat = by_lane.setdefault(p["lane"], {"count": 0, "game_ok": 0, "depths": []})
            lane_stat["count"] += 1
            lane_stat["game_ok"] += 1 if clean else 0
            depth = reason = None
            if use_llm:
                depth, reason = _depth_judge(p["copy"], p["intent_type"])
                if depth is not None:
                    depths.append(depth)
                    lane_stat["depths"].append(depth)
            if findings or (depth is not None and depth <= 2):
                flagged.append({"message_id": p["message_id"], "lane": p["lane"],
                                "intent_type": p["intent_type"], "copy": p["copy"][:200],
                                "findings": findings,
                                "depth": depth, "depth_reason": reason})

        n = len(posts)
        result = {
            "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days": days, "sampled": n,
            "game_accuracy_rate": round(game_ok / n, 3) if n else None,
            "avg_depth": round(sum(depths) / len(depths), 2) if depths else None,
            "flagged_count": len(flagged),
            "by_lane": {
                ln: {"count": s["count"],
                     "game_accuracy": round(s["game_ok"] / s["count"], 3) if s["count"] else None,
                     "avg_depth": round(sum(s["depths"]) / len(s["depths"]), 2) if s["depths"] else None}
                for ln, s in sorted(by_lane.items())
            },
            "flagged": flagged,
        }
        conn.execute(
            "INSERT INTO post_quality_runs (run_at, days, sampled, game_accuracy_rate, "
            "avg_depth, flagged_count, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (result["run_at"], days, n, result["game_accuracy_rate"], result["avg_depth"],
             len(flagged), json.dumps(result, default=str)),
        )
        conn.commit()
        return result
    finally:
        conn.close()


def _print_scorecard(r):
    print(f"\n=== Post-quality scorecard — last {r['days']}d, {r['sampled']} posts "
          f"({r['run_at']}) ===")
    ga = r["game_accuracy_rate"]
    print(f"game accuracy: {ga*100:.0f}%" if ga is not None else "game accuracy: (no posts)")
    if r["avg_depth"] is not None:
        print(f"avg depth: {r['avg_depth']}/5")
    for ln, s in r["by_lane"].items():
        acc = f"{s['game_accuracy']*100:.0f}%" if s["game_accuracy"] is not None else "-"
        dep = f"{s['avg_depth']}/5" if s["avg_depth"] is not None else "-"
        print(f"  {ln:20} n={s['count']:<3} accuracy={acc:<5} depth={dep}")
    if r["flagged"]:
        print(f"\n  FLAGGED ({r['flagged_count']}):")
        for f in r["flagged"]:
            issue = "; ".join(x["issue"] for x in f["findings"]) if f["findings"] else \
                    f"thin (depth {f['depth']}: {f['depth_reason']})"
            print(f"   [{f['lane']}] {f['copy'][:70]!r}\n       → {issue}")
    else:
        print("\n  no flags — all sampled posts pass game-accuracy and depth.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--lane", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="game-check only (headless)")
    args = ap.parse_args()
    r = run_eval(args.days, args.lane, use_llm=not args.no_llm)
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        _print_scorecard(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
