"""Arena Dispatch — the personalized weekly member report and its Monday job.

The render layer is deterministic (scorecard + full battle table); the narrative
is stubbed here so no live LLM call happens. The job test asserts INDIVIDUAL
sends (one To: per member, never BCC) and per-member failure isolation.
"""

from __future__ import annotations

import asyncio

import agent.mail.outbound as outbound
import db
from runtime import member_report
from runtime.jobs import _core

_DEFAULT_LOG = [
    {
        "battle_time": "20260707T144643.000Z",
        "game_mode_name": "Ladder",
        "mode_group": "ladder",
        "outcome": "W",
        "crowns_for": 3,
        "crowns_against": 1,
        "trophy_change": 30,
        "deck_json": '[{"id":1,"name":"Hog Rider","level":14},{"id":2,"name":"Musketeer","level":14}]',
    },
    {
        "battle_time": "20260707T120000.000Z",
        "game_mode_name": "CW_Battle_1v1",
        "mode_group": "war",
        "outcome": "L",
        "crowns_for": 0,
        "crowns_against": 2,
        "trophy_change": 0,
        "deck_json": '[{"id":3,"name":"Giant","level":13}]',
    },
]

_DEFAULT_PROGRESS = [
    {"emoji": "🏆", "text": "New peak: 12,400 (+214 this week vs +40 last)"},
    {"emoji": "🆕", "text": "Unlocked Ronin (legendary)"},
]


def _ctx(name="King Thing", *, log=None, trophies=12400, progress=None):
    """A minimal-but-shaped context dict for the render layer. by_type is derived
    from the log with the real helper so the fixture stays consistent."""
    log = _DEFAULT_LOG if log is None else log
    return {
        "name": name,
        "profile": {
            "trophies": trophies,
            "best_trophies": 13010,
            "arena": "Legendary Arena",
        },
        "battles": {
            "tally": {
                "wins": 8,
                "losses": 3,
                "draws": 0,
                "battles": 11,
                "win_rate": 8 / 11,
                "net_trophies": 214,
            },
            "prior_tally": {"net_trophies": 40},
            "battle_of_week": {
                "outcome": "W",
                "crowns_for": 3,
                "crowns_against": 1,
                "mode": "Ladder",
            },
            "log": log,
            "by_type": member_report._battles_by_type(log),
        },
        "progress": _DEFAULT_PROGRESS if progress is None else progress,
        "game_stream": {"trending_cards": [], "new_cards": []},
    }


def test_render_has_scorecard_and_no_title():
    ctx = _ctx()
    narrative = {
        "overview": "You climbed.",
        "standouts": "Big duel.",
        "meta": "Ronin arrived.",
        "closer": "See you next week. — E",
    }
    subject, body = member_report.render_member_report(ctx, narrative)

    assert subject == "King Thing — your week in the arena 👑"
    assert not body.lstrip().startswith("# ")  # no H1 title
    assert "### 🏆 12,400" in body  # scorecard
    assert "8–3" in body and "11 battles" in body
    assert "| When | Mode | Result | Crowns |" in body  # battle table header
    # narrative blocks are woven in
    assert "You climbed." in body and "Ronin arrived." in body
    assert "See you next week. — E" in body


def test_render_segments_battle_log_by_type():
    ctx = _ctx()
    narrative = {"battle_intros": {"ladder": "Your Hog cycle hummed.", "war": "Rough boat week."}}
    _, body = member_report.render_member_report(ctx, narrative)

    # One section per mode family, labeled and card-aware, each with its own table.
    assert "## Trophy Road (1 battles)" in body
    assert "## River Race (1 battles)" in body
    assert body.count("| When | Mode | Result | Crowns |") == 2  # one table per type
    assert "Your Hog cycle hummed." in body  # per-type intro used
    assert "The full tape" not in body  # the old single table is gone


def test_special_events_split_by_specific_mode():
    # Crazy Arena and Showdown are both special_event but different games — they
    # must land in separate sections, not one lumped "Events".
    log = [
        {
            "battle_time": "20260707T144643.000Z",
            "game_mode_name": "Crazy_Arena",
            "mode_group": "special_event",
            "outcome": "W",
            "crowns_for": 3,
            "crowns_against": 0,
            "trophy_change": 0,
            "deck_json": '[{"id":1,"name":"Ronin","level":11}]',
        },
        {
            "battle_time": "20260707T120000.000Z",
            "game_mode_name": "Showdown",
            "mode_group": "special_event",
            "outcome": "L",
            "crowns_for": 0,
            "crowns_against": 1,
            "trophy_change": 0,
            "deck_json": '[{"id":2,"name":"Knight","level":11}]',
        },
    ]
    by_type = member_report._battles_by_type(log)
    assert set(by_type) == {"Crazy Arena", "Showdown"}
    _, body = member_report.render_member_report(_ctx(log=log), {})
    assert "## Crazy Arena (1 battles)" in body
    assert "## Showdown (1 battles)" in body


def test_render_battle_intro_falls_back_when_absent():
    ctx = _ctx()
    _, body = member_report.render_member_report(ctx, {})  # no battle_intros
    # deterministic per-type intro names the top cards + record
    assert "Hog Rider" in body  # ladder top card in the fallback intro
    assert "## Trophy Road (1 battles)" in body


def test_render_progress_section():
    ctx = _ctx()
    _, body = member_report.render_member_report(ctx, {"progress": "Big week for the vault."})
    assert "**Your progress this week**" in body
    assert "Big week for the vault." in body  # LLM lead-in
    assert "- 🏆 New peak: 12,400" in body  # grounded bullet
    assert "- 🆕 Unlocked Ronin (legendary)" in body


def test_render_degrades_without_narrative_or_battles():
    ctx = _ctx(log=[], progress=[])
    _, body = member_report.render_member_report(ctx, None)
    assert "### 🏆" in body  # scorecard still renders
    assert "\n## " not in body  # empty log → no per-type section headers
    assert "**Your progress this week**" not in body  # no signals → no section
    # falls back to grounded prose, never blank
    assert "8" in body and "3" in body


def test_render_output_is_email_safe_html():
    from agent.mail import email_render

    ctx = _ctx()
    _, body = member_report.render_member_report(ctx, {"overview": "hi"})
    htmldoc = email_render.text_to_html(body)
    assert "<table" in htmldoc  # table extension renders the tape
    assert "<h3" in htmldoc  # scorecard is an h3


# --- The Monday job: individual sends + failure isolation ---------------------


def _job_stubs(monkeypatch, sends):
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(outbound, "send", lambda **kw: sends.append(kw) or {})
    monkeypatch.setattr(
        db,
        "list_member_emails",
        lambda: [
            {"player_tag": "#AAA", "member_name": "Ada", "email": "ada@x.com"},
            {"player_tag": "#BBB", "member_name": "Ben", "email": "ben@x.com"},
        ],
    )
    monkeypatch.setattr(
        member_report, "build_member_report_context", lambda tag, name, **kw: _ctx(name)
    )
    monkeypatch.setattr(member_report, "facts_for_model", lambda ctx: "FACTS")


def test_weekly_member_report_sends_individually(monkeypatch):
    sends: list[dict] = []
    _job_stubs(monkeypatch, sends)
    monkeypatch.setattr(
        "agent.workflows.generate_member_report",
        lambda facts: {"overview": "o", "closer": "c"},
    )

    result = asyncio.run(_core._weekly_member_report_cycle())

    assert result == {"sent": 2, "total": 2}
    assert [s["to"] for s in sends] == ["ada@x.com", "ben@x.com"]  # one To: each
    assert all("bcc" not in s for s in sends)  # never BCC
    assert all(s["subject"].endswith("your week in the arena 👑") for s in sends)


def test_weekly_member_report_isolates_one_failure(monkeypatch):
    sends: list[dict] = []
    _job_stubs(monkeypatch, sends)

    # Make the failure land on the FIRST member deterministically.
    calls = {"n": 0}

    def _gen(facts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LLM hiccup")
        return {"overview": "o", "closer": "c"}

    monkeypatch.setattr("agent.workflows.generate_member_report", _gen)

    result = asyncio.run(_core._weekly_member_report_cycle())

    assert result == {"sent": 1, "total": 2}  # one failed, one sent
    assert [s["to"] for s in sends] == ["ben@x.com"]


def test_weekly_member_report_skips_when_mail_disabled(monkeypatch):
    monkeypatch.setattr(outbound, "enabled", lambda: False)
    result = asyncio.run(_core._weekly_member_report_cycle())
    assert result == {"sent": 0, "total": 0}


def test_weekly_member_report_no_recipients(monkeypatch):
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(db, "list_member_emails", lambda: [])
    result = asyncio.run(_core._weekly_member_report_cycle())
    assert result == {"sent": 0, "total": 0}
