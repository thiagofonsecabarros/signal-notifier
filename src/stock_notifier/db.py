from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stock_notifier.models import DailyBar, Symbol

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    ticker TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    exchange TEXT NOT NULL DEFAULT '',
    asset_type TEXT NOT NULL DEFAULT 'stock',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_bars (
    symbol TEXT NOT NULL REFERENCES symbols(ticker),
    trading_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    vwap REAL,
    transactions INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, trading_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(trading_date);

CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    run_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    requested_date TEXT,
    symbols_requested INTEGER NOT NULL DEFAULT 0,
    bars_written INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def sync_symbols(self, symbols: Iterable[Symbol]) -> int:
        now = datetime.now(UTC).isoformat()
        rows = [(s.ticker, s.name, s.exchange, s.asset_type, now) for s in symbols]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO symbols(ticker, name, exchange, asset_type, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name=excluded.name, exchange=excluded.exchange,
                    asset_type=excluded.asset_type, active=1, updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def upsert_bars(self, bars: Iterable[DailyBar]) -> int:
        now = datetime.now(UTC).isoformat()
        rows = [
            (
                b.symbol,
                b.trading_date.isoformat(),
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.vwap,
                b.transactions,
                now,
            )
            for b in bars
        ]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO daily_bars(
                    symbol, trading_date, open, high, low, close, volume,
                    vwap, transactions, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trading_date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume, vwap=excluded.vwap,
                    transactions=excluded.transactions, fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def start_fetch_log(
        self, run_type: str, requested_date: str | None, symbols_requested: int
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO fetch_log(started_at, run_type, status, requested_date, symbols_requested)
                VALUES (?, ?, 'running', ?, ?)
                """,
                (datetime.now(UTC).isoformat(), run_type, requested_date, symbols_requested),
            )
            return int(cursor.lastrowid)

    def finish_fetch_log(
        self,
        log_id: int,
        *,
        status: str,
        bars_written: int,
        errors: int = 0,
        message: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE fetch_log
                SET finished_at=?, status=?, bars_written=?, errors=?, message=?
                WHERE id=?
                """,
                (datetime.now(UTC).isoformat(), status, bars_written, errors, message[:2000], log_id),
            )

    def query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute(sql, parameters).fetchall())

