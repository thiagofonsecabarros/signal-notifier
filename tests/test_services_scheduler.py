from __future__ import annotations

from datetime import UTC, datetime, timedelta

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.services.scheduler import get_service_schedule, is_service_due, save_service_schedule


def _settings(tmp_path):
    return Settings(massive_api_key="", db_path=tmp_path / "notifier.db", symbols_path=tmp_path / "symbols.txt")


def test_service_schedule_persists_and_due_logic_uses_last_run(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()

    save_service_schedule(
        database,
        "snapshot",
        {
            "enabled": True,
            "frequency_amount": 15,
            "frequency_unit": "minutes",
            "start_time": "09:30",
            "timezone": "America/Toronto",
            "notify_telegram": True,
        },
    )

    schedule = get_service_schedule(database, "snapshot")
    assert schedule["enabled"] is True
    assert schedule["frequency_amount"] == 15
    assert schedule["frequency_unit"] == "minutes"
    assert schedule["notify_telegram"] is True

    now = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)  # 10:00 America/Toronto
    assert is_service_due(database, "snapshot", now=now) is True

    database.set_app_setting("dashboard.services.snapshot.last_scheduled_run_at", (now - timedelta(minutes=5)).isoformat())
    assert is_service_due(database, "snapshot", now=now) is False

    database.set_app_setting("dashboard.services.snapshot.last_scheduled_run_at", (now - timedelta(minutes=20)).isoformat())
    assert is_service_due(database, "snapshot", now=now) is True
