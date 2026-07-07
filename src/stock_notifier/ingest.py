from __future__ import annotations

import logging
from datetime import date, timedelta

from stock_notifier.db import Database
from stock_notifier.providers.base import MarketDataNotAvailableError, MarketDataProvider

LOGGER = logging.getLogger(__name__)


def fetch_grouped_with_lookback(
    database: Database,
    provider: MarketDataProvider,
    symbols: set[str],
    requested_date: date,
    *,
    lookback_days: int = 7,
) -> tuple[date, int]:
    log_id = database.start_fetch_log("grouped_daily", requested_date.isoformat(), len(symbols))
    try:
        for offset in range(lookback_days + 1):
            candidate = requested_date - timedelta(days=offset)
            try:
                bars = provider.grouped_daily(candidate, symbols)
            except MarketDataNotAvailableError:
                LOGGER.info("Grouped data for %s is not published yet; trying the prior date", candidate)
                continue
            if bars:
                count = database.upsert_bars(bars)
                missing = len(symbols) - count
                status = "success" if missing == 0 else "partial"
                database.finish_fetch_log(
                    log_id,
                    status=status,
                    bars_written=count,
                    errors=missing,
                    message=f"Fetched {candidate}; {missing} watchlist symbols had no grouped bar",
                )
                return candidate, count
            LOGGER.info("No grouped results for %s; trying the prior date", candidate)
        raise RuntimeError(f"No grouped daily data found in {lookback_days + 1} dates")
    except Exception as error:
        database.finish_fetch_log(log_id, status="failed", bars_written=0, errors=1, message=str(error))
        raise


def backfill_symbols(
    database: Database,
    provider: MarketDataProvider,
    symbols: set[str],
    start: date,
    end: date,
) -> tuple[int, int]:
    log_id = database.start_fetch_log("historical_backfill", end.isoformat(), len(symbols))
    written = 0
    errors: list[str] = []
    for symbol in sorted(symbols):
        try:
            written += database.upsert_bars(provider.historical_daily(symbol, start, end))
        except Exception as error:  # one ticker must not abort the remaining universe
            LOGGER.exception("Backfill failed for %s", symbol)
            errors.append(f"{symbol}: {error}")
    status = "success" if not errors else ("partial" if written else "failed")
    database.finish_fetch_log(
        log_id,
        status=status,
        bars_written=written,
        errors=len(errors),
        message="; ".join(errors),
    )
    return written, len(errors)
