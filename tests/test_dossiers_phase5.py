"""Phase 5 — dossiers and carried intentions.

Two capabilities that both write model-authored text about real people into a
database and then feed it back to a model. The tests worth having are the ones
about restraint: what is refused, what is capped, and what never gets invented.
"""

from __future__ import annotations

import json

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


def test_active_focus_is_shared_without_replacing_member_authored_context(engine_conn, member):
    dossiers.upsert_dossier(
        member,
        "Phone broke, plans to be back.",
        updated_by="reflection",
        source_intent_key="message:12",
        conn=engine_conn,
    )
    body_updated_at = dossiers.dossier_for(member, conn=engine_conn)["updated_at"]

    assert dossiers.set_active_focus(
        member,
        "Turn one close ladder loss into a win.",
        source="weekly_member_report",
        period="2026-W35",
        conn=engine_conn,
    )

    stored = dossiers.dossier_for(member, conn=engine_conn)
    assert stored["body"] == "Phone broke, plans to be back."
    assert stored["updated_by"] == "reflection"
    assert stored["source_intent_key"] == "message:12"
    assert stored["updated_at"] == body_updated_at
    assert stored["active_focus"] == "Turn one close ladder loss into a win."
    assert stored["active_focus_source"] == "weekly_member_report"
    assert stored["active_focus_period"] == "2026-W35"
    prompt_context = dossiers.dossiers_for([member], conn=engine_conn)[member]
    assert prompt_context.startswith("Active focus (weekly_member_report, 2026-W35)")
    assert prompt_context.endswith("Phone broke, plans to be back.")


def test_active_focus_can_start_a_dossier_without_manufacturing_a_body(engine_conn, member):
    assert dossiers.set_active_focus(
        member,
        "Play one more war set with the same deck order.",
        source="weekly_member_report",
        period="2026-W35",
        conn=engine_conn,
    )

    stored = dossiers.dossier_for(member, conn=engine_conn)
    assert stored["body"] == ""
    assert stored["active_focus"] == "Play one more war set with the same deck order."
    assert dossiers.dossiers_for([member], conn=engine_conn) == {
        member: (
            "Active focus (weekly_member_report, 2026-W35): "
            "Play one more war set with the same deck order."
        )
    }


def test_active_focus_is_capped_on_the_way_in(engine_conn, member):
    dossiers.set_active_focus(
        member,
        "x" * 9000,
        source="weekly_member_report",
        conn=engine_conn,
    )

    assert len(dossiers.dossier_for(member, conn=engine_conn)["active_focus"]) == (
        dossiers.ACTIVE_FOCUS_MAX_CHARS
    )


def test_a_dossier_for_an_unknown_member_is_dropped_not_raised(engine_conn):
    """A tag the roster has never seen is a hallucinated player or a typo, and
    the FK would take the whole nightly job down with it."""
    written = _reflection._persist_dossiers(
        engine_conn,
        [{"member_tag": "#NOPE", "body": "invented person", "evidence_refs": ["message:1"]}],
        evidence_members={"message:1": "#NOPE"},
    )
    assert written == 0


def test_dossier_injection_can_be_disabled(monkeypatch):
    from agent import chassis

    monkeypatch.setenv("ELIXIR_DOSSIERS", "0")
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


def test_phase5_conversation_to_dossier_followup_silence_and_cursor(
    engine_conn, member, monkeypatch
):
    """The complete successful-silence slice across the real stores.

    A linked Ask Elixir message becomes referential dossier evidence; a carried
    intention becomes a due event; the responder explicitly consumes it in
    silence; only then does the wake cursor advance.
    """
    from agent import chassis
    from engine.tick import _emit_due_followups
    from runtime.awareness import respond, wake
    from storage.messages import save_message

    message_id = save_message(
        "discord:ask-elixir:phase5",
        "user",
        "My phone broke, but I plan to be back next week.",
        member_tag=member,
        workflow="interactive",
        conn=engine_conn,
    )
    context = _reflection.build_reflection_context(engine_conn, hours=24)
    evidence_ref = f"message:{message_id}"
    conversation = next(
        item for item in context["member_conversations"] if item["evidence_ref"] == evidence_ref
    )
    assert conversation["member_tag"] == member
    assert {item["ref"] for item in context["evidence_index"]} >= {evidence_ref}

    assert (
        _reflection._persist_dossiers(
            engine_conn,
            [
                {
                    "member_tag": member,
                    "body": "Phone broke; plans to be back next week.",
                    "evidence_refs": [evidence_ref],
                }
            ],
            evidence_members={evidence_ref: member},
        )
        == 1
    )
    source = engine_conn.execute(
        "SELECT source_intent_key FROM member_dossiers WHERE player_tag = ?", (member,)
    ).fetchone()
    assert source["source_intent_key"] == evidence_ref

    followup_id = dossiers.schedule_followup(
        due_at="2026-08-20T12:00:00Z",
        why="ask how the new phone worked out",
        player_tag=member,
        created_by="interactive",
        conn=engine_conn,
    )
    assert _emit_due_followups(engine_conn, "2026-08-21T00:00:00Z") == 1
    event = engine_conn.execute(
        "SELECT event_id, event_type, subject_tag, observed_at, dedup_key, payload_json "
        "FROM clan_events WHERE dedup_key = ?",
        (f"followup_due:{followup_id}",),
    ).fetchone()
    fired = {
        "wake_class": "immediate",
        "wake_model": "lightweight",
        "reason": "followup due",
        "events": [
            {
                "event_id": event["event_id"],
                "signal_key": event["dedup_key"],
                "event_type": event["event_type"],
                "subject_tag": event["subject_tag"],
                "observed_at": event["observed_at"],
                "payload": json.loads(event["payload_json"]),
            }
        ],
    }
    monkeypatch.setattr(
        chassis,
        "run_turn",
        lambda attention, seed, **kwargs: {
            "job": "followup",
            "workflow": attention.workflow,
            "posts": [],
            "rejections": [],
            "intentionally_silent": True,
            "silence_reason": "The member is already back and the question answered itself.",
        },
    )
    outcome = respond.respond(
        fired,
        deliver_fn=lambda *_: (_ for _ in ()).throw(AssertionError("silence must not deliver")),
        conn=engine_conn,
    )
    assert outcome["consumed"] is True and outcome["intentionally_silent"] is True

    consumer_key = "awareness:wake:live:immediate:lightweight:followup"
    wake.mark_fired(consumer_key, event["event_id"], conn=engine_conn)
    cursor = engine_conn.execute(
        "SELECT cursor_int FROM stream_cursors WHERE consumer_key = ? AND scope_key = ''",
        (consumer_key,),
    ).fetchone()
    assert cursor["cursor_int"] == event["event_id"]
