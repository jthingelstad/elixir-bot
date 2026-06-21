"""Discord intent consumer — the Mind→surface bridge.

Follows CommunicationIntent.Raised events and hands each to a pluggable `poster`
(which owns copy/channel/formatting — the presentation the Event Core omits).
Marks the intent fulfilled or dropped. Idempotent: an already-resolved intent is
skipped, so replays/re-runs never double-post.

The poster is injected so this is testable offline (fake poster) and wired to the
real Discord client at go-live.
"""
from __future__ import annotations

from event_core.mind.follower import FollowerRunner


class IntentConsumer(FollowerRunner):
    name = "consumer:discord"
    aggregate_name = "CommunicationIntent"

    def __init__(self, app, conn, poster):
        super().__init__(app, conn)
        self.poster = poster  # callable(intent) -> bool (True if posted)
        self.posted = 0
        self.dropped = 0

    def detect(self, event, notification) -> None:
        if type(event).__name__ != "Raised":
            return
        intent = self.app.repository.get(event.originator_id)
        if intent.status != "raised":  # already handled (idempotent)
            return
        try:
            ok = bool(self.poster(intent))
        except Exception:
            ok = False
        if ok:
            intent.fulfil()
            self.posted += 1
        else:
            intent.drop("poster_declined")
            self.dropped += 1
        self.app.save(intent)

    def run(self, batch: int = 500) -> int:
        super().run(batch)
        return self.posted
