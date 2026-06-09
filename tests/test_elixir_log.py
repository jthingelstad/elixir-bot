import asyncio
from unittest.mock import AsyncMock, patch

from runtime import elixir_log
from runtime import alerts


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
