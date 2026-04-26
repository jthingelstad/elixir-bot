"""Tests for Discord message chunking (splits at paragraph/line/word boundaries)
and custom-emoji shortcode resolution."""

from types import SimpleNamespace

from runtime.app import _resolve_custom_emoji
from runtime.helpers._common import _chunk_for_discord


def test_short_text_returns_single_chunk():
    assert _chunk_for_discord("hello") == ["hello"]


def test_empty_text_returns_empty_list():
    assert _chunk_for_discord("") == []
    assert _chunk_for_discord(None) == []


def test_splits_on_paragraph_boundary_when_possible():
    para_a = "A" * 50
    para_b = "B" * 50
    text = f"{para_a}\n\n{para_b}"
    chunks = _chunk_for_discord(text, size=80)
    assert chunks[0] == para_a
    assert chunks[1] == para_b


def test_splits_on_line_boundary_when_no_paragraph_fits():
    line_a = "A" * 40
    line_b = "B" * 40
    line_c = "C" * 40
    text = f"{line_a}\n{line_b}\n{line_c}"
    chunks = _chunk_for_discord(text, size=50)
    # Each chunk should end at a line break (i.e., start fresh on B or C).
    for chunk in chunks:
        assert "\n\n" not in chunk  # no synthetic doubling
        assert len(chunk) <= 50


def test_splits_on_word_boundary_when_no_line_fits():
    text = "word " * 40  # no newlines
    chunks = _chunk_for_discord(text, size=50)
    for chunk in chunks:
        # Each chunk should not end mid-word.
        assert not chunk.endswith("wor")
        assert not chunk.endswith("w")
        assert len(chunk) <= 50


def test_hard_char_split_when_no_whitespace():
    text = "x" * 200  # no breaks anywhere
    chunks = _chunk_for_discord(text, size=50)
    # Hard ceiling is still respected.
    assert all(len(c) <= 50 for c in chunks)
    # All content is preserved.
    assert "".join(chunks) == text


def test_all_content_preserved_across_chunks():
    text = "Paragraph one with several sentences.\n\nParagraph two.\n\nParagraph three has more words."
    chunks = _chunk_for_discord(text, size=40)
    # Rejoin and compare ignoring whitespace (strip/lstrip semantics mean adjacent
    # whitespace gets normalized, but no content should be lost).
    rejoined = "\n\n".join(chunks)
    for word in ["Paragraph", "sentences", "three", "words"]:
        assert word in rejoined


def test_never_exceeds_size_ceiling():
    text = "Some prose with occasional breaks.\n" + ("longwordwithoutspaces" * 20)
    chunks = _chunk_for_discord(text, size=100)
    assert all(len(c) <= 100 for c in chunks)


def _guild(*emojis):
    items = [
        SimpleNamespace(name=name, id=emoji_id, animated=False)
        for name, emoji_id in emojis
    ]
    return SimpleNamespace(emojis=items)


def test_resolve_custom_emoji_rewrites_known_shortcodes():
    guild = _guild(("elixir_trophy", 1001))
    assert _resolve_custom_emoji(":elixir_trophy: win", guild) == "<:elixir_trophy:1001> win"


def test_resolve_custom_emoji_strips_hallucinated_names():
    # :poap: and :poap_kings: match neither guild custom emoji nor a Unicode
    # shortcode — the model hallucinated them. Strip rather than leak raw text.
    guild = _guild(("elixir_trophy", 1001))
    result = _resolve_custom_emoji(
        ":poap: Vijay moved up to :poap_kings: Spirit Square",
        guild,
    )
    assert ":poap:" not in result
    assert ":poap_kings:" not in result
    assert "Vijay moved up" in result
    assert "Spirit Square" in result


def test_resolve_custom_emoji_keeps_unicode_shortcodes():
    # :dragon:, :crossed_swords:, :trophy: are standard CLDR Unicode emoji
    # shortcodes — the Discord client renders them on display. Don't strip.
    guild = _guild(("elixir_trophy", 1001))
    result = _resolve_custom_emoji(
        "push :dragon: into Spirit Square :crossed_swords:",
        guild,
    )
    assert ":dragon:" in result
    assert ":crossed_swords:" in result


def test_resolve_custom_emoji_normalizes_elixir_prefixed_unicode_shortcodes():
    guild = _guild(("elixir_trophy", 1001))
    result = _resolve_custom_emoji("battle day :elixir_crossed_swords:", guild)
    assert result == "battle day :crossed_swords:"


def test_resolve_custom_emoji_still_strips_unknown_elixir_prefixed_names():
    guild = _guild(("elixir_trophy", 1001))
    result = _resolve_custom_emoji("battle day :elixir_poap_kings:", guild)
    assert ":elixir_poap_kings:" not in result
    assert result == "battle day"


def test_resolve_custom_emoji_leaves_timestamps_alone():
    # Digit-led ":30:" is not an emoji name; must not be treated like one.
    guild = _guild(("elixir_trophy", 1001))
    assert _resolve_custom_emoji("kickoff at 10:30:45", guild) == "kickoff at 10:30:45"


def test_resolve_custom_emoji_without_guild_keeps_unicode_and_strips_unknown():
    # No guild context = no custom emojis, but Unicode shortcodes still render.
    result = _resolve_custom_emoji("hello :poap: and :dragon: world", None)
    assert ":poap:" not in result
    assert ":dragon:" in result
