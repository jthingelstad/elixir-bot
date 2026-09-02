"""The Elder rules are enforced in engine/management.py and explained to
members in three hand-copied prose surfaces: prompts/POLICY.md here, and
src/members.njk + src/faq.njk on poapkings.com. That copying has drifted
twice (the FAQ quoted a 15-20% band against a 20-30% engine; POLICY.md called
donations primary months after war became 0.65). This test pins every number
those surfaces quote to the constant the engine actually applies, so changing
a constant without re-editing the prose fails here instead of publishing rules
the engine no longer enforces.

The poapkings.com surfaces are checked only when the sibling checkout is on
disk (it is on Jamie's machine); CI checks POLICY.md alone.

If this test fails because you REWORDED prose without changing a number,
update the snippet here to match the new wording — the point is the numbers,
not the sentences.
"""

from pathlib import Path

import pytest

from engine.management import (
    ELDER_BAND_CEIL,
    ELDER_BAND_FLOOR,
    KICK_CONTRIB_GRACE_MAX,
    PROMOTE_TENURE_MIN,
    RANKED_FLOOR_BATTLES,
    RANKED_WEIGHT,
    SCORE_W_DONATION,
    SCORE_W_WAR,
    WAR_FLOOR_DAYS,
    WAR_FLOOR_WINDOW,
    WAR_RATE_WINDOW,
    _war_day_credit,
)

_REPO = Path(__file__).resolve().parents[1]
_POLICY = _REPO / "prompts" / "POLICY.md"
_SITE_SRC = _REPO.parent / "poapkings.com" / "src"

_BAND = f"{int(ELDER_BAND_FLOOR * 100)}-{int(ELDER_BAND_CEIL * 100)}%"
_FULL_VS_TWO = f"{_war_day_credit(4, 4) / _war_day_credit(2, 4):.1f}x"


def _normalized(path: Path) -> str:
    # En/em dashes and non-breaking hyphens all read as "-" so the site's
    # typographic "20–30%" matches the same snippet as POLICY.md's "20-30%".
    text = path.read_text(encoding="utf-8")
    for dash in ("–", "—", "‑"):
        text = text.replace(dash, "-")
    return text


def _assert_states(path: Path, snippets: list[str]) -> None:
    text = _normalized(path)
    missing = [s for s in snippets if s not in text]
    assert not missing, (
        f"{path} no longer states {missing!r}. Either an engine/management.py "
        "constant changed without the prose keeping up (fix the prose on ALL "
        "THREE surfaces: prompts/POLICY.md, poapkings.com src/members.njk, "
        "src/faq.njk), or the prose was reworded (update the snippet here)."
    )


def test_word_numbers_still_match_constants():
    # These constants are written out as WORDS in prose ("one war day",
    # "about four weeks"), which no snippet built from the constant can track.
    # If either changes, rewrite the wording on all three surfaces by hand.
    assert WAR_FLOOR_DAYS == 1, "members.njk says 'one war day' — reword it"
    assert PROMOTE_TENURE_MIN == 28, "faq.njk says 'about four weeks' — reword it"


def test_policy_md_states_the_live_elder_rules():
    _assert_states(
        _POLICY,
        [
            f"at least {PROMOTE_TENURE_MIN} days",
            f"at least {WAR_FLOOR_DAYS} finalized war day",
            f"last {WAR_FLOOR_WINDOW} days",
            f"at least {RANKED_FLOOR_BATTLES} ranked battles",
            f"averaged over {WAR_RATE_WINDOW} days",
            f"{RANKED_WEIGHT:.2f} x ranked%",
            f"{SCORE_W_WAR:.2f} x competitive",
            f"{SCORE_W_DONATION:.2f} x donation%",
            f"{_BAND} of the whole active roster",
            f"up to {KICK_CONTRIB_GRACE_MAX} extra confirm days",
            f"scores {_war_day_credit(3, 4):.2f}",
            f"scores {_war_day_credit(2, 4):.2f}",
            f"about {_FULL_VS_TWO}",
        ],
    )


@pytest.mark.skipif(not _SITE_SRC.is_dir(), reason="poapkings.com sibling checkout not present")
def test_members_page_states_the_live_elder_rules():
    _assert_states(
        _SITE_SRC / "members.njk",
        [
            f"{PROMOTE_TENURE_MIN}+ days",
            f"one war day with a deck played in the last {WAR_FLOOR_WINDOW} days",
            f"{RANKED_FLOOR_BATTLES} or more ranked battles in the last {WAR_FLOOR_WINDOW}",
            f"over {WAR_RATE_WINDOW} days",
            f"Competing is {int(SCORE_W_WAR * 100)}% of your score",
            f"donations are {int(SCORE_W_DONATION * 100)}%",
            f"{_BAND} of the clan",
            f"about {_FULL_VS_TWO} two decks",
        ],
    )


@pytest.mark.skipif(not _SITE_SRC.is_dir(), reason="poapkings.com sibling checkout not present")
def test_faq_states_the_live_elder_rules():
    _assert_states(
        _SITE_SRC / "faq.njk",
        [
            "about four weeks",
            f"{_BAND} of the clan",
        ],
    )
