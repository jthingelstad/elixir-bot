import asyncio
from unittest.mock import AsyncMock, patch

from runtime import elixir_log
from runtime import alerts
from runtime import leader_action_observability


def test_elixir_log_post_event_uses_configured_webhook(monkeypatch):
    monkeypatch.setenv(elixir_log.WEBHOOK_ENV, "https://discord.example/webhook")
    monkeypatch.setenv(elixir_log.USERNAME_ENV, "Elixir Test")

    with patch("runtime.elixir_log.requests.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None

        assert elixir_log.post_event("maintenance complete")

    mock_post.assert_called_once()
    assert mock_post.call_args.args[0] == "https://discord.example/webhook"
    assert mock_post.call_args.kwargs["json"] == {
        "content": "maintenance complete",
        "username": "Elixir Test",
        "allowed_mentions": {"parse": []},
    }


def test_elixir_log_post_event_returns_false_without_webhook(monkeypatch):
    monkeypatch.delenv(elixir_log.WEBHOOK_ENV, raising=False)

    with patch("runtime.elixir_log.requests.post") as mock_post:
        assert not elixir_log.post_event("maintenance complete")

    mock_post.assert_not_called()


def test_alert_admin_prefers_elixir_log_webhook():
    alerts._ALERT_SIGNATURES.clear()

    with (
        patch("runtime.alerts.elixir_log.post_event_async", new=AsyncMock(return_value=True)) as mock_log,
        patch("runtime.alerts.prompts.discord_channels_by_workflow") as mock_channels,
    ):
        sent = asyncio.run(alerts._alert_admin("CR API failed", "cr_api_outage", "sig-1"))

    assert sent is True
    mock_log.assert_awaited_once_with("CR API failed")
    mock_channels.assert_not_called()


def test_alert_admin_strips_mentions_from_elixir_log_webhook():
    alerts._ALERT_SIGNATURES.clear()

    with patch("runtime.alerts.elixir_log.post_event_async", new=AsyncMock(return_value=True)) as mock_log:
        sent = asyncio.run(alerts._alert_admin(
            "King Thing (<@704062105258557511>) CR API failed",
            "cr_api_outage",
            "sig-mentions",
        ))

    assert sent is True
    assert mock_log.await_args.args[0] == "King Thing CR API failed"


def test_leader_action_skip_posts_structured_elixir_log_event():
    with (
        patch("runtime.leader_action_observability.elixir_log.enabled", return_value=True),
        patch("runtime.leader_action_observability.elixir_log.post_event_async", new=AsyncMock(return_value=True)) as mock_post,
    ):
        sent = asyncio.run(leader_action_observability.post_leader_action_skip(
            source="leader_action_candidate_scan",
            action_type="kick_recommendation",
            reason="policy:open_card_backlog:5/5",
            target_player_name="Vijay",
            target_player_tag="#DEF456",
            objective="roster_health",
            rationale="last seen 8 days ago; no war participation",
            signal_types={"member_inactive", "war_idle"},
        ))

    assert sent is True
    content = mock_post.await_args.args[0]
    assert "Leader action not recommended" in content
    assert "Source: `leader_action_candidate_scan`" in content
    assert "Type: `kick_recommendation`" in content
    assert "Target: Vijay (`#DEF456`)" in content
    assert "Reason: `policy:open_card_backlog:5/5`" in content
    assert "Signals: `member_inactive`, `war_idle`" in content
    assert "Evidence: last seen 8 days ago; no war participation" in content
