import asyncio
from unittest.mock import AsyncMock, patch

from runtime import alerts, elixir_log


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
        patch(
            "runtime.alerts.elixir_log.post_event_async",
            new=AsyncMock(return_value=True),
        ) as mock_log,
        patch("runtime.alerts.prompts.discord_channels_by_workflow") as mock_channels,
    ):
        sent = asyncio.run(alerts._alert_admin("CR API failed", "cr_api_outage", "sig-1"))

    assert sent is True
    mock_log.assert_awaited_once_with("CR API failed")
    mock_channels.assert_not_called()


def test_alert_admin_strips_mentions_from_elixir_log_webhook():
    alerts._ALERT_SIGNATURES.clear()

    with patch(
        "runtime.alerts.elixir_log.post_event_async", new=AsyncMock(return_value=True)
    ) as mock_log:
        sent = asyncio.run(
            alerts._alert_admin(
                "King Thing (<@704062105258557511>) CR API failed",
                "cr_api_outage",
                "sig-mentions",
            )
        )

    assert sent is True
    assert mock_log.await_args.args[0] == "King Thing CR API failed"


def test_discord_post_failure_alert_dedups_then_refires_after_clear():
    alerts._ALERT_SIGNATURES.clear()
    surface = "#actions (leader-action cards)"

    with (
        patch(
            "runtime.alerts.elixir_log.post_event_async", new=AsyncMock(return_value=True)
        ) as mock_log,
        patch("runtime.alerts._admin_mention_ref", return_value="King Thing"),
    ):
        first = asyncio.run(
            alerts.alert_discord_post_failure(surface, "403 Forbidden posting R168.")
        )
        # Same surface+detail again → suppressed (one alert, not one per tick).
        second = asyncio.run(
            alerts.alert_discord_post_failure(surface, "403 Forbidden posting R168.")
        )
        assert first is True
        assert second is False
        assert mock_log.await_count == 1
        assert surface in mock_log.await_args.args[0]

        # A recovery clears the dedup so a later re-break alerts again.
        alerts.clear_discord_post_failure_alert(surface)
        third = asyncio.run(
            alerts.alert_discord_post_failure(surface, "403 Forbidden posting R168.")
        )
        assert third is True
        assert mock_log.await_count == 2

    alerts._ALERT_SIGNATURES.clear()


def test_job_failure_alert_dedups_then_refires_after_success():
    alerts._ALERT_SIGNATURES.clear()

    with (
        patch(
            "runtime.alerts.elixir_log.post_event_async", new=AsyncMock(return_value=True)
        ) as mock_log,
        patch("runtime.alerts._admin_mention_ref", return_value="King Thing"),
    ):
        first = asyncio.run(alerts._maybe_alert_job_failure("db_backup", "disk full"))
        second = asyncio.run(alerts._maybe_alert_job_failure("db_backup", "disk full"))
        assert first is True
        assert second is False  # same job+error → one alert, not one per run
        assert mock_log.await_count == 1
        assert "`db_backup`" in mock_log.await_args.args[0]

        # A later success re-arms the alert (the recovery clear).
        alerts.clear_job_failure_alert("db_backup")
        third = asyncio.run(alerts._maybe_alert_job_failure("db_backup", "disk full"))
        assert third is True
        assert mock_log.await_count == 2

    alerts._ALERT_SIGNATURES.clear()
