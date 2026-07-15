#!/usr/bin/env python3
"""Inspect Elixir's current event, awareness, war, and case state."""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db  # noqa: E402
from storage import events_read as event_facades  # noqa: E402

DEFAULT_WINDOWS = (7, 28, 56, 90)


def _json_dump(data) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def _short(value, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _line_items(title: str, rows: list[dict], *, empty: str) -> list[str]:
    lines = [title]
    if not rows:
        lines.append(f"- {empty}")
        return lines
    for row in rows:
        label = (
            row.get("title")
            or row.get("summary")
            or row.get("event_type")
            or (
                f"L{row['loop_number']}" if row.get("loop_number") is not None else None
            )
            or row.get("case_key")
            or row.get("rollup_key")
            or row.get("event_key")
        )
        status = row.get("status") or row.get("scope") or row.get("workflow") or ""
        suffix = f" [{status}]" if status else ""
        timestamp = (
            row.get("updated_at") or row.get("observed_at") or row.get("due_at") or ""
        )
        when = f" - {timestamp}" if timestamp else ""
        lines.append(f"- {_short(label)}{suffix}{when}")
    return lines


def _summary_payload(args) -> dict:
    limit = args.limit
    return {
        "event_windows": event_facades.summarize_event_windows(
            windows=DEFAULT_WINDOWS, scope=args.scope
        ),
        "recent_events": event_facades.list_recent_events(
            days=args.days, scope=args.scope, limit=limit
        ),
        "war_season": db.get_war_season_snapshot(),
        "decision_cases": db.decision_case_snapshot(open_limit=limit, due_limit=limit),
        "awareness": db.get_awareness_activity(limit=limit),
    }


def _print_summary(data: dict) -> None:
    print("Elixir State")
    print("")
    print("Event Windows")
    for key, window in data["event_windows"]["windows"].items():
        top_types = ", ".join(
            f"{event_type}={count}"
            for event_type, count in list((window.get("by_type") or {}).items())[:5]
        )
        type_text = f" ({top_types})" if top_types else ""
        print(
            f"- {key}: {window.get('total_events', 0)} emitted event(s), "
            f"{window.get('battles_mirrored', 0)} mirrored battle(s){type_text}"
        )
    print("")
    war_season = data.get("war_season") or {}
    print("War Season")
    if war_season:
        print(
            f"- season {war_season.get('season_id')}: {_short(war_season.get('summary'))}"
        )
    else:
        print("- none")
    print("")
    for line in _line_items(
        "Due Decision Cases",
        data.get("decision_cases", {}).get("due") or [],
        empty="none",
    ):
        print(line)
    print("")
    for line in _line_items(
        "Open Decision Cases",
        data.get("decision_cases", {}).get("open") or [],
        empty="none",
    ):
        print(line)
    print("")
    for line in _line_items(
        "Recent Awareness Decisions",
        (data.get("awareness") or {}).get("thoughts") or [],
        empty="none",
    ):
        print(line)
    print("")
    for line in _line_items(
        "Confirmed Awareness Posts",
        (data.get("awareness") or {}).get("posts") or [],
        empty="none",
    ):
        print(line)


def _events_payload(args) -> dict:
    return {
        "event_windows": event_facades.summarize_event_windows(
            windows=DEFAULT_WINDOWS,
            scope=args.scope,
            subject_key=args.subject_key,
        ),
        "events": event_facades.list_recent_events(
            days=args.days,
            scope=args.scope,
            event_type=args.event_type,
            subject_key=args.subject_key,
            limit=args.limit,
        ),
    }


def _war_payload(args) -> dict:
    return {"war_season": db.get_war_season_snapshot()}


def _cases_payload(args) -> dict:
    if args.status == "due":
        return {
            "due": db.list_due_decision_cases(
                case_type=args.case_type, limit=args.limit
            )
        }
    if args.status and args.status != "all":
        return {
            "cases": db.list_decision_cases(
                statuses=(args.status,),
                case_type=args.case_type,
                limit=args.limit,
            )
        }
    return db.decision_case_snapshot(open_limit=args.limit, due_limit=args.limit)


def _awareness_payload(args) -> dict:
    return db.get_awareness_activity(limit=args.limit)


def _print_generic(data: dict) -> None:
    for key, value in data.items():
        if isinstance(value, list):
            for line in _line_items(key.replace("_", " ").title(), value, empty="none"):
                print(line)
        elif isinstance(value, dict):
            print(key.replace("_", " ").title())
            _json_dump(value)
        else:
            print(f"{key}: {value}")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text."
    )
    parser.add_argument("--limit", type=int, default=25, help="Maximum rows to return.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    summary = sub.add_parser(
        "summary", help="Show events, war, cases, and awareness activity."
    )
    _add_common(summary)
    summary.add_argument("--days", type=int, default=7)
    summary.add_argument("--scope", choices=("public", "leadership"), default=None)

    events = sub.add_parser("events", help="Show event windows and recent event rows.")
    _add_common(events)
    events.add_argument("--days", type=int, default=7)
    events.add_argument("--scope", choices=("public", "leadership"), default=None)
    events.add_argument("--event-type")
    events.add_argument("--subject-key")

    war = sub.add_parser("war", help="Show the current war-season projection.")
    _add_common(war)

    cases = sub.add_parser("cases", help="Show open, due, or filtered decision cases.")
    _add_common(cases)
    cases.add_argument(
        "--status",
        choices=("all", "due", "open", "deferred", "resolved", "dismissed"),
        default="all",
    )
    cases.add_argument("--case-type")

    awareness = sub.add_parser(
        "awareness", help="Show current proactive decisions and posts."
    )
    _add_common(awareness)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        args = parser.parse_args(["summary"])

    if args.command == "summary":
        data = _summary_payload(args)
        if args.json:
            _json_dump(data)
        else:
            _print_summary(data)
        return 0
    if args.command == "events":
        data = _events_payload(args)
    elif args.command == "war":
        data = _war_payload(args)
    elif args.command == "cases":
        data = _cases_payload(args)
    elif args.command == "awareness":
        data = _awareness_payload(args)
    else:
        parser.error(f"unknown command: {args.command}")
        return 2

    if args.json:
        _json_dump(data)
    else:
        _print_generic(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
