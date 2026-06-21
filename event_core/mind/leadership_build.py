"""Run the leadership generators and validate vs legacy cases/recommendations.

Structural validation (lifecycle + evidence) plus a sanity comparison of flagged
players against legacy inactivity_review / kick targets — not row-for-row parity
(the legacy recompute-first policy is replaced, §6).
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from event_core import config


def _collect_mind(app) -> dict:
    """Scan the log for Recommendation/DecisionCase aggregates -> by type/player."""
    recs = defaultdict(set)  # rec_type -> {player_tag}
    cases = defaultdict(set)  # case_type -> {player_tag}
    pos = 0
    while True:
        notifs = app.recorder.select_notifications(start=pos + 1, limit=500)
        if not notifs:
            break
        for n in notifs:
            agg = n.topic.split(":")[-1].split(".")[0]
            ev = n.topic.rsplit(".", 1)[-1]
            if agg == "Recommendation" and ev == "CandidateDetected":
                e = app.mapper.to_domain_event(n)
                recs[e.recommendation_type].add(e.player_tag)
            elif agg == "DecisionCase" and ev == "Opened":
                e = app.mapper.to_domain_event(n)
                cases[e.case_type].add(e.player_tag)
            pos = n.id
    return {"recs": recs, "cases": cases}


def _norm(tag: str) -> str:
    return (tag or "").lstrip("#").upper()


def build_and_validate() -> dict:
    from event_core import build_foundation, db
    from event_core.application import ObservedWorld
    from event_core.mind.leadership import InactivityRiskDetector, LeadershipGenerator

    build_foundation.build()
    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    conn = db.connect(config.PROJECTIONS_DB)

    det = InactivityRiskDetector(app, conn)
    det.reset()
    n_det = det.run()
    gen = LeadershipGenerator(app, conn)
    gen.reset()
    n_gen = gen.run()
    conn.close()

    mind = _collect_mind(app)
    my_inactivity = {_norm(t) for t in mind["cases"].get("inactivity_review", set())}
    my_kick = {_norm(t) for t in mind["recs"].get("kick", set())}

    leg = sqlite3.connect(config.LEGACY_DB)
    legacy_inactivity = {
        _norm(r[0]) for r in leg.execute(
            "SELECT target_player_tag FROM decision_cases WHERE case_type='inactivity_review' AND target_player_tag IS NOT NULL"
        )
    }
    legacy_kick = {
        _norm(r[0]) for r in leg.execute(
            "SELECT target_player_tag FROM leader_action_recommendations "
            "WHERE action_type='kick_recommendation' AND target_player_tag IS NOT NULL"
        )
    }
    leg.close()

    return {
        "detections_emitted": n_det,
        "leadership_emitted": n_gen,
        "recommendations": {k: len(v) for k, v in mind["recs"].items()},
        "cases": {k: len(v) for k, v in mind["cases"].items()},
        "inactivity_vs_legacy": {
            "mine": len(my_inactivity),
            "legacy": len(legacy_inactivity),
            "overlap": len(my_inactivity & legacy_inactivity),
            "legacy_only": sorted(legacy_inactivity - my_inactivity),
        },
        "kick_vs_legacy": {
            "mine": len(my_kick),
            "legacy": len(legacy_kick),
            "overlap": len(my_kick & legacy_kick),
        },
    }


if __name__ == "__main__":
    print(json.dumps(build_and_validate(), indent=2, default=str))
