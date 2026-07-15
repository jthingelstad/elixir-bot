"""The weekly recap is also emailed (BCC) to clan members with a verified email."""

from __future__ import annotations

import asyncio

import agent.mail.outbound as outbound
import db
from runtime.jobs import _core


def test_email_weekly_recap_bcc_and_strips_discord_emoji(monkeypatch):
    sent = {}
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(outbound, "send", lambda **kw: sent.update(kw) or {})
    monkeypatch.setattr(
        db, "list_member_emails", lambda: [{"email": "a@b.com"}, {"email": "c@d.com"}]
    )
    n = asyncio.run(
        _core._email_weekly_recap(
            "Weekly recap: fame up <:elixir_trophy:1481449222976045086> and <a:fire:99> streak"
        )
    )
    assert n == 2
    assert sent["bcc"] == ["a@b.com", "c@d.com"]
    assert sent["to"]  # a To: is set (the sender)
    assert (
        "<:" not in sent["body"] and "<a:" not in sent["body"]
    )  # custom emoji stripped
    assert "Weekly Clan Recap" in sent["subject"]


def test_email_weekly_recap_no_recipients(monkeypatch):
    called = []
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(outbound, "send", lambda **kw: called.append(kw))
    monkeypatch.setattr(db, "list_member_emails", lambda: [])
    assert asyncio.run(_core._email_weekly_recap("x")) == 0
    assert not called


def test_email_weekly_recap_mail_disabled(monkeypatch):
    monkeypatch.setattr(outbound, "enabled", lambda: False)
    assert asyncio.run(_core._email_weekly_recap("x")) == 0
