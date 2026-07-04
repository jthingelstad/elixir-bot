"""Per-stream recognizers (recognition.md §4).

Battle and player streams feed one shared celebrate pipeline (coalesce →
cohort → accrue → ledger claim → intent); clan and war moments take the
direct path with per-type guards. Every moment — posted or suppressed —
claims the ledger first (§5).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from engine import delivery
from engine.db import cursor_get, cursor_set
from engine.recognition import compose, ledger
from engine.recognition.scorer import (
    REASON_ACCRUING,
    REASON_COALESCED,
    REASON_COHORT,
    Candidate,
    base_score,
    cohort_waves,
    decide,
    parse_utc,
    sort_key,
)

log = logging.getLogger("elixir.engine.recognition")

# C1 — kick-suppression window (detectors.py:542)
KICK_SUPPRESS_DAYS = 14

# ranked_pulse thresholds (detectors.py:400–405, verbatim)
RANKED_WINDOW_DAYS = 7
RANKED_MIN_BATTLES = 12
RANKED_MIN_DECIDED = 12
RANKED_MIN_WINS = 9
RANKED_MIN_WIN_RATE = 0.70

# trophy_push thresholds (detectors.py:334–335, verbatim)
PUSH_MIN_BATTLES = 3
PUSH_MIN_DELTA = 100
_PUSH_SCAN_DAYS = 3   # window for run re-assembly across cursor boundaries


def _payload(row) -> dict:
    try:
        return json.loads(row["payload_json"]) if row["payload_json"] else {}
    except (TypeError, ValueError):
        return {}


def _cr_compact(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


# ------------------------------------------------------------- battle stream

def _battle_id(row) -> str:
    return row["dedup_key"]


# Canonical Trophy Road arenas have stable ids; above this id the "arena" is
# the seasonal-league zone whose ids/names rotate monthly ("PANCAKES!",
# "Summit of Heroes", ...) — verified against live battle data 2026-07-03:
# ids <= 54000016 (Dragon Spa) hold stable names/trophy bands; ids above churn
# with the season. Seasonal renames + the season trophy reset otherwise read
# as arena-ups (the go-live tick posted 7 of them). One constant to revisit
# if Supercell extends the road.
ARENA_UP_MAX_CANONICAL_ID = 54000016


def _arena_up_candidates(conn, tags: set[str]) -> list[Candidate]:
    """Arena Up from battle pairs (§11: battle primary — the deciding battle is
    the moment). No arena-threshold table exists in the repo (verified:
    cr_knowledge.py has no arena data), so the deterministic observable is the
    arena transition between consecutive trophy-road battles: a win with
    positive trophy change followed by a battle in a higher arena — gated to
    the canonical road (ARENA_UP_MAX_CANONICAL_ID). The profile-side
    arena_changed event (player stream) backstops poll gaps and claims the
    same key."""
    out: list[Candidate] = []
    for tag in sorted(tags):
        rows = conn.execute(
            """SELECT dedup_key, battle_time, arena_id, arena_name, outcome,
                      trophy_change
               FROM battle_events
               WHERE player_tag = ? AND is_ladder = 1 AND arena_id IS NOT NULL
               ORDER BY battle_time ASC""",
            (tag,),
        ).fetchall()
        for prev, cur in zip(rows, rows[1:]):
            if (
                cur["arena_id"] > prev["arena_id"]
                and cur["arena_id"] <= ARENA_UP_MAX_CANONICAL_ID
                and (cur["arena_name"] or "") != (prev["arena_name"] or "")
                and prev["outcome"] == "W"
                and (prev["trophy_change"] or 0) > 0
            ):
                out.append(Candidate(
                    key=f"arena_up:{tag}:{cur['arena_id']}",
                    event_type="arena_up",
                    subject_tag=tag,
                    occurred_at=prev["battle_time"],   # exact — the deciding battle
                    payload={
                        "subject_tag": tag, "event_type": "arena_up",
                        "arena_id": cur["arena_id"], "arena_name": cur["arena_name"],
                        "prev_arena_id": prev["arena_id"],
                        "prev_arena_name": prev["arena_name"],
                        "timing": "exact", "occurred_at": prev["battle_time"],
                    },
                    event_refs=[_battle_id(prev), _battle_id(cur)],
                ))
    return out


def _trophy_push_candidates(conn, tags: set[str], now: str) -> list[Candidate]:
    """Run accumulation ported from BattleTrophyPushDetector (detectors.py
    ~322–390): consecutive positive trophy-change competitive battles; a run
    ends at a non-positive battle or end-of-stream; keyed on the run's LAST
    battle."""
    cutoff = _cr_compact((parse_utc(now) or datetime.now(timezone.utc))
                         - timedelta(days=_PUSH_SCAN_DAYS))
    out: list[Candidate] = []

    def flush(tag, run):
        delta = sum(r["trophy_change"] for r in run)
        if len(run) >= PUSH_MIN_BATTLES and delta >= PUSH_MIN_DELTA:
            last = run[-1]
            out.append(Candidate(
                key=f"trophy_push:{tag}:{last['dedup_key']}",
                event_type="trophy_push",
                subject_tag=tag,
                occurred_at=last["battle_time"],
                payload={
                    "subject_tag": tag, "event_type": "trophy_push",
                    "battle_count": len(run), "trophy_delta": delta,
                    "from_trophies": run[0]["starting_trophies"],
                    "to_trophies": last["starting_trophies"] + last["trophy_change"],
                    "timing": "exact", "occurred_at": last["battle_time"],
                },
                event_refs=[r["dedup_key"] for r in run],
            ))

    for tag in sorted(tags):
        rows = conn.execute(
            """SELECT dedup_key, battle_time, trophy_change, starting_trophies
               FROM battle_events
               WHERE player_tag = ? AND is_competitive = 1
                 AND trophy_change IS NOT NULL AND starting_trophies IS NOT NULL
                 AND battle_time >= ?
               ORDER BY battle_time ASC, dedup_key ASC""",
            (tag, cutoff),
        ).fetchall()
        run: list = []
        for r in rows:
            if r["trophy_change"] > 0:
                run.append(r)
            else:
                flush(tag, run)
                run = []
        flush(tag, run)
    return out


def _ranked_pulse_candidate(conn, now: str) -> list[Candidate]:
    """Port of RankedActivityPulseDetector: one clan-wide candidate per day,
    only when volume + record are both strong. Membership = open
    clan_memberships row (§7)."""
    anchor = parse_utc(now) or datetime.now(timezone.utc)
    cutoff = _cr_compact(anchor - timedelta(days=RANKED_WINDOW_DAYS))
    day_key = _cr_compact(anchor)[:8]
    rows = conn.execute(
        """SELECT b.player_tag AS tag, p.current_name AS name,
                  COUNT(*) AS battles,
                  SUM(CASE WHEN b.outcome = 'W' THEN 1 ELSE 0 END) AS wins,
                  SUM(CASE WHEN b.outcome = 'L' THEN 1 ELSE 0 END) AS losses,
                  MAX(b.battle_time) AS last_battle_time
           FROM battle_events b
           JOIN players p ON p.player_tag = b.player_tag
           JOIN clan_memberships cm ON cm.player_tag = b.player_tag
                AND cm.left_at IS NULL
           WHERE b.mode_group = 'ranked' AND b.battle_time >= ?
             AND b.outcome IN ('W', 'L')
             AND p.current_name IS NOT NULL AND TRIM(p.current_name) != ''
           GROUP BY b.player_tag, p.current_name""",
        (cutoff,),
    ).fetchall()
    candidates = []
    for row in rows:
        battles, wins, losses = int(row["battles"]), int(row["wins"]), int(row["losses"])
        decided = wins + losses
        win_rate = round(wins / decided, 3) if decided else 0.0
        if (battles >= RANKED_MIN_BATTLES and decided >= RANKED_MIN_DECIDED
                and wins >= RANKED_MIN_WINS and win_rate >= RANKED_MIN_WIN_RATE):
            candidates.append((row, battles, wins, losses, win_rate))
    if not candidates:
        return []
    row, battles, wins, losses, win_rate = max(
        candidates,
        key=lambda i: (i[4], i[2], i[1], str(i[0]["last_battle_time"] or "")),
    )
    tag = row["tag"]
    return [Candidate(
        key=f"ranked_pulse:{tag}:{day_key}",
        event_type="ranked_pulse",
        subject_tag=tag,
        occurred_at=row["last_battle_time"],
        payload={
            "subject_tag": tag, "event_type": "ranked_pulse",
            "window_days": RANKED_WINDOW_DAYS, "battle_count": battles,
            "wins": wins, "losses": losses, "win_rate": win_rate,
            "timing": "exact", "occurred_at": row["last_battle_time"],
        },
        event_refs=[f"battle_events:{tag}:{row['last_battle_time']}"],
    )]


def battle_candidates(conn, now: str) -> tuple[list[Candidate], int]:
    """Derived battle moments from battles since the cursor (events.md §2)."""
    pos = cursor_get(conn, "recognize:battle")
    rows = conn.execute(
        "SELECT rowid, player_tag FROM battle_events WHERE rowid > ? ORDER BY rowid",
        (pos,),
    ).fetchall()
    if not rows:
        return [], pos
    new_pos = rows[-1]["rowid"]
    tags = {r["player_tag"] for r in rows}
    out = _arena_up_candidates(conn, tags)
    out += _trophy_push_candidates(conn, tags, now)
    out += _ranked_pulse_candidate(conn, now)
    return out, new_pos


# ------------------------------------------------------------- player stream

def player_candidates(conn) -> tuple[list[Candidate], int]:
    """Celebrate candidates from new player_events. arena_changed becomes an
    arena_up claim at 85 (recognition.md §4: it has no score of its own; it
    exists only to reach that key)."""
    pos = cursor_get(conn, "recognize:player")
    rows = conn.execute(
        "SELECT * FROM player_events WHERE event_id > ? ORDER BY event_id",
        (pos,),
    ).fetchall()
    if not rows:
        return [], pos
    out: list[Candidate] = []
    for r in rows:
        payload = _payload(r)
        tag = r["player_tag"]
        et = r["event_type"]
        base = {"subject_tag": tag, "event_type": et,
                "timing": r["timing"], "occurred_at": r["observed_at"], **payload}
        if et == "arena_changed":
            arena_id = payload.get("arena_id")
            if arena_id is None or arena_id > ARENA_UP_MAX_CANONICAL_ID:
                continue  # seasonal-league zone: renames are not arena-ups
            out.append(Candidate(
                key=f"arena_up:{tag}:{arena_id}",
                event_type="arena_up",
                subject_tag=tag,
                occurred_at=r["observed_at"],   # estimated — profile backstop
                payload={**base, "event_type": "arena_up",
                         "arena_name": payload.get("arena_name")},
                event_refs=[r["dedup_key"]],
                arrival=r["event_id"],
            ))
            continue
        score, _ = base_score(et, payload)
        if score <= 0:
            continue
        out.append(Candidate(
            key=r["dedup_key"], event_type=et, subject_tag=tag,
            occurred_at=r["observed_at"], payload=base,
            event_refs=[r["dedup_key"]], arrival=r["event_id"],
        ))
    return out, rows[-1]["event_id"]


# --------------------------------------------------------- celebrate pipeline

def run_celebrate_pipeline(conn, candidates: list[Candidate], now: str) -> dict:
    """Coalesce → cohort → accrue → claim → intent (recognition.md §3)."""
    counters = {"celebrate_posted": 0, "celebrate_suppressed": 0, "cohort_posted": 0}
    if not candidates:
        return counters

    # Cohort waves consume the accruing (non-bypass) members of a wave; bypass
    # moments already strong enough to post individually stay individual.
    consumed: set[str] = set()
    for wave_key, members in sorted(cohort_waves(candidates).items()):
        wave_members = [m for m in members if not base_score(m.event_type, m.payload)[1]]
        if len({m.subject_tag for m in wave_members}) < 3:
            continue
        refs = [m.key for m in wave_members]
        if ledger.claim(conn, wave_key, "player", refs, 0):
            event_type = wave_key.split(":")[1]
            names = []
            for m in wave_members:
                names.append({"tag": m.subject_tag,
                              "name": compose.resolve_name(conn, m.subject_tag)})
            intent_id = delivery.raise_intent(
                conn, wave_key, "cohort:cohort_wave", compose.route("cohort:x", "public"),
                "public",
                {"event_type": f"cohort_wave:{event_type}", "wave_type": event_type,
                 "members": names, "member_count": len(names)},
                now,
            )
            ledger.attach_intent(conn, wave_key, intent_id)
            counters["cohort_posted"] += 1
        for m in wave_members:
            if ledger.claim(conn, m.key, "player", m.event_refs, 0):
                ledger.record_suppression(conn, m.key, REASON_COHORT,
                                          {"wave_key": wave_key})
            consumed.add(m.key)

    remaining = [c for c in candidates if c.key not in consumed]
    by_subject: dict[str, list[Candidate]] = {}
    for c in remaining:
        by_subject.setdefault(c.subject_tag, []).append(c)

    for subject in sorted(by_subject):
        group = by_subject[subject]
        selected = max(group, key=sort_key)
        stream = "battle" if selected.event_type in (
            "arena_up", "trophy_push", "ranked_pulse") else "player"
        if not ledger.claim(conn, selected.key, stream, selected.event_refs, 0):
            # Another stream already recognized this moment (e.g. the battle
            # claimed the arena-up the profile is now confirming). Back off.
            continue
        last = ledger.last_highlight_at(conn, subject)
        post, score, trace = decide(conn, subject, selected, group, last)
        conn.execute(
            "UPDATE recognition_ledger SET score = ? WHERE recognition_key = ?",
            (score, selected.key),
        )
        if post:
            name = compose.resolve_name(conn, subject)
            intent_id = delivery.raise_intent(
                conn, selected.key, f"celebrate:{selected.event_type}",
                compose.route("celebrate:x", "public"), "public",
                {**selected.payload, **({"player_name": name} if name else {}), **trace},
                now,
            )
            ledger.attach_intent(conn, selected.key, intent_id)
            counters["celebrate_posted"] += 1
        else:
            ledger.record_suppression(conn, selected.key, REASON_ACCRUING, trace)
            counters["celebrate_suppressed"] += 1
        for other in group:
            if other is selected:
                continue
            if ledger.claim(conn, other.key, stream, other.event_refs, 0):
                ledger.record_suppression(
                    conn, other.key, REASON_COALESCED,
                    {"selected_key": selected.key},
                )
            counters["celebrate_suppressed"] += 1
    return counters


# --------------------------------------------------------------- clan stream

def _was_kicked(conn, tag: str, observed_at: str) -> bool:
    """C1 kick-suppression: a done kick_recommendation within 14 days means the
    departure was a kick — don't announce it (detectors.py:568–592 query shape)."""
    anchor = parse_utc(observed_at) or datetime.now(timezone.utc)
    cutoff = (anchor - timedelta(days=KICK_SUPPRESS_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    row = conn.execute(
        """SELECT 1 FROM leader_action_recommendations
           WHERE action_type = 'kick_recommendation' AND target_player_tag = ?
             AND status = 'done' AND COALESCE(is_test, 0) = 0
             AND COALESCE(decided_at, proposed_at) >= ? LIMIT 1""",
        (tag, cutoff),
    ).fetchone()
    return row is not None


def clan_recognizer(conn, now: str) -> dict:
    """Direct-post path for clan-social + clan-entity moments (§1 boundary),
    with the §4 guards."""
    counters = {"clan_posted": 0, "clan_suppressed": 0}
    pos = cursor_get(conn, "recognize:clan")
    rows = conn.execute(
        "SELECT * FROM clan_events WHERE event_id > ? ORDER BY event_id", (pos,)
    ).fetchall()
    for r in rows:
        et = r["event_type"]
        tag = r["subject_tag"]
        payload = {"subject_tag": tag, "event_type": et, "clan_tag": r["clan_tag"],
                   "timing": r["timing"], "occurred_at": r["observed_at"],
                   **_payload(r)}
        if not ledger.claim(conn, r["dedup_key"], "clan", [r["dedup_key"]], 0):
            continue
        suppress = None
        if et == "member_left" and tag and _was_kicked(conn, tag, r["observed_at"]):
            suppress = "kick_suppressed"          # the event exists; the post doesn't (C1)
        elif et == "role_changed" and payload.get("direction") == "demoted":
            suppress = "demotion_private"         # leadership reality, not celebration
        if suppress:
            ledger.record_suppression(conn, r["dedup_key"], suppress)
            counters["clan_suppressed"] += 1
            continue
        name = compose.resolve_name(conn, tag)
        if name:
            payload["player_name"] = name
        intent_id = delivery.raise_intent(
            conn, r["dedup_key"], f"clan:{et}", compose.route("clan:x", r["scope"]),
            r["scope"], payload, now,
        )
        ledger.attach_intent(conn, r["dedup_key"], intent_id)
        counters["clan_posted"] += 1
    if rows:
        cursor_set(conn, "recognize:clan", rows[-1]["event_id"])
    return counters


# ---------------------------------------------------------------- war stream

def war_recognizer(conn, clock: dict | None, now: str) -> dict:
    """Direct path, clock-gated (§16.2 phase-appropriate behavior)."""
    counters = {"war_posted": 0, "war_suppressed": 0}
    clock = clock or {}
    pos = cursor_get(conn, "recognize:war")
    rows = conn.execute(
        "SELECT * FROM war_events WHERE event_id > ? ORDER BY event_id", (pos,)
    ).fetchall()
    for r in rows:
        et = r["event_type"]
        payload = {"event_type": et, "season_id": r["season_id"],
                   "section_index": r["section_index"],
                   "timing": r["timing"], "occurred_at": r["observed_at"],
                   **_payload(r)}
        if not ledger.claim(conn, r["dedup_key"], "war", [r["dedup_key"]], 0):
            continue
        suppress = None
        if et == "colosseum_detected":
            suppress = "clock_fact_no_post"       # feeds the clock; the war_day_opened
            #                                       events carry the colosseum framing
        elif et == "war_day_opened" and clock.get("race_finished"):
            suppress = "race_already_won"         # §16.4: urgency drops once won
        if suppress:
            ledger.record_suppression(conn, r["dedup_key"], suppress)
            counters["war_suppressed"] += 1
            continue
        if clock:
            payload["war_clock"] = {
                k: clock.get(k)
                for k in ("phase", "day_index", "is_colosseum_week", "pace_status",
                          "hours_left_in_period", "race_finished")
                if k in clock
            }
            # war_day_index is 0-based engine data; copy must speak human
            # ("battle day 3 of 4"). And right after a day opens, the clock's
            # next-10:00Z boundary reads as minutes-left on a fresh 24h day
            # (CR rolls ~09:37Z; live incident 2026-07-04: "day 2, 14 minutes
            # left" composed at the day-3 open). Correct both for the composer.
            wdi = payload.get("war_day_index")
            if isinstance(wdi, int):
                payload["war_day_human"] = f"battle day {wdi + 1} of 4"
            if et == "war_day_opened":
                hours = payload["war_clock"].get("hours_left_in_period")
                if isinstance(hours, (int, float)) and hours < 12:
                    payload["war_clock"]["hours_left_in_period"] = hours + 24.0
                payload["day_just_opened"] = True
        if et == "season_closed":
            champ, fp = payload.get("war_champ_tag"), payload.get("free_pass_tag")
            payload["war_champ_name"] = compose.resolve_name(conn, champ)
            payload["free_pass_name"] = compose.resolve_name(conn, fp)
            payload["honor_reward_diverged"] = bool(champ and fp and champ != fp)
        intent_id = delivery.raise_intent(
            conn, r["dedup_key"], f"war:{et}", compose.route("war:x", r["scope"]),
            r["scope"], payload, now,
        )
        ledger.attach_intent(conn, r["dedup_key"], intent_id)
        counters["war_posted"] += 1
    if rows:
        cursor_set(conn, "recognize:war", rows[-1]["event_id"])
    return counters
