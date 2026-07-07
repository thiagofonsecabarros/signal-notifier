from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Symbol:
    ticker: str
    name: str = ""
    asset_type: str = "stock"
    exchange: str = ""


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    transactions: int | None = None

