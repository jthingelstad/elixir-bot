"""Arena Dispatch — the personalized weekly member report and its Monday job.

The render layer is deterministic (scorecard + full battle table); the narrative
is stubbed here so no live LLM call happens. The job test asserts INDIVIDUAL
sends (one To: per member, never BCC) and per-member failure isolation.
"""

from __future__ import annotations

import asyncio
import re

import pytest

import agent.mail.outbound as outbound
import db
from runtime import email_dedup, member_report
from runtime import status as runtime_status
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
    failures: list[str] = []
    monkeypatch.setattr(
        runtime_status, "mark_job_failure", lambda name, error: failures.append(error)
    )

    result = asyncio.run(_core._weekly_member_report_cycle())

    assert result == {"sent": 1, "total": 2}  # one failed, one sent
    assert [s["to"] for s in sends] == ["ben@x.com"]
    assert len(failures) == 1
    assert "1/2 fulfilled; 1 failed" in failures[0]


def test_catch_up_retries_only_missing_recipients_under_the_owed_key(monkeypatch):
    sends: list[dict] = []
    _job_stubs(monkeypatch, sends)
    seen: set[tuple[str, str]] = set()
    monkeypatch.setattr(email_dedup, "already_sent", lambda kind, key: (kind, key) in seen)
    monkeypatch.setattr(
        email_dedup,
        "record_sent",
        lambda kind, key, **kw: bool(seen.add((kind, key))) or True,
    )

    failed_once = False

    def _gen(facts):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("one recipient failed")
        return {"overview": "o", "closer": "c"}

    monkeypatch.setattr("agent.workflows.generate_member_report", _gen)
    outcomes: list[tuple[str, str]] = []
    monkeypatch.setattr(runtime_status, "mark_job_start", lambda name: None)
    monkeypatch.setattr(
        runtime_status,
        "mark_job_success",
        lambda name, summary=None: outcomes.append(("success", summary)),
    )
    monkeypatch.setattr(
        runtime_status, "mark_job_failure", lambda name, error: outcomes.append(("failure", error))
    )

    with runtime_status.job_period("2026-W32", catch_up=True):
        first = asyncio.run(_core._weekly_member_report_cycle())
    with runtime_status.job_period("2026-W32", catch_up=True):
        second = asyncio.run(_core._weekly_member_report_cycle())

    assert first == {"sent": 1, "total": 2}
    assert second == {"sent": 1, "total": 2}
    assert [outcome for outcome, _ in outcomes] == ["failure", "success"]
    assert [item["to"] for item in sends] == ["ben@x.com", "ada@x.com"]
    assert seen == {
        ("member_report", "#AAA:2026-W32"),
        ("member_report", "#BBB:2026-W32"),
    }


def test_sent_without_a_delivery_receipt_does_not_complete_the_period(monkeypatch):
    sends: list[dict] = []
    _job_stubs(monkeypatch, sends)
    monkeypatch.setattr(
        "agent.workflows.generate_member_report",
        lambda facts: {"overview": "o", "closer": "c"},
    )
    monkeypatch.setattr(email_dedup, "already_sent", lambda kind, key: False)
    calls = 0

    def _record_sent(kind, key, **kwargs):
        nonlocal calls
        calls += 1
        return calls == 2

    monkeypatch.setattr(email_dedup, "record_sent", _record_sent)
    failures: list[str] = []
    monkeypatch.setattr(runtime_status, "mark_job_start", lambda name: None)
    monkeypatch.setattr(
        runtime_status, "mark_job_success", lambda name, summary=None: pytest.fail(summary)
    )
    monkeypatch.setattr(
        runtime_status, "mark_job_failure", lambda name, error: failures.append(error)
    )

    with runtime_status.job_period("2026-W32", catch_up=True):
        result = asyncio.run(_core._weekly_member_report_cycle())

    assert result == {"sent": 1, "total": 2}
    assert len(sends) == 2
    assert failures == ["period 2026-W32: 1/2 fulfilled; 1 failed"]


def test_weekly_member_report_skips_when_mail_disabled(monkeypatch):
    monkeypatch.setattr(outbound, "enabled", lambda: False)
    result = asyncio.run(_core._weekly_member_report_cycle())
    assert result == {"sent": 0, "total": 0}


def test_weekly_member_report_no_recipients(monkeypatch):
    monkeypatch.setattr(outbound, "enabled", lambda: True)
    monkeypatch.setattr(db, "list_member_emails", lambda: [])
    result = asyncio.run(_core._weekly_member_report_cycle())
    assert result == {"sent": 0, "total": 0}


# ── Battle + Deck Intelligence in the dispatch ────────────────────────────────
#
# The report reads both capabilities rather than recomputing them, so what these
# pin is the TRANSLATION: an intelligence answer that refuses to overclaim must
# still refuse to overclaim after the report has phrased it for a member.

_NO_NEMESIS = {
    "available": True,
    "nemeses": [],
    "cards_evaluated": 61,
    "sample_floor": 30,
    "any_losing_matchup": False,
}
_NO_EVIDENCE = {
    "available": True,
    "nemeses": [],
    "cards_evaluated": 0,
    "sample_floor": 30,
    "any_losing_matchup": False,
}
_MAXED = {
    "available": True,
    "upgrades": [],
    "no_material_upgrades": True,
    "incidental_cards_below_max": 38,
}
_SUGGESTION = {
    "archetype": "Hog Cycle",
    "family": "cycle",
    "avg_elixir": 3.0,
    "levels_from_max": 0.5,
    "fielded_by_members": 0,
    "cards": [
        {"name": "Hog Rider", "form": "base", "level": 13, "max_level": 14},
        {"name": "Mini P.E.K.K.A", "form": "Hero", "level": 14, "max_level": 14},
    ],
}


def _intel_ctx(**intel):
    ctx = _ctx()
    ctx["days"] = 7
    ctx["intel"] = {
        "coaching": None,
        "nemesis": None,
        "decks_played": None,
        "upgrades": None,
        "discover": None,
        "war_set": None,
        **intel,
    }
    return ctx


def test_no_intelligence_renders_no_deck_block():
    """Every view can be unavailable — a new member has no battles at all."""
    assert member_report._render_deck(_intel_ctx()) is None
    assert member_report._intel_brief(_intel_ctx()) == []
    # And the email still assembles.
    _, body = member_report.render_member_report(_intel_ctx())
    assert "Your deck" not in body


def test_no_losing_matchup_says_so_instead_of_naming_one():
    """The failure this replaces: a 0-4 across four games read as a nemesis.

    When the lifetime read finds no card the member actually loses to, both the
    brief and the email have to say that — the interesting bug is not a wrong
    card, it is quietly dropping the section so the week's diary becomes the
    only matchup claim in the email.
    """
    ctx = _intel_ctx(nemesis=_NO_NEMESIS)
    brief = "\n".join(member_report._intel_brief(ctx))
    assert "no card they genuinely lose to" in brief.lower() or "NO card they genuinely" in brief
    assert "do NOT promote" in brief
    assert "no card you actually lose to" in member_report._render_deck(ctx)


def test_a_real_losing_matchup_is_named_with_its_sample():
    ctx = _intel_ctx(
        nemesis={
            "available": True,
            "any_losing_matchup": True,
            "nemeses": [
                {"card": "Mega Knight", "n": 44, "member_win_rate": 0.41, "losing_matchup": True}
            ],
        }
    )
    rendered = member_report._render_deck(ctx)
    assert "Mega Knight" in rendered and "41%" in rendered and "44" in rendered
    assert "no card you actually lose to" not in rendered


def test_ranked_but_not_losing_matchups_are_never_called_a_nemesis():
    """nemesis is a RANKING: worst-first still includes cards they beat."""
    ctx = _intel_ctx(
        nemesis={
            "available": True,
            "any_losing_matchup": False,
            "cards_evaluated": 1,
            "sample_floor": 30,
            "nemeses": [
                {"card": "Arrows", "n": 36, "member_win_rate": 0.583, "losing_matchup": False}
            ],
        }
    )
    rendered = member_report._render_deck(ctx)
    assert "Arrows" not in rendered, "a 58% matchup is not a struggle"
    assert "no card you actually lose to" in rendered


def test_maxed_deck_is_good_news_not_a_card_to_chase():
    ctx = _intel_ctx(upgrades=_MAXED)
    brief = "\n".join(member_report._intel_brief(ctx))
    assert "do NOT reach for a card they barely play" in brief
    assert "38 owned cards sit below max" in brief
    assert "at or near max" in member_report._render_deck(ctx)


def test_upgrades_are_ranked_by_what_they_actually_field():
    ctx = _intel_ctx(
        upgrades={
            "available": True,
            "no_material_upgrades": False,
            "upgrades": [
                {
                    "card": "Fireball",
                    "level": 8,
                    "max_level": 14,
                    "levels_from_max": 6,
                    "usage_share": 0.082,
                },
            ],
        }
    )
    rendered = member_report._render_deck(ctx)
    assert "| Fireball | 8 | 6 | 8% |" in rendered, "a level is one number; 16 is a constant"
    assert "how much you actually field the card" in rendered


def test_suggested_decks_never_carry_a_win_rate():
    """Mirrors capabilities' own invariant: clan deck win rates are ~half player
    composition, so a rate on a deck the member has never played is a number
    about somebody else. The report must not reintroduce one while phrasing."""
    ctx = _intel_ctx(
        discover={"available": True, "suggestions": [_SUGGESTION]},
        war_set={
            "available": True,
            "distinct_cards": 32,
            "worst_deck_from_max": 2.5,
            "decks": [dict(_SUGGESTION, archetype=f"Deck {i}") for i in range(4)],
        },
    )
    rendered = member_report._render_deck(ctx)
    brief = "\n".join(member_report._intel_brief(ctx))

    # Every line that describes a deck the member does not already play: no
    # percentage, no record, no win-rate word anywhere on it.
    deck_lines = [
        line
        for line in rendered.splitlines() + brief.splitlines()
        if _SUGGESTION["archetype"] in line or "Deck " in line
    ]
    assert deck_lines, "the fixture must actually produce deck lines to inspect"
    for line in deck_lines:
        assert "%" not in line, line
        assert not re.search(r"\d+\s*[-–]\s*\d+\b", line), f"reads as a record: {line}"
        # "no win rate exists" is the disclaimer, not a claim — everything else is.
        assert "win" not in line.lower().replace("no win rate exists", ""), line
    assert "never state or imply one" in brief
    assert "already own" in rendered


def test_war_set_admits_how_weak_its_worst_deck_is():
    """Four decks sharing no card is a constraint solve — the fourth is built from
    leftovers. Handing a member an underlevelled deck without saying so is how a
    war day gets lost on advice from this email."""
    ctx = _intel_ctx(
        war_set={
            "available": True,
            "distinct_cards": 32,
            "worst_deck_from_max": 2.5,
            "decks": [dict(_SUGGESTION, archetype=f"Deck {i}") for i in range(4)],
        }
    )
    rendered = member_report._render_deck(ctx)
    assert "**2.5** levels from max" in rendered
    assert "no card" in rendered  # "four decks ... no card reused"
    assert "32" in rendered


def test_card_forms_survive_into_the_email():
    """base / Evo / Hero are different cards. Dropping the form makes the advice
    point at a card the member may not own."""
    ctx = _intel_ctx(discover={"available": True, "suggestions": [_SUGGESTION]})
    rendered = member_report._render_deck(ctx)
    assert "**Hero Mini P.E.K.K.A** (lvl 14)" in rendered
    assert "**Hog Rider** (lvl 13)" in rendered, "base form takes no prefix"


def test_too_few_battles_gets_no_matchup_read_rather_than_a_compliment():
    """The trap this closes: an empty nemesis list means "we judged every card and
    you beat them all" for a veteran and "we could not judge a single card" for a
    newcomer. Both arrive as any_losing_matchup=False. Reading it as the former
    tells a member with 40 lifetime battles they have no weaknesses -- praise
    earned by not playing.
    """
    ctx = _intel_ctx(nemesis=_NO_EVIDENCE)
    brief = "\n".join(member_report._intel_brief(ctx))
    assert "NONE AVAILABLE" in brief
    assert "do NOT say they have no weaknesses" in brief
    # And the email says nothing at all rather than guessing in either direction.
    rendered = member_report._render_deck(ctx) or ""
    assert "no card you actually lose to" not in rendered
    assert "struggle" not in rendered


def test_a_veteran_with_no_losing_matchups_is_told_so_with_the_count():
    ctx = _intel_ctx(nemesis=_NO_NEMESIS)
    assert "61 cards" in "\n".join(member_report._intel_brief(ctx))
    assert "**61** cards" in member_report._render_deck(ctx)


def _played(*archetypes):
    return {
        "available": True,
        "decks": [
            {"archetype": a, "family": "bridge spam", "avg_elixir": 4.4, "battles": 80}
            for a in archetypes
        ],
    }


def test_suggested_decks_avoid_the_archetype_they_already_play():
    """ "Decks worth trying" ranked by how close to max the member can field them,
    which puts their OWN archetype first -- their collection is built for it. A
    member looking for a way out of a rut was handed their own deck back."""
    ctx = _intel_ctx(
        decks_played=_played("Mega Knight Bridge Spam", "Mega Knight Control"),
        discover={
            "available": True,
            "suggestions": [
                dict(_SUGGESTION, archetype="Mega Knight Bridge Spam"),
                dict(_SUGGESTION, archetype="Mega Knight Control"),
                dict(_SUGGESTION, archetype="Mortar Siege"),
            ],
        },
    )
    rendered = member_report._render_deck(ctx)
    assert "Mortar Siege" in rendered
    assert "Mega Knight" not in rendered.split("Decks worth trying")[1]


def test_a_one_archetype_collection_still_gets_suggestions():
    """Filtering to novel archetypes must not empty the section for a member who
    can only build one thing."""
    ctx = _intel_ctx(
        decks_played=_played("Hog Cycle"),
        discover={"available": True, "suggestions": [dict(_SUGGESTION, archetype="Hog Cycle")]},
    )
    assert "Hog Cycle" in member_report._render_deck(ctx)


def test_nobody_runs_it_is_never_said_about_a_deck_they_run():
    """fielded_by_members counts exact deck hashes, so a sibling of the member's
    own deck arrives as unplayed. Telling them nobody runs the archetype they
    play every day is simply false."""
    ctx = _intel_ctx(
        decks_played=_played("Hog Cycle"),
        discover={
            "available": True,
            "suggestions": [dict(_SUGGESTION, archetype="Hog Cycle", fielded_by_members=0)],
        },
    )
    assert "nobody in the clan runs it" not in member_report._render_deck(ctx)


def test_only_the_worst_few_matchups_are_named_and_never_as_a_failing():
    """Six records between 43% and 49% read as a list of failings; at n=32-67 none
    of them is distinguishable from even. Name the worst few as hard games."""
    nem = {
        "available": True,
        "any_losing_matchup": True,
        "cards_evaluated": 61,
        "sample_floor": 30,
        "nemeses": [
            {
                "card": f"Card{i}",
                "n": 40,
                "member_win_rate": 0.43 + i * 0.01,
                "losing_matchup": True,
            }
            for i in range(6)
        ],
    }
    rendered = member_report._render_deck(_intel_ctx(nemesis=nem))
    assert rendered.count("across 40 battles") == 3, "only the worst three"
    assert "Card5" not in rendered
    assert "struggle" not in rendered.lower()
    assert "Close games, not lost ones" in rendered


def _deck_section(ctx):
    from runtime.member_report import _render_deck

    return _render_deck(ctx) or ""


def test_the_email_names_who_beats_them_and_why(monkeypatch):
    """The email said HOW battles were decided (levels, elixir, even) and which
    CARDS show up in losses, but never which archetype was beating them or why.
    That is the read a member can act on: the record is the evidence, the
    structural note is the mechanism."""
    ctx = {
        "intel": {
            "coaching": {
                "matchup_record": [
                    {
                        "their_family": "bait",
                        "wins": 26,
                        "losses": 44,
                        "win_rate": 0.371,
                        "enough_games": True,
                        "structural_notes": ["no small spell against a bait deck"],
                    },
                    {
                        "their_family": "beatdown",
                        "wins": 40,
                        "losses": 10,
                        "win_rate": 0.8,
                        "enough_games": True,
                    },
                    {
                        "their_family": "siege",
                        "wins": 1,
                        "losses": 3,
                        "win_rate": 0.25,
                        "enough_games": False,
                    },
                ]
            }
        }
    }
    out = _deck_section(ctx)
    assert "**bait decks**" in out and "26-44" in out
    assert "No small spell against a bait deck" in out, "the mechanism, capitalised"
    assert "beatdown" not in out, "a matchup they WIN is not who beats them"
    assert "siege" not in out, "four games is not a pattern"


def test_suggested_decks_are_loadable_and_admit_what_the_link_drops(monkeypatch):
    """A deck in an email is a list to retype; a link is a deck you can try. The
    share format cannot carry Evo or Hero, so a deck that depends on one has to
    say which cards arrive as base rather than let the member find out mid-battle."""
    ctx = {
        "intel": {
            "discover": {
                "suggestions": [
                    {
                        "archetype": "Hog Cycle",
                        "family": "cycle",
                        "avg_elixir": 3.0,
                        "levels_from_max": 0.5,
                        "cards": [{"name": "Hog Rider", "form": "base", "level": 15}],
                        "copy_link": "https://link.clashroyale.com/en?x",
                        "link_omits_forms": ["Mini P.E.K.K.A"],
                        "fielded_by_members": 0,
                    }
                ]
            }
        }
    }
    out = _deck_section(ctx)
    assert "[Load this deck in Clash Royale](https://link.clashroyale.com/en?x)" in out
    assert "in as base card " in out, "one card takes the singular"
    assert "Mini P.E.K.K.A" in out


def test_a_maxed_member_is_told_what_would_open_new_decks():
    """ "Worth levelling next — nothing" is true and a dead end. The useful half for
    someone who has maxed their deck is which upgrade opens a deck they cannot
    field yet."""
    ctx = {
        "intel": {
            "upgrades": {
                "no_material_upgrades": True,
                "unlocks": [
                    {
                        "card": "Fireball",
                        "level": 13,
                        "levels_to_max": 3,
                        "archetypes_opened": 22,
                        "archetypes": ["Balloon Beatdown", "Control"],
                    }
                ],
            }
        }
    }
    out = _deck_section(ctx)
    assert "Worth levelling next** — nothing" in out
    assert "Upgrades that would open new decks" in out
    assert "**Fireball**" in out and "22" in out and "Balloon Beatdown" in out


def test_the_deck_they_run_is_told_what_it_lacks():
    ctx = {
        "intel": {
            "coaching": {
                "primary_deck_shape": {"role_coverage": {"gaps": ["no big spell"]}},
            }
        }
    }
    assert "What that deck is missing: no big spell." in _deck_section(ctx)
