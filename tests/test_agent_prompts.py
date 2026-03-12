from agent.prompts import _promote_system


def test_promote_system_requires_exact_discord_trophy_text():
    text = _promote_system()

    assert "`discord.body`" in text
    assert "MUST end with the exact text `Required Trophies: [2000]`" in text
    assert "Do not paraphrase that phrase" in text
    assert "`reddit.title` must include the exact token `[2000]` somewhere in the title." in text
