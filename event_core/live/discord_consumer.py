"""Discord intent consumer — the Mind→surface bridge (at-least-once delivery).

Follows CommunicationIntent.Raised events and hands each to a pluggable `poster`
(which owns copy/channel/formatting — the presentation the Event Core omits).

Delivery semantics (the important part):
- The intent is marked `fulfilled` ONLY after the poster confirms a successful send
  (returns True). So the poster MUST actually post before returning True — do not
  wrap a "queue for later" poster here, or a later send failure loses the post.
- On failure (poster returns False OR raises — both treated as transient), the
  consumer STOPS without advancing past the failed intent and leaves it `raised`,
  so the next tick retries it. This is at-least-once with head-of-line blocking:
  a persistently-failing intent will block later ones (visible in monitoring as
  rising `pending` / stalled `posted`) rather than being silently dropped.
- Idempotent: an already-resolved intent is skipped; replays never double-post.

`drop()` is reserved for an explicit, deliberate decline (not wired by default).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from event_core.mind.follower import FollowerRunner

log = logging.getLogger("elixir.event_core")


class IntentConsumer(FollowerRunner):
    name = "consumer:discord"
    aggregate_name = "CommunicationIntent"

    # Don't post a backlog older than this — drop it as stale instead. This bounds
    # the flood when the consumer resumes after a long outage (post-cutover restarts
    # no longer fast-forward; see service.catch_up), while still posting recent work.
    MAX_INTENT_AGE_HOURS = 6

    def __init__(self, app, conn, poster):
        super().__init__(app, conn)
        self.poster = poster  # callable(intent) -> bool; MUST have actually posted
        self.posted = 0
        self.failed = 0
        self.dropped = 0

    def _is_stale(self, intent) -> bool:
        created = getattr(intent, "created_on", None)
        if created is None:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        return age_h > self.MAX_INTENT_AGE_HOURS

    def _deliver(self, intent) -> bool:
        """Post one intent. Returns True only on a confirmed send."""
        try:
            ok = bool(self.poster(intent))
        except Exception:
            log.exception("%s: poster raised for intent %s", self.name, intent.dedup_key)
            ok = False
        if ok:
            intent.fulfil()
            self.app.save(intent)
            self.posted += 1
        else:
            self.failed += 1
        return ok

    def run(self, batch: int = 500) -> int:
        # Own loop (not the base) so a delivery failure stops WITHOUT advancing the
        # tracked position past the failed intent — the next tick retries it.
        stop = self.app.recorder.max_notification_id()
        pos = self.last_position()
        while pos < stop:
            notifs = self.app.recorder.select_notifications(start=pos + 1, limit=batch, stop=stop)
            if not notifs:
                break
            for n in notifs:
                if self._aggregate_of(n) == self.aggregate_name:
                    try:
                        event = self.app.mapper.to_domain_event(n)
                    except Exception:
                        log.exception("%s: undecodable notification %s", self.name, n.id)
                        pos = n.id
                        continue
                    if type(event).__name__ == "Raised":
                        intent = self.app.repository.get(event.originator_id)
                        if intent.status == "raised":
                            if self._is_stale(intent):
                                # Too old to post (long-outage backlog) — drop it
                                # auditably and advance past it.
                                intent.drop("stale_backlog")
                                self.app.save(intent)
                                self.dropped += 1
                                log.info(
                                    "%s: dropped stale intent %s (raised %s)",
                                    self.name, intent.dedup_key, intent.created_on,
                                )
                            elif not self._deliver(intent):
                                self._save_position(pos)  # commit progress BEFORE this one
                                return self.posted  # stop; retry this + the rest next tick
                pos = n.id
            self._save_position(pos)
        return self.posted
