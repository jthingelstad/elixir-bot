"""Phase 0 deliverable: what would the wake evaluator have done?

Reads `wake_observations` (telemetry DB) against `awareness_delivery_intents`
(clan DB) and answers the three questions the Phase 0 exit gate asks:

1. **Latency** — for each shadow wake, how much sooner would Elixir have spoken
   than the brain actually did? Matched by signal key, so it compares the same
   events, not just the same hour.
2. **Volume** — wakes/day by class and model, plus holds and why. A policy that
   fires ten times a day for badges is visible here before it costs anything.
3. **Cost** — projected spend at the measured volume, using the per-post prices
   the 2026-08-04 scoped-composer experiment measured.

Usage:
    uv run --locked python scripts/wake_shadow_report.py
    uv run --locked python scripts/wake_shadow_report.py --days 7 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Measured 2026-08-04 (docs/plans/agentic-loop.md): mean cost of one scoped,
# tool-using composition that delivered a post. Cache discounts in production
# will pull these down; treating them as ceilings keeps the projection honest.
COST_PER_WAKE = {"lightweight": 0.024, "chat": 0.121}
BRAIN_TICK_COST = 0.50


def _utc(stamp: str | None):
    if not stamp:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def load_observations(days: int) -> list[dict]:
    from storage import telemetry

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = (
        telemetry.connect()
        .execute(
            "SELECT * FROM wake_observations WHERE recorded_at >= ? ORDER BY recorded_at",
            (cutoff,),
        )
        .fetchall()
    )
    return [dict(r) for r in rows]


def load_intents(days: int) -> list[dict]:
    import db

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT intent_key, lane, created_at, fulfilled_at, covers_json, status "
            "FROM awareness_delivery_intents WHERE created_at >= ? ORDER BY created_at",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def match_latency(observations: list[dict], intents: list[dict]) -> list[dict]:
    """For each fired wake, find the intent that actually covered its signals
    and report the delta.

    Unmatched wakes are classified, not lumped together — the first pass of this
    report called 129 of 189 wakes "a post the brain never made", and most were
    neither:

    - ``before_intent_history`` — the delivery-intent table is pruned (it only
      reaches back to mid-July), so a wake older than the earliest intent CANNOT
      match. Counting those as misses is a measurement artifact.
    - ``soft`` — a badge or arena wake the brain deliberately stayed quiet on.
      That is the milestone discipline working, and it is the strongest argument
      for demoting a wake class, not evidence of a miss.
    - ``hard_post`` — a guaranteed-coverage event with no intent. THIS is a real
      finding and the only kind that should alarm anyone.
    """
    from engine.event_contracts import hard_post_event_types

    hard_types = hard_post_event_types()
    earliest_intent = min(
        (i.get("created_at") or "" for i in intents if i.get("created_at")), default=""
    )
    by_key: dict[str, dict] = {}
    for intent in intents:
        try:
            covers = json.loads(intent.get("covers_json") or "[]")
        except TypeError, ValueError:
            covers = []
        for key in covers:
            # Earliest intent covering a key is the one that broke the news.
            if key not in by_key or (intent.get("created_at") or "") < (
                by_key[key].get("created_at") or ""
            ):
                by_key[key] = intent

    out = []
    for obs in observations:
        if not obs.get("fired"):
            continue
        try:
            keys = json.loads(obs.get("signal_keys_json") or "[]")
        except TypeError, ValueError:
            keys = []
        matches = [by_key[k] for k in keys if k in by_key]
        wake_at = obs["recorded_at"]
        event_types = json.loads(obs.get("event_types_json") or "[]")
        if not matches:
            if earliest_intent and wake_at < earliest_intent:
                kind = "before_intent_history"
            elif hard_types.intersection(event_types):
                kind = "hard_post"
            else:
                kind = "soft"
            out.append(
                {
                    "wake_at": wake_at,
                    "wake_class": obs["wake_class"],
                    "event_types": event_types,
                    "matched": False,
                    "unmatched_kind": kind,
                    "saved_minutes": None,
                }
            )
            continue
        wake_at = _utc(obs["recorded_at"])
        posted = min(_utc(m.get("created_at")) for m in matches if _utc(m.get("created_at")))
        saved = (posted - wake_at).total_seconds() / 60.0 if posted and wake_at else None
        out.append(
            {
                "wake_at": obs["recorded_at"],
                "posted_at": posted.strftime("%Y-%m-%dT%H:%M:%SZ") if posted else None,
                "wake_class": obs["wake_class"],
                "wake_model": obs["wake_model"],
                "event_types": json.loads(obs.get("event_types_json") or "[]"),
                "matched": True,
                "saved_minutes": round(saved, 1) if saved is not None else None,
            }
        )
    return out


def simulate_observations(days: int) -> list[dict]:
    """Replay historical events through the wake policy, in the shape the live
    evaluator would have produced.

    This exists so Phase 0 can answer the exit-gate questions with a month of
    real history on day one, instead of only after a week of live shadow. The
    fidelity limits are worth stating: it assumes every event was seen at the
    next 10-minute tick boundary (true — that IS the tick cadence), and it does
    not model min-lead suppression or the daily budget (both of which only ever
    REDUCE wakes, so the simulated volume is an upper bound).
    """
    import db
    from engine.event_contracts import wake_policy
    from runtime.awareness.wake import BATCH_MAX_EVENTS, batch_window_minutes

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    subject = {
        "player_events": "player_tag",
        "clan_events": "subject_tag",
        "war_events": "NULL",
        "game_events": "subject_tag",
    }
    events = []
    conn = db.get_connection()
    try:
        for table, column in subject.items():
            rows = conn.execute(
                f"SELECT event_type, {column} AS subject_tag, observed_at, dedup_key "
                f"FROM {table} WHERE observed_at >= ? ORDER BY observed_at",
                (cutoff,),
            ).fetchall()
            events.extend(dict(r) for r in rows)
    finally:
        conn.close()

    for event in events:
        event["wake_class"], event["wake_model"] = wake_policy(event["event_type"])
    events = [e for e in events if e["wake_class"] in ("immediate", "batch")]
    events.sort(key=lambda e: e["observed_at"] or "")

    def tick_boundary(stamp: str) -> datetime:
        seen = _utc(stamp) or datetime.now(timezone.utc)
        return (seen + timedelta(minutes=10 - (seen.minute % 10))).replace(second=0, microsecond=0)

    # Immediate: everything of one model tier landing on the same tick rides one
    # wake — the grouping the live evaluator does.
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for event in (e for e in events if e["wake_class"] == "immediate"):
        grouped[(tick_boundary(event["observed_at"]), "immediate", event["wake_model"])].append(
            event
        )

    # Batch: coalesce per model tier until the window expires or the count fires
    # it early.
    window = timedelta(minutes=batch_window_minutes())
    open_batches: dict[str, list[dict]] = defaultdict(list)

    def close(model: str):
        batch = open_batches.pop(model, [])
        if not batch:
            return
        opened = _utc(batch[0]["observed_at"])
        fire_at = (
            tick_boundary(batch[-1]["observed_at"])
            if len(batch) >= BATCH_MAX_EVENTS
            else (opened + window if opened else None)
        )
        grouped[(fire_at, "batch", model)].extend(batch)

    for event in (e for e in events if e["wake_class"] == "batch"):
        model = event["wake_model"]
        batch = open_batches[model]
        opened = _utc(batch[0]["observed_at"]) if batch else None
        now = _utc(event["observed_at"])
        if batch and opened and now and now - opened >= window:
            close(model)
        open_batches[model].append(event)
        if len(open_batches[model]) >= BATCH_MAX_EVENTS:
            close(model)
    for model in list(open_batches):
        close(model)

    out = []
    for (fire_at, wake_class, model), group in sorted(
        grouped.items(), key=lambda kv: kv[0][0] or datetime.min.replace(tzinfo=timezone.utc)
    ):
        if fire_at is None:
            continue
        out.append(
            {
                "recorded_at": fire_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "mode": "simulated",
                "wake_class": wake_class,
                "wake_model": model,
                "fired": 1,
                "event_count": len(group),
                "signal_keys_json": json.dumps([e["dedup_key"] for e in group if e["dedup_key"]]),
                "event_types_json": json.dumps(sorted({e["event_type"] for e in group})),
                "reason": "",
            }
        )
    return out


def build_report(days: int, *, simulate: bool = False) -> dict:
    observations = simulate_observations(days) if simulate else load_observations(days)
    intents = load_intents(days)
    latency = match_latency(observations, intents)

    fired = [o for o in observations if o.get("fired")]
    held = [o for o in observations if not o.get("fired")]
    day_span = max(1.0, days)

    by_class = Counter(f"{o['wake_class']}/{o['wake_model']}" for o in fired)
    hold_reasons = Counter(
        (o.get("reason") or "").split("—")[0].split("(")[0].strip() for o in held
    )
    types = Counter()
    for o in fired:
        for t in json.loads(o.get("event_types_json") or "[]"):
            types[t] += 1

    projected = sum(COST_PER_WAKE.get(o["wake_model"], 0.05) for o in fired) / day_span
    saved = [entry["saved_minutes"] for entry in latency if entry.get("saved_minutes") is not None]
    unmatched = [entry for entry in latency if not entry["matched"]]
    unmatched_kinds = Counter(entry.get("unmatched_kind") for entry in unmatched)
    real_misses = [e for e in unmatched if e.get("unmatched_kind") == "hard_post"]
    # A wake class that fires often and is almost never posted is the clearest
    # demote-to-digest candidate the shadow run can produce.
    soft_noise = Counter()
    for entry in unmatched:
        if entry.get("unmatched_kind") == "soft":
            for event_type in entry["event_types"]:
                soft_noise[event_type] += 1

    per_day: dict[str, int] = defaultdict(int)
    for o in fired:
        per_day[o["recorded_at"][:10]] += 1

    return {
        "window_days": days,
        "source": "simulated from event history" if simulate else "live shadow observations",
        "wakes_fired": len(fired),
        "wakes_per_day": round(len(fired) / day_span, 1),
        "wakes_by_class": dict(by_class),
        "event_types": dict(types.most_common()),
        "holds": len(held),
        "hold_reasons": dict(hold_reasons),
        "per_day": dict(sorted(per_day.items())),
        "latency": {
            "matched": len(saved),
            "unmatched": len(unmatched),
            "median_minutes_saved": round(sorted(saved)[len(saved) // 2], 1) if saved else None,
            "max_minutes_saved": round(max(saved), 1) if saved else None,
            "mean_minutes_saved": round(sum(saved) / len(saved), 1) if saved else None,
        },
        "unmatched_kinds": dict(unmatched_kinds),
        "hard_post_misses": real_misses[:10],
        "soft_never_posted": dict(soft_noise.most_common()),
        "projected_wake_cost_per_day": round(projected, 2),
        "current_brain_cost_per_day": round(BRAIN_TICK_COST * 4, 2),
        "detail": latency,
    }


def render(report: dict) -> str:
    lines = [
        f"Wake shadow report — last {report['window_days']} day(s)",
        f"source: {report.get('source', 'live shadow observations')}",
        "=" * 52,
        "",
        f"Wakes that would have fired : {report['wakes_fired']} ({report['wakes_per_day']}/day)",
        f"Holds (fired=0)             : {report['holds']}",
        "",
        "By class/model:",
    ]
    for key, count in sorted(report["wakes_by_class"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {key:<28} {count}")
    if report["event_types"]:
        lines += ["", "Event types waking:"]
        for key, count in report["event_types"].items():
            lines.append(f"  {key:<28} {count}")
    if report["hold_reasons"]:
        lines += ["", "Hold reasons:"]
        for key, count in report["hold_reasons"].items():
            lines.append(f"  {key[:44]:<46} {count}")

    lat = report["latency"]
    lines += [
        "",
        "Latency vs what the brain actually did:",
        f"  matched wakes             : {lat['matched']}",
        f"  median minutes saved      : {lat['median_minutes_saved']}",
        f"  mean minutes saved        : {lat['mean_minutes_saved']}",
        f"  max minutes saved         : {lat['max_minutes_saved']}",
        f"  unmatched                 : {lat['unmatched']} {report['unmatched_kinds']}",
    ]
    if report["soft_never_posted"]:
        lines += [
            "",
            "Soft events that woke but were never posted (demote-to-digest candidates):",
        ]
        for key, count in report["soft_never_posted"].items():
            lines.append(f"  {key:<28} {count}")
    if report["hard_post_misses"]:
        lines += ["", "HARD-POST MISSES (a guaranteed post with no intent — investigate):"]
        for entry in report["hard_post_misses"]:
            lines.append(f"  {entry['wake_at']}  {', '.join(entry['event_types'])}")

    lines += [
        "",
        "Cost projection (experiment-measured per-post ceilings):",
        f"  wakes                     : ${report['projected_wake_cost_per_day']}/day",
        f"  today's scheduled brain   : ${report['current_brain_cost_per_day']}/day",
        "",
        "Phase 0 posts nothing. These are measurements only.",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="replay historical events through the policy instead of reading live shadow rows",
    )
    args = parser.parse_args()

    report = build_report(max(1, args.days), simulate=args.simulate)
    if args.json:
        print(json.dumps(report, indent=1, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
