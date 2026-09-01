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
        "20260703T211500.000Z",  # CR compact, millis
        "20260703T211500Z",  # CR compact, bare Z
        "20260703T211500",  # CR compact, suffixless
        "20260703T211500.000+00:00",  # war_analytics-observed hybrid
        "2026-07-03T21:15:00Z",  # ISO Z
        "2026-07-03T21:15:00+00:00",  # ISO offset
        "2026-07-03T21:15:00",  # ISO suffixless → UTC (engine convention)
        datetime(2026, 7, 3, 21, 15),  # naive datetime → UTC
        expected,  # aware datetime passthrough
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
    for max_level in (14, 12, 11, 8, 5, 4):  # common..champion-era values
        assert card_display_level(max_level, max_level) == 16
    assert card_display_level(11, 14) == 13  # mid-level common
    assert card_display_level(1, 8) == 9  # fresh legendary
    assert card_display_level(None, 14) is None
    assert card_display_level(5, None) == 5  # invalid maxLevel → passthrough
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
    assert pol_rank_improved(None, 500) is True  # newly attained
    assert pol_rank_improved(100, 50) is True
    assert pol_rank_improved(50, 100) is False
    assert pol_rank_improved(50, 50) is False
    assert pol_rank_improved(50, None) is False


def test_live_chaos_event_mode_keys_keep_their_authoritative_labels():
    """The 2026-08 live event feed supplies these exact in-game titles.

    Generic splitting would render ``Crazy Arena Epic Only`` and
    ``Chaos 1v 1 Triple Draft``, losing the official C.H.A.O.S branding used
    by the linked event records and in the client.
    """
    from engine.normalize import humanize_game_mode

    assert humanize_game_mode("Crazy_Arena_EpicOnly") == "C.H.A.O.S Epic Only"
    assert humanize_game_mode("Crazy_Arena_InfiniteElixir") == "C.H.A.O.S Infinite Elixir"
    assert humanize_game_mode("Chaos_1v1_TripleDraft") == "C.H.A.O.S Triple Draft"
    assert humanize_game_mode("Crazy_Arena_SuddenDeath") == "C.H.A.O.S Sudden Death"
    assert humanize_game_mode("Chaos_1v1_MegaDraft_All") == "Ken's C.H.A.O.S Mega Draft Tournament"


def test_tag_forms_named():
    assert canon_tag("abc123") == "#ABC123"
    assert canon_tag("#abc123") == "#ABC123"
    assert bare_tag("#abc123") == "ABC123"
    assert canon_tag(None) == "" and bare_tag("") == ""


def test_annotate_player_adds_display_level_beside_raw():
    payload = {"tag": "#A", "cards": [{"name": "Knight", "level": 11, "maxLevel": 14}]}
    out = annotate(payload, "player")
    card = out["cards"][0]
    assert card["level"] == 11  # raw untouched
    assert card["maxLevel"] == 14  # raw untouched
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
        [
            "grep",
            "-rlE",
            pattern,
            "--include=*.py",
            "engine",
            "storage",
            "db",
            "runtime",
            "agent",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
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


def test_mastery_badges_use_the_card_name_not_supercells_internal_key():
    """The badge API uses internal card keys the card API never returns. A plain
    camelCase split turns them into confident non-cards: a member's weekly email
    announced "Card Mastery: Witch Mother" and "Moving Cannon" and he read it as
    badges he had never earned, correctly, because no such cards exist. 18 of the
    56 mastery keys observed split into something that is not a card."""
    from engine.normalize import humanize_badge

    assert humanize_badge("MasteryWitchMother") == "Card Mastery: Mother Witch"
    assert humanize_badge("MasteryMovingCannon") == "Card Mastery: Cannon Cart"
    assert humanize_badge("MasteryAssassin") == "Card Mastery: Bandit"
    assert humanize_badge("MasteryDarkWitch") == "Card Mastery: Night Witch"
    assert humanize_badge("MasteryXbow") == "Card Mastery: X-Bow"
    # Keys that were already correct must not regress.
    assert humanize_badge("MasteryRonin") == "Card Mastery: Ronin"
    assert humanize_badge("MasterySuspiciousBush") == "Card Mastery: Suspicious Bush"


def test_the_two_post_map_cards_resolve():
    """GiantBuffer and MergeMaiden postdate Supercell's published key map. Both were
    recovered from the game's own gamedata.json and then corroborated against play
    data: every member holding the badge plays exactly that card."""
    from engine.normalize import humanize_badge

    assert humanize_badge("MasteryGiantBuffer") == "Card Mastery: Rune Giant"
    assert humanize_badge("MasteryMergeMaiden") == "Card Mastery: Spirit Empress"


def test_axeman_is_executioner_not_lumberjack():
    """A community alias map has this backwards. Supercell's own data gives
    Executioner the key "AxeMan" and Lumberjack "RageBarbarian". Also pinned:
    ZapMachine is Sparky while MiniSparkys is Zappies -- near-swapped names where
    getting it wrong names a real but wrong card, the hardest kind to notice."""
    from engine.normalize import humanize_badge

    assert humanize_badge("MasteryAxeMan") == "Card Mastery: Executioner"
    assert humanize_badge("MasteryRageBarbarian") == "Card Mastery: Lumberjack"
    assert humanize_badge("MasteryZapMachine") == "Card Mastery: Sparky"
    assert humanize_badge("MasteryMiniSparkys") == "Card Mastery: Zappies"


def test_an_unknown_mastery_key_is_never_given_an_invented_card_name():
    """Cards released after the key map was built resolve to nothing. Naming a
    card we cannot verify is the exact failure being fixed, so with the catalog
    supplied the label degrades to the achievement rather than inventing a card."""
    from engine.normalize import humanize_badge, mastery_card

    catalog = {"Ronin", "Mother Witch", "Cannon Cart"}
    # A key we have never seen, and one that resolves to a card this catalog lacks.
    assert humanize_badge("MasteryNotARealCard", catalog) == "a new Card Mastery badge"
    assert mastery_card("MasteryNotARealCard", catalog) is None
    assert mastery_card("MasteryGiantBuffer", catalog) is None, "Rune Giant not in this catalog"
    assert humanize_badge("MasteryRonin", catalog) == "Card Mastery: Ronin"
    # Non-mastery badges are untouched by any of this.
    assert humanize_badge("Chaos_S2", catalog) == "Chaos S2"


def test_badge_facts_resolves_the_card_and_its_foreign_key():
    """The upstream form: one raw key in, everything a reader needs out. The
    card_id comes from the catalog index, never from a hand-written table, so the
    foreign key cannot drift from the catalog it points at."""
    from engine.normalize import badge_facts

    catalog = {"Night Witch": 26000048, "Ronin": 26000106, "Chaos": 1}
    assert badge_facts("MasteryDarkWitch", catalog) == {
        "badge_label": "Card Mastery: Night Witch",
        "card_name": "Night Witch",
        "card_id": 26000048,
    }
    # A non-mastery badge carries a label and no card — there is no card to point at.
    assert badge_facts("Chaos_S2", catalog) == {"badge_label": "Chaos S2"}
    # Unresolvable: no card_name, so no card_id, so nothing downstream can name it.
    assert badge_facts("MasteryNotARealCard", catalog) == {
        "badge_label": "a new Card Mastery badge"
    }


def test_the_brain_never_sees_a_raw_badge_key_as_language():
    """On 2026-07-03 Elixir told the clan a member had mastered "Dark Witch" and
    "Archer". Both came from the awareness read handing the brain the raw badge
    key while the weekly email resolved it — one surface fixed, the other not.
    The compact signal now separates language (badge_label) from identity
    (badge_key), so there is no field a reader can mistake for a card name."""
    from runtime.awareness.read import _compact_signal

    catalog = {"Night Witch": 26000048, "Archers": 26000001}
    compact = _compact_signal(
        {
            "event_type": "badge_earned",
            "stream": "player",
            "payload": {"badge_name": "MasteryDarkWitch", "level": 3},
        },
        catalog,
    )
    assert compact["badge_label"] == "Card Mastery: Night Witch"
    assert compact["card_name"] == "Night Witch"
    assert compact["badge_key"] == "MasteryDarkWitch"
    assert "Dark Witch" not in compact["badge_label"]
    # An event written by the current emitter carries the label already; the read
    # must prefer the stamped value over re-deriving it.
    stamped = _compact_signal(
        {
            "event_type": "badge_earned",
            "stream": "player",
            "payload": {
                "badge_name": "MasteryArcher",
                "badge_label": "Card Mastery: Archers",
                "card_name": "Archers",
                "level": 1,
            },
        },
        catalog,
    )
    assert stamped["badge_label"] == "Card Mastery: Archers"
    assert stamped["card_name"] == "Archers"
