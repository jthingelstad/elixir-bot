"""Mass emails must not re-broadcast on a re-run.

Three of the four deliverables had no guard at all. This is not hypothetical:
on 2026-08-03 the weekly recap was manually re-triggered twice while debugging
a missing report, and each run mailed every member again.

The dedup deliberately records AFTER the send. Recording first would mean a
failed send is remembered as sent and the deliverable silently never goes out —
which is the failure this whole area spent the day fixing. Write-after-send
risks one visible duplicate; write-before risks a silent omission.
"""

from __future__ import annotations

import asyncio

import agent.mail.outbound as outbound
import db
from runtime import email_dedup
from runtime.jobs import _core


def _fake_store(monkeypatch):
    """In-memory stand-in for the contextual-memory rows the dedup uses."""
    seen: set[tuple[str, str]] = set()
    monkeypatch.setattr(email_dedup, "already_sent", lambda kind, key: (kind, key) in seen)
    monkeypatch.setattr(
        email_dedup,
        "record_sent",
        lambda kind, key, **kw: bool(seen.add((kind, key))) or True,
    )
    return seen


def test_weekly_recap_does_not_resend_in_the_same_week(monkeypatch):
    sends = []
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(outbound, "send", lambda **kw: sends.append(kw) or {})
    monkeypatch.setattr(db, "list_member_emails", lambda: [{"email": "a@b.com"}])
    _fake_store(monkeypatch)

    first = asyncio.run(_core._email_weekly_recap("**Week.** Body."))
    second = asyncio.run(_core._email_weekly_recap("**Week.** Body."))

    assert first == 1, "first run sends"
    assert second == 0, "second run in the same week must not re-broadcast"
    assert len(sends) == 1


def test_a_failed_send_does_not_mark_the_week_as_sent(monkeypatch):
    """Fail-then-retry must still deliver — the whole point of write-after-send."""
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(db, "list_member_emails", lambda: [{"email": "a@b.com"}])
    _fake_store(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("JMAP down")

    monkeypatch.setattr(outbound, "send", _boom)
    try:
        asyncio.run(_core._email_weekly_recap("**Week.** Body."))
    except RuntimeError:
        pass

    sends = []
    monkeypatch.setattr(outbound, "send", lambda **kw: sends.append(kw) or {})
    assert asyncio.run(_core._email_weekly_recap("**Week.** Body.")) == 1, (
        "a send that failed must not be remembered as sent"
    )
    assert len(sends) == 1


def test_dedup_fails_open_when_the_store_is_unreadable(monkeypatch):
    """A broken memory store must not silently suppress the deliverable."""

    def _explode(*a, **k):
        raise RuntimeError("memory store down")

    monkeypatch.setattr("storage.contextual_memory.list_memories", _explode)
    assert email_dedup.already_sent("weekly_recap", "2026-W32") is False


def test_record_sent_reports_failure_rather_than_raising(monkeypatch):
    def _explode(**k):
        raise RuntimeError("write failed")

    monkeypatch.setattr("storage.contextual_memory.upsert_summary_memory", _explode)
    assert email_dedup.record_sent("weekly_recap", "2026-W32") is False
