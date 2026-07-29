from __future__ import annotations

import db


def test_admin_command_telemetry_records_metadata_and_aggregates_accepted_use(tmp_path):
    conn = db.get_connection(tmp_path / "admin-telemetry.db")
    try:
        accepted_id = db.record_admin_command_invocation(
            "activity.run",
            "activity_run",
            discord_user_id=123,
            channel_id=456,
            write_requested=True,
            accepted=True,
            conn=conn,
        )
        rejected_id = db.record_admin_command_invocation(
            "activity.run",
            "activity_run",
            discord_user_id=789,
            channel_id=999,
            write_requested=True,
            accepted=False,
            conn=conn,
        )

        rows = conn.execute(
            "SELECT * FROM admin_command_invocations ORDER BY invocation_id"
        ).fetchall()
        assert [row["invocation_id"] for row in rows] == [accepted_id, rejected_id]
        assert dict(rows[0]) == {
            "invocation_id": accepted_id,
            "command_key": "activity.run",
            "event_type": "activity_run",
            "discord_user_id": "123",
            "channel_id": "456",
            "write_requested": 1,
            "accepted": 1,
            "invoked_at": rows[0]["invoked_at"],
        }
        assert "T" in rows[0]["invoked_at"]

        assert db.list_admin_command_usage(conn=conn) == [
            {
                "command_key": "activity.run",
                "event_type": "activity_run",
                "invocation_count": 1,
                "last_invoked_at": rows[0]["invoked_at"],
            }
        ]
        assert (
            db.list_admin_command_usage(accepted_only=False, conn=conn)[0]["invocation_count"] == 2
        )
    finally:
        conn.close()
