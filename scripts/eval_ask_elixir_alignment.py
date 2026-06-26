"""Evaluate recent #ask-elixir question/answer alignment.

This harness is read-only. It scores exact recent stored `messages` rows and
local router trace logs to catch answer/topic failures that are not prompt
failures: blank mention-only replies, stale-context dispatches, and obvious
question-domain mismatches.

Run with:
    python scripts/eval_ask_elixir_alignment.py --days 14
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db


ASK_ELIXIR_CHANNEL_ID = "1482368505058955467"
DEFAULT_LOG_PATHS = ("elixir-v5.log", "elixir.log")
LOG_TIMEZONE = ZoneInfo("America/Chicago")

_LOG_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")
_INTENT_RE = re.compile(
    r"intent_router mode=dispatch channel_id=(?P<channel_id>\d+) "
    r"author_id=(?P<author_id>\d+) workflow=(?P<workflow>\S+) "
    r"mentioned=(?P<mentioned>True|False) route=(?P<route>\S+)"
)
_ROUTE_RE = re.compile(
    r"message_route route=(?P<route>\S+) channel_id=(?P<channel_id>\d+) "
    r"author_id=(?P<author_id>\d+) mentioned=(?P<mentioned>True|False)"
)

DOMAIN_RULES = {
    "donations": {
        "question": ("donat", "donor", "donating", "donated"),
        "answer": ("donat", "donor", "donating", "donated"),
    },
    "war_champ": {
        "question": ("war champ",),
        "answer": ("war champ", "fame", "standings"),
    },
    "deck": {
        "question": ("deck", "cards in", "card levels"),
        "answer": ("deck", "card"),
    },
}


@dataclass(frozen=True)
class Thresholds:
    max_blank_user_reply_count: int = 0
    max_not_for_bot_route_count: int = 0
    max_blank_route_count: int = 0
    max_ignored_question_blank_route_count: int = 0
    max_topic_mismatch_count: int = 0
    followup_minutes: int = 5


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_log_time(value: str) -> datetime | None:
    try:
        return (
            datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=LOG_TIMEZONE)
            .astimezone(timezone.utc)
        )
    except ValueError:
        return None


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _open_db(path: str | os.PathLike[str]) -> sqlite3.Connection:
    raw = os.fspath(path)
    if raw.startswith("file:"):
        conn = sqlite3.connect(raw, uri=True)
    else:
        absolute = Path(raw).expanduser().resolve()
        conn = sqlite3.connect(f"file:{absolute}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _literal_field(line: str, field: str, *, before: str | None = None) -> Any:
    marker = f"{field}="
    start = line.index(marker) + len(marker)
    end = line.index(before, start) if before else len(line)
    return ast.literal_eval(line[start:end].strip())


def _load_messages(
    conn: sqlite3.Connection,
    *,
    since: datetime,
    end: datetime,
    channel_id: str,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "messages"):
        raise RuntimeError("messages table is missing")
    rows = conn.execute(
        """
        SELECT message_id, discord_message_id, thread_id, channel_id, discord_user_id,
               member_id, author_type, workflow, event_type, content, summary,
               created_at, intent_id
        FROM messages
        WHERE channel_id = ?
          AND created_at >= ?
          AND created_at <= ?
        ORDER BY thread_id ASC, created_at ASC, message_id ASC
        """,
        (channel_id, _format_time(since), _format_time(end)),
    ).fetchall()
    return [dict(row) for row in rows]


def _entry_from_match(path: str, line: str, match: re.Match[str], *, kind: str) -> dict[str, Any] | None:
    ts_match = _LOG_TS_RE.search(line)
    if not ts_match:
        return None
    timestamp = _parse_log_time(ts_match.group("ts"))
    if timestamp is None:
        return None
    try:
        raw_question = _literal_field(line, "raw_question", before=" original=" if kind == "message_route" else None)
    except (ValueError, SyntaxError):
        raw_question = None
    entry = {
        "kind": kind,
        "timestamp": timestamp,
        "timestamp_text": ts_match.group("ts"),
        "log_path": path,
        "channel_id": match.group("channel_id"),
        "author_id": match.group("author_id"),
        "route": match.group("route"),
        "mentioned": match.group("mentioned") == "True",
        "raw_question": raw_question,
        "line": line.strip(),
    }
    if kind == "message_route":
        try:
            entry["original"] = _literal_field(line, "original")
        except (ValueError, SyntaxError):
            entry["original"] = None
    return entry


def _load_log_entries(
    paths: list[str],
    *,
    since: datetime,
    end: datetime,
    channel_id: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _INTENT_RE.search(line)
                kind = "intent_router"
                if not match:
                    match = _ROUTE_RE.search(line)
                    kind = "message_route"
                if not match or match.group("channel_id") != channel_id:
                    continue
                entry = _entry_from_match(os.fspath(path), line, match, kind=kind)
                if not entry:
                    continue
                if since <= entry["timestamp"] <= end:
                    entries.append(entry)
    entries.sort(key=lambda item: item["timestamp"])
    return entries


def _minutes_between(a: datetime, b: datetime) -> float:
    return abs((b - a).total_seconds()) / 60


def _message_time(row: dict[str, Any]) -> datetime | None:
    return _parse_time(row.get("created_at"))


def _blank_user_replies(
    messages: list[dict[str, Any]],
    *,
    followup_minutes: int,
) -> list[dict[str, Any]]:
    by_thread: dict[Any, list[dict[str, Any]]] = {}
    for row in messages:
        by_thread.setdefault(row.get("thread_id"), []).append(row)
    findings: list[dict[str, Any]] = []
    for thread_rows in by_thread.values():
        for idx, row in enumerate(thread_rows):
            if row.get("author_type") != "user" or (row.get("content") or "").strip():
                continue
            row_time = _message_time(row)
            if row_time is None:
                continue
            for later in thread_rows[idx + 1:]:
                if later.get("author_type") != "assistant":
                    continue
                later_time = _message_time(later)
                if later_time is None:
                    continue
                if 0 <= (later_time - row_time).total_seconds() <= followup_minutes * 60:
                    findings.append({
                        "user_message_id": row.get("message_id"),
                        "user_discord_message_id": row.get("discord_message_id"),
                        "assistant_message_id": later.get("message_id"),
                        "assistant_discord_message_id": later.get("discord_message_id"),
                        "thread_id": row.get("thread_id"),
                        "workflow": later.get("workflow"),
                        "event_type": later.get("event_type"),
                        "created_at": row.get("created_at"),
                        "assistant_created_at": later.get("created_at"),
                        "assistant_preview": (later.get("content") or "")[:300],
                    })
                    break
    return findings


def _domain_for_question(text: str) -> str | None:
    lowered = text.lower()
    for domain, rules in DOMAIN_RULES.items():
        if any(token in lowered for token in rules["question"]):
            return domain
    return None


def _answer_matches_domain(answer: str, domain: str) -> bool:
    lowered = answer.lower()
    rules = DOMAIN_RULES[domain]
    return any(token in lowered for token in rules["answer"])


def _topic_mismatches(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_thread: dict[Any, list[dict[str, Any]]] = {}
    for row in messages:
        by_thread.setdefault(row.get("thread_id"), []).append(row)
    findings: list[dict[str, Any]] = []
    for thread_rows in by_thread.values():
        for idx, row in enumerate(thread_rows):
            if row.get("author_type") != "user":
                continue
            question = (row.get("content") or "").strip()
            if not question:
                continue
            domain = _domain_for_question(question)
            if not domain:
                continue
            for later in thread_rows[idx + 1:]:
                if later.get("author_type") != "assistant":
                    continue
                answer = later.get("content") or ""
                if not _answer_matches_domain(answer, domain):
                    findings.append({
                        "domain": domain,
                        "user_message_id": row.get("message_id"),
                        "user_discord_message_id": row.get("discord_message_id"),
                        "assistant_message_id": later.get("message_id"),
                        "assistant_discord_message_id": later.get("discord_message_id"),
                        "thread_id": row.get("thread_id"),
                        "question": question,
                        "assistant_preview": answer[:300],
                    })
                break
    return findings


def _blank_routes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if entry["kind"] == "message_route"
        and not str(entry.get("raw_question") or "").strip()
    ]


def _not_for_bot_routes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if entry["kind"] == "intent_router"
        and entry["route"] == "not_for_bot"
    ]


def _ignored_question_blank_routes(
    entries: list[dict[str, Any]],
    *,
    followup_minutes: int,
) -> list[dict[str, Any]]:
    intents = [
        entry
        for entry in _not_for_bot_routes(entries)
        if str(entry.get("raw_question") or "").strip()
    ]
    routes = _blank_routes(entries)
    findings: list[dict[str, Any]] = []
    for intent in intents:
        for route in routes:
            if route["channel_id"] != intent["channel_id"] or route["author_id"] != intent["author_id"]:
                continue
            delta = (route["timestamp"] - intent["timestamp"]).total_seconds()
            if 0 < delta <= followup_minutes * 60:
                findings.append({
                    "ignored_at": intent["timestamp_text"],
                    "ignored_question": intent.get("raw_question"),
                    "ignored_route": intent.get("route"),
                    "blank_route_at": route["timestamp_text"],
                    "blank_route": route.get("route"),
                    "blank_original": route.get("original"),
                    "author_id": intent["author_id"],
                    "channel_id": intent["channel_id"],
                    "minutes_between": round(_minutes_between(route["timestamp"], intent["timestamp"]), 2),
                })
                break
    return findings


def _metric(value: Any, threshold: dict[str, Any], passed: bool, definition: str) -> dict[str, Any]:
    return {
        "value": value,
        "threshold": threshold,
        "passed": bool(passed),
        "definition": definition,
    }


def evaluate(
    db_path: str | os.PathLike[str],
    *,
    since: datetime,
    end: datetime,
    log_paths: list[str] | None = None,
    channel_id: str = ASK_ELIXIR_CHANNEL_ID,
    thresholds: Thresholds = Thresholds(),
) -> dict[str, Any]:
    conn = _open_db(db_path)
    try:
        messages = _load_messages(conn, since=since, end=end, channel_id=channel_id)
    finally:
        conn.close()

    entries = _load_log_entries(
        list(log_paths or DEFAULT_LOG_PATHS),
        since=since,
        end=end,
        channel_id=channel_id,
    )
    blank_user_replies = _blank_user_replies(
        messages,
        followup_minutes=thresholds.followup_minutes,
    )
    not_for_bot_routes = _not_for_bot_routes(entries)
    blank_routes = _blank_routes(entries)
    ignored_then_blank = _ignored_question_blank_routes(
        entries,
        followup_minutes=thresholds.followup_minutes,
    )
    topic_mismatches = _topic_mismatches(messages)

    metrics = {
        "blank_user_reply_count": _metric(
            len(blank_user_replies),
            {"<=": thresholds.max_blank_user_reply_count},
            len(blank_user_replies) <= thresholds.max_blank_user_reply_count,
            "Stored blank/empty #ask-elixir user messages followed by an assistant reply in the same thread within followup_minutes.",
        ),
        "not_for_bot_route_count": _metric(
            len(not_for_bot_routes),
            {"<=": thresholds.max_not_for_bot_route_count},
            len(not_for_bot_routes) <= thresholds.max_not_for_bot_route_count,
            "#ask-elixir intent_router traces classified as not_for_bot. This open lane is defined as addressed to Elixir.",
        ),
        "blank_route_count": _metric(
            len(blank_routes),
            {"<=": thresholds.max_blank_route_count},
            len(blank_routes) <= thresholds.max_blank_route_count,
            "Router message_route traces in #ask-elixir with an empty stripped raw_question.",
        ),
        "ignored_question_blank_route_count": _metric(
            len(ignored_then_blank),
            {"<=": thresholds.max_ignored_question_blank_route_count},
            len(ignored_then_blank) <= thresholds.max_ignored_question_blank_route_count,
            "A nonempty #ask-elixir question classified not_for_bot followed by an empty routed reply from the same author/channel.",
        ),
        "topic_mismatch_count": _metric(
            len(topic_mismatches),
            {"<=": thresholds.max_topic_mismatch_count},
            len(topic_mismatches) <= thresholds.max_topic_mismatch_count,
            "Obvious adjacent user/assistant domain mismatches, such as a donation question answered without donation content.",
        ),
    }
    return {
        "harness": "eval_ask_elixir_alignment",
        "window": {
            "since": _format_time(since),
            "end": _format_time(end),
        },
        "channel_id": channel_id,
        "thresholds": thresholds.__dict__,
        "passed": all(metric["passed"] for metric in metrics.values()),
        "metrics": metrics,
        "counts": {
            "messages": len(messages),
            "log_entries": len(entries),
        },
        "findings": {
            "blank_user_replies": blank_user_replies,
            "blank_routes": [
                {
                    "timestamp": entry["timestamp_text"],
                    "route": entry["route"],
                    "author_id": entry["author_id"],
                    "raw_question": entry.get("raw_question"),
                    "original": entry.get("original"),
                    "log_path": entry["log_path"],
                }
                for entry in blank_routes
            ],
            "not_for_bot_routes": [
                {
                    "timestamp": entry["timestamp_text"],
                    "route": entry["route"],
                    "author_id": entry["author_id"],
                    "raw_question": entry.get("raw_question"),
                    "log_path": entry["log_path"],
                }
                for entry in not_for_bot_routes
            ],
            "ignored_question_blank_routes": ignored_then_blank,
            "topic_mismatches": topic_mismatches,
        },
    }


def _resolve_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end = _parse_time(args.end) or datetime.now(timezone.utc)
    if args.since:
        since = _parse_time(args.since)
        if since is None:
            raise SystemExit(f"invalid --since timestamp: {args.since}")
    else:
        since = end - timedelta(days=max(1, int(args.days)))
    return since, end


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.fspath(db.DB_PATH), help="SQLite DB path or file: URI")
    parser.add_argument("--days", type=int, default=14, help="Lookback window when --since is omitted")
    parser.add_argument("--since", help="Inclusive UTC start time, e.g. 2026-06-18T00:00:00Z")
    parser.add_argument("--end", help="Inclusive UTC end time; defaults to now")
    parser.add_argument("--log", action="append", dest="logs", help="Log path to scan; repeatable")
    parser.add_argument("--out", default="scripts/ask_elixir_alignment_eval_results.json")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when thresholds fail")
    args = parser.parse_args()

    since, end = _resolve_window(args)
    result = evaluate(args.db, since=since, end=end, log_paths=args.logs)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    print("Ask-Elixir alignment eval")
    print(f"  window: {result['window']['since']} -> {result['window']['end']}")
    print(f"  messages: {result['counts']['messages']}")
    print(f"  log entries: {result['counts']['log_entries']}")
    for name, metric in result["metrics"].items():
        status = "PASS" if metric["passed"] else "FAIL"
        print(f"  {status} {name}: {metric['value']} threshold={metric['threshold']}")
    print(f"  overall: {'PASS' if result['passed'] else 'FAIL'}")
    print(f"  results: {out_path}")

    if args.strict and not result["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
