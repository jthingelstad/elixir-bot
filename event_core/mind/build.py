"""Run the Mind Followers over a built event store and validate vs signal_log.

signal_log is thin — (signal_date, signal_type) only — so validation compares
detection presence/timing (Chicago dates) against legacy signal dates, not row
parity. The event log is the richer record.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from event_core import config
from event_core.timeutil import chicago_day_for_utc


def run_detectors(app, conn) -> dict:
    from event_core.mind.detectors import ALL_DETECTORS

    out = {}
    for cls in ALL_DETECTORS:
        d = cls(app, conn)
        d.reset()
        out[d.name] = d.run()
    return out


def detection_dates_by_type(app) -> dict:
    res = defaultdict(set)
    counts = defaultdict(int)
    pos = 0
    while True:
        notifs = app.recorder.select_notifications(start=pos + 1, limit=500)
        if not notifs:
            break
        for n in notifs:
            if n.topic.split(":")[-1].split(".")[0] == "Detection":
                ev = app.mapper.to_domain_event(n)
                res[ev.detection_type].add(chicago_day_for_utc(ev.occurred_at))
                counts[ev.detection_type] += 1
            pos = n.id
    return {"dates": res, "counts": dict(counts)}


def validate_vs_signals(app, legacy_path: str | None = None) -> dict:
    leg = sqlite3.connect(legacy_path or config.LEGACY_DB)
    info = detection_dates_by_type(app)
    dates = info["dates"]
    out = {"detection_counts": info["counts"], "by_type": {}}
    for dt in (
        "player_level_up",
        "best_trophies_peak",
        "battle_hot_streak",
        "card_level_milestone",
        "new_card_unlocked",
        "new_champion_unlocked",
        "badge_earned",
        "battle_trophy_push",
    ):
        legacy_dates = {
            r[0] for r in leg.execute(
                "SELECT signal_date FROM signal_log WHERE signal_type=?", (dt,)
            )
        }
        mine = dates.get(dt, set())
        out["by_type"][dt] = {
            "detection_dates": len(mine),
            "legacy_signal_dates": len(legacy_dates),
            "overlap": len(mine & legacy_dates),
            "mine_not_legacy": sorted(mine - legacy_dates)[:10],
            "legacy_not_mine": sorted(legacy_dates - mine)[:10],
        }
    leg.close()
    return out


def build_and_validate() -> dict:
    from event_core import build_foundation, db
    from event_core.application import ObservedWorld

    build_foundation.build()
    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    conn = db.connect(config.PROJECTIONS_DB)
    emitted = run_detectors(app, conn)
    validation = validate_vs_signals(app)
    conn.close()
    return {"detector_emitted": emitted, "validation": validation}


if __name__ == "__main__":
    print(json.dumps(build_and_validate(), indent=2, default=str))
