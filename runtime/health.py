"""Daily engine health checks — the institutionalized live audit.

Born from the 2026-07-04 end-to-end review: a live behavioral audit found a
season-breaking gap and a cold review found ten more issues that tests missed.
These checks encode that audit's queries so the watching never stops. Pure
read-only functions over the operational DB; the `engine-health` activity runs
them daily and posts to #elixir-log ONLY when something is off.
"""

from __future__ import annotations

import json
import os


def _rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def check_tick_errors(conn) -> list[str]:
    """Any *_error keys in the last 24h of tick history, or a recent failure."""
    problems: list[str] = []
    rows = _rows(conn, """
        SELECT counters_json FROM tick_history
        WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day')
        ORDER BY tick_id DESC LIMIT 200""")
    err_ticks = 0
    for r in rows:
        try:
            counters = json.loads(r["counters_json"])
        except (TypeError, ValueError):
            continue
        if any(k.endswith("_error") for k in counters):
            err_ticks += 1
    if err_ticks:
        problems.append(f"{err_ticks} tick(s) with step errors in the last 24h")
    row = conn.execute(
        "SELECT status_json FROM runtime_job_status WHERE job_name = 'engine_tick'"
    ).fetchone()
    if row:
        try:
            state = json.loads(row["status_json"])
            last_failure = state.get("last_failure_at") or ""
            if last_failure and last_failure >= _cutoff_iso(conn, "-1 day"):
                problems.append(f"engine_tick recorded a failure at {last_failure}")
        except (TypeError, ValueError):
            pass
    return problems


def check_stuck_intents(conn) -> list[str]:
    n = conn.execute("""
        SELECT COUNT(*) FROM communication_intents
        WHERE status IN ('pending', 'failed')
          AND created_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-2 hours')
    """).fetchone()[0]
    return [f"{n} intent(s) stuck pending/failed for >2h"] if n else []


def check_ledger_duplicates(conn) -> list[str]:
    total, distinct = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT recognition_key) FROM recognition_ledger"
    ).fetchone()
    return (
        [f"recognition ledger has {total - distinct} duplicate key(s) — the one-moment-one-post invariant broke"]
        if total != distinct else []
    )


def check_poll_starvation(conn) -> list[str]:
    """Fairness floors (runtime.md §4): battlelog ≤6h, profile ≤24h for every
    open-membership member."""
    problems = []
    rows = _rows(conn, """
        SELECT ps.player_tag,
               COALESCE(ps.last_battlelog_poll, '') < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-6 hours') AS bl_starved,
               COALESCE(ps.last_profile_poll, '')  < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-24 hours') AS pf_starved
        FROM poll_state ps
        WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                      WHERE cm.player_tag = ps.player_tag AND cm.left_at IS NULL)""")
    bl = sum(1 for r in rows if r["bl_starved"])
    pf = sum(1 for r in rows if r["pf_starved"])
    if bl:
        problems.append(f"{bl} member(s) past the 6h battlelog floor")
    if pf:
        problems.append(f"{pf} member(s) past the 24h profile floor")
    return problems


def check_memory_writes(conn) -> list[str]:
    row = conn.execute("SELECT MAX(created_at) FROM memories").fetchone()
    latest = row[0] if row else None
    if latest and latest < _cutoff_iso(conn, "-2 days"):
        return [f"no memory written since {latest} — writers may be broken"]
    return []


def check_db_size(conn, previous_bytes: int | None) -> tuple[list[str], int]:
    """Flag >25% day-over-day growth; returns (problems, current_size)."""
    import db as _db

    size = os.path.getsize(_db._resolve_db_path())
    problems = []
    if previous_bytes and size > previous_bytes * 1.25:
        problems.append(
            f"db grew {size / previous_bytes:.0%} of yesterday's size "
            f"({previous_bytes / 1e6:.0f}MB → {size / 1e6:.0f}MB)"
        )
    return problems, size


def _cutoff_iso(conn, offset: str) -> str:
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)", (offset,)
    ).fetchone()[0]


def run_all(conn, previous_size: int | None = None) -> tuple[list[str], int]:
    problems: list[str] = []
    for check in (check_tick_errors, check_stuck_intents,
                  check_ledger_duplicates, check_poll_starvation,
                  check_memory_writes):
        try:
            problems.extend(check(conn))
        except Exception as exc:  # a broken check is itself a finding
            problems.append(f"health check {check.__name__} failed: {exc!r}")
    size_problems, size = check_db_size(conn, previous_size)
    problems.extend(size_problems)
    return problems, size
