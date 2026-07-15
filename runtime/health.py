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


def check_new_incidents(conn) -> list[str]:
    """Unresolved error-severity incidents in the last 24h (confidence plan §1)
    — surfaces the silent failures the ledger now captures, so the daily digest
    to #elixir-log names them."""
    try:
        from storage.incidents import count_open_since
        n = count_open_since(conn, hours=24)
    except Exception as exc:  # noqa: BLE001
        return [f"incident check failed: {exc!r}"]
    return [f"{n} unresolved incident(s) in the last 24h — see /incidents"] if n else []


def check_data_integrity(conn) -> list[str]:
    """Relational + semantic invariants that a plain integrity_check misses."""
    problems: list[str] = []
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        problems.append(f"{len(foreign_keys)} foreign-key violation(s)")
    overlaps = conn.execute(
        "SELECT COUNT(*) FROM clan_memberships a JOIN clan_memberships b "
        "ON a.player_tag=b.player_tag AND a.clan_tag=b.clan_tag "
        "AND a.membership_id<b.membership_id "
        "WHERE a.joined_at < COALESCE(b.left_at, '9999-12-31') "
        "AND b.joined_at < COALESCE(a.left_at, '9999-12-31')"
    ).fetchone()[0]
    if overlaps:
        problems.append(f"{overlaps} overlapping membership interval(s)")
    mismatches = conn.execute(
        "WITH profile AS (SELECT entity_tag AS player_tag, "
        "CAST(json_extract(payload_json, '$.best_trophies') AS INTEGER) AS best, "
        "CAST(json_extract(payload_json, '$.exp_level') AS INTEGER) AS exp "
        "FROM state_baselines WHERE entity_kind='player' AND aspect='profile') "
        "SELECT COUNT(*) FROM profile p JOIN player_current_state cs USING(player_tag) "
        "WHERE (p.best IS NOT NULL AND cs.best_trophies IS NOT p.best) "
        "OR (p.exp > 0 AND cs.exp_level IS NOT p.exp)"
    ).fetchone()[0]
    if mismatches:
        problems.append(f"{mismatches} profile-owned player projection mismatch(es)")
    best_drops = conn.execute(
        "WITH ordered AS (SELECT player_tag, metric_date, best_trophies, "
        "LAG(best_trophies) OVER (PARTITION BY player_tag ORDER BY metric_date) prior "
        "FROM player_daily_metrics) SELECT COUNT(*) FROM ordered "
        "WHERE best_trophies IS NULL AND prior IS NOT NULL"
    ).fetchone()[0]
    if best_drops:
        problems.append(f"{best_drops} daily best-trophy null regression(s)")
    compact_war = 0
    for table, column in (
        ("war_weeks", "created_date"),
        ("war_participation", "observed_at"),
        ("war_week_clans", "observed_at"),
        ("war_attendance_days", "observed_at"),
    ):
        compact_war += conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL "
            f"AND (length({column}) < 10 OR substr({column},5,1)<>'-' "
            f"OR substr({column},8,1)<>'-')"
        ).fetchone()[0]
    if compact_war:
        problems.append(f"{compact_war} noncanonical war timestamp(s)")
    return problems


SILENCE_HOURS = 14  # no Discord output at all in this window → investigate
LEADER_ACTION_STALE_HOURS = 2  # a proposed card unposted this long → posting broken


def check_output_silence(conn) -> list[str]:
    """Silence is itself an alarm (Jamie, 2026-07-05). The `can_post_leader_action`
    NameError killed ALL card posting with no symptom but quiet. This catches two
    silence signatures: no Discord output at all, and leader-action cards that were
    recommended but never posted."""
    problems = []
    # (a) total output silence — the Pulse alone posts every ~8h, so 14h dark is wrong.
    row = conn.execute("SELECT MAX(posted_at) FROM awareness_posts").fetchone()
    last = row[0] if row else None
    if last:
        hrs = conn.execute(
            "SELECT ROUND((julianday('now') - julianday(?)) * 24, 1)", (last,)
        ).fetchone()[0]
        if hrs is not None and hrs > SILENCE_HOURS:
            problems.append(
                f"no Discord output in {hrs}h (last post {last}) — Elixir may be silently stuck")
    # (b) the can_post_leader_action signature: proposed but never posted.
    stuck = conn.execute(
        "SELECT COUNT(*) FROM leader_action_recommendations "
        "WHERE status = 'proposed' AND copy_message_id IS NULL "
        "AND COALESCE(is_test, 0) = 0 AND proposed_at < ?",
        (_cutoff_iso(conn, f"-{LEADER_ACTION_STALE_HOURS} hours"),),
    ).fetchone()[0]
    if stuck:
        problems.append(
            f"{stuck} leader-action(s) proposed >{LEADER_ACTION_STALE_HOURS}h ago but never "
            f"posted — card posting may be broken")
    return problems


def _cutoff_iso(conn, offset: str) -> str:
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)", (offset,)
    ).fetchone()[0]


def run_all(conn, previous_size: int | None = None) -> tuple[list[str], int]:
    problems: list[str] = []
    for check in (check_tick_errors, check_ledger_duplicates, check_poll_starvation,
                  check_memory_writes, check_new_incidents, check_output_silence,
                  check_data_integrity):
        try:
            problems.extend(check(conn))
        except Exception as exc:  # a broken check is itself a finding
            problems.append(f"health check {check.__name__} failed: {exc!r}")
    size_problems, size = check_db_size(conn, previous_size)
    problems.extend(size_problems)
    return problems, size
