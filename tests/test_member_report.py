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


def _ctx(name="King Thing", *, log=None, trophies=12400):
    """A minimal-but-shaped context dict for the render layer."""
    return {
        "name": name,
        "profile": {"trophies": trophies, "best_trophies": 13010, "arena": "Legendary Arena"},
        "battles": {
            "tally": {"wins": 8, "losses": 3, "draws": 0, "battles": 11,
                      "win_rate": 8 / 11, "net_trophies": 214},
            "battle_of_week": {"outcome": "victory", "crowns_for": 3,
                               "crowns_against": 1, "mode": "Ladder"},
            "log": log if log is not None else [
                {"battle_time": "20260707T144643.000Z", "game_mode_name": "Ladder",
                 "mode_group": "ladder", "outcome": "victory",
                 "crowns_for": 3, "crowns_against": 1, "trophy_change": 30},
            ],
        },
        "game_stream": {"trending_cards": [], "new_cards": []},
    }


def test_render_has_scorecard_table_and_no_title():
    ctx = _ctx()
    narrative = {"overview": "You climbed.", "standouts": "Big duel.",
                 "meta": "Ronin arrived.", "closer": "See you next week. — E"}
    subject, body = member_report.render_member_report(ctx, narrative)

    assert subject == "King Thing — your week in the arena 👑"
    assert not body.lstrip().startswith("# ")          # no H1 title
    assert "### 🏆 12,400" in body                       # scorecard
    assert "8–3" in body and "11 battles" in body
    assert "## The full tape" in body                   # the one structured block
    assert "| When | Mode | Result | Crowns |" in body  # battle table header
    assert "Ladder" in body
    # narrative blocks are woven in
    assert "You climbed." in body and "Ronin arrived." in body
    assert "See you next week. — E" in body


def test_render_degrades_without_narrative_or_battles():
    ctx = _ctx(log=[])
    subject, body = member_report.render_member_report(ctx, None)
    assert "### 🏆" in body                       # scorecard still renders
    assert "## The full tape" not in body        # empty log → no table
    # falls back to grounded prose, never blank
    assert "8" in body and "3" in body


def test_render_output_is_email_safe_html():
    from agent.mail import email_render
    ctx = _ctx()
    _, body = member_report.render_member_report(ctx, {"overview": "hi"})
    htmldoc = email_render.text_to_html(body)
    assert "<table" in htmldoc                     # table extension renders the tape
    assert "<h3" in htmldoc                         # scorecard is an h3


# --- The Monday job: individual sends + failure isolation ---------------------

def _job_stubs(monkeypatch, sends):
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(outbound, "send", lambda **kw: sends.append(kw) or {})
    monkeypatch.setattr(db, "list_member_emails", lambda: [
        {"player_tag": "#AAA", "member_name": "Ada", "email": "ada@x.com"},
        {"player_tag": "#BBB", "member_name": "Ben", "email": "ben@x.com"},
    ])
    monkeypatch.setattr(member_report, "build_member_report_context",
                        lambda tag, name, **kw: _ctx(name))
    monkeypatch.setattr(member_report, "facts_for_model", lambda ctx: "FACTS")


def test_weekly_member_report_sends_individually(monkeypatch):
    sends: list[dict] = []
    _job_stubs(monkeypatch, sends)
    monkeypatch.setattr("agent.workflows.generate_member_report",
                        lambda facts: {"overview": "o", "closer": "c"})

    result = asyncio.run(_core._weekly_member_report_cycle())

    assert result == {"sent": 2, "total": 2}
    assert [s["to"] for s in sends] == ["ada@x.com", "ben@x.com"]   # one To: each
    assert all("bcc" not in s for s in sends)                       # never BCC
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

    assert result == {"sent": 1, "total": 2}       # one failed, one sent
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
