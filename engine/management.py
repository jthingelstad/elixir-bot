"""Clan-management deterministic core (docs/v5.1/management.md — ratified
2026-07-03). Layer-1 sustained-signal evaluators + Layer-2 candidacy machines.

The LLM reads states, never metrics-to-judgment (management.md §1). The tick
runs `run_tick_evaluators` (kick path, continuous); the Monday review runs
`run_weekly_review` (the ONLY place the weekly grain rolls — runtime.md §2
step 5 / §3).

State internals (qualifying-week history, miss counters) persist in
member_management.state_json so auto-withdraw and "why is X eligible?" are
answerable (management.md §4).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from engine.db import utcnow

# management.md §5 — ratified defaults (move into CLAN.md at the cut).
DONOR_WEEK_MIN = 50
WAR_QUALIFY_RATE = 0.75
BATTLE_DAYS_MIN = 8
PROMOTE_TENURE_MIN = 28
PROMOTE_QUALIFYING_WEEKS = 4
DEMOTE_WEEKS = 4
KICK_CONFIRM_DAYS = 7
NEW_MEMBER_GRACE = 14
KICK_WATCH_DAYS = 3          # CLAN.md inactivity_days
HOLD_WINDOW = 4              # Layer-1: 3-of-4 holds, 1-of-4 lapses
HOLD_NEED = 3
LAPSE_MAX = 1

ELDER_PLUS = ("elder", "coLeader", "leader")


def _parse_ts(value: str) -> datetime:
    """Delegates to the normalizer's single parser (engine/normalize.py)."""
    from engine.normalize import parse_cr_time

    return parse_cr_time(value)


def _state(row) -> dict:
    try:
        return json.loads(row["state_json"] or "{}")
    except (TypeError, ValueError):
        return {}


# --------------------------------------------------------- Layer 1 (§2)

def advance_layer1(current: str, history: list) -> str:
    """One shared machine. `history` = qualifying flags for recent closed
    weeks, newest last; None entries (skipped weeks, e.g. training-only war
    weeks) are excluded from the window. management.md §2."""
    window = [h for h in history if h is not None][-HOLD_WINDOW:]
    qualifying = sum(1 for h in window if h)
    latest = window[-1] if window else None
    if current in (None, "", "none"):
        return "building" if latest else "none"
    if current == "building":
        if len(window) >= HOLD_WINDOW and qualifying >= HOLD_NEED:
            return "holding"
        return "building"
    if current == "holding":
        if len(window) >= HOLD_WINDOW and qualifying <= LAPSE_MAX:
            return "lapsed"
        return "holding"
    if current == "lapsed":
        return "building" if latest else "lapsed"
    return current


def _week_qualifies_donor(conn, tag, week_anchor: str):
    """Donations of the closed week: the frozen Sunday row before week_anchor."""
    row = conn.execute(
        """SELECT donations_week FROM player_daily_metrics
           WHERE player_tag = ? AND metric_date < ?
             AND strftime('%w', metric_date) = '0' AND donations_week IS NOT NULL
           ORDER BY metric_date DESC LIMIT 1""",
        (tag, week_anchor),
    ).fetchone()
    if row is None:
        return None  # no data — skip, don't fail
    return (row["donations_week"] or 0) >= DONOR_WEEK_MIN


def _week_qualifies_war(conn, tag, week_anchor: str):
    """Attendance across the closed week's finalized battle days
    (war_attendance_days; runtime.md §3 finalization). Training-only weeks
    (no rows clan-wide) skip — management.md §2."""
    week_start = (
        datetime.fromisoformat(week_anchor) - timedelta(days=7)
    ).date().isoformat()
    clan_days = conn.execute(
        "SELECT COUNT(*) FROM war_attendance_days WHERE observed_at >= ? AND observed_at < ?",
        (week_start, week_anchor),
    ).fetchone()[0]
    if not clan_days:
        return None  # no battle days observed that week — skip
    row = conn.execute(
        """SELECT SUM(decks_used) AS used, SUM(decks_available) AS avail
           FROM war_attendance_days
           WHERE player_tag = ? AND observed_at >= ? AND observed_at < ?""",
        (tag, week_start, week_anchor),
    ).fetchone()
    if not row or not row["avail"]:
        return False  # war ran; this member showed no decks
    return (row["used"] / row["avail"]) >= WAR_QUALIFY_RATE


def _week_qualifies_battle(conn, tag, week_anchor: str):
    row = conn.execute(
        "SELECT battle_days_last_28 FROM member_management WHERE player_tag = ?",
        (tag,),
    ).fetchone()
    if row is None or row["battle_days_last_28"] is None:
        return None
    return row["battle_days_last_28"] >= BATTLE_DAYS_MIN


# ------------------------------------------------------ kick path (§3.3)

def run_tick_evaluators(conn, now: str | None = None) -> list[dict]:
    """Continuous kick_state evaluation (Q1 reactive path). Returns the
    transitions that crossed into 'recommended' this run — the caller raises
    the leader actions. States only; the weekly grain never moves here."""
    now = now or utcnow()
    now_dt = _parse_ts(now)
    epoch_row = conn.execute("SELECT MIN(observed_at) FROM battle_events").fetchone()
    if not epoch_row or not epoch_row[0]:
        return []  # stream empty (fresh cut warm-up) — no data, no judgment
    epoch = _parse_ts(epoch_row[0])
    fired: list[dict] = []
    members = conn.execute(
        """SELECT mm.player_tag, mm.tenure_days, mm.role, mm.kick_state,
                  mm.kick_state_since, mm.state_json,
                  pcs.trophies
           FROM member_management mm
           LEFT JOIN player_current_state pcs ON pcs.player_tag = mm.player_tag
           WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                         WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL)"""
    ).fetchall()
    for m in members:
        tag = m["player_tag"]
        last_row = conn.execute(
            "SELECT MAX(battle_time) FROM battle_events WHERE player_tag = ?", (tag,)
        ).fetchone()
        reference = _parse_ts(last_row[0]) if last_row and last_row[0] else epoch
        days_idle = (now_dt - reference).total_seconds() / 86400.0

        state = m["kick_state"] or "none"
        since = m["kick_state_since"]
        new_state = state

        if last_row and last_row[0] and state != "none" and days_idle < KICK_WATCH_DAYS:
            new_state = "none"  # any battle → none; auto-withdraw (§3.3)
        elif (m["tenure_days"] or 0) < NEW_MEMBER_GRACE:
            new_state = "none" if state in ("at_risk", "recommended") else state
        else:
            trophies = m["trophies"] or 5000
            at_risk_days = max(7.0, trophies / 1000.0 * 1.4)
            if days_idle >= at_risk_days + KICK_CONFIRM_DAYS:
                new_state = "recommended"
            elif days_idle >= at_risk_days:
                new_state = "at_risk"
            elif days_idle >= KICK_WATCH_DAYS:
                new_state = "watch"
            else:
                new_state = "none"
            # Guards: elder+ never fires the reactive path (§3.3);
            # an open leadership watch memory suppresses 'recommended'.
            if new_state == "recommended" and (
                (m["role"] or "member") in ELDER_PLUS or _has_leadership_hold(tag)
            ):
                new_state = "at_risk"

        if new_state != state:
            if new_state == "recommended":
                fired.append({
                    "player_tag": tag,
                    "days_idle": round(days_idle, 1),
                    "from_state": state,
                })
            conn.execute(
                """UPDATE member_management
                   SET kick_state = ?, kick_state_since = ?
                   WHERE player_tag = ?""",
                (new_state, now if new_state != state else since, tag),
            )
    return fired


def _has_leadership_hold(tag: str) -> bool:
    """Open flag_member_watch hold: an active leadership watch memory for the
    tag. v5.1 memory pass: memories live in the engine DB (memory.md D1), so
    this reads `memories` directly — member_tag column, not the never-populated
    link table. Fail-open to False so a memory hiccup never blocks the
    pipeline silently — the policy gate still applies."""
    try:
        from engine.db import connect

        mconn = connect()
        row = mconn.execute(
            """SELECT 1 FROM memories m
               WHERE m.member_tag = ? AND m.title LIKE 'Watch:%'
                 AND m.retired_at IS NULL
                 AND (m.expires_at IS NULL OR m.expires_at > strftime('%Y-%m-%dT%H:%M:%S', 'now'))
               LIMIT 1""",
            (tag,),
        ).fetchone()
        mconn.close()
        return row is not None
    except Exception:
        return False


# --------------------------------------------------- weekly review (§3.1–3.2)

def run_weekly_review(conn, week_anchor: str, now: str | None = None) -> dict:
    """Roll the weekly grain (the ONLY place it moves) and advance Layer-1 +
    Layer-2 machines. week_anchor = ISO date of the Monday running the review.
    Returns eligibles + withdrawals + per-member summary for the review post."""
    now = now or utcnow()
    promote_eligible: list[str] = []
    demote_eligible: list[str] = []
    withdrawn: list[dict] = []
    rows_out: list[dict] = []

    members = conn.execute(
        """SELECT * FROM member_management mm
           WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                         WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL)"""
    ).fetchall()
    for m in members:
        tag = m["player_tag"]
        st = _state(m)
        if st.get("week_anchor") == week_anchor:
            continue  # this week already rolled (idempotent re-run)

        # Layer 1: append this closed week's qualifying flags, advance states.
        weeks = st.setdefault("weeks", {"donor": [], "war": [], "battle": []})
        weeks["donor"].append(_week_qualifies_donor(conn, tag, week_anchor))
        weeks["war"].append(_week_qualifies_war(conn, tag, week_anchor))
        weeks["battle"].append(_week_qualifies_battle(conn, tag, week_anchor))
        for k in weeks:
            weeks[k] = weeks[k][-8:]  # bounded history
        donor = advance_layer1(m["sustained_donor"], weeks["donor"])
        war = advance_layer1(m["war_reliable"], weeks["war"])
        battle = advance_layer1(m["battle_active"], weeks["battle"])

        # Layer 2 — promote (member → elder only, §3.1)
        role = m["role"] or "member"
        holding = sum(1 for s in (donor, war, battle) if s == "holding")
        gate = (
            role == "member"
            and (m["tenure_days"] or 0) >= PROMOTE_TENURE_MIN
            and holding >= 2
        )
        p_state = m["promote_state"] or "none"
        p_weeks = m["promote_qualifying_weeks"] or 0
        p_miss = st.get("promote_misses", 0)
        if p_state == "none":
            if gate:
                p_state, p_weeks, p_miss = "building", 1, 0
        elif p_state == "building":
            if gate:
                p_weeks += 1
                p_miss = 0
            else:
                p_miss += 1
                if p_miss >= 2:
                    p_state, p_weeks, p_miss = "none", 0, 0
            if p_state == "building" and p_weeks >= PROMOTE_QUALIFYING_WEEKS:
                p_state = "eligible"
        elif p_state in ("eligible", "recommended"):
            if gate:
                p_miss = 0
            else:
                p_miss += 1
                if p_miss >= 2:
                    withdrawn.append({"player_tag": tag, "kind": "promote"})
                    p_state, p_weeks, p_miss = "building", max(0, p_weeks // 2), 0
        st["promote_misses"] = p_miss
        if p_state == "eligible":
            promote_eligible.append(tag)

        # Layer 2 — demote (elder → member, §3.2)
        d_state = m["demote_state"] or "none"
        d_gate = role == "elder" and donor == "lapsed" and war == "lapsed"
        d_weeks = st.get("demote_weeks", 0)
        if d_gate:
            d_weeks += 1
            d_state = "eligible" if d_weeks >= DEMOTE_WEEKS else "building"
        else:
            if d_state in ("eligible", "recommended"):
                withdrawn.append({"player_tag": tag, "kind": "demote"})
            d_state, d_weeks = "none", 0
        st["demote_weeks"] = d_weeks
        if d_state == "eligible":
            demote_eligible.append(tag)

        st["week_anchor"] = week_anchor
        conn.execute(
            """UPDATE member_management SET
                   week_anchor = ?, computed_at = ?,
                   sustained_donor = ?, war_reliable = ?, battle_active = ?,
                   promote_state = ?, promote_qualifying_weeks = ?,
                   demote_state = ?, state_json = ?
               WHERE player_tag = ?""",
            (
                week_anchor, now, donor, war, battle,
                p_state, p_weeks, d_state,
                json.dumps(st, ensure_ascii=False), tag,
            ),
        )
        rows_out.append({
            "player_tag": tag, "role": role,
            "sustained_donor": donor, "war_reliable": war, "battle_active": battle,
            "promote_state": p_state, "promote_qualifying_weeks": p_weeks,
            "demote_state": d_state, "kick_state": m["kick_state"] or "none",
        })

    return {
        "week_anchor": week_anchor,
        "promote_eligible": promote_eligible,
        "demote_eligible": demote_eligible,
        "withdrawn": withdrawn,
        "rows": rows_out,
    }
