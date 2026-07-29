"""DM-outreach flow (Phase 1): propose leader-gated cards to collect missing
member profile info, and act on the leader's decision.

A leader approves every card before any DM. The Discord + DB side effects are
injected so this module stays unit-testable. See storage/member_outreach.py for
the durable state and elixir-dm-outreach for the design.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import db
from storage import member_outreach as mo

# A member DMing us mid-flow either declines, sends an email, or sends the code.
_OPT_OUT_RE = re.compile(
    r"\b(no\s*thanks|no\s*thank\s*you|not\s*interested|unsubscribe|stop|"
    r"leave\s*me|don'?t\s*ask|do\s*not\s*ask|no)\b",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"\b(\d{6})\b")
_ACTIVE_DM_STATUSES = ("awaiting_reply", "verifying")

log = logging.getLogger("elixir.outreach")

ACTION_TYPE = "member_outreach"
# Mirror db.ACTION_DONE / db.ACTION_REJECTED without importing db into this pure
# flow module. A leader's ✅ resolves 'done'; ❌ resolves 'rejected'.
STATUS_APPROVE = "done"
STATUS_DECLINE = "rejected"
COOLDOWN_DAYS = 14  # don't re-ask a member within this window after an attempt
CARDS_PER_RUN = 3  # cap cards raised per proposal run so #actions never floods
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _now(now: Optional[str]) -> str:
    return now or datetime.now(timezone.utc).strftime(_ISO)


def _cooldown_from(now: str) -> str:
    try:
        base = datetime.strptime(now[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except TypeError, ValueError:
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
    compose: Optional[Callable[[dict], str]] = None,
    limit: int = CARDS_PER_RUN,
    now: Optional[str] = None,
    conn=None,
) -> list[dict]:
    """Pick eligible targets and raise one leader card each. ``compose(target) ->
    str`` writes the ask DM (Elixir's agent voice); it falls back to the
    deterministic ``compose_ask`` template if omitted, empty, or it raises.
    ``raise_card(target, copy)`` creates + posts the #actions card and returns the
    action dict (with ``action_id``), or None on failure. Returns the outreach rows
    moved to 'proposed'."""
    now = _now(now)
    targets = mo.eligible_targets(limit=limit, now=now, conn=conn)
    proposed: list[dict] = []
    for target in targets:
        tag = target["player_tag"]
        copy = ""
        if compose is not None:
            try:
                copy = (compose(target) or "").strip()
            except Exception:
                log.exception("outreach: compose failed for %s; using template", tag)
                copy = ""
        if not copy:
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
        log.warning("outreach: missing discord_user_id/copy for %s; marking failed", tag)
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


# -- Phase 2: DM-receive reply state machine -------------------------------


def classify_reply(text: str) -> tuple[str, str]:
    """Read a member's DM reply → ('opt_out'|'email'|'code'|'other', value)."""
    body = (text or "").strip()
    if not body:
        return ("other", "")
    # Opt-out first, but never mistake an address ("no-reply@x.com") for a 'no'.
    if "@" not in body and _OPT_OUT_RE.search(body):
        return ("opt_out", body)
    for token in re.split(r"\s+", body):
        token = token.strip(".,;:<>()[]\"'!?")
        if db.is_valid_email(token):
            return ("email", token)
    match = _CODE_RE.search(body)
    if match:
        return ("code", match.group(1))
    return ("other", body)


def handle_dm_reply(
    member_tag: str,
    text: str,
    *,
    start_verification: Callable[[str, str], dict],
    check_code: Callable[[str, str], dict],
    now: Optional[str] = None,
    conn=None,
) -> Optional[str]:
    """Advance a member's outreach conversation from their DM reply. Returns the
    text Elixir should DM back, or None to stay silent (member isn't mid-flow).

    ``start_verification(tag, email) -> {ok, email?, error?}`` mails the 6-digit
    code; ``check_code(tag, code) -> {ok, email?, error?}`` verifies it. Both are
    injected (runtime.email_verification) so this stays unit-testable."""
    row = mo.get_outreach(member_tag, conn=conn)
    if not row or row.get("status") not in _ACTIVE_DM_STATUSES:
        return None  # not mid-outreach — Elixir is not a general DM bot
    now = _now(now)
    kind, value = classify_reply(text)

    if kind == "opt_out":
        mo.opt_out(member_tag, reason="declined via DM", now=now, conn=conn)
        return "No worries at all — I won't ask again. Thanks for letting me know! \U0001f44d"

    if kind == "email":
        result = start_verification(member_tag, value)
        if result.get("ok"):
            mo.upsert_outreach(
                member_tag,
                status="verifying",
                pending_email=result.get("email") or value,
                now=now,
                conn=conn,
            )
            return (
                f"Great — I've emailed a 6-digit code to **{result.get('email') or value}**. "
                "Reply here with it to confirm (it expires in 15 minutes)."
            )
        return result.get("error") or "Hmm, that didn't work — mind trying again?"

    if kind == "code" and row.get("status") == "verifying":
        result = check_code(member_tag, value)
        if result.get("ok"):
            mo.upsert_outreach(member_tag, status="fulfilled", now=now, conn=conn)
            return (
                f"You're all set — **{result.get('email')}** is verified and on your "
                "profile. Thank you! \U0001f389"
            )
        return result.get("error") or "That code didn't work — mind trying again?"

    # Anything else: a gentle nudge appropriate to where they are.
    if row.get("status") == "verifying":
        return (
            "Reply with the 6-digit code I emailed you, or send a different email "
            "address to try again."
        )
    return (
        "Just reply with your email and I'll send a quick code to confirm it — or "
        'reply "no thanks" and I\'ll stop asking.'
    )
