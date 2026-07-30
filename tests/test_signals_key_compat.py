"""`signals_by_lane` → `signals_by_category`: the stored rows keep the old key.

The rename fixed a real collision — "lane" meant a Discord channel in
`DISCORD.md` and a signal category in the awareness read, and both reached the
model inside the same prompt.

But ~393 `awareness_thoughts` rows were already written with the old spelling,
and two places read that JSON back: the system-status `signals_in` count and
the Observatory's loop list. Measured against the live database at rename time,
dropping the fallback took the 7-day `signals_in` from **136 to 0** — a silent
zero, the failure mode this codebase keeps producing.

So the dual read is load-bearing until those rows age out. These tests exist so
it is removed on purpose rather than tidied away.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3

SQL = """
SELECT COALESCE(SUM(json_array_length(category.value)), 0)
FROM awareness_thoughts AS thought,
     json_each(CASE
         WHEN json_valid(thought.read_json)
         THEN COALESCE(
                  json_extract(thought.read_json, '$.signals_by_category'),
                  json_extract(thought.read_json, '$.signals_by_lane'),
                  '{}')
         ELSE '{}'
     END) AS category
"""


@contextlib.contextmanager
def _db(*reads):
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE awareness_thoughts (thought_id TEXT, at TEXT, read_json TEXT)")
        for i, read in enumerate(reads):
            conn.execute(
                "INSERT INTO awareness_thoughts VALUES (?,?,?)",
                (f"t{i}", "2026-07-29T00:00:00Z", json.dumps(read)),
            )
        yield conn
    finally:
        conn.close()


def test_counts_signals_under_either_key():
    """A pre-rename row and a post-rename row must both be counted."""
    with _db(
        {"signals_by_lane": {"war": [1, 2], "milestone": [3]}},  # old spelling
        {"signals_by_category": {"war": [1], "clan_event": [2]}},  # new spelling
    ) as conn:
        assert conn.execute(SQL).fetchone()[0] == 5


def test_dropping_the_fallback_would_silently_zero_history():
    """The regression guard: new-key-only reads historical rows as nothing.

    Not a hypothetical — this is exactly what the live status page would have
    reported for the week spanning the rename.
    """
    with _db({"signals_by_lane": {"war": [1, 2, 3]}}) as conn:
        assert conn.execute(SQL).fetchone()[0] == 3

        new_key_only = SQL.replace(
            "COALESCE(\n                  json_extract(thought.read_json, '$.signals_by_category'),\n"
            "                  json_extract(thought.read_json, '$.signals_by_lane'),\n"
            "                  '{}')",
            "COALESCE(json_extract(thought.read_json, '$.signals_by_category'), '{}')",
        )
        assert conn.execute(new_key_only).fetchone()[0] == 0


def test_the_new_key_is_preferred_when_both_are_present():
    with _db(
        {"signals_by_category": {"war": [1]}, "signals_by_lane": {"war": [1, 2, 3, 4]}}
    ) as conn:
        assert conn.execute(SQL).fetchone()[0] == 1


def test_the_producer_writes_only_the_new_key():
    """Nothing should still be emitting the old spelling."""
    from runtime.awareness import read as read_mod

    source = (read_mod.__file__ or "").replace(".pyc", ".py")
    with open(source, encoding="utf-8") as handle:
        body = handle.read()
    assert "signals_by_category" in body
    assert "signals_by_lane" not in body
