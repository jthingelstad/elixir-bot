"""Exact-parity check: rebuilt clan_daily_metrics_proj vs frozen legacy.

Compares the rebuilt clan_daily_metrics projection against the legacy
clan_daily_metrics table on the directly-observable fields. Because Elixir is
stopped, the legacy DB is static, so this is deterministic.

Scope: only Chicago days reproducible from the raw /clans archive. Legacy days
that predate the archive horizon are reported separately (outside_archive), not
as failures — mirroring parity.py's archive-horizon handling.

Deferred fields (joins_today / leaves_today / net_member_change) are roster
join/leave diffs (a separate roster-lifecycle concern) and are NOT compared.
"""
from __future__ import annotations

import sqlite3

from event_core import config
from event_core.domain.clan import canon_tag
from event_core.timeutil import chicago_day_for_utc

# Directly-observable parity fields (everything the projection sources straight
# from the /clans payload). Excludes clan_name (cosmetic) is kept IN — it is
# directly observable and legacy stores it — but listed last so a name-only diff
# is easy to spot. Excludes the deferred roster-lifecycle fields entirely.
OBSERVABLE_FIELDS = [
    "member_count",
    "open_slots",
    "clan_score",
    "clan_war_trophies",
    "required_trophies",
    "weekly_donations_total",
    "total_member_trophies",
    "avg_member_trophies",
    "top_member_trophies",
]
DEFERRED_FIELDS = ["joins_today", "leaves_today", "net_member_change"]


def _reproducible_days(legacy: sqlite3.Connection) -> set[str]:
    """Chicago days covered by at least one archived /clans payload."""
    days: set[str] = set()
    for r in legacy.execute(
        "SELECT fetched_at FROM raw_api_payloads WHERE endpoint='clan'"
    ):
        d = chicago_day_for_utc(r["fetched_at"])
        if d:
            days.add(d)
    return days


def _eq(a, b) -> bool:
    """Numeric-tolerant equality (avg is REAL; treat 7531.5 == 7531.50)."""
    if a is None or b is None:
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-6
    return a == b


def check_clan_daily_metrics_parity(
    legacy_path: str | None = None, projections_path: str | None = None
) -> dict:
    legacy = sqlite3.connect(legacy_path or config.LEGACY_DB)
    legacy.row_factory = sqlite3.Row
    proj = sqlite3.connect(projections_path or config.PROJECTIONS_DB)
    proj.row_factory = sqlite3.Row

    try:
        repro = _reproducible_days(legacy)
        legacy_rows = {
            (canon_tag(r["clan_tag"]), r["metric_date"]): r
            for r in legacy.execute("SELECT * FROM clan_daily_metrics")
        }
        proj_rows = {
            (canon_tag(r["clan_tag"]), r["metric_date"]): r
            for r in proj.execute("SELECT * FROM clan_daily_metrics_proj")
        }
    finally:
        legacy.close()
        proj.close()

    matched, mismatches, missing_projection = [], [], []
    outside_archive = []

    for key, leg in legacy_rows.items():
        _tag, day = key
        if day not in repro:
            outside_archive.append(day)
            continue
        pr = proj_rows.get(key)
        if pr is None:
            missing_projection.append(day)
            continue
        diffs = {}
        for col in OBSERVABLE_FIELDS:
            lv, pv = leg[col], pr[col]
            if not _eq(lv, pv):
                diffs[col] = {"legacy": lv, "projection": pv}
        if diffs:
            mismatches.append(
                {
                    "day": day,
                    "diffs": diffs,
                    "legacy_observed_at": leg["observed_at"],
                    "proj_observed_at": pr["observed_at"],
                }
            )
        else:
            matched.append(day)

    # Days the projection has that legacy doesn't (e.g. partial current day).
    proj_only = [
        day
        for (tag, day) in proj_rows
        if (tag, day) not in legacy_rows and day in repro
    ]

    return {
        "reproducible_days": len(repro),
        "compared": len(matched) + len(mismatches) + len(missing_projection),
        "matched": len(matched),
        "mismatched": len(mismatches),
        "missing_projection": len(missing_projection),
        "outside_archive_horizon": len(outside_archive),
        "projection_only_days": sorted(proj_only),
        "deferred_fields": DEFERRED_FIELDS,
        "mismatch_detail": sorted(mismatches, key=lambda m: m["day"]),
        "matched_days": sorted(matched),
        "missing_detail": sorted(set(missing_projection)),
    }
