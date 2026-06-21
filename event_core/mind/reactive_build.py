"""Phase 3 reactive build — detectors -> communication intents -> agent tools.

Offline validation of the reactive trigger and the agent read side (on the branch;
no cutover). Builds the World, runs all Mind Followers, the communication policy,
the detections projection, then exercises the agent read-side tools.
"""
from __future__ import annotations

import json
from collections import defaultdict

from event_core import config


def _count_intents(app) -> dict:
    by = defaultdict(int)
    pos = 0
    while True:
        notifs = app.recorder.select_notifications(start=pos + 1, limit=500)
        if not notifs:
            break
        for n in notifs:
            if n.topic.split(":")[-1].split(".")[0] == "CommunicationIntent" and n.topic.endswith("Raised"):
                e = app.mapper.to_domain_event(n)
                by[e.scope] += 1
            pos = n.id
    return dict(by)


def build_and_validate() -> dict:
    from event_core import build_foundation, db
    from event_core.application import ObservedWorld
    from event_core.mind.communication import CommunicationPolicy
    from event_core.mind.detectors import ALL_DETECTORS
    from event_core.mind.leadership import InactivityRiskDetector, LeadershipGenerator
    from event_core.projections.detections import DetectionsProjection
    from event_core.read import tools

    build_foundation.build()
    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    conn = db.connect(config.PROJECTIONS_DB)

    # 1. detectors + leadership generators emit Detections / Recommendations
    for cls in [*ALL_DETECTORS, InactivityRiskDetector]:
        d = cls(app, conn)
        d.reset()
        d.run()
    gen = LeadershipGenerator(app, conn)
    gen.reset()
    gen.run()

    # 2. reactive policy turns those into communication intents
    policy = CommunicationPolicy(app, conn)
    policy.reset()
    intents_emitted = policy.run()

    # 3. detections projection so the agent can query
    proj = DetectionsProjection(app, conn)
    proj.reset()
    proj.run()

    # 4. exercise the agent read side on a real detection
    sample = conn.execute(
        "SELECT subject_tag, occurred_at FROM detections WHERE detection_type='battle_hot_streak' "
        "ORDER BY occurred_at DESC LIMIT 1"
    ).fetchone()
    tool_demo = None
    if sample:
        det = {"subject_tag": sample["subject_tag"], "occurred_at": sample["occurred_at"]}
        evidence = tools.resolve_evidence(conn, det)
        tool_demo = {
            "subject": sample["subject_tag"],
            "detections_for_player": len(tools.get_player_detections(conn, sample["subject_tag"])),
            "resolved_evidence_battles": len(evidence),
            "recent_battles": len(tools.get_player_battles(conn, sample["subject_tag"], limit=5)),
            "sample_opponent_tag": evidence[0].get("opponent_tag") if evidence else None,
        }

    total_detections = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
    conn.close()

    return {
        "intents_emitted": intents_emitted,
        "intents_by_scope": _count_intents(app),
        "detections_projected": total_detections,
        "agent_tool_demo": tool_demo,
    }


if __name__ == "__main__":
    print(json.dumps(build_and_validate(), indent=2, default=str))
