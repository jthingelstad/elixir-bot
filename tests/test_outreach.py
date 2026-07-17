import db
from runtime import outreach
from storage import member_outreach as mo
from storage.identity import link_discord_user_to_member


def _seed(conn):
    db.snapshot_members([{"tag": "#AAA", "name": "Alpha", "role": "member"}], conn=conn)
    link_discord_user_to_member("1001", "#AAA", conn=conn)


def test_compose_ask_names_the_member_and_invites_opt_out():
    copy = outreach.compose_ask("Alpha")
    assert "Alpha" in copy
    assert "email" in copy.lower()
    assert "no thanks" in copy.lower()  # opt-out is always offered


def test_propose_is_dormant_unless_enabled(tmp_path, monkeypatch):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        monkeypatch.delenv("ELIXIR_DM_OUTREACH", raising=False)
        raised = []
        out = outreach.propose_cards(
            raise_card=lambda t, c: raised.append(t) or {"action_id": 1}, conn=conn
        )
        assert out == [] and raised == []  # gated off by default
    finally:
        conn.close()


def test_propose_raises_cards_and_marks_proposed(tmp_path, monkeypatch):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        monkeypatch.setenv("ELIXIR_DM_OUTREACH", "1")
        calls = []

        def raise_card(target, copy):
            calls.append((target["player_tag"], copy))
            return {"action_id": 77, "action_type": "member_outreach"}

        out = outreach.propose_cards(raise_card=raise_card, conn=conn)
        assert len(out) == 1
        assert calls and calls[0][0] == "#AAA" and "Alpha" in calls[0][1]
        row = mo.get_outreach("#AAA", conn=conn)
        assert row["status"] == "proposed"
        assert row["leader_action_id"] == 77
        assert row["discord_user_id"] == "1001"
        # A proposed (in-flight) member is not re-targeted next run.
        out2 = outreach.propose_cards(raise_card=raise_card, conn=conn)
        assert out2 == []
    finally:
        conn.close()


def _card(tag, copy="Hey Alpha, share your email?"):
    return {
        "action_type": "member_outreach",
        "target_player_tag": tag,
        "target_discord_user_id": "1001",
        "copy_current_text": copy,
    }


def test_on_decision_approve_sends_dm_and_awaits_reply(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        mo.upsert_outreach("#AAA", status="proposed", discord_user_id="1001", conn=conn)
        sent = []
        row = outreach.on_decision(
            _card("#AAA"),
            "done",
            send_dm=lambda uid, copy: sent.append((uid, copy)) or (True, "sent"),
            conn=conn,
        )
        assert sent == [("1001", "Hey Alpha, share your email?")]
        assert row["status"] == "awaiting_reply"
        assert row["attempts"] == 1
        assert row["next_eligible_at"]  # cooldown stamped
    finally:
        conn.close()


def test_on_decision_decline_marks_skipped_without_sending(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        mo.upsert_outreach("#AAA", status="proposed", discord_user_id="1001", conn=conn)
        sent = []
        row = outreach.on_decision(
            _card("#AAA"),
            "rejected",
            send_dm=lambda uid, copy: sent.append(1) or (True, "sent"),
            conn=conn,
        )
        assert sent == []  # decline never messages the member
        assert row["status"] == "skipped"
    finally:
        conn.close()


def test_on_decision_send_failure_marks_failed(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        mo.upsert_outreach("#AAA", status="proposed", discord_user_id="1001", conn=conn)
        row = outreach.on_decision(
            _card("#AAA"),
            "done",
            send_dm=lambda uid, copy: (False, "user has DMs closed"),
            conn=conn,
        )
        assert row["status"] == "failed"
        assert "DMs closed" in row["last_error"]
    finally:
        conn.close()


def test_on_decision_ignores_other_action_types(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        result = outreach.on_decision(
            {"action_type": "in_game_relay", "target_player_tag": "#AAA"},
            "done",
            send_dm=lambda uid, copy: (True, "sent"),
            conn=conn,
        )
        assert result is None
    finally:
        conn.close()
