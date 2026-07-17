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
    assert (
        f"not_exactly_once:{clan_chat_copy.DISCORD_INVITE_ROUTE}" in repeated.violations
    )


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


def test_clip_ends_on_complete_sentence_not_mid_word():
    # Two sentences that overflow the limit: the clip should drop the whole
    # trailing sentence and end on the first sentence's period — never a
    # dangling "...word".
    text = (
        "POAP KINGS clinched the war week early. Every deck got played and "
        "the boat is basically home already tonight."
    )
    clipped = clan_chat_copy.clip_clan_chat_text(text, limit=45)
    assert clipped == "POAP KINGS clinched the war week early."
    assert "..." not in clipped


def test_clip_falls_back_to_ellipsis_without_sentence_break():
    # A single run-on with no early sentence break still clips with an ellipsis
    # (the safety net), rather than returning an empty/tiny fragment.
    text = (
        "sniperhendo keeps climbing and climbing and climbing all the way up the ladder"
    )
    clipped = clan_chat_copy.clip_clan_chat_text(text, limit=30)
    assert clipped.endswith("...")
    assert len(clipped) <= 30


def test_relay_copy_keeps_final_word_within_200_regression_r158():
    # R158: the brain authored a complete 181-char line ending "still climbing."
    # The old 180-char relay limit chopped it to "still... - E". At 200 it
    # survives intact, signature and all.
    brain = (
        "sniperhendo just hit a new personal best - 13,750 trophies in Spirit "
        "Square! Up 433 trophies this week alone, vs just 61 the week before. "
        "6 years on this account and still climbing."
    )
    signed = clan_chat_copy.sign_clan_chat_text(
        brain, limit=clan_chat_copy.CLAN_CHAT_DEFAULT_MAX_CHARS
    )
    assert signed.endswith("still climbing. - E")
    assert "still... - E" not in signed
    assert len(signed) <= clan_chat_copy.CLAN_CHAT_DEFAULT_MAX_CHARS


def test_sign_clan_chat_text_appends_signature_inside_limit():
    copy = clan_chat_copy.sign_clan_chat_text(
        "POAP KINGS had a huge war push from the middle of the roster tonight.",
        limit=80,
    )

    assert copy.endswith(clan_chat_copy.CLAN_CHAT_SIGNATURE_TEXT)
    assert len(copy) <= 80


def test_generate_clan_chat_copy_uses_fallback_when_llm_violates_guardrails():
    with patch(
        "runtime.clan_chat_copy.elixir_agent.generate_clan_chat_copy",
        return_value={
            "messages": ["Read more at https://example.com"],
        },
    ) as mock_generate:
        result = asyncio.run(
            clan_chat_copy.generate_clan_chat_copy(
                intent="weekly_story_relay",
                context="Story context",
                max_messages=1,
                fallback_messages=["POAP KINGS keeps rolling this week."],
            )
        )

    request = mock_generate.call_args.args[0]
    assert request["target_surface"] == "Clash Royale in-game clan chat"
    assert request["signature"] == {
        "enabled": True,
        "text": clan_chat_copy.CLAN_CHAT_SIGNATURE_TEXT,
        "placement": "append",
    }
    assert result is not None
    assert result.used_fallback is True
    assert result.messages == [
        f"POAP KINGS keeps rolling this week. {clan_chat_copy.CLAN_CHAT_SIGNATURE_TEXT}"
    ]


def test_signed_valid_messages_accepts_plain_brain_copy():
    out = clan_chat_copy.signed_valid_messages(
        ["Andy just cracked 9,000 trophies, first time — nice climb 13 days in."]
    )
    assert out is not None and len(out) == 1
    assert out[0].endswith(clan_chat_copy.CLAN_CHAT_SIGNATURE_TEXT)
    assert "Andy" in out[0]


def test_signed_valid_messages_accepts_single_string():
    out = clan_chat_copy.signed_valid_messages(
        "War week clinched — nice work everyone."
    )
    assert out is not None and len(out) == 1


def test_signed_valid_messages_rejects_markdown_and_links():
    assert clan_chat_copy.signed_valid_messages(["**Bold** milestone"]) is None
    assert clan_chat_copy.signed_valid_messages(["Join https://poapkings.com"]) is None
    assert clan_chat_copy.signed_valid_messages(["ping <@123> now"]) is None


def test_signed_valid_messages_empty_is_none_and_caps_at_two():
    assert clan_chat_copy.signed_valid_messages([]) is None
    assert clan_chat_copy.signed_valid_messages(["", "   "]) is None
    out = clan_chat_copy.signed_valid_messages(["one", "two", "three"])
    assert out is not None and len(out) == 2
