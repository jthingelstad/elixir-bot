#!/usr/bin/env python3
"""Print recent prompt failures and ask-elixir feedback for review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db


def _print_review_item(row: dict, *, include_raw: bool) -> None:
    row_kind = row.get("kind") or "failure"
    row_id = row.get("failure_id") or row.get("feedback_id") or "-"
    print(
        f"[{row_kind}:{row_id}] {row['recorded_at']} "
        f"{row.get('workflow') or 'unknown'} {row['failure_type']}/{row['failure_stage']}"
    )
    print(f"question: {row.get('question') or '-'}")
    if row.get("channel_name") or row.get("channel_id"):
        print(
            f"channel: {row.get('channel_name') or '-'} ({row.get('channel_id') or '-'}) "
            f"user: {row.get('discord_user_id') or '-'} msg: {row.get('discord_message_id') or '-'}"
        )
    if row.get("feedback_value"):
        print(f"feedback: {row['feedback_value']}")
    if row.get("detail"):
        print(f"detail: {row['detail']}")
    if row.get("openai_last_model") or row.get("openai_last_error"):
        print(
            f"openai: model={row.get('openai_last_model') or '-'} "
            f"at={row.get('openai_last_call_at') or '-'} "
            f"error={row.get('openai_last_error') or '-'}"
        )
    if row.get("result_preview"):
        print(f"preview: {row['result_preview']}")
    if include_raw and row.get("raw_json"):
        print("raw_json:")
        print(row["raw_json"])
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Review recent Elixir agent failures and ask-elixir feedback.")
    parser.add_argument("--limit", type=int, default=20, help="Number of review items to show.")
    parser.add_argument("--workflow", help="Filter to a workflow like clanops or interactive.")
    parser.add_argument("--json", action="store_true", help="Emit JSON for copy/paste into another model.")
    parser.add_argument("--raw", action="store_true", help="Include stored raw_json in text output.")
    parser.add_argument(
        "--include-positive",
        action="store_true",
        help="Include active thumbs-up feedback in addition to failures and thumbs-down feedback.",
    )
    args = parser.parse_args()

    rows = db.list_prompt_review_items(
        limit=args.limit,
        workflow=args.workflow,
        include_positive=args.include_positive,
    )
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("No agent feedback items recorded.")
        return
    for row in rows:
        _print_review_item(row, include_raw=args.raw)


if __name__ == "__main__":
    main()
