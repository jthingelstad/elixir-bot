"""The Clan Wars Intel email — previously untested end to end.

It was the most delicate email deliverable in the repo and the only one with
zero coverage: season detection, a lookback window, memory-backed idempotency,
and a write-AFTER-send ordering whose own source comment records that the
duplicate window fired live on 2026-08-03.

These pin the decisions, not the prose.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import agent.mail.outbound as outbound
from runtime.jobs import _intel


def _fresh_conn_factory(conn):
    """The job opens its connection inside asyncio.to_thread, and a sqlite3
    connection cannot cross threads — hand out a new one per call."""
    import sqlite3

    path = conn.execute("PRAGMA database_list").fetchall()[0][2]

    def _factory(*a, **k):
        fresh = sqlite3.connect(path)
        fresh.row_factory = sqlite3.Row
        return fresh

    return _factory


def _season_row(conn, season_id: int, started_at: str, ended_at=None):
    conn.execute(
        "INSERT OR REPLACE INTO war_seasons (season_id, started_at, ended_at) VALUES (?, ?, ?)",
        (season_id, started_at, ended_at),
    )
    conn.commit()


@pytest.fixture
def statuses(monkeypatch):
    seen = {"success": [], "failure": [], "start": []}
    monkeypatch.setattr(
        _intel.runtime_status, "mark_job_start", lambda job: seen["start"].append(job)
    )
    monkeypatch.setattr(
        _intel.runtime_status,
        "mark_job_success",
        lambda job, detail="": seen["success"].append(detail),
    )
    monkeypatch.setattr(
        _intel.runtime_status,
        "mark_job_failure",
        lambda job, detail="": seen["failure"].append(detail),
    )
    return seen


def test_skips_cleanly_when_mail_is_not_configured(monkeypatch, statuses):
    monkeypatch.setattr(outbound, "enabled", lambda: False)
    asyncio.run(_intel._clan_wars_intel_email())
    assert statuses["success"] == ["skipped: mail not configured"]
    assert not statuses["failure"]


def test_a_season_older_than_the_lookback_is_not_reported(monkeypatch, statuses, engine_conn):
    """The report is 'what to expect this season'; stale is worse than absent."""
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    stale = (datetime.now(timezone.utc) - timedelta(days=_intel.INTEL_LOOKBACK_DAYS + 2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _season_row(engine_conn, 999, stale)
    monkeypatch.setattr("db.get_connection", _fresh_conn_factory(engine_conn))
    sends = []
    monkeypatch.setattr(outbound, "send", lambda **kw: sends.append(kw))
    asyncio.run(_intel._clan_wars_intel_email())
    assert not sends
    assert statuses["success"] == ["no season owed a report"]


def test_a_season_already_reported_is_not_reported_again(monkeypatch, statuses, engine_conn):
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _season_row(engine_conn, 1001, fresh)
    monkeypatch.setattr("db.get_connection", _fresh_conn_factory(engine_conn))
    # The memory row is what "already reported" means.
    monkeypatch.setattr(
        "storage.contextual_memory.list_memories",
        lambda **kw: [{"event_id": "1001"}],
    )
    sends = []
    monkeypatch.setattr(outbound, "send", lambda **kw: sends.append(kw))
    asyncio.run(_intel._clan_wars_intel_email())
    assert not sends, "a season with an existing memory row must not re-send"
    assert statuses["success"] == ["no season owed a report"]


def test_the_idempotency_check_uses_the_same_api_that_writes_it():
    """Guards the trap named in the source: hand-rolling the memory key here
    would make the check quietly never match, and the report would re-send
    every day for a week."""
    import inspect

    source = inspect.getsource(_intel._clan_wars_intel_email)
    assert "list_memories" in source
    assert '"event_type": "clan_wars_intel"' in source
    assert "event_id" in source


def test_send_precedes_the_memory_write():
    """Write-after-send is deliberate and must stay that way.

    Recording first would mean a failed send is remembered as sent and the
    season's report silently never goes out. After-send risks one visible
    duplicate instead, which is the trade this job documents.
    """
    import inspect

    source = inspect.getsource(_intel._clan_wars_intel_email)
    assert source.index("outbound.send") < source.index("upsert_intel_report_memory"), (
        "the memory write must follow the send, never precede it"
    )
