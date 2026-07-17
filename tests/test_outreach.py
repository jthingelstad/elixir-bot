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


# -- Phase 2: DM-receive state machine -------------------------------------


def test_classify_reply():
    assert outreach.classify_reply("no thanks")[0] == "opt_out"
    assert outreach.classify_reply("STOP")[0] == "opt_out"
    assert outreach.classify_reply("sure, it's me@example.com!") == (
        "email",
        "me@example.com",
    )
    assert outreach.classify_reply("123456") == ("code", "123456")
    assert outreach.classify_reply("the code is 004521 I think") == ("code", "004521")
    assert outreach.classify_reply("hey what's this about")[0] == "other"
    # An address containing "no" is never mistaken for an opt-out.
    assert outreach.classify_reply("no-reply@x.com")[0] == "email"


def _fake_verify():
    started, checked = [], []

    def start(tag, email):
        started.append((tag, email))
        return {"ok": True, "email": email}

    def check(tag, code):
        checked.append((tag, code))
        return (
            {"ok": True, "email": "me@example.com"}
            if code == "123456"
            else {
                "ok": False,
                "error": "that code didn't match. 4 attempt(s) left.",
            }
        )

    return start, check, started, checked


def test_dm_reply_ignored_when_not_mid_flow(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)  # no outreach row -> not mid-flow
        start, check, *_ = _fake_verify()
        out = outreach.handle_dm_reply(
            "#AAA",
            "me@example.com",
            start_verification=start,
            check_code=check,
            conn=conn,
        )
        assert out is None
    finally:
        conn.close()


def test_dm_email_triggers_verification_and_moves_to_verifying(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        mo.upsert_outreach("#AAA", status="awaiting_reply", conn=conn)
        start, check, started, _ = _fake_verify()
        out = outreach.handle_dm_reply(
            "#AAA",
            "ok it's me@example.com",
            start_verification=start,
            check_code=check,
            conn=conn,
        )
        assert started == [("#AAA", "me@example.com")]
        assert "code" in out.lower()
        row = mo.get_outreach("#AAA", conn=conn)
        assert row["status"] == "verifying"
        assert row["pending_email"] == "me@example.com"
    finally:
        conn.close()


def test_dm_correct_code_verifies_and_fulfills(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        mo.upsert_outreach("#AAA", status="verifying", conn=conn)
        start, check, _, checked = _fake_verify()
        out = outreach.handle_dm_reply(
            "#AAA", "123456", start_verification=start, check_code=check, conn=conn
        )
        assert checked == [("#AAA", "123456")]
        assert "verified" in out.lower()
        assert mo.get_outreach("#AAA", conn=conn)["status"] == "fulfilled"
    finally:
        conn.close()


def test_dm_wrong_code_stays_verifying_with_error(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        mo.upsert_outreach("#AAA", status="verifying", conn=conn)
        start, check, *_ = _fake_verify()
        out = outreach.handle_dm_reply(
            "#AAA", "000000", start_verification=start, check_code=check, conn=conn
        )
        assert "didn't match" in out
        assert mo.get_outreach("#AAA", conn=conn)["status"] == "verifying"
    finally:
        conn.close()


def test_dm_opt_out_marks_skipped_and_confirms(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        _seed(conn)
        mo.upsert_outreach("#AAA", status="awaiting_reply", conn=conn)
        start, check, started, _ = _fake_verify()
        out = outreach.handle_dm_reply(
            "#AAA", "no thanks", start_verification=start, check_code=check, conn=conn
        )
        assert started == []  # never starts verification on an opt-out
        assert "won't ask again" in out.lower()
        assert mo.get_outreach("#AAA", conn=conn)["status"] == "opted_out"
    finally:
        conn.close()
