"""The deterministic notability scorer — constants are law (recognition.md §2–§3).

Ported VERBATIM from event_core/mind/communication.py (threshold 80, 14-day
accrual, base scores, bypass set, dynamic scores, priority sort, cohort waves),
translated to v5.1 event names per events.md §6. The one new constant is
arena_up (85, bypass; same-tick priority 90) — ratified 2026-07-03.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HIGHLIGHT_THRESHOLD = 80                      # communication.py:71
ACCRUAL_WINDOW = timedelta(days=14)           # communication.py:70
REASON_ACCRUING = "player_highlight_accruing"
REASON_COALESCED = "player_highlight_coalesced:same_tick"
REASON_COHORT = "cohort_wave"
POLICY_ID = "player_highlight_score:v1"

# recognition.md §2 — base scores (v5.1 names; scores unchanged)
BASE_SCORES = {
    "ultimate_champion_reached": 120,
    "pol_global_rank_attained": 110,
    "card_level_milestone": 95,
    "career_wins_milestone": 85,
    "collection_level_milestone": 80,
    "level_up": 80,
    "badge_earned": 55,
    "pol_promotion": 45,
    "best_trophies_peak": 40,
    "ranked_pulse": 30,
}

BYPASS_TYPES = {
    "ultimate_champion_reached",
    "pol_global_rank_attained",
    "card_level_milestone",
    "career_wins_milestone",
    "collection_level_milestone",
    "level_up",
    "arena_up",                               # new — ratified 85/bypass
}

# recognition.md §2 — same-tick priority sort (arena_up 90 is the one addition)
CELEBRATE_PRIORITY = {
    "ultimate_champion_reached": 100,
    "pol_global_rank_attained": 95,
    "arena_up": 90,
    "card_level_milestone": 80,
    "card_unlocked": 75,
    "badge_earned": 70,
    "collection_level_milestone": 65,
    "career_wins_milestone": 60,
    "level_up": 55,
    "pol_promotion": 50,
    "best_trophies_peak": 40,
    "ranked_pulse": 15,
    "trophy_push": 10,
}

# Every celebrate-class type (the §1 boundary: these go through the scorer;
# clan-social and war moments take the direct path).
CELEBRATE_TYPES = set(CELEBRATE_PRIORITY)

# recognition.md §3 — cohort waves
COHORT_MIN_MEMBERS = 3
COHORT_WAVE_TYPES = {"badge_earned", "card_level_milestone", "card_unlocked"}

_UTC_MIN = datetime.min.replace(tzinfo=timezone.utc)
_CHICAGO = ZoneInfo("America/Chicago")


def parse_utc(value) -> datetime | None:
    """Parse ISO ('2026-07-03T12:00:00Z') or CR-compact ('20260703T120000.000Z')."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        if "T" in text and "-" not in text.split("T", 1)[0]:
            # CR compact: 20260703T120000.000Z
            core = text.rstrip("Z").split(".", 1)[0]
            try:
                dt = datetime.strptime(core, "%Y%m%dT%H%M%S")
            except ValueError:
                return None
        else:
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def chicago_day(value) -> str | None:
    dt = parse_utc(value)
    return dt.astimezone(_CHICAGO).date().isoformat() if dt else None


def base_score(event_type: str, payload: dict | None) -> tuple[int, bool]:
    """(score, bypass) per recognition.md §2. Dynamic rules first."""
    payload = payload or {}
    if event_type == "card_unlocked":
        rarity = str(payload.get("rarity") or "").strip().lower()
        if rarity == "champion":
            return 90, True
        if rarity == "legendary":
            return 65, False
        return 0, False
    if event_type == "trophy_push":
        try:
            delta = int(payload.get("trophy_delta") or 0)
        except (TypeError, ValueError):
            delta = 0
        return min(45, 25 + max(0, delta - 100) // 50 * 5), False
    if event_type == "arena_up":
        return 85, True
    return BASE_SCORES.get(event_type, 0), event_type in BYPASS_TYPES


@dataclass
class Candidate:
    """A celebrate-class recognition candidate (from any stream)."""

    key: str                    # recognition_key (event dedup_key or derived key)
    event_type: str
    subject_tag: str
    occurred_at: str | None     # event time text (ISO or CR compact)
    payload: dict = field(default_factory=dict)
    event_refs: list = field(default_factory=list)
    arrival: int = 0            # insertion id, for the final sort tiebreak


def sort_key(candidate: Candidate) -> tuple:
    """Port of _candidate_sort_key (communication.py:324–338): priority table,
    then payload magnitude, then time, then arrival order."""
    p = candidate.payload or {}
    magnitude = (
        p.get("milestone") or p.get("level") or p.get("peak")
        or p.get("boundary") or p.get("trophy_delta") or 0
    )
    return (
        CELEBRATE_PRIORITY.get(candidate.event_type, 0),
        magnitude if isinstance(magnitude, (int, float)) else 0,
        parse_utc(candidate.occurred_at) or _UTC_MIN,
        candidate.arrival,
    )


def _evidence_rows(conn, subject_tag: str, anchor: datetime, cutoff_at: str | None):
    """The subject's celebrate-type player_events in the trailing 14-day window,
    cut off at the last posted highlight (recognition.md §3 step 3)."""
    window_start = anchor - ACCRUAL_WINDOW
    rows = conn.execute(
        """SELECT dedup_key, event_type, payload_json, observed_at
           FROM player_events WHERE player_tag = ? ORDER BY event_id""",
        (subject_tag,),
    ).fetchall()
    cutoff_dt = parse_utc(cutoff_at)
    out = []
    for r in rows:
        if r["event_type"] not in CELEBRATE_TYPES:
            continue
        t = parse_utc(r["observed_at"])
        if t is None or t < window_start or t > anchor:
            continue
        if cutoff_dt is not None and t <= cutoff_dt:
            continue
        out.append(r)
    return out


def decide(conn, subject_tag: str, selected: Candidate, group: list[Candidate],
           last_highlight: str | None) -> tuple[bool, int, dict]:
    """The accrual decision (recognition.md §3 steps 3–5).

    Evidence = stored celebrate player_events in the window (post-cutoff) plus
    the current group's candidates, deduped by key; score = sum; post iff the
    selected candidate is bypass or the sum clears the threshold. Returns
    (post, score, trace) — the trace is the §3 step-5 scoring record.
    """
    import json as _json

    anchor = parse_utc(selected.occurred_at) or datetime.now(timezone.utc)
    evidence: dict[str, tuple[str, int, str | None]] = {}
    for r in _evidence_rows(conn, subject_tag, anchor, last_highlight):
        try:
            payload = _json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (TypeError, ValueError):
            payload = {}
        score, _ = base_score(r["event_type"], payload)
        if score > 0:
            evidence[r["dedup_key"]] = (r["event_type"], score, r["observed_at"])
    for cand in group:
        score, _ = base_score(cand.event_type, cand.payload)
        if score > 0:
            t = parse_utc(cand.occurred_at)
            if t is not None and (t < anchor - ACCRUAL_WINDOW or t > anchor):
                continue
            evidence[cand.key] = (cand.event_type, score, cand.occurred_at)

    total = sum(score for _, score, _ in evidence.values())
    sel_score, sel_bypass = base_score(selected.event_type, selected.payload)
    post = sel_bypass or total >= HIGHLIGHT_THRESHOLD
    trace = {
        "recognition_policy": POLICY_ID,
        "recognition_decision": "bypass" if sel_bypass else ("accrued" if post else "accruing"),
        "recognition_score": total,
        "recognition_threshold": HIGHLIGHT_THRESHOLD,
        "recognition_selected_score": sel_score,
        "recognition_evidence": [
            {"dedup_key": k, "event_type": et, "score": s, "occurred_at": t}
            for k, (et, s, t) in sorted(evidence.items())
        ],
    }
    return post, total, trace


def cohort_waves(candidates: list[Candidate]) -> dict[str, list[Candidate]]:
    """Group wave-type candidates by (event_type, Chicago day); return the
    groups with ≥3 distinct members (recognition.md §3 cohort rule), keyed by
    the wave's ledger key cohort_wave:{event_type}:{chicago_day}."""
    groups: dict[str, list[Candidate]] = {}
    for c in candidates:
        if c.event_type not in COHORT_WAVE_TYPES:
            continue
        day = chicago_day(c.occurred_at)
        if not day:
            continue
        groups.setdefault(f"cohort_wave:{c.event_type}:{day}", []).append(c)
    return {
        key: members
        for key, members in groups.items()
        if len({m.subject_tag for m in members}) >= COHORT_MIN_MEMBERS
    }
