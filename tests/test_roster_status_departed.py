"""Departed members must be flagged, not reported as active roster players /
war no-shows (QA H6/M1/M20/L18).

member_roster_status classifies active (open membership) vs departed (only
closed memberships, with the last left_at) vs unknown (never in
clan_memberships); it's the shared annotation used across the member tools.
"""

from __future__ import annotations

import db
from storage._enrichment import _clear_member_ranks_cache


def test_member_roster_status_active_vs_departed_vs_unknown():
    _clear_member_ranks_cache()
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO clans (clan_tag, first_seen_at, last_seen_at, is_home) "
            "VALUES ('#J2RGCRVG','2026-07-01','2026-07-11',1)"
        )
        for tag, name in (
            ("#ACT", "Active"),
            ("#DEP", "Departed"),
            ("#GHOST", "Ghost"),
        ):
            conn.execute(
                "INSERT INTO players (player_tag, current_name, display_name, first_seen_at, last_seen_at) "
                "VALUES (?,?,?,'2026-07-01','2026-07-11')",
                (tag, name, name),
            )
        conn.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, join_source) VALUES ('#ACT','2026-07-01','t')"
        )
        conn.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, left_at, join_source) "
            "VALUES ('#DEP','2026-06-01','2026-07-05T00:00:00Z','t')"
        )
        # #GHOST has a players row but never a membership → unknown
        conn.commit()

        assert db.member_roster_status("#ACT", conn=conn)["roster_status"] == "active"
        dep = db.member_roster_status("#DEP", conn=conn)
        assert dep["roster_status"] == "departed"
        assert dep["left_at"] == "2026-07-05T00:00:00Z"
        assert (
            db.member_roster_status("#GHOST", conn=conn)["roster_status"] == "unknown"
        )

        # The enrichment choke point cascades the flag to member-facing rows.
        from storage._enrichment import _member_reference_fields

        enriched = _member_reference_fields(
            conn, "#DEP", {"tag": "#DEP", "name": "Departed"}
        )
        assert enriched["roster_status"] == "departed" and enriched["left_at"]
    finally:
        conn.close()
