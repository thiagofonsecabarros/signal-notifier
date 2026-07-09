from __future__ import annotations

from datetime import date
from typing import Protocol

from stock_notifier.models import CompanyProfile, DailyBar, MarketSnapshot


class MarketDataNotAvailableError(RuntimeError):
    """The requested market data is not published for this date yet."""


class MarketDataProvider(Protocol):
    def grouped_daily(self, trading_date: date, symbols: set[str]) -> list[DailyBar]: ...

    def historical_daily(self, symbol: str, start: date, end: date) -> list[DailyBar]: ...

    def full_market_snapshot(self) -> list[MarketSnapshot]: ...

    def ticker_overview(self, symbol: str) -> CompanyProfile | None: ...
