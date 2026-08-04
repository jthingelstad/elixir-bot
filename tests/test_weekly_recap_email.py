"""The weekly recap is also emailed (BCC) to clan members with a verified email."""

from __future__ import annotations

import asyncio
import inspect

import pytest

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
    assert "<:" not in sent["body"] and "<a:" not in sent["body"]  # custom emoji stripped
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


def test_email_body_is_email_markdown_not_a_discord_post(monkeypatch):
    """The body must carry real headings for a stylesheet to hook.

    The email used to be the Discord post with its emoji filed off: a bold
    pseudo-title, no h1, no h2, one undifferentiated wall. Nothing for a design
    layer to style.
    """
    sent = {}
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(outbound, "send", lambda **kw: sent.update(kw) or {})
    monkeypatch.setattr(db, "list_member_emails", lambda: [{"email": "a@b.com"}])
    recap = (
        "**Season 134 closed perfect.** Four weeks, four first-place finishes.\n\n"
        "The Ranked season closed the same day, and it was ours too.\n\n"
        "**The ladder was loud.** AHMO climbed +587 to 11,862."
    )
    asyncio.run(_core._email_weekly_recap(recap))
    body = sent["body"]

    assert body.startswith("# Weekly Recap"), body[:80]
    # Each bold lead becomes its own section heading.
    assert "## Season 134 closed perfect" in body
    assert "## The ladder was loud" in body
    # A paragraph with no bold lead stays a paragraph, not a heading.
    assert "## The Ranked season" not in body
    assert "The Ranked season closed the same day" in body
    # The brain's words survive intact — this re-marks structure, it does not rewrite.
    assert "AHMO climbed +587 to 11,862." in body


def test_email_strips_discord_shortcode_emoji(monkeypatch):
    sent = {}
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(outbound, "send", lambda **kw: sent.update(kw) or {})
    monkeypatch.setattr(db, "list_member_emails", lambda: [{"email": "a@b.com"}])
    asyncio.run(_core._email_weekly_recap("**Big week.** We won :crossed_swords: again"))
    assert ":crossed_swords:" not in sent["body"]


def test_send_errors_reach_the_caller(monkeypatch):
    """The helper must not swallow — the caller decides what a failure means."""
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(db, "list_member_emails", lambda: [{"email": "a@b.com"}])

    def _boom(**kw):
        raise RuntimeError("JMAP down")

    monkeypatch.setattr(outbound, "send", _boom)
    with pytest.raises(RuntimeError, match="JMAP down"):
        asyncio.run(_core._email_weekly_recap("**Big week.** We won."))


def test_the_job_does_not_report_success_when_the_email_failed():
    """The 2026-08-03 failure mode: report missing, job green.

    A structural check, not a behavioural one — driving the whole job needs a
    live Discord channel. It asserts the shape that matters: the success call is
    reached only when no email error was recorded, and the failure path says so
    in words a human will understand in #elixir-log.
    """
    source = inspect.getsource(_core._weekly_clan_recap)
    assert "email_error" in source, "the email outcome must be captured, not swallowed"
    failure_at = source.index("mark_job_failure")
    success_at = source.index('mark_job_success("weekly_clan_recap"')
    assert failure_at < success_at, (
        "the email-failure branch must be checked BEFORE the unconditional success call"
    )
    assert "did NOT go out" in source, "the alert should name what actually failed"


def test_email_is_its_own_composition_not_the_discord_post(monkeypatch):
    """Decoupled (Jamie, 2026-08-03): email composes independently.

    Discord gets the short punchy post; email gets the expansive edition with
    headings and tables. Reformatting one into the other gave the worst of both.
    """
    sent = {}
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(outbound, "send", lambda **kw: sent.update(kw) or {})
    monkeypatch.setattr(db, "list_member_emails", lambda: [{"email": "a@b.com"}])
    monkeypatch.setattr("runtime.awareness.read.build_read", lambda *a, **k: {"clan": "POAP KINGS"})
    seen = {}

    def _compose(read, week_context, discord_recap="", **kw):
        seen["week_context"] = week_context
        seen["discord_recap"] = discord_recap
        return (
            "# Weekly Clan Report\n\n## War\n\n| Clan | Fame |\n|---|---|\n| POAP KINGS | 51,900 |"
        )

    monkeypatch.setattr(_core.elixir_agent, "generate_weekly_recap_email", _compose)

    asyncio.run(_core._email_weekly_recap("**Short discord post.** Punchy.", "WEEK FACTS"))

    # It composed from the week facts, and saw the Discord post only as context.
    assert seen["week_context"] == "WEEK FACTS"
    assert "Short discord post" in seen["discord_recap"]
    # The email body is the composer's output, not the Discord post.
    assert sent["body"].startswith("# Weekly Clan Report")
    assert "| Clan | Fame |" in sent["body"], "email formatting (tables) survives to the body"
    assert "Punchy." not in sent["body"]


def test_email_falls_back_to_the_discord_recap_when_composition_fails(monkeypatch):
    """A plainer email still beats a missing one."""
    sent = {}
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(outbound, "send", lambda **kw: sent.update(kw) or {})
    monkeypatch.setattr(db, "list_member_emails", lambda: [{"email": "a@b.com"}])
    monkeypatch.setattr("runtime.awareness.read.build_read", lambda *a, **k: {})
    monkeypatch.setattr(_core.elixir_agent, "generate_weekly_recap_email", lambda *a, **k: None)

    n = asyncio.run(_core._email_weekly_recap("**Season closed.** We won.", "WEEK FACTS"))
    assert n == 1
    assert sent["body"].startswith("# Weekly Recap")
    assert "## Season closed" in sent["body"]


def test_no_email_context_means_the_reformat_path(monkeypatch):
    """Callers that pass no context (previews, older paths) still work."""
    sent = {}
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(outbound, "send", lambda **kw: sent.update(kw) or {})
    monkeypatch.setattr(db, "list_member_emails", lambda: [{"email": "a@b.com"}])
    asyncio.run(_core._email_weekly_recap("**Big week.** We won."))
    assert sent["body"].startswith("# Weekly Recap")
