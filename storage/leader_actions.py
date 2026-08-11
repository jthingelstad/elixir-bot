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
# Sentinel written to source_message_id the instant BEFORE a card is posted, so a
# crash mid-post can't double-post next tick. A card carrying it has NOT landed on
# Discord yet — treat it as unposted (see count_open_leader_actions, and the
# Forbidden-clear path in runtime.app._post_pending_leader_action_cards).
POSTING_SENTINEL = "posting"

# How a decision was entered. A ✅/❌ REACTION can be taken back by removing it
# (clear_leader_action_decision_by_message); a BUTTON press cannot, because the
# button path stores the very same "✅" and a leader who clicked Done, then
# added and removed a ✅ reaction, silently reopened their own decided card.
# Recorded in outcome_json so this needs no schema change.
DECIDED_VIA_REACTION = "reaction"
DECIDED_VIA_BUTTON = "button"
# Action types resolved by an explicit classification (not done/decline). Their
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
    except TypeError, json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _json_loads_list(value) -> list:
    if not value:
        return []
    try:
        data = json.loads(value)
    except TypeError, json.JSONDecodeError:
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
    item["note_interpret"] = _json_loads(item.get("note_interpret_json"))
    item["premise_rejected"] = bool(item.get("premise_rejected"))
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


def _utc_plus(*, hours: int | float = 0, days: int | float = 0, start: str | None = None) -> str:
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
        phrase in text for phrase in ("already done", "already full", "full already", "not needed")
    ):
        return {
            "note_category": "state_already_satisfied",
            "suppressed_until": _utc_plus(days=1, start=noted_at),
        }
    return {}


def note_text_hash(note: str | None) -> str | None:
    """Stable hash of a leader note's text, used to make async interpretation
    idempotent (skip an already-interpreted note; re-interpret an edited one)."""
    body = " ".join((note or "").split())
    if not body:
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _mark_note_pending(conn, action_id: int, note: str | None) -> None:
    """Flag a freshly-written leader note for async interpretation. Deterministic,
    inside the same transaction as the note write — no LLM here. A NULL/blank note
    clears the interpretation state (nothing to read)."""
    body_hash = note_text_hash(note)
    if body_hash is None:
        conn.execute(
            "UPDATE leader_action_recommendations "
            "SET note_interpret_status = NULL, note_interpret_note_hash = NULL "
            "WHERE action_id = ?",
            (int(action_id),),
        )
        return
    conn.execute(
        "UPDATE leader_action_recommendations "
        "SET note_interpret_status = 'pending', note_interpret_note_hash = ? "
        "WHERE action_id = ?",
        (body_hash, int(action_id)),
    )


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
            "clan_fame": _outcome_delta(base_day.get("clan_fame"), current.get("clan_fame")),
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
        prompt_text = " ".join(f"{objective}. {rationale or ''}".split()).strip(". ") + "."
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
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        -- Re-proposing an existing action_key refreshes the card in place. Two
        -- rules govern what may move.
        --
        -- 1. A DECIDED card is frozen. `rationale` used to refresh
        --    unconditionally, so a re-proposal rewrote the "Why" on a card a
        --    leader had already answered — and on an OPEN card it rewrote the
        --    Why while leaving the "Decision" line untouched, because
        --    prompt_text was not in this list at all. The leader read a fresh
        --    rationale under stale instructions. 30 of the action_ids in
        --    production are re-proposals, so this was not hypothetical.
        -- 2. A leader's own edits win. copy_current_text stays COALESCEd so a
        --    manual Edit Copy is never overwritten by a regenerated one.
        ON CONFLICT(action_key) DO UPDATE SET
            target_channel_key = excluded.target_channel_key,
            target_channel_id = excluded.target_channel_id,
            source_message_id = COALESCE(excluded.source_message_id, leader_action_recommendations.source_message_id),
            prompt_text = CASE WHEN leader_action_recommendations.status = 'proposed'
                               THEN excluded.prompt_text
                               ELSE leader_action_recommendations.prompt_text END,
            rationale = CASE WHEN leader_action_recommendations.status = 'proposed'
                             THEN excluded.rationale
                             ELSE leader_action_recommendations.rationale END,
            baseline_json = COALESCE(leader_action_recommendations.baseline_json, excluded.baseline_json),
            copy_original_text = CASE WHEN leader_action_recommendations.status = 'proposed'
                                      THEN COALESCE(excluded.copy_original_text, leader_action_recommendations.copy_original_text)
                                      ELSE leader_action_recommendations.copy_original_text END,
            copy_current_text = COALESCE(leader_action_recommendations.copy_current_text, excluded.copy_current_text),
            is_test = excluded.is_test,
            ui_version = COALESCE(excluded.ui_version, leader_action_recommendations.ui_version),
            -- Never wipe a suppression window. No producer passes expires_at, so
            -- `excluded.expires_at` is always NULL here: the old unconditional
            -- assignment silently cleared a note-derived re-nomination hold
            -- (`timing_hold`, "revisit in a month") on every re-proposal.
            expires_at = COALESCE(excluded.expires_at, leader_action_recommendations.expires_at),
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
    clean_reason = " ".join((reason or "Auto-withdrawn by the management evaluator.").split())
    if not clean_type or not clean_tag:
        return 0
    rows = conn.execute(
        """SELECT action_id
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


@managed_connection
def clear_leader_action_source_message(
    action_id, conn: Optional[sqlite3.Connection] = None
) -> None:
    """Reset source_message_id to NULL so the poster will retry the card.

    update_leader_action_message() cannot do this — it treats source_message_id=None
    as 'no change' — so a post that failed AFTER claiming the POSTING_SENTINEL would
    otherwise be stranded at 'posting' forever, invisible on Discord yet still on the
    books. Used when we KNOW the post never landed (e.g. a 403)."""
    if not action_id:
        return
    conn.execute(
        "UPDATE leader_action_recommendations SET source_message_id = NULL, updated_at = ? WHERE action_id = ?",
        (_db._utcnow(), int(action_id)),
    )


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
    clean = "\n".join(line.strip() for line in str(copy_text or "").splitlines()).strip()
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
    # Editor copy-edit feeder (engine/editor.py): the leader's rewrite is a paired
    # before/after exemplar for the rubric. Never blocks the edit itself.
    try:
        from engine import editor as _editor

        _editor.record_copy_edit_pair(conn, int(action_id), original, clean)
    except Exception:
        log.debug("editor copy-edit feeder failed for action %s", action_id, exc_info=True)
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
        ACTION_REJECTED: sum(1 for item in actions if item.get("status") == ACTION_REJECTED),
        "with_notes": sum(1 for item in actions if item.get("decision_note")),
    }
    types = sorted({item.get("action_type") for item in actions if item.get("action_type")})
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
    guidance = [str(item).strip() for item in profile.get("guidance") or [] if str(item).strip()]
    if guidance:
        lines.append("Guidance:")
        lines.extend(f"- {item}" for item in guidance[:8])
    avoid = [str(item).strip() for item in profile.get("avoid") or [] if str(item).strip()]
    if avoid:
        lines.append("Avoid:")
        lines.extend(f"- {item}" for item in avoid[:5])
    try_next = [str(item).strip() for item in profile.get("try_next") or [] if str(item).strip()]
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
        ["actions", "leader-action-feedback", clean_type],
        actor="elixir:leader-action-feedback",
        conn=conn,
    )
    return memory


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
        # Classification cards resolve only via their buttons; a bare ✅/❌
        # reaction can't express which choice. Ignore it.
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


# Enacting a card must clear the management state that produced it. SQL per
# action type; `state_json` carries the hysteresis counters alongside the column.
_ENACTED_STATE_RESET = {
    "promotion_recommendation": (
        "promote_state = 'none', promote_qualifying_weeks = 0, "
        "state_json = json_set(COALESCE(state_json, '{}'), '$.promote_misses', 0)"
    ),
    "demotion_recommendation": (
        "demote_state = 'none', "
        "state_json = json_set(COALESCE(state_json, '{}'), '$.demote_weeks', 0)"
    ),
}


def _reconcile_management_state(conn, action: dict, status: str) -> None:
    """Write an enacted decision back to `member_management`.

    Reconciliation used to run ONE direction: `withdraw_stale_actions` closes a
    CARD when the STATE withdraws, but nothing wrote back the other way. So an
    enacted promotion left `promote_state='eligible'` until the next weekly
    review — up to 7 days — and the awareness brain reads those states as live
    recommendations. Live on 2026-07-28: pax had been promoted and Tere demoted
    hours earlier, yet the read still carried "promote: 1, demote: 2" with zero
    open cards behind them. The same staleness handed Fullboat and dez42 fresh
    "promote to Elder" cards a week after they were promoted (R214/R215); the
    guard added then only patches it at review time, and only when the role
    happens to mismatch.

    Only DONE clears. A DECLINE leaves the state alone — the member may still
    genuinely warrant the action (OllieTurtle remained outranked after his
    demotion was declined), and the re-nomination cooldown already owns whether
    a card comes back. `management_read_summary` reports warranted separately
    from open-ask so a declined-but-still-true state stops reading as a pending
    request.

    Kick is deliberately excluded: `kick_state` is recomputed EVERY tick from
    idleness and `run_tick_evaluators` fires a card on the transition INTO
    'recommended'. Clearing it to 'none' would re-arm that transition and raise
    a second card on the next tick whenever the member has not left yet.
    """
    if status != ACTION_DONE:
        return
    assignment = _ENACTED_STATE_RESET.get(action.get("action_type") or "")
    tag = action.get("target_player_tag")
    if not assignment or not tag:
        return
    conn.execute(
        f"UPDATE member_management SET {assignment} WHERE UPPER(player_tag) = UPPER(?)",
        (_db._canon_tag(tag),),
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
    decided_via: str = DECIDED_VIA_REACTION,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    action = get_leader_action_by_id(action_id, conn=conn)
    if not action:
        return None
    status = (status or "").strip()
    if status not in {ACTION_DONE, ACTION_REJECTED}:
        raise ValueError(f"invalid leader action status: {status}")
    # A decision only lands on an OPEN card. Without this the UPDATE below was a
    # blind last-writer-wins on action_id: two leaders on the same card, or one
    # leader clicking Done on a card the engine had already auto-withdrawn,
    # silently overwrote the first decision with no trace of it. The flip was
    # also asymmetric — _reconcile_management_state clears promote_state /
    # demote_state on DONE and cannot restore them on the way back out, so the
    # card read "Declined" while member_management read "enacted".
    #
    # Re-deciding is not a supported workflow: a refreshed card drops its
    # buttons once decided (LeaderActionView renders primary buttons only while
    # open), and the reaction path reopens a card when the leader REMOVES their
    # reaction. So the escape hatch for a misclick is reopen-then-decide, which
    # goes through clear_leader_action_decision_by_message and leaves a trail.
    if action.get("status") != ACTION_PROPOSED:
        return None
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
    outcome = {**(outcome or {}), "decided_via": decided_via}
    cursor = conn.execute(
        """
        UPDATE leader_action_recommendations
        SET status = ?, decided_at = ?, decided_by_discord_user_id = ?,
            decision_emoji = ?, decision_note = COALESCE(?, decision_note),
            decision_note_at = CASE WHEN ? IS NOT NULL THEN ? ELSE decision_note_at END,
            decision_note_by_discord_user_id = CASE WHEN ? IS NOT NULL THEN ? ELSE decision_note_by_discord_user_id END,
            expires_at = ?, outcome_json = ?, updated_at = ?
        WHERE action_id = ? AND status = ?
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
            ACTION_PROPOSED,
        ),
    )
    # The status re-check above closes the read-then-write window between two
    # connections; if another writer decided the card first, claim nothing.
    if cursor.rowcount == 0:
        return None
    if status == ACTION_REJECTED:
        _mark_note_pending(conn, action["action_id"], clean_note)
    _reconcile_management_state(conn, action, status)
    return get_leader_action_by_id(action["action_id"], conn=conn)


# A departure is attributed to a kick when a kick recommendation was marked
# done within this window before the member left. An explicit leader
# classification overrides this inference.
_KICK_ATTRIBUTION_DAYS = 14


def _departure_was_kick(conn, tag: str, left_at: str | None) -> bool:
    """Return whether the latest departure is known or inferred to be a kick."""
    verified = conn.execute(
        """SELECT leave_source FROM clan_memberships
           WHERE UPPER(player_tag) = UPPER(?) AND left_at IS NOT NULL
           ORDER BY left_at DESC, membership_id DESC LIMIT 1""",
        (tag,),
    ).fetchone()
    source = (verified["leave_source"] if verified else None) or ""
    if source == "leader_verified_kick":
        return True
    if source in {"leader_verified_leave", "leader_ignored_departure"}:
        return False
    params: list = [tag]
    window = ""
    anchor = _parse_utc(left_at) if left_at else None
    if anchor:
        cutoff = (anchor - timedelta(days=_KICK_ATTRIBUTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        window = "AND COALESCE(decided_at, proposed_at) >= ?"
        params.append(cutoff)
    row = conn.execute(
        f"""SELECT 1 FROM leader_action_recommendations
            WHERE action_type = 'kick_recommendation' AND target_player_tag = ?
              AND status = 'done' AND COALESCE(is_test, 0) = 0 {window} LIMIT 1""",
        params,
    ).fetchone()
    return row is not None


# A departure is only carded if it was detected within this window, avoiding a
# flood of historical departures on first deploy.
_DEPARTURE_CARD_LOOKBACK_DAYS = 2


@managed_connection
def raise_departure_verification_cards(
    *,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Raise action cards for recent departures whose cause is ambiguous."""
    current = str(now or "").strip() or _db._utcnow()
    anchor = _parse_utc(current) or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (anchor - timedelta(days=_DEPARTURE_CARD_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """SELECT cm.membership_id, cm.player_tag, cm.left_at,
                  COALESCE(p.display_name, p.current_name, cm.player_tag) AS name,
                  CAST(julianday(cm.left_at) - julianday(cm.joined_at) AS INTEGER) AS tenure_days
           FROM clan_memberships cm
           LEFT JOIN players p ON p.player_tag = cm.player_tag
           WHERE cm.left_at IS NOT NULL
             AND cm.leave_source = 'roster_diff'
             AND cm.left_at >= ?""",
        (cutoff,),
    ).fetchall()
    raised: list[dict] = []
    for row in rows:
        tag = row["player_tag"]
        if _departure_was_kick(conn, tag, row["left_at"]):
            conn.execute(
                "UPDATE clan_memberships SET leave_source = 'leader_verified_kick' "
                "WHERE membership_id = ?",
                (row["membership_id"],),
            )
            continue
        signal_key = f"engine:departure:{tag}:{row['left_at']}"
        exists = conn.execute(
            """SELECT 1 FROM leader_action_recommendations
               WHERE action_type = 'departure_verification'
                 AND (source_signal_key = ?
                      OR (UPPER(target_player_tag) = UPPER(?) AND status = 'proposed'))
                 AND COALESCE(is_test, 0) = 0 LIMIT 1""",
            (signal_key, tag),
        ).fetchone()
        if exists:
            continue
        name = row["name"]
        tenure = row["tenure_days"]
        tenure_text = f"{tenure} days" if tenure is not None else "unknown tenure"
        create_leader_action_recommendation(
            action_type="departure_verification",
            objective=f"Confirm departure: did {name} leave or get kicked?",
            prompt_text=(
                f"{name} is no longer in the clan (tenure {tenure_text}). Elixir can't tell "
                f"what happened — choose KICKED, LEFT, or IGNORE. LEFT opens an optional "
                f"note for farewell context (e.g. “alt account of X”, or a detail worth a "
                f"mention) and then posts the goodbye. KICKED and IGNORE close this card "
                f"silently with no public post."
            ),
            rationale=f"Departure detected {row['left_at']}; leave_source unverified (roster_diff).",
            target_player_tag=tag,
            target_player_name=name,
            source_signal_key=signal_key,
            source_signal_type="engine_departure",
            conn=conn,
        )
        raised.append({"player_tag": tag, "player_name": name, "left_at": row["left_at"]})
    return raised


# Unanswered departure cards settle to a benign unverified leave so the action
# board stays clean and no stale goodbye fires.
_DEPARTURE_CARD_TIMEOUT_DAYS = 3


@managed_connection
def expire_departure_verification_cards(
    *,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Auto-settle departure cards that leaders did not answer in time."""
    current = str(now or "").strip() or _db._utcnow()
    anchor = _parse_utc(current) or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (anchor - timedelta(days=_DEPARTURE_CARD_TIMEOUT_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """SELECT action_id, target_player_tag FROM leader_action_recommendations
           WHERE action_type = 'departure_verification' AND status = 'proposed'
             AND COALESCE(is_test, 0) = 0
             AND COALESCE(proposed_at, created_at) <= ?""",
        (cutoff,),
    ).fetchall()
    expired: list[dict] = []
    for row in rows:
        tag = row["target_player_tag"]
        if tag:
            conn.execute(
                """UPDATE clan_memberships SET leave_source = 'leave_unverified'
                   WHERE membership_id = (
                       SELECT membership_id FROM clan_memberships
                       WHERE UPPER(player_tag) = UPPER(?) AND left_at IS NOT NULL
                         AND leave_source = 'roster_diff'
                       ORDER BY left_at DESC, membership_id DESC LIMIT 1
                   )""",
                (tag,),
            )
        conn.execute(
            """UPDATE leader_action_recommendations
               SET status = 'done', decided_at = ?, decided_by_discord_user_id = 'system',
                   decision_emoji = '⌛',
                   decision_note = COALESCE(decision_note, ?),
                   outcome_json = ?, updated_at = ?
               WHERE action_id = ?""",
            (
                current,
                "Auto-settled: no leader verification within the window; treated as "
                "an organic leave. No public goodbye was posted.",
                _json_dumps({"classification": "leave_unverified", "auto_settled": True}),
                current,
                row["action_id"],
            ),
        )
        expired.append({"action_id": row["action_id"], "target_player_tag": tag})
    return expired


# Authoritative leave_source values written when a leader verifies a departure.
LEAVE_SOURCE_VERIFIED = {
    "leave": "leader_verified_leave",
    "kick": "leader_verified_kick",
    "ignore": "leader_ignored_departure",
}
_CLASSIFY_EMOJI = {"leave": "🚶", "kick": "🚪", "ignore": "🔕"}


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
    LEFT on their own (``classification='leave'``), was KICKED (``'kick'``), or
    should be silently ignored (``'ignore'``).

    Writes the authoritative ``clan_memberships.leave_source`` (the member's row
    always exists, unlike a decision case), marks the card done-with-classification,
    and — when the leader added a comment on WHY — records it as a durable
    leadership memory on that member."""
    classification = (classification or "").strip().lower()
    if classification not in LEAVE_SOURCE_VERIFIED:
        raise ValueError(f"invalid departure classification: {classification}")
    action = get_leader_action_by_id(action_id, conn=conn)
    if not action:
        return None
    # Same open-card rule as decide_leader_action. Classifying twice rewrote
    # clan_memberships.leave_source (against the LATEST departure for the tag,
    # which need not be the one this card was raised for) and created a SECOND
    # departure memory from the same comment.
    if action.get("status") != ACTION_PROPOSED:
        return None
    stamp = decided_at or _db._utcnow()
    clean_comment = " ".join((comment or "").split()) or None
    canon = (
        _db._canon_tag(action.get("target_player_tag")) if action.get("target_player_tag") else None
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

    # Departure comments are factual context for the farewell/memory, not
    # management instructions. Do not route them through the recommendation
    # note interpreter (the production "no need to comment" note was otherwise
    # misread as an unrelated premise rejection).
    _mark_note_pending(conn, action["action_id"], None)
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
    # Removing a reaction takes back a REACTION decision. It must not take back
    # a button press: the button path stores the same "✅", so a leader who
    # clicked Done and later added-then-removed a ✅ on that message silently
    # reopened their own decided card — and the reopen is only a partial
    # inverse (it leaves decision_note, expires_at, premise_rejected and the
    # cleared management state behind).
    if (action.get("outcome") or {}).get("decided_via") == DECIDED_VIA_BUTTON:
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
    _mark_note_pending(conn, action["action_id"], body)
    return get_leader_action_by_message(source_message_id, conn=conn)


# ---------------------------------------------------------------------------
# Leader-note feedback loop (v7): async interpretation persistence + the two
# behaviour-changing effect setters (a timing hold on re-nomination, and a
# premise rejection). Interpretation runs off the delivery transaction, so these
# are called from the background interpreter thread with their own connection.
# ---------------------------------------------------------------------------


@managed_connection
def record_note_interpretation(
    action_id: int,
    *,
    status: str,
    effect: dict | None = None,
    note_hash: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Persist the outcome of an async note interpretation: the lifecycle status
    (``interpreted`` / ``failed`` / ``undone`` / ``none``), the effect blob
    (effect kind + params + the human ``reading`` echoed on the card + a
    prior-state snapshot for Undo), and the note hash that was interpreted."""
    conn.execute(
        "UPDATE leader_action_recommendations "
        "SET note_interpret_status = ?, note_interpret_json = ?, "
        "    note_interpret_note_hash = COALESCE(?, note_interpret_note_hash), "
        "    updated_at = ? "
        "WHERE action_id = ?",
        (
            (status or "").strip() or None,
            _json_dumps(effect) if effect is not None else None,
            note_hash,
            _db._utcnow(),
            int(action_id),
        ),
    )
    return get_leader_action_by_id(action_id, conn=conn)


@managed_connection
def set_leader_action_suppression(
    action_id: int,
    suppressed_until: str | None,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """Timing-hold effect: write ``expires_at`` (the re-nomination hold window).
    Returns the PRIOR ``expires_at`` so the interpreter can snapshot it for an
    exact Undo (restore the prior value, never NULL)."""
    row = conn.execute(
        "SELECT expires_at FROM leader_action_recommendations WHERE action_id = ?",
        (int(action_id),),
    ).fetchone()
    if row is None:
        return None
    prior = row["expires_at"]
    conn.execute(
        "UPDATE leader_action_recommendations SET expires_at = ?, updated_at = ? "
        "WHERE action_id = ?",
        (suppressed_until, _db._utcnow(), int(action_id)),
    )
    return prior


@managed_connection
def set_leader_action_premise(
    action_id: int,
    *,
    rejected: bool,
    fingerprint: str | None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Premise effect: set/clear ``premise_rejected`` + ``premise_fingerprint``.
    Returns the PRIOR values as a snapshot for Undo."""
    row = conn.execute(
        "SELECT premise_rejected, premise_fingerprint "
        "FROM leader_action_recommendations WHERE action_id = ?",
        (int(action_id),),
    ).fetchone()
    if row is None:
        return None
    prior = {
        "premise_rejected": bool(row["premise_rejected"]),
        "premise_fingerprint": row["premise_fingerprint"],
    }
    conn.execute(
        "UPDATE leader_action_recommendations "
        "SET premise_rejected = ?, premise_fingerprint = ?, updated_at = ? "
        "WHERE action_id = ?",
        (1 if rejected else 0, fingerprint, _db._utcnow(), int(action_id)),
    )
    return prior


@managed_connection
def set_leader_action_note_text(
    action_id: int,
    *,
    note: str | None,
    discord_user_id: str | int,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Replace a card's leader-note text (the Fix-reading flow) and re-flag it for
    interpretation. Works on open or decided cards."""
    body = " ".join((note or "").split()) or None
    now = _db._utcnow()
    conn.execute(
        "UPDATE leader_action_recommendations "
        "SET decision_note = ?, decision_note_at = ?, "
        "    decision_note_by_discord_user_id = ?, updated_at = ? "
        "WHERE action_id = ?",
        (body, now, str(discord_user_id), now, int(action_id)),
    )
    _mark_note_pending(conn, action_id, body)
    return get_leader_action_by_id(action_id, conn=conn)


@managed_connection
def list_interpreted_leader_actions(
    *,
    limit: int = 50,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Decided-but-interpreted cards whose Undo / Fix-reading buttons must be
    re-registered as persistent views on startup."""
    rows = conn.execute(
        "SELECT * FROM leader_action_recommendations "
        "WHERE note_interpret_status = 'interpreted' "
        "  AND source_message_id IS NOT NULL "
        "  AND status != 'proposed' "
        "ORDER BY updated_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [_row_to_action(row) for row in rows]


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
    return get_leader_action_by_key(action["action_key"], conn=conn)


# `evaluate_leader_action` measures an action by diffing the baseline captured
# at proposal time against state read RIGHT NOW. That comparison only means
# something close to the decision: an in_game_relay decided in June, evaluated
# today, would report the delta between June's war day and today's. Past this
# much time beyond its due point, an outcome is recorded as not-evaluated
# rather than given a fabricated measurement.
OUTCOME_EVALUATION_GRACE_HOURS = 168


def _unevaluated_outcome(action: dict, *, reason: str, now: str) -> dict:
    return {
        "evaluated_at": now,
        "action_type": action.get("action_type"),
        "status": action.get("status"),
        "pending_evaluation": False,
        "not_evaluated": reason,
    }


@managed_connection
def refresh_due_leader_action_outcomes(
    *,
    limit: int = 20,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Evaluate decided cards whose outcome is still pending and now due.

    The pending filter belongs in SQL. It used to run in Python AFTER
    `ORDER BY decided_at ASC LIMIT 20`, so the window was permanently pinned to
    the twenty oldest decided cards — all of them long since evaluated. Every
    run fetched the same twenty rows, skipped all twenty, and returned nothing.
    Measured on the live database when this was found: 132 decided cards, 105
    still carrying `pending_evaluation`, and zero pending among the oldest
    twenty. The job had never once evaluated an outcome on its own; the only
    thing that ever moved one was the manual `/relay status` admin path.
    """
    rows = conn.execute(
        "SELECT * FROM leader_action_recommendations "
        "WHERE status = ? AND decided_at IS NOT NULL "
        "AND (outcome_json IS NULL "
        "     OR json_extract(outcome_json, '$.pending_evaluation') = 1) "
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
        # Due-time stays in Python: `_parse_utc` tolerates both the Z-suffixed
        # and naive stamps this column carries, which a SQL string compare
        # would not.
        delay = _outcome_delay_hours(action.get("action_type"))
        due_at = decided_at + timedelta(hours=delay)
        if now < due_at:
            continue
        if now > due_at + timedelta(hours=OUTCOME_EVALUATION_GRACE_HOURS):
            conn.execute(
                "UPDATE leader_action_recommendations SET outcome_json = ?, updated_at = ? "
                "WHERE action_id = ?",
                (
                    _json_dumps(
                        _unevaluated_outcome(action, reason="window_passed", now=_db._utcnow())
                    ),
                    _db._utcnow(),
                    action["action_id"],
                ),
            )
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
    "has_recent_leader_action",
    "leader_action_decision_stats",
    "list_leader_actions",
    "record_leader_action_note_by_message",
    "refresh_due_leader_action_outcomes",
    "refresh_leader_action_outcome",
    "update_leader_action_copy_messages",
    "update_leader_action_message",
    "clear_leader_action_source_message",
    "POSTING_SENTINEL",
    "update_leader_action_copy_text",
    "upsert_leader_action_feedback_profile",
]
