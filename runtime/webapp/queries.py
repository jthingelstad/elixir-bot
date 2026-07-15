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
    except (TypeError, ValueError):
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
                        for m, v in sorted(
                            p["modes"].items(), key=lambda kv: -kv[1]["battles"]
                        )
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
        return _one(
            conn, "SELECT * FROM raw_api_payloads WHERE payload_id = ?", (payload_id,)
        )
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
            seasons.setdefault(
                sid, {"season_id": sid, "awards": [], "war_participants": n}
            )
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
                (
                    a["player_tag"]
                    for a in season["awards"]
                    if a["award_type"] == "free_pass"
                ),
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


def recognition_page(
    stream: str | None, suppressed_only: bool, q: str | None, limit: int = 150
) -> dict:
    conn = db.get_connection()
    try:
        where, params = ["1=1"], []
        if stream:
            where.append("l.stream = ?")
            params.append(stream)
        if q:
            where.append("l.recognition_key LIKE ?")
            params.append(f"%{q}%")
        rows = _rows(
            conn,
            f"""
            SELECT l.recognition_key, l.stream, l.score, l.claimed_at, l.intent_id,
                   l.event_refs_json
            FROM recognition_ledger l
            WHERE {" AND ".join(where)}
            ORDER BY l.claimed_at DESC LIMIT ?""",
            (*params, limit),
        )
        for r in rows:
            blob = _parse_json(r.pop("event_refs_json"), {})
            suppressed = blob.get("suppressed") if isinstance(blob, dict) else None
            r["suppressed_reason"] = (suppressed or {}).get("reason")
        if suppressed_only:
            rows = [r for r in rows if r["suppressed_reason"]]
        return {
            "claims": rows,
            "stream": stream or "",
            "q": q or "",
            "suppressed_only": suppressed_only,
        }
    finally:
        conn.close()


def recognition_detail(recognition_key: str) -> dict | None:
    conn = db.get_connection()
    try:
        claim = _one(
            conn,
            "SELECT * FROM recognition_ledger WHERE recognition_key = ?",
            (recognition_key,),
        )
        if claim is None:
            return None
        claim["refs"] = _parse_json(claim.get("event_refs_json"), {})
        return {"claim": claim}
    finally:
        conn.close()


def member_page(tag: str) -> dict | None:
    conn = db.get_connection()
    try:
        player = _one(conn, "SELECT * FROM players WHERE player_tag = ?", (tag,))
        if player is None:
            return None
        state = _one(
            conn, "SELECT * FROM player_current_state WHERE player_tag = ?", (tag,)
        )
        poll = _one(conn, "SELECT * FROM poll_state WHERE player_tag = ?", (tag,))
        management = _one(
            conn, "SELECT * FROM member_management WHERE player_tag = ?", (tag,)
        )
        if management:
            management["state"] = _parse_json(management.pop("state_json", None), {})
        membership = _one(
            conn,
            """
            SELECT joined_at, left_at, join_source FROM clan_memberships
            WHERE player_tag = ? ORDER BY joined_at DESC LIMIT 1""",
            (tag,),
        )
        claims = _rows(
            conn,
            """
            SELECT l.recognition_key, l.stream, l.score, l.claimed_at, l.intent_id,
                   l.event_refs_json
            FROM recognition_ledger l
            WHERE l.recognition_key LIKE ? ORDER BY l.claimed_at DESC LIMIT 50""",
            (f"%{tag}%",),
        )
        for c in claims:
            blob = _parse_json(c.pop("event_refs_json"), {})
            c["suppressed_reason"] = ((blob or {}).get("suppressed") or {}).get(
                "reason"
            )
        events = events_read.list_recent_events(
            days=30, subject_key=tag, limit=60, conn=conn
        )
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
        return {
            "player": player,
            "state": state,
            "poll": poll,
            "management": management,
            "membership": membership,
            "claims": claims,
            "events": events,
            "battles": battles,
            "attendance": attendance,
            "links": links,
            "aliases": aliases,
            "contact": contact,
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


def management_page() -> dict:
    conn = db.get_connection()
    try:
        rows = _rows(
            conn,
            """
            SELECT mm.*, p.current_name FROM member_management mm
            LEFT JOIN players p ON p.player_tag = mm.player_tag
            ORDER BY (mm.kick_state != 'none') DESC, (mm.promote_state != 'none') DESC,
                     (mm.demote_state != 'none') DESC, p.current_name LIMIT 200""",
        )
        for r in rows:
            r.pop("state_json", None)
        actions = leader_actions.list_leader_actions(
            status="proposed", limit=25, conn=conn
        )
        recent = leader_actions.list_leader_actions(limit=15, conn=conn)
        open_cases = cases.list_decision_cases(limit=25, conn=conn)  # open + deferred
        resolved_cases = cases.list_decision_cases(
            statuses=(cases.CASE_RESOLVED, cases.CASE_DISMISSED), limit=10, conn=conn
        )
        pending_revisits = revisits.list_pending_revisits(limit=25, conn=conn)
        decision_contract = management_capability.get_management_decisions(
            view="board", conn=conn
        )
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
            snapshot = war_capability.get_war_season_view(view="snapshot", conn=conn)[
                "data"
            ]
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
            for r in _rows(
                conn, "SELECT DISTINCT workflow FROM llm_calls ORDER BY workflow"
            )
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


def memories_page(
    kind: str | None, member: str | None, q: str | None, limit: int = 100
) -> dict:
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
                rows = [
                    r
                    for r in rows
                    if (r.get("member_tag") or "").upper() == canon.upper()
                ]
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
