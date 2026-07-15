"""Deliver the brain's post plan to Discord.

``deliver_posts`` takes ``plan["posts"]`` and sends each one to its channel,
records it for the next tick's ``channel_memory`` (dedup), and — for posts the
brain flags as clan-chat-worthy — escalates an in-game relay HITL card. The
awareness loop always renders its plan to the #thinking diagnostic as well; that
is the observability record, separate from this member-facing delivery.

Design contract (locked with Jamie): **fail-hard, no fallback.** Any send
failure or an uncovered hard-post floor fails the whole tick; the loop records
it as failed, the cursor does NOT advance (store.last_tick_at excludes failed
ticks), and the same signals re-surface next loop for a genuine retry. There is
no template fallback anywhere — a post either lands as the brain wrote it or it
waits for the next loop.

The Discord/DB specifics are injected as callables so this module stays pure and
unit-testable:

- ``post_fn(channel_id: int, copy: str) -> int | None`` — send, return message id
- ``record_fn(*, lane, content, covers, message_id, loop_number) -> None`` — dedup memory
- ``relay_fn(post: dict, channel_name: str) -> None`` — optional clan-chat escalation
"""

from __future__ import annotations

import logging

from capabilities.game_truth import awareness_post_facts
from engine.recognition import compose as engine_compose
from runtime.awareness.policy import validate_plan, validate_repair

log = logging.getLogger("elixir")

# The only two channels the brain posts to. Anything else in a plan is a bug in
# the prompt/output and fails the tick rather than leaking to a wrong channel.
POSTABLE_CHANNELS = ("announcements", "elixir")

# A new member is ALWAYS welcomed in-game, not just on Discord (Jamie, 2026-07-12):
# whichever post covers the join signal force-raises a clan-chat welcome relay card
# even if the brain didn't set relay_to_clan_chat. The prompt also mandates the
# brain author the `clan_chat` welcome (with account detail) so the copy stays
# grounded; this backstop guarantees the card exists regardless.
JOIN_EVENT_TYPES = frozenset({"member_joined"})


def _post_content(post: dict) -> str:
    """Join a post's content into a single Discord-ready string. ``content`` may
    be a string or a list of chunks (the schema allows either)."""
    content = post.get("content")
    if isinstance(content, list):
        return "\n\n".join(str(c) for c in content if c is not None).strip()
    return str(content or "").strip()


def deliver_posts(
    read: dict,
    plan: dict,
    *,
    post_fn,
    record_fn,
    relay_fn=None,
    repair_fn=None,
    intent_store=None,
    loop_number: int | None = None,
) -> dict:
    """Deliver every post in ``plan``. Returns a result dict:
    ``{"delivered": int, "failed": bool, "reason": str|None, "uncovered_hard": [...]}``.

    Fails the tick (``failed=True``) on: an unknown/absent channel, a send that
    raises or returns no message id, or a hard-post-floor signal left uncovered.
    A post that lands is recorded immediately, so a mid-plan failure still leaves
    the already-sent posts in channel_memory (the brain won't repeat them)."""
    violations = validate_plan(read, plan)
    if violations and repair_fn is not None:
        log.warning(
            "awareness copy policy rejected plan; attempting one repair: %s", violations
        )
        original_plan = plan
        try:
            repaired = repair_fn(read, plan, violations)
        except Exception:
            log.exception("awareness copy policy repair raised")
            repaired = None
        if isinstance(repaired, dict):
            violations = validate_repair(original_plan, repaired) + validate_plan(
                read, repaired
            )
            if not violations:
                # Keep persistence/diagnostics aligned with what actually
                # reaches Discord instead of retaining the rejected draft.
                original_plan.clear()
                original_plan.update(repaired)
                plan = original_plan
    if violations:
        reason = "copy policy violation after repair: " + ", ".join(violations)
        log.error("awareness deliver: %s — failing tick before send", reason)
        return {"delivered": 0, "failed": True, "reason": reason, "uncovered_hard": []}

    posts = plan.get("posts") or []
    lanes = engine_compose.channels()
    covered: set[str] = set()
    delivery_posts = []

    # Validate the entire hard side-effect plan before the first Discord send.
    # Previously an uncovered mandatory signal was discovered only after one
    # or more posts had already escaped, guaranteeing a partial retry problem.
    for post in posts:
        channel = post.get("channel")
        cfg = lanes.get(channel) if channel in POSTABLE_CHANNELS else None
        if cfg is None:
            reason = f"unroutable channel {channel!r}"
            log.error("awareness deliver: %s — failing tick before send", reason)
            return {
                "delivered": 0,
                "failed": True,
                "reason": reason,
                "uncovered_hard": [],
            }
        copy = _post_content(post)
        if not copy:
            reason = f"empty content for channel {channel!r}"
            log.error("awareness deliver: %s — failing tick before send", reason)
            return {
                "delivered": 0,
                "failed": True,
                "reason": reason,
                "uncovered_hard": [],
            }
        prepared_post = dict(post)
        prepared_post["_delivery_content"] = copy
        delivery_posts.append(prepared_post)
        covered.update(post.get("covers_signal_keys") or [])

    mandatory = {
        signal.get("signal_key")
        for signal in (read.get("hard_post_signals") or [])
        if isinstance(signal, dict) and signal.get("signal_key")
    }

    if intent_store is not None:
        try:
            # Load durable work before inserting this draft. Coverage from a
            # previously fulfilled sibling intent still counts when another
            # post in the same plan is retrying and the event cursor has not
            # advanced yet.
            existing_work = intent_store.prepare_delivery_intents(
                [], required_signal_keys=mandatory
            )
        except Exception as exc:
            log.exception("awareness deliver: could not load delivery outbox")
            return {
                "delivered": 0,
                "failed": True,
                "reason": f"delivery intent load failed: {exc}",
                "uncovered_hard": [],
            }
        ambiguous = [
            item["intent_key"] for item in existing_work if item["status"] == "sending"
        ]
        if ambiguous:
            reason = f"ambiguous prior delivery intents still within lease: {ambiguous}"
            log.error("awareness deliver: %s", reason)
            return {
                "delivered": 0,
                "failed": True,
                "reason": reason,
                "uncovered_hard": [],
            }
        existing_covered = {
            signal_key
            for item in existing_work
            for signal_key in (item["post"].get("covers_signal_keys") or [])
        }
        uncovered = sorted(mandatory - covered - existing_covered)
        if uncovered:
            log.error(
                "awareness deliver: hard-post floor uncovered %s — "
                "rejecting plan before persistence",
                uncovered,
            )
            return {
                "delivered": 0,
                "failed": True,
                "reason": f"uncovered hard-post signals: {uncovered}",
                "uncovered_hard": uncovered,
            }
        try:
            work = intent_store.prepare_delivery_intents(
                delivery_posts, required_signal_keys=mandatory
            )
        except Exception as exc:
            log.exception("awareness deliver: could not persist delivery plan")
            return {
                "delivered": 0,
                "failed": True,
                "reason": f"delivery intent prepare failed: {exc}",
                "uncovered_hard": [],
            }
        ambiguous = [item["intent_key"] for item in work if item["status"] == "sending"]
        if ambiguous:
            reason = f"ambiguous prior delivery intents still within lease: {ambiguous}"
            log.error("awareness deliver: %s", reason)
            return {
                "delivered": 0,
                "failed": True,
                "reason": reason,
                "uncovered_hard": [],
            }
    else:
        work = [
            {
                "intent_key": None,
                "status": "pending",
                "post": post,
                "from_current_plan": True,
            }
            for post in delivery_posts
        ]

    # The outbox, not a fresh LLM re-plan, owns already-persisted work. Include
    # pending posts from a prior partial attempt even if this turn chose silence
    # or regrouped its signals, and align diagnostics with the exact stored copy.
    plan["posts"] = [
        {
            key: value
            for key, value in item["post"].items()
            if key != "_delivery_content"
        }
        for item in work
    ]
    covered = {
        signal_key
        for item in work
        for signal_key in (item["post"].get("covers_signal_keys") or [])
    }
    uncovered = sorted(mandatory - covered)
    if uncovered:
        log.error(
            "awareness deliver: hard-post floor uncovered %s — failing before send",
            uncovered,
        )
        return {
            "delivered": 0,
            "failed": True,
            "reason": f"uncovered hard-post signals: {uncovered}",
            "uncovered_hard": uncovered,
        }

    delivered = 0
    replayed = 0

    # Signal keys of any member-join hard-post this tick. A post covering one of
    # these always relays an in-game welcome (see JOIN_EVENT_TYPES).
    join_signal_keys = {
        s.get("signal_key")
        for s in (read.get("hard_post_signals") or [])
        if isinstance(s, dict)
        and s.get("event_type") in JOIN_EVENT_TYPES
        and s.get("signal_key")
    }

    for item in work:
        if item["status"] == "fulfilled":
            continue
        post = item["post"]
        if not item["from_current_plan"]:
            replayed += 1
        intent_key = item["intent_key"]
        channel = post.get("channel")
        cfg = lanes[channel]
        copy = post["_delivery_content"]

        if intent_store is not None:
            try:
                claimed = intent_store.mark_delivery_sending(intent_key)
                if claimed is False:
                    raise RuntimeError("intent was no longer pending")
            except Exception as exc:
                log.exception("awareness deliver: could not claim delivery intent")
                return {
                    "delivered": delivered,
                    "failed": True,
                    "reason": f"delivery intent claim failed: {exc}",
                    "uncovered_hard": [],
                }

        try:
            message_id = post_fn(cfg["channel_id"], copy)
        except Exception as exc:
            log.exception(
                "awareness deliver: send to #%s failed; catch up next loop",
                cfg.get("channel_name") or channel,
            )
            if intent_store is not None:
                try:
                    intent_store.mark_delivery_pending(intent_key, str(exc))
                except Exception:
                    log.exception(
                        "awareness deliver: could not return failed intent to pending"
                    )
            return {
                "delivered": delivered,
                "failed": True,
                "reason": f"send failed: {exc}",
                "uncovered_hard": [],
            }
        if message_id is None:
            reason = f"send to #{cfg.get('channel_name') or channel} returned no id"
            log.error("awareness deliver: %s — failing tick", reason)
            if intent_store is not None:
                try:
                    intent_store.mark_delivery_pending(intent_key, reason)
                except Exception:
                    log.exception(
                        "awareness deliver: could not return empty-send intent to pending"
                    )
            return {
                "delivered": delivered,
                "failed": True,
                "reason": reason,
                "uncovered_hard": [],
            }

        covers = post.get("covers_signal_keys") or []
        try:
            record_args = dict(
                lane=channel,
                content=copy,
                covers=covers,
                message_id=message_id,
                loop_number=loop_number,
                post=post,
                evidence=awareness_post_facts(read, post),
            )
            if intent_key is not None:
                record_args["intent_key"] = intent_key
            record_fn(**record_args)
        except Exception:
            # Recording is dedup-memory only; a failure here must not fail an
            # already-delivered post (it's landed in Discord).
            log.exception("awareness deliver: record post failed (non-fatal)")
        if intent_store is not None:
            try:
                intent_store.mark_delivery_fulfilled(
                    intent_key, message_id, loop_number=loop_number
                )
            except Exception as exc:
                log.exception("awareness deliver: could not fulfill delivery intent")
                return {
                    "delivered": delivered + 1,
                    "failed": True,
                    "reason": f"delivery intent finalize failed: {exc}",
                    "uncovered_hard": [],
                }
        delivered += 1

        # Clan-chat escalation rides a delivered post and never fails it — the
        # Discord post already landed; a relay hiccup is logged and dropped.
        # A new-member join ALWAYS relays a welcome, even if the brain didn't flag
        # it — the clan welcomes every newcomer in-game.
        should_relay = bool(post.get("relay_to_clan_chat"))
        if not should_relay and join_signal_keys.intersection(covers):
            should_relay = True
            log.info(
                "awareness deliver: forcing in-game welcome relay for join %s",
                sorted(join_signal_keys.intersection(covers)),
            )
        if relay_fn is not None and should_relay:
            try:
                relay_fn(post, cfg.get("channel_name") or channel)
            except Exception:
                log.exception("awareness deliver: clan-chat relay failed (non-fatal)")

    result = {
        "delivered": delivered,
        "failed": False,
        "reason": None,
        "uncovered_hard": [],
    }
    if intent_store is not None:
        result["replayed"] = replayed
    return result


__all__ = ["deliver_posts", "POSTABLE_CHANNELS"]
