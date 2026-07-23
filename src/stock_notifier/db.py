from __future__ import annotations

import sqlite3
import json
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from stock_notifier.models import CompanyProfile, DailyBar, MarketSnapshot, PriceTarget, Symbol

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

CREATE TABLE IF NOT EXISTS company_profiles (
    ticker TEXT PRIMARY KEY REFERENCES symbols(ticker),
    name TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    locale TEXT NOT NULL DEFAULT '',
    primary_exchange TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    currency_name TEXT NOT NULL DEFAULT '',
    cik TEXT NOT NULL DEFAULT '',
    composite_figi TEXT NOT NULL DEFAULT '',
    share_class_figi TEXT NOT NULL DEFAULT '',
    sic_code TEXT NOT NULL DEFAULT '',
    sic_description TEXT NOT NULL DEFAULT '',
    market_cap REAL,
    weighted_shares_outstanding REAL,
    total_employees INTEGER,
    homepage_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    list_date TEXT NOT NULL DEFAULT '',
    logo_url TEXT NOT NULL DEFAULT '',
    icon_url TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_profiles_sic
ON company_profiles(sic_description, sic_code);

CREATE INDEX IF NOT EXISTS idx_company_profiles_market_cap
ON company_profiles(market_cap DESC);

CREATE TABLE IF NOT EXISTS symbol_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbol_list_members (
    list_id INTEGER NOT NULL REFERENCES symbol_lists(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL REFERENCES symbols(ticker) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (list_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_symbol_list_members_symbol
ON symbol_list_members(symbol);

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
    frequency_amount INTEGER NOT NULL DEFAULT 15,
    frequency_unit TEXT NOT NULL DEFAULT 'minutes',
    start_time TEXT NOT NULL DEFAULT '09:45',
    timezone TEXT NOT NULL DEFAULT 'America/Toronto',
    market_hours_only INTEGER NOT NULL DEFAULT 1 CHECK (market_hours_only IN (0, 1)),
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

CREATE TABLE IF NOT EXISTS pending_alerts (
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
    score_id INTEGER REFERENCES signal_scores(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'dropped')),
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_alerts_status ON pending_alerts(status, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_alerts_open
ON pending_alerts(signal_name, symbol, direction)
WHERE status='pending';

CREATE TABLE IF NOT EXISTS market_snapshots (
    symbol TEXT PRIMARY KEY REFERENCES symbols(ticker),
    snapshot_at TEXT NOT NULL,
    price REAL NOT NULL,
    day_open REAL,
    day_high REAL,
    day_low REAL,
    day_close REAL,
    day_volume REAL,
    previous_close REAL,
    percent_change REAL,
    minute_volume REAL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_volume
ON market_snapshots(day_volume DESC, price DESC);

CREATE TABLE IF NOT EXISTS market_snapshot_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL REFERENCES symbols(ticker),
    snapshot_at TEXT NOT NULL,
    price REAL NOT NULL,
    percent_change REAL,
    day_volume REAL,
    dollar_volume REAL,
    fetched_at TEXT NOT NULL,
    UNIQUE(symbol, snapshot_at)
);

CREATE INDEX IF NOT EXISTS idx_market_snapshot_history_symbol_time
ON market_snapshot_history(symbol, snapshot_at DESC);

CREATE INDEX IF NOT EXISTS idx_market_snapshot_history_fetched
ON market_snapshot_history(fetched_at DESC);

CREATE TABLE IF NOT EXISTS price_targets_latest (
    symbol TEXT NOT NULL REFERENCES symbols(ticker),
    brokerage TEXT NOT NULL,
    company_name TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    rating TEXT NOT NULL DEFAULT '',
    target_price REAL,
    previous_target_price REAL,
    price_then REAL,
    source_current_price REAL,
    effective_date TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    raw_payload_json TEXT NOT NULL DEFAULT '{}',
    captured_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, brokerage)
);

CREATE INDEX IF NOT EXISTS idx_price_targets_latest_symbol
ON price_targets_latest(symbol, target_price);

CREATE TABLE IF NOT EXISTS price_target_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL REFERENCES symbols(ticker),
    brokerage TEXT NOT NULL,
    company_name TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    rating TEXT NOT NULL DEFAULT '',
    previous_target_price REAL,
    target_price REAL,
    price_then REAL,
    source_current_price REAL,
    effective_date TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    import_source TEXT NOT NULL DEFAULT '',
    raw_payload_json TEXT NOT NULL DEFAULT '{}',
    captured_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_target_events_symbol_date
ON price_target_events(symbol, effective_date DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS scan_cycle_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed', 'skipped')),
    snapshots_fetched INTEGER NOT NULL DEFAULT 0,
    symbols_filtered INTEGER NOT NULL DEFAULT 0,
    symbols_scored INTEGER NOT NULL DEFAULT 0,
    alerts_created INTEGER NOT NULL DEFAULT 0,
    deliveries_attempted INTEGER NOT NULL DEFAULT 0,
    delivered INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS service_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed', 'cancelled')),
    requested_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_service_runs_started
ON service_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
            self._migrate(connection)

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}

    def _migrate(self, connection: sqlite3.Connection) -> None:
        alert_rule_columns = self._columns(connection, "alert_rules")
        alert_rule_additions = {
            "frequency_amount": "INTEGER NOT NULL DEFAULT 15",
            "frequency_unit": "TEXT NOT NULL DEFAULT 'minutes'",
            "start_time": "TEXT NOT NULL DEFAULT '09:45'",
            "timezone": "TEXT NOT NULL DEFAULT 'America/Toronto'",
            "market_hours_only": "INTEGER NOT NULL DEFAULT 1",
        }
        for column, definition in alert_rule_additions.items():
            if column not in alert_rule_columns:
                connection.execute(f"ALTER TABLE alert_rules ADD COLUMN {column} {definition}")

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

    def get_app_setting(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value_json FROM app_settings WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(str(rows[0]["value_json"]))
        except json.JSONDecodeError:
            return default

    def set_app_setting(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, json.dumps(value), datetime.now(UTC).isoformat()),
            )

    def ensure_symbols(self, tickers: Iterable[str]) -> int:
        now = datetime.now(UTC).isoformat()
        rows = [(ticker.upper().strip(), now) for ticker in tickers if ticker.strip()]
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO symbols(ticker, updated_at)
                VALUES (?, ?)
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

    def upsert_market_snapshots(self, snapshots: Iterable[MarketSnapshot]) -> int:
        now = datetime.now(UTC).isoformat()
        rows = [
            (
                snapshot.symbol,
                snapshot.snapshot_at.isoformat(),
                snapshot.price,
                snapshot.day_open,
                snapshot.day_high,
                snapshot.day_low,
                snapshot.day_close,
                snapshot.day_volume,
                snapshot.previous_close,
                snapshot.percent_change,
                snapshot.minute_volume,
                now,
            )
            for snapshot in snapshots
        ]
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO market_snapshots(
                    symbol, snapshot_at, price, day_open, day_high, day_low,
                    day_close, day_volume, previous_close, percent_change,
                    minute_volume, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    snapshot_at=excluded.snapshot_at,
                    price=excluded.price,
                    day_open=excluded.day_open,
                    day_high=excluded.day_high,
                    day_low=excluded.day_low,
                    day_close=excluded.day_close,
                    day_volume=excluded.day_volume,
                    previous_close=excluded.previous_close,
                    percent_change=excluded.percent_change,
                    minute_volume=excluded.minute_volume,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def append_market_snapshot_history(self, snapshots: Iterable[MarketSnapshot]) -> int:
        now = datetime.now(UTC).isoformat()
        rows = []
        for snapshot in snapshots:
            day_volume = snapshot.day_volume or 0
            rows.append(
                (
                    snapshot.symbol,
                    snapshot.snapshot_at.isoformat(),
                    snapshot.price,
                    snapshot.percent_change,
                    snapshot.day_volume,
                    snapshot.price * day_volume,
                    now,
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO market_snapshot_history(
                    symbol, snapshot_at, price, percent_change, day_volume, dollar_volume, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, snapshot_at) DO UPDATE SET
                    price=excluded.price,
                    percent_change=excluded.percent_change,
                    day_volume=excluded.day_volume,
                    dollar_volume=excluded.dollar_volume,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def prune_market_snapshot_history(self, *, keep_hours: float = 10.0) -> int:
        cutoff = datetime.now(UTC) - timedelta(hours=max(float(keep_hours), 0.0))
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM market_snapshot_history WHERE snapshot_at < ?",
                (cutoff.isoformat(),),
            )
            return int(cursor.rowcount or 0)

    def market_snapshot_history_status(self) -> dict[str, Any]:
        rows = self.query(
            """
            SELECT COUNT(*) AS count, MIN(snapshot_at) AS oldest_snapshot_at,
                   MAX(snapshot_at) AS latest_snapshot_at, MAX(fetched_at) AS latest_fetched_at
            FROM market_snapshot_history
            """
        )
        return dict(rows[0]) if rows else {
            "count": 0,
            "oldest_snapshot_at": None,
            "latest_snapshot_at": None,
            "latest_fetched_at": None,
        }

    def upsert_price_targets(
        self,
        targets: Iterable[PriceTarget],
        *,
        import_source: str = "",
        update_latest: bool = True,
    ) -> tuple[int, int]:
        now = datetime.now(UTC).isoformat()
        latest_rows: list[tuple[Any, ...]] = []
        event_rows: list[tuple[Any, ...]] = []
        for target in targets:
            symbol = str(target.symbol or "").upper().strip()
            brokerage = str(target.brokerage or "").strip()
            if not symbol or not brokerage:
                continue
            captured_at = str(target.captured_at or "").strip() or now
            effective_date = str(target.effective_date or "").strip()[:10]
            source_url = str(target.source_url or "").strip()
            raw_json = str(target.raw_payload_json or "{}")
            latest_rows.append(
                (
                    symbol,
                    brokerage,
                    str(target.company_name or "").strip(),
                    str(target.action or "").strip(),
                    str(target.rating or "").strip(),
                    target.target_price,
                    target.previous_target_price,
                    target.price_then,
                    target.source_current_price,
                    effective_date,
                    source_url,
                    raw_json,
                    captured_at,
                    now,
                )
            )
            event_key = "|".join(
                [
                    symbol,
                    brokerage.lower(),
                    effective_date,
                    str(target.action or "").strip().lower(),
                    str(target.rating or "").strip().lower(),
                    "" if target.previous_target_price is None else f"{float(target.previous_target_price):.8f}",
                    "" if target.target_price is None else f"{float(target.target_price):.8f}",
                    captured_at[:19],
                ]
            )
            event_rows.append(
                (
                    event_key,
                    symbol,
                    brokerage,
                    str(target.company_name or "").strip(),
                    str(target.action or "").strip(),
                    str(target.rating or "").strip(),
                    target.previous_target_price,
                    target.target_price,
                    target.price_then,
                    target.source_current_price,
                    effective_date,
                    source_url,
                    import_source,
                    raw_json,
                    captured_at,
                    now,
                )
            )
        if not latest_rows:
            return 0, 0
        with self.connect() as connection:
            if update_latest:
                connection.executemany(
                    """
                    INSERT INTO price_targets_latest(
                        symbol, brokerage, company_name, action, rating, target_price,
                        previous_target_price, price_then, source_current_price,
                        effective_date, source_url, raw_payload_json, captured_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, brokerage) DO UPDATE SET
                        company_name=excluded.company_name,
                        action=excluded.action,
                        rating=excluded.rating,
                        target_price=excluded.target_price,
                        previous_target_price=excluded.previous_target_price,
                        price_then=excluded.price_then,
                        source_current_price=excluded.source_current_price,
                        effective_date=excluded.effective_date,
                        source_url=excluded.source_url,
                        raw_payload_json=excluded.raw_payload_json,
                        captured_at=excluded.captured_at,
                        updated_at=excluded.updated_at
                    """,
                    latest_rows,
                )
            cursor = connection.executemany(
                """
                INSERT OR IGNORE INTO price_target_events(
                    event_key, symbol, brokerage, company_name, action, rating,
                    previous_target_price, target_price, price_then, source_current_price,
                    effective_date, source_url, import_source, raw_payload_json,
                    captured_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                event_rows,
            )
            inserted_events = int(cursor.rowcount or 0)
        return (len(latest_rows) if update_latest else 0), inserted_events

    def price_target_service_status(self) -> dict[str, Any]:
        rows = self.query(
            """
            SELECT COUNT(*) AS latest_count,
                   COUNT(DISTINCT symbol) AS symbol_count,
                   MAX(captured_at) AS latest_captured_at,
                   MAX(effective_date) AS latest_effective_date
            FROM price_targets_latest
            """
        )
        return dict(rows[0]) if rows else {
            "latest_count": 0,
            "symbol_count": 0,
            "latest_captured_at": None,
            "latest_effective_date": None,
        }

    def recent_price_targets(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT symbol, brokerage, company_name, action, rating, target_price,
                   previous_target_price, price_then, source_current_price,
                   effective_date, captured_at
            FROM price_target_events
            ORDER BY captured_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def price_target_averages(self) -> dict[str, float]:
        rows = self.query(
            """
            SELECT symbol, AVG(target_price) AS average_target
            FROM price_targets_latest
            WHERE target_price IS NOT NULL AND target_price > 0
            GROUP BY symbol
            """
        )
        return {str(row["symbol"]): float(row["average_target"]) for row in rows if row["average_target"] is not None}

    def price_targets_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT
                p.symbol,
                p.brokerage,
                p.company_name,
                p.action,
                p.rating,
                p.target_price,
                p.previous_target_price,
                COALESCE(p.price_then, p.source_current_price, then_bar.close) AS price_then,
                p.source_current_price,
                p.effective_date,
                p.source_url,
                p.captured_at,
                COALESCE(m.price, latest_bar.close) AS price_now,
                latest_bar.trading_date AS latest_trading_date,
                reached.reached_date
            FROM price_targets_latest p
            LEFT JOIN market_snapshots m ON m.symbol=p.symbol
            LEFT JOIN (
                SELECT b.symbol, b.trading_date, b.close
                FROM daily_bars b
                JOIN (
                    SELECT symbol, MAX(trading_date) AS trading_date
                    FROM daily_bars
                    GROUP BY symbol
                ) x ON x.symbol=b.symbol AND x.trading_date=b.trading_date
            ) latest_bar ON latest_bar.symbol=p.symbol
            LEFT JOIN daily_bars then_bar
              ON then_bar.symbol=p.symbol
             AND then_bar.trading_date = (
                SELECT MAX(b2.trading_date)
                FROM daily_bars b2
                WHERE b2.symbol=p.symbol
                  AND p.effective_date <> ''
                  AND b2.trading_date <= p.effective_date
             )
            LEFT JOIN (
                SELECT
                    p2.symbol,
                    p2.brokerage,
                    MIN(b.trading_date) AS reached_date
                FROM price_targets_latest p2
                JOIN daily_bars b ON b.symbol=p2.symbol
                WHERE p2.target_price IS NOT NULL
                  AND p2.effective_date <> ''
                  AND b.trading_date >= p2.effective_date
                  AND (
                    (COALESCE(p2.price_then, p2.source_current_price, 0) <= p2.target_price AND b.high >= p2.target_price)
                    OR
                    (COALESCE(p2.price_then, p2.source_current_price, 0) > p2.target_price AND b.low <= p2.target_price)
                  )
                GROUP BY p2.symbol, p2.brokerage
            ) reached ON reached.symbol=p.symbol AND reached.brokerage=p.brokerage
            WHERE p.symbol=?
            ORDER BY p.effective_date DESC, p.brokerage ASC
            """,
            (symbol.upper().strip(),),
        )
        return [dict(row) for row in rows]

    def upsert_company_profile(self, profile: CompanyProfile) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO company_profiles(
                    ticker, name, market, locale, primary_exchange, type, active,
                    currency_name, cik, composite_figi, share_class_figi,
                    sic_code, sic_description, market_cap,
                    weighted_shares_outstanding, total_employees, homepage_url,
                    description, list_date, logo_url, icon_url, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name=excluded.name,
                    market=excluded.market,
                    locale=excluded.locale,
                    primary_exchange=excluded.primary_exchange,
                    type=excluded.type,
                    active=excluded.active,
                    currency_name=excluded.currency_name,
                    cik=excluded.cik,
                    composite_figi=excluded.composite_figi,
                    share_class_figi=excluded.share_class_figi,
                    sic_code=excluded.sic_code,
                    sic_description=excluded.sic_description,
                    market_cap=excluded.market_cap,
                    weighted_shares_outstanding=excluded.weighted_shares_outstanding,
                    total_employees=excluded.total_employees,
                    homepage_url=excluded.homepage_url,
                    description=excluded.description,
                    list_date=excluded.list_date,
                    logo_url=excluded.logo_url,
                    icon_url=excluded.icon_url,
                    updated_at=excluded.updated_at
                """,
                (
                    profile.ticker,
                    profile.name,
                    profile.market,
                    profile.locale,
                    profile.primary_exchange,
                    profile.type,
                    1 if profile.active else 0,
                    profile.currency_name,
                    profile.cik,
                    profile.composite_figi,
                    profile.share_class_figi,
                    profile.sic_code,
                    profile.sic_description,
                    profile.market_cap,
                    profile.weighted_shares_outstanding,
                    profile.total_employees,
                    profile.homepage_url,
                    profile.description[:10000],
                    profile.list_date,
                    profile.logo_url,
                    profile.icon_url,
                    now,
                ),
            )

    def mark_company_profile_unavailable(self, ticker: str, reason: str = "Ticker overview not found") -> None:
        symbol = ticker.upper().strip()
        if not symbol:
            return
        self.ensure_symbols([symbol])
        self.upsert_company_profile(
            CompanyProfile(
                ticker=symbol,
                name=symbol,
                active=False,
                type="unavailable",
                description=reason,
            )
        )

    def symbols_missing_profiles(self, *, limit: int = 100) -> list[str]:
        rows = self.query(
            """
            SELECT s.ticker
            FROM symbols s
            LEFT JOIN company_profiles p ON p.ticker=s.ticker
            WHERE s.active=1 AND p.ticker IS NULL
            ORDER BY s.ticker
            LIMIT ?
            """,
            (limit,),
        )
        return [str(row["ticker"]) for row in rows]

    def count_company_profiles(self) -> int:
        rows = self.query("SELECT COUNT(*) AS count FROM company_profiles")
        return int(rows[0]["count"]) if rows else 0

    def create_symbol_list(self, name: str, description: str = "") -> int:
        list_name = name.strip()
        if not list_name:
            raise ValueError("List name is required")
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO symbol_lists(name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description=COALESCE(NULLIF(excluded.description, ''), symbol_lists.description),
                    updated_at=excluded.updated_at
                RETURNING id
                """,
                (list_name, description.strip(), now, now),
            )
            return int(cursor.fetchone()[0])

    def update_symbol_list(self, list_id: int, *, name: str, description: str = "") -> None:
        list_name = name.strip()
        if not list_name:
            raise ValueError("List name is required")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE symbol_lists
                SET name=?, description=?, updated_at=?
                WHERE id=?
                """,
                (list_name, description.strip(), datetime.now(UTC).isoformat(), list_id),
            )

    def list_symbol_lists(self) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT l.id, l.name, l.description, COUNT(m.symbol) AS symbol_count,
                   l.created_at, l.updated_at
            FROM symbol_lists l
            LEFT JOIN symbol_list_members m ON m.list_id=l.id
            GROUP BY l.id
            ORDER BY l.name
            """
        )
        return [dict(row) for row in rows]

    def delete_symbol_list(self, list_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM symbol_lists WHERE id=?", (list_id,))

    def add_symbols_to_list(self, list_id: int, symbols: Iterable[str]) -> int:
        requested = sorted({symbol.upper().strip() for symbol in symbols if symbol.strip()})
        if not requested:
            return 0
        self.ensure_symbols(requested)
        now = datetime.now(UTC).isoformat()
        rows = [(list_id, symbol, now) for symbol in requested]
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO symbol_list_members(list_id, symbol, added_at)
                VALUES (?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                "UPDATE symbol_lists SET updated_at=? WHERE id=?",
                (now, list_id),
            )
        return len(rows)

    def replace_symbols_in_list(self, list_id: int, symbols: Iterable[str]) -> int:
        requested = sorted({symbol.upper().strip() for symbol in symbols if symbol.strip()})
        if requested:
            self.ensure_symbols(requested)
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("DELETE FROM symbol_list_members WHERE list_id=?", (list_id,))
            if requested:
                rows = [(list_id, symbol, now) for symbol in requested]
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO symbol_list_members(list_id, symbol, added_at)
                    VALUES (?, ?, ?)
                    """,
                    rows,
                )
            connection.execute(
                "UPDATE symbol_lists SET updated_at=? WHERE id=?",
                (now, list_id),
            )
        return len(requested)

    def remove_symbol_from_list(self, list_id: int, symbol: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM symbol_list_members WHERE list_id=? AND symbol=?",
                (list_id, symbol.upper().strip()),
            )

    def symbols_in_list(self, list_id: int) -> list[str]:
        rows = self.query(
            """
            SELECT symbol
            FROM symbol_list_members
            WHERE list_id=?
            ORDER BY symbol
            """,
            (list_id,),
        )
        return [str(row["symbol"]) for row in rows]

    def symbols_for_list_names(self, list_names: Iterable[str]) -> set[str]:
        names = sorted({name.strip() for name in list_names if name.strip()})
        if not names:
            return set()
        placeholders = ", ".join("?" for _ in names)
        rows = self.query(
            f"""
            SELECT DISTINCT m.symbol
            FROM symbol_lists l
            JOIN symbol_list_members m ON m.list_id=l.id
            WHERE l.name IN ({placeholders})
            """,
            tuple(names),
        )
        return {str(row["symbol"]) for row in rows}

    def filtered_snapshot_symbols(
        self,
        *,
        min_price: float,
        min_day_volume: float,
        max_symbols: int,
        symbols: Iterable[str] | None = None,
    ) -> list[str]:
        parameters: list[Any] = [min_price, min_day_volume]
        symbol_clause = ""
        if symbols:
            requested = sorted({symbol.upper() for symbol in symbols})
            if requested:
                placeholders = ", ".join("?" for _ in requested)
                symbol_clause = f" AND s.ticker IN ({placeholders})"
                parameters.extend(requested)
        parameters.append(max_symbols)
        rows = self.query(
            f"""
            SELECT s.ticker
            FROM symbols s
            JOIN market_snapshots m ON m.symbol=s.ticker
            WHERE s.active=1
              AND lower(s.asset_type) IN ('stock', 'common_stock', 'common stock', '')
              AND m.price >= ?
              AND COALESCE(m.day_volume, 0) >= ?
              {symbol_clause}
            ORDER BY (m.price * COALESCE(m.day_volume, 0)) DESC, m.day_volume DESC
            LIMIT ?
            """,
            tuple(parameters),
        )
        return [str(row["ticker"]) for row in rows]

    def load_price_history(
        self,
        symbols: Iterable[str],
        *,
        min_bars: int = 260,
        include_latest_snapshot: bool = False,
    ) -> dict[str, list[sqlite3.Row]]:
        requested = sorted({symbol.upper() for symbol in symbols})
        if not requested:
            return {}
        placeholders = ", ".join("?" for _ in requested)
        row_limit = max(int(min_bars), 1)
        rows = self.query(
            f"""
            WITH ranked AS (
                SELECT symbol, trading_date, open, high, low, close, volume,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                FROM daily_bars
                WHERE symbol IN ({placeholders})
            )
            SELECT symbol, trading_date, open, high, low, close, volume
            FROM ranked
            WHERE rn <= ?
            ORDER BY symbol, trading_date
            """,
            tuple(requested) + (row_limit,),
        )
        grouped: dict[str, list[sqlite3.Row]] = {symbol: [] for symbol in requested}
        for row in rows:
            grouped[str(row["symbol"])].append(row)
        if include_latest_snapshot:
            snapshot_rows = self.query(
                f"""
                SELECT symbol, snapshot_at AS trading_date,
                       COALESCE(day_open, price) AS open,
                       COALESCE(day_high, price) AS high,
                       COALESCE(day_low, price) AS low,
                       price AS close,
                       COALESCE(day_volume, 0) AS volume
                FROM market_snapshots
                WHERE symbol IN ({placeholders})
                """,
                tuple(requested),
            )
            for row in snapshot_rows:
                grouped[str(row["symbol"])].append(row)
        return {symbol: values[-row_limit:] for symbol, values in grouped.items() if values}

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
        frequency_amount: int = 15,
        frequency_unit: str = "minutes",
        start_time: str = "09:45",
        timezone: str = "America/Toronto",
        market_hours_only: bool = True,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        definitions = self.list_signal_definitions(enabled_only=True)
        with self.connect() as connection:
            for definition in definitions:
                connection.execute(
                    """
                    INSERT INTO alert_rules(
                        signal_id, signal_name, enabled, buy_threshold, sell_threshold,
                        cooldown_hours, frequency_amount, frequency_unit, start_time,
                        timezone, market_hours_only, created_at, updated_at
                    )
                    VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        frequency_amount,
                        frequency_unit,
                        start_time,
                        timezone,
                        1 if market_hours_only else 0,
                        now,
                        now,
                    ),
                )
        return len(definitions)

    def list_alert_rules(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = """
            SELECT id, signal_id, signal_name, enabled, buy_threshold, sell_threshold,
                   cooldown_hours, frequency_amount, frequency_unit, start_time, timezone,
                   market_hours_only, created_at, updated_at
            FROM alert_rules
        """
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY signal_name"
        return [dict(row) for row in self.query(sql)]

    def update_alert_rule(
        self,
        rule_id: int,
        *,
        enabled: bool,
        buy_threshold: float,
        sell_threshold: float,
        frequency_amount: int,
        frequency_unit: str,
        start_time: str,
        timezone: str,
        market_hours_only: bool,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE alert_rules
                SET enabled=?, buy_threshold=?, sell_threshold=?,
                    frequency_amount=?, frequency_unit=?, start_time=?,
                    timezone=?, market_hours_only=?, updated_at=?
                WHERE id=?
                """,
                (
                    1 if enabled else 0,
                    buy_threshold,
                    sell_threshold,
                    frequency_amount,
                    frequency_unit,
                    start_time,
                    timezone,
                    1 if market_hours_only else 0,
                    datetime.now(UTC).isoformat(),
                    rule_id,
                ),
            )

    def latest_score_for_signal_symbol(self, signal_name: str, symbol: str) -> dict[str, Any] | None:
        rows = self.query(
            """
            SELECT id AS score_id, signal_id, signal_name, symbol, trading_date, close,
                   score, eligible, message AS score_message, created_at AS scored_at
            FROM signal_scores
            WHERE lower(signal_name)=lower(?) AND symbol=? AND is_latest=1
            ORDER BY id DESC
            LIMIT 1
            """,
            (signal_name, symbol.upper().strip()),
        )
        return dict(rows[0]) if rows else None

    def latest_scores_for_alert_rules(self, signal_names: set[str] | None = None) -> list[dict[str, Any]]:
        signal_filter = ""
        parameters: tuple[Any, ...] = ()
        if signal_names:
            names = sorted({name.strip().lower() for name in signal_names if name.strip()})
            if names:
                placeholders = ", ".join("?" for _ in names)
                signal_filter = f" AND lower(r.signal_name) IN ({placeholders})"
                parameters = tuple(names)
        rows = self.query(
            f"""
            SELECT
                r.id AS alert_rule_id,
                r.signal_id,
                r.signal_name,
                r.buy_threshold,
                r.sell_threshold,
                r.cooldown_hours,
                r.frequency_amount,
                r.frequency_unit,
                r.start_time,
                r.timezone,
                r.market_hours_only,
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
            {signal_filter}
            ORDER BY r.signal_name, s.score DESC
            """,
            parameters,
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

    def upsert_pending_alert(
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
        score_id: int | None,
        message: str,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO pending_alerts(
                    alert_rule_id, signal_id, signal_name, symbol, direction,
                    score, threshold, trading_date, close, score_id, status,
                    message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(signal_name, symbol, direction) WHERE status='pending'
                DO UPDATE SET
                    alert_rule_id=excluded.alert_rule_id,
                    signal_id=excluded.signal_id,
                    score=excluded.score,
                    threshold=excluded.threshold,
                    trading_date=excluded.trading_date,
                    close=excluded.close,
                    score_id=excluded.score_id,
                    message=excluded.message,
                    updated_at=excluded.updated_at
                RETURNING id
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
                    score_id,
                    message[:2000],
                    now,
                    now,
                ),
            )
            return int(cursor.fetchone()[0])

    def pending_alerts_for_rules(self, signal_names: set[str] | None = None) -> list[dict[str, Any]]:
        signal_filter = ""
        parameters: tuple[Any, ...] = ()
        if signal_names:
            names = sorted({name.strip().lower() for name in signal_names if name.strip()})
            if names:
                placeholders = ", ".join("?" for _ in names)
                signal_filter = f" AND lower(p.signal_name) IN ({placeholders})"
                parameters = tuple(names)
        rows = self.query(
            f"""
            SELECT
                p.id,
                p.alert_rule_id,
                p.signal_id,
                p.signal_name,
                p.symbol,
                p.direction,
                p.score,
                p.threshold,
                p.trading_date,
                p.close,
                p.score_id,
                p.message,
                p.created_at,
                p.updated_at,
                r.buy_threshold,
                r.sell_threshold,
                r.frequency_amount,
                r.frequency_unit,
                r.start_time,
                r.timezone,
                r.market_hours_only,
                s.id AS latest_score_id,
                s.score AS latest_score,
                s.eligible AS latest_eligible,
                s.trading_date AS latest_trading_date,
                s.close AS latest_close
            FROM pending_alerts p
            JOIN alert_rules r ON r.id=p.alert_rule_id AND r.enabled=1
            LEFT JOIN signal_scores s
              ON lower(s.signal_name)=lower(p.signal_name)
             AND s.symbol=p.symbol
             AND s.is_latest=1
            WHERE p.status='pending'
            {signal_filter}
            ORDER BY p.updated_at
            """,
            parameters,
        )
        return [dict(row) for row in rows]

    def update_pending_alert_status(self, pending_id: int, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE pending_alerts
                SET status=?, updated_at=?
                WHERE id=?
                """,
                (status, datetime.now(UTC).isoformat(), pending_id),
            )

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

    def start_scan_cycle_run(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_cycle_runs(started_at, status)
                VALUES (?, 'running')
                """,
                (datetime.now(UTC).isoformat(),),
            )
            return int(cursor.lastrowid)

    def finish_scan_cycle_run(
        self,
        run_id: int,
        *,
        status: str,
        snapshots_fetched: int = 0,
        symbols_filtered: int = 0,
        symbols_scored: int = 0,
        alerts_created: int = 0,
        deliveries_attempted: int = 0,
        delivered: int = 0,
        duration_seconds: float = 0.0,
        message: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE scan_cycle_runs
                SET finished_at=?, status=?, snapshots_fetched=?, symbols_filtered=?,
                    symbols_scored=?, alerts_created=?, deliveries_attempted=?,
                    delivered=?, duration_seconds=?, message=?
                WHERE id=?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    status,
                    snapshots_fetched,
                    symbols_filtered,
                    symbols_scored,
                    alerts_created,
                    deliveries_attempted,
                    delivered,
                    duration_seconds,
                    message[:2000],
                    run_id,
                ),
            )

    def recent_scan_cycle_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT started_at, finished_at, status, snapshots_fetched,
                   symbols_filtered, symbols_scored, alerts_created,
                   deliveries_attempted, delivered, duration_seconds, message
            FROM scan_cycle_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def start_service_run(
        self,
        service_name: str,
        *,
        scope: str = "",
        requested_count: int = 0,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO service_runs(service_name, scope, started_at, status, requested_count)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (service_name, scope[:500], datetime.now(UTC).isoformat(), requested_count),
            )
            return int(cursor.lastrowid)

    def finish_service_run(
        self,
        run_id: int,
        *,
        status: str,
        processed_count: int = 0,
        success_count: int = 0,
        skipped_count: int = 0,
        error_count: int = 0,
        duration_seconds: float = 0.0,
        message: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE service_runs
                SET finished_at=?, status=?, processed_count=?, success_count=?,
                    skipped_count=?, error_count=?, duration_seconds=?, message=?
                WHERE id=?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    status,
                    processed_count,
                    success_count,
                    skipped_count,
                    error_count,
                    duration_seconds,
                    message[:2000],
                    run_id,
                ),
            )

    def recent_service_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT service_name, scope, started_at, finished_at, status,
                   requested_count, processed_count, success_count,
                   skipped_count, error_count, duration_seconds, message
            FROM service_runs
            ORDER BY id DESC
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
