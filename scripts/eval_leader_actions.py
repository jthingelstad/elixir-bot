"""Evaluate leader-action recommendations from exact stored artifacts.

This harness is read-only. It scores recent rows from
leader_action_recommendations and enriches them with stored Discord message
artifacts from messages when available.

Run with:
    python scripts/eval_leader_actions.py --days 14
    python scripts/eval_leader_actions.py --since 2026-06-18T00:00:00Z
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db


TERMINAL_STATUSES = {"done", "deferred", "rejected"}
RECOMMENDATION_TYPES = {
    "demotion_recommendation",
    "kick_recommendation",
    "promotion_recommendation",
}
RELAY_TYPES = {
    "celebration_relay",
    "discord_invite_relay",
    "in_game_relay",
    "welcome_relay",
}


@dataclass(frozen=True)
class Thresholds:
    min_decision_rate: float = 0.95
    min_trace_rate: float = 0.95
    min_relay_copy_text_rate: float = 0.95
    max_stale_open_count: int = 0
    max_recommendation_unaccepted_rate: float = 0.80
    stale_hours: int = 24


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


def _json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _copy_message_ids(row: sqlite3.Row | dict[str, Any]) -> list[str]:
    ids: list[str] = []
    primary = row["copy_message_id"] if "copy_message_id" in row.keys() else None
    if primary:
        ids.append(str(primary))
    extra = _json_loads(
        row["copy_message_ids_json"] if "copy_message_ids_json" in row.keys() else None,
        [],
    )
    if isinstance(extra, list):
        ids.extend(str(item) for item in extra if item)
    return list(dict.fromkeys(ids))


def _load_actions(conn: sqlite3.Connection, *, since: datetime, end: datetime) -> list[sqlite3.Row]:
    if not _table_exists(conn, "leader_action_recommendations"):
        raise RuntimeError("leader_action_recommendations table is missing")
    return list(
        conn.execute(
            """
            SELECT *
            FROM leader_action_recommendations
            WHERE COALESCE(is_test, 0) = 0
              AND proposed_at >= ?
              AND proposed_at <= ?
            ORDER BY proposed_at ASC, action_id ASC
            """,
            (_format_time(since), _format_time(end)),
        ).fetchall()
    )


def _load_messages(conn: sqlite3.Connection, ids: set[str]) -> dict[str, dict[str, Any]]:
    if not ids or not _table_exists(conn, "messages"):
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT discord_message_id, channel_id, author_type, workflow, event_type,
               content, summary, created_at, raw_json, intent_id
        FROM messages
        WHERE discord_message_id IN ({placeholders})
        """,
        tuple(sorted(ids)),
    ).fetchall()
    return {str(row["discord_message_id"]): dict(row) for row in rows}


def _action_artifact(row: sqlite3.Row, messages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    source_id = str(row["source_message_id"]) if row["source_message_id"] else None
    copy_ids = _copy_message_ids(row)
    return {
        "action_id": row["action_id"],
        "action_key": row["action_key"],
        "action_type": row["action_type"],
        "objective": row["objective"],
        "status": row["status"],
        "target_channel_key": row["target_channel_key"],
        "target_channel_id": row["target_channel_id"],
        "target_player_tag": row["target_player_tag"],
        "target_player_name": row["target_player_name"],
        "source_signal_key": row["source_signal_key"],
        "source_signal_type": row["source_signal_type"],
        "source_message_id": source_id,
        "copy_message_id": str(row["copy_message_id"]) if row["copy_message_id"] else None,
        "copy_message_ids": copy_ids,
        "prompt_text": row["prompt_text"],
        "rationale": row["rationale"],
        "baseline": _json_loads(row["baseline_json"], {}),
        "outcome": _json_loads(row["outcome_json"], {}),
        "proposed_at": row["proposed_at"],
        "expires_at": row["expires_at"],
        "decided_at": row["decided_at"],
        "decision_emoji": row["decision_emoji"],
        "decision_note": row["decision_note"],
        "decision_note_at": row["decision_note_at"],
        "copy_original_text": row["copy_original_text"],
        "copy_current_text": row["copy_current_text"],
        "copy_edited_at": row["copy_edited_at"],
        "ui_version": row["ui_version"],
        "case_id": row["case_id"],
        "source_message": messages.get(source_id) if source_id else None,
        "copy_messages": [messages[item] for item in copy_ids if item in messages],
    }


def _metric(value: Any, threshold: dict[str, Any], passed: bool, definition: str) -> dict[str, Any]:
    return {
        "value": value,
        "threshold": threshold,
        "passed": bool(passed),
        "definition": definition,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _score(actions: list[sqlite3.Row], *, now: datetime, thresholds: Thresholds) -> dict[str, Any]:
    total = len(actions)
    terminal = [row for row in actions if row["status"] in TERMINAL_STATUSES]
    traceable = [
        row
        for row in actions
        if row["source_message_id"] or row["copy_message_id"] or _copy_message_ids(row)
    ]
    relays = [row for row in actions if row["action_type"] in RELAY_TYPES]
    relays_with_copy = [
        row
        for row in relays
        if (row["copy_current_text"] or row["copy_original_text"] or row["prompt_text"] or "").strip()
    ]
    recommendation_rows = [row for row in actions if row["action_type"] in RECOMMENDATION_TYPES]
    unaccepted_recommendations = [
        row for row in recommendation_rows if row["status"] in {"deferred", "rejected"}
    ]

    stale_cutoff = now - timedelta(hours=thresholds.stale_hours)
    stale_open = []
    for row in actions:
        if row["status"] != "proposed":
            continue
        proposed_at = _parse_time(row["proposed_at"])
        expires_at = _parse_time(row["expires_at"])
        if proposed_at and proposed_at <= stale_cutoff and (expires_at is None or expires_at > now):
            stale_open.append(row)

    decision_rate = _rate(len(terminal), total)
    trace_rate = _rate(len(traceable), total)
    relay_copy_text_rate = _rate(len(relays_with_copy), len(relays))
    recommendation_unaccepted_rate = _rate(len(unaccepted_recommendations), len(recommendation_rows))

    metrics = {
        "decision_rate": _metric(
            decision_rate,
            {">=": thresholds.min_decision_rate},
            decision_rate is None or decision_rate >= thresholds.min_decision_rate,
            "Terminal leader actions (done/deferred/rejected) divided by all non-test actions in the window.",
        ),
        "trace_rate": _metric(
            trace_rate,
            {">=": thresholds.min_trace_rate},
            trace_rate is None or trace_rate >= thresholds.min_trace_rate,
            "Actions with at least one stored source/copy Discord message id divided by all non-test actions.",
        ),
        "relay_copy_text_rate": _metric(
            relay_copy_text_rate,
            {">=": thresholds.min_relay_copy_text_rate},
            relay_copy_text_rate is None
            or relay_copy_text_rate >= thresholds.min_relay_copy_text_rate,
            "Relay actions with stored copy text divided by all relay actions in the window.",
        ),
        "stale_open_count": _metric(
            len(stale_open),
            {"<=": thresholds.max_stale_open_count, "stale_hours": thresholds.stale_hours},
            len(stale_open) <= thresholds.max_stale_open_count,
            "Open proposed cards older than stale_hours and not expired.",
        ),
        "recommendation_unaccepted_rate": _metric(
            recommendation_unaccepted_rate,
            {"<=": thresholds.max_recommendation_unaccepted_rate},
            recommendation_unaccepted_rate is None
            or recommendation_unaccepted_rate <= thresholds.max_recommendation_unaccepted_rate,
            "Rejected or deferred promotion/kick/demotion recommendations divided by all such recommendations.",
        ),
    }
    return {
        "metrics": metrics,
        "passed": all(item["passed"] for item in metrics.values()),
        "counts": {
            "total_actions": total,
            "terminal_actions": len(terminal),
            "traceable_actions": len(traceable),
            "relay_actions": len(relays),
            "relay_actions_with_copy_text": len(relays_with_copy),
            "recommendation_actions": len(recommendation_rows),
            "unaccepted_recommendations": len(unaccepted_recommendations),
            "stale_open_actions": len(stale_open),
        },
        "stale_open_action_ids": [row["action_id"] for row in stale_open],
    }


def _counts_by(rows: list[sqlite3.Row], *fields: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = "/".join(str(row[field] or "unknown") for field in fields)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def evaluate(
    db_path: str | os.PathLike[str],
    *,
    since: datetime,
    end: datetime,
    thresholds: Thresholds = Thresholds(),
) -> dict[str, Any]:
    conn = _open_db(db_path)
    try:
        actions = _load_actions(conn, since=since, end=end)
        message_ids: set[str] = set()
        for row in actions:
            if row["source_message_id"]:
                message_ids.add(str(row["source_message_id"]))
            message_ids.update(_copy_message_ids(row))
        messages = _load_messages(conn, message_ids)
        scored = _score(actions, now=end, thresholds=thresholds)
        artifacts = [_action_artifact(row, messages) for row in actions]
    finally:
        conn.close()

    return {
        "harness": "eval_leader_actions",
        "window": {
            "since": _format_time(since),
            "end": _format_time(end),
        },
        "thresholds": thresholds.__dict__,
        **scored,
        "status_counts": _counts_by(actions, "status"),
        "action_type_status_counts": _counts_by(actions, "action_type", "status"),
        "artifacts": artifacts,
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
    parser.add_argument("--out", default="scripts/leader_actions_eval_results.json")
    parser.add_argument("--min-decision-rate", type=float, default=0.95)
    parser.add_argument("--min-trace-rate", type=float, default=0.95)
    parser.add_argument("--min-relay-copy-text-rate", type=float, default=0.95)
    parser.add_argument("--max-stale-open-count", type=int, default=0)
    parser.add_argument("--max-recommendation-unaccepted-rate", type=float, default=0.80)
    parser.add_argument("--stale-hours", type=int, default=24)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when thresholds fail")
    args = parser.parse_args()

    since, end = _resolve_window(args)
    thresholds = Thresholds(
        min_decision_rate=args.min_decision_rate,
        min_trace_rate=args.min_trace_rate,
        min_relay_copy_text_rate=args.min_relay_copy_text_rate,
        max_stale_open_count=args.max_stale_open_count,
        max_recommendation_unaccepted_rate=args.max_recommendation_unaccepted_rate,
        stale_hours=args.stale_hours,
    )
    result = evaluate(args.db, since=since, end=end, thresholds=thresholds)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    print("Leader-action eval")
    print(f"  window: {result['window']['since']} -> {result['window']['end']}")
    print(f"  actions: {result['counts']['total_actions']}")
    print(f"  status counts: {result['status_counts']}")
    print(f"  action/type status counts: {result['action_type_status_counts']}")
    for name, metric in result["metrics"].items():
        status = "PASS" if metric["passed"] else "FAIL"
        print(f"  {status} {name}: {metric['value']} threshold={metric['threshold']}")
    print(f"  overall: {'PASS' if result['passed'] else 'FAIL'}")
    print(f"  results: {out_path}")

    if args.strict and not result["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
