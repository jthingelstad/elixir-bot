import db
from storage import member_outreach as mo
from storage.identity import link_discord_user_to_member, set_member_email


def _seed(conn):
    db.snapshot_members(
        [
            {"tag": "#AAA", "name": "Alpha", "role": "member"},
            {"tag": "#BBB", "name": "Bravo", "role": "member"},
            {"tag": "#CCC", "name": "Charlie", "role": "member"},
            {"tag": "#DDD", "name": "Delta", "role": "member"},
        ],
        conn=conn,
    )
    # Alpha: linked, no email at all -> TARGET.
    link_discord_user_to_member("1001", "#AAA", conn=conn)
    # Bravo: linked with a VERIFIED email -> not a target.
    link_discord_user_to_member("1002", "#BBB", conn=conn)
    set_member_email(
        "#BBB",
        "b@example.com",
        source="admin_set",
        verified_at="2026-01-01T00:00:00Z",
        conn=conn,
    )
    # Charlie: NO discord link -> unreachable, never a target.
    # Delta: linked, email present but UNVERIFIED -> still a target.
    link_discord_user_to_member("1004", "#DDD", conn=conn)
    set_member_email("#DDD", "d@example.com", source="self_service", conn=conn)


def test_targeting_picks_reachable_members_missing_verified_email(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        tags = {t["player_tag"] for t in mo.eligible_targets(conn=conn)}
        assert "#AAA" in tags  # linked, no email
        assert "#DDD" in tags  # linked, unverified email
        assert "#BBB" not in tags  # verified email already
        assert "#CCC" not in tags  # not reachable by DM
    finally:
        conn.close()


def test_opt_out_is_durable_and_excludes_member(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        mo.opt_out("#AAA", reason="asked not to be contacted", conn=conn)
        tags = {t["player_tag"] for t in mo.eligible_targets(conn=conn)}
        assert "#AAA" not in tags
        row = mo.get_outreach("#AAA", conn=conn)
        assert row["status"] == "opted_out" and row["consent"] == "opted_out"
    finally:
        conn.close()


def test_in_flight_and_cooldown_exclude_then_reeligible(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        # A raised leader card (proposed) is in-flight — excluded.
        mo.upsert_outreach("#AAA", status="proposed", conn=conn)
        # A failed attempt with a future cooldown — excluded until it passes.
        mo.upsert_outreach(
            "#DDD", status="failed", next_eligible_at="2999-01-01T00:00:00Z", conn=conn
        )
        tags = {t["player_tag"] for t in mo.eligible_targets(conn=conn)}
        assert "#AAA" not in tags and "#DDD" not in tags
        # Once the cooldown passes, the failed member is eligible to retry.
        later = {
            t["player_tag"] for t in mo.eligible_targets(now="2999-06-01T00:00:00Z", conn=conn)
        }
        assert "#DDD" in later
        assert "#AAA" not in later  # 'proposed' is in-flight regardless of time
    finally:
        conn.close()


def test_departed_member_is_not_targeted(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        conn.execute(
            "UPDATE clan_memberships SET left_at = ? WHERE player_tag = ?",
            ("2026-07-01T00:00:00Z", "#AAA"),
        )
        conn.commit()
        tags = {t["player_tag"] for t in mo.eligible_targets(conn=conn)}
        assert "#AAA" not in tags
    finally:
        conn.close()


def test_upsert_bumps_attempts_and_preserves_untouched_fields(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        mo.upsert_outreach(
            "#AAA", status="sent", discord_user_id="1001", bump_attempts=True, conn=conn
        )
        mo.upsert_outreach("#AAA", status="awaiting_reply", bump_attempts=True, conn=conn)
        row = mo.get_outreach("#AAA", conn=conn)
        assert row["attempts"] == 2
        assert row["status"] == "awaiting_reply"
        assert row["discord_user_id"] == "1001"  # preserved across the second upsert
    finally:
        conn.close()
