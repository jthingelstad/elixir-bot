import asyncio
from unittest.mock import patch

from runtime import clan_chat_copy


def test_validate_clan_chat_messages_rejects_discord_artifacts_and_links():
    result = clan_chat_copy.validate_clan_chat_messages(
        ["Copy: **Join** https://poapkings.com <@123>"],
        max_messages=1,
        required_terms=("POAP KINGS",),
    )

    assert "message_1_raw_link" in result.violations
    assert "message_1_discord_mention" in result.violations
    assert "message_1_discord_markdown" in result.violations
    assert "message_1_label" in result.violations
    assert "missing_required:POAP KINGS" in result.violations


def test_validate_clan_chat_messages_enforces_exact_once_route():
    ok = clan_chat_copy.validate_clan_chat_messages(
        ["Join through POAPKINGS . COM > Members."],
        max_messages=1,
        exact_once_terms=(clan_chat_copy.DISCORD_INVITE_ROUTE,),
    )
    repeated = clan_chat_copy.validate_clan_chat_messages(
        ["POAPKINGS . COM > Members", "Again: POAPKINGS . COM > Members"],
        max_messages=2,
        exact_once_terms=(clan_chat_copy.DISCORD_INVITE_ROUTE,),
    )

    assert ok.violations == []
    assert f"not_exactly_once:{clan_chat_copy.DISCORD_INVITE_ROUTE}" in repeated.violations


def test_role_action_clan_chat_copy_uses_public_reason_and_word_boundary():
    copy = clan_chat_copy.role_action_clan_chat_copy(
        action_type="kick_recommendation",
        target_player_name="1spaceO2",
        rationale=(
            "no battle in 8 days, last login 8 days ago (threshold 7.0d at 4914 trophies); "
            "0 donations this week; 0 war races played this season"
        ),
    )

    assert copy == (
        "Removing 1spaceO2 for now: no battle in 8 days, last login 8 days ago; "
        "0 donations this week. - E"
    )
    assert "...." not in copy


def test_sign_clan_chat_text_appends_signature_inside_limit():
    copy = clan_chat_copy.sign_clan_chat_text(
        "POAP KINGS had a huge war push from the middle of the roster tonight.",
        limit=80,
    )

    assert copy.endswith(clan_chat_copy.CLAN_CHAT_SIGNATURE_TEXT)
    assert len(copy) <= 80


def test_generate_clan_chat_copy_uses_fallback_when_llm_violates_guardrails():
    with patch("runtime.clan_chat_copy.elixir_agent.generate_clan_chat_copy", return_value={
        "messages": ["Read more at https://example.com"],
    }) as mock_generate:
        result = asyncio.run(clan_chat_copy.generate_clan_chat_copy(
            intent="weekly_story_relay",
            context="Story context",
            max_messages=1,
            fallback_messages=["POAP KINGS keeps rolling this week."],
        ))

    request = mock_generate.call_args.args[0]
    assert request["target_surface"] == "Clash Royale in-game clan chat"
    assert request["signature"] == {
        "enabled": True,
        "text": clan_chat_copy.CLAN_CHAT_SIGNATURE_TEXT,
        "placement": "append",
    }
    assert result is not None
    assert result.used_fallback is True
    assert result.messages == [f"POAP KINGS keeps rolling this week. {clan_chat_copy.CLAN_CHAT_SIGNATURE_TEXT}"]
