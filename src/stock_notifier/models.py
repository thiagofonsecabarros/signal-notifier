from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


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


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    snapshot_at: datetime
    price: float
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    day_close: float | None = None
    day_volume: float | None = None
    previous_close: float | None = None
    percent_change: float | None = None
    minute_volume: float | None = None


@dataclass(frozen=True)
class CompanyProfile:
    ticker: str
    name: str = ""
    market: str = ""
    locale: str = ""
    primary_exchange: str = ""
    type: str = ""
    active: bool = True
    currency_name: str = ""
    cik: str = ""
    composite_figi: str = ""
    share_class_figi: str = ""
    sic_code: str = ""
    sic_description: str = ""
    market_cap: float | None = None
    weighted_shares_outstanding: float | None = None
    total_employees: int | None = None
    homepage_url: str = ""
    description: str = ""
    list_date: str = ""
    logo_url: str = ""
    icon_url: str = ""
