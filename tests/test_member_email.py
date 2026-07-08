"""Member email: verified-contact identity (storage.identity) + the 6-digit
verification loop (runtime.email_verification)."""
from __future__ import annotations

import re

import pytest

import db
from runtime import email_verification as ev

TAG = "#EMAILME1"


def test_is_valid_email():
    assert db.is_valid_email("player@example.com")
    assert not db.is_valid_email("nope")
    assert not db.is_valid_email("a@b")          # no TLD
    assert not db.is_valid_email("")


def test_admin_set_shows_verified(engine_conn):
    db.set_member_email(TAG, "admin@set.com", source="admin_set",
                        verified_at="2026-07-08T00:00:00Z", conn=engine_conn)
    ident = db.get_member_identity(TAG, conn=engine_conn)
    assert ident["email"] == "admin@set.com"
    assert ident["email_source"] == "admin_set"
    assert ident["email_status"] == "verified"


def test_clear_email(engine_conn):
    db.set_member_email(TAG, "x@y.com", source="admin_set", conn=engine_conn)
    db.clear_member_email(TAG, conn=engine_conn)
    ident = db.get_member_identity(TAG, conn=engine_conn)
    assert ident["email"] == ""
    assert ident["email_status"] == "none"


def test_unverified_status_when_no_verified_at(engine_conn):
    db.set_member_email(TAG, "pending@x.com", source="self_service", conn=engine_conn)
    ident = db.get_member_identity(TAG, conn=engine_conn)
    assert ident["email_status"] == "unverified"


@pytest.fixture
def _stub_mail(monkeypatch):
    monkeypatch.setattr(ev.outbound, "enabled", lambda: True)
    monkeypatch.setattr(ev.outbound, "send", lambda **kw: {"emailId": "X"})
    # deterministic code = 123456
    monkeypatch.setattr(ev.secrets, "randbelow", lambda n: 123456)


def test_verification_happy_path(engine_conn, _stub_mail):
    r = ev.start_verification(TAG, "me@example.com")
    assert r["ok"] and r["email"] == "me@example.com"
    ok = ev.check_code(TAG, "123456")
    assert ok["ok"] and ok["email"] == "me@example.com"
    ident = db.get_member_identity(TAG)
    assert ident["email"] == "me@example.com"
    assert ident["email_status"] == "verified"
    assert ident["email_source"] == "self_service"
    # challenge cleared after success
    assert db.get_email_challenge(TAG) is None


def test_verification_captured_code_matches_email_body(engine_conn, monkeypatch):
    sent = {}
    monkeypatch.setattr(ev.outbound, "enabled", lambda: True)
    monkeypatch.setattr(ev.outbound, "send", lambda **kw: sent.update(kw) or {})
    r = ev.start_verification(TAG, "me@example.com")
    assert r["ok"]
    code = re.search(r"\*\*(\d{6})\*\*", sent["body"]).group(1)
    assert ev.check_code(TAG, code)["ok"]


def test_verification_wrong_code_counts_down(engine_conn, _stub_mail):
    ev.start_verification(TAG, "me@example.com")
    r = ev.check_code(TAG, "999999")
    assert not r["ok"]
    assert "attempt" in r["error"]
    # still pending; email not set
    assert db.get_member_identity(TAG)["email"] == ""


def test_verification_too_many_attempts(engine_conn, _stub_mail):
    ev.start_verification(TAG, "me@example.com")
    for _ in range(ev.MAX_ATTEMPTS):
        ev.check_code(TAG, "999999")
    r = ev.check_code(TAG, "123456")   # correct, but attempts exhausted
    assert not r["ok"]
    assert "too many" in r["error"].lower()


def test_verification_expired(engine_conn, _stub_mail):
    ev.start_verification(TAG, "me@example.com")
    # force the challenge into the past
    db.upsert_email_challenge(TAG, pending_email="me@example.com",
                              code_hash=ev._hash_code("123456", TAG),
                              expires_at="2000-01-01T00:00:00Z")
    r = ev.check_code(TAG, "123456")
    assert not r["ok"] and "expired" in r["error"]


def test_start_rejects_bad_email(engine_conn, _stub_mail):
    r = ev.start_verification(TAG, "not-an-email")
    assert not r["ok"]
    assert db.get_email_challenge(TAG) is None


def test_start_requires_mail_configured(engine_conn, monkeypatch):
    monkeypatch.setattr(ev.outbound, "enabled", lambda: False)
    r = ev.start_verification(TAG, "me@example.com")
    assert not r["ok"]


def _add_membership(conn, tag, *, left_at=None):
    conn.execute("INSERT OR IGNORE INTO clans (clan_tag, first_seen_at, last_seen_at) "
                 "VALUES ('#J2RGCRVG','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO clan_memberships (player_tag, joined_at, join_source, left_at) "
                 "VALUES (?, '2026-01-01T00:00:00Z', 'test', ?)", (tag, left_at))


def test_list_member_emails_only_verified_and_current(engine_conn):
    # current member, verified → included
    db.set_member_email("#CUR1", "cur@x.com", source="self_service",
                        verified_at="2026-07-08T00:00:00Z", conn=engine_conn)
    _add_membership(engine_conn, "#CUR1")
    # current member, unverified → excluded
    db.set_member_email("#UNV1", "unv@x.com", source="self_service", conn=engine_conn)
    _add_membership(engine_conn, "#UNV1")
    # verified but LEFT the clan → excluded
    db.set_member_email("#LEFT1", "left@x.com", source="admin_set",
                        verified_at="2026-07-08T00:00:00Z", conn=engine_conn)
    _add_membership(engine_conn, "#LEFT1", left_at="2026-02-01T00:00:00Z")
    engine_conn.commit()

    emails = {m["email"] for m in db.list_member_emails(conn=engine_conn)}
    assert "cur@x.com" in emails
    assert "unv@x.com" not in emails      # unverified
    assert "left@x.com" not in emails     # no longer in the clan
