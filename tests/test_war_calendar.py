from datetime import datetime, timezone

from storage.war_calendar import war_reset_window_utc, war_signal_date


def test_war_reset_window_just_before_daily_reset():
    started_at, ends_at = war_reset_window_utc("2026-03-14T09:59:59")

    assert started_at == datetime(2026, 3, 13, 10, 0, 0, tzinfo=timezone.utc)
    assert ends_at == datetime(2026, 3, 14, 10, 0, 0, tzinfo=timezone.utc)
    assert war_signal_date("2026-03-14T09:59:59") == "2026-03-13"


def test_war_reset_window_exactly_at_daily_reset():
    started_at, ends_at = war_reset_window_utc("2026-03-14T10:00:00")

    assert started_at == datetime(2026, 3, 14, 10, 0, 0, tzinfo=timezone.utc)
    assert ends_at == datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    assert war_signal_date("2026-03-14T10:00:00") == "2026-03-14"
