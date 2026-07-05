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

# --- Elder band (§3, ratified 2026-07-05): filters → score → band → hysteresis
PROMOTE_TENURE_MIN = 28       # four-week hard filter before elder consideration
WAR_FLOOR_DAYS = 1           # mandatory war participation: ≥N played days ...
WAR_FLOOR_WINDOW = 14        # ... in the last WAR_FLOOR_WINDOW days
WAR_RATE_WINDOW = 28         # war_rate score window (decks used ÷ available)
RANKED_FLOOR_LEAGUE = 4      # Champion — the ranked alternative to the war floor
SCORE_W_WAR = 0.65           # war-weighted rank blend (core elder duty)
SCORE_W_DONATION = 0.35      # "lead by example" — the lighter half
ELDER_BAND_FLOOR = 0.15      # elder share of the non-leadership roster ...
ELDER_BAND_CEIL = 0.20       # ... a range to maintain, NOT a quota to fill
WORTHINESS_MIN_PERCENTILE = 0.50   # below-floor promotions still need ≥ median score
PROMOTE_QUALIFYING_WEEKS = 3  # sustained weeks in the promotable set
DEMOTE_WEEKS = 2             # deliberately fewer — demotion is easier than promotion

# --- Kick path + Layer-1 evidence signals (unchanged by the band)
WAR_QUALIFY_RATE = 0.75      # legacy war_reliable signal — evidence rendering only
BATTLE_DAYS_MIN = 8
KICK_CONFIRM_DAYS = 7
NEW_MEMBER_GRACE = 14
KICK_WATCH_DAYS = 3          # CLAN.md inactivity_days
HOLD_WINDOW = 4              # Layer-1: 3-of-4 holds, 1-of-4 lapses
HOLD_NEED = 3
LAPSE_MAX = 1

ELDER_PLUS = ("elder", "coLeader", "leader")
LEADERSHIP_ROLES = ("coLeader", "leader")   # excluded from the elder ranking


def _parse_ts(value: str) -> datetime:
    """Delegates to the normalizer's single parser (engine/normalize.py)."""
    from engine.normalize import parse_cr_time

    return parse_cr_time(value)


def _state(row) -> dict:
    try:
        return json.loads(row["state_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def _row_name(row) -> str | None:
    try:
        return row["player_name"] or row["current_name"]
    except (KeyError, IndexError):
        return None


def _eligibility_row(row, *, kind: str, rationale: str) -> dict:
    return {
        "player_tag": row["player_tag"],
        "player_name": _row_name(row),
        "kind": kind,
        "role": row["role"] or "member",
        "rationale": rationale,
    }


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


def _closed_week_donations(conn, tag, week_anchor: str):
    """This member's donations for the closed week (frozen Sunday row)."""
    row = conn.execute(
        """SELECT donations_week FROM player_daily_metrics
           WHERE player_tag = ? AND metric_date < ?
             AND strftime('%w', metric_date) = '0' AND donations_week IS NOT NULL
           ORDER BY metric_date DESC LIMIT 1""",
        (tag, week_anchor),
    ).fetchone()
    return None if row is None else (row["donations_week"] or 0)


def _roster_donor_median(conn, week_anchor: str) -> float | None:
    """Median closed-week donations across the ACTIVE roster. The bar is the
    clan itself, not a static number (Jamie 2026-07-05: "remove the static
    filter, let the math do the work") — self-calibrating as the clan's
    donation culture shifts (the old DONOR_WEEK_MIN=50 was calibrated on a
    long-gone distribution and had drifted to select ~78% of the roster)."""
    vals = sorted(
        v for (v,) in conn.execute(
            """SELECT donations_week FROM player_daily_metrics pm
               WHERE metric_date = (
                   SELECT MAX(metric_date) FROM player_daily_metrics
                   WHERE metric_date < ? AND strftime('%w', metric_date) = '0')
                 AND donations_week IS NOT NULL
                 AND EXISTS (SELECT 1 FROM clan_memberships cm
                             WHERE cm.player_tag = pm.player_tag AND cm.left_at IS NULL)""",
            (week_anchor,),
        ).fetchall()
    )
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _week_qualifies_donor(conn, tag, week_anchor: str):
    """Sustained donor = donated something AND at/above the active roster's
    median for the closed week. Relative, self-calibrating; a freeloader
    (0 donations) never qualifies even in a low-donation week."""
    mine = _closed_week_donations(conn, tag, week_anchor)
    if mine is None:
        return None  # no data — skip, don't fail
    median = _roster_donor_median(conn, week_anchor)
    if median is None:
        return None
    return mine > 0 and mine >= median


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


# ------------------------------------------------ elder band (§3.1–3.4)

def _passes_war_floor(conn, tag, now: str) -> bool:
    """Mandatory war participation: ≥ WAR_FLOOR_DAYS finalized war days with an
    actual deck played in the last WAR_FLOOR_WINDOW days (§3.1)."""
    n = conn.execute(
        """SELECT COUNT(*) FROM war_attendance_days
           WHERE player_tag = ? AND decks_used > 0
             AND julianday(observed_at) >= julianday(?) - ?""",
        (tag, now, WAR_FLOOR_WINDOW),
    ).fetchone()[0]
    return n >= WAR_FLOOR_DAYS


def _ranked_standing(conn, tag) -> tuple[int, int]:
    """Best-of (current, last-season) Ranked league + its rating. Uses only
    current and last (both post-rework 7-league scale) — never the all-time
    `best` field, which can carry old 10-league values (normalize.md epoch
    quirk). Returns (league, rating); (0, 0) when no ranked data."""
    row = conn.execute(
        "SELECT ranked_league, ranked_trophies FROM player_current_state WHERE player_tag = ?",
        (tag,),
    ).fetchone()
    cur_lg = (row["ranked_league"] if row else None) or 0
    cur_rt = (row["ranked_trophies"] if row else None) or 0
    last_lg, last_rt = 0, 0
    bl = conn.execute(
        """SELECT payload_json FROM state_baselines
           WHERE entity_kind = 'player' AND entity_tag = ? AND aspect = 'ranked'""",
        (tag,),
    ).fetchone()
    if bl:
        try:
            last = (json.loads(bl["payload_json"]) or {}).get("last") or {}
            last_lg = last.get("league") or 0
            last_rt = last.get("trophies") or 0
        except (TypeError, ValueError):
            pass
    league = max(cur_lg, last_lg)
    rating = max([rt for lg, rt in ((cur_lg, cur_rt), (last_lg, last_rt)) if lg == league] or [0])
    return league, rating


def _passes_ranked_floor(conn, tag) -> bool:
    """Champion (league ≥ 4) on the best-of current/last season."""
    league, _ = _ranked_standing(conn, tag)
    return league >= RANKED_FLOOR_LEAGUE


def _ranked_prestige(league: int, rating: int) -> float:
    """ABSOLUTE 0–1 prestige from best-of league (not a percentile). Below
    Champion → 0; UC scales the rating band 1200–2000 into the top +0.1."""
    base = {4: 0.5, 5: 0.65, 6: 0.8, 7: 0.9}.get(league, 0.0)
    if league >= 7:
        base += min(0.1, max(0.0, (rating - 1200) / 800.0 * 0.1))
    return base


def _passes_competitive_floor(conn, tag, now: str) -> bool:
    """The mandatory floor: war OR ranked. An elder is only auto-demoted for
    abandonment when BOTH fail (§3.1, §3.4)."""
    return _passes_war_floor(conn, tag, now) or _passes_ranked_floor(conn, tag)


def _war_rate(conn, tag, now: str) -> float:
    """Decks used ÷ available over the trailing war window (§3.2). 0.0 when the
    member has no finalized war days — non-participation ranks at the bottom."""
    row = conn.execute(
        """SELECT SUM(decks_used) AS used, SUM(decks_available) AS avail
           FROM war_attendance_days
           WHERE player_tag = ? AND julianday(observed_at) >= julianday(?) - ?""",
        (tag, now, WAR_RATE_WINDOW),
    ).fetchone()
    if not row or not row["avail"]:
        return 0.0
    return (row["used"] or 0) / row["avail"]


def _percentile(value: float, values: list[float]) -> float:
    """Mid-rank percentile of `value` within `values` (ties share the midpoint):
    (count below + 0.5·count equal) / N. Range (0, 1); robust to ties."""
    n = len(values)
    if n == 0:
        return 0.0
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (below + 0.5 * equal) / n


def _elder_scores(conn, now: str, week_anchor: str) -> dict:
    """Rank the active NON-LEADERSHIP roster (members + elders) by a
    war-weighted percentile blend (§3.2). Leaders/co-leaders are excluded from
    the ranking entirely. Percentiles are over the whole ranked roster so the
    bar is the clan itself; filter failures affect candidacy (§3.1), not the
    distribution."""
    members = conn.execute(
        """SELECT mm.player_tag, mm.role, mm.tenure_days, p.current_name
           FROM member_management mm
           LEFT JOIN players p ON p.player_tag = mm.player_tag
           WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                         WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL)"""
    ).fetchall()
    ranked = [m for m in members if (m["role"] or "member") not in LEADERSHIP_ROLES]
    raw = {}
    for m in ranked:
        tag = m["player_tag"]
        league, rating = _ranked_standing(conn, tag)
        raw[tag] = {
            "role": m["role"] or "member",
            "tenure": m["tenure_days"] or 0,
            "name": m["current_name"],
            "war_rate": _war_rate(conn, tag, now),
            "ranked_league": league,
            "ranked_prestige": _ranked_prestige(league, rating),
            "donations": _closed_week_donations(conn, tag, week_anchor) or 0,
        }
    war_vals = [r["war_rate"] for r in raw.values()]
    don_vals = [r["donations"] for r in raw.values()]
    for r in raw.values():
        r["war_pct"] = _percentile(r["war_rate"], war_vals)
        r["donation_pct"] = _percentile(r["donations"], don_vals)
        # competitive = the better of war-participation percentile and ABSOLUTE
        # ranked prestige (§3.2): a Ranked specialist who skips clan war ranks
        # on that specialism, not penalized for it.
        r["competitive"] = max(r["war_pct"], r["ranked_prestige"])
        r["score"] = SCORE_W_WAR * r["competitive"] + SCORE_W_DONATION * r["donation_pct"]
    return raw


def _elder_band(conn, scores: dict, now: str) -> dict:
    """Size the corps to 15–20% of the ranked roster and select the promotable
    / demotable sets (§3.3–3.4). Deterministic ranking: score desc, then tenure
    desc, then tag asc."""
    n = len(scores)
    order = sorted(scores.items(), key=lambda kv: (-kv[1]["score"], -kv[1]["tenure"], kv[0]))
    rank = {tag: i + 1 for i, (tag, _) in enumerate(order)}
    floor = round(ELDER_BAND_FLOOR * n)
    ceil = round(ELDER_BAND_CEIL * n)
    current_elders = sum(1 for r in scores.values() if r["role"] == "elder")
    score_vals = sorted(r["score"] for r in scores.values())
    median = score_vals[n // 2] if n and n % 2 else (
        (score_vals[n // 2 - 1] + score_vals[n // 2]) / 2 if n else 0.0)

    # Promotable: only when below floor, only filter-passing members, top-ranked,
    # and only those clearing the worthiness floor (≥ median score). "Range, not
    # quota" — a thin/weak clan promotes nobody rather than the undeserving.
    promotable: set[str] = set()
    if current_elders < floor:
        need = floor - current_elders
        candidates = [
            tag for tag, _ in order
            if scores[tag]["role"] == "member"
            and (scores[tag]["tenure"] or 0) >= PROMOTE_TENURE_MIN
            and _passes_competitive_floor(conn, tag, now)
            and scores[tag]["score"] >= median
        ]
        promotable = set(candidates[:need])

    # Demotable (§3.4): an elder who abandoned the war duty (direct, any rank —
    # even below floor: not doing the job outranks slot math), PLUS, only when
    # OVER the ceiling, the lowest-ranked participating elders needed to get
    # back to the ceiling. A participating elder inside the band is never
    # churned — "a range to maintain, not a quota to fill" cuts both ways.
    demotable: set[str] = set()
    participating_elders = []
    for tag, r in scores.items():
        if r["role"] != "elder":
            continue
        # Auto-demote only when BOTH floors fail — a UC Ranked grinder who
        # skips clan war is doing a core duty, just a different one (§3.4).
        if not _passes_competitive_floor(conn, tag, now):
            demotable.add(tag)
        else:
            participating_elders.append(tag)
    overflow = len(participating_elders) - ceil
    if overflow > 0:
        # lowest-ranked (highest rank number) participating elders first
        for tag in sorted(participating_elders, key=lambda t: -rank[t])[:overflow]:
            demotable.add(tag)

    return {
        "n": n, "floor": floor, "ceil": ceil, "current_elders": current_elders,
        "rank": rank, "median": median, "promotable": promotable, "demotable": demotable,
    }


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
    fired: list[dict] = []
    members = conn.execute(
        """SELECT mm.player_tag, mm.tenure_days, mm.role, mm.kick_state,
                  mm.kick_state_since, mm.state_json,
                  pcs.trophies, p.last_seen_at,
                  (SELECT MAX(cm2.joined_at) FROM clan_memberships cm2
                   WHERE cm2.player_tag = mm.player_tag AND cm2.left_at IS NULL)
                  AS membership_joined_at
           FROM member_management mm
           LEFT JOIN player_current_state pcs ON pcs.player_tag = mm.player_tag
           LEFT JOIN players p ON p.player_tag = mm.player_tag
           WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                         WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL)"""
    ).fetchall()
    for m in members:
        tag = m["player_tag"]
        last_row = conn.execute(
            "SELECT MAX(battle_time) FROM battle_events WHERE player_tag = ?", (tag,)
        ).fetchone()
        if last_row and last_row[0]:
            reference = _parse_ts(last_row[0])
        else:
            # Never battled in the stream: idle from THEIR OWN anchor — the
            # later of membership joined_at and players.last_seen_at — never
            # the shared stream epoch (cold review 2026-07-04 #10: the epoch
            # marched every zero-battle member toward at_risk in lockstep,
            # false kick cards landing as a batch mid-July).
            candidates = [
                _parse_ts(v)
                for v in (m["membership_joined_at"], m["last_seen_at"])
                if v
            ]
            candidates = [c for c in candidates if c is not None]
            if not candidates:
                continue  # no personal anchor — no judgment
            reference = max(candidates)
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


def withdraw_stale_actions(conn, now: str | None = None) -> list[dict]:
    """Close proposed leader-action cards whose backing management state has
    already withdrawn. This is the enacted half of the management.md
    auto-withdraw rule; the state machines own judgment, storage owns the
    action-board transition.
    """
    rows = conn.execute(
        """SELECT lar.action_type, lar.target_player_tag,
                  mm.kick_state, mm.promote_state, mm.demote_state
           FROM leader_action_recommendations lar
           LEFT JOIN member_management mm ON mm.player_tag = lar.target_player_tag
           WHERE lar.status = 'proposed'
             AND COALESCE(lar.is_test, 0) = 0
             AND lar.action_type IN (
                'kick_recommendation',
                'promotion_recommendation',
                'demotion_recommendation'
             )"""
    ).fetchall()
    if not rows:
        return []
    from storage.leader_actions import auto_withdraw_leader_actions

    withdrawn: list[dict] = []
    rules = {
        "kick_recommendation": (
            "kick_state",
            {"recommended"},
            "kick",
            "Auto-withdrawn: kick candidacy cleared by recent battle activity or management guard.",
        ),
        "promotion_recommendation": (
            "promote_state",
            {"eligible", "recommended"},
            "promote",
            "Auto-withdrawn: promotion candidacy no longer meets sustained weekly gates.",
        ),
        "demotion_recommendation": (
            "demote_state",
            {"eligible", "recommended"},
            "demote",
            "Auto-withdrawn: demotion candidacy no longer meets sustained weekly gates.",
        ),
    }
    for row in rows:
        action_type = row["action_type"]
        state_col, active_states, kind, reason = rules[action_type]
        current_state = row[state_col] if row[state_col] is not None else "none"
        if current_state in active_states:
            continue
        count = auto_withdraw_leader_actions(
            action_type=action_type,
            target_player_tag=row["target_player_tag"],
            reason=reason,
            conn=conn,
        )
        if count:
            withdrawn.append({
                "player_tag": row["target_player_tag"],
                "kind": kind,
                "action_type": action_type,
                "count": count,
                "reason": reason,
            })
    return withdrawn


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
    promote_eligible: list[dict] = []
    demote_eligible: list[dict] = []
    withdrawn: list[dict] = []
    rows_out: list[dict] = []

    members = conn.execute(
        """SELECT mm.*, p.current_name AS player_name
           FROM member_management mm
           LEFT JOIN players p ON p.player_tag = mm.player_tag
           WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                         WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL)"""
    ).fetchall()

    # The elder band (§3): score the roster once, size the corps, pick the
    # promotable/demotable sets. The per-member loop only advances hysteresis.
    scores = _elder_scores(conn, now, week_anchor)
    band = _elder_band(conn, scores, now)

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

        # Layer 2 — the elder band (§3.3–3.4). The band already applied the
        # filters, worthiness floor, and slot math; here we only pace the move.
        role = m["role"] or "member"
        sc = scores.get(tag)
        rank = band["rank"].get(tag)
        band_evidence = (
            "not ranked (leadership)" if not sc else
            f"score {sc['score']:.2f} [competitive {sc['competitive']:.2f} "
            f"(war {sc['war_pct']:.2f} / ranked {sc['ranked_prestige']:.2f}, "
            f"league {sc['ranked_league']}), donations {sc['donation_pct']:.2f}], "
            f"rank {rank} of {band['n']}, band {band['floor']}-{band['ceil']}, "
            f"elders {band['current_elders']}"
        )

        # Promote (member → elder)
        gate = tag in band["promotable"]
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
            promote_eligible.append(_eligibility_row(
                m, kind="promote",
                rationale=f"Below-band elder slot: {band_evidence}; held {p_weeks} wk.",
            ))

        # Demote (elder → member) — 2 weeks, no grace (easier than promotion)
        d_state = m["demote_state"] or "none"
        d_gate = tag in band["demotable"]
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
            floor_ok = sc and _passes_competitive_floor(conn, tag, now)
            reason = ("abandoned both war and ranked duty" if not floor_ok
                      else "ranked below the elder band (over ceiling)")
            demote_eligible.append(_eligibility_row(
                m, kind="demote",
                rationale=f"{reason}: {band_evidence}; held {d_weeks} wk.",
            ))

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
