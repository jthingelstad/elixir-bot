#!/usr/bin/env python3
"""Retrospective post-quality eval over Elixir's ACTIVE output paths.

Samples delivered awareness posts plus stored assistant messages and grades
each post three ways:
  (a) deterministic GAME-KNOWLEDGE accuracy — engine.game_check (no LLM);
  (b) an LLM DEPTH judge — did it look at the subject, voice, member-value
      (best-effort; skipped headless if no API so the harness always runs).
  (c) deterministic near-duplicate detection within the same lane.

Emits a per-lane scorecard + a flagged list (post + the specific bad/thin
claim), stores idempotent editorial lessons that awareness reads on its next
tick, records a summary row in post_quality_runs (trend), and exposes
`run_eval(days, lane=None) -> dict` for the Phase-5 confidence report.

    python scripts/eval_post_quality.py --days 3
    python scripts/eval_post_quality.py --days 7 --json
    python scripts/eval_post_quality.py --lane battle-feed --no-llm
"""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import db  # noqa: E402
from engine import game_check  # noqa: E402
from capabilities.game_truth import awareness_post_facts  # noqa: E402

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


def _loads(value, default):
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(default)) else default
    except (TypeError, ValueError):
        return default


def _post_text(post: dict) -> str:
    value = post.get("content")
    if isinstance(value, list):
        return "\n\n".join(str(item) for item in value if item is not None).strip()
    return str(value or "").strip()


def _plan_post(plan: dict, covers: list, preview: str) -> dict:
    posts = [post for post in (plan.get("posts") or []) if isinstance(post, dict)]
    if covers:
        for post in posts:
            if list(post.get("covers_signal_keys") or []) == list(covers):
                return post
    normalized = " ".join((preview or "").split())
    for post in posts:
        if " ".join(_post_text(post).split()).startswith(normalized[:120]):
            return post
    return {}


def _parse_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _historical_thought_match(thoughts: list[dict], posted_at, covers, preview):
    """Recover the pre-link era's thought by copy/evidence + delivery time."""
    posted = _parse_time(posted_at)
    if posted is None:
        return {}, {}
    best = None
    for thought in thoughts:
        at = _parse_time(thought.get("at"))
        if at is None or abs((at - posted).total_seconds()) > 15 * 60:
            continue
        post = _plan_post(thought["plan"], covers, preview)
        if not post:
            continue
        distance = abs((at - posted).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, thought["read"], post)
    return (best[1], best[2]) if best else ({}, {})


def _sample(conn, *, days, lane):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    awareness_rows = conn.execute(
        """SELECT ap.post_id, ap.lane, ap.content_preview, ap.covers_json,
                  ap.posted_at, ap.discord_message_id, at.read_json, at.plan_json
           FROM awareness_posts ap
           LEFT JOIN awareness_thoughts at ON at.loop_number = ap.loop_number
           WHERE datetime(ap.posted_at) >= datetime(?)
           ORDER BY datetime(ap.posted_at) DESC, ap.post_id DESC""",
        (cutoff,),
    ).fetchall()
    thought_rows = conn.execute(
        "SELECT loop_number, at, read_json, plan_json FROM awareness_thoughts "
        "WHERE datetime(at) >= datetime(?, '-15 minutes') ORDER BY datetime(at)",
        (cutoff,),
    ).fetchall()
    thoughts = [
        {
            "loop_number": row["loop_number"],
            "at": row["at"],
            "read": _loads(row["read_json"], {}),
            "plan": _loads(row["plan_json"], {}),
        }
        for row in thought_rows
    ]
    out = []
    awareness_ids: set[str] = set()
    for r in awareness_rows:
        if lane and r["lane"] != lane:
            continue
        covers = _loads(r["covers_json"], [])
        read = _loads(r["read_json"], {})
        plan = _loads(r["plan_json"], {})
        post = _plan_post(plan, covers, r["content_preview"])
        if not post:
            read, post = _historical_thought_match(
                thoughts, r["posted_at"], covers, r["content_preview"]
            )
        facts = awareness_post_facts(read, post) if post else {}
        message_id = str(r["discord_message_id"] or f"awareness:{r['post_id']}")
        awareness_ids.add(str(r["discord_message_id"] or ""))
        out.append({
            "copy": r["content_preview"],
            "intent_type": f"awareness:{post.get('leads_with') or 'post'}",
            "lane": r["lane"],
            "facts": facts,
            "message_id": message_id,
            "at": r["posted_at"],
            "source": "awareness",
            "member_tags": post.get("member_tags") or [],
        })

    message_rows = conn.execute(
        """SELECT discord_message_id, workflow, event_type, channel_id, content, created_at
           FROM messages
           WHERE author_type = 'assistant' AND discord_message_id IS NOT NULL
             AND TRIM(content) <> '' AND datetime(created_at) >= datetime(?)
           ORDER BY datetime(created_at) DESC, message_id DESC""",
        (cutoff,),
    ).fetchall()
    for r in message_rows:
        message_id = str(r["discord_message_id"])
        if message_id in awareness_ids:
            continue
        output_lane = str(r["workflow"] or r["channel_id"] or "assistant")
        if lane and output_lane != lane:
            continue
        out.append({
            "copy": r["content"],
            "intent_type": r["event_type"] or r["workflow"] or "assistant_message",
            "lane": output_lane,
            "facts": {},
            "message_id": message_id,
            "at": r["created_at"],
            "source": "messages",
            "member_tags": [],
        })
    return out


def _normalized(value: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in (value or "").lower()).split())


def _repetition_findings(posts: list[dict]) -> dict[str, dict]:
    findings: dict[str, dict] = {}
    history: dict[str, list[dict]] = {}
    for post in sorted(posts, key=lambda item: item.get("at") or ""):
        current = _normalized(post.get("copy") or "")
        if len(current) >= 60:
            best = None
            for prior in history.get(post["lane"], [])[-20:]:
                other = _normalized(prior.get("copy") or "")
                if len(other) < 60:
                    continue
                ratio = SequenceMatcher(None, current, other).ratio()
                if ratio >= 0.82 and (best is None or ratio > best["similarity"]):
                    best = {
                        "message_id": prior.get("message_id"),
                        "at": prior.get("at"),
                        "similarity": round(ratio, 3),
                    }
            if best:
                findings[str(post["message_id"])] = best
        history.setdefault(post["lane"], []).append(post)
    return findings


def run_eval(
    days: int = 3,
    lane: str | None = None,
    *,
    use_llm: bool = True,
    record_feedback: bool = True,
) -> dict:
    """Grade recent posts and optionally persist idempotent editorial lessons."""
    conn = db.get_connection()
    try:
        conn.execute(_RUNS_DDL)
        posts = _sample(conn, days=days, lane=lane)
        repetitions = _repetition_findings(posts)
        by_lane: dict[str, dict] = {}
        flagged: list[dict] = []
        depths: list[int] = []
        game_ok = 0
        feedback_written = 0
        for p in posts:
            findings = game_check.check_post(p["copy"], p["facts"], conn)
            repetition = repetitions.get(str(p["message_id"]))
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
            if findings or (depth is not None and depth <= 2) or repetition:
                flagged.append({"message_id": p["message_id"], "lane": p["lane"],
                                "source": p["source"],
                                "intent_type": p["intent_type"], "copy": p["copy"][:200],
                                "findings": findings,
                                "depth": depth, "depth_reason": reason,
                                "repetition": repetition})
                if record_feedback:
                    from engine.editor import record_post_quality_feedback

                    mid = record_post_quality_feedback(
                        conn,
                        message_id=str(p["message_id"]),
                        lane=p["lane"],
                        source=p["source"],
                        copy=p["copy"],
                        findings=findings,
                        depth=depth,
                        depth_reason=reason,
                        repetition=repetition,
                    )
                    feedback_written += 1 if mid is not None else 0

        n = len(posts)
        result = {
            "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days": days, "sampled": n,
            "game_accuracy_rate": round(game_ok / n, 3) if n else None,
            "avg_depth": round(sum(depths) / len(depths), 2) if depths else None,
            "flagged_count": len(flagged),
            "feedback_memories_written": feedback_written,
            "by_source": {
                source: sum(1 for post in posts if post["source"] == source)
                for source in sorted({post["source"] for post in posts})
            },
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
            reasons = [x["issue"] for x in f["findings"]]
            if f.get("depth") is not None and f["depth"] <= 2:
                reasons.append(f"thin (depth {f['depth']}: {f['depth_reason']})")
            if f.get("repetition"):
                reasons.append(
                    f"repeats {f['repetition'].get('message_id')} "
                    f"({f['repetition'].get('similarity', 0):.0%} similar)"
                )
            issue = "; ".join(reasons) or "quality finding"
            print(f"   [{f['lane']}] {f['copy'][:70]!r}\n       → {issue}")
    else:
        print("\n  no flags — all sampled posts pass game-accuracy and depth.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--lane", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="game-check only (headless)")
    ap.add_argument("--no-feedback", action="store_true", help="do not write editorial memories")
    args = ap.parse_args()
    r = run_eval(
        args.days,
        args.lane,
        use_llm=not args.no_llm,
        record_feedback=not args.no_feedback,
    )
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        _print_scorecard(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
