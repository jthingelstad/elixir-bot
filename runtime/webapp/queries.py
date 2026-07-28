"""Read functions for the Observatory pages.

All synchronous; every function opens a fresh read connection via
db.get_connection() and closes it in finally — never the engine tick's
connection (handlers call these through asyncio.to_thread). One function per
page batches that page's queries onto one connection. Everything is LIMIT-ed
and must render sanely on an empty v5.1 DB.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

import db
from capabilities import management as management_capability
from capabilities import war as war_capability
from engine.clock import war_clock
from engine.tick import HOME_CLAN
from storage import cases, events_read, leader_actions, revisits, runtime_status

log = logging.getLogger("elixir.webapp.queries")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _parse_json(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except TypeError, ValueError:
        return default


# ------------------------------------------------------------- shared bits


def _war_clock_dict(conn) -> dict | None:
    """The live war clock, rebuilt from the stored race projection (the same
    adapter as engine.tick._current_clock — snake_case → CR-shaped keys)."""
    row = conn.execute(
        "SELECT payload_json, observed_at FROM state_baselines "
        "WHERE entity_kind='riverrace' AND entity_tag=? AND aspect='race'",
        (HOME_CLAN,),
    ).fetchone()
    if row is None:
        return None
    p = _parse_json(row["payload_json"], {}) or {}
    cr_shaped = {
        "periodType": p.get("period_type"),
        "periodIndex": p.get("period_index"),
        "sectionIndex": p.get("section_index"),
        "clan": {"tag": p.get("our_tag"), "fame": p.get("our_fame")},
    }
    clock = asdict(war_clock(cr_shaped, _now(), season_id=p.get("season_id")))
    clock["baseline_observed_at"] = row["observed_at"]
    return clock


def _latest_tick_from_status(statuses: dict) -> dict | None:
    state = statuses.get("engine_tick") or {}
    summary = _parse_json(state.get("last_summary"), None)
    if isinstance(summary, dict):
        summary["_persisted"] = True
        return summary
    return None


def _awareness_summary(conn) -> dict:
    activity = runtime_status.get_awareness_activity(limit=1, conn=conn)
    return {
        "thoughts_24h": conn.execute(
            "SELECT COUNT(*) FROM awareness_thoughts "
            "WHERE at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day')"
        ).fetchone()[0],
        "posts_24h": conn.execute(
            "SELECT COUNT(*) FROM awareness_posts "
            "WHERE posted_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day')"
        ).fetchone()[0],
        "failures_24h": conn.execute(
            "SELECT COUNT(*) FROM awareness_thoughts "
            "WHERE at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day') "
            "AND skipped_reason LIKE '⚠️ tick failed:%'"
        ).fetchone()[0],
        "latest_thought": (activity["thoughts"] or [None])[0],
        "latest_post": (activity["posts"] or [None])[0],
    }


def _poll_summary(conn) -> dict:
    temps = {
        r["temperature"]: r["n"]
        for r in conn.execute(
            """SELECT temperature, COUNT(*) AS n FROM poll_state ps
           WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                         WHERE cm.player_tag = ps.player_tag AND cm.left_at IS NULL)
           GROUP BY temperature"""
        ).fetchall()
    }
    oldest = _one(
        conn,
        """
        SELECT MIN(last_battlelog_poll) AS oldest_battlelog,
               MIN(last_profile_poll) AS oldest_profile FROM poll_state""",
    )
    return {"temperatures": temps, **(oldest or {})}


# ------------------------------------------------------------------ pages


def overview() -> dict:
    conn = db.get_connection()
    try:
        statuses = runtime_status.list_runtime_job_status(conn=conn)
        prompt_failures_24h = conn.execute(
            "SELECT COUNT(*) FROM prompt_failures WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now','-1 day')"
        ).fetchone()[0]
        job_errors = {
            name: state.get("last_error")
            for name, state in statuses.items()
            if state.get("last_error")
        }
        return {
            "statuses": statuses,
            "latest_tick": _latest_tick_from_status(statuses),
            "war_clock": _war_clock_dict(conn),
            "poll": _poll_summary(conn),
            "awareness": _awareness_summary(conn),
            "errors": {
                "job_errors": job_errors,
                "prompt_failures_24h": prompt_failures_24h,
            },
        }
    finally:
        conn.close()


def ticks_page() -> dict:
    conn = db.get_connection()
    try:
        cursors = _rows(
            conn,
            """
            SELECT consumer_key, scope_key, cursor_int, cursor_text, updated_at,
                   metadata_json
            FROM stream_cursors ORDER BY consumer_key, scope_key LIMIT 100""",
        )
        return {
            "statuses": runtime_status.list_runtime_job_status(conn=conn),
            "cursors": cursors,
        }
    finally:
        conn.close()


# Per-call USD cost, in micro-dollars (÷1e6 for USD). Extends the canonical
# storage/identity.py formula (Sonnet/Haiku weights per million) with an Opus
# branch — the intensive workflows run Opus, which the canonical formula prices
# at $0 (a pre-existing undercount worth unifying later).
_LLM_COST_CASE = """
    CASE
        WHEN model LIKE 'claude-opus%' OR model LIKE 'Codex-opus%'
        THEN COALESCE(prompt_tokens,0)*15 + COALESCE(cache_read_tokens,0)*1.5
             + COALESCE(cache_creation_tokens,0)*18.75 + COALESCE(completion_tokens,0)*75
        WHEN model LIKE 'claude-sonnet%' OR model LIKE 'Codex-sonnet%'
        THEN COALESCE(prompt_tokens,0)*3 + COALESCE(cache_read_tokens,0)*0.3
             + COALESCE(cache_creation_tokens,0)*3.75 + COALESCE(completion_tokens,0)*15
        WHEN model LIKE 'claude-haiku%' OR model LIKE 'Codex-haiku%'
        THEN COALESCE(prompt_tokens,0)*1 + COALESCE(cache_read_tokens,0)*0.1
             + COALESCE(cache_creation_tokens,0)*1.25 + COALESCE(completion_tokens,0)*5
        ELSE 0 END
"""


def llm_cost_page() -> dict:
    """LLM spend by workflow (7d + 30d) and by model — cost was tracked per call
    but never surfaced; only a single 7-day total existed in the status report."""
    conn = db.get_connection()

    def _by_workflow(days: int) -> list:
        return _rows(
            conn,
            f"""
            SELECT workflow,
                   COUNT(*) AS calls,
                   SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures,
                   SUM(COALESCE(total_tokens, 0)) AS tokens,
                   ROUND(COALESCE(SUM({_LLM_COST_CASE}), 0) / 1000000.0, 4) AS cost_usd
            FROM llm_calls
            WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
            GROUP BY workflow ORDER BY cost_usd DESC, calls DESC
            """,
            (f"-{days} days",),
        )

    try:
        wf_7d = _by_workflow(7)
        wf_30d = _by_workflow(30)
        by_model = _rows(
            conn,
            f"""
            SELECT model, COUNT(*) AS calls,
                   ROUND(COALESCE(SUM({_LLM_COST_CASE}), 0) / 1000000.0, 4) AS cost_usd
            FROM llm_calls
            WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')
            GROUP BY model ORDER BY cost_usd DESC
            """,
        )
        return {
            "workflows_7d": wf_7d,
            "workflows_30d": wf_30d,
            "by_model": by_model,
            "total_7d": round(sum(w["cost_usd"] or 0 for w in wf_7d), 2),
            "total_30d": round(sum(w["cost_usd"] or 0 for w in wf_30d), 2),
        }
    finally:
        conn.close()


def api_sentinel_page() -> dict:
    """API ingestion health: the admission ledger (every HTTP response's
    accept/reject verdict) and the schema-drift sentinel (first-seen CR API
    paths). Neither had a UI — schema drift was only visible via #leaders posts."""
    conn = db.get_connection()
    try:
        status_counts = {
            r["admission_status"]: r["n"]
            for r in _rows(
                conn,
                "SELECT admission_status, COUNT(*) n FROM api_observation_receipts "
                "GROUP BY admission_status ORDER BY n DESC",
            )
        }
        rejections = _rows(
            conn,
            "SELECT endpoint, entity_key, fetched_at, admission_status, "
            "admission_errors_json FROM api_observation_receipts "
            "WHERE admission_status = 'rejected' ORDER BY receipt_id DESC LIMIT 30",
        )
        for r in rejections:
            r["errors"] = _parse_json(r.get("admission_errors_json"), []) or []
        recent_paths = _rows(
            conn,
            "SELECT sentinel_type, endpoint, name, entity_key, first_seen_at, "
            "last_seen_at FROM api_sentinel_observations "
            "ORDER BY first_seen_at DESC, observation_id DESC LIMIT 40",
        )
        by_endpoint = _rows(
            conn,
            "SELECT endpoint, COUNT(*) n, MAX(last_seen_at) last_seen "
            "FROM api_sentinel_observations GROUP BY endpoint ORDER BY n DESC",
        )
        return {
            "status_counts": status_counts,
            "rejections": rejections,
            "recent_paths": recent_paths,
            "by_endpoint": by_endpoint,
            "total_paths": sum(e["n"] for e in by_endpoint),
        }
    finally:
        conn.close()


def _resolve_activity_defaults(value):
    """Resolve a schedule_config's RuntimeAttrRefs to their DEFAULTS (no
    runtime.app import — the display schedule; the live scheduler's next_run
    reflects any env overrides)."""
    from runtime.activities import RuntimeAttrRef

    if isinstance(value, RuntimeAttrRef):
        return value.default
    if isinstance(value, dict):
        return {k: _resolve_activity_defaults(v) for k, v in value.items()}
    return value


def activities_page() -> dict:
    """The scheduled-activity registry: every recurring job with its schedule,
    purpose, lane, and last-run outcome + next run. Previously only next-run
    times were visible (buried on Overview); ~18 jobs had no registry view."""
    from runtime import activities as activities_mod

    specs = []
    for act in activities_mod.list_registered_activities():
        resolved = {
            "schedule_kind": act.schedule_kind,
            "schedule_config": _resolve_activity_defaults(act.schedule_config),
            "active_window": _resolve_activity_defaults(act.active_window),
        }
        specs.append(
            {
                "activity_key": act.activity_key,
                "activity_role": act.activity_role,
                "owner_lane": act.owner_lane,
                "purpose": act.purpose,
                "job_id": act.job_id,
                "schedule": activities_mod._format_schedule_description(resolved),
                "delivery_targets": list(act.delivery_targets),
                "manual_trigger_allowed": act.manual_trigger_allowed,
                "enabled_by_default": act.enabled_by_default,
            }
        )

    next_runs: dict[str, str] = {}
    try:
        from runtime.helpers._common import _job_next_runs

        for item in _job_next_runs():
            next_runs[item["id"]] = item["next_run"]
            next_runs[item["id"].replace("-", "_")] = item["next_run"]
    except Exception:
        log.debug("activities_page: scheduler next-runs unavailable", exc_info=True)

    conn = db.get_connection()
    try:
        statuses = runtime_status.list_runtime_job_status(conn=conn)
    finally:
        conn.close()

    rows = []
    for s in specs:
        skey = (s["activity_key"] or "").replace("-", "_")
        st = statuses.get(skey) or statuses.get(s["job_id"] or "") or {}
        rows.append(
            {
                **s,
                "run_count": st.get("run_count"),
                "success_count": st.get("success_count"),
                "failure_count": st.get("failure_count"),
                "last_error": st.get("last_error"),
                "last_success_at": st.get("last_success_at"),
                "last_finished_at": st.get("last_finished_at"),
                "last_summary": st.get("last_summary"),
                "running": bool(st.get("running")),
                "next_run": next_runs.get(s["job_id"] or "") or next_runs.get(skey),
            }
        )
    return {"activities": rows}


def _thought_outcome(row: dict) -> str:
    if (row.get("skipped_reason") or "").startswith("⚠️ tick failed"):
        return "failed"
    return "silence" if row.get("chose_silence") else "posted"


def _thought_tier(model: str | None) -> str:
    """The gate tier that decided this loop: gate:skip/gate:triage → skip/triage;
    a null model means the Sonnet brain deliberated."""
    if not model:
        return "deliberate"
    return str(model).replace("gate:", "")


def awareness_page() -> dict:
    """The awareness loop's own view: recent loops with their gate tier, the
    signals they saw, the decision, and the posts that resulted. This is the
    hourly brain — previously visible only as a buried card on Overview."""
    conn = db.get_connection()
    try:
        raw = _rows(
            conn,
            """
            SELECT thought_id, loop_number, at, chose_silence, post_count,
                   model, skipped_reason, read_json, tool_trace_json
            FROM awareness_thoughts ORDER BY loop_number DESC LIMIT 60""",
        )
        loops = []
        for t in raw:
            read = _parse_json(t.get("read_json"), {}) or {}
            sbl = read.get("signals_by_lane") or {}
            hard = read.get("hard_post_signals") or []
            trace = _parse_json(t.get("tool_trace_json"), []) or []
            pulse = read.get("posting_pulse") or {}
            loops.append(
                {
                    "loop_number": t.get("loop_number"),
                    "at": t.get("at"),
                    "outcome": _thought_outcome(t),
                    "post_count": t.get("post_count") or 0,
                    "tier": _thought_tier(t.get("model")),
                    "signal_counts": {lane: len(items) for lane, items in sbl.items() if items},
                    "signal_total": sum(len(v) for v in sbl.values() if v),
                    "hard_posts": [
                        h.get("event_type")
                        for h in hard
                        if isinstance(h, dict) and h.get("event_type")
                    ],
                    "degraded": read.get("_degraded") or [],
                    "tool_calls": len(trace),
                    "quiet_stretch": bool(pulse.get("is_quiet_stretch")),
                    "reason": (t.get("skipped_reason") or "")[:300],
                }
            )
        posts = _rows(
            conn,
            """
            SELECT lane, content_preview, covers_json, loop_number, posted_at,
                   discord_message_id
            FROM awareness_posts ORDER BY posted_at DESC LIMIT 20""",
        )
        for p in posts:
            p["covers"] = _parse_json(p.get("covers_json"), []) or []
        return {
            "summary": _awareness_summary(conn),
            "loops": loops,
            "posts": posts,
        }
    finally:
        conn.close()


def members_page() -> dict:
    conn = db.get_connection()
    try:
        rows = _rows(
            conn,
            """
            SELECT p.player_tag, p.current_name,
                   pcs.role, pcs.trophies,
                   ps.temperature, ps.last_battle_seen,
                   mm.sustained_donor, mm.war_reliable, mm.battle_active,
                   mm.promote_state, mm.demote_state, mm.kick_state,
                   mm.tenure_days
            FROM clan_memberships cm
            JOIN players p ON p.player_tag = cm.player_tag
            LEFT JOIN player_current_state pcs ON pcs.player_tag = p.player_tag
            LEFT JOIN poll_state ps ON ps.player_tag = p.player_tag
            LEFT JOIN member_management mm ON mm.player_tag = p.player_tag
            WHERE cm.left_at IS NULL
            ORDER BY (COALESCE(mm.kick_state,'none') != 'none') DESC,
                     (COALESCE(mm.promote_state,'none') != 'none') DESC,
                     (COALESCE(mm.demote_state,'none') != 'none') DESC,
                     p.current_name COLLATE NOCASE
            LIMIT 200""",
        )
        # Playstyle identity chip + mode mix (ranked-and-profiles.md §2.3) —
        # a computed read over rollups per member; profile failures are blank
        # chips, never a broken page.
        from engine.profiles import player_mode_profile

        for r in rows:
            try:
                p = player_mode_profile(conn, r["player_tag"])
                r["identity"] = p["identity"]
                r["mode_mix"] = (
                    ", ".join(
                        f"{m} {v['battles']}"
                        for m, v in sorted(p["modes"].items(), key=lambda kv: -kv[1]["battles"])
                    )
                    or None
                )
            except Exception:
                log.debug(
                    "member mode profile unavailable tag=%s",
                    r.get("player_tag"),
                    exc_info=True,
                )
                r["identity"] = None
                r["mode_mix"] = None
        leavers = _rows(
            conn,
            """
            SELECT p.player_tag, p.current_name, cm.joined_at, cm.left_at,
                   cm.leave_source
            FROM clan_memberships cm
            JOIN players p ON p.player_tag = cm.player_tag
            WHERE cm.left_at IS NOT NULL
            ORDER BY cm.left_at DESC LIMIT 10""",
        )
        return {"rows": rows, "leavers": leavers}
    finally:
        conn.close()


def streams_page(stream: str | None, event_type: str | None, limit: int = 100) -> dict:
    conn = db.get_connection()
    try:
        if stream == "battle":
            # The battle stream's log IS battle_events (no event_type column —
            # the events reader covers only the emitted streams). Present
            # battles in the row shape the template renders. Live fix
            # 2026-07-04: the Battle filter rendered empty.
            rows = _rows(
                conn,
                """
                SELECT battle_time, player_tag, mode_group, outcome,
                       trophy_change, arena_name, game_mode_name, is_war,
                       season_id, section_index
                FROM battle_events
                WHERE (? = '' OR mode_group = ?)
                ORDER BY rowid DESC LIMIT ?""",
                (event_type or "", event_type or "", limit),
            )
            events = [
                {
                    "stream": "battle",
                    "event_type": r["mode_group"] or "battle",
                    "subject": r["player_tag"],
                    "observed_at": r["battle_time"],
                    "payload": {
                        "outcome": r["outcome"],
                        "trophy_change": r["trophy_change"],
                        "arena": r["arena_name"],
                        "mode": r["game_mode_name"],
                        "war": bool(r["is_war"]),
                        "season": r["season_id"],
                        "week": r["section_index"],
                    },
                }
                for r in rows
            ]
        else:
            events = events_read.list_recent_events(
                days=30, event_type=event_type or None, limit=limit, conn=conn
            )
            if stream:
                events = [e for e in events if e.get("stream") == stream]
        windows = events_read.summarize_event_windows(conn=conn)
        baselines = _rows(
            conn,
            """
            SELECT entity_kind, aspect, COUNT(*) AS entities,
                   MIN(observed_at) AS oldest, MAX(observed_at) AS newest
            FROM state_baselines GROUP BY entity_kind, aspect ORDER BY entity_kind, aspect""",
        )
        raw = _rows(
            conn,
            """
            SELECT p.payload_id, p.endpoint, p.entity_key, p.fetched_at,
                   p.last_fetched_at, COUNT(r.receipt_id) AS receipt_count
            FROM raw_api_payloads p
            LEFT JOIN api_observation_receipts r ON r.payload_id = p.payload_id
            GROUP BY p.payload_id
            ORDER BY COALESCE(p.last_fetched_at, p.fetched_at) DESC LIMIT 30""",
        )
        baseline_rows = _rows(
            conn,
            """
            SELECT entity_kind, entity_tag, aspect, observed_at, prev_observed_at
            FROM state_baselines ORDER BY entity_kind, aspect, entity_tag LIMIT 100""",
        )
        return {
            "events": events,
            "windows": windows,
            "baselines": baselines,
            "baseline_rows": baseline_rows,
            "raw_payloads": raw,
            "stream": stream or "",
            "event_type": event_type or "",
        }
    finally:
        conn.close()


def raw_payload(payload_id: int) -> dict | None:
    conn = db.get_connection()
    try:
        return _one(conn, "SELECT * FROM raw_api_payloads WHERE payload_id = ?", (payload_id,))
    finally:
        conn.close()


def baseline_detail(entity_kind: str, entity_tag: str, aspect: str) -> dict | None:
    conn = db.get_connection()
    try:
        return _one(
            conn,
            """
            SELECT entity_kind, entity_tag, aspect, payload_json, payload_hash,
                   observed_at, prev_observed_at
            FROM state_baselines
            WHERE entity_kind = ? AND entity_tag = ? AND aspect = ?""",
            (entity_kind, entity_tag, aspect),
        )
    finally:
        conn.close()


def awards_page() -> dict:
    conn = db.get_connection()
    try:
        rows = _rows(
            conn,
            """
            SELECT a.award_type, a.season_id, a.player_tag, p.current_name,
                   a.rank, a.metric_value, a.metric_unit, a.awarded_at
            FROM awards a LEFT JOIN players p ON p.player_tag = a.player_tag
            WHERE a.award_type != 'war_participant'
            ORDER BY a.season_id DESC, a.award_type, a.rank LIMIT 300""",
        )
        participants = {
            r["season_id"]: r["n"]
            for r in conn.execute(
                """SELECT season_id, COUNT(*) AS n FROM awards
               WHERE award_type = 'war_participant' GROUP BY season_id"""
            ).fetchall()
        }
        seasons: dict[int, dict] = {}
        for r in rows:
            season = seasons.setdefault(
                r["season_id"],
                {
                    "season_id": r["season_id"],
                    "awards": [],
                    "war_participants": participants.get(r["season_id"], 0),
                },
            )
            season["awards"].append(r)
        for sid, n in participants.items():  # seasons with only participant rows
            seasons.setdefault(sid, {"season_id": sid, "awards": [], "war_participants": n})
        # Rotation visibility (Q2): flag seasons where the free pass diverged
        # from the rank-1 war champ.
        for season in seasons.values():
            champ = next(
                (
                    a["player_tag"]
                    for a in season["awards"]
                    if a["award_type"] == "war_champ" and a["rank"] == 1
                ),
                None,
            )
            fp = next(
                (a["player_tag"] for a in season["awards"] if a["award_type"] == "free_pass"),
                None,
            )
            season["rotation_diverged"] = bool(champ and fp and champ != fp)
        season_rows = _rows(
            conn,
            """
            SELECT season_id, started_at, ended_at, final_rank, weeks,
                   war_champ_tag, free_pass_tag
            FROM war_seasons ORDER BY season_id DESC LIMIT 20""",
        )
        return {
            "seasons": sorted(seasons.values(), key=lambda s: -s["season_id"]),
            "war_seasons": season_rows,
        }
    finally:
        conn.close()


def member_page(tag: str) -> dict | None:
    conn = db.get_connection()
    try:
        player = _one(conn, "SELECT * FROM players WHERE player_tag = ?", (tag,))
        if player is None:
            return None
        state = _one(conn, "SELECT * FROM player_current_state WHERE player_tag = ?", (tag,))
        poll = _one(conn, "SELECT * FROM poll_state WHERE player_tag = ?", (tag,))
        management = _one(conn, "SELECT * FROM member_management WHERE player_tag = ?", (tag,))
        if management:
            management["state"] = _parse_json(management.pop("state_json", None), {})
        membership = _one(
            conn,
            """
            SELECT joined_at, left_at, join_source FROM clan_memberships
            WHERE player_tag = ? ORDER BY joined_at DESC LIMIT 1""",
            (tag,),
        )
        events = events_read.list_recent_events(days=30, subject_key=tag, limit=60, conn=conn)
        battles = _rows(
            conn,
            """
            SELECT battle_time, game_mode_name, outcome, trophy_change, arena_name,
                   is_war, season_id, section_index
            FROM battle_events WHERE player_tag = ?
            ORDER BY battle_time DESC LIMIT 25""",
            (tag,),
        )
        attendance = _rows(
            conn,
            """
            SELECT season_id, section_index, war_day_index, decks_used, decks_available,
                   fame_delta, observed_at
            FROM war_attendance_days WHERE player_tag = ?
            ORDER BY season_id DESC, section_index DESC, war_day_index DESC LIMIT 20""",
            (tag,),
        )
        links = _rows(
            conn,
            """
            SELECT dl.discord_user_id, du.display_name, du.username,
                   dl.confidence, dl.source, dl.linked_at, dl.is_primary
            FROM discord_links dl
            LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id
            WHERE dl.player_tag = ? ORDER BY dl.is_primary DESC, dl.linked_at""",
            (tag,),
        )
        aliases = _rows(
            conn,
            """
            SELECT alias, source, observed_at FROM player_aliases
            WHERE player_tag = ? ORDER BY observed_at DESC LIMIT 15""",
            (tag,),
        )
        contact = (
            _one(
                conn,
                """
            SELECT email, email_verified_at, email_source FROM player_metadata
            WHERE player_tag = ?""",
                (tag,),
            )
            or {}
        )
        # Leadership trail for this member: every card that named them, from
        # nomination through the leader's decision (kick/promote/demote/departure).
        leader_actions = _rows(
            conn,
            """
            SELECT action_type, objective, status, rationale, proposed_at,
                   decided_at, decision_emoji, decision_note
            FROM leader_action_recommendations
            WHERE target_player_tag = ?
            ORDER BY proposed_at DESC LIMIT 25""",
            (tag,),
        )
        return {
            "player": player,
            "state": state,
            "poll": poll,
            "management": management,
            "membership": membership,
            "events": events,
            "battles": battles,
            "attendance": attendance,
            "links": links,
            "aliases": aliases,
            "contact": contact,
            "leader_actions": leader_actions,
        }
    finally:
        conn.close()


def polling_page() -> dict:
    from engine import polling as engine_polling

    conn = db.get_connection()
    try:
        rows = _rows(
            conn,
            """
            SELECT ps.*, p.current_name,
                   EXISTS (SELECT 1 FROM clan_memberships cm
                           WHERE cm.player_tag = ps.player_tag AND cm.left_at IS NULL)
                       AS is_member
            FROM poll_state ps LEFT JOIN players p ON p.player_tag = ps.player_tag
            ORDER BY ps.heat DESC, ps.player_tag LIMIT 200""",
        )
        # Read-only preview of what the next tick would poll (pure SELECTs;
        # never commit, never note_polled/decay_all on this connection).
        now_iso = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            plan = engine_polling.plan(conn, now_iso)
        except Exception:
            log.debug("polling plan preview unavailable", exc_info=True)
            plan = []
        names = {r["player_tag"]: (r.get("current_name") or "") for r in rows}
        plan_rows = [
            {"endpoint": endpoint, "player_tag": tag, "name": names.get(tag, "")}
            for endpoint, tag in plan
        ]
        return {"rows": rows, "plan": plan_rows, "summary": _poll_summary(conn)}
    finally:
        conn.close()


def command_page() -> dict:
    """Operator home: what needs your decision, who needs attention, the clan at a
    glance, and what Elixir's been saying — organized around running the clan, not
    Elixir's subsystems. Reuses the same sources as the deeper pages."""
    conn = db.get_connection()
    try:
        open_actions = leader_actions.list_leader_actions(status="proposed", limit=25, conn=conn)
        open_cases = cases.list_decision_cases(limit=25, conn=conn)
        pending_revisits = revisits.list_pending_revisits(limit=15, conn=conn)

        attention = _rows(
            conn,
            """
            SELECT p.current_name AS name, mm.player_tag,
                   mm.kick_state, mm.promote_state, mm.demote_state
            FROM member_management mm
            LEFT JOIN players p ON p.player_tag = mm.player_tag
            -- Current members only: kick/promote/demote state freezes at whatever
            -- it held just before someone leaves and nothing resets it on
            -- departure, so ex-members otherwise sit at the TOP of this panel
            -- (the kick_state ordering below promotes them).
            WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                           WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL)
              AND (mm.kick_state != 'none'
                   OR mm.promote_state IN ('eligible', 'recommended')
                   OR mm.demote_state IN ('eligible', 'recommended'))
            ORDER BY (mm.kick_state != 'none') DESC,
                     (mm.promote_state = 'recommended') DESC,
                     p.current_name COLLATE NOCASE
            LIMIT 30
            """,
        )

        # Use the same rich war-season snapshot the awareness read uses (day,
        # rank, weekly fame, standings) rather than the thin clock/week-clans path.
        war_state = {}
        war_standings = []
        try:
            snap = war_capability.get_war_season_view(view="snapshot", conn=conn)["data"]
            war_state = snap.get("state") or {}
            race = war_state.get("race") or {}
            war_standings = [
                {
                    "rank": s.get("rank"),
                    "name": s.get("clan_name"),
                    "fame": s.get("fame"),
                }
                for s in (race.get("race_standings") or [])[:5]
            ]
        except Exception:
            log.debug("command war snapshot unavailable", exc_info=True)

        roster_count = (
            _one(
                conn,
                "SELECT COUNT(*) AS n FROM clan_memberships WHERE left_at IS NULL",
            )
            or {}
        ).get("n")
        recent_posts = _rows(
            conn,
            "SELECT lane, content_preview, posted_at FROM awareness_posts "
            "ORDER BY posted_at DESC LIMIT 6",
        )
        open_incidents = (
            _one(
                conn,
                "SELECT COUNT(*) AS n FROM runtime_incidents WHERE resolved_at IS NULL",
            )
            or {}
        ).get("n")

        outreach_counts = {
            row["status"]: row["n"]
            for row in _rows(
                conn,
                "SELECT status, COUNT(*) AS n FROM member_outreach GROUP BY status",
            )
        }
        outreach_recent = _rows(
            conn,
            "SELECT mo.status, mo.updated_at, p.current_name AS name "
            "FROM member_outreach mo LEFT JOIN players p ON p.player_tag = mo.player_tag "
            "ORDER BY mo.updated_at DESC LIMIT 6",
        )

        return {
            "open_actions": open_actions,
            "open_cases": open_cases,
            "pending_revisits": pending_revisits,
            "attention": attention,
            "war_state": war_state,
            "war_standings": war_standings,
            "roster_count": roster_count,
            "recent_posts": recent_posts,
            "open_incidents": open_incidents,
            "outreach_counts": outreach_counts,
            "outreach_total": sum(outreach_counts.values()),
            "outreach_recent": outreach_recent,
        }
    finally:
        conn.close()


def management_page() -> dict:
    conn = db.get_connection()
    try:
        rows = _rows(
            conn,
            """
            SELECT mm.*, p.current_name FROM member_management mm
            LEFT JOIN players p ON p.player_tag = mm.player_tag
            -- Current members only — see the attention-panel note above: stale
            -- kick_state on departed members would lead this page.
            WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                           WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL)
            ORDER BY (mm.kick_state != 'none') DESC, (mm.promote_state != 'none') DESC,
                     (mm.demote_state != 'none') DESC, p.current_name LIMIT 200""",
        )
        for r in rows:
            r.pop("state_json", None)
        actions = leader_actions.list_leader_actions(status="proposed", limit=25, conn=conn)
        recent = leader_actions.list_leader_actions(limit=15, conn=conn)
        open_cases = cases.list_decision_cases(limit=25, conn=conn)  # open + deferred
        resolved_cases = cases.list_decision_cases(
            statuses=(cases.CASE_RESOLVED, cases.CASE_DISMISSED), limit=10, conn=conn
        )
        pending_revisits = revisits.list_pending_revisits(limit=25, conn=conn)
        decision_contract = management_capability.get_management_decisions(view="board", conn=conn)
        return {
            "rows": rows,
            "open_actions": actions,
            "recent_actions": recent,
            "open_cases": open_cases,
            "resolved_cases": resolved_cases,
            "pending_revisits": pending_revisits,
            "decision_contract": decision_contract,
        }
    finally:
        conn.close()


def war_page() -> dict:
    conn = db.get_connection()
    try:
        clock = _war_clock_dict(conn)
        snapshot = None
        try:
            snapshot = war_capability.get_war_season_view(view="snapshot", conn=conn)["data"]
        except Exception:
            log.debug("war snapshot unavailable", exc_info=True)
            snapshot = None
        season_id = (clock or {}).get("season_id")
        section_index = (clock or {}).get("section_index")
        standings = participation = attendance = []
        if season_id is not None and section_index is not None:
            standings = _rows(
                conn,
                """
                SELECT wwc.clan_tag, c.name, wwc.fame, wwc.rank, wwc.observed_at
                FROM war_week_clans wwc LEFT JOIN clans c ON c.clan_tag = wwc.clan_tag
                WHERE wwc.season_id = ? AND wwc.section_index = ?
                ORDER BY wwc.fame DESC""",
                (season_id, section_index),
            )
            participation = _rows(
                conn,
                """
                SELECT wp.player_tag, p.current_name, wp.fame, wp.decks_used,
                       wp.decks_used_today, wp.boat_attacks, wp.observed_at
                FROM war_participation wp LEFT JOIN players p ON p.player_tag = wp.player_tag
                WHERE wp.season_id = ? AND wp.section_index = ?
                ORDER BY wp.fame DESC LIMIT 60""",
                (season_id, section_index),
            )
            attendance = _rows(
                conn,
                """
                SELECT war_day_index, COUNT(*) AS players,
                       SUM(decks_used) AS decks_used, SUM(decks_available) AS decks_available
                FROM war_attendance_days
                WHERE season_id = ? AND section_index = ?
                GROUP BY war_day_index ORDER BY war_day_index""",
                (season_id, section_index),
            )
        seasons = _rows(
            conn,
            """
            SELECT season_id, started_at, ended_at, final_rank, weeks,
                   war_champ_tag, free_pass_tag
            FROM war_seasons ORDER BY season_id DESC LIMIT 8""",
        )
        return {
            "clock": clock,
            "snapshot": snapshot,
            "standings": standings,
            "participation": participation,
            "attendance": attendance,
            "seasons": seasons,
        }
    finally:
        conn.close()


def llm_page(workflow: str | None = None, limit: int = 50) -> dict:
    conn = db.get_connection()
    try:
        where = "WHERE workflow = ? " if workflow else ""
        params = (workflow, limit) if workflow else (limit,)
        calls = _rows(
            conn,
            f"""
            SELECT call_id, recorded_at, workflow, model, ok, error, duration_ms,
                   prompt_tokens, completion_tokens, total_tokens,
                   cache_creation_tokens, cache_read_tokens
            FROM llm_calls {where}ORDER BY call_id DESC LIMIT ?""",
            params,
        )
        workflows = [
            r["workflow"]
            for r in _rows(conn, "SELECT DISTINCT workflow FROM llm_calls ORDER BY workflow")
        ]
        failures = _rows(
            conn,
            """
            SELECT recorded_at, workflow, failure_type, failure_stage, channel_name,
                   substr(COALESCE(question,''),1,140) AS question,
                   substr(COALESCE(detail,''),1,200) AS detail
            FROM prompt_failures ORDER BY failure_id DESC LIMIT 25""",
        )
        feedback = _rows(
            conn,
            """
            SELECT recorded_at, workflow, channel_name, feedback_value,
                   substr(COALESCE(question,''),1,120) AS question,
                   substr(COALESCE(response_preview,''),1,160) AS response_preview
            FROM prompt_feedback ORDER BY prompt_feedback_id DESC LIMIT 20""",
        )
        suggestions = _rows(
            conn,
            """
            SELECT created_at, category, status, severity, title,
                   github_issue_url
            FROM elixir_improvement_suggestions
            ORDER BY suggestion_id DESC LIMIT 15""",
        )
        return {
            "calls": calls,
            "failures": failures,
            "feedback": feedback,
            "suggestions": suggestions,
            "workflows": workflows,
            "active_workflow": workflow or "",
        }
    finally:
        conn.close()


def memories_page(kind: str | None, member: str | None, q: str | None, limit: int = 100) -> dict:
    """The memory half of "see what Elixir knows" (memory.md M6): browse +
    FTS search over the v5.1 memories store, kind/member filters."""
    import memory_store

    conn = db.get_connection()
    try:
        memory_store.ensure_memory_schema(conn)
        if q:
            results = memory_store.search_memories(
                q,
                viewer_scope="leadership",
                filters={"kind": kind} if kind else None,
                limit=limit,
                conn=conn,
            )
            rows = [dict(r.memory, rank_score=round(r.rank_score, 3)) for r in results]
            if member:
                canon = member if member.startswith("#") else f"#{member}"
                rows = [r for r in rows if (r.get("member_tag") or "").upper() == canon.upper()]
        else:
            filters = {}
            if kind:
                filters["kind"] = kind
            if member:
                filters["member_tag"] = member
            rows = memory_store.list_memories(
                viewer_scope="leadership",
                filters=filters or None,
                limit=limit,
                conn=conn,
            )
        counts = _rows(
            conn,
            """
            SELECT kind, COUNT(*) AS n,
                   SUM(CASE WHEN retired_at IS NOT NULL THEN 1 ELSE 0 END) AS retired
            FROM memories GROUP BY kind ORDER BY n DESC""",
        )
        total = _one(conn, "SELECT COUNT(*) AS n FROM memories")
        return {
            "memories": rows,
            "counts": counts,
            "total": (total or {}).get("n", 0),
            "kind": kind or "",
            "member": member or "",
            "q": q or "",
        }
    finally:
        conn.close()


def incidents_page(limit: int = 200) -> dict:
    """The incident ledger (confidence plan §1): fail-soft failures captured
    with tracebacks, unresolved first."""
    conn = db.get_connection()
    try:
        from storage.incidents import ensure_incidents_schema

        ensure_incidents_schema(conn)
        rows = _rows(
            conn,
            """
            SELECT incident_id, at, component, severity, summary, detail,
                   context_json, resolved_at
            FROM runtime_incidents ORDER BY (resolved_at IS NOT NULL), at DESC
            LIMIT ?""",
            (limit,),
        )
        open_count = conn.execute(
            "SELECT COUNT(*) FROM runtime_incidents WHERE resolved_at IS NULL"
        ).fetchone()[0]
        by_component = _rows(
            conn,
            """
            SELECT component, COUNT(*) n FROM runtime_incidents
            WHERE resolved_at IS NULL GROUP BY component ORDER BY n DESC""",
        )
        return {
            "incidents": rows,
            "open_count": open_count,
            "by_component": by_component,
        }
    finally:
        conn.close()


def editorial_page(limit: int = 50) -> dict:
    """The active editorial rubric and retrospective quality reports."""
    conn = db.get_connection()
    try:
        rubric = _rows(
            conn,
            """
            SELECT m.memory_id, m.title, m.body, m.confidence, m.created_by,
                   m.created_at, m.retired_at,
                   (SELECT group_concat(tag, ' ') FROM memory_tags t
                    WHERE t.memory_id = m.memory_id) AS tags
            FROM memories m
            WHERE m.memory_id IN (SELECT memory_id FROM memory_tags WHERE tag = 'editorial')
              AND NOT EXISTS (SELECT 1 FROM memory_tags t2
                              WHERE t2.memory_id = m.memory_id AND t2.tag = 'weekly-review')
            ORDER BY m.updated_at DESC LIMIT 100""",
        )
        for r in rubric:
            tags = set((r.get("tags") or "").split())
            r["kind_label"] = (
                "anti-pattern"
                if "anti-pattern" in tags
                else "exemplar"
                if "exemplar" in tags
                else "note"
            )
            r["is_candidate"] = "candidate" in tags or "proposed" in tags
        reports = _rows(
            conn,
            """
            SELECT m.memory_id, m.title, m.body, m.created_at
            FROM memories m
            JOIN memory_tags t ON t.memory_id = m.memory_id AND t.tag = 'weekly-review'
            WHERE m.memory_id IN (SELECT memory_id FROM memory_tags WHERE tag = 'editorial')
            ORDER BY m.created_at DESC LIMIT 10""",
        )
        return {"rubric": rubric, "reports": reports}
    finally:
        conn.close()


# ------------------------------------------------------------- llm drill-down


def llm_call_detail(call_id: int) -> dict | None:
    """Full detail for one LLM call — metadata + the captured prompt (system,
    messages, tools) and response (text, tool calls). The blobs are None once
    pruned (LLM_PROMPT_RETENTION_DAYS); the row survives for cost analysis."""
    detail = db.get_llm_call(call_id)
    if detail is None:
        return None
    detail["prompt"] = detail.get("prompt") or None
    detail["response"] = detail.get("response") or None
    return detail
