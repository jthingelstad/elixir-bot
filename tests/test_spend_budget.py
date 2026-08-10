"""The awareness / Ask Elixir ceiling: bounded cost without starving jobs.

Written the day the 14-day cost record showed a median day of $4.02 and a worst
of $9.70 — the problem being unboundedness, not the average.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent import spend_budget

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def test_a_hard_post_workflow_is_never_budget_gated(monkeypatch, engine_conn):
    """The floor guarantee outranks the ceiling, at every spend level.

    If the ceiling could silence a farewell or a welcome, the ceiling would be a
    bug — a member's arrival is not a discretionary purchase.
    """
    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "1.00")
    spend_budget.record_spend_usd("awareness", 999.0, now=NOW, conn=engine_conn)
    for workflow in sorted(spend_budget.ESSENTIAL):
        allowed, why = spend_budget.may_run(workflow, conn=engine_conn, now=NOW)
        assert allowed, f"{workflow} was gated at ${999.0}: {why}"


def test_discretionary_work_stops_at_the_ceiling(monkeypatch, engine_conn):
    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "3.20")
    assert spend_budget.may_run("interactive", conn=engine_conn, now=NOW)[0] is True
    spend_budget.record_spend_usd("interactive", 3.25, now=NOW, conn=engine_conn)
    allowed, why = spend_budget.may_run("interactive", conn=engine_conn, now=NOW)
    assert allowed is False
    assert "ceiling reached" in why


def test_deferrable_work_sheds_earlier_than_member_conversation(monkeypatch, engine_conn):
    """Shedding order matters: a member notices a missing reply, not a missing
    deck review. Deferrable work stops at the warning line so the remaining
    budget goes to the person who is actually waiting."""
    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "4.00")
    monkeypatch.setenv("ELIXIR_SPEND_WARN_AT", "0.75")
    spend_budget.record_spend_usd("interactive", 3.10, now=NOW, conn=engine_conn)
    assert spend_budget.may_run("deck_review", conn=engine_conn, now=NOW)[0] is False
    assert spend_budget.may_run("interactive", conn=engine_conn, now=NOW)[0] is True, (
        "conversation outlives analysis"
    )


def test_the_ceiling_fails_open(monkeypatch):
    """An unreadable counter must never be what stops Elixir working."""

    def _boom(**kwargs):
        raise RuntimeError("no database")

    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "0.01")
    monkeypatch.setattr(spend_budget, "spend_today_usd", _boom)
    assert spend_budget.may_run("deck_review")[0] is True


def test_setting_the_ceiling_to_zero_disables_it(monkeypatch, engine_conn):
    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "0")
    spend_budget.record_spend_usd("awareness", 500.0, now=NOW, conn=engine_conn)
    assert spend_budget.may_run("deck_review", conn=engine_conn, now=NOW)[0] is True


def test_the_counter_lives_in_the_clan_db_not_telemetry(engine_conn):
    """Same rule that moved the wake budget: a number that can refuse a model
    call is a decision, and decisions may not depend on a deletable file."""
    spend_budget.record_spend_usd("awareness", 1.5, now=NOW, conn=engine_conn)
    row = engine_conn.execute(
        "SELECT cursor_int FROM stream_cursors WHERE consumer_key = ? AND scope_key = ?",
        (spend_budget.SPEND_CURSOR_KEY, "2026-08-05"),
    ).fetchone()
    assert row and row[0] == 1_500_000
    assert abs(spend_budget.spend_today_usd(now=NOW, conn=engine_conn) - 1.5) < 1e-9

    import inspect

    source = inspect.getsource(spend_budget)
    assert "telemetry" not in source.split('"""', 2)[-1], (
        "the spend ceiling must not read the telemetry database"
    )


@pytest.mark.parametrize(
    "workflow",
    [
        "member_report",
        "weekly_recap",
        "weekly_recap_email",
        "memory_synthesis",
        "recruiting_copy",
        "release_notes",
        "leader_action_feedback",
        "clanops",
    ],
)
def test_jobs_and_non_ask_surfaces_are_neither_charged_nor_gated(
    workflow, monkeypatch, engine_conn
):
    """The source policy from #268: promised work cannot consume or be refused
    by a budget that belongs to awareness and Ask Elixir."""
    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "0.01")
    spend_budget.record_spend_usd(workflow, 999.0, now=NOW, conn=engine_conn)
    assert spend_budget.spend_today_usd(now=NOW, conn=engine_conn) == 0
    assert spend_budget.may_run(workflow, conn=engine_conn, now=NOW) == (True, "")


def test_the_new_ledger_ignores_the_old_mixed_workflow_total(monkeypatch, engine_conn):
    """Deploying the policy must release today's jobs and discretionary room;
    the old key includes report and synthesis costs that cannot be separated
    without making behavior depend on admin telemetry."""
    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "3.20")
    engine_conn.execute(
        "INSERT INTO stream_cursors (consumer_key, scope_key, cursor_int, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("budget:llm:spend_micros", "2026-08-05", 999_000_000, "2026-08-05T12:00:00Z"),
    )
    assert spend_budget.spend_today_usd(now=NOW, conn=engine_conn) == 0
    assert spend_budget.may_run("awareness", conn=engine_conn, now=NOW) == (True, "")


def test_required_floor_recovery_can_cross_the_awareness_ceiling(monkeypatch, engine_conn):
    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "1.00")
    spend_budget.record_spend_usd("awareness", 2.0, now=NOW, conn=engine_conn)
    assert spend_budget.may_run("awareness", conn=engine_conn, now=NOW)[0] is False
    with spend_budget.required_work():
        assert spend_budget.may_run("awareness", conn=engine_conn, now=NOW) == (True, "")
    assert spend_budget.may_run("awareness", conn=engine_conn, now=NOW)[0] is False


@pytest.mark.parametrize(
    "model,tokens,expected",
    [
        # 1M output tokens at the published rate, as a sanity anchor.
        ("claude-sonnet-5", 1_000_000, 10.0),
        ("claude-opus-5", 1_000_000, 25.0),
        ("claude-haiku-4-5-20251001", 1_000_000, 5.0),
    ],
)
def test_cost_maths_matches_published_rates(model, tokens, expected):
    assert (
        abs(spend_budget.call_cost_usd(model, 0, tokens, 0, 0, effective_at=NOW) - expected) < 1e-6
    )


def test_cache_reads_are_a_tenth_of_input():
    """Almost all of Elixir's input is cache reads — if this were wrong the
    ceiling would be wrong by an order of magnitude."""
    full = spend_budget.call_cost_usd("claude-sonnet-5", 1_000_000, 0, 0, 0, effective_at=NOW)
    cached = spend_budget.call_cost_usd("claude-sonnet-5", 0, 0, 0, 1_000_000, effective_at=NOW)
    assert abs(cached - full * 0.1) < 1e-6


def test_a_member_is_told_the_truth_not_told_to_retry():
    """The generic router fallback is "try again in a sec". For the ceiling that
    is a lie — retrying does not help until midnight UTC — and a member who
    believes it retries until they give up, seeing a broken bot each time."""
    from agent.core import SpendCeilingReached
    from runtime.channel_router import _member_error_text

    generic = "Hit an error reviewing the deck. Try again in a sec."
    ceiling_text = _member_error_text(SpendCeilingReached("deck_review: ceiling"), generic)
    assert ceiling_text != generic
    assert "try again in a sec" not in ceiling_text.lower()
    assert "midnight utc" in ceiling_text.lower(), "say WHEN it clears"
    assert "welcome" in ceiling_text.lower(), "say what still works"

    # A real error still reads like a real error.
    assert _member_error_text(ValueError("boom"), generic) == generic


def test_leadership_is_told_once_per_day_not_once_per_refusal(monkeypatch, engine_conn):
    """A ceiling nobody is told about is indistinguishable from a fault — but a
    busy afternoon must not become a notification storm."""
    sent = []
    from runtime import alerts

    monkeypatch.setattr(alerts, "schedule_spend_ceiling_notice", lambda detail: sent.append(detail))
    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "1.00")
    spend_budget._ANNOUNCED.clear()
    spend_budget.record_spend_usd("interactive", 5.0, now=NOW, conn=engine_conn)
    for _ in range(4):
        spend_budget.may_run("deck_review", conn=engine_conn, now=NOW)
    assert len(sent) == 1, f"announced {len(sent)} times, expected once"


# ------------------------------------------------- the startup budget line


def test_startup_reports_the_remaining_budget(monkeypatch):
    """A restart is exactly when someone asks "is it working?" — so the boot
    message says what is left, turning an expected pause into a visible state
    rather than a suspected fault."""
    from runtime import startup

    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "3.20")
    monkeypatch.setattr(spend_budget, "spend_today_usd", lambda **k: 0.21)
    line = startup._startup_budget_summary()
    assert "$2.99" in line and "$3.20" in line and "$0.21" in line


def test_startup_names_the_paused_state_not_just_the_number(monkeypatch):
    """At the ceiling the number alone is ambiguous. Say what is paused and
    what is not, because "deck reviews are refusing" is otherwise indistinguish-
    able from a fault."""
    from runtime import startup

    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "3.20")
    monkeypatch.setattr(spend_budget, "spend_today_usd", lambda **k: 3.25)
    line = startup._startup_budget_summary()
    assert "Ceiling reached" in line
    assert "midnight UTC" in line
    assert "Jobs and hard posts are unaffected" in line


def test_startup_says_so_when_there_is_no_ceiling(monkeypatch):
    from runtime import startup

    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "0")
    assert "unbounded" in startup._startup_budget_summary()


def test_startup_budget_line_never_breaks_the_boot_message(monkeypatch):
    """The boot message must survive an unreadable counter — it is the thing
    that tells us the bot came up at all."""
    from runtime import startup

    monkeypatch.setenv("ELIXIR_DAILY_SPEND_USD", "3.20")

    def _boom(**kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr(spend_budget, "spend_today_usd", _boom)
    line = startup._startup_budget_summary()
    assert "unreadable" in line and "fails open" in line


def test_the_budget_is_not_pinned_to_the_day_the_tests_were_written(engine_conn):
    """Regression: these tests were green when written and failed a few hours
    later, when UTC rolled past midnight.

    The counter is keyed by UTC DATE. The tests wrote with a fixed `now` and
    read with the real clock, so they only agreed on 2026-08-05. That is the
    date-boundary flake class this repo already knows from the battle-rollup
    tests — worth a guard rather than a second discovery.
    """
    from datetime import timedelta

    for offset in (-40, -1, 0, 1, 40):
        day = NOW + timedelta(days=offset)
        spend_budget.record_spend_usd("interactive", 1.0, now=day, conn=engine_conn)
        assert abs(spend_budget.spend_today_usd(now=day, conn=engine_conn) - 1.0) < 1e-9, (
            f"spend must be readable on its own day ({day:%Y-%m-%d}), not just today"
        )
    # And days do not bleed into each other.
    assert abs(spend_budget.spend_today_usd(now=NOW, conn=engine_conn) - 1.0) < 1e-9
