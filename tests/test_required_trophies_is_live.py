"""The join floor is a clan SETTING, not a constant, and must never be quoted
from memory.

It was 2,000 when the clan started and is 7,000 today. CLAN.md stated the old
value as a literal, and `prompts/agents/awareness.md` gave the model the exact
phrasing to copy — so every welcome post carried it. The result was not merely
stale: a member who joined at 7,053 was told they were "well clear of our
2,000-trophy entry line", when they had cleared the real floor by 53. A wrong
floor manufactures praise.

The live value was already polled and stored the whole time
(`engine/projections.refresh_clan_rollups` writes `requiredTrophies` into
`clan_daily_metrics.required_trophies`) — nothing read it into the prompt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import db
import prompts

ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = ROOT / "prompts"


@pytest.fixture
def clan_db(tmp_path, monkeypatch):
    path = str(tmp_path / "floor.db")
    original = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: original(path))
    conn = original(path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
            "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-30', 1)"
        )
        conn.commit()
        yield conn
    finally:
        conn.close()


def _seed_floor(conn, metric_date: str, value: int):
    conn.execute(
        "INSERT INTO clan_daily_metrics (clan_tag, metric_date, required_trophies, observed_at) "
        "VALUES ('#J2RGCRVG', ?, ?, ?)",
        (metric_date, value, f"{metric_date}T00:00:00Z"),
    )
    conn.commit()


def test_the_prompt_renders_the_live_floor(clan_db):
    _seed_floor(clan_db, "2026-07-30", 7000)

    line = next(x for x in prompts.clan().splitlines() if "Join requirement" in x)

    assert "7,000" in line
    assert "2,000" not in line, "the prompt is still quoting the old floor"


def test_a_changed_floor_is_picked_up_without_a_code_change(clan_db):
    """The whole point: Jamie changes it in-game and the prompt follows."""
    _seed_floor(clan_db, "2026-07-29", 7000)
    _seed_floor(clan_db, "2026-07-30", 8000)

    line = next(x for x in prompts.clan().splitlines() if "Join requirement" in x)

    assert "8,000" in line, "the prompt did not follow the newest clan setting"


def test_an_unavailable_floor_says_so_instead_of_guessing(clan_db):
    """No row at all. The prompt must still build — and must NOT emit a number,
    because a confident wrong floor is worse than no floor."""
    line = next(x for x in prompts.clan().splitlines() if "Join requirement" in x)

    assert "read it live" in line
    assert not re.search(r"\d,?\d00", line), f"a number was invented: {line!r}"


def test_no_prompt_file_hardcodes_a_join_floor():
    """The gate. Any prompt asserting a specific join-trophy number will drift
    the moment the clan setting changes, which is exactly what happened.

    Matched narrowly: a 4-5 digit number adjacent to trophy/join/floor wording.
    The two example lines in the recruiting/Discord copy guidance are allowed
    because each explicitly tells the model to use CLAN.md's current value.
    """
    pattern = re.compile(
        r"(join|entry|entrance|floor|requirement|required|minimum|to join)"
        r"[^\n]{0,60}?(\d[\d,]{3,})"
        r"|(\d[\d,]{3,})[^\n]{0,30}?(trophy|trophies)[^\n]{0,30}?"
        r"(to join|floor|entry|requirement|minimum)",
        re.IGNORECASE,
    )
    offenders = []
    for path in sorted(PROMPT_DIR.rglob("*.md")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not pattern.search(line):
                continue
            if "CLAN.md" in line or "never quote a remembered number" in line:
                continue  # explicitly defers to the live value
            if "NEVER quote the join floor" in line:
                continue  # the rule itself, citing the incident
            offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:100]}")

    assert offenders == [], (
        "prompt(s) hardcode a trophy floor. It is a clan setting that changes — "
        "use the <<REQUIRED_TROPHIES>> token in CLAN.md so it is read live:\n  "
        + "\n  ".join(offenders)
    )
