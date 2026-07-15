"""Leader action recommendations and feedback loop tracking."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional

import db as _db
from db import managed_connection

log = logging.getLogger("elixir.storage.leader_actions")


ACTION_DONE = "done"
ACTION_DEFERRED = "deferred"
ACTION_PROPOSED = "proposed"
ACTION_REJECTED = "rejected"
# Action types resolved by a two-way classification (not done/decline). Their
# cards ignore the ✅/❌ reaction path and resolve only via their own buttons.
_CLASSIFICATION_ACTION_TYPES = {"departure_verification"}
ACTION_OUTCOME_DELAY_HOURS = {
    "in_game_relay": 24,
    "celebration_relay": 24,
    "welcome_relay": 24,
    "discord_invite_relay": 24,
    "promotion_recommendation": 24,
    "kick_recommendation": 24,
    "demotion_recommendation": 24,
}
LEADER_ACTION_FEEDBACK_EVENT_TYPE = "leader_action_feedback_profile"


def _json_loads(value) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _json_loads_list(value) -> list:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _json_dumps(data) -> str | None:
    if data is None:
        return None
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)


def _row_to_action(row) -> dict:
    item = dict(row)
    item["baseline"] = _json_loads(item.pop("baseline_json", None))
    item["outcome"] = _json_loads(item.pop("outcome_json", None))
    item["copy_message_ids"] = _json_loads_list(item.pop("copy_message_ids_json", None))
    item["copy_edit_diff"] = _json_loads(item.pop("copy_edit_diff_json", None))
    item["is_test"] = bool(item.get("is_test"))
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


def _cutoff_hours_ago(hours: int | float) -> str:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        hours=max(1, float(hours or 1))
    )
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _format_utc(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _utc_plus(
    *, hours: int | float = 0, days: int | float = 0, start: str | None = None
) -> str:
    base = _parse_utc(start) or datetime.now(timezone.utc).replace(tzinfo=None)
    return _format_utc(base + timedelta(hours=float(hours or 0), days=float(days or 0)))


def _outcome_delay_hours(action_type: str | None) -> int:
    return ACTION_OUTCOME_DELAY_HOURS.get((action_type or "").strip(), 24)


def _pending_outcome(action: dict, *, decided_at: str) -> dict:
    delay = _outcome_delay_hours(action.get("action_type"))
    return {
        "action_type": action.get("action_type"),
        "status": action.get("status"),
        "pending_evaluation": True,
        "decided_at": decided_at,
        "due_at": _utc_plus(hours=delay, start=decided_at),
        "evaluation_delay_hours": delay,
    }


def _note_feedback(note: str, *, noted_at: str) -> dict:
    text = " ".join((note or "").lower().split())
    if not text:
        return {}
    if "revisit" in text or "check again" in text:
        if "month" in text:
            return {
                "note_category": "revisit",
                "suppressed_until": _utc_plus(days=30, start=noted_at),
            }
        if "2 week" in text or "two week" in text:
            return {
                "note_category": "revisit",
                "suppressed_until": _utc_plus(days=14, start=noted_at),
            }
        if "week" in text:
            return {
                "note_category": "revisit",
                "suppressed_until": _utc_plus(days=7, start=noted_at),
            }
        if "tomorrow" in text:
            return {
                "note_category": "revisit",
                "suppressed_until": _utc_plus(days=1, start=noted_at),
            }
        return {
            "note_category": "revisit",
            "suppressed_until": _utc_plus(days=7, start=noted_at),
        }
    if any(
        phrase in text
        for phrase in ("already done", "already full", "full already", "not needed")
    ):
        return {
            "note_category": "state_already_satisfied",
            "suppressed_until": _utc_plus(days=1, start=noted_at),
        }
    return {}


def _member_baseline(tag: str | None, *, conn) -> dict:
    if not tag:
        return {}
    profile = _db.get_member_profile(tag, conn=conn) or {}
    if not profile:
        resolved = _db.resolve_member(tag, "any", 1, conn=conn)
        profile = resolved[0] if resolved else {}
    return {
        "player_tag": profile.get("player_tag") or _db._canon_tag(tag),
        "name": profile.get("member_name")
        or profile.get("current_name")
        or profile.get("name"),
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
    elif action_type in {
        "promotion_recommendation",
        "kick_recommendation",
        "demotion_recommendation",
        "welcome_relay",
    }:
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
            "engaged_count": _outcome_delta(
                base_day.get("engaged_count"), current.get("engaged_count")
            ),
            "finished_count": _outcome_delta(
                base_day.get("finished_count"), current.get("finished_count")
            ),
            "untouched_count": _outcome_delta(
                base_day.get("untouched_count"), current.get("untouched_count")
            ),
            "clan_fame": _outcome_delta(
                base_day.get("clan_fame"), current.get("clan_fame")
            ),
        }
    elif action_type in {
        "promotion_recommendation",
        "kick_recommendation",
        "demotion_recommendation",
        "welcome_relay",
    }:
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
    prompt_text: str | None = None,
    rationale: str | None = None,
    target_channel_key: str | None = None,
    target_channel_id: str | int | None = None,
    target_player_tag: str | None = None,
    target_player_name: str | None = None,
    source_signal_key: str | None = None,
    source_signal_type: str | None = None,
    source_message_id: str | int | None = None,
    copy_original_text: str | None = None,
    copy_current_text: str | None = None,
    baseline: dict | None = None,
    expires_at: str | None = None,
    action_key: str | None = None,
    case_id: int | None = None,
    is_test: bool = False,
    ui_version: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    action_type = (action_type or "").strip()
    objective = (objective or "").strip()
    prompt_text = " ".join((prompt_text or "").split())
    if not prompt_text:
        # v5.1: engine-created recommendations (kick/promote/demote state
        # machines) carry their case in objective/rationale; derive the prompt.
        prompt_text = (
            " ".join(f"{objective}. {rationale or ''}".split()).strip(". ") + "."
        )
    if not action_type or not objective or not prompt_text:
        raise ValueError("action_type and objective are required")
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
            expires_at, copy_original_text, copy_current_text, is_test, ui_version,
            case_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(action_key) DO UPDATE SET
            target_channel_key = excluded.target_channel_key,
            target_channel_id = excluded.target_channel_id,
            case_id = COALESCE(excluded.case_id, leader_action_recommendations.case_id),
            source_message_id = COALESCE(excluded.source_message_id, leader_action_recommendations.source_message_id),
            rationale = excluded.rationale,
            baseline_json = COALESCE(leader_action_recommendations.baseline_json, excluded.baseline_json),
            copy_original_text = COALESCE(leader_action_recommendations.copy_original_text, excluded.copy_original_text),
            copy_current_text = COALESCE(leader_action_recommendations.copy_current_text, excluded.copy_current_text),
            is_test = excluded.is_test,
            ui_version = COALESCE(excluded.ui_version, leader_action_recommendations.ui_version),
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
            (copy_original_text or "").strip() or None,
            (copy_current_text or copy_original_text or "").strip() or None,
            1 if is_test else 0,
            (ui_version or "").strip() or None,
            int(case_id) if case_id is not None else None,
            now,
            now,
        ),
    )
    # No conn.commit() here: @managed_connection commits when it owns the conn;
    # when the engine tick passes its conn, committing mid-step would defeat the
    # tick's per-step rollback guard.
    return get_leader_action_by_key(action_key, conn=conn) or {}


@managed_connection
def auto_withdraw_leader_actions(
    *,
    action_type: str,
    target_player_tag: str | None,
    reason: str,
    actor: str = "system:auto-withdraw",
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Close open system recommendations when the deterministic state machine
    no longer supports them.

    v5.1 management calls this an auto-withdraw; the action board's existing
    terminal status for a non-actioned recommendation is `rejected`, with the
    note explaining that the system withdrew it rather than a leader declining
    it manually.
    """
    clean_type = (action_type or "").strip()
    clean_tag = _db._canon_tag(target_player_tag) if target_player_tag else None
    clean_reason = " ".join(
        (reason or "Auto-withdrawn by the management evaluator.").split()
    )
    if not clean_type or not clean_tag:
        return 0
    rows = conn.execute(
        """SELECT action_id, case_id
           FROM leader_action_recommendations
           WHERE action_type = ? AND target_player_tag = ?
             AND status = ? AND COALESCE(is_test, 0) = 0""",
        (clean_type, clean_tag, ACTION_PROPOSED),
    ).fetchall()
    if not rows:
        return 0
    action_ids = [int(row["action_id"]) for row in rows]
    now = _db._utcnow()
    placeholders = ",".join("?" for _ in action_ids)
    conn.execute(
        f"""UPDATE leader_action_recommendations
            SET status = ?, decided_at = ?, decided_by_discord_user_id = ?,
                decision_emoji = ?, decision_note = ?,
                decision_note_at = ?, decision_note_by_discord_user_id = ?,
                updated_at = ?
            WHERE action_id IN ({placeholders})""",
        (
            ACTION_REJECTED,
            now,
            actor,
            "auto-withdraw",
            clean_reason,
            now,
            actor,
            now,
            *action_ids,
        ),
    )
    case_ids = sorted(
        {int(row["case_id"]) for row in rows if row["case_id"] is not None}
    )
    if case_ids:
        case_placeholders = ",".join("?" for _ in case_ids)
        conn.execute(
            f"""UPDATE decision_cases
                SET status = 'dismissed', resolved_at = ?, resolution = ?,
                    due_at = NULL, updated_at = ?
                WHERE case_id IN ({case_placeholders})
                  AND status NOT IN ('resolved', 'dismissed')""",
            (now, clean_reason, now, *case_ids),
        )
    # No conn.commit(): see create_leader_action_recommendation — the decorator
    # commits when it owns the conn; a mid-tick commit would break step atomicity.
    return len(action_ids)


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
def update_leader_action_copy_message(
    action_id: int,
    *,
    copy_message_id: str | int | None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    if not action_id or copy_message_id is None:
        return
    conn.execute(
        "UPDATE leader_action_recommendations SET copy_message_id = ?, updated_at = ? WHERE action_id = ?",
        (str(copy_message_id), _db._utcnow(), int(action_id)),
    )
    conn.commit()


@managed_connection
def update_leader_action_copy_messages(
    action_id: int,
    *,
    copy_message_ids: list[str | int] | tuple[str | int, ...] | None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    ids = [str(item) for item in (copy_message_ids or []) if item is not None]
    if not action_id or not ids:
        return
    conn.execute(
        """
        UPDATE leader_action_recommendations
        SET copy_message_id = ?, copy_message_ids_json = ?, updated_at = ?
        WHERE action_id = ?
        """,
        (ids[0], _json_dumps(ids), _db._utcnow(), int(action_id)),
    )
    conn.commit()


def _copy_diff(original: str, edited: str) -> dict:
    old = original or ""
    new = edited or ""
    matcher = SequenceMatcher(None, old, new)
    changed = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed.append(
            {
                "op": tag,
                "old": old[i1:i2],
                "new": new[j1:j2],
            }
        )
    return {
        "changed": old != new,
        "similarity": round(matcher.ratio(), 4),
        "ops": changed[:20],
    }


@managed_connection
def update_leader_action_copy_text(
    action_id: int,
    *,
    copy_text: str,
    discord_user_id: str | int,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM leader_action_recommendations WHERE action_id = ?",
        (int(action_id),),
    ).fetchone()
    if not row:
        return None
    action = _row_to_action(row)
    clean = "\n".join(
        line.strip() for line in str(copy_text or "").splitlines()
    ).strip()
    original = (
        action.get("copy_original_text")
        or action.get("copy_current_text")
        or action.get("prompt_text")
        or ""
    )
    now = _db._utcnow()
    conn.execute(
        """
        UPDATE leader_action_recommendations
        SET copy_original_text = COALESCE(copy_original_text, ?),
            copy_current_text = ?, copy_edited_at = ?,
            copy_edited_by_discord_user_id = ?, copy_edit_diff_json = ?,
            updated_at = ?
        WHERE action_id = ?
        """,
        (
            original,
            clean,
            now,
            str(discord_user_id),
            _json_dumps(_copy_diff(original, clean)),
            now,
            int(action_id),
        ),
    )
    conn.commit()
    # Editor copy-edit feeder (editor.md §3): the leader's rewrite is a paired
    # before/after exemplar for the rubric. Never blocks the edit itself.
    try:
        from engine import editor as _editor

        _editor.record_copy_edit_pair(conn, int(action_id), original, clean)
    except Exception:
        log.debug(
            "editor copy-edit feeder failed for action %s", action_id, exc_info=True
        )
    return get_leader_action_by_id(action_id, conn=conn)


@managed_connection
def get_leader_action_by_id(
    action_id: int,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM leader_action_recommendations WHERE action_id = ?",
        (int(action_id),),
    ).fetchone()
    return _row_to_action(row) if row else None


@managed_connection
def get_leader_action_by_key(
    action_key: str, *, conn: Optional[sqlite3.Connection] = None
) -> Optional[dict]:
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
    message_id = str(source_message_id)
    row = conn.execute(
        "SELECT * FROM leader_action_recommendations "
        "WHERE source_message_id = ? OR copy_message_id = ? OR copy_message_ids_json LIKE ? "
        "ORDER BY action_id DESC LIMIT 1",
        (message_id, message_id, f'%"{message_id}"%'),
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


def _compact_action_for_feedback(action: dict) -> dict:
    outcome = action.get("outcome") or {}
    return {
        "action_id": action.get("action_id"),
        "case_id": action.get("case_id"),
        "action_type": action.get("action_type"),
        "objective": action.get("objective"),
        "status": action.get("status"),
        "target_player_tag": action.get("target_player_tag"),
        "target_player_name": action.get("target_player_name"),
        "prompt_text": action.get("prompt_text"),
        "rationale": action.get("rationale"),
        "decision_emoji": action.get("decision_emoji"),
        "decision_note": action.get("decision_note"),
        "decision_note_at": action.get("decision_note_at"),
        "defer_days": action.get("defer_days"),
        "deferred_until": action.get("deferred_until"),
        "copy_original_text": action.get("copy_original_text"),
        "copy_current_text": action.get("copy_current_text"),
        "copy_edit_diff": action.get("copy_edit_diff"),
        "is_test": action.get("is_test"),
        "proposed_at": action.get("proposed_at"),
        "decided_at": action.get("decided_at"),
        "expires_at": action.get("expires_at"),
        "outcome": outcome if outcome else None,
    }


@managed_connection
def build_leader_action_feedback_synthesis_context(
    *,
    action_type: str | None = None,
    limit: int = 50,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    clean_type = (action_type or "").strip() or None
    where = [
        "(status != ? OR decision_note IS NOT NULL OR outcome_json IS NOT NULL)",
        "COALESCE(is_test, 0) = 0",
    ]
    params: list = [ACTION_PROPOSED]
    if clean_type:
        where.append("action_type = ?")
        params.append(clean_type)
    rows = conn.execute(
        f"SELECT * FROM leader_action_recommendations WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(decision_note_at, decided_at, proposed_at) DESC, action_id DESC LIMIT ?",
        (*params, max(1, min(int(limit or 50), 100))),
    ).fetchall()
    actions = [_compact_action_for_feedback(_row_to_action(row)) for row in rows]
    counts = {
        "total": len(actions),
        ACTION_DONE: sum(1 for item in actions if item.get("status") == ACTION_DONE),
        ACTION_REJECTED: sum(
            1 for item in actions if item.get("status") == ACTION_REJECTED
        ),
        "with_notes": sum(1 for item in actions if item.get("decision_note")),
    }
    types = sorted(
        {item.get("action_type") for item in actions if item.get("action_type")}
    )
    return {
        "action_type": clean_type or "all",
        "counts": counts,
        "action_types_seen": types,
        "recent_actions": actions,
    }


def _feedback_event_id(action_type: str | None) -> str:
    clean = (action_type or "all").strip() or "all"
    return f"leader_action_feedback:{clean}"


def _profile_summary(profile: dict) -> str:
    summary = " ".join(str(profile.get("summary") or "").split())
    if len(summary) <= 220:
        return summary
    return summary[:217].rstrip() + "..."


def _profile_body(profile: dict) -> str:
    lines = []
    summary = " ".join(str(profile.get("summary") or "").split())
    if summary:
        lines.append(summary)
    guidance = [
        str(item).strip() for item in profile.get("guidance") or [] if str(item).strip()
    ]
    if guidance:
        lines.append("Guidance:")
        lines.extend(f"- {item}" for item in guidance[:8])
    avoid = [
        str(item).strip() for item in profile.get("avoid") or [] if str(item).strip()
    ]
    if avoid:
        lines.append("Avoid:")
        lines.extend(f"- {item}" for item in avoid[:5])
    try_next = [
        str(item).strip() for item in profile.get("try_next") or [] if str(item).strip()
    ]
    if try_next:
        lines.append("Try next:")
        lines.extend(f"- {item}" for item in try_next[:5])
    evidence = profile.get("evidence") or []
    evidence_lines = []
    for item in evidence[:6]:
        if not isinstance(item, dict):
            continue
        lesson = " ".join(str(item.get("lesson") or "").split())
        if not lesson:
            continue
        action_id = item.get("action_id")
        prefix = f"R{action_id}: " if action_id is not None else ""
        evidence_lines.append(f"- {prefix}{lesson}")
    if evidence_lines:
        lines.append("Evidence:")
        lines.extend(evidence_lines)
    return "\n".join(lines).strip()


@managed_connection
def upsert_leader_action_feedback_profile(
    *,
    action_type: str,
    profile: dict,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    if not isinstance(profile, dict) or profile.get("_error"):
        return None
    clean_type = (action_type or profile.get("action_type") or "all").strip() or "all"
    body = _profile_body(profile)
    if not body:
        return None
    decision_stats = None
    if clean_type != "all":
        decision_stats = leader_action_decision_stats(action_type=clean_type, conn=conn)
        decided = decision_stats.get("decided") or 0
        if decided:
            rate = decision_stats.get("decline_rate")
            rate_text = f"{rate:.0%}" if rate is not None else "n/a"
            body = (
                f"Decision stats (last {decision_stats['window_days']}d): "
                f"done {decision_stats[ACTION_DONE]} · declined {decision_stats[ACTION_REJECTED]} · "
                f"decline rate {rate_text}\n\n"
            ) + body
    title = f"Arena Relay Feedback: {clean_type}"
    event_id = _feedback_event_id(clean_type)
    metadata = {
        "action_type": clean_type,
        "sample_count": profile.get("sample_count"),
        "profile": profile,
        "decision_stats": decision_stats,
    }
    from memory_store import attach_tags, create_memory, list_memories, update_memory

    existing = list_memories(
        viewer_scope="system_internal",
        include_system_internal=True,
        filters={"event_type": LEADER_ACTION_FEEDBACK_EVENT_TYPE, "event_id": event_id},
        limit=1,
        conn=conn,
    )
    if existing:
        memory = update_memory(
            existing[0]["memory_id"],
            actor="elixir:leader-action-feedback",
            title=title,
            body=body,
            summary=_profile_summary(profile),
            metadata=metadata,
            conn=conn,
        )
    else:
        memory = create_memory(
            title=title,
            body=body,
            summary=_profile_summary(profile),
            source_type="elixir_synthesis",
            is_inference=False,
            confidence=1.0,
            created_by="elixir:leader-action-feedback",
            scope="leadership",
            event_type=LEADER_ACTION_FEEDBACK_EVENT_TYPE,
            event_id=event_id,
            metadata=metadata,
            conn=conn,
        )
    attach_tags(
        memory["memory_id"],
        ["arena-relay", "leader-action-feedback", clean_type],
        actor="elixir:leader-action-feedback",
        conn=conn,
    )
    return memory


@managed_connection
def list_leader_action_feedback_profiles(
    *,
    action_type: str | None = None,
    limit: int = 5,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    from memory_store import list_memories

    filters = {"event_type": LEADER_ACTION_FEEDBACK_EVENT_TYPE}
    if action_type:
        filters["event_id"] = _feedback_event_id(action_type)
    return list_memories(
        viewer_scope="leadership",
        filters=filters,
        limit=max(1, min(int(limit or 5), 10)),
        conn=conn,
    )


@managed_connection
def get_recent_leader_action_for_target(
    *,
    action_type: str,
    target_player_tag: str,
    status: str | None = None,
    within_hours: int = 168,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    where = [
        "action_type = ?",
        "target_player_tag = ?",
        "proposed_at >= ?",
        "COALESCE(is_test, 0) = 0",
    ]
    params: list = [
        (action_type or "").strip(),
        _db._canon_tag(target_player_tag),
        _cutoff_hours_ago(within_hours),
    ]
    if status:
        where.append("status = ?")
        params.append((status or "").strip())
    row = conn.execute(
        f"SELECT * FROM leader_action_recommendations WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(decided_at, proposed_at) DESC, action_id DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    return _row_to_action(row) if row else None


@managed_connection
def has_recent_leader_action(
    *,
    action_type: str,
    target_player_tag: str | None = None,
    objective: str | None = None,
    within_hours: int = 168,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    now = _db._utcnow()
    where = [
        "action_type = ?",
        "COALESCE(is_test, 0) = 0",
        "((expires_at IS NOT NULL AND expires_at > ?) OR (expires_at IS NULL AND proposed_at >= ?))",
    ]
    params: list = [(action_type or "").strip(), now, _cutoff_hours_ago(within_hours)]
    if target_player_tag:
        where.append("target_player_tag = ?")
        params.append(_db._canon_tag(target_player_tag))
    if objective:
        where.append("objective = ?")
        params.append((objective or "").strip())
    row = conn.execute(
        f"SELECT 1 FROM leader_action_recommendations WHERE {' AND '.join(where)} LIMIT 1",
        tuple(params),
    ).fetchone()
    return row is not None


def _compact_action_for_board(action: dict) -> dict:
    prompt_text = (action.get("prompt_text") or "")[:200]
    note = (action.get("decision_note") or "")[:200] or None
    return {
        "action_id": action.get("action_id"),
        "action_type": action.get("action_type"),
        "status": action.get("status"),
        "prompt_text": prompt_text,
        "target_player_tag": action.get("target_player_tag"),
        "target_player_name": action.get("target_player_name"),
        "proposed_at": action.get("proposed_at"),
        "decided_at": action.get("decided_at"),
        "decision_note": note,
    }


@managed_connection
def leader_action_board_snapshot(
    *,
    open_limit: int = 10,
    decided_limit: int = 10,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Compact view of the arena-relay action board.

    Built for the awareness Situation: ``open`` is what the leader has not
    decided yet (so the agent doesn't post about a member with a pending
    card), ``recent_decisions`` is what the leader recently did/declined
    (so the agent doesn't contradict or re-litigate it).
    """
    open_rows = conn.execute(
        "SELECT * FROM leader_action_recommendations "
        "WHERE status = ? AND COALESCE(is_test, 0) = 0 "
        "ORDER BY proposed_at DESC, action_id DESC LIMIT ?",
        (ACTION_PROPOSED, max(1, min(int(open_limit or 10), 25))),
    ).fetchall()
    decided_rows = conn.execute(
        "SELECT * FROM leader_action_recommendations "
        "WHERE status IN (?, ?, ?) AND decided_at IS NOT NULL AND COALESCE(is_test, 0) = 0 "
        "ORDER BY decided_at DESC, action_id DESC LIMIT ?",
        (
            ACTION_DONE,
            ACTION_REJECTED,
            ACTION_DEFERRED,
            max(1, min(int(decided_limit or 10), 25)),
        ),
    ).fetchall()
    return {
        "open": [_compact_action_for_board(_row_to_action(row)) for row in open_rows],
        "recent_decisions": [
            _compact_action_for_board(_row_to_action(row)) for row in decided_rows
        ],
    }


@managed_connection
def leader_action_decision_stats(
    *,
    action_type: str | None = None,
    days: int = 30,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Trailing decision counts and decline rate, per action type.

    decline_rate is rejected / (done + rejected). Returns one stats dict when
    action_type is given, else a mapping of action_type -> stats dict.
    """
    window_days = max(1, int(days or 30))
    cutoff = _cutoff_hours_ago(window_days * 24)
    where = [
        "COALESCE(is_test, 0) = 0",
        "decided_at >= ?",
        "status IN (?, ?)",
    ]
    params: list = [cutoff, ACTION_DONE, ACTION_REJECTED]
    clean_type = (action_type or "").strip() or None
    if clean_type:
        where.append("action_type = ?")
        params.append(clean_type)
    rows = conn.execute(
        f"SELECT action_type, status, COUNT(*) AS cnt FROM leader_action_recommendations "
        f"WHERE {' AND '.join(where)} GROUP BY action_type, status",
        tuple(params),
    ).fetchall()

    def _empty() -> dict:
        return {
            "window_days": window_days,
            ACTION_DONE: 0,
            ACTION_REJECTED: 0,
            "decided": 0,
            "decline_rate": None,
        }

    by_type: dict[str, dict] = {}
    for row in rows:
        stats = by_type.setdefault(row["action_type"], _empty())
        stats[row["status"]] = int(row["cnt"])
    for stats in by_type.values():
        decided = stats[ACTION_DONE] + stats[ACTION_REJECTED]
        stats["decided"] = decided
        stats["decline_rate"] = (stats[ACTION_REJECTED] / decided) if decided else None
    if clean_type:
        return by_type.get(clean_type) or _empty()
    return by_type


@managed_connection
def decide_leader_action_by_message(
    source_message_id: str | int,
    *,
    status: str,
    discord_user_id: str | int,
    emoji: str,
    decision_note: str | None = None,
    decided_at: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    action = get_leader_action_by_message(source_message_id, conn=conn)
    if not action:
        return None
    if action.get("action_type") in _CLASSIFICATION_ACTION_TYPES:
        # Two-way classification cards (LEAVE vs KICK) resolve only via their
        # buttons; a bare ✅/❌ reaction can't express which choice. Ignore it.
        return None
    return decide_leader_action(
        action["action_id"],
        status=status,
        discord_user_id=discord_user_id,
        emoji=emoji,
        decision_note=decision_note,
        decided_at=decided_at,
        conn=conn,
    )


@managed_connection
def decide_leader_action(
    action_id: int,
    *,
    status: str,
    discord_user_id: str | int,
    emoji: str | None = None,
    decision_note: str | None = None,
    decided_at: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    action = get_leader_action_by_id(action_id, conn=conn)
    if not action:
        return None
    status = (status or "").strip()
    if status not in {ACTION_DONE, ACTION_REJECTED}:
        raise ValueError(f"invalid leader action status: {status}")
    stamp = decided_at or _db._utcnow()
    outcome = None
    if status == ACTION_DONE:
        action["status"] = status
        outcome = _pending_outcome(action, decided_at=stamp)
    clean_note = " ".join((decision_note or "").split()) or None
    expires_at = action.get("expires_at")
    # A decline reason like "revisit in a month" tunes when the engine may
    # re-nominate this member: parse it into a suppression window written to
    # expires_at (mirrors record_leader_action_note_by_message). No note → the
    # engine's default per-dimension re-nomination cooldown applies.
    if status == ACTION_REJECTED and clean_note:
        feedback = _note_feedback(clean_note, noted_at=stamp)
        if feedback.get("suppressed_until"):
            expires_at = feedback["suppressed_until"]
            outcome = {
                **(outcome or {}),
                "leader_note": {
                    "category": feedback.get("note_category"),
                    "suppressed_until": feedback.get("suppressed_until"),
                },
            }
    conn.execute(
        """
        UPDATE leader_action_recommendations
        SET status = ?, decided_at = ?, decided_by_discord_user_id = ?,
            decision_emoji = ?, decision_note = COALESCE(?, decision_note),
            decision_note_at = CASE WHEN ? IS NOT NULL THEN ? ELSE decision_note_at END,
            decision_note_by_discord_user_id = CASE WHEN ? IS NOT NULL THEN ? ELSE decision_note_by_discord_user_id END,
            expires_at = ?, outcome_json = ?, updated_at = ?
        WHERE action_id = ?
        """,
        (
            status,
            stamp,
            str(discord_user_id),
            emoji or "",
            clean_note,
            clean_note,
            stamp,
            clean_note,
            str(discord_user_id),
            expires_at,
            _json_dumps(outcome),
            stamp,
            action["action_id"],
        ),
    )
    conn.commit()
    updated = get_leader_action_by_id(action["action_id"], conn=conn)
    if updated and updated.get("case_id"):
        try:
            # Inlined at the v5.1 cut (storage.decision_cases retired with Gen B):
            # the decision_cases table is carried, so the case-sync is two UPDATEs.
            case_now = _db._utcnow()
            case_status = "resolved" if status == ACTION_DONE else "dismissed"
            default_note = (
                "Leader accepted the recommended action."
                if status == ACTION_DONE
                else "Leader declined the recommended action."
            )
            conn.execute(
                """UPDATE decision_cases
                   SET status = ?, resolved_at = ?, resolution = ?,
                       due_at = NULL, updated_at = ?
                   WHERE case_id = ?""",
                (
                    case_status,
                    case_now,
                    clean_note or default_note,
                    case_now,
                    int(updated["case_id"]),
                ),
            )
            conn.commit()
        except Exception as exc:
            # The card decision is the user action of record; case sync should
            # not make the Discord reaction handler fail, but the split state
            # must be visible for repair.
            from storage.incidents import record_incident

            log.warning(
                "leader action case sync failed action_id=%s case_id=%s",
                updated.get("action_id"),
                updated.get("case_id"),
                exc_info=True,
            )
            record_incident(
                "leader_actions.case_sync",
                exc,
                context={
                    "action_id": updated.get("action_id"),
                    "case_id": updated.get("case_id"),
                    "status": status,
                },
            )
    return updated


# Authoritative leave_source values written when a leader verifies a departure
# (vs the inferred 'roster_diff'). See storage.cases._departure_was_kick, which
# prefers these over the 14-day inference.
LEAVE_SOURCE_VERIFIED = {
    "leave": "leader_verified_leave",
    "kick": "leader_verified_kick",
}
_CLASSIFY_EMOJI = {"leave": "🚶", "kick": "🚪"}


@managed_connection
def classify_departure(
    action_id: int,
    *,
    classification: str,
    discord_user_id: str | int,
    comment: str | None = None,
    decided_at: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Resolve a departure_verification card. The leader confirms the member
    LEFT on their own (``classification='leave'``) or was KICKED (``'kick'``).

    Writes the authoritative ``clan_memberships.leave_source`` (the member's row
    always exists, unlike a decision case), marks the card done-with-classification,
    and — when the leader added a comment on WHY — records it as a durable
    leadership memory on that member. Open member-review cases reconcile against
    the new leave_source on the next engine tick (see
    ``storage.cases.reconcile_departed_member_cases``)."""
    classification = (classification or "").strip().lower()
    if classification not in LEAVE_SOURCE_VERIFIED:
        raise ValueError(f"invalid departure classification: {classification}")
    action = get_leader_action_by_id(action_id, conn=conn)
    if not action:
        return None
    stamp = decided_at or _db._utcnow()
    clean_comment = " ".join((comment or "").split()) or None
    canon = (
        _db._canon_tag(action.get("target_player_tag"))
        if action.get("target_player_tag")
        else None
    )

    if canon:
        conn.execute(
            """UPDATE clan_memberships SET leave_source = ?
               WHERE membership_id = (
                   SELECT membership_id FROM clan_memberships
                   WHERE UPPER(player_tag) = UPPER(?) AND left_at IS NOT NULL
                   ORDER BY left_at DESC, membership_id DESC LIMIT 1
               )""",
            (LEAVE_SOURCE_VERIFIED[classification], canon),
        )

    outcome = {
        "evaluated_at": stamp,
        "action_type": action.get("action_type"),
        "status": ACTION_DONE,
        "classification": classification,
    }
    conn.execute(
        """UPDATE leader_action_recommendations
           SET status = ?, decided_at = ?, decided_by_discord_user_id = ?,
               decision_emoji = ?,
               decision_note = COALESCE(?, decision_note),
               decision_note_at = CASE WHEN ? IS NOT NULL THEN ? ELSE decision_note_at END,
               decision_note_by_discord_user_id = CASE WHEN ? IS NOT NULL THEN ? ELSE decision_note_by_discord_user_id END,
               outcome_json = ?, updated_at = ?
           WHERE action_id = ?""",
        (
            ACTION_DONE,
            stamp,
            str(discord_user_id),
            _CLASSIFY_EMOJI[classification],
            clean_comment,
            clean_comment,
            stamp,
            clean_comment,
            str(discord_user_id),
            _json_dumps(outcome),
            stamp,
            action["action_id"],
        ),
    )

    if clean_comment and canon:
        try:
            from memory_store import create_memory

            verb = "was kicked from" if classification == "kick" else "left"
            name = action.get("target_player_name") or canon
            create_memory(
                body=f"{name} {verb} the clan. Leader note: {clean_comment}",
                source_type="leader_note",
                is_inference=False,
                confidence=1.0,
                created_by=f"discord:{discord_user_id}",
                scope="leadership",
                title=f"Departure ({classification}): {name}",
                member_tag=canon,
                conn=conn,
            )
        except Exception:
            log.warning("departure memory write failed for %s", canon, exc_info=True)

    conn.commit()
    return get_leader_action_by_id(action["action_id"], conn=conn)


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
            decision_emoji = NULL, defer_days = NULL, deferred_until = NULL,
            outcome_json = NULL, updated_at = ?
        WHERE action_id = ?
        """,
        (ACTION_PROPOSED, now, action["action_id"]),
    )
    conn.commit()
    return get_leader_action_by_message(source_message_id, conn=conn)


@managed_connection
def record_leader_action_note_by_message(
    source_message_id: str | int,
    *,
    note: str,
    discord_user_id: str | int,
    note_message_id: str | int | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    action = get_leader_action_by_message(source_message_id, conn=conn)
    if not action:
        return None
    body = " ".join((note or "").split())
    if not body:
        return action
    now = _db._utcnow()
    feedback = _note_feedback(body, noted_at=now)
    outcome = action.get("outcome") or {}
    if feedback:
        outcome = {
            **outcome,
            "leader_note": {
                "category": feedback.get("note_category"),
                "suppressed_until": feedback.get("suppressed_until"),
            },
        }
    conn.execute(
        """
        UPDATE leader_action_recommendations
        SET decision_note = ?, decision_note_at = ?,
            decision_note_message_id = ?, decision_note_by_discord_user_id = ?,
            expires_at = COALESCE(?, expires_at), outcome_json = ?, updated_at = ?
        WHERE action_id = ?
        """,
        (
            body,
            now,
            str(note_message_id) if note_message_id is not None else None,
            str(discord_user_id),
            feedback.get("suppressed_until"),
            _json_dumps(outcome) if outcome else None,
            now,
            action["action_id"],
        ),
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


@managed_connection
def refresh_due_leader_action_outcomes(
    *,
    limit: int = 20,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM leader_action_recommendations "
        "WHERE status = ? AND decided_at IS NOT NULL "
        "ORDER BY decided_at ASC LIMIT ?",
        (ACTION_DONE, max(1, min(int(limit or 20), 100))),
    ).fetchall()
    refreshed = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in rows:
        action = _row_to_action(row)
        decided_at = _parse_utc(action.get("decided_at"))
        if decided_at is None:
            continue
        outcome = action.get("outcome") or {}
        if outcome and not outcome.get("pending_evaluation"):
            continue
        delay = _outcome_delay_hours(action.get("action_type"))
        if now < decided_at + timedelta(hours=delay):
            continue
        refreshed_action = refresh_leader_action_outcome(action["action_id"], conn=conn)
        if refreshed_action:
            refreshed.append(refreshed_action)
    return refreshed


__all__ = [
    "ACTION_OUTCOME_DELAY_HOURS",
    "ACTION_DEFERRED",
    "ACTION_DONE",
    "ACTION_PROPOSED",
    "ACTION_REJECTED",
    "LEADER_ACTION_FEEDBACK_EVENT_TYPE",
    "build_leader_action_feedback_synthesis_context",
    "build_leader_action_baseline",
    "auto_withdraw_leader_actions",
    "classify_departure",
    "clear_leader_action_decision_by_message",
    "create_leader_action_recommendation",
    "decide_leader_action",
    "decide_leader_action_by_message",
    "get_leader_action_by_id",
    "get_leader_action_by_key",
    "get_leader_action_by_message",
    "get_recent_leader_action_for_target",
    "has_recent_leader_action",
    "leader_action_board_snapshot",
    "leader_action_decision_stats",
    "list_leader_action_feedback_profiles",
    "list_leader_actions",
    "record_leader_action_note_by_message",
    "refresh_due_leader_action_outcomes",
    "refresh_leader_action_outcome",
    "update_leader_action_copy_messages",
    "update_leader_action_message",
    "update_leader_action_copy_message",
    "update_leader_action_copy_text",
    "upsert_leader_action_feedback_profile",
]
