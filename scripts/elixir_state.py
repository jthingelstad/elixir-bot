#!/usr/bin/env python3
"""Inspect Elixir's internal event/project/case/intent state."""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db  # noqa: E402


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
            or row.get("intent_type")
            or row.get("project_key")
            or row.get("case_key")
            or row.get("rollup_key")
            or row.get("event_key")
            or row.get("intent_key")
        )
        status = row.get("status") or row.get("scope") or row.get("workflow") or ""
        suffix = f" [{status}]" if status else ""
        timestamp = row.get("updated_at") or row.get("observed_at") or row.get("due_at") or ""
        when = f" - {timestamp}" if timestamp else ""
        lines.append(f"- {_short(label)}{suffix}{when}")
    return lines


def _summary_payload(args) -> dict:
    limit = args.limit
    return {
        "event_windows": db.summarize_events_by_window(windows=DEFAULT_WINDOWS, scope=args.scope),
        "recent_events": db.list_recent_events(days=args.days, scope=args.scope, limit=limit),
        "active_projects": db.list_projects(statuses=("active",), limit=limit),
        "active_war_project": db.get_active_war_season_project_snapshot(),
        "operating_projects": db.get_active_operating_project_snapshots(),
        "decision_cases": db.decision_case_snapshot(open_limit=limit, due_limit=limit),
        "recent_intents": db.list_recent_communication_intents(limit=limit),
        "failed_intents": db.list_recent_communication_intents(status="failed", limit=limit),
        "recent_rollups": db.list_event_rollups(scope=args.scope, limit=limit),
    }


def _print_summary(data: dict) -> None:
    print("Elixir State")
    print("")
    print("Event Windows")
    for key, window in data["event_windows"].items():
        top_types = ", ".join(
            f"{event_type}={count}"
            for event_type, count in list((window.get("by_type") or {}).items())[:5]
        )
        type_text = f" ({top_types})" if top_types else ""
        print(f"- {key}: {window.get('total', 0)} event(s){type_text}")
    print("")
    active_war = data.get("active_war_project") or {}
    print("Active War Project")
    if active_war:
        print(f"- {active_war.get('project_key')}: {_short(active_war.get('summary'))}")
    else:
        print("- none")
    print("")
    for line in _line_items(
        "Operating Projects",
        [
            project
            for project in (data.get("operating_projects") or {}).values()
            if project
        ],
        empty="none",
    ):
        print(line)
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
    for line in _line_items("Recent Communication Intents", data.get("recent_intents") or [], empty="none"):
        print(line)
    print("")
    for line in _line_items("Failed Communication Intents", data.get("failed_intents") or [], empty="none"):
        print(line)
    print("")
    for line in _line_items("Recent Event Rollups", data.get("recent_rollups") or [], empty="none"):
        print(line)


def _events_payload(args) -> dict:
    return {
        "event_windows": db.summarize_events_by_window(
            windows=DEFAULT_WINDOWS,
            scope=args.scope,
            subject_type=args.subject_type,
            subject_key=args.subject_key,
        ),
        "events": db.list_recent_events(
            days=args.days,
            scope=args.scope,
            event_type=args.event_type,
            subject_type=args.subject_type,
            subject_key=args.subject_key,
            limit=args.limit,
        ),
    }


def _projects_payload(args) -> dict:
    if args.project_key:
        return {
            "detail": db.get_project_detail(
                args.project_key,
                event_limit=args.limit,
                intent_limit=args.limit,
            )
        }
    statuses = None if args.status == "all" else (args.status or "active",)
    return {
        "projects": db.list_projects(
            project_type=args.project_type,
            statuses=statuses,
            limit=args.limit,
        )
    }


def _cases_payload(args) -> dict:
    if args.status == "due":
        return {"due": db.list_due_decision_cases(case_type=args.case_type, limit=args.limit)}
    if args.status and args.status != "all":
        return {
            "cases": db.list_decision_cases(
                statuses=(args.status,),
                case_type=args.case_type,
                limit=args.limit,
            )
        }
    return db.decision_case_snapshot(open_limit=args.limit, due_limit=args.limit)


def _intents_payload(args) -> dict:
    status = args.status if args.status and args.status != "all" else None
    return {
        "intents": db.list_recent_communication_intents(
            status=status,
            workflow=args.workflow,
            target_channel_key=args.target_channel_key,
            limit=args.limit,
        )
    }


def _rollups_payload(args) -> dict:
    scope = None if args.scope == "all" else args.scope
    return {
        "rollups": db.list_event_rollups(
            rollup_type=args.rollup_type,
            scope=scope,
            subject_type=args.subject_type,
            subject_key=args.subject_key,
            project_key=args.project_key,
            season_id=args.season_id,
            limit=args.limit,
        )
    }


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
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument("--limit", type=int, default=25, help="Maximum rows to return.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    summary = sub.add_parser("summary", help="Show event windows, active projects, cases, and intents.")
    _add_common(summary)
    summary.add_argument("--days", type=int, default=7)
    summary.add_argument("--scope", choices=("public", "leadership", "system_internal"), default=None)

    events = sub.add_parser("events", help="Show event windows and recent event rows.")
    _add_common(events)
    events.add_argument("--days", type=int, default=7)
    events.add_argument("--scope", choices=("public", "leadership", "system_internal"), default=None)
    events.add_argument("--event-type")
    events.add_argument("--subject-type")
    events.add_argument("--subject-key")

    projects = sub.add_parser("projects", help="List projects or show one project detail.")
    _add_common(projects)
    projects.add_argument("--project-type")
    projects.add_argument("--project-key")
    projects.add_argument("--status", default="active")

    cases = sub.add_parser("cases", help="Show open, due, or filtered decision cases.")
    _add_common(cases)
    cases.add_argument("--status", choices=("all", "due", "open", "deferred", "resolved", "dismissed"), default="all")
    cases.add_argument("--case-type")

    intents = sub.add_parser("intents", help="Show recent communication intents.")
    _add_common(intents)
    intents.add_argument("--status", default=None)
    intents.add_argument("--workflow")
    intents.add_argument("--target-channel-key")

    rollups = sub.add_parser("rollups", help="Show long-term event rollups.")
    _add_common(rollups)
    rollups.add_argument("--rollup-type", choices=("member_90d", "war_cycle", "project_summary", "case_history"))
    rollups.add_argument("--scope", choices=("all", "public", "leadership", "system_internal"), default="all")
    rollups.add_argument("--subject-type")
    rollups.add_argument("--subject-key")
    rollups.add_argument("--project-key")
    rollups.add_argument("--season-id")

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
    elif args.command == "projects":
        data = _projects_payload(args)
    elif args.command == "cases":
        data = _cases_payload(args)
    elif args.command == "intents":
        data = _intents_payload(args)
    elif args.command == "rollups":
        data = _rollups_payload(args)
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
