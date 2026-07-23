"""The awareness read carries an absolute, UTC-anchored wall-clock (clock block)
so the brain knows exactly when it's speaking without misreading a bare timestamp
as local. POAP KINGS is international — no single local time — so it's UTC plus
labeled reference conversions, not "the" local time."""

from __future__ import annotations

import re

from runtime.awareness import read as read_mod

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def test_clock_block_is_utc_anchored_with_reference_zones():
    clock = read_mod._clock_block()
    assert set(clock) == {"utc", "us_central_ref", "india_ref"}
    # UTC is the primary, explicitly labeled, with a weekday.
    assert clock["utc"].endswith("UTC")
    assert any(clock["utc"].startswith(day) for day in _WEEKDAYS)
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$", clock["utc"])
    # Reference zones are labeled (not presented as "the" local time).
    assert clock["india_ref"].endswith("IST")
    assert clock["us_central_ref"].rstrip().endswith(("CDT", "CST"))


def test_build_read_includes_clock_block(engine_conn):
    r = read_mod.build_read(conn=engine_conn)
    assert "clock" in r
    assert "clock" not in r.get("_degraded", [])
    assert r["clock"]["utc"].endswith("UTC")


def test_delivery_block_carries_cadence_and_leader_gate(monkeypatch):
    monkeypatch.setenv("AWARENESS_LOOP_HOURS", "*/3")
    monkeypatch.setenv("AWARENESS_LOOP_MINUTE", "5")
    d = read_mod._delivery_block()
    # The two runway facts are always present (never break the read).
    assert "not continuously" in d["not_continuous"]
    assert "leader must manually paste" in d["clan_chat_is_leader_gated"]
    # Cadence is derived from the scheduler env, so the brain never drifts from it.
    assert d["typical_interval"] == "~3h"
    assert d["next_run_utc"].endswith("UTC")
    assert d["minutes_until_next_run"] <= 3 * 60


def test_delivery_block_survives_a_bad_cron_expr(monkeypatch):
    monkeypatch.setenv("AWARENESS_LOOP_HOURS", "not-a-cron")
    d = read_mod._delivery_block()
    # A broken schedule must still yield the prose runway facts, no exception.
    assert d["not_continuous"] and d["clan_chat_is_leader_gated"]
    assert "next_run_utc" not in d


def test_build_read_includes_delivery_block(engine_conn):
    r = read_mod.build_read(conn=engine_conn)
    assert "delivery" in r
    assert "delivery" not in r.get("_degraded", [])
    assert r["delivery"]["clan_chat_is_leader_gated"]


def test_build_read_closes_snapshot_it_starts_on_borrowed_connection(engine_conn):
    # First use may initialize durable event cursors; the caller owns that
    # write transaction and commits it explicitly.
    read_mod.build_read(conn=engine_conn)
    engine_conn.commit()
    assert engine_conn.in_transaction is False

    read_mod.build_read(conn=engine_conn)

    assert engine_conn.in_transaction is False
