from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.models import Symbol
from stock_notifier.notifications.service import scan_alerts, seed_alert_rules, send_telegram_test


def _settings(tmp_path: Path, *, dry_run: bool = True) -> Settings:
    return Settings(
        massive_api_key="",
        db_path=tmp_path / "notifier.db",
        symbols_path=tmp_path / "symbols.txt",
        telegram_bot_token="token",
        telegram_chat_id="123",
        dashboard_base_url="http://example.test",
        alert_default_buy_threshold=75,
        alert_default_sell_threshold=40,
        alert_cooldown_hours=12,
        alert_dry_run=dry_run,
    )


def _component() -> SimpleNamespace:
    return SimpleNamespace(
        name="Momentum",
        component_type="price_change_pct",
        mode="score",
        value=8.0,
        passed=True,
        score=90.0,
        weight=1.0,
        contribution=90.0,
        message="5-day price change %: 8.00",
    )


def _store_score(database: Database, *, score: float) -> None:
    run_id = database.start_signal_run(1, "MA Momentum")
    database.store_signal_scores(
        run_id=run_id,
                signal_id=1,
                signal_name="MA Momentum",
                scores=[
            SimpleNamespace(
                symbol="AAPL",
                signal_name="MA Momentum",
                trading_date="2026-07-07",
                close=200.0,
                score=score,
                eligible=True,
                components=[_component()],
                message="OK",
            )
        ],
    )
    database.finish_signal_run(run_id, status="success", symbols_scored=1)


def test_alert_scan_dry_run_creates_alert_once_and_records_delivery(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.sync_symbols([Symbol("AAPL", "Apple")])
    database.upsert_signal_definition("MA Momentum", {"components": []})
    seed_alert_rules(database, settings)
    _store_score(database, score=80)

    first = scan_alerts(database, settings, dry_run=True)
    second = scan_alerts(database, settings, dry_run=True)

    assert first.alerts_created == 1
    assert first.deliveries_attempted == 1
    assert second.alerts_created == 0
    assert len(database.recent_alerts()) == 1
    assert database.recent_notification_deliveries()[0]["status"] == "dry_run"


def test_sell_alert_fires_after_score_drops_below_threshold(tmp_path):
    settings = _settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.sync_symbols([Symbol("AAPL", "Apple")])
    database.upsert_signal_definition("MA Momentum", {"components": []})
    seed_alert_rules(database, settings)

    _store_score(database, score=80)
    scan_alerts(database, settings, dry_run=True)
    _store_score(database, score=35)
    result = scan_alerts(database, settings, dry_run=True)

    alerts = database.recent_alerts()
    assert result.alerts_created == 1
    assert alerts[0]["direction"] == "SELL"


def test_telegram_test_respects_dry_run(tmp_path):
    settings = _settings(tmp_path, dry_run=True)
    database = Database(settings.db_path)
    database.initialize()

    sent = send_telegram_test(database, settings)

    assert sent is False
    deliveries = database.recent_notification_deliveries()
    assert deliveries[0]["status"] == "dry_run"
