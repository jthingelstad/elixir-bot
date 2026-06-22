"""v5 health snapshot — for active post-cutover monitoring.

Reports event-store head, follower/projection lag, detection + intent counts (by
status), and recent posted intents. Works pre-flip (validates the build) and
post-flip (monitors steady state). Pair with: service up (`launchctl list | grep
elixir`), fresh `elixir-v5.log`, and the live channels.
"""
from __future__ import annotations

import json
from collections import Counter

from event_core import config

# Detectors that scan current/external state instead of following the event log, so
# head-minus-tracked-position is NOT lag for them. (war_update can carry a stale row
# from when it was event-driven; the others never track a position.)
SCAN_STYLE_DETECTORS = {
    "detector:war_update",
    "detector:cake_day",
    "detector:weekly_donation_leader",
    "detector:battle_trophy_push",
}


def health_snapshot() -> dict:
    from event_core import db
    from event_core.application import ObservedWorld

    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    conn = db.connect(config.PROJECTIONS_DB)
    try:
        head = app.recorder.max_notification_id() or 0
        tracking = {
            r["projection_name"]: r["last_global_position"]
            for r in conn.execute(
                "SELECT projection_name, last_global_position FROM projection_tracking"
            )
        }
        consumer_pos = tracking.get("consumer:discord", 0)

        # head-minus-position is real lag ONLY for log-following followers. Scan-style
        # detectors read current/external state and don't track a meaningful log
        # position (a stale row would otherwise read as huge false lag); the cutover
        # marker is bookkeeping, not a follower.
        non_lag = SCAN_STYLE_DETECTORS | {"cutover:v5"}
        lag = {n: head - p for n, p in sorted(tracking.items()) if n not in non_lag}
        scan_detectors = sorted(n for n in tracking if n in SCAN_STYLE_DETECTORS)

        # intents by lifecycle status (scan the log); track how many Raised intents
        # sit AFTER the consumer cursor — that is the real deliverable backlog.
        intent_status = Counter()
        detections = 0
        deliverable_pending = 0
        pos = 0
        while True:
            notifs = app.recorder.select_notifications(start=pos + 1, limit=1000)
            if not notifs:
                break
            for n in notifs:
                agg = n.topic.split(":")[-1].split(".")[0]
                ev = n.topic.rsplit(".", 1)[-1]
                if agg == "CommunicationIntent":
                    intent_status[ev] += 1  # Raised / Fulfilled / Dropped
                    if ev == "Raised" and n.id > consumer_pos:
                        deliverable_pending += 1
                elif agg == "Detection" and ev == "Detected":
                    detections += 1
                pos = n.id

        posted = intent_status.get("Fulfilled", 0)
        raised = intent_status.get("Raised", 0)
        dropped = intent_status.get("Dropped", 0)
        # Anything Raised but still un-fulfilled/un-dropped AND at/below the consumer
        # cursor was drained historically (the one-time go-live fast-forward), not
        # deliverable. This is what made the old `raised - posted - dropped` pending
        # count lie after fast_forward().
        drained_historical = max(0, raised - posted - dropped - deliverable_pending)
        return {
            "event_log_head": head,
            "consumer_position": consumer_pos,
            "follower_lag": lag,
            "max_follower_lag": max(lag.values()) if lag else 0,
            "scan_detectors": scan_detectors,
            "detections_total": detections,
            "intents": {
                "raised": raised,
                "fulfilled_posted": posted,
                "dropped": dropped,
                "deliverable_pending": deliverable_pending,
                "drained_historical": drained_historical,
            },
        }
    finally:
        conn.close()


if __name__ == "__main__":
    print(json.dumps(health_snapshot(), indent=2, default=str))
