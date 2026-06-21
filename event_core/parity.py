"""Exact-parity check: rebuilt projection vs frozen legacy.

Compares player_current_profile (elixir-v5.db) against the latest
player_profile_snapshots per member in elixir.db.legacy. Because Elixir is
stopped, the legacy DB is static, so this is a deterministic comparison.

Scope: only members with at least one archived /players payload are reproducible
from raw history (the ~2-week archive horizon). Members whose latest legacy
snapshot predates the archive are reported separately, not as failures.
"""
from __future__ import annotations

import sqlite3

from event_core import config
from event_core.domain.player import ROSTER_FIELDS, canon_tag
from event_core.projections.player_state import PROFILE_COLUMNS

# Columns present in BOTH the projection and player_profile_snapshots.
PARITY_COLUMNS = [c for c in PROFILE_COLUMNS if c not in ("name", "role")]


def _tag_key(tag: str) -> str:
    return canon_tag(tag).lstrip("#")


def check_player_profile_parity(
    legacy_path: str | None = None, projections_path: str | None = None
) -> dict:
    legacy = sqlite3.connect(legacy_path or config.LEGACY_DB)
    legacy.row_factory = sqlite3.Row
    proj = sqlite3.connect(projections_path or config.PROJECTIONS_DB)
    proj.row_factory = sqlite3.Row

    try:
        # tags that have archived player payloads (the reproducible set)
        archive_tags = {
            _tag_key(r["entity_key"])
            for r in legacy.execute(
                "SELECT DISTINCT entity_key FROM raw_api_payloads WHERE endpoint='player'"
            )
        }
        # latest legacy snapshot per member (max fetched_at, tiebreak max snapshot_id)
        legacy_latest: dict[str, sqlite3.Row] = {}
        q = """
            SELECT m.player_tag AS player_tag, ps.*
            FROM player_profile_snapshots ps
            JOIN members m ON m.member_id = ps.member_id
            JOIN (
                SELECT member_id, MAX(fetched_at) AS mx FROM player_profile_snapshots GROUP BY member_id
            ) latest ON latest.member_id = ps.member_id AND latest.mx = ps.fetched_at
        """
        for r in legacy.execute(q):
            tk = _tag_key(r["player_tag"])
            prev = legacy_latest.get(tk)
            if prev is None or r["snapshot_id"] > prev["snapshot_id"]:
                legacy_latest[tk] = r

        proj_rows = {
            _tag_key(r["player_tag"]): r
            for r in proj.execute("SELECT * FROM player_current_profile")
        }
    finally:
        legacy.close()
        proj.close()

    matched, mismatches, missing_projection, outside_archive = [], [], [], []

    for tk, leg in legacy_latest.items():
        if tk not in archive_tags:
            outside_archive.append(tk)
            continue
        pr = proj_rows.get(tk)
        if pr is None:
            missing_projection.append(tk)
            continue
        field_diffs = {}
        for col in PARITY_COLUMNS:
            lv, pv = leg[col], pr[col]
            if lv != pv:
                field_diffs[col] = {"legacy": lv, "projection": pv}
        if field_diffs:
            mismatches.append({"tag": tk, "diffs": field_diffs})
        else:
            matched.append(tk)

    return {
        "reproducible_members": len(matched) + len(mismatches) + len(missing_projection),
        "matched": len(matched),
        "mismatched": len(mismatches),
        "missing_projection": len(missing_projection),
        "outside_archive_horizon": len(outside_archive),
        "mismatch_detail": mismatches[:25],
        "missing_detail": missing_projection[:25],
    }


def check_member_current_state_parity(
    legacy_path: str | None = None, projections_path: str | None = None
) -> dict:
    """Compare member_current_state_proj vs legacy member_current_state.

    Reproducible set = members present in the projection (i.e. observed in an
    archived /clans roster). Legacy rows for members who left before the archive
    window are reported as outside_archive_horizon, not failures.
    """
    legacy = sqlite3.connect(legacy_path or config.LEGACY_DB)
    legacy.row_factory = sqlite3.Row
    proj = sqlite3.connect(projections_path or config.PROJECTIONS_DB)
    proj.row_factory = sqlite3.Row

    try:
        legacy_rows = {
            _tag_key(r["player_tag"]): r
            for r in legacy.execute(
                "SELECT m.player_tag AS player_tag, mcs.* FROM member_current_state mcs "
                "JOIN members m ON m.member_id = mcs.member_id"
            )
        }
        # Only members actually observed in an archived roster (observed_at set);
        # tag-only rows come from profile-ingest Registered for ex-members.
        proj_rows = {
            _tag_key(r["player_tag"]): r
            for r in proj.execute(
                "SELECT * FROM member_current_state_proj WHERE observed_at IS NOT NULL"
            )
        }
    finally:
        legacy.close()
        proj.close()

    matched, mismatches, missing_legacy, v5_more_current = [], [], [], []
    for tk, pr in proj_rows.items():
        leg = legacy_rows.get(tk)
        if leg is None:
            missing_legacy.append(tk)
            continue
        field_diffs = {}
        for col in ROSTER_FIELDS:
            if leg[col] != pr[col]:
                field_diffs[col] = {"legacy": leg[col], "projection": pr[col]}
        if not field_diffs:
            matched.append(tk)
            continue
        # Classify: legacy member_current_state is heartbeat-only, while backfill
        # consumes every archived clan fetch. A projection that observed a later
        # roster snapshot than legacy is more-current, not wrong.
        lp, ll = pr["last_seen_api"], leg["last_seen_api"]
        if lp and ll and str(lp) > str(ll):
            v5_more_current.append({"tag": tk, "diffs": field_diffs})
        else:
            mismatches.append({"tag": tk, "diffs": field_diffs})

    outside = [tk for tk in legacy_rows if tk not in proj_rows]
    return {
        "reproducible_members": len(matched) + len(mismatches) + len(v5_more_current),
        "matched": len(matched),
        "mismatched": len(mismatches),
        "v5_more_current": len(v5_more_current),
        "missing_in_legacy": len(missing_legacy),
        "outside_archive_horizon": len(outside),
        "mismatch_detail": mismatches[:25],
        "more_current_detail": v5_more_current[:25],
    }
