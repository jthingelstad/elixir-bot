"""The CR normalizer quirk catalog — golden tests per quirk
(docs/reference/v5.1/normalize.md). Each case is a real observed input form
or the live incident that taught the rule."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from engine.normalize import (
    ARENA_UP_MAX_CANONICAL_ID,
    annotate,
    arena_kind,
    bare_tag,
    canon_tag,
    card_display_level,
    parse_cr_time,
    pol_rank_improved,
    war_day,
)

REPO = Path(__file__).resolve().parent.parent


def test_parse_cr_time_all_observed_forms():
    expected = datetime(2026, 7, 3, 21, 15, 0, tzinfo=timezone.utc)
    forms = [
        "20260703T211500.000Z",        # CR compact, millis
        "20260703T211500Z",            # CR compact, bare Z
        "20260703T211500",             # CR compact, suffixless
        "20260703T211500.000+00:00",   # war_analytics-observed hybrid
        "2026-07-03T21:15:00Z",        # ISO Z
        "2026-07-03T21:15:00+00:00",   # ISO offset
        "2026-07-03T21:15:00",         # ISO suffixless → UTC (engine convention)
        datetime(2026, 7, 3, 21, 15),  # naive datetime → UTC
        expected,                       # aware datetime passthrough
    ]
    for form in forms:
        dt = parse_cr_time(form)
        assert dt == expected, f"form {form!r} → {dt}"
        assert dt.tzinfo is not None  # ALWAYS aware


def test_parse_cr_time_rejects_garbage():
    for bad in (None, "", "not a time", "2026-13-99T99:99:99", 42):
        assert parse_cr_time(bad) is None or bad == 42 and parse_cr_time(bad) is None


def test_card_display_level_per_rarity():
    # docs/cr-api-docs/cards.md: display = level + (16 − maxLevel);
    # every rarity's max card displays as 16.
    for max_level in (14, 12, 11, 8, 5, 4):   # common..champion-era values
        assert card_display_level(max_level, max_level) == 16
    assert card_display_level(11, 14) == 13     # mid-level common
    assert card_display_level(1, 8) == 9        # fresh legendary
    assert card_display_level(None, 14) is None
    assert card_display_level(5, None) == 5     # invalid maxLevel → passthrough
    assert card_display_level(5, 99) == 5


def test_war_day_structure_and_humanization():
    # periodIndex % 7: 0–2 training, 3–6 battle (0-based — the quirk).
    d = war_day(21)
    assert (d.day_in_week, d.war_day_index, d.phase) == (0, None, "training")
    assert d.human == "training day 1 of 3"
    d = war_day(24)
    assert (d.war_day_index, d.human) == (0, "battle day 1 of 4")
    d = war_day(27)
    assert (d.war_day_index, d.human) == (3, "battle day 4 of 4")
    # Live incident 2026-07-04: periodIndex 33 → copy said "day 2"; truth:
    d = war_day(33)
    assert d.human == "battle day 3 of 4"
    assert war_day(None) is None and war_day(-1) is None


def test_arena_kind_boundary():
    assert arena_kind(ARENA_UP_MAX_CANONICAL_ID) == "road"
    assert arena_kind(ARENA_UP_MAX_CANONICAL_ID + 1) == "seasonal"  # PANCAKES! zone
    assert arena_kind(None) is None


def test_pol_rank_lower_is_better():
    assert pol_rank_improved(None, 500) is True     # newly attained
    assert pol_rank_improved(100, 50) is True
    assert pol_rank_improved(50, 100) is False
    assert pol_rank_improved(50, 50) is False
    assert pol_rank_improved(50, None) is False


def test_tag_forms_named():
    assert canon_tag("abc123") == "#ABC123"
    assert canon_tag("#abc123") == "#ABC123"
    assert bare_tag("#abc123") == "ABC123"
    assert canon_tag(None) == "" and bare_tag("") == ""


def test_annotate_player_adds_display_level_beside_raw():
    payload = {"tag": "#A", "cards": [{"name": "Knight", "level": 11, "maxLevel": 14}]}
    out = annotate(payload, "player")
    card = out["cards"][0]
    assert card["level"] == 11          # raw untouched
    assert card["maxLevel"] == 14       # raw untouched
    assert card["display_level"] == 13  # derived alongside


def test_annotate_battlelog_list_and_riverrace():
    battles = [{"team": [{"cards": [{"level": 8, "maxLevel": 8}]}], "opponent": []}]
    out = annotate(battles, "player_battlelog")
    assert out[0]["team"][0]["cards"][0]["display_level"] == 16
    race = {"periodIndex": 33, "periodType": "colosseum"}
    out = annotate(race, "clan_war")
    assert out["day_human"] == "battle day 3 of 4" and out["day_phase"] == "battle"
    assert out["periodIndex"] == 33  # raw untouched


def test_annotate_unknown_endpoint_untouched():
    payload = {"weird": {"level": 3}}
    assert annotate(payload, "leaderboards") == {"weird": {"level": 3}}
    assert annotate(payload, None) is payload


def _grep(pattern: str, exclude: set[str]) -> list[str]:
    out = subprocess.run(
        ["grep", "-rlE", pattern, "--include=*.py",
         "engine", "storage", "db", "runtime", "agent"],
        capture_output=True, text=True, cwd=REPO,
    )
    hits = [line for line in out.stdout.splitlines() if line]
    return [h for h in hits if h not in exclude]


def test_grep_gates_quirks_have_one_home():
    """The normalizer owns the quirk math — no local copies may creep back."""
    assert _grep(r"strptime\(.*%Y%m%dT", {"engine/normalize.py"}) == []
    assert _grep(r"16 - max_level|16 - maxLevel", {"engine/normalize.py"}) == []
    # day arithmetic: % PERIODS_PER_SECTION / % 7 day math is normalize/clock only
    assert _grep(r"% *7\) *- *TRAINING_DAYS", set()) == []


def test_tool_annotation_wraps_dispatch():
    """agent/cr_api_tool._execute_cr_api routes through normalize.annotate."""
    from agent import cr_api_tool

    assert hasattr(cr_api_tool, "_execute_cr_api_inner")
    src = Path(REPO, "agent", "cr_api_tool.py").read_text()
    assert re.search(r"from engine\.normalize import annotate", src)
