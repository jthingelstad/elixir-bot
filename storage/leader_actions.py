"""Leader action recommendations and feedback loop tracking."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Optional

import db as _db
from db import managed_connection


ACTION_DONE = "done"
ACTION_PROPOSED = "proposed"
ACTION_REJECTED = "rejected"


def _json_loads(value) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _json_dumps(data) -> str | None:
    if data is None:
        return None
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)


def _row_to_action(row) -> dict:
    item = dict(row)
    item["baseline"] = _json_loads(item.pop("baseline_json", None))
    item["outcome"] = _json_loads(item.pop("outcome_json", None))
    return item


def _stable_action_key(
    *,
    action_type: str,
    objective: str,
    prompt_text: str,
    target_player_tag: str | None = None,
    source_signal_key: str | None = None,
) -> str:
    parts = [
        action_type or "",
        objective or "",
        _db._canon_tag(target_player_tag) if target_player_tag else "",
        source_signal_key or "",
        " ".join((prompt_text or "").split()),
    ]
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{action_type}:{digest}"


def _member_baseline(tag: str | None, *, conn) -> dict:
    if not tag:
        return {}
    profile = _db.get_member_profile(tag, conn=conn) or {}
    if not profile:
        resolved = _db.resolve_member(tag, "any", 1, conn=conn)
        profile = resolved[0] if resolved else {}
    return {
        "player_tag": profile.get("player_tag") or _db._canon_tag(tag),
        "name": profile.get("member_name") or profile.get("current_name") or profile.get("name"),
        "status": profile.get("status"),
        "role": profile.get("role"),
        "donations_week": profile.get("donations_week"),
        "last_seen_at": profile.get("last_seen_at"),
    }


def build_leader_action_baseline(
    *,
    action_type: str,
    target_player_tag: str | None = None,
    signals: list[dict] | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    baseline = {
        "action_type": action_type,
        "captured_at": _db._utcnow(),
    }
    if signals:
        baseline["signals"] = [
            {
                "type": signal.get("type"),
                "signal_key": signal.get("signal_key") or signal.get("signal_log_type"),
            }
            for signal in signals[:5]
        ]
    if action_type == "in_game_relay":
        war_day = _db.get_current_war_day_state(conn=conn) or {}
        war_status = _db.get_current_war_status(conn=conn) or {}
        baseline["war_day"] = {
            "war_day_key": war_day.get("war_day_key"),
            "observed_at": war_day.get("observed_at"),
            "phase": war_day.get("phase"),
            "phase_display": war_day.get("phase_display"),
            "engaged_count": war_day.get("engaged_count"),
            "finished_count": war_day.get("finished_count"),
            "untouched_count": war_day.get("untouched_count"),
            "clan_fame": war_day.get("clan_fame"),
            "race_rank": war_day.get("race_rank"),
        }
        baseline["war_status"] = {
            "observed_at": war_status.get("observed_at"),
            "fame": war_status.get("fame"),
            "race_rank": war_status.get("race_rank"),
            "period_index": war_status.get("period_index"),
            "phase": war_status.get("phase"),
        }
    elif action_type in {"promotion_recommendation", "kick_recommendation", "demotion_recommendation"}:
        baseline["member"] = _member_baseline(target_player_tag, conn=conn)
    return baseline


def _outcome_delta(before, after):
    if before is None or after is None:
        return None
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return after - before
    return None


def evaluate_leader_action(action: dict, *, conn) -> dict:
    baseline = action.get("baseline") or {}
    action_type = action.get("action_type")
    outcome = {
        "evaluated_at": _db._utcnow(),
        "action_type": action_type,
        "status": action.get("status"),
    }
    if action_type == "in_game_relay":
        current = _db.get_current_war_day_state(conn=conn) or {}
        base_day = baseline.get("war_day") or {}
        outcome["war_day"] = {
            "war_day_key": current.get("war_day_key"),
            "observed_at": current.get("observed_at"),
            "engaged_count": current.get("engaged_count"),
            "finished_count": current.get("finished_count"),
            "untouched_count": current.get("untouched_count"),
            "clan_fame": current.get("clan_fame"),
            "race_rank": current.get("race_rank"),
        }
        outcome["deltas"] = {
            "engaged_count": _outcome_delta(base_day.get("engaged_count"), current.get("engaged_count")),
            "finished_count": _outcome_delta(base_day.get("finished_count"), current.get("finished_count")),
            "untouched_count": _outcome_delta(base_day.get("untouched_count"), current.get("untouched_count")),
            "clan_fame": _outcome_delta(base_day.get("clan_fame"), current.get("clan_fame")),
        }
    elif action_type in {"promotion_recommendation", "kick_recommendation", "demotion_recommendation"}:
        current = _member_baseline(action.get("target_player_tag"), conn=conn)
        base_member = baseline.get("member") or {}
        outcome["member"] = current
        outcome["changed"] = {
            "role": base_member.get("role") != current.get("role"),
            "status": base_member.get("status") != current.get("status"),
        }
    return outcome


@managed_connection
def create_leader_action_recommendation(
    *,
    action_type: str,
    objective: str,
    prompt_text: str,
    rationale: str | None = None,
    target_channel_key: str | None = None,
    target_channel_id: str | int | None = None,
    target_player_tag: str | None = None,
    target_player_name: str | None = None,
    source_signal_key: str | None = None,
    source_signal_type: str | None = None,
    source_message_id: str | int | None = None,
    baseline: dict | None = None,
    expires_at: str | None = None,
    action_key: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    action_type = (action_type or "").strip()
    objective = (objective or "").strip()
    prompt_text = " ".join((prompt_text or "").split())
    if not action_type or not objective or not prompt_text:
        raise ValueError("action_type, objective, and prompt_text are required")
    action_key = action_key or _stable_action_key(
        action_type=action_type,
        objective=objective,
        prompt_text=prompt_text,
        target_player_tag=target_player_tag,
        source_signal_key=source_signal_key,
    )
    now = _db._utcnow()
    conn.execute(
        """
        INSERT INTO leader_action_recommendations (
            action_key, action_type, objective, status, target_channel_key, target_channel_id,
            target_player_tag, target_player_name, source_signal_key, source_signal_type,
            source_message_id, prompt_text, rationale, baseline_json, proposed_at,
            expires_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(action_key) DO UPDATE SET
            target_channel_key = excluded.target_channel_key,
            target_channel_id = excluded.target_channel_id,
            source_message_id = COALESCE(excluded.source_message_id, leader_action_recommendations.source_message_id),
            rationale = excluded.rationale,
            baseline_json = COALESCE(leader_action_recommendations.baseline_json, excluded.baseline_json),
            expires_at = excluded.expires_at,
            updated_at = excluded.updated_at
        """,
        (
            action_key,
            action_type,
            objective,
            ACTION_PROPOSED,
            target_channel_key,
            str(target_channel_id) if target_channel_id is not None else None,
            _db._canon_tag(target_player_tag) if target_player_tag else None,
            target_player_name,
            source_signal_key,
            source_signal_type,
            str(source_message_id) if source_message_id is not None else None,
            prompt_text,
            (rationale or "").strip() or None,
            _json_dumps(baseline),
            now,
            expires_at,
            now,
            now,
        ),
    )
    conn.commit()
    return get_leader_action_by_key(action_key, conn=conn) or {}


@managed_connection
def update_leader_action_message(
    action_id: int,
    *,
    source_message_id: str | int | None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    if not action_id or source_message_id is None:
        return
    conn.execute(
        "UPDATE leader_action_recommendations SET source_message_id = ?, updated_at = ? WHERE action_id = ?",
        (str(source_message_id), _db._utcnow(), int(action_id)),
    )
    conn.commit()


@managed_connection
def get_leader_action_by_key(action_key: str, *, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM leader_action_recommendations WHERE action_key = ?",
        ((action_key or "").strip(),),
    ).fetchone()
    return _row_to_action(row) if row else None


@managed_connection
def get_leader_action_by_message(
    source_message_id: str | int,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM leader_action_recommendations WHERE source_message_id = ? ORDER BY action_id DESC LIMIT 1",
        (str(source_message_id),),
    ).fetchone()
    return _row_to_action(row) if row else None


@managed_connection
def list_leader_actions(
    *,
    status: str | None = None,
    limit: int = 10,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    params: list = []
    where = ""
    if status:
        where = "WHERE status = ?"
        params.append(status)
    rows = conn.execute(
        f"SELECT * FROM leader_action_recommendations {where} ORDER BY proposed_at DESC, action_id DESC LIMIT ?",
        (*params, max(1, min(int(limit or 10), 50))),
    ).fetchall()
    return [_row_to_action(row) for row in rows]


@managed_connection
def decide_leader_action_by_message(
    source_message_id: str | int,
    *,
    status: str,
    discord_user_id: str | int,
    emoji: str,
    decided_at: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    action = get_leader_action_by_message(source_message_id, conn=conn)
    if not action:
        return None
    status = (status or "").strip()
    if status not in {ACTION_DONE, ACTION_REJECTED}:
        raise ValueError(f"invalid leader action status: {status}")
    stamp = decided_at or _db._utcnow()
    outcome = None
    if status == ACTION_DONE:
        action["status"] = status
        outcome = evaluate_leader_action(action, conn=conn)
    conn.execute(
        """
        UPDATE leader_action_recommendations
        SET status = ?, decided_at = ?, decided_by_discord_user_id = ?,
            decision_emoji = ?, outcome_json = ?, updated_at = ?
        WHERE action_id = ?
        """,
        (
            status,
            stamp,
            str(discord_user_id),
            emoji,
            _json_dumps(outcome),
            stamp,
            action["action_id"],
        ),
    )
    conn.commit()
    return get_leader_action_by_message(source_message_id, conn=conn)


@managed_connection
def clear_leader_action_decision_by_message(
    source_message_id: str | int,
    *,
    discord_user_id: str | int,
    emoji: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    action = get_leader_action_by_message(source_message_id, conn=conn)
    if not action:
        return None
    if str(action.get("decided_by_discord_user_id") or "") != str(discord_user_id):
        return action
    if str(action.get("decision_emoji") or "") != str(emoji):
        return action
    now = _db._utcnow()
    conn.execute(
        """
        UPDATE leader_action_recommendations
        SET status = ?, decided_at = NULL, decided_by_discord_user_id = NULL,
            decision_emoji = NULL, outcome_json = NULL, updated_at = ?
        WHERE action_id = ?
        """,
        (ACTION_PROPOSED, now, action["action_id"]),
    )
    conn.commit()
    return get_leader_action_by_message(source_message_id, conn=conn)


@managed_connection
def refresh_leader_action_outcome(
    action_id: int,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM leader_action_recommendations WHERE action_id = ?",
        (int(action_id),),
    ).fetchone()
    if not row:
        return None
    action = _row_to_action(row)
    outcome = evaluate_leader_action(action, conn=conn)
    conn.execute(
        "UPDATE leader_action_recommendations SET outcome_json = ?, updated_at = ? WHERE action_id = ?",
        (_json_dumps(outcome), _db._utcnow(), int(action_id)),
    )
    conn.commit()
    return get_leader_action_by_key(action["action_key"], conn=conn)


__all__ = [
    "ACTION_DONE",
    "ACTION_PROPOSED",
    "ACTION_REJECTED",
    "build_leader_action_baseline",
    "clear_leader_action_decision_by_message",
    "create_leader_action_recommendation",
    "decide_leader_action_by_message",
    "get_leader_action_by_key",
    "get_leader_action_by_message",
    "list_leader_actions",
    "refresh_leader_action_outcome",
    "update_leader_action_message",
]
