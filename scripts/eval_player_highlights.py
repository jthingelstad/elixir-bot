"""Evaluate delivered #player-highlights messaging from exact stored artifacts.

This harness is read-only. It scores recent player-highlight communication
intents and enriches them with exact Discord message bodies from the messages
table and runtime payload audit trail.

Run with:
    python scripts/eval_player_highlights.py --days 14
    python scripts/eval_player_highlights.py --since 2026-06-18T00:00:00Z
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db


TARGET_CHANNEL_KEY = "player-highlights"
DELIVERED_STATUSES = {"delivered", "fulfilled"}
RAW_PLAYER_TAG_RE = re.compile(r"#(?:[0289PYLQGRJCUV]{3,})\b", re.IGNORECASE)
META_PATTERNS = (
    "signal data",
    "messaging would be stale",
    "skipping post",
    "cannot determine",
    "not enough context",
    "insufficient data",
    "i should not",
    "as an ai",
)


@dataclass(frozen=True)
class Thresholds:
    min_delivery_rate: float = 0.95
    min_trace_rate: float = 0.95
    min_message_id_rate: float = 0.95
    min_exact_copy_rate: float = 0.95
    min_non_meta_copy_rate: float = 1.0
    max_raw_player_tag_copy_count: int = 0


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


def _payload_message_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in payload.get("message_ids") or []:
        if item:
            ids.append(str(item))
    for post in payload.get("posted_messages") or []:
        if isinstance(post, dict) and post.get("discord_message_id"):
            ids.append(str(post["discord_message_id"]))
    return list(dict.fromkeys(ids))


def _payload_posted_messages(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    posts: dict[str, dict[str, Any]] = {}
    for post in payload.get("posted_messages") or []:
        if not isinstance(post, dict):
            continue
        message_id = post.get("discord_message_id")
        if message_id:
            posts[str(message_id)] = dict(post)
    return posts


def _load_intents(conn: sqlite3.Connection, *, since: datetime, end: datetime) -> list[sqlite3.Row]:
    if not _table_exists(conn, "communication_intents"):
        raise RuntimeError("communication_intents table is missing")
    return list(
        conn.execute(
            """
            SELECT *
            FROM communication_intents
            WHERE target_channel_key = ?
              AND created_at >= ?
              AND created_at <= ?
            ORDER BY created_at ASC, intent_id ASC
            """,
            (TARGET_CHANNEL_KEY, _format_time(since), _format_time(end)),
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


def _looks_like_meta(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(pattern in lowered for pattern in META_PATTERNS)


def _raw_player_tags(text: str | None) -> list[str]:
    if not text:
        return []
    return list(dict.fromkeys(match.group(0).upper() for match in RAW_PLAYER_TAG_RE.finditer(text)))


def _exact_copy_texts(
    row: sqlite3.Row,
    payload: dict[str, Any],
    messages: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    texts: list[dict[str, Any]] = []
    posted_by_id = _payload_posted_messages(payload)
    for message_id in _payload_message_ids(payload):
        stored = messages.get(message_id)
        if stored and (stored.get("content") or "").strip():
            texts.append({
                "source": "messages",
                "discord_message_id": message_id,
                "content": stored["content"],
                "created_at": stored.get("created_at"),
            })
            continue
        posted = posted_by_id.get(message_id)
        if posted and (posted.get("content") or "").strip():
            texts.append({
                "source": "payload_posted_messages",
                "discord_message_id": message_id,
                "content": posted["content"],
                "created_at": posted.get("discord_created_at"),
            })
    if not texts and (payload.get("original_copy") or "").strip():
        texts.append({
            "source": "payload_original_copy",
            "discord_message_id": None,
            "content": payload["original_copy"],
            "created_at": row["delivered_at"],
        })
    return texts


def _intent_artifact(row: sqlite3.Row, messages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    payload = _json_loads(row["payload_json"], {})
    summary = _json_loads(row["summary"], {})
    message_ids = _payload_message_ids(payload)
    exact_copies = _exact_copy_texts(row, payload, messages)
    return {
        "intent_id": row["intent_id"],
        "intent_key": row["intent_key"],
        "workflow": row["workflow"],
        "intent_type": row["intent_type"],
        "status": row["status"],
        "target_channel_key": row["target_channel_key"],
        "target_channel_id": row["target_channel_id"],
        "source_signal_key": row["source_signal_key"],
        "source_signal_type": row["source_signal_type"],
        "created_at": row["created_at"],
        "delivered_at": row["delivered_at"],
        "failed_at": row["failed_at"],
        "skipped_reason": row["skipped_reason"],
        "error_detail": row["error_detail"],
        "message_ids": message_ids,
        "summary": summary,
        "content_preview": row["content_preview"],
        "exact_copies": exact_copies,
        "messages": [messages[item] for item in message_ids if item in messages],
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


def _has_trace(row: sqlite3.Row) -> bool:
    summary = _json_loads(row["summary"], {})
    return bool(
        row["intent_id"]
        and row["intent_key"]
        and row["intent_type"]
        and row["source_signal_key"]
        and row["source_signal_type"]
        and isinstance(summary, dict)
        and summary.get("detection_type")
    )


def _score(
    intents: list[sqlite3.Row],
    messages: dict[str, dict[str, Any]],
    *,
    thresholds: Thresholds,
) -> dict[str, Any]:
    total = len(intents)
    delivered = [row for row in intents if row["status"] in DELIVERED_STATUSES]
    traceable = [row for row in intents if _has_trace(row)]
    with_ids = [
        row
        for row in delivered
        if _payload_message_ids(_json_loads(row["payload_json"], {}))
    ]
    with_copy = [
        row
        for row in delivered
        if _exact_copy_texts(row, _json_loads(row["payload_json"], {}), messages)
    ]
    meta_copy = []
    raw_player_tag_copy = []
    for row in delivered:
        payload = _json_loads(row["payload_json"], {})
        copies = _exact_copy_texts(row, payload, messages)
        if any(_looks_like_meta(copy.get("content")) for copy in copies):
            meta_copy.append(row)
        tags = []
        for copy in copies:
            tags.extend(_raw_player_tags(copy.get("content")))
        if tags:
            raw_player_tag_copy.append((row, list(dict.fromkeys(tags))))

    delivery_rate = _rate(len(delivered), total)
    trace_rate = _rate(len(traceable), total)
    message_id_rate = _rate(len(with_ids), len(delivered))
    exact_copy_rate = _rate(len(with_copy), len(delivered))
    non_meta_copy_rate = _rate(len(delivered) - len(meta_copy), len(delivered))

    metrics = {
        "delivery_rate": _metric(
            delivery_rate,
            {">=": thresholds.min_delivery_rate},
            delivery_rate is None or delivery_rate >= thresholds.min_delivery_rate,
            "Delivered/fulfilled player-highlight intents divided by all player-highlight intents in the window.",
        ),
        "trace_rate": _metric(
            trace_rate,
            {">=": thresholds.min_trace_rate},
            trace_rate is None or trace_rate >= thresholds.min_trace_rate,
            "Intents with intent id/key/type, source signal key/type, and detection_type summary divided by all rows.",
        ),
        "message_id_rate": _metric(
            message_id_rate,
            {">=": thresholds.min_message_id_rate},
            message_id_rate is None or message_id_rate >= thresholds.min_message_id_rate,
            "Delivered player-highlight intents with at least one stored Discord message id.",
        ),
        "exact_copy_rate": _metric(
            exact_copy_rate,
            {">=": thresholds.min_exact_copy_rate},
            exact_copy_rate is None or exact_copy_rate >= thresholds.min_exact_copy_rate,
            "Delivered player-highlight intents with exact stored copy from messages or the posted-message audit payload.",
        ),
        "non_meta_copy_rate": _metric(
            non_meta_copy_rate,
            {">=": thresholds.min_non_meta_copy_rate},
            non_meta_copy_rate is None or non_meta_copy_rate >= thresholds.min_non_meta_copy_rate,
            "Delivered player-highlight intents whose exact copy does not look like agent diagnostics or refusal text.",
        ),
        "raw_player_tag_copy_count": _metric(
            len(raw_player_tag_copy),
            {"<=": thresholds.max_raw_player_tag_copy_count},
            len(raw_player_tag_copy) <= thresholds.max_raw_player_tag_copy_count,
            "Delivered player-highlight intents whose exact public copy contains a raw Clash Royale player tag.",
        ),
    }
    return {
        "metrics": metrics,
        "passed": all(item["passed"] for item in metrics.values()),
        "counts": {
            "total_intents": total,
            "delivered_intents": len(delivered),
            "traceable_intents": len(traceable),
            "delivered_with_message_ids": len(with_ids),
            "delivered_with_exact_copy": len(with_copy),
            "delivered_with_meta_copy": len(meta_copy),
            "delivered_with_raw_player_tag_copy": len(raw_player_tag_copy),
        },
        "meta_copy_intent_ids": [row["intent_id"] for row in meta_copy],
        "raw_player_tag_copy_intents": [
            {"intent_id": row["intent_id"], "tags": tags}
            for row, tags in raw_player_tag_copy
        ],
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
        intents = _load_intents(conn, since=since, end=end)
        message_ids: set[str] = set()
        for row in intents:
            message_ids.update(_payload_message_ids(_json_loads(row["payload_json"], {})))
        messages = _load_messages(conn, message_ids)
        scored = _score(intents, messages, thresholds=thresholds)
        artifacts = [_intent_artifact(row, messages) for row in intents]
    finally:
        conn.close()

    return {
        "harness": "eval_player_highlights",
        "window": {
            "since": _format_time(since),
            "end": _format_time(end),
        },
        "thresholds": thresholds.__dict__,
        **scored,
        "status_counts": _counts_by(intents, "status"),
        "source_type_counts": _counts_by(intents, "source_signal_type"),
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
    parser.add_argument("--out", default="scripts/player_highlights_eval_results.json")
    parser.add_argument("--min-delivery-rate", type=float, default=0.95)
    parser.add_argument("--min-trace-rate", type=float, default=0.95)
    parser.add_argument("--min-message-id-rate", type=float, default=0.95)
    parser.add_argument("--min-exact-copy-rate", type=float, default=0.95)
    parser.add_argument("--min-non-meta-copy-rate", type=float, default=1.0)
    parser.add_argument("--max-raw-player-tag-copy-count", type=int, default=0)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when thresholds fail")
    args = parser.parse_args()

    since, end = _resolve_window(args)
    thresholds = Thresholds(
        min_delivery_rate=args.min_delivery_rate,
        min_trace_rate=args.min_trace_rate,
        min_message_id_rate=args.min_message_id_rate,
        min_exact_copy_rate=args.min_exact_copy_rate,
        min_non_meta_copy_rate=args.min_non_meta_copy_rate,
        max_raw_player_tag_copy_count=args.max_raw_player_tag_copy_count,
    )
    result = evaluate(args.db, since=since, end=end, thresholds=thresholds)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    print("Player-highlight eval")
    print(f"  window: {result['window']['since']} -> {result['window']['end']}")
    print(f"  intents: {result['counts']['total_intents']}")
    print(f"  status counts: {result['status_counts']}")
    print(f"  source types: {result['source_type_counts']}")
    for name, metric in result["metrics"].items():
        status = "PASS" if metric["passed"] else "FAIL"
        print(f"  {status} {name}: {metric['value']} threshold={metric['threshold']}")
    print(f"  overall: {'PASS' if result['passed'] else 'FAIL'}")
    print(f"  results: {out_path}")

    if args.strict and not result["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
