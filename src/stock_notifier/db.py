from __future__ import annotations

import sqlite3
import json
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

CREATE TABLE IF NOT EXISTS signal_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signal_definitions(id),
    signal_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    symbols_scored INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS signal_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES signal_runs(id) ON DELETE CASCADE,
    signal_id INTEGER REFERENCES signal_definitions(id),
    signal_name TEXT NOT NULL,
    symbol TEXT NOT NULL REFERENCES symbols(ticker),
    trading_date TEXT,
    close REAL,
    score REAL NOT NULL,
    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
    message TEXT NOT NULL DEFAULT '',
    is_latest INTEGER NOT NULL DEFAULT 1 CHECK (is_latest IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, signal_name, symbol)
);

CREATE INDEX IF NOT EXISTS idx_signal_scores_latest
ON signal_scores(signal_name, is_latest, score DESC);

CREATE TABLE IF NOT EXISTS signal_score_components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    score_id INTEGER NOT NULL REFERENCES signal_scores(id) ON DELETE CASCADE,
    component_name TEXT NOT NULL,
    component_type TEXT NOT NULL,
    mode TEXT NOT NULL,
    value REAL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    component_score REAL NOT NULL,
    weight REAL NOT NULL,
    contribution REAL NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notification_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_type TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signal_definitions(id),
    signal_name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    buy_threshold REAL NOT NULL DEFAULT 75,
    sell_threshold REAL NOT NULL DEFAULT 40,
    cooldown_hours REAL NOT NULL DEFAULT 12,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_rule_id INTEGER REFERENCES alert_rules(id),
    signal_id INTEGER REFERENCES signal_definitions(id),
    signal_name TEXT NOT NULL,
    symbol TEXT NOT NULL REFERENCES symbols(ticker),
    direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
    score REAL NOT NULL,
    threshold REAL NOT NULL,
    trading_date TEXT,
    close REAL,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER REFERENCES alerts(id) ON DELETE CASCADE,
    channel_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('dry_run', 'delivered', 'failed')),
    request_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_state (
    signal_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
    last_score REAL,
    last_alerted_at TEXT,
    last_alert_id INTEGER REFERENCES alerts(id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (signal_name, symbol, direction)
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

    def upsert_signal_definition(
        self,
        name: str,
        config: dict[str, Any],
        *,
        enabled: bool = True,
        description: str = "",
    ) -> int:
        now = datetime.now(UTC).isoformat()
        description = description or str(config.get("description") or "")
        config_json = json.dumps(config, sort_keys=True)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO signal_definitions(name, description, config_json, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description=excluded.description,
                    config_json=excluded.config_json,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                RETURNING id
                """,
                (name, description, config_json, 1 if enabled else 0, now, now),
            )
            return int(cursor.fetchone()[0])

    def list_signal_definitions(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = """
            SELECT id, name, description, config_json, enabled, created_at, updated_at
            FROM signal_definitions
        """
        parameters: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name"
        rows = self.query(sql, parameters)
        return [self._signal_definition_from_row(row) for row in rows]

    def get_signal_definition(self, identifier: str | int) -> dict[str, Any] | None:
        if isinstance(identifier, int) or str(identifier).isdigit():
            rows = self.query(
                """
                SELECT id, name, description, config_json, enabled, created_at, updated_at
                FROM signal_definitions WHERE id=?
                """,
                (int(identifier),),
            )
        else:
            rows = self.query(
                """
                SELECT id, name, description, config_json, enabled, created_at, updated_at
                FROM signal_definitions WHERE lower(name)=lower(?)
                """,
                (str(identifier),),
            )
        return self._signal_definition_from_row(rows[0]) if rows else None

    def delete_signal_definition(self, signal_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM signal_definitions WHERE id=?", (signal_id,))

    def start_signal_run(self, signal_id: int | None, signal_name: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO signal_runs(signal_id, signal_name, started_at, status)
                VALUES (?, ?, ?, 'running')
                """,
                (signal_id, signal_name, datetime.now(UTC).isoformat()),
            )
            return int(cursor.lastrowid)

    def finish_signal_run(
        self,
        run_id: int,
        *,
        status: str,
        symbols_scored: int,
        errors: int = 0,
        message: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE signal_runs
                SET finished_at=?, status=?, symbols_scored=?, errors=?, message=?
                WHERE id=?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    status,
                    symbols_scored,
                    errors,
                    message[:2000],
                    run_id,
                ),
            )

    def load_price_history(
        self,
        symbols: Iterable[str],
        *,
        min_bars: int = 260,
    ) -> dict[str, list[sqlite3.Row]]:
        requested = sorted({symbol.upper() for symbol in symbols})
        if not requested:
            return {}
        placeholders = ", ".join("?" for _ in requested)
        rows = self.query(
            f"""
            SELECT symbol, trading_date, open, high, low, close, volume
            FROM daily_bars
            WHERE symbol IN ({placeholders})
            ORDER BY symbol, trading_date
            """,
            tuple(requested),
        )
        grouped: dict[str, list[sqlite3.Row]] = {symbol: [] for symbol in requested}
        for row in rows:
            grouped[str(row["symbol"])].append(row)
        return {symbol: values[-min_bars:] for symbol, values in grouped.items() if values}

    def active_symbols(self) -> list[str]:
        return [str(row["ticker"]) for row in self.query("SELECT ticker FROM symbols WHERE active=1 ORDER BY ticker")]

    def store_signal_scores(
        self,
        *,
        run_id: int,
        signal_id: int | None,
        signal_name: str,
        scores: Iterable[Any],
    ) -> int:
        now = datetime.now(UTC).isoformat()
        count = 0
        with self.connect() as connection:
            connection.execute(
                "UPDATE signal_scores SET is_latest=0 WHERE signal_name=? AND is_latest=1",
                (signal_name,),
            )
            for scored in scores:
                cursor = connection.execute(
                    """
                    INSERT INTO signal_scores(
                        run_id, signal_id, signal_name, symbol, trading_date, close,
                        score, eligible, message, is_latest, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        run_id,
                        signal_id,
                        signal_name,
                        scored.symbol,
                        scored.trading_date,
                        scored.close,
                        scored.score,
                        1 if scored.eligible else 0,
                        scored.message,
                        now,
                    ),
                )
                score_id = int(cursor.lastrowid)
                component_rows = [
                    (
                        score_id,
                        component.name,
                        component.component_type,
                        component.mode,
                        component.value,
                        1 if component.passed else 0,
                        component.score,
                        component.weight,
                        component.contribution,
                        component.message,
                    )
                    for component in scored.components
                ]
                connection.executemany(
                    """
                    INSERT INTO signal_score_components(
                        score_id, component_name, component_type, mode, value, passed,
                        component_score, weight, contribution, message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    component_rows,
                )
                count += 1
        return count

    def upsert_notification_channel(
        self,
        name: str,
        *,
        channel_type: str = "telegram",
        enabled: bool = True,
        config: dict[str, Any] | None = None,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO notification_channels(
                    channel_type, name, enabled, config_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    channel_type=excluded.channel_type,
                    enabled=excluded.enabled,
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
                RETURNING id
                """,
                (
                    channel_type,
                    name,
                    1 if enabled else 0,
                    json.dumps(config or {}, sort_keys=True),
                    now,
                    now,
                ),
            )
            return int(cursor.fetchone()[0])

    def seed_alert_rules(
        self,
        *,
        buy_threshold: float,
        sell_threshold: float,
        cooldown_hours: float,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        definitions = self.list_signal_definitions(enabled_only=True)
        with self.connect() as connection:
            for definition in definitions:
                connection.execute(
                    """
                    INSERT INTO alert_rules(
                        signal_id, signal_name, enabled, buy_threshold, sell_threshold,
                        cooldown_hours, created_at, updated_at
                    )
                    VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                    ON CONFLICT(signal_name) DO UPDATE SET
                        signal_id=excluded.signal_id,
                        updated_at=excluded.updated_at
                    """,
                    (
                        definition["id"],
                        definition["name"],
                        buy_threshold,
                        sell_threshold,
                        cooldown_hours,
                        now,
                        now,
                    ),
                )
        return len(definitions)

    def list_alert_rules(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = """
            SELECT id, signal_id, signal_name, enabled, buy_threshold, sell_threshold,
                   cooldown_hours, created_at, updated_at
            FROM alert_rules
        """
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY signal_name"
        return [dict(row) for row in self.query(sql)]

    def latest_scores_for_alert_rules(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT
                r.id AS alert_rule_id,
                r.signal_id,
                r.signal_name,
                r.buy_threshold,
                r.sell_threshold,
                r.cooldown_hours,
                s.id AS score_id,
                s.symbol,
                s.trading_date,
                s.close,
                s.score,
                s.eligible,
                s.message AS score_message,
                s.created_at AS scored_at
            FROM alert_rules r
            JOIN signal_scores s
              ON lower(s.signal_name)=lower(r.signal_name)
             AND s.is_latest=1
            WHERE r.enabled=1
            ORDER BY r.signal_name, s.score DESC
            """
        )
        return [dict(row) for row in rows]

    def score_components_for_score(self, score_id: int) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT component_name, component_type, mode, value, passed,
                   component_score, weight, contribution, message
            FROM signal_score_components
            WHERE score_id=?
            ORDER BY id
            """,
            (score_id,),
        )
        return [dict(row) for row in rows]

    def get_alert_state(self, signal_name: str, symbol: str, direction: str) -> dict[str, Any] | None:
        rows = self.query(
            """
            SELECT signal_name, symbol, direction, last_score, last_alerted_at,
                   last_alert_id, updated_at
            FROM alert_state
            WHERE signal_name=? AND symbol=? AND direction=?
            """,
            (signal_name, symbol, direction),
        )
        return dict(rows[0]) if rows else None

    def upsert_alert_state(
        self,
        *,
        signal_name: str,
        symbol: str,
        direction: str,
        last_score: float,
        last_alerted_at: str | None = None,
        last_alert_id: int | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO alert_state(
                    signal_name, symbol, direction, last_score, last_alerted_at,
                    last_alert_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_name, symbol, direction) DO UPDATE SET
                    last_score=excluded.last_score,
                    last_alerted_at=COALESCE(excluded.last_alerted_at, alert_state.last_alerted_at),
                    last_alert_id=COALESCE(excluded.last_alert_id, alert_state.last_alert_id),
                    updated_at=excluded.updated_at
                """,
                (
                    signal_name,
                    symbol,
                    direction,
                    last_score,
                    last_alerted_at,
                    last_alert_id,
                    now,
                ),
            )

    def create_alert(
        self,
        *,
        alert_rule_id: int,
        signal_id: int | None,
        signal_name: str,
        symbol: str,
        direction: str,
        score: float,
        threshold: float,
        trading_date: str | None,
        close: float | None,
        message: str,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO alerts(
                    alert_rule_id, signal_id, signal_name, symbol, direction,
                    score, threshold, trading_date, close, message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_rule_id,
                    signal_id,
                    signal_name,
                    symbol,
                    direction,
                    score,
                    threshold,
                    trading_date,
                    close,
                    message,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def record_notification_delivery(
        self,
        *,
        alert_id: int | None,
        channel_type: str,
        status: str,
        request: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        error_text: str = "",
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO notification_deliveries(
                    alert_id, channel_type, status, request_json, response_json,
                    error_text, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    channel_type,
                    status,
                    json.dumps(request or {}, sort_keys=True),
                    json.dumps(response or {}, sort_keys=True),
                    error_text[:2000],
                    datetime.now(UTC).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT id, created_at, signal_name, symbol, direction, score, threshold,
                   trading_date, close, message
            FROM alerts
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def recent_notification_deliveries(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT d.id, d.created_at, d.alert_id, a.signal_name, a.symbol, a.direction,
                   d.channel_type, d.status, d.error_text
            FROM notification_deliveries d
            LEFT JOIN alerts a ON a.id=d.alert_id
            ORDER BY d.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _signal_definition_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "config": json.loads(row["config_json"]),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
