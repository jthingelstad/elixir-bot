"""C1d regression: list_member_metadata_rows must read v5.1 tables
(players/player_current_state/player_metadata/discord_links+discord_users),
not the dropped v4 members/member_metadata/member_current_state.
"""
from __future__ import annotations

import db


def _seed_member(conn, tag, name, *, active=True, role="member", clan_rank=1):
    conn.execute("INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
                 "VALUES (?,?,?,?)", (tag, name, "2026-01-01T00:00:00", "2026-07-01T00:00:00"))
    conn.execute("INSERT INTO player_current_state (player_tag, observed_at, role, clan_rank) "
                 "VALUES (?,?,?,?)", (tag, "2026-07-01T00:00:00", role, clan_rank))
    conn.execute("INSERT INTO player_metadata (player_tag, joined_at, birth_month, birth_day, "
                 "profile_url, cr_collection_level) VALUES (?,?,?,?,?,?)",
                 (tag, "2026-02-15", 3, 14, "https://example.com/p", 1600))
    conn.execute("INSERT INTO clan_memberships (player_tag, joined_at, left_at, join_source) "
                 "VALUES (?,?,?,'observed_join')",
                 (tag, "2026-02-15", None if active else "2026-06-01"))


def test_list_member_metadata_rows_reads_v51_and_filters_active():
    conn = db.get_connection()
    try:
        conn.execute("INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
                     "VALUES ('#J2RGCRVG','POAP KINGS','x','x',1)")
        _seed_member(conn, "#A1", "Ada", active=True, clan_rank=1)
        _seed_member(conn, "#B2", "Ben", active=False, clan_rank=2)
        # Discord identity for Ada via discord_users + discord_links
        conn.execute("INSERT INTO discord_users (discord_user_id, username, global_name, display_name, "
                     "first_seen_at, last_seen_at) VALUES ('111','ada_cr','Ada','Ada CR','x','x')")
        conn.execute("INSERT INTO discord_links (discord_user_id, player_tag, linked_at, source, "
                     "confidence, is_primary) VALUES ('111','#A1','x','manual',1.0,1)")
        conn.commit()

        active = db.list_member_metadata_rows()          # default status='active'
        tags = {r["player_tag"] for r in active}
        assert tags == {"#A1"}                             # Ben (left) excluded
        ada = active[0]
        assert ada["current_name"] == "Ada"
        assert ada["status"] == "active"
        assert ada["role"] == "member"
        assert ada["joined_date"] == "2026-02-15"
        assert ada["birth_month"] == 3 and ada["birth_day"] == 14
        assert ada["cr_collection_level"] == 1600
        assert ada["discord_username"] == "ada_cr"
        assert ada["discord_display_name"] == "Ada CR"
        assert ada["poap_address"] == ""                   # dropped in v5.1

        every = db.list_member_metadata_rows(status=None)  # all players
        assert {r["player_tag"] for r in every} == {"#A1", "#B2"}
        ben = next(r for r in every if r["player_tag"] == "#B2")
        assert ben["status"] == "left"
    finally:
        conn.close()
