"""Exact-parity check: rebuilt projection vs frozen legacy.

Compares player_current_profile (elixir-v5.db) against the latest
player_profile_snapshots per member in elixir.db.legacy. Because Elixir is
stopped, the legacy DB is static, so this is a deterministic comparison.

Scope: only members with at least one archived /players payload are reproducible
from raw history (the ~2-week archive horizon). Members whose latest legacy
snapshot predates the archive are reported separately, not as failures.
"""
from __future__ import annotations

import json
import sqlite3

from event_core import config
from event_core.domain.player import PROFILE_BADGE_FIELD_TYPES, ROSTER_FIELDS, canon_tag
from event_core.ingest.profile import build_profile_observation
from event_core.projections.player_state import PROFILE_COLUMNS

# Columns present in BOTH the projection and player_profile_snapshots. Badge-backed
# profile fields are validated separately against raw /players payloads because
# legacy stored them in member_metadata or not at all.
PARITY_COLUMNS = [
    c
    for c in PROFILE_COLUMNS
    if c not in ("name", "role") and c not in PROFILE_BADGE_FIELD_TYPES
]


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


def check_player_profile_badge_parity(
    legacy_path: str | None = None, projections_path: str | None = None
) -> dict:
    """Compare v5 badge-backed profile fields against latest raw /players payloads."""
    legacy = sqlite3.connect(legacy_path or config.LEGACY_DB)
    legacy.row_factory = sqlite3.Row
    proj = sqlite3.connect(projections_path or config.PROJECTIONS_DB)
    proj.row_factory = sqlite3.Row

    try:
        latest_payloads: dict[str, dict] = {}
        for r in legacy.execute(
            "SELECT entity_key, payload_json FROM raw_api_payloads "
            "WHERE endpoint='player' ORDER BY entity_key, fetched_at DESC, payload_id DESC"
        ):
            tk = _tag_key(r["entity_key"])
            if tk not in latest_payloads:
                latest_payloads[tk] = json.loads(r["payload_json"])

        proj_rows = {
            _tag_key(r["player_tag"]): r
            for r in proj.execute("SELECT * FROM player_current_profile")
        }
    finally:
        legacy.close()
        proj.close()

    matched, mismatches, missing_projection, without_badges = [], [], [], []
    badge_cols = tuple(PROFILE_BADGE_FIELD_TYPES)

    for tk, payload in latest_payloads.items():
        expected = build_profile_observation(payload)
        if not any(col in expected for col in badge_cols):
            without_badges.append(tk)
            continue
        pr = proj_rows.get(tk)
        if pr is None:
            missing_projection.append(tk)
            continue
        field_diffs = {}
        for col in badge_cols:
            expected_value = expected.get(col)
            projected_value = pr[col]
            if expected_value != projected_value:
                field_diffs[col] = {
                    "raw_payload": expected_value,
                    "projection": projected_value,
                }
        if field_diffs:
            mismatches.append({"tag": tk, "diffs": field_diffs})
        else:
            matched.append(tk)

    return {
        "reproducible_members": len(matched) + len(mismatches) + len(missing_projection),
        "matched": len(matched),
        "mismatched": len(mismatches),
        "missing_projection": len(missing_projection),
        "without_badges": len(without_badges),
        "mismatch_detail": mismatches[:25],
        "missing_detail": missing_projection[:25],
    }


def check_battle_telemetry_parity(
    legacy_path: str | None = None, projections_path: str | None = None
) -> dict:
    """Compare battle_telemetry identities vs legacy member_battle_facts.

    Scoped per member to that member's covered window [min,max battle_time present
    in telemetry] to avoid battlelog-window boundary effects. Identity =
    (tag, battle_time, battle_type, opponent_tag, crowns_for, crowns_against).
    """
    legacy = sqlite3.connect(legacy_path or config.LEGACY_DB)
    legacy.row_factory = sqlite3.Row
    proj = sqlite3.connect(projections_path or config.PROJECTIONS_DB)
    proj.row_factory = sqlite3.Row

    def ident(tag, bt, btype, opp, cf, ca):
        return (_tag_key(tag), bt, btype, _tag_key(opp or ""), cf, ca)

    # outcome is deterministic (crowns/trophy/boat) -> must match exactly.
    # the classification flags derive from classify_battle_mode, whose logic has
    # evolved; legacy stores historical values, backfill applies current canonical
    # logic, so differences there are drift, not defects.
    outcome_cols = ["outcome"]
    classification_cols = [
        "is_war", "is_ladder", "is_ranked", "is_competitive",
        "is_special_event", "game_mode_id",
    ]
    derived_cols = outcome_cols + classification_cols

    try:
        mine: dict[tuple, dict] = {}
        window: dict[str, list] = {}
        for r in proj.execute("SELECT * FROM battle_telemetry"):
            tk = _tag_key(r["player_tag"])
            key = ident(r["player_tag"], r["battle_time"], r["battle_type"],
                        r["opponent_tag"], r["crowns_for"], r["crowns_against"])
            mine[key] = {c: r[c] for c in derived_cols}
            w = window.setdefault(tk, [r["battle_time"], r["battle_time"]])
            w[0] = min(w[0], r["battle_time"])
            w[1] = max(w[1], r["battle_time"])

        legacy_rows: dict[tuple, dict] = {}
        for r in legacy.execute(
            "SELECT m.player_tag AS pt, mbf.* FROM member_battle_facts mbf "
            "JOIN members m ON m.member_id = mbf.member_id"
        ):
            tk = _tag_key(r["pt"])
            if tk not in window:
                continue
            lo, hi = window[tk]
            if not (lo <= r["battle_time"] <= hi):
                continue
            key = ident(r["pt"], r["battle_time"], r["battle_type"],
                        r["opponent_tag"], r["crowns_for"], r["crowns_against"])
            legacy_rows[key] = {c: r[c] for c in derived_cols}
    finally:
        legacy.close()
        proj.close()

    mine_keys, legacy_keys = set(mine), set(legacy_rows)
    inter = mine_keys & legacy_keys
    outcome_mismatch = []
    classification_drift = 0
    fully_clean = 0
    for key in inter:
        out_diffs = {c: {"legacy": legacy_rows[key][c], "projection": mine[key][c]}
                     for c in outcome_cols if legacy_rows[key][c] != mine[key][c]}
        cls_drift = any(legacy_rows[key][c] != mine[key][c] for c in classification_cols)
        if out_diffs:
            outcome_mismatch.append({"battle": list(key), "diffs": out_diffs})
        if cls_drift:
            classification_drift += 1
        if not out_diffs and not cls_drift:
            fully_clean += 1

    return {
        "battles_matched_identity": len(inter),
        "fully_clean": fully_clean,
        "outcome_mismatch": len(outcome_mismatch),
        "classification_drift": classification_drift,  # current vs historical classify logic
        # coverage artifacts (expected, not pipeline defects):
        "only_in_projection": len(mine_keys - legacy_keys),  # v5 captured pre-tracking history
        "only_in_legacy": len(legacy_keys - mine_keys),      # battlelog rolling-window gaps
        "outcome_mismatch_detail": outcome_mismatch[:25],
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
