"""Clan-management deterministic core (docs/reference/v5.1/management.md — ratified
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

import hashlib
import json
import logging
from datetime import datetime, timedelta

from engine.db import utcnow

log = logging.getLogger("engine.management")

# management.md §5 — ratified defaults (move into CLAN.md at the cut).

# --- Elder band (§3, ratified 2026-07-05): filters → score → band → hysteresis
PROMOTE_TENURE_MIN = 28  # four-week hard filter before elder consideration
WAR_FLOOR_DAYS = 1  # mandatory war participation: ≥N played days ...
WAR_FLOOR_WINDOW = 14  # ... in the last WAR_FLOOR_WINDOW days
WAR_RATE_WINDOW = 28  # war_rate score window (decks used ÷ available)
# Ranked is scored/floored by PARTICIPATION now, not league reached (2026-07-12):
# every elder metric must be in the player's control and reward participation,
# not account power. League/prestige was a backdoor for collection strength.
RANKED_FLOOR_LEAGUE = 4  # Champion — kept only for DISPLAY (league name), not gating
RANKED_FLOOR_BATTLES = 5  # mandatory ranked participation: ≥N ranked battles ...
# ... in the last WAR_FLOOR_WINDOW days (the ranked alt to war)
SCORE_W_WAR = 0.65  # war-weighted rank blend (core elder duty)
SCORE_W_DONATION = 0.35  # "lead by example" — the lighter half
# War is direct clan contribution (Fame + league progress); ranked only reps the
# clan. So ranked participation is muted and fills part of the gap war leaves:
# competitive = war_pct + RANKED_WEIGHT * ranked_pct * (1 - war_pct). War-primary,
# ranked-secondary, doing-both rewarded, bounded 0-1 (no saturation).
RANKED_WEIGHT = 0.40
ELDER_BAND_FLOOR = 0.15  # elder share of the non-leadership roster ...
ELDER_BAND_CEIL = 0.20  # ... a range to maintain, NOT a quota to fill
WORTHINESS_MIN_PERCENTILE = 0.50  # below-floor promotions still need ≥ median score
PROMOTE_QUALIFYING_WEEKS = 3  # sustained weeks in the promotable set
DEMOTE_WEEKS = 2  # abandonment demotion cadence (easier than promotion)
# Anti-flap deadband (§3.4): a member displaces an elder only when they out-score
# them by ≥ SWAP_MARGIN, so near-boundary jitter never swaps the seat. 0.05 of the
# 0–1 blend ≈ a clear one-tier gap on the dominant (competitive×0.65) term — big
# enough that a hair-lead is ignored, small enough that a real overtake lands.
SWAP_MARGIN = 0.05

# --- Kick path: activity-first, scarcity-aware (redesign 2026-07-12) ---------
# Idle escalates on a flat clock (no trophy buffer, no newcomer shield —
# newcomers are judged from their own join anchor like everyone; if anything a
# newcomer should be engaging MORE early). Resolves inside the 5–10 day window
# so member status tracks reality instead of drifting for weeks.
WAR_QUALIFY_RATE = 0.75  # legacy war_reliable signal — evidence rendering only
BATTLE_DAYS_MIN = 8
KICK_WATCH_DAYS = 3  # CLAN.md inactivity_days — the "getting quiet" line
KICK_AT_RISK_DAYS = 5  # idle days → at_risk (concerning); flat, not trophy-scaled
KICK_CONFIRM_DAYS = 3  # + this → recommended (card) ≈ day 8, before "10 looks unmanaged"
ROSTER_CAP = 50  # CR clan cap; open_slots = ROSTER_CAP − active members
# Contribution grace: a member who CURRENTLY clears the elder floor (recent war
# participation OR Champion ranked — the SAME test that earns elder, so ranked
# counts equally, and it's live/last-season, never a 3-season average) earns
# extra runway before a card — but ONLY while there are open slots. An idle
# member only costs a slot when the roster is full, so the grace fades linearly
# with slack (open_slots/ROSTER_CAP) and is 0 at 50/50. Capped so a valued idle
# member with open slots still can't drift much past ~12 days idle.
KICK_CONTRIB_GRACE_MAX = 4  # max extra confirm days at a fully-open roster
HOLD_WINDOW = 4  # Layer-1: 3-of-4 holds, 1-of-4 lapses
HOLD_NEED = 3
LAPSE_MAX = 1

# Re-nomination after a decline (defer retired 2026-07-10). Declining a card is
# now the only "not now" — the engine reconsiders on sustained evidence, not a
# leader-set clock. After a decline, a member who STILL qualifies is re-carded
# once this cooldown lapses (a decline note like "revisit in a month" writes a
# longer expires_at that overrides it). Kick uses the per-tick sweep
# (renominate_after_cooldown); promote/demote honour the same window in the
# weekly review, which re-emits every eligible member each Monday.
KICK_RENOMINATE_COOLDOWN_DAYS = 7
PROMOTE_RENOMINATE_COOLDOWN_DAYS = 14
DEMOTE_RENOMINATE_COOLDOWN_DAYS = 14
_RENOMINATE_COOLDOWN_DAYS = {
    "kick_recommendation": KICK_RENOMINATE_COOLDOWN_DAYS,
    "promotion_recommendation": PROMOTE_RENOMINATE_COOLDOWN_DAYS,
    "demotion_recommendation": DEMOTE_RENOMINATE_COOLDOWN_DAYS,
}

ELDER_PLUS = ("elder", "coLeader", "leader")
LEADERSHIP_ROLES = ("coLeader", "leader")  # excluded from the elder ranking


def _parse_ts(value: str) -> datetime:
    """Delegates to the normalizer's single parser (engine/normalize.py)."""
    from engine.normalize import parse_cr_time

    return parse_cr_time(value)


def _state(row) -> dict:
    """Load a member's Layer-1 state-machine history. Corruption resets to the
    default empty state (so one bad row can't fail the tick) but is now logged
    rather than swallowed silently — a member whose qualifying-week history
    vanishes would otherwise re-accrue from zero with no trace."""
    raw = row["state_json"] or "{}"
    try:
        loaded = json.loads(raw)
    except TypeError, ValueError:
        tag = _row_tag(row)
        log.warning("member_management.state_json unparseable, resetting to {} (tag=%s)", tag)
        return {}
    if not isinstance(loaded, dict):
        tag = _row_tag(row)
        log.warning(
            "member_management.state_json is %s not object, resetting to {} (tag=%s)",
            type(loaded).__name__,
            tag,
        )
        return {}
    return loaded


def _row_tag(row) -> str | None:
    try:
        return row["player_tag"]
    except KeyError, IndexError:
        return None


def _row_name(row) -> str | None:
    try:
        return row["player_name"] or row["current_name"]
    except KeyError, IndexError:
        return None


def _eligibility_row(row, *, kind: str, rationale: str) -> dict:
    return {
        "player_tag": row["player_tag"],
        "player_name": _row_name(row),
        "kind": kind,
        "role": row["role"] or "member",
        "rationale": rationale,
    }


# The engine's actionable tiers per management verdict. Below these a member is
# only trending (watch/at_risk/building) — context, not a call to act.
_MGMT_ACTIONABLE = {
    "kick": {"recommended"},
    "promote": {"eligible", "recommended"},
    "demote": {"eligible", "recommended"},
}
_MGMT_BUILDING = {
    "kick": {"watch", "at_risk"},
    "promote": {"building"},
    "demote": {"building"},
}
_MGMT_STATE_COL = {
    "kick": "kick_state",
    "promote": "promote_state",
    "demote": "demote_state",
}


def management_read_summary(conn) -> dict:
    """Read-only snapshot of the engine's CURRENT management verdicts for current
    members — the authoritative source for who (if anyone) warrants a kick /
    promotion / demotion right now.

    The awareness brain must treat this as the source of truth for those
    recommendations rather than re-deriving them from raw donation/war stats: the
    state machines here ARE the "right logic". ``actionable`` lists the members
    the engine currently flags (these already back #leader-actions cards);
    ``building_counts`` is how many are trending toward each action but not yet
    actionable — context, not a call to act.
    """
    rows = conn.execute(
        """SELECT mm.player_tag, mm.role, mm.kick_state, mm.promote_state, mm.demote_state,
                  mm.judgment_status, mm.judgment_reason, mm.evidence_as_of,
                  mm.materialization_id,
                  COALESCE(p.display_name, p.current_name) AS current_name
           FROM member_management mm
           LEFT JOIN players p ON p.player_tag = mm.player_tag
           WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                         WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL)"""
    ).fetchall()
    actionable: dict[str, list] = {"kick": [], "promote": [], "demote": []}
    building = {"kick": 0, "promote": 0, "demote": 0}
    readiness_counts = {"ready": 0, "held": 0, "unknown": 0}
    held: list[dict] = []
    for row in rows:
        judgment_status = row["judgment_status"] or "unknown"
        readiness_counts[judgment_status] += 1
        if judgment_status != "ready":
            held.append(
                {
                    "player_tag": row["player_tag"],
                    "player_name": row["current_name"] or row["player_tag"],
                    "status": judgment_status,
                    "reason": row["judgment_reason"],
                    "evidence_as_of": row["evidence_as_of"],
                    "materialization_id": row["materialization_id"],
                }
            )
            continue
        for action, col in _MGMT_STATE_COL.items():
            state = row[col] or "none"
            if state in _MGMT_ACTIONABLE[action]:
                actionable[action].append(
                    {
                        "player_tag": row["player_tag"],
                        "player_name": row["current_name"] or row["player_tag"],
                        "role": row["role"] or "member",
                        "state": state,
                    }
                )
            elif state in _MGMT_BUILDING[action]:
                building[action] += 1
    from engine.readiness import latest_materialization

    materialization = latest_materialization(conn)
    return {
        "actionable": actionable,
        "building_counts": building,
        "members_evaluated": len(rows),
        "readiness": {
            "counts": readiness_counts,
            "held_members": held,
        },
        "materialization": materialization,
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
    """This member's donations for the most recent closed donation-week.

    NOT the Sunday row: Clash Royale's donation counter RESETS on Sunday, so that
    row is the post-reset ~0, not a frozen weekly total (live: Sunday averages 71
    with 152 zeros, Saturday averages 186 with 40). Take the week's PEAK — the
    counter climbs monotonically then resets, so its max IS the week's total.
    Dates shift a day so %W groups Sun-Sat.
    """
    row = conn.execute(
        """SELECT MAX(donations_week) AS peak FROM player_daily_metrics
           WHERE player_tag = ? AND metric_date < ? AND donations_week IS NOT NULL
             -- Skip the in-progress donation week. Its only elapsed day may be
             -- the Sunday reset itself, whose peak is 0 — which would report a
             -- 700-a-week donor as having given nothing.
             AND strftime('%Y-%W', metric_date)
                 <> strftime('%Y-%W', ?)
           GROUP BY strftime('%Y-%W', metric_date)
           ORDER BY strftime('%Y-%W', metric_date) DESC LIMIT 1""",
        (tag, week_anchor, week_anchor),
    ).fetchone()
    return None if row is None else (row["peak"] or 0)


def _roster_donor_median(conn, week_anchor: str) -> float | None:
    """Median closed-week donations across the ACTIVE roster. The bar is the
    clan itself, not a static number (Jamie 2026-07-05: "remove the static
    filter, let the math do the work") — self-calibrating as the clan's
    donation culture shifts (the old DONOR_WEEK_MIN=50 was calibrated on a
    long-gone distribution and had drifted to select ~78% of the roster)."""
    # Peak of the clan's most recent closed donation-week, per member — not the
    # Sunday row, which is the post-reset ~0 (see _closed_week_donations).
    vals = sorted(
        v
        for (v,) in conn.execute(
            """SELECT MAX(pm.donations_week) FROM player_daily_metrics pm
               WHERE pm.metric_date < ? AND pm.donations_week IS NOT NULL
                 AND strftime('%Y-%W', pm.metric_date) = (
                     SELECT MAX(strftime('%Y-%W', metric_date))
                       FROM player_daily_metrics
                      WHERE metric_date < ? AND donations_week IS NOT NULL
                        AND strftime('%Y-%W', metric_date)
                            <> strftime('%Y-%W', ?))
                 AND EXISTS (SELECT 1 FROM clan_memberships cm
                             WHERE cm.player_tag = pm.player_tag AND cm.left_at IS NULL)
               GROUP BY pm.player_tag""",
            (week_anchor, week_anchor, week_anchor),
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
    week_start = (datetime.fromisoformat(week_anchor) - timedelta(days=7)).date().isoformat()
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
        except TypeError, ValueError:
            pass
    league = max(cur_lg, last_lg)
    rating = max([rt for lg, rt in ((cur_lg, cur_rt), (last_lg, last_rt)) if lg == league] or [0])
    return league, rating


def _ranked_battles(conn, tag, now: str, window: int) -> int:
    """Ranked battles played in the trailing `window` days — the ranked
    PARTICIPATION signal (in the player's control), replacing league/prestige
    (which was skill/account-power, not effort)."""
    return (
        conn.execute(
            """SELECT COUNT(*) FROM battle_events
           WHERE player_tag = ? AND mode_group = 'ranked'
             AND julianday(observed_at) >= julianday(?) - ?""",
            (tag, now, window),
        ).fetchone()[0]
        or 0
    )


def _passes_ranked_floor(conn, tag, now: str) -> bool:
    """Mandatory ranked participation: ≥ RANKED_FLOOR_BATTLES ranked battles in
    the last WAR_FLOOR_WINDOW days (the ranked alternative to the war floor).
    Participation, not league reached — a strong account no longer clears it by
    sitting on an old climb."""
    return _ranked_battles(conn, tag, now, WAR_FLOOR_WINDOW) >= RANKED_FLOOR_BATTLES


def _passes_competitive_floor(conn, tag, now: str) -> bool:
    """The mandatory floor: war OR ranked. An elder is only auto-demoted for
    abandonment when BOTH fail (§3.1, §3.4)."""
    return _passes_war_floor(conn, tag, now) or _passes_ranked_floor(conn, tag, now)


def _war_rate(conn, tag, now: str) -> float:
    """Decks used ÷ available over the trailing war window (§3.2). 0.0 when the
    member has no finalized war days — non-participation ranks at the bottom."""
    row = conn.execute(
        # Window on the DATE, not the instant. war_attendance_days rows are
        # bulk-stamped one timestamp per war week (17 distinct values live, each a
        # Monday ~10:00Z covering that week's 4 days), so an hour-precise cutoff
        # moves an ENTIRE war week in or out depending on what time the review
        # runs. Measured: the same calendar day at 08:00Z vs 20:49Z changed
        # war_rate for 15 of 39 members — Aaqib Javed by 0.131, which at weight
        # 0.65 is 2.6x SWAP_MARGIN. Seats must not depend on the clock.
        """SELECT SUM(decks_used) AS used, SUM(decks_available) AS avail
           FROM war_attendance_days
           WHERE player_tag = ? AND date(observed_at) >= date(?, ?)""",
        (tag, now, f"-{WAR_RATE_WINDOW} days"),
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
        """SELECT mm.player_tag, mm.role, mm.tenure_days, mm.donations_4wk_avg,
                  COALESCE(p.display_name, p.current_name) AS current_name
           FROM member_management mm
           LEFT JOIN players p ON p.player_tag = mm.player_tag
           WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                         WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL)"""
    ).fetchall()
    ranked = [m for m in members if (m["role"] or "member") not in LEADERSHIP_ROLES]
    raw = {}
    for m in ranked:
        tag = m["player_tag"]
        league, rating = _ranked_standing(conn, tag)  # league kept for display only
        raw[tag] = {
            "role": m["role"] or "member",
            "tenure": m["tenure_days"] or 0,
            "name": m["current_name"],
            "war_rate": _war_rate(conn, tag, now),
            "ranked_league": league,
            "ranked_battles": _ranked_battles(conn, tag, now, WAR_RATE_WINDOW),
            # Trailing 4-week donation average (materialized by the projection),
            # not a single closed week (2026-07-12): one week's snapshot swings on
            # the donation reset — averaging 4 weeks is stable, and it matches the
            # number the Elder Standing report and webapp already display.
            "donations": m["donations_4wk_avg"] or 0,
        }
    war_vals = [r["war_rate"] for r in raw.values()]
    don_vals = [r["donations"] for r in raw.values()]
    rk_vals = [r["ranked_battles"] for r in raw.values()]
    for r in raw.values():
        r["war_pct"] = _percentile(r["war_rate"], war_vals)
        r["donation_pct"] = _percentile(r["donations"], don_vals)
        r["ranked_pct"] = _percentile(r["ranked_battles"], rk_vals)
        # competitive = war participation PRIMARY; ranked participation fills part
        # of the gap war leaves (headroom), muted by RANKED_WEIGHT because war is
        # direct clan contribution and ranked only reps the clan. War-maxed → ~1;
        # ranked-only → caps low; doing both wins; bounded 0-1 (never saturates).
        r["competitive"] = r["war_pct"] + RANKED_WEIGHT * r["ranked_pct"] * (1 - r["war_pct"])
        r["score"] = SCORE_W_WAR * r["competitive"] + SCORE_W_DONATION * r["donation_pct"]
    return raw


def _elder_band(conn, scores: dict, now: str) -> dict:
    """The elder corps TRACKS THE RANKING (§3.3–3.4, ratified 2026-07-05): the
    Elders should be the top-K of the eligible roster, K sized to the 15–20%
    band. An elder loses the seat by being outranked (a swap) or by abandoning
    the competitive floor. Swaps are guarded by SWAP_MARGIN + sustained weeks so
    they never flap. Deterministic ranking: score desc, tenure desc, tag asc."""
    n = len(scores)
    order = sorted(scores.items(), key=lambda kv: (-kv[1]["score"], -kv[1]["tenure"], kv[0]))
    rank = {tag: i + 1 for i, (tag, _) in enumerate(order)}
    floor = round(ELDER_BAND_FLOOR * n)
    ceil = round(ELDER_BAND_CEIL * n)
    current_elders = sum(1 for r in scores.values() if r["role"] == "elder")
    score_vals = sorted(r["score"] for r in scores.values())
    median = (
        score_vals[n // 2]
        if n and n % 2
        else ((score_vals[n // 2 - 1] + score_vals[n // 2]) / 2 if n else 0.0)
    )

    # Eligibility to HOLD elder: pass the competitive floor (war OR ranked).
    # A member additionally needs the tenure filter to be promotable IN; an
    # elder who fails the floor entirely has ABANDONED the duty (fast demote).
    def _eligible(tag: str) -> bool:
        r = scores[tag]
        if not _passes_competitive_floor(conn, tag, now):
            return False
        if r["role"] == "elder":
            return True
        return (r["tenure"] or 0) >= PROMOTE_TENURE_MIN

    abandoned = {
        tag
        for tag, r in scores.items()
        if r["role"] == "elder" and not _passes_competitive_floor(conn, tag, now)
    }
    eligible_order = [tag for tag, _ in order if _eligible(tag)]

    # K = the target corps size, clamped into the band around the current count
    # (grow toward the floor, shrink toward the ceiling, otherwise hold + swap).
    k = min(max(current_elders, floor), ceil)
    should_be = set(eligible_order[:k])
    # Below-floor growth still requires worthiness (≥ median) so a thin/weak
    # clan promotes nobody rather than elevating the undeserving to hit 15%.
    if current_elders < floor:
        should_be = {
            t for t in should_be if scores[t]["role"] == "elder" or scores[t]["score"] >= median
        }

    want_promote = [t for t in should_be if scores[t]["role"] == "member"]
    outranked = [
        t
        for t, r in scores.items()
        if r["role"] == "elder" and t not in should_be and t not in abandoned
    ]

    # Displacement swaps carry the anti-flap deadband: pair the boundary member
    # with the boundary elder (both closest to the K line) and only swap when the
    # member clears SWAP_MARGIN. A near-tie holds the incumbent.
    demote_reasons = {t: "abandoned" for t in abandoned}
    promotable: set[str] = set()
    demotable: set[str] = set(abandoned)
    # Pair ACROSS the K line, starting at the boundary and working outward: the
    # weakest challenger (just above the line) against the strongest outranked
    # elder (just below it). That is the closest contest, so the deadband lands
    # where a near-tie actually happens.
    #
    # Both lists used to be sorted the same way, which paired the STRONGEST
    # challenger with the STRONGEST elder — the widest gap, the easiest swap — so
    # SWAP_MARGIN was tested against a contest that was never close and the seat
    # nearest the line got taken on a hair's lead. Live: Sandeep beat Tere by
    # 0.020, well inside the 0.05 deadband, yet Tere was carded for demotion
    # because the code matched her against pax (+0.100) instead.
    prom_by_rank = sorted(want_promote, key=lambda t: -rank[t])
    out_by_rank = sorted(outranked, key=lambda t: rank[t])
    paired = min(len(prom_by_rank), len(out_by_rank))
    for i in range(paired):
        m, e = prom_by_rank[i], out_by_rank[i]
        if scores[m]["score"] - scores[e]["score"] >= SWAP_MARGIN:
            promotable.add(m)
            demotable.add(e)
            demote_reasons[e] = "outranked"
        # else: deadband — the seat holds, the challenge is cancelled this week
    # Unpaired promotes = genuine empty slots (below-floor growth): promote.
    for m in prom_by_rank[paired:]:
        promotable.add(m)
    # Unpaired outranked = over-ceiling overflow: demote to return to the band.
    for e in out_by_rank[paired:]:
        demotable.add(e)
        demote_reasons[e] = "outranked"

    return {
        "n": n,
        "floor": floor,
        "ceil": ceil,
        "current_elders": current_elders,
        "rank": rank,
        "median": median,
        "promotable": promotable,
        "demotable": demotable,
        "demote_reasons": demote_reasons,
        "should_be": should_be,
    }


def elder_evidence(conn, tag: str, now: str | None = None) -> dict | None:
    """The 'why this member is Elder-worthy' facts, for composing a grounded
    promotion post (§3.2). Elixir's OWN reasoning — score, rank, and the
    strongest components — so #clan-events explains the promotion instead of a
    bare '🎉 promoted!'. Returns None if the member isn't rankable (leadership)."""
    from engine.normalize import ranked_league_name

    now = now or utcnow()
    week_anchor = now[:10]
    try:
        scores = _elder_scores(conn, now, week_anchor)
    except Exception:
        return None
    me = scores.get(tag)
    if not me:
        return None
    order = sorted(scores.items(), key=lambda kv: (-kv[1]["score"], -kv[1]["tenure"], kv[0]))
    rank = next((i + 1 for i, (t, _) in enumerate(order) if t == tag), None)
    league = me.get("ranked_league") or 0
    return {
        "elder_score": round(me["score"], 3),
        "rank": rank,
        "roster_size": len(scores),
        "war_deck_rate": round(me.get("war_rate") or 0.0, 2),
        "ranked_league": league,
        "ranked_league_name": ranked_league_name(league) if league >= RANKED_FLOOR_LEAGUE else None,
        "ranked_battles": me.get("ranked_battles") or 0,
        "ranked_pct": round(me.get("ranked_pct") or 0.0, 2),
        "donations_4wk_avg": round(me.get("donations") or 0),
        "competitive": round(me.get("competitive") or 0.0, 2),
    }


def member_participation_facts(conn, tag: str, now: str | None = None) -> str:
    """What a MEMBER may hear about their own contribution, as a plain phrase.

    Deliberately the member-safe subset of :func:`elder_evidence`: the score,
    percentile, rank and roster size are leadership internals and are dropped.
    What remains is what the member actually did, in the same vocabulary the
    public weekly Elder Standing uses ("100% war decks, 213 ranked battles,
    ~11 donations/wk") so the two surfaces can never describe the same person
    differently. Returns "" when there is nothing rankable to say.
    """
    try:
        evidence = elder_evidence(conn, tag, now)
    except Exception:
        return ""
    if not evidence:
        return ""
    bits: list[str] = []
    war_rate = evidence.get("war_deck_rate")
    if war_rate is not None:
        bits.append(f"{round(float(war_rate) * 100)}% war decks")
    battles = evidence.get("ranked_battles") or 0
    if battles:
        league = evidence.get("ranked_league_name")
        bits.append(f"{battles} ranked battles" + (f" in {league}" if league else ""))
    donations = evidence.get("donations_4wk_avg") or 0
    if donations:
        bits.append(f"~{donations} donations a week")
    return ", ".join(bits)


# ------------------------------------------------------ kick path (§3.3)


def run_tick_evaluators(
    conn,
    now: str | None = None,
    *,
    readiness: dict | None = None,
    materialization_id: int | None = None,
) -> list[dict]:
    """Continuous kick_state evaluation (Q1 reactive path). Returns the
    transitions that crossed into 'recommended' this run — the caller raises
    the leader actions. States only; the weekly grain never moves here."""
    now = now or utcnow()
    now_dt = _parse_ts(now)
    members = conn.execute(
        """SELECT mm.player_tag, mm.tenure_days, mm.role, mm.kick_state,
                  mm.kick_state_since, mm.state_json, p.last_seen_at,
                  p.display_name, p.current_name,
                  (SELECT MAX(cm2.joined_at) FROM clan_memberships cm2
                   WHERE cm2.player_tag = mm.player_tag AND cm2.left_at IS NULL)
                  AS membership_joined_at
           FROM member_management mm
           LEFT JOIN players p ON p.player_tag = mm.player_tag
           WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                         WHERE cm.player_tag = mm.player_tag AND cm.left_at IS NULL)"""
    ).fetchall()
    epoch_row = conn.execute("SELECT MIN(observed_at) FROM battle_events").fetchone()
    if not epoch_row or not epoch_row[0]:
        if readiness is not None:
            conn.execute(
                """UPDATE member_management SET
                       judgment_status = 'held',
                       judgment_reason = 'battle_stream_empty',
                       evidence_as_of = NULL,
                       materialization_id = ?
                   WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                                 WHERE cm.player_tag = member_management.player_tag
                                   AND cm.left_at IS NULL)""",
                (materialization_id,),
            )
        return []  # fresh-cut warm-up — no data, explicitly no judgment

    if readiness is not None:
        ready_members = []
        member_readiness = readiness.get("members") or {}
        for member in members:
            state = member_readiness.get(member["player_tag"]) or {
                "status": "held",
                "reasons": ["freshness_unknown"],
                "evidence_as_of": None,
            }
            status = state.get("status") or "held"
            reasons = state.get("reasons") or []
            conn.execute(
                """UPDATE member_management SET
                       judgment_status = ?, judgment_reason = ?,
                       evidence_as_of = ?, materialization_id = ?
                   WHERE player_tag = ?""",
                (
                    status,
                    ",".join(reasons) if reasons else None,
                    state.get("evidence_as_of"),
                    materialization_id,
                    member["player_tag"],
                ),
            )
            if status == "ready":
                ready_members.append(member)
        members = ready_members

    fired: list[dict] = []
    # Slot scarcity: an idle member only costs a slot when the clan is full, so
    # the contribution grace fades with open slots — full grace when empty, zero
    # at 50/50. members are exactly the active roster (open-membership rows above).
    slack = max(0, ROSTER_CAP - len(members)) / ROSTER_CAP
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
            candidates = [_parse_ts(v) for v in (m["membership_joined_at"], m["last_seen_at"]) if v]
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
        else:
            # Contribution grace = the SAME floor that earns elder (recent war
            # participation OR recent ranked participation — ranked counts
            # equally). It only buys extra confirm days while there are open
            # slots, fading to 0 when full — an idle member only costs a slot at
            # a full roster. Watch/at_risk are unaffected, so they still surface
            # on the watchlist; only the card is delayed.
            confirm_days = KICK_CONFIRM_DAYS
            if slack > 0 and (
                _passes_war_floor(conn, tag, now) or _passes_ranked_floor(conn, tag, now)
            ):
                confirm_days += round(KICK_CONTRIB_GRACE_MAX * slack)
            if days_idle >= KICK_AT_RISK_DAYS + confirm_days:
                new_state = "recommended"
            elif days_idle >= KICK_AT_RISK_DAYS:
                new_state = "at_risk"
            elif days_idle >= KICK_WATCH_DAYS:
                new_state = "watch"
            else:
                new_state = "none"
            # Guards: elder+ never fires the reactive path (§3.3);
            # an open leadership watch memory (a member who told leaders they'll
            # be away) suppresses 'recommended' → grace until it lapses.
            if new_state == "recommended" and (
                (m["role"] or "member") in ELDER_PLUS
                or _has_leadership_hold(tag)
                or _has_member_shield(tag)
            ):
                new_state = "at_risk"

        if new_state != state:
            if new_state == "recommended":
                fired.append(
                    {
                        "player_tag": tag,
                        "player_name": m["display_name"] or m["current_name"] or tag,
                        "days_idle": round(days_idle, 1),
                        "from_state": state,
                    }
                )
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
             -- Leadership-initiated manual actions are NOT reconciled against
             -- the evaluator states: a human deliberately raised them (e.g. an
             -- off-cycle promotion), so the state machine has no standing to
             -- auto-withdraw them (live 2026-07-05: a manual Atternam promotion
             -- was killed by this reconciler because his promote_state was
             -- still 'none' pre-accrual).
             AND COALESCE(lar.source_signal_type, '') != 'leadership_manual'
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
            withdrawn.append(
                {
                    "player_tag": row["target_player_tag"],
                    "kind": kind,
                    "action_type": action_type,
                    "count": count,
                    "reason": reason,
                }
            )
    return withdrawn


def _iso_naive(value: str | None):
    """Parse a stored ISO stamp (with or without a trailing Z) to a naive UTC
    datetime. Storage stamps (decided_at, expires_at) have no Z; the engine's
    ``now`` carries one — normalise both. None/garbage → None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().rstrip("Z"))
    except ValueError:
        return None


def _renomination_blocked_until(conn, tag: str, action_type: str, cooldown_days: int):
    """When may a fresh card be raised for (tag, action_type)? Reads the latest
    DECIDED card: if it was a decline, returns the naive-UTC datetime before
    which we must not re-raise — ``decided_at + cooldown``, or a longer
    note-derived ``expires_at`` ("revisit in a month"). Returns None when the
    last decision was not a decline (nothing to hold) or there is no decided
    card, so first-time nominations are never blocked."""
    row = conn.execute(
        """SELECT status, decided_at, expires_at
             FROM leader_action_recommendations
            WHERE target_player_tag = ? AND action_type = ?
              AND status IN ('done', 'rejected')
              AND COALESCE(is_test, 0) = 0
            ORDER BY decided_at DESC, action_id DESC LIMIT 1""",
        (tag, action_type),
    ).fetchone()
    if row is None or row["status"] != "rejected":
        return None
    decided_dt = _iso_naive(row["decided_at"])
    if decided_dt is None:
        return None
    ready = decided_dt + timedelta(days=cooldown_days)
    expires_dt = _iso_naive(row["expires_at"])
    if expires_dt is not None and expires_dt > ready:
        ready = expires_dt
    return ready


def renominate_after_cooldown(conn, now: str | None = None) -> list[dict]:
    """Kick-only re-nomination sweep. ``run_tick_evaluators`` fires a kick card
    only on the transition INTO 'recommended', so a member declined once but
    persistently idle would never resurface (their state stays 'recommended', no
    new transition). Re-raise them once the decline cooldown lapses and they
    STILL qualify. Idempotent: an open 'proposed' kick card suppresses it, so it
    creates at most one card per decline cycle.

    Promote/demote are NOT swept here — they re-nominate through the weekly
    review (which re-emits every eligible member each Monday), gated by the same
    ``_renomination_blocked_until`` window (see ``run_weekly_review``). Sweeping
    them per-tick would create cards off the weekly grain (management.md §2)."""
    now = now or utcnow()
    now_dt = _iso_naive(now)
    if now_dt is None:
        return []
    rows = conn.execute(
        """SELECT mm.player_tag AS player_tag,
                  COALESCE(p.display_name, mm.player_tag) AS player_name
             FROM member_management mm
             LEFT JOIN players p ON p.player_tag = mm.player_tag
            WHERE mm.kick_state = 'recommended'
              AND NOT EXISTS (
                  SELECT 1 FROM leader_action_recommendations l3
                   WHERE l3.target_player_tag = mm.player_tag
                     AND l3.action_type = 'kick_recommendation'
                     AND l3.status = 'proposed'
                     AND COALESCE(l3.is_test, 0) = 0
              )"""
    ).fetchall()
    fired: list[dict] = []
    for row in rows:
        if _premise_still_rejected(conn, row["player_tag"], "kick_recommendation"):
            continue  # leader rejected the premise; evidence unchanged → stay blocked
        blocked_until = _renomination_blocked_until(
            conn,
            row["player_tag"],
            "kick_recommendation",
            KICK_RENOMINATE_COOLDOWN_DAYS,
        )
        if blocked_until is None:
            continue  # last decision wasn't a decline → nothing to re-raise
        if blocked_until > now_dt:
            continue  # still inside the cooldown / note window
        fired.append(
            {
                "player_tag": row["player_tag"],
                "player_name": row["player_name"],
                "action_type": "kick_recommendation",
                "rationale": (
                    "Re-nominated: kick candidacy sustained "
                    f"{KICK_RENOMINATE_COOLDOWN_DAYS}+ days after the last card was declined."
                ),
            }
        )
    return fired


def _has_leadership_hold(tag: str) -> bool:
    """The LOA exception: True only when a member has an explicit *leave hold* —
    a `Hold:`-titled memory recording that they told leaders they'll be away
    (grace until it expires / they return).

    NOT the same as a `Watch:` memory. `flag_member_watch` writes `Watch:` notes
    as the brain's *inference* observations ("no battle activity in 10 days,
    trending toward removal") — an inactivity flag, the OPPOSITE of an approved
    leave. Matching those (as this guard used to) was self-defeating: the more
    the brain noticed a member was idle, the more it shielded them from the kick
    card documenting that idleness (2026-07-11: xOMENKILLER, 10d dark on a full
    roster, held at at_risk by his own auto-written watch note). A leave hold is
    a deliberate `Hold:`/`Away:`/`LOA:` signal, distinct from an observation.

    v5.1 memory pass: memories live in the engine DB (memory.md D1); read
    `memories` directly (member_tag column, not the never-populated link table).
    Fail-open to False so a memory hiccup never blocks the pipeline silently —
    the policy gate still applies."""
    try:
        from engine.db import connect

        mconn = connect()
        row = mconn.execute(
            """SELECT 1 FROM memories m
               WHERE m.member_tag = ? AND m.retired_at IS NULL
                 AND (m.title LIKE 'Hold:%' OR m.title LIKE 'Away:%'
                      OR m.title LIKE 'LOA:%')
                 -- Compare as TIME, not text. `expires_at` is written from an
                 -- LLM-supplied `away_until` and is stored in mixed shapes (live
                 -- DB: 21 date-only, 63 ending in Z), so a lexicographic `>`
                 -- gives wrong answers: a date-only "2026-08-03" tests FALSE from
                 -- 00:00:00Z that day — a leader granting leave "until the 3rd"
                 -- lost the member's shield at 7pm Chicago on the 2nd. A shape it
                 -- cannot parse (e.g. "08/03/2026") compared FALSE immediately,
                 -- silently leaving the member unprotected while the card said
                 -- the hold was recorded. A date-only value covers that whole day.
                 AND (m.expires_at IS NULL
                      OR julianday(
                           CASE WHEN length(m.expires_at) = 10
                                THEN m.expires_at || 'T23:59:59'
                                ELSE replace(m.expires_at, 'Z', '') END
                         ) > julianday('now')
                      -- Unparseable shape → julianday() is NULL. Fail SAFE: keep
                      -- the member shielded rather than silently exposing them.
                      OR julianday(replace(m.expires_at, 'Z', '')) IS NULL)
               LIMIT 1""",
            (tag,),
        ).fetchone()
        mconn.close()
        return row is not None
    except Exception:
        return False


# --------------------------------------------------- leader-note effects (v7)
#
# A leader who declines a card can reject its PREMISE ("no longer relevant to
# the clan", "wrong call") rather than just decline-for-now. That must stop the
# engine re-raising the SAME card on the SAME evidence, while still re-raising
# when materially new evidence appears. We pin the evidence with a fingerprint at
# rejection time and compare it live in the re-nomination gate.
#
# The anchor is the member's last MATERIAL evidence — for a kick, the last battle
# (or, for a never-battled member, their clan join). It deliberately excludes
# players.last_seen_at (being "seen online" without battling is not new kick
# evidence, and would wrongly unblock a rejected premise). For a role
# recommendation the material event is a role change, so the anchor is the
# member's current role. Both write (reject) and read (gate) call the same helper,
# so the fingerprint is reproducible for an unchanged member.


def _kick_evidence_anchor(conn, tag: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(battle_time) FROM battle_events WHERE player_tag = ?", (tag,)
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    row = conn.execute(
        "SELECT MAX(joined_at) FROM clan_memberships WHERE player_tag = ? AND left_at IS NULL",
        (tag,),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    return None


def _premise_evidence_anchor(conn, tag: str, action_type: str) -> str | None:
    if action_type == "kick_recommendation":
        return _kick_evidence_anchor(conn, tag)
    if action_type in ("promotion_recommendation", "demotion_recommendation"):
        row = conn.execute(
            "SELECT role FROM member_management WHERE player_tag = ?", (tag,)
        ).fetchone()
        if row and row[0]:
            return f"role:{row[0]}"
        return None
    return None


def _premise_fingerprint(conn, tag: str, action_type: str) -> str | None:
    anchor = _premise_evidence_anchor(conn, tag, action_type)
    if anchor is None:
        return None
    return hashlib.sha256(f"{action_type}|{anchor}".encode("utf-8")).hexdigest()[:16]


def compute_premise_fingerprint(tag: str, action_type: str) -> str | None:
    """Fingerprint the current evidence for (tag, action_type), opening a
    short-lived connection. Called from the async note interpreter at rejection
    time; the gate recomputes it live via ``_premise_fingerprint``."""
    try:
        from engine.db import connect

        conn = connect()
        try:
            return _premise_fingerprint(conn, tag, action_type)
        finally:
            conn.close()
    except Exception:
        return None


def _premise_still_rejected(conn, tag: str, action_type: str) -> bool:
    """True when the leader rejected this recommendation's premise AND the
    evidence is unchanged since — so re-nomination stays blocked. A materially
    changed anchor (a fresh battle, a role change) flips this to False and the
    normal cooldown gate takes over. Fail-open to False so a hiccup never blocks
    the pipeline silently."""
    try:
        row = conn.execute(
            """SELECT premise_rejected, premise_fingerprint
                 FROM leader_action_recommendations
                WHERE target_player_tag = ? AND action_type = ?
                  AND status IN ('done', 'rejected')
                  AND COALESCE(is_test, 0) = 0
                ORDER BY decided_at DESC, action_id DESC LIMIT 1""",
            (tag, action_type),
        ).fetchone()
    except Exception:
        return False
    if row is None or not row["premise_rejected"]:
        return False
    stored = row["premise_fingerprint"]
    if not stored:
        return False
    return _premise_fingerprint(conn, tag, action_type) == stored


def _has_member_shield(tag: str) -> bool:
    """True when a leader recorded a durable protective context on this member —
    an ``alt account`` or an explicit ``never flag``. Gated on a structured
    ``memory_tags`` tag (``shield:alt`` / ``shield:never_flag``), NOT a title
    prefix (title-LIKE matching caused the xOMENKILLER false-shield). Time-boxed
    LOAs keep flowing through the ``Hold:``/``LOA:`` title path in
    ``_has_leadership_hold``. Fail-open to False."""
    try:
        from engine.db import connect

        mconn = connect()
        row = mconn.execute(
            """SELECT 1 FROM memories m
                 JOIN memory_tags t ON t.memory_id = m.memory_id
                WHERE m.member_tag = ? AND m.retired_at IS NULL
                  AND t.tag IN ('shield:alt', 'shield:never_flag')
                  AND (m.expires_at IS NULL
                       OR m.expires_at > strftime('%Y-%m-%dT%H:%M:%S', 'now'))
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
        """SELECT mm.*, COALESCE(p.display_name, p.current_name) AS player_name
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
            "not ranked (leadership)"
            if not sc
            else f"score {sc['score']:.2f} [competitive {sc['competitive']:.2f} "
            f"(war {sc['war_pct']:.2f} / ranked {sc['ranked_pct']:.2f} "
            f"[{sc['ranked_battles']} btl], league {sc['ranked_league']}), "
            f"donations {sc['donation_pct']:.2f}], "
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
        # A recommendation must describe something still TRUE. `eligible` is a
        # sticky state, so without this guard the review re-raised a promote card
        # every week for a member who already held the role: Fullboat and dez42
        # were promoted 2026-07-20 and were handed fresh "promote to Elder" cards
        # on 2026-07-27 (R214/R215), while the Elder Standing report was already
        # listing them as Elders. The outcome happening IS the completion — clear
        # the machine rather than asking for it again.
        if p_state == "eligible" and role != "member":
            p_state, p_weeks, p_miss = "none", 0, 0
        st["promote_misses"] = p_miss
        if p_state == "eligible":
            promote_eligible.append(
                _eligibility_row(
                    m,
                    kind="promote",
                    rationale=f"Below-band elder slot: {band_evidence}; held {p_weeks} wk.",
                )
            )

        # Demote (elder → member). Cadence depends on WHY (§3.4): abandonment
        # of the competitive floor is fast (DEMOTE_WEEKS=2); being outranked
        # matches the challenger's promotion cadence (PROMOTE_QUALIFYING_WEEKS=3)
        # so the swap lands together and the count never dips mid-swap.
        d_state = m["demote_state"] or "none"
        d_gate = tag in band["demotable"]
        d_reason = band.get("demote_reasons", {}).get(tag)
        d_threshold = DEMOTE_WEEKS if d_reason == "abandoned" else PROMOTE_QUALIFYING_WEEKS
        d_weeks = st.get("demote_weeks", 0)
        if d_gate:
            d_weeks += 1
            d_state = "eligible" if d_weeks >= d_threshold else "building"
        else:
            if d_state in ("eligible", "recommended"):
                withdrawn.append({"player_tag": tag, "kind": "demote"})
            d_state, d_weeks = "none", 0
        # Same rule on the way down: never ask to demote someone who is already
        # a member. d_gate normally clears this, but the guard is explicit so the
        # two directions cannot drift apart.
        if d_state == "eligible" and role != "elder":
            d_state, d_weeks = "none", 0
        st["demote_weeks"] = d_weeks
        if d_state == "eligible":
            reason = (
                "abandoned both war and ranked duty"
                if d_reason == "abandoned"
                else "outranked — the elder seat goes to a higher-ranked member"
            )
            demote_eligible.append(
                _eligibility_row(
                    m,
                    kind="demote",
                    rationale=f"{reason}: {band_evidence}; held {d_weeks} wk.",
                )
            )

        st["week_anchor"] = week_anchor
        conn.execute(
            """UPDATE member_management SET
                   week_anchor = ?, computed_at = ?,
                   sustained_donor = ?, war_reliable = ?, battle_active = ?,
                   promote_state = ?, promote_qualifying_weeks = ?,
                   demote_state = ?, state_json = ?
               WHERE player_tag = ?""",
            (
                week_anchor,
                now,
                donor,
                war,
                battle,
                p_state,
                p_weeks,
                d_state,
                json.dumps(st, ensure_ascii=False),
                tag,
            ),
        )
        rows_out.append(
            {
                "player_tag": tag,
                "role": role,
                "sustained_donor": donor,
                "war_reliable": war,
                "battle_active": battle,
                "promote_state": p_state,
                "promote_qualifying_weeks": p_weeks,
                "demote_state": d_state,
                "kick_state": m["kick_state"] or "none",
            }
        )

    # Honour the decline cooldown: a member the leaders recently declined stays
    # 'eligible' (they still qualify) but is not re-carded until the cooldown
    # (or a longer decline-note window) lapses — otherwise a Monday decline
    # reappears the very next Monday, ignoring the leader's "not now".
    now_dt = _iso_naive(now)

    def _past_cooldown(rows: list[dict], action_type: str) -> list[dict]:
        cooldown = _RENOMINATE_COOLDOWN_DAYS[action_type]
        kept: list[dict] = []
        for r in rows:
            # A member with a durable protective context (alt / never-flag) is
            # neither promoted nor demoted; a leader-rejected premise stays
            # blocked until the member's role materially changes.
            if _has_member_shield(r["player_tag"]):
                continue
            if _premise_still_rejected(conn, r["player_tag"], action_type):
                continue
            blocked = _renomination_blocked_until(conn, r["player_tag"], action_type, cooldown)
            if blocked is not None and now_dt is not None and blocked > now_dt:
                continue
            kept.append(r)
        return kept

    promote_eligible = _past_cooldown(promote_eligible, "promotion_recommendation")
    demote_eligible = _past_cooldown(demote_eligible, "demotion_recommendation")

    return {
        "week_anchor": week_anchor,
        "promote_eligible": promote_eligible,
        "demote_eligible": demote_eligible,
        "withdrawn": withdrawn,
        "rows": rows_out,
    }
