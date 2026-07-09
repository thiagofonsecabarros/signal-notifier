from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.models import DailyBar, MarketSnapshot, Symbol
from stock_notifier.notifications.schedule import (
    AlertSchedule,
    is_schedule_due,
    next_eligible_send_at,
)
from stock_notifier.notifications.service import scan_alerts, seed_alert_rules
from stock_notifier.pipeline import run_scan_cycle
from stock_notifier.scoring.service import seed_starter_signals


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        massive_api_key="test",
        db_path=tmp_path / "notifier.db",
        symbols_path=tmp_path / "symbols.txt",
        telegram_bot_token="token",
        telegram_chat_id="123",
        dashboard_base_url="http://example.test",
        alert_dry_run=True,
        scan_max_symbols=2,
        scan_min_price=5,
        scan_min_day_volume=1_000,
        scan_market_hours_only=False,
        scan_lock_path=tmp_path / "scan.lock",
    )


def _bars(symbol: str, count: int = 260) -> list[DailyBar]:
    start = date(2025, 1, 1)
    return [
        DailyBar(
            symbol=symbol,
            trading_date=start + timedelta(days=offset),
            open=99 + offset,
            high=102 + offset,
            low=98 + offset,
            close=100 + offset,
            volume=1_000_000 + offset,
        )
        for offset in range(count)
    ]


def test_schedule_frequency_and_next_send_time():
    schedule = AlertSchedule(
        frequency_amount=15,
        frequency_unit="minutes",
        start_time="09:45",
        timezone="America/Toronto",
        market_hours_only=True,
    )
    now = datetime(2026, 7, 9, 14, 0, tzinfo=UTC)  # 10:00 Toronto

    assert is_schedule_due(now=now, schedule=schedule, last_alerted_at=None)
    assert not is_schedule_due(
        now=now,
        schedule=schedule,
        last_alerted_at=datetime(2026, 7, 9, 13, 50, tzinfo=UTC).isoformat(),
    )
    assert next_eligible_send_at(
        now=now,
        schedule=schedule,
        last_alerted_at=datetime(2026, 7, 9, 13, 50, tzinfo=UTC).isoformat(),
    ) == datetime(2026, 7, 9, 14, 15, tzinfo=UTC)


def test_alert_outside_schedule_is_queued_then_sent_when_due(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.sync_symbols([Symbol("AAPL", "Apple")])
    signal_id = database.upsert_signal_definition("MA Momentum", {"components": []})
    seed_alert_rules(database, settings)
    database.update_alert_rule(
        1,
        enabled=True,
        buy_threshold=75,
        sell_threshold=40,
        frequency_amount=15,
        frequency_unit="minutes",
        start_time="09:45",
        timezone="America/Toronto",
        market_hours_only=True,
    )
    run_id = database.start_signal_run(signal_id, "MA Momentum")
    database.store_signal_scores(
        run_id=run_id,
        signal_id=signal_id,
        signal_name="MA Momentum",
        scores=[
            type(
                "Score",
                (),
                {
                    "symbol": "AAPL",
                    "trading_date": "2026-07-09",
                    "close": 200,
                    "score": 80,
                    "eligible": True,
                    "message": "OK",
                    "components": [],
                },
            )()
        ],
    )

    queued = scan_alerts(database, settings, dry_run=True, now=datetime(2026, 7, 9, 13, 0, tzinfo=UTC))
    sent = scan_alerts(database, settings, dry_run=True, now=datetime(2026, 7, 9, 14, 0, tzinfo=UTC))

    assert queued.queued == 1
    assert queued.alerts_created == 0
    assert sent.alerts_created == 1
    assert database.recent_notification_deliveries()[0]["status"] == "dry_run"


class _Provider:
    def full_market_snapshot(self) -> list[MarketSnapshot]:
        at = datetime(2026, 7, 9, 14, 0, tzinfo=UTC)
        return [
            MarketSnapshot("AAPL", at, price=360, day_volume=2_000_000),
            MarketSnapshot("MSFT", at, price=380, day_volume=3_000_000),
            MarketSnapshot("TINY", at, price=2, day_volume=10_000),
        ]

    def grouped_daily(self, trading_date, symbols):
        return []

    def historical_daily(self, symbol, start, end):
        return []


def test_scan_cycle_filters_snapshots_scores_candidates_and_scans_alerts(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.sync_symbols([Symbol("AAPL", "Apple"), Symbol("MSFT", "Microsoft"), Symbol("TINY", "Tiny")])
    database.upsert_bars([*_bars("AAPL"), *_bars("MSFT"), *_bars("TINY")])
    seed_starter_signals(database)
    seed_alert_rules(database, settings)

    result = run_scan_cycle(
        database,
        _Provider(),
        settings,
        dry_run=True,
        benchmark=True,
    )

    assert result.snapshots_fetched == 3
    assert result.symbols_filtered == 2
    assert result.symbols_scored > 0
    assert "bench snapshots=3" in result.message
