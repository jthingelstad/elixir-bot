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
        lag = {name: head - pos for name, pos in sorted(tracking.items())}

        # intents by lifecycle status (scan the log)
        intent_status = Counter()
        detections = 0
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
                elif agg == "Detection" and ev == "Detected":
                    detections += 1
                pos = n.id

        posted = intent_status.get("Fulfilled", 0)
        raised = intent_status.get("Raised", 0)
        return {
            "event_log_head": head,
            "follower_lag": lag,
            "max_follower_lag": max(lag.values()) if lag else 0,
            "detections_total": detections,
            "intents": {
                "raised": raised,
                "fulfilled_posted": posted,
                "dropped": intent_status.get("Dropped", 0),
                "pending": raised - posted - intent_status.get("Dropped", 0),
            },
        }
    finally:
        conn.close()


if __name__ == "__main__":
    print(json.dumps(health_snapshot(), indent=2, default=str))
