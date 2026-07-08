"""Member email verification: a member sets an email, Elixir mails a 6-digit code,
the member enters it in Discord to verify. Orchestrates code gen + email send +
the challenge row (storage.identity holds the DB primitives).

Codes are hashed with the player tag as salt; challenges expire and cap attempts.
All functions are synchronous — callers run them in a thread from the Discord loop.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

import db
from agent.mail import outbound

log = logging.getLogger("elixir.email_verify")

CODE_TTL_MINUTES = 15
MAX_ATTEMPTS = 5
_CODE_MODULO = 10 ** 6  # 6-digit, zero-padded


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _hash_code(code: str, player_tag: str) -> str:
    return hashlib.sha256(f"{player_tag}:{code}".encode()).hexdigest()


def start_verification(player_tag: str, email: str) -> dict:
    """Generate a code, store the challenge, and email the code. Returns
    {ok: bool, email?: str, error?: str}."""
    email = (email or "").strip()
    if not db.is_valid_email(email):
        return {"ok": False, "error": "that doesn't look like a valid email address."}
    if not outbound.enabled():
        return {"ok": False, "error": "email isn't configured on my end yet — flag a leader."}
    code = f"{secrets.randbelow(_CODE_MODULO):06d}"
    db.upsert_email_challenge(
        player_tag, pending_email=email, code_hash=_hash_code(code, player_tag),
        expires_at=_iso(_now() + timedelta(minutes=CODE_TTL_MINUTES)),
    )
    try:
        outbound.send(
            to=email,
            subject="Your Elixir verification code",
            body=(
                "Someone (hopefully you) asked to link this email to your POAP KINGS "
                "profile in Elixir.\n\n"
                f"Your verification code is **{code}**.\n\n"
                f"Enter it in Discord with `/elixir email verify {code}` within "
                f"{CODE_TTL_MINUTES} minutes. If this wasn't you, just ignore this email — "
                "nothing is linked until the code is entered."
            ),
        )
    except Exception as exc:
        log.exception("verification email send failed for %s", player_tag)
        db.clear_email_challenge(player_tag)
        return {"ok": False, "error": f"couldn't send the code: {exc}"}
    return {"ok": True, "email": email}


def check_code(player_tag: str, code: str) -> dict:
    """Validate a submitted code. On success, store the email as verified and clear
    the challenge. Returns {ok: bool, email?: str, error?: str}."""
    code = (code or "").strip()
    challenge = db.get_email_challenge(player_tag)
    if not challenge:
        return {"ok": False, "error": "no pending verification — start with `/elixir email set`."}
    expires = _parse(challenge["expires_at"])
    if expires is None or _now() > expires:
        db.clear_email_challenge(player_tag)
        return {"ok": False, "error": "that code expired — run `/elixir email set` to get a new one."}
    if challenge["attempts"] >= MAX_ATTEMPTS:
        db.clear_email_challenge(player_tag)
        return {"ok": False, "error": "too many attempts — run `/elixir email set` to get a new code."}
    if _hash_code(code, player_tag) != challenge["code_hash"]:
        left = max(0, MAX_ATTEMPTS - db.bump_email_challenge_attempts(player_tag))
        return {"ok": False, "error": f"that code didn't match. {left} attempt(s) left."}
    db.set_member_email(player_tag, challenge["pending_email"],
                        source="self_service", verified_at=_iso(_now()))
    db.clear_email_challenge(player_tag)
    return {"ok": True, "email": challenge["pending_email"]}
