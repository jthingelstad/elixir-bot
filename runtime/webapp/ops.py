"""Safe ops for the Observatory (the approved plan's three, nothing more).

Each is deliberately narrow: run a tick now (through the scheduler so
max_instances=1 still guards), retry a failed/expired intent (delivery picks
it up next tick), and a weekly-review dry-run (transaction + ROLLBACK — the
real review mutates week_anchor and counters)."""

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
    app.scheduler.add_job(
        app._engine_tick, trigger="date", id=job_id, misfire_grace_time=60
    )
    return job_id


def retry_intent(intent_id: int) -> bool:
    """failed/expired → pending with a fresh 6h expiry; next tick delivers."""
    conn = db.get_connection()
    try:
        expires = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        cur = conn.execute(
            """UPDATE communication_intents
               SET status='pending', expires_at=?, last_error=NULL
               WHERE intent_id=? AND status IN ('failed','expired')""",
            (expires, int(intent_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


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
