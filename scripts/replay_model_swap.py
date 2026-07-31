#!/usr/bin/env python3
"""Head-to-head replay of a workflow's REAL captured prompts on a cheaper model.

A model downgrade justified by "this workflow looks mechanical" is a guess. Every
call Elixir makes is stored in ``llm_calls`` with its exact prompt and response, so
the same downgrade can be justified by replaying real traffic instead: send the
captured prompts to the candidate model and check the output against both the
workflow's response schema and the incumbent's answer.

This is the same evidence that demoted leader_action_feedback from Opus to Sonnet
on 2026-07-23; this script makes it repeatable rather than a one-off.

Reports, per prompt:
  * whether the candidate's JSON parses at all
  * whether every schema-required field is present and non-empty
  * length delta vs the incumbent (a collapse usually means a thinner answer)

Cost is a few cents: N prompts on the candidate model, nothing on the incumbent
(its answers are already stored).

Usage:
    python scripts/replay_model_swap.py --workflow leader_action_feedback \
        --candidate claude-haiku-4-5-20251001 --n 8
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import anthropic  # noqa: E402

import db  # noqa: E402
from agent.workflow_registry import get_workflow_spec  # noqa: E402


def _extract_json(text: str):
    """Sonnet/Haiku both fence JSON sometimes; the production parser strips it too."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--candidate", default="claude-haiku-4-5-20251001")
    ap.add_argument("--n", type=int, default=8)
    args = ap.parse_args()
    if not os.getenv("CLAUDE_API_KEY"):
        print("CLAUDE_API_KEY not set", file=sys.stderr)
        return 2

    try:
        required = (get_workflow_spec(args.workflow).response_schema or {}).get("required", [])
    except KeyError:
        required = []
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT model, prompt_json, response_json FROM llm_calls "
        "WHERE workflow = ? AND ok = 1 AND prompt_json IS NOT NULL "
        "ORDER BY recorded_at DESC LIMIT ?",
        (args.workflow, args.n),
    ).fetchall()
    conn.close()
    if not rows:
        print(f"no captured calls for {args.workflow}", file=sys.stderr)
        return 1

    client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"), timeout=120)
    print(f"replaying {len(rows)} real {args.workflow} prompts on {args.candidate}")
    print(f"schema requires: {required or '(no schema)'}\n")
    ok_parse = ok_schema = 0
    deltas = []
    for i, r in enumerate(rows, 1):
        p = json.loads(r["prompt_json"])
        incumbent = (json.loads(r["response_json"]) or {}).get("text") or ""
        try:
            resp = client.messages.create(
                model=args.candidate,
                max_tokens=p.get("max_tokens", 1200),
                temperature=p.get("temperature", 0.7),
                system=p.get("system", ""),
                messages=p.get("messages", []),
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        except Exception as exc:  # noqa: BLE001 - a replay records failures
            print(f"  [{i}] API ERROR {type(exc).__name__}: {exc}")
            continue
        parsed = _extract_json(text)
        missing = [f for f in required if not (parsed or {}).get(f)]
        ok_parse += parsed is not None
        ok_schema += parsed is not None and not missing
        d = len(text) - len(incumbent)
        deltas.append(d)
        status = "OK   " if parsed is not None and not missing else "FAIL "
        print(
            f"  [{i}] {status} parsed={parsed is not None!s:5} "
            f"missing={missing or '-'}  len {len(incumbent)}->{len(text)} ({d:+})"
        )
    n = len(rows)
    print(f"\n  parses:          {ok_parse}/{n}")
    print(f"  schema-complete: {ok_schema}/{n}")
    if deltas:
        print(
            f"  mean length delta vs incumbent ({rows[0]['model']}): {sum(deltas) / len(deltas):+.0f} chars"
        )
    print("\n  VERDICT:", "safe to swap" if ok_schema == n else "DO NOT SWAP — schema failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
