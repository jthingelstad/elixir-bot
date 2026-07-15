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


def test_build_read_closes_snapshot_it_starts_on_borrowed_connection(engine_conn):
    # First use may initialize durable event cursors; the caller owns that
    # write transaction and commits it explicitly.
    read_mod.build_read(conn=engine_conn)
    engine_conn.commit()
    assert engine_conn.in_transaction is False

    read_mod.build_read(conn=engine_conn)

    assert engine_conn.in_transaction is False
