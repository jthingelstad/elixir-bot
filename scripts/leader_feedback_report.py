#!/usr/bin/env python3
"""How leadership actually responded to Elixir's recommendations.

Read-only evidence for the weekly improvement review. This is the signal
`review_improvement_opportunities.py` used to bury inside a scored,
SQLite-backed suggestion queue that nothing drained (#209): Elixir graded its
own homework in-product, and the resulting rows sat unread on an Observatory
panel for weeks.

Now the analysis is just a report. An AGENT-TEAM role runs it inside Codex,
reads the evidence, and files GitHub issues under the normal WORKFLOW rules —
GitHub is the queue, so there is no second store to keep in sync and nothing
that can silently accumulate.

Deliberately NOT here:
  * prompt failures — already covered by scripts/review_agent_feedback.py
  * confidence / severity scores — a human-or-agent judgment, not arithmetic
    over a 30-day window
  * any write. This script opens the DB read-only and never calls `gh`.

Usage:
    uv run --locked python scripts/leader_feedback_report.py
    uv run --locked python scripts/leader_feedback_report.py --days 14 --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DAYS = 7
_TRUNCATE = 220


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _truncate(value, limit: int = _TRUNCATE) -> str:
    text = _clean(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _loads_dict(value) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except TypeError, ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _connect() -> sqlite3.Connection:
    """Open the live DB READ-ONLY. This report must never be able to mutate
    production, and must never trigger a schema migration as a side effect."""
    import os

    from dotenv import load_dotenv

    load_dotenv()  # standalone script: ELIXIR_DB_PATH comes from .env
    path = os.getenv("ELIXIR_DB_PATH", str(ROOT / "elixir-v51.db"))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect(conn, *, days: int) -> dict:
    cutoff = _cutoff(days)
    rows = conn.execute(
        """
        SELECT action_id, action_type, status, target_player_name, decision_note,
               rationale, prompt_text, copy_edit_diff_json,
               decided_by_discord_user_id,
               COALESCE(decision_note_at, decided_at, updated_at, proposed_at) AS at
          FROM leader_action_recommendations
         WHERE COALESCE(is_test, 0) = 0
           AND COALESCE(decision_note_at, decided_at, updated_at, proposed_at) >= ?
         ORDER BY at DESC, action_id DESC
        """,
        (cutoff,),
    ).fetchall()

    by_type: Counter = Counter()
    by_status: Counter = Counter()
    outcomes: dict[str, Counter] = {}
    notes: list[dict] = []
    edits: list[dict] = []

    system_withdrawn = 0
    for row in rows:
        atype = row["action_type"] or "unknown"
        status = row["status"] or "unknown"
        by_type[atype] += 1
        by_status[status] += 1

        # An auto-withdrawal is the ENGINE retracting its own card, not the leader
        # declining it. Counting those as declines understates Elixir's accuracy
        # and puts words in leadership's mouth: R214/R215 (2026-07-27) read as
        # "promotion declined" when the engine withdrew them itself.
        by_system = str(row["decided_by_discord_user_id"] or "").startswith("system:")
        if by_system:
            system_withdrawn += 1
            continue
        outcomes.setdefault(atype, Counter())[status] += 1

        note = _clean(row["decision_note"])
        if note:
            notes.append(
                {
                    "action_id": row["action_id"],
                    "action_type": atype,
                    "status": status,
                    "member": row["target_player_name"],
                    "note": _truncate(note),
                    "at": row["at"],
                }
            )

        diff = _loads_dict(row["copy_edit_diff_json"])
        if diff.get("changed"):
            edits.append(
                {
                    "action_id": row["action_id"],
                    "action_type": atype,
                    "member": row["target_player_name"],
                    "similarity": diff.get("similarity"),
                    "at": row["at"],
                }
            )

    # Acceptance rate per action type — the headline quality number. A type the
    # leader routinely declines or rewrites is the engine being wrong or the copy
    # being wrong, and which one it is shows up in the notes and edits below.
    acceptance = {}
    for atype, counts in outcomes.items():
        decided = counts.get("done", 0) + counts.get("rejected", 0)
        if decided:
            acceptance[atype] = {
                "done": counts.get("done", 0),
                "rejected": counts.get("rejected", 0),
                "rate": round(counts.get("done", 0) / decided, 3),
            }

    return {
        "window_days": days,
        "since": cutoff,
        "cards": len(rows),
        "by_action_type": dict(by_type.most_common()),
        "by_status": dict(by_status.most_common()),
        "acceptance_by_type": acceptance,
        "system_withdrawn": system_withdrawn,
        "decision_notes": notes,
        "copy_edits": edits,
        "open_undecided": by_status.get("proposed", 0),
    }


def render(report: dict) -> str:
    out = [
        f"=== LEADER FEEDBACK · last {report['window_days']}d (since {report['since']}) ===",
        f"cards touched: {report['cards']}   awaiting a decision: {report['open_undecided']}"
        f"   engine auto-withdrew: {report['system_withdrawn']} (excluded — not leadership)",
        "",
    ]
    if report["acceptance_by_type"]:
        out.append("acceptance by action type (leader-decided cards only):")
        for atype, a in sorted(report["acceptance_by_type"].items(), key=lambda kv: kv[1]["rate"]):
            out.append(
                f"  {atype:28} {a['rate']:.0%}  ({a['done']} done / {a['rejected']} declined)"
            )
        out.append("")
    else:
        out.append("no cards were decided in this window.\n")

    if report["decision_notes"]:
        out.append(
            f"decision notes ({len(report['decision_notes'])}) — leadership in their own words:"
        )
        for n in report["decision_notes"]:
            who = f" [{n['member']}]" if n["member"] else ""
            out.append(f"  R{n['action_id']} {n['action_type']}/{n['status']}{who}")
            out.append(f"      {n['note']}")
        out.append("")

    if report["copy_edits"]:
        out.append(f"copy edits ({len(report['copy_edits'])}) — Elixir's wording was rewritten:")
        for e in report["copy_edits"]:
            sim = f"similarity {e['similarity']}" if e.get("similarity") is not None else "changed"
            who = f" [{e['member']}]" if e["member"] else ""
            out.append(f"  R{e['action_id']} {e['action_type']}{who} — {sim}")
        out.append("")

    if not report["decision_notes"] and not report["copy_edits"]:
        out.append("no notes and no copy edits — leadership accepted Elixir's wording as written.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help=f"window (default {DEFAULT_DAYS})"
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    args = ap.parse_args()

    conn = _connect()
    try:
        report = collect(conn, days=args.days)
    finally:
        conn.close()

    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
