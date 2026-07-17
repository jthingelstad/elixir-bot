"""Safe ops for the Observatory.

Each is deliberately narrow: run a tick now (through the scheduler so
max_instances=1 still guards), and a weekly-review dry-run (transaction +
ROLLBACK — the real review mutates week_anchor and counters). The legacy intent
queue is offline-only and has no production retry operation."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import db

log = logging.getLogger("elixir.webapp")


def schedule_tick_now() -> str:
    """One-shot engine tick via the bot's scheduler (never inline — the
    scheduler's max_instances=1 guard must keep applying)."""
    import runtime.app as app

    job_id = f"engine-tick-web-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    app.scheduler.add_job(app._engine_tick, trigger="date", id=job_id, misfire_grace_time=60)
    return job_id


class _NoCommitConnection:
    """Dry-run guard (cold review 2026-07-04 #9): the ROLLBACK guarantee rests
    entirely on nothing inside the review committing. That's true today, but
    one refactor (e.g. calling create_leader_action_recommendation, which
    commits internally) would silently make the dry run real. This proxy makes
    any commit() during the dry run raise instead."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):  # noqa: D102 — the whole point
        raise RuntimeError(
            "commit() during weekly-review DRY RUN — a code path inside "
            "run_weekly_review now commits; the dry run would have mutated "
            "live state. Fix the path or drop the dry-run feature."
        )

    def __getattr__(self, name):
        return getattr(self._conn, name)


def set_member_email(tag: str, email: str) -> str:
    """Admin set/clear a member's email from the Observatory. The webapp is
    tailnet-only behind the login allowlist, so the viewer is the admin; an email
    set here is trusted (verified). Empty / 'clear' removes it."""
    tag = tag if tag.startswith("#") else f"#{tag}"
    email = (email or "").strip()
    if email.lower() in ("", "clear", "none"):
        db.clear_member_email(tag)
        return "email cleared"
    if not db.is_valid_email(email):
        return f"invalid email: {email}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.set_member_email(tag, email, source="admin_set", verified_at=now)
    return f"email set to {email}"


def weekly_review_dryrun() -> dict:
    """Run the weekly review inside a transaction and ROLL BACK: renders what
    Monday would decide without rolling week_anchor or the hysteresis
    counters. The _NoCommitConnection proxy turns any accidental commit into
    a loud failure rather than a silent live mutation."""
    from engine import management

    today = date.today()
    monday = (today - timedelta(days=today.weekday())).isoformat()
    conn = db.get_connection()
    try:
        conn.execute("BEGIN")
        try:
            result = management.run_weekly_review(_NoCommitConnection(conn), monday)
        finally:
            conn.rollback()
        result["dry_run"] = True
        result["week_anchor"] = monday
        return result
    finally:
        conn.close()
