from agent.prompts import _deck_review_system, _discord_emoji_guidance, _promote_system
from runtime.emoji import available_emoji_names


def test_promote_system_requires_exact_discord_trophy_text():
    text = _promote_system()

    assert "`discord.body`" in text
    assert "MUST end with the exact text `Required Trophies: [2000]`" in text
    assert "Do not paraphrase that phrase" in text
    assert "`reddit.title` must include the exact token `[2000]` somewhere in the title." in text


def test_promote_system_uses_custom_trophy_threshold():
    text = _promote_system(required_trophies=5000)

    assert "MUST end with the exact text `Required Trophies: [5000]`" in text
    assert "`reddit.title` must include the exact token `[5000]` somewhere in the title." in text
    assert "MUST include the exact token `[2000]`" not in text
    assert "MUST end with the exact text `Required Trophies: [2000]`" not in text


def test_discord_emoji_guidance_enumerates_real_guild_emoji():
    guidance = _discord_emoji_guidance()
    names = available_emoji_names()

    assert names, "expected assets/emoji to ship at least one emoji"
    for name in names:
        assert f":{name}:" in guidance
    assert "Do not invent custom emoji names" in guidance
    assert "The `elixir_` prefix is reserved" in guidance
    assert "not :elixir_crossed_swords:" in guidance
    # Unicode shortcodes like :dragon: / :trophy: do render via the Discord
    # client, so the guidance should call that out as an allowed source.
    assert "Unicode emoji shortcodes" in guidance


def test_deck_review_prompt_distinguishes_ownership_from_played_as():
    text = _deck_review_system("#ask-elixir")

    assert "played_as" in text
    assert "actually deployed in that mode" in text
    assert "signature_cards" in text
    assert "losses cards" in text
    assert "ownership, support, or current slot configuration" in text
    assert "infer battle deployment from them" in text
