from agent.prompts import _promote_system


def test_promote_system_requires_discord_title_token_at_end():
    text = _promote_system()

    assert "`discord.body`" in text
    assert "must include the exact token `[2000]`" in text
    assert "must appear at the end of the line, not the front" in text
