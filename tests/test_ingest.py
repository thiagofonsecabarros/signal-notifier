from datetime import date

from stock_notifier.db import Database
from stock_notifier.ingest import fetch_grouped_with_lookback
from stock_notifier.models import DailyBar, Symbol
from stock_notifier.providers.base import MarketDataNotAvailableError


class FakeProvider:
    def grouped_daily(self, trading_date, symbols):
        if trading_date == date(2026, 7, 2):
            return [DailyBar("AAPL", trading_date, 200, 205, 199, 203, 1_000)]
        return []

    def historical_daily(self, symbol, start, end):
        return []


class CurrentDateUnavailableProvider(FakeProvider):
    def grouped_daily(self, trading_date, symbols):
        if trading_date == date(2026, 7, 4):
            raise MarketDataNotAvailableError("Attempted to request today's data before end of day")
        return super().grouped_daily(trading_date, symbols)


def test_grouped_fetch_steps_back_over_non_trading_days(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    database.sync_symbols([Symbol("AAPL")])

    actual_date, count = fetch_grouped_with_lookback(
        database, FakeProvider(), {"AAPL"}, date(2026, 7, 4), lookback_days=3
    )

    assert actual_date == date(2026, 7, 2)
    assert count == 1
    assert database.query("SELECT status FROM fetch_log")[0]["status"] == "success"


def test_grouped_fetch_steps_back_when_current_eod_is_not_published(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    database.sync_symbols([Symbol("AAPL")])

    actual_date, count = fetch_grouped_with_lookback(
        database,
        CurrentDateUnavailableProvider(),
        {"AAPL"},
        date(2026, 7, 4),
        lookback_days=3,
    )

    assert actual_date == date(2026, 7, 2)
    assert count == 1
