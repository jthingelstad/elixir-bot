"""Phase 5 — dossiers and carried intentions.

Two capabilities that both write model-authored text about real people into a
database and then feed it back to a model. The tests worth having are the ones
about restraint: what is refused, what is capped, and what never gets invented.
"""

from __future__ import annotations

import pytest

from runtime.jobs import _reflection
from storage import dossiers


@pytest.fixture
def member(engine_conn):
    engine_conn.execute(
        "INSERT OR REPLACE INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        ("#AAA", "canavar", "2026-01-01T00:00:00Z", "2026-08-19T00:00:00Z"),
    )
    engine_conn.commit()
    return "#AAA"


# --- dossiers -----------------------------------------------------------------


def test_a_dossier_round_trips(engine_conn, member):
    assert dossiers.upsert_dossier(
        member, "Phone broke, plans to be back.", updated_by="t", conn=engine_conn
    )
    assert dossiers.dossiers_for([member], conn=engine_conn) == {
        member: "Phone broke, plans to be back."
    }


def test_a_dossier_replaces_rather_than_accumulates(engine_conn, member):
    """One row per member. Append-only notes about a person become a file, and
    a file is what nobody wants written about them."""
    dossiers.upsert_dossier(member, "First note.", updated_by="t", conn=engine_conn)
    dossiers.upsert_dossier(
        member, "Carried forward, plus today.", updated_by="t", conn=engine_conn
    )
    assert dossiers.dossiers_for([member], conn=engine_conn)[member] == (
        "Carried forward, plus today."
    )


def test_a_dossier_is_capped_on_the_way_in(engine_conn, member):
    """The writer is a model and 'keep it short' is not enforcement."""
    dossiers.upsert_dossier(member, "x" * 9000, updated_by="t", conn=engine_conn)
    stored = dossiers.dossiers_for([member], conn=engine_conn)[member]
    assert len(stored) == dossiers.DOSSIER_MAX_CHARS


def test_a_dossier_for_an_unknown_member_is_dropped_not_raised(engine_conn):
    """A tag the roster has never seen is a hallucinated player or a typo, and
    the FK would take the whole nightly job down with it."""
    written = _reflection._persist_dossiers(
        engine_conn, [{"member_tag": "#NOPE", "body": "invented person"}]
    )
    assert written == 0


def test_dossier_injection_is_flagged_off_by_default(monkeypatch):
    """Capturing a dossier and letting it shape a member-facing post are
    different risks; the second is the one a member would notice."""
    from agent import chassis

    monkeypatch.delenv("ELIXIR_DOSSIERS", raising=False)
    assert chassis._dossiers(("#AAA",)) == {}


def test_a_turn_with_no_members_asks_for_no_dossiers(monkeypatch):
    from agent import chassis

    monkeypatch.setenv("ELIXIR_DOSSIERS", "1")
    assert chassis._dossiers(()) == {}


# --- carried intentions -------------------------------------------------------


def test_a_followup_is_scheduled_and_comes_due(engine_conn, member):
    fid = dossiers.schedule_followup(
        due_at="2026-08-20T12:00:00Z",
        why="ask how the new phone worked out",
        player_tag=member,
        created_by="agent",
        conn=engine_conn,
    )
    assert fid
    assert dossiers.due_followups(now="2026-08-19T00:00:00Z", conn=engine_conn) == []
    due = dossiers.due_followups(now="2026-08-21T00:00:00Z", conn=engine_conn)
    assert [d["followup_id"] for d in due] == [fid]


def test_a_fired_followup_does_not_come_due_twice(engine_conn, member):
    """`fired` means EMITTED. The event carries the retry guarantees from there;
    two independent retry mechanisms is how a check-in becomes harassment."""
    fid = dossiers.schedule_followup(
        due_at="2026-08-01T00:00:00Z", why="check in", created_by="agent", conn=engine_conn
    )
    dossiers.mark_followup_fired(fid, conn=engine_conn)
    assert dossiers.due_followups(now="2026-08-21T00:00:00Z", conn=engine_conn) == []


def test_a_followup_needs_both_a_time_and_a_reason(engine_conn):
    assert (
        dossiers.schedule_followup(due_at="", why="something", created_by="a", conn=engine_conn)
        is None
    )
    assert (
        dossiers.schedule_followup(
            due_at="2026-09-01T00:00:00Z", why="  ", created_by="a", conn=engine_conn
        )
        is None
    )


def test_the_tick_emits_a_due_followup_as_an_ordinary_event(engine_conn, member):
    """It travels the standard wake path rather than growing a second scheduler."""
    from engine.tick import _emit_due_followups

    fid = dossiers.schedule_followup(
        due_at="2026-08-01T00:00:00Z",
        why="ask how the phone is",
        player_tag=member,
        created_by="agent",
        conn=engine_conn,
    )
    assert _emit_due_followups(engine_conn, "2026-08-19T00:00:00Z") == 1

    row = engine_conn.execute(
        "SELECT event_type, dedup_key, subject_tag, payload_json FROM clan_events "
        "WHERE event_type = 'followup_due'"
    ).fetchone()
    assert row["dedup_key"] == f"followup_due:{fid}"
    assert row["subject_tag"] == member
    assert "phone" in row["payload_json"]

    # Emitted once, ever: the row is fired and the dedup key would refuse a second.
    assert _emit_due_followups(engine_conn, "2026-08-19T01:00:00Z") == 0


def test_the_followup_event_is_not_a_hard_post():
    """A check-in is a kindness, not an obligation. A floor here would block the
    cursor until someone is asked how their phone is."""
    from engine.event_contracts import EVENT_CONTRACTS

    contract = EVENT_CONTRACTS["followup_due"]
    assert contract.hard_post is False
    assert contract.wake == "immediate"


def test_the_followup_job_claims_the_event():
    from runtime.awareness import respond

    assert respond.JOB_BY_EVENT_TYPE["followup_due"] == "followup"
    assert respond.job_for([{"event_type": "followup_due", "signal_key": "followup_due:1"}]) == (
        "followup"
    )


def test_schedule_followup_reaches_both_write_audiences():
    """The documented trap: a tool in one write set is invisible to the other,
    and a shipped tool was once offered to a model zero times this way."""
    from agent.tool_exec import TOOL_EXECUTOR_NAMES
    from agent.workflow_registry import _WRITE_TOOL_NAMES, AWARENESS_WRITE_TOOL_NAMES

    assert "schedule_followup" in _WRITE_TOOL_NAMES
    assert "schedule_followup" in AWARENESS_WRITE_TOOL_NAMES
    assert "schedule_followup" in TOOL_EXECUTOR_NAMES
