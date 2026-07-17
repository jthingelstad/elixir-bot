"""DM-outreach flow (Phase 1): propose leader-gated cards to collect missing
member profile info, and act on the leader's decision.

The whole feature is member-facing, so it is double-gated and OFF by default:
- ``ELIXIR_DM_OUTREACH=1`` — raise #actions cards at all (else fully dormant).
- ``ELIXIR_DM_OUTREACH_SEND=1`` — actually deliver a DM on approve (else dry-run:
  log what *would* be sent). Approving a card without this flag advances state but
  never messages a member — so the whole flow can be exercised and reviewed first.

A leader approves every card before any DM. The Discord + DB side effects are
injected so this module stays unit-testable. See storage/member_outreach.py for
the durable state and elixir-dm-outreach for the design.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from storage import member_outreach as mo

log = logging.getLogger("elixir.outreach")

ACTION_TYPE = "member_outreach"
# Mirror db.ACTION_DONE / db.ACTION_REJECTED without importing db into this pure
# flow module. A leader's ✅ resolves 'done'; ❌ resolves 'rejected'.
STATUS_APPROVE = "done"
STATUS_DECLINE = "rejected"
COOLDOWN_DAYS = 14  # don't re-ask a member within this window after an attempt
CARDS_PER_RUN = 3  # cap cards raised per proposal run so #actions never floods
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def outreach_enabled() -> bool:
    return os.getenv("ELIXIR_DM_OUTREACH", "0") == "1"


def send_enabled() -> bool:
    return os.getenv("ELIXIR_DM_OUTREACH_SEND", "0") == "1"


def _now(now: Optional[str]) -> str:
    return now or datetime.now(timezone.utc).strftime(_ISO)


def _cooldown_from(now: str) -> str:
    try:
        base = datetime.strptime(now[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        base = datetime.now(timezone.utc)
    return (base + timedelta(days=COOLDOWN_DAYS)).strftime(_ISO)


def compose_ask(member_name: Optional[str]) -> str:
    """The DM Elixir proposes to send. A leader can edit it on the card before
    approving, so this is the warm default ask — Discord renders markdown/emoji,
    so (unlike clan chat) it can breathe."""
    name = (member_name or "").strip() or "there"
    return (
        f"Hey {name}! It's Elixir from POAP KINGS \U0001f44b\n\n"
        "I'm filling in clanmate profiles and don't have your email yet. If you're "
        "happy to share it, just reply here with your email and I'll get you set — "
        "I'll send a quick 6-digit code to confirm it's really you.\n\n"
        'Totally optional — reply "no thanks" and I won\'t ask again.'
    )


def propose_cards(
    *,
    raise_card: Callable[[dict, str], Optional[dict]],
    limit: int = CARDS_PER_RUN,
    now: Optional[str] = None,
    conn=None,
) -> list[dict]:
    """Pick eligible targets and raise one leader card each. ``raise_card(target,
    copy)`` creates + posts the #actions card and returns the action dict (with
    ``action_id``), or None on failure. Returns the outreach rows moved to
    'proposed'. No-op (returns []) unless ``ELIXIR_DM_OUTREACH=1``."""
    if not outreach_enabled():
        return []
    now = _now(now)
    targets = mo.eligible_targets(limit=limit, now=now, conn=conn)
    proposed: list[dict] = []
    for target in targets:
        tag = target["player_tag"]
        copy = compose_ask(target.get("member_name"))
        try:
            action = raise_card(target, copy)
        except Exception:
            log.exception("outreach: failed to raise card for %s", tag)
            action = None
        if not action:
            continue
        row = mo.upsert_outreach(
            tag,
            status="proposed",
            discord_user_id=str(target.get("discord_user_id") or "") or None,
            leader_action_id=action.get("action_id"),
            last_asked_at=now,
            now=now,
            conn=conn,
        )
        proposed.append(row)
    if proposed:
        log.info("outreach: proposed %d card(s)", len(proposed))
    return proposed


def on_decision(
    action: dict,
    status: str,
    *,
    send_dm: Callable[[str, str], tuple[bool, str]],
    now: Optional[str] = None,
    conn=None,
) -> Optional[dict]:
    """Act on a leader's decision for a member_outreach card. On approve
    (``done``) send the DM via ``send_dm(discord_user_id, copy) -> (ok, detail)``
    and advance to 'awaiting_reply' (or 'failed'); on decline (``rejected``) mark
    'skipped'. Returns the updated outreach row, or None if not our card."""
    if not action or action.get("action_type") != ACTION_TYPE:
        return None
    tag = action.get("target_player_tag")
    if not tag:
        log.warning("outreach: member_outreach card has no target_player_tag")
        return None
    now = _now(now)

    if str(status) == STATUS_DECLINE:
        return mo.upsert_outreach(tag, status="skipped", now=now, conn=conn)
    if str(status) != STATUS_APPROVE:
        return None  # a non-terminal reaction change we don't act on

    # Approve path.
    existing = mo.get_outreach(tag, conn=conn) or {}
    discord_user_id = str(
        action.get("target_discord_user_id") or existing.get("discord_user_id") or ""
    ).strip()
    copy = action.get("copy_current_text") or action.get("copy_original_text") or ""
    if not discord_user_id or not copy.strip():
        log.warning(
            "outreach: missing discord_user_id/copy for %s; marking failed", tag
        )
        return mo.upsert_outreach(
            tag,
            status="failed",
            last_error="missing discord_user_id or copy",
            next_eligible_at=_cooldown_from(now),
            now=now,
            conn=conn,
        )
    try:
        ok, detail = send_dm(discord_user_id, copy)
    except Exception as exc:
        log.exception("outreach: send_dm raised for %s", tag)
        ok, detail = False, f"send raised: {exc}"
    if ok:
        return mo.upsert_outreach(
            tag,
            status="awaiting_reply",
            discord_user_id=discord_user_id,
            bump_attempts=True,
            last_asked_at=now,
            next_eligible_at=_cooldown_from(now),
            last_error=(None if detail == "sent" else detail),
            now=now,
            conn=conn,
        )
    return mo.upsert_outreach(
        tag,
        status="failed",
        last_error=detail,
        next_eligible_at=_cooldown_from(now),
        now=now,
        conn=conn,
    )
