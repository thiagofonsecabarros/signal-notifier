from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.notifications.service import scan_alerts, seed_alert_rules, send_sample_alert, send_telegram_test
from stock_notifier.pipeline import run_scan_cycle, scan_cycle_lock
from stock_notifier.notifications.schedule import next_eligible_send_at, parse_schedule
from stock_notifier.providers.massive import MassiveClient
from stock_notifier.scoring.engine import SignalDefinition, evaluate_signal
from stock_notifier.scoring.service import required_history_bars, score_signal, seed_starter_signals
from stock_notifier.services.scheduler import (
    SCHEDULE_UNITS,
    get_service_schedule,
    get_signal_schedule,
    is_service_due,
    is_signal_due,
    run_signal_test_alert,
    save_service_schedule,
    save_signal_schedule,
)

st.set_page_config(page_title="Stock Signal Notifier", layout="wide")
settings = Settings.from_env(require_api_key=False)
database = Database(settings.db_path)
database.initialize()


@st.cache_data(ttl=300, show_spinner=False)
def read_frame(
    query: str,
    parameters: tuple[object, ...] = (),
    db_cache_key: float | None = None,
) -> pd.DataFrame:
    del db_cache_key  # only used to invalidate cache when the SQLite file changes
    if not settings.db_path.exists():
        return pd.DataFrame()
    with sqlite3.connect(settings.db_path) as connection:
        return pd.read_sql_query(query, connection, params=parameters)


DISPLAY_TIMEZONE = ZoneInfo("America/New_York")


def _format_timestamp(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return str(value)
    if getattr(timestamp, "tzinfo", None) is None:
        timestamp = timestamp.tz_localize("UTC")
    timestamp = timestamp.tz_convert(DISPLAY_TIMEZONE)
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


def _format_timestamps(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    formatted = frame.copy()
    timestamp_columns = [
        column
        for column in formatted.columns
        if column.endswith("_at")
        or column in {"created_at", "updated_at", "started_at", "finished_at", "trading_date"}
    ]
    for column in timestamp_columns:
        formatted[column] = formatted[column].map(_format_timestamp)
    return formatted


def _humanize_column_name(column: str) -> str:
    replacements = {
        "id": "ID",
        "url": "URL",
        "api": "API",
        "json": "JSON",
        "pct": "%",
    }
    words = str(column).replace("_", " ").split()
    return " ".join(replacements.get(word.lower(), word.capitalize()) for word in words)


def _display_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    display = _format_timestamps(frame.copy())
    return display.rename(columns={column: _humanize_column_name(column) for column in display.columns})


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _line_value(text: str, label: str) -> str:
    prefix = f"{label}:"
    for line in str(text or "").splitlines():
        if line.strip().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def _notification_type_and_related(row: pd.Series) -> pd.Series:
    request = _json_object(row.get("request_json"))
    response = _json_object(row.get("response_json"))
    kind = str(request.get("kind") or response.get("kind") or "").strip()
    text = str(request.get("text") or "")
    alert_id = row.get("alert_id")
    signal_name = str(row.get("signal_name") or "").strip()
    symbol = str(row.get("symbol") or "").strip()
    direction = str(row.get("direction") or "").strip()

    if pd.notna(alert_id) and str(alert_id).strip() and str(alert_id).strip() != "0":
        related = " · ".join(part for part in [direction, symbol, signal_name] if part)
        return pd.Series({"notification_type": "Signal alert", "related_item": related or f"Alert #{alert_id}"})

    if kind == "scheduled-service" or "Stock Notifier service" in text:
        service_name = str(request.get("service_label") or "").strip()
        if not service_name:
            service_name = _line_value(text, "Service")
        if not service_name:
            service_key = str(request.get("service") or "").strip()
            service_name = {
                "snapshot": "Market snapshot",
                "historical": "Historical data",
                "profiles": "Company profiles",
            }.get(service_key, service_key)
        return pd.Series({"notification_type": "Service run", "related_item": service_name})

    if kind in {"scheduled-signal", "scheduled-signal-digest"} or "Stock Notifier signal schedule" in text:
        scheduled_signal_name = str(request.get("signal_name") or "").strip()
        if not scheduled_signal_name:
            scheduled_signal_name = _line_value(text, "Signal")
        notification_type = "Signal digest" if kind == "scheduled-signal-digest" else "Signal scheduled run"
        return pd.Series({"notification_type": notification_type, "related_item": scheduled_signal_name})

    if kind == "sample-alert":
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        return pd.Series({"notification_type": "Sample alert", "related_item": first_line})

    if kind == "telegram-test" or "Telegram test" in text or "test message" in text.lower():
        return pd.Series({"notification_type": "Telegram test", "related_item": "Configuration test"})

    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return pd.Series({"notification_type": "Telegram message", "related_item": first_line})


def _display_notification_deliveries(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    display = frame.copy()
    context = display.apply(_notification_type_and_related, axis=1)
    display.insert(1, "notification_type", context["notification_type"])
    display.insert(2, "related_item", context["related_item"])
    display = display.drop(columns=["request_json", "response_json"], errors="ignore")
    ordered_columns = [
        "created_at",
        "notification_type",
        "related_item",
        "channel_type",
        "status",
        "alert_id",
        "direction",
        "symbol",
        "signal_name",
        "error_text",
    ]
    display = display[[column for column in ordered_columns if column in display.columns]]
    return _display_history_frame(display)


def _display_market_data_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    display = _format_timestamps(frame.copy())
    return display.rename(
        columns={
            "ticker": "Ticker",
            "name": "Name",
            "asset_type": "Asset type",
            "sic_description": "SIC description",
            "market_cap": "Market cap",
            "trading_date": "Trading date",
            "close": "Close",
            "previous_close": "Previous close",
            "volume": "Volume",
            "daily_change_pct": "Daily change %",
            "dollar_volume": "Dollar volume",
        }
    )


def _percent_color(value: object) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    if numeric > 0:
        return "color: #16a34a; font-weight: 600"
    if numeric < 0:
        return "color: #dc2626; font-weight: 600"
    return ""


def _styled_change_frame(frame: pd.DataFrame) -> object:
    if frame.empty:
        return frame
    change_columns = [
        column
        for column in ["Daily change %", "Change %", "daily_change_pct", "change_pct"]
        if column in frame.columns
    ]
    if not change_columns:
        return frame
    return frame.style.format({column: "{:.2f}%" for column in change_columns}, na_rep="").applymap(
        _percent_color,
        subset=change_columns,
    )


def _colored_percent_html(value: object) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "—"
    color = "#16a34a" if numeric > 0 else "#dc2626" if numeric < 0 else "inherit"
    return f"<span style='color:{color}; font-weight:700'>{numeric:+.2f}%</span>"


st.title("Stock Signal Notifier")
st.caption("Configurable signal scoring · full-market snapshots · scheduled Telegram alerts")


def _latest_watchlist(symbols: set[str] | None = None) -> pd.DataFrame:
    requested_symbols = sorted({symbol.upper().strip() for symbol in symbols or set() if symbol.strip()})
    if symbols is not None and not requested_symbols:
        return pd.DataFrame()
    symbol_filter = ""
    parameters: tuple[object, ...] = ()
    if requested_symbols:
        placeholders = ", ".join("?" for _ in requested_symbols)
        symbol_filter = f" AND s.ticker IN ({placeholders})"
        parameters = tuple(requested_symbols)
    return read_frame(
        f"""
        WITH latest_dates AS (
            SELECT symbol, MAX(trading_date) AS trading_date
            FROM daily_bars
            GROUP BY symbol
        ),
        previous_dates AS (
            SELECT b.symbol, MAX(b.trading_date) AS trading_date
            FROM daily_bars b
            JOIN latest_dates latest
              ON latest.symbol = b.symbol
             AND b.trading_date < latest.trading_date
            GROUP BY b.symbol
        )
        SELECT s.ticker,
               COALESCE(NULLIF(p.name, ''), s.name) AS name,
               s.asset_type,
               p.sic_description,
               p.market_cap,
               COALESCE(m.snapshot_at, current.trading_date) AS trading_date,
               COALESCE(m.price, current.close) AS close,
               m.previous_close,
               COALESCE(m.day_volume, current.volume) AS volume,
               COALESCE(
                   m.percent_change,
                   ROUND(100.0 * (current.close / previous.close - 1.0), 2)
               ) AS daily_change_pct,
               COALESCE(m.price, current.close) * COALESCE(m.day_volume, current.volume, 0) AS dollar_volume
        FROM symbols s
        LEFT JOIN company_profiles p ON p.ticker = s.ticker
        LEFT JOIN market_snapshots m ON m.symbol = s.ticker
        LEFT JOIN latest_dates latest ON latest.symbol = s.ticker
        LEFT JOIN daily_bars current
          ON current.symbol = latest.symbol
         AND current.trading_date = latest.trading_date
        LEFT JOIN previous_dates previous_day ON previous_day.symbol = s.ticker
        LEFT JOIN daily_bars previous
          ON previous.symbol = previous_day.symbol
         AND previous.trading_date = previous_day.trading_date
        WHERE s.active = 1
          {symbol_filter}
        ORDER BY s.ticker
        """,
        parameters,
        db_cache_key=_db_cache_key(),
    )


def _symbol_matches(latest: pd.DataFrame, query: str, limit: int = 8) -> pd.DataFrame:
    columns = ["ticker", "name", "close", "daily_change_pct"]
    if "sic_description" in latest.columns:
        columns.append("sic_description")
    candidates = latest.loc[latest["close"].notna(), columns].copy()
    if candidates.empty:
        return candidates

    normalized_query = query.strip().upper()
    if not normalized_query:
        return candidates.sort_values("ticker").head(limit)

    candidates["ticker_text"] = candidates["ticker"].fillna("").astype(str).str.upper()
    candidates["name_text"] = candidates["name"].fillna("").astype(str).str.upper()
    candidates["match_rank"] = 1000
    candidates.loc[candidates["ticker_text"].eq(normalized_query), "match_rank"] = 0
    candidates.loc[candidates["ticker_text"].str.startswith(normalized_query), "match_rank"] = candidates[
        "match_rank"
    ].clip(upper=1)
    candidates.loc[candidates["ticker_text"].str.contains(normalized_query, regex=False), "match_rank"] = candidates[
        "match_rank"
    ].clip(upper=2)
    candidates.loc[candidates["name_text"].str.startswith(normalized_query), "match_rank"] = candidates[
        "match_rank"
    ].clip(upper=3)
    candidates.loc[candidates["name_text"].str.contains(normalized_query, regex=False), "match_rank"] = candidates[
        "match_rank"
    ].clip(upper=4)

    return (
        candidates.loc[candidates["match_rank"] < 1000]
        .sort_values(["match_rank", "ticker"])
        .head(limit)
        .drop(columns=["ticker_text", "name_text", "match_rank"])
    )


def _global_symbol_matches(query: str, limit: int = 8) -> pd.DataFrame:
    normalized_query = query.strip().upper()
    if not normalized_query:
        return pd.DataFrame()
    like_query = f"%{normalized_query}%"
    starts_query = f"{normalized_query}%"
    return read_frame(
        """
        SELECT s.ticker,
               COALESCE(NULLIF(p.name, ''), s.name) AS name,
               s.asset_type,
               p.sic_description,
               COALESCE(m.price, latest.close) AS close,
               COALESCE(
                   m.percent_change,
                   ROUND(100.0 * (latest.close / previous.close - 1.0), 2)
               ) AS daily_change_pct,
               CASE
                   WHEN upper(s.ticker)=? THEN 0
                   WHEN upper(s.ticker) LIKE ? THEN 1
                   WHEN upper(s.ticker) LIKE ? THEN 2
                   WHEN upper(COALESCE(NULLIF(p.name, ''), s.name)) LIKE ? THEN 3
                   ELSE 4
               END AS match_rank
        FROM symbols s
        LEFT JOIN company_profiles p ON p.ticker=s.ticker
        LEFT JOIN market_snapshots m ON m.symbol=s.ticker
        LEFT JOIN (
            SELECT b.symbol, b.close
            FROM daily_bars b
            JOIN (
                SELECT symbol, MAX(trading_date) AS trading_date
                FROM daily_bars
                GROUP BY symbol
            ) latest_dates
              ON latest_dates.symbol=b.symbol
             AND latest_dates.trading_date=b.trading_date
        ) latest ON latest.symbol=s.ticker
        LEFT JOIN (
            SELECT b.symbol, b.close
            FROM daily_bars b
            JOIN (
                SELECT b2.symbol, MAX(b2.trading_date) AS trading_date
                FROM daily_bars b2
                JOIN (
                    SELECT symbol, MAX(trading_date) AS trading_date
                    FROM daily_bars
                    GROUP BY symbol
                ) latest_dates
                  ON latest_dates.symbol=b2.symbol
                 AND b2.trading_date < latest_dates.trading_date
                GROUP BY b2.symbol
            ) previous_dates
              ON previous_dates.symbol=b.symbol
             AND previous_dates.trading_date=b.trading_date
        ) previous ON previous.symbol=s.ticker
        WHERE s.active=1
          AND (
              upper(s.ticker) LIKE ?
              OR upper(COALESCE(NULLIF(p.name, ''), s.name)) LIKE ?
          )
        ORDER BY match_rank, s.ticker
        LIMIT ?
        """,
        (
            normalized_query,
            starts_query,
            like_query,
            starts_query,
            like_query,
            like_query,
            int(limit),
        ),
        db_cache_key=_db_cache_key(),
    ).drop(columns=["match_rank"], errors="ignore")


def _app_setting(key: str, default: object) -> object:
    return database.get_app_setting(f"dashboard.{key}", default)


def _save_app_settings(values: dict[str, object]) -> None:
    for key, value in values.items():
        database.set_app_setting(f"dashboard.{key}", value)


def _option_index(options: list[str], value: object, default_index: int = 0) -> int:
    try:
        return options.index(str(value))
    except ValueError:
        return default_index


def _active_universe_symbols() -> list[str]:
    rows = database.query("SELECT ticker FROM symbols WHERE active=1 ORDER BY ticker")
    return [str(row["ticker"]) for row in rows]


def _symbols_with_profiles() -> set[str]:
    rows = database.query("SELECT ticker FROM company_profiles")
    return {str(row["ticker"]) for row in rows}


def _profile_scope_symbols(
    *,
    scope: str,
    selected_lists: list[str],
    typed_symbols: str,
) -> list[str]:
    symbols: set[str] = set()
    if scope == "Stocks universe":
        symbols.update(_active_universe_symbols())
    if scope in {"Selected lists", "Lists + typed tickers"} and selected_lists:
        symbols.update(database.symbols_for_list_names(selected_lists))
    if scope in {"Typed tickers", "Lists + typed tickers"}:
        symbols.update(item.strip().upper() for item in typed_symbols.split(",") if item.strip())
    return sorted(symbols)


def _profile_progress_frame(symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    placeholders = ", ".join("?" for _ in symbols)
    return read_frame(
        f"""
        SELECT s.ticker,
               COALESCE(NULLIF(p.name, ''), s.name) AS name,
               p.type AS profile_type,
               p.sic_description,
               p.market_cap,
               p.updated_at AS profile_updated_at
        FROM symbols s
        LEFT JOIN company_profiles p ON p.ticker=s.ticker
        WHERE s.ticker IN ({placeholders})
        ORDER BY s.ticker
        """,
        tuple(symbols),
    )


def _sync_profile_chunk(symbols: list[str], *, requests_per_minute: int) -> dict[str, object]:
    provider = MassiveClient(
        settings.massive_api_key,
        base_url=settings.massive_base_url,
        requests_per_minute=requests_per_minute,
        timeout_seconds=settings.http_timeout_seconds,
    )
    progress = st.progress(0.0, text="Starting profile sync chunk...")
    status = st.empty()
    errors: list[dict[str, str]] = []
    fetched = 0
    unavailable = 0
    started = time.monotonic()

    for index, symbol in enumerate(symbols, start=1):
        progress.progress(index / max(len(symbols), 1), text=f"Fetching {symbol} ({index}/{len(symbols)})")
        status.caption(f"Current symbol: {symbol}")
        try:
            profile = provider.ticker_overview(symbol)
            if profile is None:
                database.mark_company_profile_unavailable(symbol)
                unavailable += 1
                continue
            database.ensure_symbols([profile.ticker])
            database.upsert_company_profile(profile)
            fetched += 1
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})

    duration = time.monotonic() - started
    progress.progress(1.0, text=f"Chunk complete in {duration:.1f}s")
    st.cache_data.clear()
    return {
        "fetched": fetched,
        "unavailable": unavailable,
        "errors": errors,
        "duration": duration,
    }


def _historical_bar_counts(symbols: list[str], start: date, end: date) -> dict[str, int]:
    if not symbols:
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    rows = database.query(
        f"""
        SELECT symbol, COUNT(*) AS bar_count
        FROM daily_bars
        WHERE symbol IN ({placeholders})
          AND trading_date >= ?
          AND trading_date <= ?
        GROUP BY symbol
        """,
        tuple(symbols) + (start.isoformat(), end.isoformat()),
    )
    counts = {str(row["symbol"]): int(row["bar_count"] or 0) for row in rows}
    return {symbol: counts.get(symbol, 0) for symbol in symbols}


def _historical_progress_frame(symbols: list[str], start: date, end: date, expected_bars: int) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    counts = _historical_bar_counts(symbols, start, end)
    placeholders = ", ".join("?" for _ in symbols)
    names = read_frame(
        f"""
        SELECT s.ticker, COALESCE(NULLIF(p.name, ''), s.name) AS name
        FROM symbols s
        LEFT JOIN company_profiles p ON p.ticker=s.ticker
        WHERE s.ticker IN ({placeholders})
        ORDER BY s.ticker
        """,
        tuple(symbols),
    )
    name_map = dict(zip(names.get("ticker", []), names.get("name", []))) if not names.empty else {}
    return pd.DataFrame(
        [
            {
                "ticker": symbol,
                "name": name_map.get(symbol, ""),
                "stored_bars_in_range": counts.get(symbol, 0),
                "target_bars_estimate": expected_bars,
                "coverage_pct": round(100.0 * counts.get(symbol, 0) / max(expected_bars, 1), 1),
            }
            for symbol in symbols
        ]
    )


def _sync_historical_symbols(
    symbols: list[str],
    *,
    start: date,
    end: date,
    requests_per_minute: int,
    chunk_size: int,
) -> dict[str, object]:
    provider = MassiveClient(
        settings.massive_api_key,
        base_url=settings.massive_base_url,
        requests_per_minute=requests_per_minute,
        timeout_seconds=settings.http_timeout_seconds,
    )
    progress = st.progress(0.0, text="Starting historical data backfill...")
    status = st.empty()
    errors: list[dict[str, str]] = []
    symbols_success = 0
    bars_written = 0
    started = time.monotonic()
    total = len(symbols)
    chunk_size = max(int(chunk_size), 1)
    total_chunks = (total + chunk_size - 1) // chunk_size

    for index, symbol in enumerate(symbols, start=1):
        current_chunk = ((index - 1) // chunk_size) + 1
        progress.progress(
            index / max(total, 1),
            text=(
                f"Fetching {symbol} ({index}/{total}); "
                f"chunk {current_chunk}/{total_chunks}"
            ),
        )
        status.caption(f"Current symbol: {symbol}; range {start.isoformat()} to {end.isoformat()}")
        try:
            bars = provider.historical_daily(symbol, start, end)
            database.ensure_symbols([symbol])
            written = database.upsert_bars(bars)
            bars_written += written
            symbols_success += 1
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})

    duration = time.monotonic() - started
    progress.progress(1.0, text=f"Historical backfill run complete in {duration:.1f}s")
    st.cache_data.clear()
    return {
        "symbols_success": symbols_success,
        "bars_written": bars_written,
        "errors": errors,
        "duration": duration,
        "chunks_processed": total_chunks,
    }


def _snapshot_scope_symbols(
    *,
    scope: str,
    selected_lists: list[str],
    typed_symbols: str,
) -> set[str] | None:
    if scope == "Stocks universe":
        return None
    symbols: set[str] = set()
    if scope in {"Selected lists", "Lists + typed tickers"} and selected_lists:
        symbols.update(database.symbols_for_list_names(selected_lists))
    if scope in {"Typed tickers", "Lists + typed tickers"}:
        symbols.update(item.strip().upper() for item in typed_symbols.split(",") if item.strip())
    return symbols


def _snapshot_dollar_volume(snapshot: object) -> float:
    price = float(getattr(snapshot, "price", 0) or 0)
    volume = float(getattr(snapshot, "day_volume", 0) or 0)
    return price * volume


def _snapshot_rows(snapshots: list[object], *, limit: int = 50) -> pd.DataFrame:
    rows = [
        {
            "symbol": getattr(snapshot, "symbol", ""),
            "snapshot_at": getattr(snapshot, "snapshot_at", None),
            "price": getattr(snapshot, "price", None),
            "change_pct": getattr(snapshot, "percent_change", None),
            "previous_close": getattr(snapshot, "previous_close", None),
            "volume": getattr(snapshot, "day_volume", None),
            "dollar_volume": _snapshot_dollar_volume(snapshot),
        }
        for snapshot in snapshots[:limit]
    ]
    return pd.DataFrame(rows)


def _latest_snapshot_status() -> dict[str, object]:
    rows = database.query(
        """
        SELECT COUNT(*) AS count,
               MAX(fetched_at) AS latest_fetched_at,
               MAX(snapshot_at) AS latest_snapshot_at
        FROM market_snapshots
        """
    )
    return dict(rows[0]) if rows else {"count": 0, "latest_fetched_at": None, "latest_snapshot_at": None}


def _db_cache_key() -> float:
    try:
        return settings.db_path.stat().st_mtime
    except OSError:
        return 0.0


@st.cache_data(ttl=300)
def _symbol_history(symbol: str, db_cache_key: float) -> pd.DataFrame:
    del db_cache_key  # only used to invalidate cache when the SQLite file changes
    if not settings.db_path.exists():
        return pd.DataFrame()
    with sqlite3.connect(settings.db_path) as connection:
        history = pd.read_sql_query(
            """
            SELECT trading_date, open, high, low, close, volume
            FROM daily_bars
            WHERE symbol = ?
            ORDER BY trading_date
            """,
            connection,
            params=(symbol,),
        )
    if history.empty:
        return history
    history["trading_date"] = pd.to_datetime(history["trading_date"], errors="coerce")
    history = history.dropna(subset=["trading_date"]).sort_values("trading_date").reset_index(drop=True)
    return history


def _history_for_range(history: pd.DataFrame, range_label: str) -> pd.DataFrame:
    rows_by_range = {
        "Intraday": 1,
        "5D": 5,
        "1W": 5,
        "1M": 23,
        "3M": 66,
        "6M": 132,
        "1Y": 252,
        "3Y": 756,
    }
    row_count = rows_by_range.get(range_label, 66)
    visible = history.tail(row_count).copy().reset_index(drop=True)
    if visible.empty:
        return visible
    visible["trade_index"] = range(len(visible))
    visible["date_label"] = visible["trading_date"].dt.strftime("%Y-%m-%d")
    return visible


def _render_price_volume_chart(history: pd.DataFrame, selected: str) -> None:
    if history.empty:
        st.info("No historical bars available for this symbol yet.")
        return
    base = alt.Chart(history).encode(
        x=alt.X(
            "date_label:O",
            title="Date",
            sort=None,
            axis=alt.Axis(labelAngle=-45, labelOverlap=True, labelLimit=90),
        )
    )
    price = (
        base.mark_line(point=True)
        .encode(
            y=alt.Y("close:Q", title="Close", scale=alt.Scale(zero=False)),
            tooltip=[
                alt.Tooltip("trading_date:T", title="Date", format="%Y-%m-%d"),
                alt.Tooltip("open:Q", title="Open", format="$.2f"),
                alt.Tooltip("high:Q", title="High", format="$.2f"),
                alt.Tooltip("low:Q", title="Low", format="$.2f"),
                alt.Tooltip("close:Q", title="Close", format="$.2f"),
                alt.Tooltip("volume:Q", title="Volume", format=",.0f"),
            ],
        )
        .properties(height=320, title=f"{selected} price")
    )
    volume = (
        base.mark_bar(opacity=0.65)
        .encode(
            y=alt.Y("volume:Q", title="Volume"),
            tooltip=[
                alt.Tooltip("trading_date:T", title="Date", format="%Y-%m-%d"),
                alt.Tooltip("volume:Q", title="Volume", format=",.0f"),
            ],
        )
        .properties(height=130)
    )
    st.altair_chart(alt.vconcat(price, volume).resolve_scale(x="shared"), use_container_width=True)


def _stock_detail_summary(symbol: str) -> dict[str, object] | None:
    rows = database.query(
        """
        SELECT s.ticker,
               COALESCE(NULLIF(p.name, ''), s.name) AS name,
               s.exchange,
               s.asset_type,
               p.market,
               p.locale,
               p.primary_exchange,
               p.type AS profile_type,
               p.active AS profile_active,
               p.currency_name,
               p.sic_code,
               p.sic_description,
               p.market_cap,
               p.total_employees,
               p.homepage_url,
               p.description,
               p.list_date,
               p.updated_at AS profile_updated_at,
               m.snapshot_at,
               m.fetched_at AS snapshot_fetched_at,
               m.price,
               m.day_open,
               m.day_high,
               m.day_low,
               m.day_close,
               m.day_volume,
               m.previous_close,
               m.percent_change,
               m.minute_volume
        FROM symbols s
        LEFT JOIN company_profiles p ON p.ticker=s.ticker
        LEFT JOIN market_snapshots m ON m.symbol=s.ticker
        WHERE s.ticker=?
        LIMIT 1
        """,
        (symbol.upper().strip(),),
    )
    return dict(rows[0]) if rows else None


def _stock_detail_lists(symbol: str) -> pd.DataFrame:
    return read_frame(
        """
        SELECT l.name, l.description, m.added_at
        FROM symbol_list_members m
        JOIN symbol_lists l ON l.id=m.list_id
        WHERE m.symbol=?
        ORDER BY l.name
        """,
        (symbol.upper().strip(),),
        db_cache_key=_db_cache_key(),
    )


def _stock_detail_scores(symbol: str) -> pd.DataFrame:
    return read_frame(
        """
        SELECT id, created_at, signal_name, trading_date, close, score, eligible, message
        FROM signal_scores
        WHERE symbol=? AND is_latest=1
        ORDER BY score DESC, signal_name
        """,
        (symbol.upper().strip(),),
        db_cache_key=_db_cache_key(),
    )


def _stock_detail_components(score_id: int) -> pd.DataFrame:
    return read_frame(
        """
        SELECT component_name, component_type, mode, value, passed,
               component_score, weight, contribution, message
        FROM signal_score_components
        WHERE score_id=?
        ORDER BY id
        """,
        (int(score_id),),
        db_cache_key=_db_cache_key(),
    )


def _stock_detail_alerts(symbol: str) -> pd.DataFrame:
    return read_frame(
        """
        SELECT created_at, direction, signal_name, score, threshold, trading_date, close, message
        FROM alerts
        WHERE symbol=?
        ORDER BY id DESC
        LIMIT 20
        """,
        (symbol.upper().strip(),),
        db_cache_key=_db_cache_key(),
    )


def _refresh_stock_profile(symbol: str) -> str:
    if not settings.massive_api_key:
        return "MASSIVE_API_KEY is missing."
    provider = MassiveClient(
        settings.massive_api_key,
        base_url=settings.massive_base_url,
        requests_per_minute=settings.profile_requests_per_minute,
        timeout_seconds=settings.http_timeout_seconds,
    )
    profile = provider.ticker_overview(symbol)
    if profile is None:
        database.mark_company_profile_unavailable(symbol)
        st.cache_data.clear()
        return f"{symbol} profile was not found; marked unavailable."
    database.ensure_symbols([profile.ticker])
    database.upsert_company_profile(profile)
    st.cache_data.clear()
    return f"{profile.ticker} profile refreshed."


def _render_stock_detail(symbol: str) -> None:
    symbol = symbol.upper().strip()
    summary = _stock_detail_summary(symbol)
    if not summary:
        st.warning(f"{symbol} is not in the current symbol universe.")
        return

    name = str(summary.get("name") or "").strip()
    st.markdown(f"### {symbol}" + (f" · {name}" if name else ""))
    action_cols = st.columns([1, 1, 2])
    with action_cols[0]:
        if st.button("Refresh profile", key=f"refresh_profile_{symbol}", use_container_width=True):
            try:
                st.success(_refresh_stock_profile(symbol))
                st.rerun()
            except Exception as exc:
                st.error(f"Profile refresh failed: {exc}")
    with action_cols[1]:
        if st.button("Close details", key=f"close_details_{symbol}", use_container_width=True):
            st.session_state.market_detail_symbol = None
            st.rerun()
    with action_cols[2]:
        st.markdown(
            f"[TradingView](https://www.tradingview.com/chart/?symbol={symbol}) · "
            f"[Yahoo Finance](https://finance.yahoo.com/quote/{symbol})"
        )

    price = summary.get("price")
    previous_close = summary.get("previous_close")
    day_volume = summary.get("day_volume")
    percent_change = summary.get("percent_change")
    dollar_volume = (float(price or 0) * float(day_volume or 0)) if price is not None and day_volume is not None else None
    metric_cols = st.columns(5)
    metric_cols[0].metric("Price", f"${float(price):,.2f}" if price is not None else "—")
    metric_cols[1].markdown(
        "<div style='font-size:0.875rem; color:rgba(49,51,63,0.7); margin-bottom:0.15rem'>Change</div>"
        f"<div style='font-size:1.75rem; line-height:1.2'>{_colored_percent_html(percent_change)}</div>",
        unsafe_allow_html=True,
    )
    metric_cols[2].metric("Previous close", f"${float(previous_close):,.2f}" if previous_close is not None else "—")
    metric_cols[3].metric("Volume", f"{float(day_volume):,.0f}" if day_volume is not None else "—")
    metric_cols[4].metric("Dollar volume", f"${float(dollar_volume):,.0f}" if dollar_volume is not None else "—")

    meta_cols = st.columns(4)
    meta_cols[0].metric("Asset type", str(summary.get("asset_type") or summary.get("profile_type") or "—"))
    meta_cols[1].metric("Exchange", str(summary.get("primary_exchange") or summary.get("exchange") or "—"))
    market_cap = summary.get("market_cap")
    meta_cols[2].metric("Market cap", f"${float(market_cap):,.0f}" if market_cap is not None else "—")
    meta_cols[3].metric("Sector/SIC", str(summary.get("sic_description") or "—")[:40])
    st.caption(
        f"Snapshot: {_format_timestamp(summary.get('snapshot_at')) or '—'} · "
        f"Fetched: {_format_timestamp(summary.get('snapshot_fetched_at')) or '—'} · "
        f"Profile: {_format_timestamp(summary.get('profile_updated_at')) or '—'}"
    )

    description = str(summary.get("description") or "").strip()
    if description:
        with st.expander("Company description", expanded=False):
            st.write(description)
            homepage = str(summary.get("homepage_url") or "").strip()
            if homepage:
                st.markdown(f"[Company website]({homepage})")

    st.markdown("#### Symbol chart")
    st.caption("Chart history loads automatically for the opened stock detail and is cached per symbol.")
    range_options = ["Intraday", "5D", "1W", "1M", "3M", "6M", "1Y", "3Y"]
    selected_range = st.radio(
        "Range",
        range_options,
        horizontal=True,
        index=3,
        label_visibility="collapsed",
        key=f"detail_chart_range_{symbol}",
    )
    with st.spinner(f"Loading chart history for {symbol}..."):
        history = _symbol_history(symbol, _db_cache_key())
        visible_history = _history_for_range(history, selected_range)
    if selected_range == "Intraday":
        st.info("Intraday data is not enabled yet, so this shows the latest available daily bar for now.")
    _render_price_volume_chart(visible_history, symbol)

    list_frame = _stock_detail_lists(symbol)
    score_frame = _stock_detail_scores(symbol)
    alert_frame = _stock_detail_alerts(symbol)
    cols = st.columns(3)
    with cols[0]:
        st.markdown("#### Lists")
        if list_frame.empty:
            st.info("Not in any custom list.")
        else:
            st.dataframe(_display_history_frame(list_frame), use_container_width=True, hide_index=True)
    with cols[1]:
        st.markdown("#### Latest signal scores")
        if score_frame.empty:
            st.info("No latest scores for this symbol.")
        else:
            display_scores = score_frame.drop(columns=["id"], errors="ignore")
            st.dataframe(_display_history_frame(display_scores), use_container_width=True, hide_index=True)
    with cols[2]:
        st.markdown("#### Recent alerts")
        if alert_frame.empty:
            st.info("No recent alerts for this symbol.")
        else:
            st.dataframe(_display_history_frame(alert_frame), use_container_width=True, hide_index=True)

    if not score_frame.empty:
        score_options = [
            f"{row['signal_name']} · {float(row['score']):.1f}"
            for row in score_frame.to_dict("records")
        ]
        selected_score_label = st.selectbox("Score component breakdown", score_options, key=f"detail_score_{symbol}")
        selected_index = score_options.index(selected_score_label)
        score_id = int(score_frame.iloc[selected_index]["id"])
        components = _stock_detail_components(score_id)
        if components.empty:
            st.info("No component breakdown saved for this score.")
        else:
            st.dataframe(_display_component_breakdown_frame(components.to_dict("records")), use_container_width=True, hide_index=True)


def _list_member_frame(list_id: int, latest: pd.DataFrame) -> pd.DataFrame:
    members = database.symbols_in_list(list_id)
    if not members:
        return pd.DataFrame()
    columns = [
        column
        for column in [
            "ticker",
            "name",
            "sic_description",
            "market_cap",
            "close",
            "previous_close",
            "volume",
            "daily_change_pct",
            "dollar_volume",
        ]
        if column in latest.columns
    ]
    member_frame = latest.loc[latest["ticker"].isin(members), columns].copy()
    visible_members = set(member_frame["ticker"].tolist()) if not member_frame.empty and "ticker" in member_frame else set()
    missing_members = sorted(set(members) - visible_members)
    if missing_members:
        member_frame = pd.concat([member_frame, pd.DataFrame({"ticker": missing_members})], ignore_index=True)
    return member_frame.sort_values("ticker") if "ticker" in member_frame.columns else member_frame


def _render_signal_list_builder() -> None:
    definitions = database.list_signal_definitions()
    lists = database.list_symbol_lists()
    with st.expander("Create or update a list from a signal", expanded=False):
        st.caption(
            "Use a saved signal as a filter/ranker, then write the matching symbols into a reusable list. "
            "For a liquidity list, create a signal with liquidity gates such as Dollar volume >= your threshold."
        )
        if not definitions:
            st.info("Create and save a signal first, then return here to build a list from it.")
            return

        with st.form("create_list_from_signal"):
            signal_options = [f"{item['id']}: {item['name']}" for item in definitions]
            selected_signal = st.selectbox("Signal", signal_options)
            selected_signal_id = int(selected_signal.split(":", 1)[0])

            target_mode = st.radio(
                "Target list",
                ["Create new list", "Use existing list"],
                horizontal=True,
                help="Create a new list or write into one you already have.",
            )
            target_list_id: int | None = None
            existing_list_options = [f"{item['id']}: {item['name']} ({item['symbol_count']})" for item in lists]
            if target_mode == "Use existing list" and existing_list_options:
                selected_target = st.selectbox("Existing list", existing_list_options)
                target_list_id = int(selected_target.split(":", 1)[0])
                target_name = selected_target.split(":", 1)[1].rsplit("(", 1)[0].strip()
            elif target_mode == "Use existing list":
                st.info("No existing lists yet. Choose Create new list.")
                target_name = ""
            else:
                target_name = st.text_input("New list name", placeholder="Liquid stocks, Momentum candidates...")

            description = st.text_input(
                "List description",
                placeholder="Optional note, e.g. Built from Liquidity signal.",
            )
            candidate_source = st.radio(
                "Candidate source",
                ["Market snapshot by dollar volume", "Signal universe"],
                horizontal=True,
                help=(
                    "Market snapshot is faster for large universes and ranks candidates by dollar volume before scoring. "
                    "Signal universe follows the signal's own universe/list settings."
                ),
            )
            source_columns = st.columns(3)
            max_candidates = source_columns[0].number_input(
                "Max candidates to score",
                min_value=10,
                max_value=20000,
                value=1000,
                step=50,
                help="Safety cap before scoring. Increase slowly on the small Oracle VM.",
            )
            min_price = source_columns[1].number_input(
                "Snapshot min price",
                min_value=0.0,
                value=0.0,
                step=1.0,
                help="Only used with Market snapshot source.",
            )
            min_volume = source_columns[2].number_input(
                "Snapshot min volume",
                min_value=0.0,
                value=0.0,
                step=100000.0,
                help="Only used with Market snapshot source.",
            )

            filter_columns = st.columns(4)
            eligible_only = filter_columns[0].checkbox("Eligible only", value=True)
            min_score = filter_columns[1].number_input("Minimum score", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
            max_symbols = filter_columns[2].number_input("Max symbols in list", min_value=1, max_value=20000, value=500, step=25)
            replace_existing = filter_columns[3].checkbox(
                "Replace list members",
                value=True,
                help="If off, matching symbols are appended to the target list.",
            )
            include_latest_snapshot = st.checkbox(
                "Include latest market snapshot in scoring",
                value=True,
                help="Recommended for intraday/liquidity-style lists because daily bars alone may not contain today's snapshot volume/price.",
            )

            submitted = st.form_submit_button("Build list from signal", type="primary", use_container_width=True)

        if not submitted:
            return

        try:
            signal_row = database.get_signal_definition(selected_signal_id)
            if not signal_row:
                st.error("Selected signal was not found.")
                return
            candidate_symbols: set[str] | None = None
            if candidate_source == "Market snapshot by dollar volume":
                candidate_symbols = set(
                    database.filtered_snapshot_symbols(
                        min_price=float(min_price),
                        min_day_volume=float(min_volume),
                        max_symbols=int(max_candidates),
                    )
                )
                if not candidate_symbols:
                    st.error("No snapshot candidates matched those filters. Run the Market snapshot service first or loosen the filters.")
                    return

            results = score_signal(
                database,
                signal_row,
                symbols=candidate_symbols,
                store=False,
                include_latest_snapshot=bool(include_latest_snapshot),
            )
            filtered = [
                item
                for item in results
                if (item.eligible or not eligible_only) and float(item.score) >= float(min_score)
            ]
            filtered.sort(key=lambda item: (float(item.score), item.symbol), reverse=True)
            selected_symbols = [item.symbol for item in filtered[: int(max_symbols)]]
            if not selected_symbols:
                st.warning("The signal ran, but no symbols matched the list filters.")
                return

            if target_mode == "Create new list":
                if not str(target_name).strip():
                    st.error("New list name is required.")
                    return
                target_list_id = database.create_symbol_list(str(target_name), description)
            elif target_list_id is None:
                st.error("Choose an existing list or create a new one.")
                return

            if replace_existing:
                written = database.replace_symbols_in_list(int(target_list_id), selected_symbols)
                action = "replaced"
            else:
                written = database.add_symbols_to_list(int(target_list_id), selected_symbols)
                action = "added"

            st.success(
                f"List updated: {written} symbols {action} from {len(results)} scored symbols "
                f"({len(filtered)} matched filters)."
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "symbol": item.symbol,
                            "score": item.score,
                            "eligible": item.eligible,
                            "close": item.close,
                            "trading_date": item.trading_date,
                            "message": item.message,
                        }
                        for item in filtered[:50]
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            st.cache_data.clear()
        except Exception as exc:
            st.error(f"Could not build list from signal: {exc}")


def _render_lists_tab() -> None:
    st.subheader("Lists")
    st.caption(
        "Create reusable stock groups such as Portfolio, Potential, or Sector Watch. "
        "These lists can be viewed in Market Data and used as Signal Builder universes."
    )
    latest = _latest_watchlist()
    lists = database.list_symbol_lists()

    st.markdown("#### Existing lists")
    if lists:
        st.dataframe(_format_timestamps(pd.DataFrame(lists)), use_container_width=True, hide_index=True)
    else:
        st.info("No custom lists yet.")

    st.markdown("#### Create/Manage Lists")
    list_options = ["Create New List"] + [f"{item['id']}: {item['name']} ({item['symbol_count']})" for item in lists]
    pending_list_selection = st.session_state.pop("lists_manage_pending_selection", None)
    if pending_list_selection in list_options:
        st.session_state.lists_manage_selection = pending_list_selection
    if st.session_state.get("lists_manage_selection") not in list_options:
        st.session_state.lists_manage_selection = "Create New List"
    selected_list_option = st.selectbox("List", list_options, key="lists_manage_selection")

    if selected_list_option == "Create New List":
        with st.form("create_symbol_list_full_width"):
            new_list_name = st.text_input("Name", placeholder="Portfolio, Potential, Liquid Universe...")
            new_list_description = st.text_input("Description", placeholder="Optional")
            if st.form_submit_button("Create list", type="primary", use_container_width=True):
                try:
                    created_id = database.create_symbol_list(new_list_name, new_list_description)
                    st.success(f"List saved: {new_list_name}")
                    st.session_state.lists_manage_pending_selection = next(
                        (
                            f"{item['id']}: {item['name']} ({item['symbol_count']})"
                            for item in database.list_symbol_lists()
                            if int(item["id"]) == int(created_id)
                        ),
                        "Create New List",
                    )
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not create list: {exc}")

        _render_signal_list_builder()
        return

    selected_list_id = int(selected_list_option.split(":", 1)[0])
    selected_list = next(item for item in lists if int(item["id"]) == selected_list_id)

    st.markdown("#### List details")
    with st.form(f"edit_symbol_list_{selected_list_id}"):
        edited_name = st.text_input("Name", value=str(selected_list["name"]))
        edited_description = st.text_input("Description", value=str(selected_list["description"] or ""))
        if st.form_submit_button("Save list details", use_container_width=True):
            try:
                database.update_symbol_list(
                    selected_list_id,
                    name=edited_name,
                    description=edited_description,
                )
                st.session_state.lists_manage_pending_selection = next(
                    (
                        f"{item['id']}: {item['name']} ({item['symbol_count']})"
                        for item in database.list_symbol_lists()
                        if int(item["id"]) == int(selected_list_id)
                    ),
                    "Create New List",
                )
                st.success("List updated.")
                st.cache_data.clear()
                st.rerun()
            except Exception as exc:
                st.error(f"Could not update list: {exc}")

    st.markdown("#### Add symbols")
    manual_symbols = st.text_input(
        "Tickers",
        placeholder="AAPL, MSFT, NVDA",
        help="Comma-separated tickers to add to the selected list.",
    )
    add_columns = st.columns(2)
    with add_columns[0]:
        if st.button("Add typed tickers", use_container_width=True):
            tickers = [item.strip().upper() for item in manual_symbols.split(",") if item.strip()]
            added = database.add_symbols_to_list(selected_list_id, tickers)
            st.success(f"Added {added} ticker entries.")
            st.cache_data.clear()
            st.rerun()
    with add_columns[1]:
        selected_chart_symbol = st.session_state.get("market_selected_symbol")
        if st.button(
            "Add selected chart symbol",
            use_container_width=True,
            disabled=not bool(selected_chart_symbol),
            help="Adds the symbol currently selected in Market Data.",
        ):
            database.add_symbols_to_list(selected_list_id, [str(selected_chart_symbol)])
            st.success(f"Added {selected_chart_symbol}.")
            st.cache_data.clear()
            st.rerun()

    if not latest.empty:
        search_query = st.text_input("Search universe to add", placeholder="Type ticker or company name")
        matches = _symbol_matches(latest, search_query, limit=12) if search_query.strip() else pd.DataFrame()
        if not matches.empty:
            match_options = [f"{row['ticker']} · {row.get('name') or ''}".strip() for _, row in matches.iterrows()]
            selected_match = st.selectbox("Closest matches", match_options)
            matched_symbol = selected_match.split("·", 1)[0].strip()
            if st.button(f"Add {matched_symbol}", use_container_width=True):
                database.add_symbols_to_list(selected_list_id, [matched_symbol])
                st.success(f"Added {matched_symbol}.")
                st.cache_data.clear()
                st.rerun()

    st.markdown("#### Members")
    member_frame = _list_member_frame(selected_list_id, latest)
    if member_frame.empty:
        st.info("This list is empty.")
    else:
        st.dataframe(
            member_frame,
            use_container_width=True,
            hide_index=True,
            column_config={
                "close": st.column_config.NumberColumn(format="$%.2f"),
                "previous_close": st.column_config.NumberColumn("Previous close", format="$%.2f"),
                "volume": st.column_config.NumberColumn(format="%.0f"),
                "daily_change_pct": st.column_config.NumberColumn("Change %", format="%.2f%%"),
                "dollar_volume": st.column_config.NumberColumn("Dollar volume", format="$%.0f"),
                "market_cap": st.column_config.NumberColumn("Market cap", format="$%.0f"),
            },
        )
        remove_symbol = st.selectbox("Remove ticker", database.symbols_in_list(selected_list_id))
        if st.button("Remove selected ticker", use_container_width=True):
            database.remove_symbol_from_list(selected_list_id, remove_symbol)
            st.cache_data.clear()
            st.rerun()

    _render_signal_list_builder()

    st.markdown("#### Danger zone")
    if st.button("Delete list", type="secondary", use_container_width=True):
        database.delete_symbol_list(selected_list_id)
        st.session_state.lists_manage_pending_selection = "Create New List"
        st.cache_data.clear()
        st.rerun()


def _market_view_options() -> list[dict[str, object]]:
    universe_count = len(database.active_symbols())
    options: list[dict[str, object]] = [
        {
            "kind": "universe",
            "label": "Stocks universe",
            "display_label": f"Stocks universe ({universe_count:,} stocks)",
            "list_id": None,
            "symbol_count": universe_count,
        }
    ]
    options.extend(
        {
            "kind": "list",
            "label": str(item["name"]),
            "display_label": f"{item['name']} ({int(item['symbol_count'] or 0):,} stocks)",
            "list_id": int(item["id"]),
            "symbol_count": int(item["symbol_count"] or 0),
        }
        for item in database.list_symbol_lists()
    )
    return options


def _select_market_view() -> dict[str, object]:
    options = _market_view_options()
    labels = [str(option["label"]) for option in options]
    persisted_label = str(_app_setting("market_data.selected_view_label", labels[0]))
    selected_label = str(st.session_state.get("market_view_label", persisted_label))
    if selected_label not in labels:
        selected_label = labels[0]
        st.session_state.market_view_label = selected_label
        database.set_app_setting("dashboard.market_data.selected_view_label", selected_label)
    generation = int(st.session_state.get("market_view_generation", 0))

    st.caption("Choose one view:")
    columns = st.columns(min(4, len(options)))
    checked_labels: list[str] = []
    for index, option in enumerate(options):
        label = labels[index]
        display_label = str(option.get("display_label") or label)
        key = f"market_view_checkbox_{generation}_{index}"
        if key not in st.session_state:
            st.session_state[key] = label == selected_label
        with columns[index % len(columns)]:
            checked = st.checkbox(display_label, key=key)
        if checked:
            checked_labels.append(label)

    if not checked_labels:
        st.session_state.market_view_generation = generation + 1
        st.rerun()

    next_label = selected_label
    for label in checked_labels:
        if label != selected_label:
            next_label = label
            break

    if next_label != selected_label or len(checked_labels) > 1:
        st.session_state.market_view_label = next_label
        database.set_app_setting("dashboard.market_data.selected_view_label", next_label)
        st.session_state.market_view_generation = generation + 1
        if next_label != selected_label:
            st.rerun()

    return options[labels.index(selected_label)]


def _render_stock_lookup() -> None:
    st.markdown("#### Stock lookup")
    lookup_cols = st.columns([2, 1])
    detail_query = lookup_cols[0].text_input(
        "Search any symbol or company",
        placeholder="Type a ticker or company name, e.g. NVDA or Nvidia",
        key="market_detail_search",
        help="Searches the full active Stocks universe, not only the currently selected table/list view.",
    )
    detail_matches = _global_symbol_matches(detail_query, limit=8) if detail_query.strip() else pd.DataFrame()
    if detail_query.strip() and detail_matches.empty:
        lookup_cols[1].warning("No matching active symbols.")
    elif not detail_matches.empty:
        lookup_cols[1].caption("Open closest matches")
        match_columns = st.columns(4)
        for index, row in detail_matches.reset_index(drop=True).iterrows():
            ticker = str(row["ticker"])
            name = str(row.get("name") or "").strip()
            label = ticker if not name else f"{ticker} · {name[:28]}"
            with match_columns[index % len(match_columns)]:
                if st.button(label, key=f"detail_match_{ticker}", use_container_width=True):
                    st.session_state.market_detail_symbol = ticker
                    st.session_state.market_selected_symbol = ticker
                    st.rerun()


COMPONENT_LABELS = {
    "price_vs_sma": "Close vs SMA",
    "price_vs_ema": "Close vs EMA",
    "sma_crossover": "SMA crossover / stack",
    "ema_crossover": "EMA crossover / stack",
    "adx": "ADX trend strength",
    "volume_ratio": "Relative volume",
    "latest_volume": "Latest volume",
    "dollar_volume": "Dollar volume",
    "price_change_pct": "Price change %",
}

COMPONENT_HELP = {
    "price_vs_sma": "Value is how far the latest close is above/below the selected SMA, in %. Example: threshold 0 means close must be above the SMA.",
    "price_vs_ema": "Value is how far the latest close is above/below the selected EMA, in %. EMA reacts faster than SMA.",
    "sma_crossover": "Value is how far the fast SMA is above/below the slow SMA, in %. Example: fast 5, slow 20, threshold 0 means SMA5 above SMA20.",
    "ema_crossover": "Value is how far the fast EMA is above/below the slow EMA, in %. EMA crossover is more reactive than SMA crossover.",
    "adx": "Value is ADX. Higher means stronger trend; it does not say bullish/bearish by itself. Common trend threshold: 20–25.",
    "volume_ratio": "Value is current volume divided by average volume for the period. Example: 2.0 means twice normal volume.",
    "latest_volume": "Value is the latest volume. In scan cycles, this is the current day volume from the latest snapshot.",
    "dollar_volume": "Value is latest close multiplied by latest volume. This is usually the best liquidity filter because it adjusts for stock price.",
    "price_change_pct": "Value is percent price change over the selected number of daily bars. Example: 5 means +5%.",
}

COMPONENT_EXAMPLES = {
    "price_vs_sma": "Gate: Close above SMA50 → Op >=, threshold 0, period 50. Score: price extension → score min 0, score max 10.",
    "price_vs_ema": "Gate: Close above EMA20 → Op >=, threshold 0, period 20. Useful for faster trend checks.",
    "sma_crossover": "Gate: SMA5 above SMA20 → Op >=, threshold 0, period 5, slow period 20.",
    "ema_crossover": "Gate: EMA8 above EMA21 → Op >=, threshold 0, period 8, slow period 21.",
    "adx": "Gate: Trend exists → Op >=, threshold 25, period 14. Score: stronger trend → score min 15, score max 35.",
    "volume_ratio": "Gate: Volume breakout → Op >=, threshold 2.0, period 20. Score: score min 1.0, score max 3.0.",
    "latest_volume": "Gate: At least 100k shares → Op >=, threshold 100000.",
    "dollar_volume": "Gate: At least $100k traded → Op >=, threshold 100000. Example: price $10 and volume 10,000 = $100,000.",
    "price_change_pct": "Score: 5-day momentum → period 5, score min 0, score max 8. Gate: require positive change → Op >=, threshold 0.",
}

COMPONENTS_WITH_PERIOD = {
    "price_vs_sma",
    "price_vs_ema",
    "sma_crossover",
    "ema_crossover",
    "adx",
    "volume_ratio",
    "price_change_pct",
}

COMPONENT_DEFAULTS = {
    "price_vs_sma": {"threshold": 0.0, "period": 50, "score_min": 0.0, "score_max": 10.0},
    "price_vs_ema": {"threshold": 0.0, "period": 20, "score_min": 0.0, "score_max": 10.0},
    "sma_crossover": {"threshold": 0.0, "period": 5, "slow_period": 20, "score_min": 0.0, "score_max": 5.0},
    "ema_crossover": {"threshold": 0.0, "period": 8, "slow_period": 21, "score_min": 0.0, "score_max": 5.0},
    "adx": {"threshold": 25.0, "period": 14, "score_min": 15.0, "score_max": 35.0},
    "volume_ratio": {"threshold": 1.0, "period": 20, "score_min": 1.0, "score_max": 3.0},
    "latest_volume": {"threshold": 100_000.0, "period": 1, "score_min": 100_000.0, "score_max": 2_000_000.0},
    "dollar_volume": {"threshold": 100_000.0, "period": 1, "score_min": 100_000.0, "score_max": 5_000_000.0},
    "price_change_pct": {"threshold": 0.0, "period": 5, "score_min": 0.0, "score_max": 8.0},
}


def _render_signal_builder_help() -> None:
    with st.expander("How to build signal components", expanded=False):
        st.markdown(
            """
            A signal is a set of components. Each component calculates one value, such as
            “close vs SMA20” or “ADX14.” Components can be used in two ways:

            - **Gate**: a pass/fail filter. If a gate fails, the symbol becomes ineligible and its final score is 0.
            - **Score**: a weighted ranking input. Score components are normalized from 0–100, multiplied by weight, and averaged.

            Field meanings:

            - **Op + Threshold**: the pass line for the component. Example: `>= 0` for close above a moving average, or `>= 25` for ADX.
            - **Period**: number of daily bars used by the indicator. For 15-minute scan cycles, the latest snapshot is appended as the current bar, but historical periods are still daily bars in this version.
            - **Weight**: importance of a score component. A weight of 2 counts twice as much as weight 1. Gates ignore weight.
            - **Score min / max**: maps raw indicator values to 0–100. At score min, the component scores near 0. At score max or better, it scores near 100.
            - **Fast / slow period**: for crossovers, “period” is the fast average and “slow period” is the slower comparison average.

            Useful recipes:

            - **Close above SMA50 gate**: Type `Close vs SMA`, Mode `gate`, Op `>=`, Threshold `0`, Period `50`.
            - **SMA5 above SMA20 gate**: Type `SMA crossover`, Mode `gate`, Op `>=`, Threshold `0`, Period `5`, Slow period `20`.
            - **ADX14 trend gate**: Type `ADX`, Mode `gate`, Op `>=`, Threshold `25`, Period `14`.
            - **Dollar-volume liquidity gate**: Type `Dollar volume`, Mode `gate`, Op `>=`, Threshold `100000`.
            - **Volume breakout score**: Type `Relative volume`, Mode `score`, Threshold `1`, Period `20`, Score min `1`, Score max `3`.
            - **5-day momentum score**: Type `Price change %`, Mode `score`, Period `5`, Score min `0`, Score max `8`.
            """
        )


def _component_from_inputs(index: int, default_component: dict[str, object] | None = None) -> dict[str, object]:
    default_component = dict(default_component or {})
    default_params = dict(default_component.get("params") or {})
    columns = st.columns([1.6, 1.4, 0.9, 0.8, 1.0])
    with columns[0]:
        component_options = [
            "price_vs_sma",
            "price_vs_ema",
            "sma_crossover",
            "ema_crossover",
            "adx",
            "volume_ratio",
            "latest_volume",
            "dollar_volume",
            "price_change_pct",
        ]
        default_type = str(default_component.get("type") or "price_vs_sma")
        component_type = st.selectbox(
            "Type",
            component_options,
            index=component_options.index(default_type) if default_type in component_options else 0,
            key=f"component_type_{index}",
            format_func=lambda value: COMPONENT_LABELS.get(value, value),
            help="Choose the indicator this component calculates. The raw value is shown later in the component breakdown.",
        )
    defaults = COMPONENT_DEFAULTS.get(component_type, {})
    with columns[1]:
        name = st.text_input(
            "Name",
            value=str(default_component.get("name") or COMPONENT_LABELS.get(component_type, f"Component {index + 1}")),
            key=f"component_name_{index}",
            help="Friendly label shown in score breakdowns and Telegram alert explanations.",
        )
    with columns[2]:
        default_mode = str(default_component.get("mode") or "score").lower()
        mode = st.selectbox(
            "Mode",
            ["score", "gate"],
            index=1 if default_mode == "gate" else 0,
            key=f"component_mode_{index}",
            help="Score ranks symbols from 0–100 using weight. Gate is pass/fail; if it fails, the symbol is filtered out.",
        )
    with columns[3]:
        operator_options = [">=", ">", "<=", "<", "=="]
        default_operator = str(default_component.get("operator") or ">=")
        operator = st.selectbox(
            "Op",
            operator_options,
            index=operator_options.index(default_operator) if default_operator in operator_options else 0,
            key=f"component_op_{index}",
            help="Comparison used with Threshold. Example: value >= threshold.",
        )
    with columns[4]:
        threshold = st.number_input(
            "Threshold",
            value=float(default_component.get("threshold", defaults.get("threshold", 0.0))),
            key=f"component_threshold_{index}",
            help="Pass line for the component. For MA distance/crossovers, 0 means above. For ADX, 25 is a common trend-strength line. For volume ratio, 2 means 2x average volume.",
        )

    parameter_columns = st.columns([1, 1, 1, 1, 2])
    period = int(default_params.get("period") or default_params.get("days") or default_params.get("fast_period") or defaults.get("period", 20))
    if component_type in COMPONENTS_WITH_PERIOD:
        period_label = "Lookback days"
        if component_type in {"sma_crossover", "ema_crossover"}:
            period_label = "Fast period"
        elif component_type == "price_change_pct":
            period_label = "Change days"
        with parameter_columns[0]:
            period = st.number_input(
                period_label,
                value=int(period),
                min_value=1,
                step=1,
                key=f"component_period_{index}",
                help="Number of daily bars used. In 15-minute scan cycles, the latest snapshot is appended as the current bar, but lookback periods are still daily bars.",
            )
    else:
        with parameter_columns[0]:
            st.caption("No lookback period needed for this component.")

    slow = int(default_params.get("slow_period") or defaults.get("slow_period", 50 if int(period) < 50 else 200))
    if component_type in {"sma_crossover", "ema_crossover"}:
        with parameter_columns[1]:
            slow = st.number_input(
                "Slow period",
                value=slow,
                min_value=1,
                step=1,
                key=f"component_slow_{index}",
                help="The slower moving-average period used for comparison. The main Period field is the fast average.",
            )

    weight = 0.0
    score_min = float(default_component.get("score_min", defaults.get("score_min", 0.0)))
    score_max = float(default_component.get("score_max", defaults.get("score_max", 10.0)))
    if mode == "score":
        with parameter_columns[2]:
            weight = st.number_input(
                "Weight",
                value=float(default_component.get("weight", 1.0)),
                min_value=0.0,
                key=f"component_weight_{index}",
                help="Higher weight means this component matters more in the final score.",
            )
        with parameter_columns[3]:
            score_min = st.number_input(
                "Score min",
                value=score_min,
                key=f"component_score_min_{index}",
                help="Normalization floor. A raw value at this level scores near 0.",
            )
        with parameter_columns[4]:
            score_max = st.number_input(
                "Score max",
                value=score_max,
                key=f"component_score_max_{index}",
                help="Normalization ceiling. A raw value at this level or better scores near 100.",
            )
    else:
        with parameter_columns[2]:
            st.caption("Gate mode ignores weight and score min/max.")

    help_columns = st.columns([1])
    with help_columns[0]:
        st.caption(f"**Meaning:** {COMPONENT_HELP.get(component_type, '')}")
        st.caption(f"**Example:** {COMPONENT_EXAMPLES.get(component_type, '')}")

    params: dict[str, object]
    if component_type in {"sma_crossover", "ema_crossover"}:
        params = {"fast_period": int(period), "slow_period": int(slow)}
    elif component_type == "price_change_pct":
        params = {"days": int(period)}
    elif component_type in COMPONENTS_WITH_PERIOD:
        params = {"period": int(period)}
    else:
        params = {}

    return {
        "name": name,
        "type": component_type,
        "mode": mode,
        "weight": float(weight) if mode == "score" else 0.0,
        "operator": operator,
        "threshold": float(threshold),
        "score_min": float(score_min),
        "score_max": float(score_max),
        "params": params,
    }


SIGNAL_BUILDER_WIDGET_KEYS = {
    "signal_builder_name",
    "signal_builder_enabled",
    "signal_builder_description",
    "signal_builder_universe_mode",
    "signal_builder_selected_lists",
    "signal_builder_selected_symbols",
    "signal_builder_component_count",
    "signal_builder_use_advanced_json",
    "signal_builder_config_text",
}


def _clear_signal_builder_form_state() -> None:
    """Clear generated Signal Builder widgets so saved DB values become defaults again."""
    for key in list(st.session_state.keys()):
        if key.startswith("component_") or key in SIGNAL_BUILDER_WIDGET_KEYS:
            del st.session_state[key]


def _signal_builder_empty_config() -> dict[str, object]:
    return {"description": "", "universe": {"mode": "all", "symbols": [], "lists": []}, "components": []}


def _load_signal_builder_form_state(selected_row: dict[str, object] | None) -> None:
    """Load the selected saved signal into Streamlit widget state before rendering fields."""
    _clear_signal_builder_form_state()
    config = dict(selected_row.get("config") or {}) if selected_row else _signal_builder_empty_config()
    universe = dict(config.get("universe") or {})
    components = list(config.get("components") or [])

    st.session_state.signal_builder_name = str(selected_row.get("name") or "") if selected_row else ""
    st.session_state.signal_builder_enabled = bool(selected_row.get("enabled")) if selected_row else True
    st.session_state.signal_builder_description = str(
        config.get("description") or (selected_row.get("description") if selected_row else "") or ""
    )
    st.session_state.signal_builder_universe_mode = "selected" if universe.get("mode") == "selected" else "all"
    st.session_state.signal_builder_selected_lists = list(universe.get("lists") or [])
    st.session_state.signal_builder_selected_symbols = ", ".join(str(item).upper() for item in universe.get("symbols") or [])
    st.session_state.signal_builder_component_count = max(1, len(components) or 3)
    st.session_state.signal_builder_use_advanced_json = False
    st.session_state.signal_builder_config_text = json.dumps(config, indent=2)

    for index, component in enumerate(components[:8]):
        component = dict(component or {})
        params = dict(component.get("params") or {})
        component_type = str(component.get("type") or "price_vs_sma")
        defaults = COMPONENT_DEFAULTS.get(component_type, {})
        period = int(
            params.get("period")
            or params.get("days")
            or params.get("fast_period")
            or defaults.get("period", 20)
        )
        st.session_state[f"component_type_{index}"] = component_type
        st.session_state[f"component_name_{index}"] = str(
            component.get("name") or COMPONENT_LABELS.get(component_type, f"Component {index + 1}")
        )
        st.session_state[f"component_mode_{index}"] = str(component.get("mode") or "score").lower()
        st.session_state[f"component_op_{index}"] = str(component.get("operator") or ">=")
        st.session_state[f"component_threshold_{index}"] = float(
            component.get("threshold", defaults.get("threshold", 0.0))
        )
        st.session_state[f"component_period_{index}"] = period
        st.session_state[f"component_slow_{index}"] = int(
            params.get("slow_period") or defaults.get("slow_period", 50 if period < 50 else 200)
        )
        st.session_state[f"component_weight_{index}"] = float(component.get("weight", 1.0))
        st.session_state[f"component_score_min_{index}"] = float(
            component.get("score_min", defaults.get("score_min", 0.0))
        )
        st.session_state[f"component_score_max_{index}"] = float(
            component.get("score_max", defaults.get("score_max", 10.0))
        )


def _signal_preview_symbols(config: dict[str, object]) -> set[str]:
    universe = dict(config.get("universe") or {})
    configured_symbols = {
        str(symbol).strip().upper()
        for symbol in universe.get("symbols", [])
        if str(symbol).strip()
    }
    configured_lists = [
        str(name).strip()
        for name in universe.get("lists", [])
        if str(name).strip()
    ]
    configured_universe = configured_symbols | database.symbols_for_list_names(configured_lists)
    if universe.get("mode") == "selected" and configured_universe:
        return configured_universe
    return set(database.active_symbols())


def _history_frames_for_preview(history: dict[str, list[object]]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol, rows in history.items():
        records = [dict(row) for row in rows]
        if records:
            frames[symbol] = pd.DataFrame.from_records(records)
    return frames


def _signal_preview_key(selected_row: dict[str, object] | None, signal_name: str) -> str:
    if selected_row and selected_row.get("id"):
        return f"signal_builder.preview.signal_id.{int(selected_row['id'])}"
    normalized = (signal_name or "unsaved").strip().lower().replace(" ", "_")[:80]
    return f"signal_builder.preview.unsaved.{normalized or 'preview'}"


def _clear_signal_builder_preview_state() -> None:
    for key in [
        "signal_builder_preview_rows",
        "signal_builder_preview_components",
        "signal_builder_preview_label",
        "signal_builder_preview_detail_symbol",
        "signal_builder_preview_run_at",
        "signal_builder_preview_config_json",
    ]:
        st.session_state.pop(key, None)


def _preview_payload_from_results(
    *,
    results: list[object],
    label: str,
    config: dict[str, object],
    duration_seconds: float | None = None,
) -> dict[str, object]:
    return {
        "label": label,
        "run_at": datetime.now(UTC).isoformat(),
        "duration_seconds": duration_seconds,
        "config_json": json.dumps(config, sort_keys=True),
        "rows": [
            {
                "symbol": item.symbol,
                "score": item.score,
                "eligible": item.eligible,
                "trading_date": item.trading_date,
                "close": item.close,
                "message": item.message,
            }
            for item in results
        ],
        "components": {
            item.symbol: [component.__dict__ for component in item.components]
            for item in results
        },
    }


def _load_signal_preview_payload(preview_key: str) -> dict[str, object] | None:
    payload = database.get_app_setting(preview_key, None)
    return payload if isinstance(payload, dict) else None


def _save_signal_preview_payload(preview_key: str, payload: dict[str, object]) -> None:
    database.set_app_setting(preview_key, payload)


def _hydrate_signal_builder_preview_state(payload: dict[str, object] | None) -> None:
    _clear_signal_builder_preview_state()
    if not payload:
        return
    st.session_state.signal_builder_preview_rows = payload.get("rows") or []
    st.session_state.signal_builder_preview_components = payload.get("components") or {}
    st.session_state.signal_builder_preview_label = payload.get("label") or "Preview"
    st.session_state.signal_builder_preview_run_at = payload.get("run_at") or ""
    st.session_state.signal_builder_preview_config_json = payload.get("config_json") or ""


def _display_component_breakdown_frame(components: list[dict[str, object]]) -> pd.DataFrame:
    if not components:
        return pd.DataFrame()
    display = pd.DataFrame(components).copy()
    if "mode" in display.columns:
        gate_mask = display["mode"].astype(str).str.lower().eq("gate")
        for column in ["score", "weight", "contribution"]:
            if column in display.columns:
                display[column] = display[column].astype(object)
                display.loc[gate_mask, column] = "Filter only"
    return display.rename(
        columns={
            "name": "Name",
            "component_type": "Component type",
            "mode": "Mode",
            "value": "Value",
            "passed": "Passed",
            "score": "Score",
            "weight": "Weight",
            "contribution": "Contribution",
            "message": "Message",
        }
    )


def _preview_cancel_requested() -> bool:
    return bool(st.session_state.get("signal_builder_cancel_preview"))


def _raise_if_preview_cancelled() -> None:
    if _preview_cancel_requested():
        st.session_state.signal_builder_cancel_preview = False
        raise RuntimeError("Preview cancelled by user.")


def _preview_signal_with_progress(preview_row: dict[str, object]) -> tuple[list[object], float]:
    started = time.monotonic()
    progress = st.progress(0.0, text="Preparing preview...")
    status = st.empty()
    config = dict(preview_row["config"])
    _raise_if_preview_cancelled()

    symbols = _signal_preview_symbols(config)
    progress.progress(0.1, text=f"Resolved preview universe: {len(symbols):,} symbols")
    if len(symbols) >= 1000:
        status.warning(
            f"Large preview: {len(symbols):,} symbols. This can take several minutes because it loads historical bars and evaluates indicators."
        )
    else:
        status.caption(f"Preview universe: {len(symbols):,} symbols")
    _raise_if_preview_cancelled()

    required_bars = required_history_bars(config)
    progress.progress(0.25, text=f"Loading latest {required_bars} daily bars per symbol from SQLite...")
    history = database.load_price_history(symbols, min_bars=required_bars, include_latest_snapshot=False)
    _raise_if_preview_cancelled()

    progress.progress(0.55, text=f"Loaded history for {len(history):,} symbols; building frames...")
    frames = _history_frames_for_preview(history)
    definition = SignalDefinition(
        name=str(preview_row["name"]),
        config=config,
        signal_id=int(preview_row["id"] or 0),
        enabled=bool(preview_row["enabled"]),
    )
    progress.progress(0.75, text=f"Evaluating signal components for {len(frames):,} symbols...")
    _raise_if_preview_cancelled()

    results = evaluate_signal(definition, frames)
    duration = time.monotonic() - started
    progress.progress(1.0, text=f"Preview complete: {len(results):,} scored symbols in {duration:.1f}s")
    status.success(f"Preview complete in {duration:.1f}s. Scored {len(results):,} symbols.")
    return results, duration


def _render_signal_builder() -> None:
    st.subheader("Signal Builder")
    flash_message = st.session_state.pop("signal_builder_flash", None)
    if flash_message:
        st.success(str(flash_message))
    st.caption("Build weighted/gated technical signals without changing backend code.")
    _render_signal_builder_help()

    if st.button("Seed/update starter signals", use_container_width=True):
        count = seed_starter_signals(database)
        st.success(f"Seeded {count} starter signals.")
        st.cache_data.clear()
        st.rerun()

    definitions = database.list_signal_definitions()
    options = ["Create New Signal"] + [f"{item['id']}: {item['name']}" for item in definitions]
    pending_selection = st.session_state.pop("signal_builder_pending_selection", None)
    force_reload = bool(st.session_state.pop("signal_builder_force_reload", False))
    if pending_selection in options:
        st.session_state.signal_builder_selected = pending_selection
        force_reload = True
    if st.session_state.get("signal_builder_selected") not in options:
        st.session_state.signal_builder_selected = "Create New Signal"
    if force_reload:
        st.session_state.signal_builder_loaded_selection = None

    st.markdown("#### Create/Manage Signals")
    selected = st.selectbox("Signal", options, key="signal_builder_selected")
    selected_row = None
    if selected != "Create New Signal":
        selected_id = int(selected.split(":", 1)[0])
        selected_row = database.get_signal_definition(selected_id)
    preview_storage_key = _signal_preview_key(selected_row, selected_row["name"] if selected_row else "")
    if st.session_state.get("signal_builder_loaded_selection") != selected:
        _load_signal_builder_form_state(selected_row)
        st.session_state.signal_builder_loaded_selection = selected
        _hydrate_signal_builder_preview_state(_load_signal_preview_payload(preview_storage_key))

    default_config = selected_row["config"] if selected_row else _signal_builder_empty_config()
    detail_columns = st.columns([1.6, 0.5, 2.4])
    with detail_columns[0]:
        signal_name = st.text_input(
            "Signal name",
            value=selected_row["name"] if selected_row else "",
            key="signal_builder_name",
        )
        if selected_row:
            delete_label = f"Delete {selected_row['name']}"
            if st.button(delete_label, key="delete_signal_button", type="secondary", use_container_width=True):
                database.delete_signal_definition(int(selected_row["id"]))
                st.session_state.signal_builder_pending_selection = "Create New Signal"
                st.cache_data.clear()
                st.rerun()
    with detail_columns[1]:
        enabled = st.checkbox(
            "Enabled",
            value=bool(selected_row["enabled"]) if selected_row else True,
            key="signal_builder_enabled",
        )
    with detail_columns[2]:
        description = st.text_input(
            "Description",
            value=str(default_config.get("description") or selected_row.get("description") if selected_row else ""),
            key="signal_builder_description",
        )

    preview_storage_key = _signal_preview_key(selected_row, signal_name)
    if not st.session_state.get("signal_builder_preview_rows"):
        _hydrate_signal_builder_preview_state(_load_signal_preview_payload(preview_storage_key))

    universe_config = default_config.get("universe") or {}
    universe_mode = st.radio(
        "Universe",
        ["all", "selected"],
        horizontal=True,
        index=0 if universe_config.get("mode") != "selected" else 1,
        key="signal_builder_universe_mode",
        help="Use all active symbols, or restrict this signal to selected lists and/or typed tickers.",
    )
    available_lists = [item["name"] for item in database.list_symbol_lists()]
    selected_lists: list[str] = []
    selected_symbols_text = ""
    if universe_mode == "selected":
        universe_columns = st.columns([1.5, 2])
        with universe_columns[0]:
            selected_lists = st.multiselect(
                "Selected lists",
                available_lists,
                default=universe_config.get("lists") or [],
                key="signal_builder_selected_lists",
                help="Symbols from all selected lists are combined.",
            )
        with universe_columns[1]:
            selected_symbols_text = st.text_input(
                "Extra selected tickers",
                value=", ".join(universe_config.get("symbols") or []),
                key="signal_builder_selected_symbols",
                help="Optional comma-separated tickers to add to the selected lists.",
            )

    st.markdown("#### Component builder")
    default_components = list(default_config.get("components") or [])
    component_count = st.number_input(
        "Components",
        min_value=1,
        max_value=8,
        value=max(1, len(default_components) or 3),
        step=1,
        key="signal_builder_component_count",
    )
    built_components = [
        _component_from_inputs(index, default_components[index] if index < len(default_components) else None)
        for index in range(int(component_count))
    ]

    generated_config = {
        "description": description,
        "universe": {
            "mode": universe_mode,
            "lists": selected_lists,
            "symbols": [
                item.strip().upper() for item in selected_symbols_text.split(",") if item.strip()
            ],
        },
        "components": built_components,
    }
    use_advanced_json = st.checkbox(
        "Use Advanced JSON instead of visual builder fields",
        value=False,
        key="signal_builder_use_advanced_json",
        help="Leave this off unless you intentionally want the JSON text to override the visual component fields.",
    )
    with st.expander("Advanced JSON definition", expanded=use_advanced_json):
        config_text = st.text_area(
            "JSON override",
            value=json.dumps(generated_config, indent=2),
            height=320,
            key="signal_builder_config_text",
            help="Only used when the checkbox above is enabled.",
        )
        st.caption("Tip: if a component is a gate, its weight and score min/max are ignored.")

    preview_symbol_count = len(_signal_preview_symbols(generated_config))
    if preview_symbol_count >= 1000:
        st.warning(
            f"Preview universe has {preview_symbol_count:,} symbols. Preview may take several minutes. "
            "For faster iteration, use a selected list or build a liquidity list first."
        )

    action_left, action_mid, action_right = st.columns([1, 1, 1])
    preview_clicked = False
    preview_running = bool(st.session_state.get("signal_builder_preview_running"))
    with action_left:
        if st.button("Save signal definition", type="primary", use_container_width=True):
            if not signal_name.strip():
                st.error("Signal name is required.")
            else:
                try:
                    parsed_config = json.loads(config_text) if use_advanced_json else generated_config
                    saved_name = signal_name.strip()
                    saved_id = database.upsert_signal_definition(
                        saved_name,
                        parsed_config,
                        enabled=enabled,
                        description=str(parsed_config.get("description") or description),
                    )
                    current_preview_rows = st.session_state.get("signal_builder_preview_rows") or []
                    if current_preview_rows:
                        _save_signal_preview_payload(
                            f"signal_builder.preview.signal_id.{saved_id}",
                            {
                                "label": st.session_state.get("signal_builder_preview_label") or saved_name,
                                "run_at": st.session_state.get("signal_builder_preview_run_at") or datetime.now(UTC).isoformat(),
                                "duration_seconds": None,
                                "config_json": st.session_state.get("signal_builder_preview_config_json") or json.dumps(parsed_config, sort_keys=True),
                                "rows": current_preview_rows,
                                "components": st.session_state.get("signal_builder_preview_components") or {},
                            },
                        )
                    st.session_state.signal_builder_pending_selection = f"{saved_id}: {saved_name}"
                    st.session_state.signal_builder_force_reload = True
                    st.session_state.signal_builder_flash = f"Signal saved: {saved_name}"
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not save signal: {exc}")
    with action_mid:
        preview_clicked = st.button("Preview rankings", use_container_width=True, disabled=preview_running)
        if preview_clicked:
            st.session_state.signal_builder_preview_running = True
            st.session_state.signal_builder_cancel_preview = False
            preview_running = True
    with action_right:
        if preview_running:
            if st.button("Cancel preview", key="cancel_preview_button", use_container_width=True):
                st.session_state.signal_builder_cancel_preview = True
                st.warning("Preview cancellation requested. If a database read is already in progress, it will stop after that step finishes.")

    if preview_clicked:
        try:
            parsed_config = json.loads(config_text) if use_advanced_json else generated_config
            preview_row = {
                "id": selected_row["id"] if selected_row else 0,
                "name": signal_name.strip() or "Preview",
                "config": parsed_config,
                "enabled": enabled,
            }
            results, duration = _preview_signal_with_progress(preview_row)
            payload = _preview_payload_from_results(
                results=results,
                label=signal_name.strip() or "Preview",
                config=parsed_config,
                duration_seconds=duration,
            )
            _save_signal_preview_payload(preview_storage_key, payload)
            _hydrate_signal_builder_preview_state(payload)
        except Exception as exc:
            st.error(f"Preview failed: {exc}")
        finally:
            st.session_state.signal_builder_preview_running = False
            st.session_state.signal_builder_cancel_preview = False

    preview_rows = st.session_state.get("signal_builder_preview_rows") or []
    preview_components = st.session_state.get("signal_builder_preview_components") or {}
    if preview_rows:
        preview_run_at = _format_timestamp(st.session_state.get("signal_builder_preview_run_at"))
        title = f"Preview rankings: {st.session_state.get('signal_builder_preview_label', 'Preview')}"
        if preview_run_at:
            title += f" · Run at {preview_run_at}"
        st.markdown(f"#### {title}")
        current_config_json = json.dumps(json.loads(config_text) if use_advanced_json else generated_config, sort_keys=True)
        if st.session_state.get("signal_builder_preview_config_json") and st.session_state.get("signal_builder_preview_config_json") != current_config_json:
            st.caption("This preview was run before the latest unsaved field changes. Run Preview rankings again to refresh it.")
        preview = pd.DataFrame(preview_rows)
        st.dataframe(preview, use_container_width=True, hide_index=True)
        detail_options = [str(row["symbol"]) for row in preview_rows[:25]]
        if detail_options:
            detail_symbol = st.selectbox(
                "Preview component breakdown",
                detail_options,
                key="signal_builder_preview_detail_symbol",
            )
            st.dataframe(
                _display_component_breakdown_frame(preview_components.get(detail_symbol, [])),
                use_container_width=True,
                hide_index=True,
            )
        if st.button("Clear preview", use_container_width=True):
            database.set_app_setting(preview_storage_key, None)
            _clear_signal_builder_preview_state()
            st.rerun()

    st.markdown("#### Signal schedule")
    if selected_row:
        signal_id = int(selected_row["id"])
        _render_scheduler_form(
            key_prefix=f"signal_{signal_id}",
            label=str(selected_row["name"]),
            schedule=get_signal_schedule(database, signal_id),
            due=is_signal_due(database, signal_id),
            last_run=_app_setting(f"signals.{signal_id}.last_scheduled_run_at", None),
            save_callback=lambda config, sid=signal_id: save_signal_schedule(database, sid, config),
        )
        if st.button(
            "Test alert",
            type="primary",
            use_container_width=True,
            help=(
                "Refreshes Massive snapshot data for this signal, scores it, and sends a compact top-10 digest. "
                "If ALERT_DRY_RUN=true, it records a dry-run delivery instead of sending Telegram."
            ),
        ):
            with st.spinner("Scoring signal and sending sample alert..."):
                result = run_signal_test_alert(database, settings, signal_id)
            if result.status == "success":
                st.success(result.message)
            else:
                st.error(result.message)
            st.cache_data.clear()
    else:
        st.info("Save the signal first to enable scheduling and Test alert.")

    st.markdown("#### Latest saved scores")
    latest_scores = read_frame(
        """
        SELECT signal_name, symbol, trading_date, close, score, eligible, message, created_at
        FROM signal_scores
        WHERE is_latest = 1
        ORDER BY signal_name, score DESC
        LIMIT 200
        """
    )
    if latest_scores.empty:
        st.info("No saved scores yet. Run a signal from the CLI or preview/save first.")
    else:
        st.dataframe(_format_timestamps(latest_scores), use_container_width=True, hide_index=True)


def _latest_scan_cycle_status() -> dict[str, object] | None:
    rows = database.query(
        """
        SELECT started_at, finished_at, status, snapshots_fetched, symbols_filtered,
               symbols_scored, alerts_created, deliveries_attempted, delivered,
               duration_seconds, message
        FROM scan_cycle_runs
        ORDER BY id DESC
        LIMIT 1
        """
    )
    return dict(rows[0]) if rows else None


def _render_notifications() -> None:
    st.subheader("Notifications")
    st.caption("Telegram alert delivery based on saved signal scores.")

    configured = bool(settings.telegram_bot_token and settings.telegram_chat_id)
    status_columns = st.columns(4)
    status_columns[0].metric("Telegram token", "Configured" if settings.telegram_bot_token else "Missing")
    status_columns[1].metric("Telegram chat", "Configured" if settings.telegram_chat_id else "Missing")
    status_columns[2].metric("Dry run", "On" if settings.alert_dry_run else "Off")
    status_columns[3].metric(
        "Default frequency",
        f"{settings.alert_default_frequency_amount} {settings.alert_default_frequency_unit}",
    )

    if not configured:
        st.warning(
            "Telegram is not fully configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in `.env`."
        )

    st.markdown("#### Pipeline status")
    snapshot_status = _latest_snapshot_status()
    history_status = database.market_snapshot_history_status()
    latest_cycle = _latest_scan_cycle_status()
    status_cols = st.columns(5)
    status_cols[0].metric("Latest snapshot", _format_timestamp(snapshot_status.get("latest_snapshot_at")) or "—")
    status_cols[1].metric("History rows", f"{int(history_status.get('count') or 0):,}")
    status_cols[2].metric("Last cycle", _format_timestamp(latest_cycle.get("started_at")) if latest_cycle else "—")
    status_cols[3].metric("Scored", f"{int(latest_cycle.get('symbols_scored') or 0):,}" if latest_cycle else "—")
    status_cols[4].metric("Delivered", f"{int(latest_cycle.get('delivered') or 0):,}" if latest_cycle else "—")
    if latest_cycle:
        st.caption(
            "Latest full cycle: "
            f"status={latest_cycle.get('status')}, "
            f"snapshots={int(latest_cycle.get('snapshots_fetched') or 0):,}, "
            f"filtered={int(latest_cycle.get('symbols_filtered') or 0):,}, "
            f"alerts={int(latest_cycle.get('alerts_created') or 0):,}, "
            f"deliveries={int(latest_cycle.get('deliveries_attempted') or 0):,}, "
            f"duration={float(latest_cycle.get('duration_seconds') or 0):.2f}s. "
            f"{latest_cycle.get('message') or ''}"
        )
        if float(latest_cycle.get("duration_seconds") or 0) > 720:
            st.warning("The latest scan cycle exceeded 12 minutes. Reduce SCAN_MAX_SYMBOLS or tighten filters before running every 15 minutes.")
    st.info(
        "For 15-minute notifications, use a full scan cycle. It fetches a fresh snapshot, filters symbols, scores enabled signals, then evaluates and sends/queues alerts. "
        "Alert scan only reads existing saved scores and does not fetch or process new market data."
    )

    st.markdown("#### Pipeline actions")
    if not settings.massive_api_key:
        st.warning("MASSIVE_API_KEY is missing. Full scan cycle requires API access.")
    action_left, action_middle, action_right = st.columns(3)
    with action_left:
        if st.button("Seed alert rules", use_container_width=True):
            count = seed_alert_rules(database, settings)
            st.success(f"Seeded/updated {count} alert rules.")
            st.cache_data.clear()
            st.rerun()
    with action_middle:
        if st.button("Send Telegram test", use_container_width=True):
            sent = send_telegram_test(database, settings)
            if settings.alert_dry_run:
                st.info("Recorded Telegram test as dry-run. Set ALERT_DRY_RUN=false to send.")
            elif sent:
                st.success("Telegram test delivered.")
            else:
                st.error("Telegram test failed. Check delivery history.")
            st.cache_data.clear()
    with action_right:
        if st.button("Run alert scan only", use_container_width=True):
            result = scan_alerts(database, settings)
            st.success(
                "Alert scan complete from existing saved scores: "
                f"{result.alerts_created} alerts, {result.queued} queued, "
                f"{result.deliveries_attempted} deliveries, dry_run={result.dry_run}"
            )
            st.cache_data.clear()

    with st.expander("Run/test full 15-minute scan cycle", expanded=False):
        st.caption("This is the production-style path: fetch snapshot → filter → score → alert scan → Telegram/dry-run.")
        cycle_cols = st.columns([1, 1, 2])
        cycle_max_symbols = cycle_cols[0].number_input(
            "Max symbols",
            min_value=1,
            value=int(settings.scan_max_symbols),
            step=50,
            help="Use a small value like 25–50 for safe tests; production can use SCAN_MAX_SYMBOLS.",
        )
        cycle_force = cycle_cols[1].checkbox(
            "Force outside market hours",
            value=False,
            help="Use only for testing. Normal production cycles should respect market hours.",
        )
        cycle_symbols_text = cycle_cols[2].text_input(
            "Optional tickers",
            value="",
            help="Comma-separated symbols for a tiny test cycle, e.g. AAPL,MSFT,NVDA.",
        )
        cycle_a, cycle_b, cycle_c = st.columns(3)
        requested_symbols = [item.strip().upper() for item in cycle_symbols_text.split(",") if item.strip()] or None
        with cycle_a:
            if st.button("Dry run full cycle", use_container_width=True):
                try:
                    with scan_cycle_lock(settings.scan_lock_path):
                        result = run_scan_cycle(
                            database,
                            MassiveClient(
                                settings.massive_api_key,
                                base_url=settings.massive_base_url,
                                requests_per_minute=settings.requests_per_minute,
                                timeout_seconds=settings.http_timeout_seconds,
                            ),
                            settings,
                            dry_run=True,
                            max_symbols=int(cycle_max_symbols),
                            symbols=requested_symbols,
                            benchmark=True,
                            force=cycle_force,
                        )
                    st.success(
                        f"Dry-run cycle complete: snapshots={result.snapshots_fetched:,}, filtered={result.symbols_filtered:,}, "
                        f"scored={result.symbols_scored:,}, alerts={result.alerts.alerts_created}, queued={result.alerts.queued}, "
                        f"history_rows={result.snapshot_history_rows:,}, duration={result.duration_seconds:.2f}s"
                    )
                    st.cache_data.clear()
                except Exception as exc:
                    st.error(f"Dry-run full cycle failed: {exc}")
        with cycle_b:
            if st.button("Real Telegram full cycle", use_container_width=True):
                try:
                    with scan_cycle_lock(settings.scan_lock_path):
                        result = run_scan_cycle(
                            database,
                            MassiveClient(
                                settings.massive_api_key,
                                base_url=settings.massive_base_url,
                                requests_per_minute=settings.requests_per_minute,
                                timeout_seconds=settings.http_timeout_seconds,
                            ),
                            settings,
                            dry_run=False,
                            max_symbols=int(cycle_max_symbols),
                            symbols=requested_symbols,
                            benchmark=True,
                            force=cycle_force,
                        )
                    st.success(
                        f"Real cycle complete: snapshots={result.snapshots_fetched:,}, filtered={result.symbols_filtered:,}, "
                        f"scored={result.symbols_scored:,}, alerts={result.alerts.alerts_created}, delivered={result.alerts.delivered}, "
                        f"duration={result.duration_seconds:.2f}s"
                    )
                    st.cache_data.clear()
                except Exception as exc:
                    st.error(f"Real full cycle failed: {exc}")
        with cycle_c:
            if st.button("Full cycle without Telegram", use_container_width=True):
                try:
                    with scan_cycle_lock(settings.scan_lock_path):
                        result = run_scan_cycle(
                            database,
                            MassiveClient(
                                settings.massive_api_key,
                                base_url=settings.massive_base_url,
                                requests_per_minute=settings.requests_per_minute,
                                timeout_seconds=settings.http_timeout_seconds,
                            ),
                            settings,
                            max_symbols=int(cycle_max_symbols),
                            symbols=requested_symbols,
                            skip_telegram=True,
                            benchmark=True,
                            force=cycle_force,
                        )
                    st.success(
                        f"Cycle complete without Telegram: filtered={result.symbols_filtered:,}, scored={result.symbols_scored:,}, "
                        f"alerts={result.alerts.alerts_created}, duration={result.duration_seconds:.2f}s"
                    )
                    st.cache_data.clear()
                except Exception as exc:
                    st.error(f"No-Telegram full cycle failed: {exc}")

    with st.expander("Send sample alert", expanded=False):
        definitions = database.list_signal_definitions()
        signal_names = [str(item["name"]) for item in definitions]
        if not signal_names:
            st.info("Create a signal before sending sample alerts.")
        else:
            sample_cols = st.columns([1.4, 0.8, 0.8, 0.8])
            sample_signal = sample_cols[0].selectbox("Signal", signal_names, key="sample_alert_signal")
            sample_symbol = sample_cols[1].text_input("Symbol", value="AAPL", key="sample_alert_symbol").upper().strip()
            sample_direction = sample_cols[2].selectbox("Direction", ["BUY", "SELL"], key="sample_alert_direction")
            sample_real_send = sample_cols[3].checkbox("Real send", value=False, help="Off records a dry-run delivery only.")
            if st.button("Send sample alert", use_container_width=True):
                try:
                    sent = send_sample_alert(
                        database,
                        settings,
                        signal_name=sample_signal,
                        symbol=sample_symbol,
                        direction=sample_direction,
                        dry_run=not sample_real_send,
                    )
                    if sample_real_send:
                        st.success("Sample alert delivered." if sent else "Sample alert attempted; check delivery history for failure details.")
                    else:
                        st.info("Recorded sample alert in dry-run mode.")
                    st.cache_data.clear()
                except Exception as exc:
                    st.error(f"Sample alert failed: {exc}")

    st.markdown("#### Alert rules")
    missing_rule_signals = read_frame(
        """
        SELECT s.id, s.name, s.enabled, s.updated_at
        FROM signal_definitions s
        LEFT JOIN alert_rules r ON lower(r.signal_name)=lower(s.name)
        WHERE r.id IS NULL
        ORDER BY s.name
        """
    )
    if not missing_rule_signals.empty:
        st.warning(
            "Some saved signals do not have notification rules yet. "
            "Create default alert rules before they can be configured or scanned for alerts."
        )
        st.dataframe(_format_timestamps(missing_rule_signals), use_container_width=True, hide_index=True)
        if st.button("Create default rules for missing signals", use_container_width=True):
            count = seed_alert_rules(database, settings)
            st.success(f"Created/updated {count} alert rules.")
            st.cache_data.clear()
            st.rerun()

    rules = read_frame(
        """
        SELECT r.id, r.signal_name, r.enabled, r.buy_threshold, r.sell_threshold,
               r.frequency_amount, r.frequency_unit, r.start_time, r.timezone,
               r.market_hours_only, r.updated_at,
               MAX(st.last_alerted_at) AS last_sent_at
        FROM alert_rules r
        LEFT JOIN alert_state st ON lower(st.signal_name)=lower(r.signal_name)
        GROUP BY r.id
        ORDER BY r.signal_name
        """
    )
    if rules.empty:
        st.info("No alert rules yet. Create a signal, then use the button above or Seed alert rules.")
    else:
        display_rules = rules.copy()
        display_rules["next_eligible_send_at"] = [
            next_eligible_send_at(
                now=pd.Timestamp.utcnow().to_pydatetime(),
                schedule=parse_schedule(dict(row)),
                last_alerted_at=row.get("last_sent_at"),
            ).isoformat()
            for row in display_rules.to_dict("records")
        ]
        st.dataframe(_format_timestamps(display_rules), use_container_width=True, hide_index=True)

        with st.expander("Edit alert rules"):
            for row in rules.to_dict("records"):
                with st.form(f"alert_rule_{int(row['id'])}"):
                    st.markdown(f"**{row['signal_name']}**")
                    columns = st.columns([0.8, 1, 1, 0.9, 1, 1, 1, 0.9])
                    enabled = columns[0].checkbox("Enabled", value=bool(row["enabled"]))
                    buy_threshold = columns[1].number_input(
                        "BUY ≥",
                        value=float(row["buy_threshold"]),
                        min_value=0.0,
                        max_value=100.0,
                        step=1.0,
                    )
                    sell_threshold = columns[2].number_input(
                        "SELL ≤",
                        value=float(row["sell_threshold"]),
                        min_value=0.0,
                        max_value=100.0,
                        step=1.0,
                    )
                    frequency_amount = columns[3].number_input(
                        "Every",
                        value=int(row["frequency_amount"]),
                        min_value=1,
                        step=1,
                    )
                    frequency_unit = columns[4].selectbox(
                        "Unit",
                        ["minutes", "hours", "days"],
                        index=["minutes", "hours", "days"].index(str(row["frequency_unit"]))
                        if str(row["frequency_unit"]) in {"minutes", "hours", "days"}
                        else 0,
                    )
                    start_time = columns[5].text_input("Start", value=str(row["start_time"]))
                    timezone = columns[6].text_input("Timezone", value=str(row["timezone"]))
                    market_hours_only = columns[7].checkbox(
                        "Mkt hrs",
                        value=bool(row["market_hours_only"]),
                    )
                    if st.form_submit_button("Save rule", use_container_width=True):
                        database.update_alert_rule(
                            int(row["id"]),
                            enabled=enabled,
                            buy_threshold=buy_threshold,
                            sell_threshold=sell_threshold,
                            frequency_amount=int(frequency_amount),
                            frequency_unit=frequency_unit,
                            start_time=start_time,
                            timezone=timezone,
                            market_hours_only=market_hours_only,
                        )
                        st.success("Alert rule saved.")
                        st.cache_data.clear()
                        st.rerun()

    st.markdown("#### Pending alerts")
    pending_alerts = read_frame(
        """
        SELECT created_at, updated_at, direction, symbol, signal_name, score,
               threshold, trading_date, close, message
        FROM pending_alerts
        WHERE status='pending'
        ORDER BY updated_at DESC LIMIT 50
        """
    )
    if pending_alerts.empty:
        st.info("No pending alerts.")
    else:
        st.dataframe(_format_timestamps(pending_alerts), use_container_width=True, hide_index=True)

    st.markdown("#### Recent alerts")
    alerts = read_frame(
        """
        SELECT created_at, direction, symbol, signal_name, score, threshold,
               trading_date, close, message
        FROM alerts ORDER BY id DESC LIMIT 50
        """
    )
    if alerts.empty:
        st.info("No generated alerts yet.")
    else:
        st.dataframe(_format_timestamps(alerts), use_container_width=True, hide_index=True)

    st.markdown("#### Delivery history")
    deliveries = read_frame(
        """
        SELECT d.created_at, d.channel_type, d.status, d.alert_id,
               a.symbol, a.direction, a.signal_name, d.error_text
        FROM notification_deliveries d
        LEFT JOIN alerts a ON a.id=d.alert_id
        ORDER BY d.id DESC LIMIT 50
        """
    )
    if deliveries.empty:
        st.info("No notification deliveries yet.")
    else:
        st.dataframe(_format_timestamps(deliveries), use_container_width=True, hide_index=True)


def _weekday_labels() -> list[tuple[str, int]]:
    return [("M", 0), ("Tu", 1), ("W", 2), ("Th", 3), ("F", 4), ("Sa", 5), ("Su", 6)]


def _render_scheduler_form(
    *,
    key_prefix: str,
    label: str,
    schedule: dict[str, object],
    due: bool,
    last_run: object,
    save_callback: object,
) -> None:
    unit_labels = {
        "minutes": "minutes",
        "hours": "hours",
        "days": "days",
        "business_days": "business days",
        "weeks": "weeks",
    }
    st.markdown("##### Scheduler")
    cols = st.columns([0.7, 0.8, 1.1, 0.8, 0.8, 0.8])
    enabled = cols[0].checkbox("Enabled", value=bool(schedule.get("enabled")), key=f"{key_prefix}_enabled")
    amount = cols[1].number_input(
        "Every",
        min_value=1,
        max_value=10000,
        value=max(int(schedule.get("frequency_amount") or 1), 1),
        step=1,
        key=f"{key_prefix}_amount",
    )
    unit = cols[2].selectbox(
        "Unit",
        SCHEDULE_UNITS,
        index=SCHEDULE_UNITS.index(str(schedule.get("frequency_unit")))
        if str(schedule.get("frequency_unit")) in SCHEDULE_UNITS
        else 2,
        format_func=lambda item: unit_labels.get(str(item), str(item)),
        key=f"{key_prefix}_unit",
    )
    start_time = cols[3].text_input(
        "Start",
        value=str(schedule.get("start_time") or "09:45"),
        help="Earliest local time the scheduler may run.",
        key=f"{key_prefix}_start_time",
    )
    end_time = cols[4].text_input(
        "End",
        value=str(schedule.get("end_time") or "16:00"),
        disabled=unit != "minutes",
        help="Only used for minute schedules. The scheduler will not run after this local time.",
        key=f"{key_prefix}_end_time",
    )
    timezone = str(schedule.get("timezone") or settings.alert_default_timezone)
    notify = cols[5].checkbox(
        "Telegram",
        value=bool(schedule.get("notify_telegram")),
        help="Send Telegram delivery/dry-run rows after completion or failure.",
        key=f"{key_prefix}_notify",
    )
    weekdays = list(schedule.get("weekdays") or [0, 1, 2, 3, 4])
    if unit == "minutes":
        st.caption("Minute schedule days")
        weekday_columns = st.columns(7)
        selected_weekdays: list[int] = []
        for index, (weekday_label, weekday_value) in enumerate(_weekday_labels()):
            checked = weekday_columns[index].checkbox(
                weekday_label,
                value=int(weekday_value) in {int(item) for item in weekdays},
                key=f"{key_prefix}_weekday_{weekday_value}",
            )
            if checked:
                selected_weekdays.append(int(weekday_value))
    else:
        selected_weekdays = weekdays
    if st.button(f"Save {label} schedule", use_container_width=True, key=f"{key_prefix}_save_schedule"):
        save_callback(
            {
                "enabled": enabled,
                "frequency_amount": int(amount),
                "frequency_unit": unit,
                "start_time": start_time,
                "end_time": end_time,
                "weekdays": selected_weekdays,
                "timezone": timezone,
                "notify_telegram": notify,
            }
        )
        st.success(f"{label} schedule saved.")
        st.rerun()
    weekday_text = ""
    if str(schedule.get("frequency_unit")) == "minutes":
        selected = {int(item) for item in list(schedule.get("weekdays") or [])}
        weekday_text = " · days " + ",".join(label for label, value in _weekday_labels() if value in selected)
    st.caption(
        f"Saved schedule: {'enabled' if bool(schedule.get('enabled')) else 'disabled'} · "
        f"every {int(schedule.get('frequency_amount') or 1)} {unit_labels.get(str(schedule.get('frequency_unit')), str(schedule.get('frequency_unit')))} · "
        f"start {schedule.get('start_time')} · "
        f"end {schedule.get('end_time') if str(schedule.get('frequency_unit')) == 'minutes' else 'n/a'} "
        f"{schedule.get('timezone')}{weekday_text} · "
        f"Telegram {'on' if bool(schedule.get('notify_telegram')) else 'off'} · "
        f"last scheduled run {_format_timestamp(last_run) or 'never'} · "
        f"{'due now' if due else 'not due'}"
    )


def _render_service_scheduler(service_key: str, label: str) -> None:
    _render_scheduler_form(
        key_prefix=f"service_{service_key}",
        label=label,
        schedule=get_service_schedule(database, service_key),
        due=is_service_due(database, service_key),
        last_run=_app_setting(f"services.{service_key}.last_scheduled_run_at", None),
        save_callback=lambda config: save_service_schedule(database, service_key, config),
    )


def _render_services() -> None:
    st.subheader("Services")
    st.caption("Run bounded maintenance/data-ingestion jobs without blocking the dashboard for a full-universe sync.")

    lists = database.list_symbol_lists()
    list_names = [str(item["name"]) for item in lists]

    with st.expander("Data ingestion: market snapshot", expanded=True):
        st.markdown(
            "Fetch the latest Massive full-market stock snapshot and store lightweight market fields: "
            "last price, intraday/change %, previous close, volume, and dollar volume. "
            "This is the fast foundation for filtering, scoring, and deciding which symbols deserve heavier enrichment."
        )
        if not settings.massive_api_key:
            st.warning("MASSIVE_API_KEY is missing. Market snapshot ingestion requires API access.")

        _render_service_scheduler("snapshot", "market snapshot")

        snapshot_status = _latest_snapshot_status()
        snapshot_status_cols = st.columns(3)
        snapshot_status_cols[0].metric("Stored snapshots", f"{int(snapshot_status.get('count') or 0):,}")
        snapshot_status_cols[1].metric("Latest snapshot", _format_timestamp(snapshot_status.get("latest_snapshot_at")) or "—")
        snapshot_status_cols[2].metric("Last fetched", _format_timestamp(snapshot_status.get("latest_fetched_at")) or "—")

        snapshot_scope_options = ["Stocks universe", "Selected lists", "Typed tickers", "Lists + typed tickers"]
        snapshot_scope = st.radio(
            "Run snapshot for",
            snapshot_scope_options,
            horizontal=True,
            key="snapshot_scope",
            index=_option_index(
                snapshot_scope_options,
                _app_setting("services.snapshot.scope", "Stocks universe"),
            ),
            help="Stocks universe stores the filtered full-market snapshot. Lists/tickers store only matching symbols.",
        )
        snapshot_lists: list[str] = []
        if snapshot_scope in {"Selected lists", "Lists + typed tickers"}:
            saved_snapshot_lists = _app_setting("services.snapshot.lists", [])
            snapshot_lists = st.multiselect(
                "Snapshot lists",
                list_names,
                key="snapshot_lists",
                default=[item for item in saved_snapshot_lists if item in list_names]
                if isinstance(saved_snapshot_lists, list)
                else [],
                help="The snapshot service will store matching symbols from the selected lists.",
            )
            if not list_names:
                st.info("No custom lists yet. Create lists in the Lists tab first.")

        snapshot_typed_symbols = ""
        if snapshot_scope in {"Typed tickers", "Lists + typed tickers"}:
            snapshot_typed_symbols = st.text_input(
                "Snapshot tickers",
                placeholder="AAPL, MSFT, NVDA",
                key="snapshot_tickers",
                value=str(_app_setting("services.snapshot.typed_symbols", "")),
                help="Comma-separated tickers. These are added to the selected scope.",
            )

        filter_cols = st.columns(4)
        min_price = filter_cols[0].number_input(
            "Min price",
            min_value=0.0,
            value=float(_app_setting("services.snapshot.min_price", settings.scan_min_price)),
            step=1.0,
            key="snapshot_min_price",
        )
        min_day_volume = filter_cols[1].number_input(
            "Min day volume",
            min_value=0.0,
            value=float(_app_setting("services.snapshot.min_day_volume", settings.scan_min_day_volume)),
            step=100_000.0,
            key="snapshot_min_day_volume",
        )
        min_dollar_volume = filter_cols[2].number_input(
            "Min dollar volume",
            min_value=0.0,
            value=float(_app_setting("services.snapshot.min_dollar_volume", 0.0)),
            step=1_000_000.0,
            key="snapshot_min_dollar_volume",
            help="Calculated as last price × day volume.",
        )
        max_store = filter_cols[3].number_input(
            "Max snapshot rows to store",
            min_value=1,
            max_value=20_000,
            value=int(_app_setting("services.snapshot.max_store", 5_000)),
            step=500,
            key="snapshot_max_store",
            help="Limits stored latest snapshot rows after filters. It does not limit which tickers are added to the Stocks universe.",
        )
        snapshot_defaults: dict[str, object] = {
            "services.snapshot.scope": snapshot_scope,
            "services.snapshot.min_price": float(min_price),
            "services.snapshot.min_day_volume": float(min_day_volume),
            "services.snapshot.min_dollar_volume": float(min_dollar_volume),
            "services.snapshot.max_store": int(max_store),
        }
        if snapshot_scope in {"Selected lists", "Lists + typed tickers"}:
            snapshot_defaults["services.snapshot.lists"] = snapshot_lists
        if snapshot_scope in {"Typed tickers", "Lists + typed tickers"}:
            snapshot_defaults["services.snapshot.typed_symbols"] = snapshot_typed_symbols
        _save_app_settings(snapshot_defaults)

        st.caption(
            "Tip: this service now adds every fetched snapshot ticker to the Stocks universe first. "
            "Price/volume/max-store filters only limit which latest snapshot rows are stored for dashboard/scoring."
        )

        if st.button(
            "Fetch latest market snapshot",
            type="primary",
            use_container_width=True,
            disabled=not bool(settings.massive_api_key),
        ):
            started = time.monotonic()
            service_scope = (
                f"{snapshot_scope}; min_price={min_price}; min_day_volume={min_day_volume}; "
                f"min_dollar_volume={min_dollar_volume}; max_store={max_store}"
            )
            service_run_id = database.start_service_run("market_snapshot", scope=service_scope)
            try:
                provider = MassiveClient(
                    settings.massive_api_key,
                    base_url=settings.massive_base_url,
                    requests_per_minute=settings.requests_per_minute,
                    timeout_seconds=settings.http_timeout_seconds,
                )
                with st.spinner("Fetching full-market snapshot from Massive..."):
                    snapshots = provider.full_market_snapshot()
                universe_symbols = database.ensure_symbols(snapshot.symbol for snapshot in snapshots)

                scope_symbols = _snapshot_scope_symbols(
                    scope=snapshot_scope,
                    selected_lists=snapshot_lists,
                    typed_symbols=snapshot_typed_symbols,
                )
                filtered = [
                    snapshot
                    for snapshot in snapshots
                    if (scope_symbols is None or snapshot.symbol in scope_symbols)
                    and float(snapshot.price or 0) >= float(min_price)
                    and float(snapshot.day_volume or 0) >= float(min_day_volume)
                    and _snapshot_dollar_volume(snapshot) >= float(min_dollar_volume)
                ]
                filtered.sort(key=_snapshot_dollar_volume, reverse=True)
                selected_snapshots = filtered[: int(max_store)]

                stored = database.upsert_market_snapshots(selected_snapshots)
                st.cache_data.clear()
                duration = time.monotonic() - started
                database.finish_service_run(
                    service_run_id,
                    status="success",
                    processed_count=len(snapshots),
                    success_count=stored,
                    skipped_count=max(len(snapshots) - stored, 0),
                    duration_seconds=duration,
                    message=f"universe_symbols={universe_symbols}, matched={len(filtered)}, stored={stored}",
                )
                st.success(
                    "Market snapshot complete: "
                    f"fetched={len(snapshots):,}, universe_symbols={universe_symbols:,}, matched={len(filtered):,}, "
                    f"stored={stored:,}, duration={duration:.1f}s"
                )
                if selected_snapshots:
                    st.markdown("#### Stored snapshot preview")
                    snapshot_preview = _snapshot_rows(selected_snapshots, limit=50)
                    st.dataframe(
                        _styled_change_frame(snapshot_preview),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "price": st.column_config.NumberColumn("Last price", format="$%.2f"),
                            "change_pct": st.column_config.NumberColumn("Change %", format="%.2f%%"),
                            "previous_close": st.column_config.NumberColumn("Previous close", format="$%.2f"),
                            "volume": st.column_config.NumberColumn("Volume", format="%.0f"),
                            "dollar_volume": st.column_config.NumberColumn("Dollar volume", format="$%.0f"),
                        },
                    )
            except Exception as exc:
                duration = time.monotonic() - started
                database.finish_service_run(
                    service_run_id,
                    status="failed",
                    duration_seconds=duration,
                    error_count=1,
                    message=str(exc),
                )
                st.cache_data.clear()
                st.error(f"Market snapshot failed: {exc}")
    with st.expander("Data ingestion: historical data", expanded=False):
        st.markdown(
            "Backfill daily historical bars for a selected universe, list, or typed tickers. "
            "Massive aggregate history is fetched with one API call per symbol for the selected date range, "
            "then stored in SQLite for Signal Builder indicators such as MA200 and ADX."
        )
        if not settings.massive_api_key:
            st.warning("MASSIVE_API_KEY is missing. Historical data ingestion requires API access.")

        _render_service_scheduler("historical", "historical data")

        historical_scope_options = ["Stocks universe", "Selected lists", "Typed tickers", "Lists + typed tickers"]
        historical_scope = st.radio(
            "Run historical backfill for",
            historical_scope_options,
            horizontal=True,
            key="historical_scope",
            index=_option_index(
                historical_scope_options,
                _app_setting("services.historical.scope", "Selected lists"),
            ),
            help="For full-universe history, run in chunks. Lists are usually safer on the small Oracle VM.",
        )
        historical_lists: list[str] = []
        if historical_scope in {"Selected lists", "Lists + typed tickers"}:
            saved_historical_lists = _app_setting("services.historical.lists", [])
            historical_lists = st.multiselect(
                "Historical lists",
                list_names,
                key="historical_lists",
                default=[item for item in saved_historical_lists if item in list_names]
                if isinstance(saved_historical_lists, list)
                else [],
                help="The service will backfill the union of all selected list members.",
            )
            if not list_names:
                st.info("No custom lists yet. Create lists in the Lists tab first.")

        historical_typed_symbols = ""
        if historical_scope in {"Typed tickers", "Lists + typed tickers"}:
            historical_typed_symbols = st.text_input(
                "Historical tickers",
                placeholder="AAPL, MSFT, NVDA",
                key="historical_tickers",
                value=str(_app_setting("services.historical.typed_symbols", "")),
                help="Comma-separated tickers. These are added to the selected scope.",
            )

        history_mode_options = ["Only incomplete history", "Refresh selected range"]
        history_mode = st.radio(
            "Historical mode",
            history_mode_options,
            horizontal=True,
            key="historical_mode",
            index=_option_index(
                history_mode_options,
                _app_setting("services.historical.mode", "Only incomplete history"),
            ),
            help="Incomplete mode skips symbols that already have enough daily bars in the selected range.",
        )
        option_cols = st.columns(6)
        years = option_cols[0].number_input(
            "Years",
            min_value=1,
            max_value=5,
            value=int(_app_setting("services.historical.years", 1)),
            step=1,
            key="historical_years",
            help="Massive Stock Starter includes up to 5 years of historical data.",
        )
        chunk_size = option_cols[1].number_input(
            "Chunk size",
            min_value=1,
            max_value=1000,
            value=int(_app_setting("services.historical.chunk_size", 50)),
            step=5,
            key="historical_chunk_size",
            help="Progress grouping size. The run can automatically continue across multiple chunks.",
        )
        chunks_to_run = option_cols[2].number_input(
            "Chunks to run",
            min_value=1,
            max_value=10000,
            value=int(_app_setting("services.historical.chunks_to_run", 1)),
            step=1,
            key="historical_chunks_to_run",
            help="How many chunks to process after clicking Run. Increase this to continue automatically.",
        )
        requests_per_minute = option_cols[3].number_input(
            "Requests/minute",
            min_value=1,
            max_value=1000,
            value=int(_app_setting("services.historical.requests_per_minute", settings.profile_requests_per_minute)),
            step=10,
            key="historical_requests_per_minute",
            help="Aggregate-history request pace. Your API plan and VM/network still apply.",
        )
        preview_rows = option_cols[4].number_input(
            "Preview rows",
            min_value=5,
            max_value=500,
            value=int(_app_setting("services.historical.preview_rows", 50)),
            step=5,
            key="historical_preview_rows",
        )
        coverage_threshold_pct = option_cols[5].number_input(
            "Complete at %",
            min_value=50,
            max_value=100,
            value=int(_app_setting("services.historical.coverage_threshold_pct", 90)),
            step=5,
            key="historical_coverage_threshold_pct",
            help="A symbol is considered complete when stored bars reach this percentage of expected trading days.",
        )
        run_all_remaining = st.checkbox(
            "Run all remaining chunks automatically",
            value=bool(_app_setting("services.historical.run_all_remaining", False)),
            key="historical_run_all_remaining",
            help="If checked, one click processes every remaining symbol in the selected scope. This can take a long time for thousands of symbols.",
        )

        end_date = date.today()
        start_date = end_date - timedelta(days=int(years) * 365)
        expected_bars = max(int(int(years) * 252), 1)
        complete_bars = max(int(expected_bars * int(coverage_threshold_pct) / 100), 1)

        historical_defaults: dict[str, object] = {
            "services.historical.scope": historical_scope,
            "services.historical.mode": history_mode,
            "services.historical.years": int(years),
            "services.historical.chunk_size": int(chunk_size),
            "services.historical.chunks_to_run": int(chunks_to_run),
            "services.historical.run_all_remaining": bool(run_all_remaining),
            "services.historical.requests_per_minute": int(requests_per_minute),
            "services.historical.preview_rows": int(preview_rows),
            "services.historical.coverage_threshold_pct": int(coverage_threshold_pct),
        }
        if historical_scope in {"Selected lists", "Lists + typed tickers"}:
            historical_defaults["services.historical.lists"] = historical_lists
        if historical_scope in {"Typed tickers", "Lists + typed tickers"}:
            historical_defaults["services.historical.typed_symbols"] = historical_typed_symbols
        _save_app_settings(historical_defaults)

        scope_symbols = _profile_scope_symbols(
            scope=historical_scope,
            selected_lists=historical_lists,
            typed_symbols=historical_typed_symbols,
        )
        bar_counts = _historical_bar_counts(scope_symbols, start_date, end_date)
        complete_symbols = {symbol for symbol, count in bar_counts.items() if count >= complete_bars}
        pending_symbols = (
            [symbol for symbol in scope_symbols if symbol not in complete_symbols]
            if history_mode == "Only incomplete history"
            else scope_symbols
        )
        chunks_remaining = (len(pending_symbols) + int(chunk_size) - 1) // max(int(chunk_size), 1)
        run_chunk_count = chunks_remaining if run_all_remaining else min(int(chunks_to_run), chunks_remaining)
        run_symbol_limit = int(chunk_size) * max(run_chunk_count, 0)
        symbols_to_run = pending_symbols[:run_symbol_limit]

        total_estimated_calls = len(pending_symbols)
        estimated_minutes_total = total_estimated_calls / max(int(requests_per_minute), 1)
        estimated_minutes_run = len(symbols_to_run) / max(int(requests_per_minute), 1)

        metric_cols = st.columns(5)
        metric_cols[0].metric("Scope symbols", f"{len(scope_symbols):,}")
        metric_cols[1].metric("Complete", f"{len(complete_symbols):,}")
        metric_cols[2].metric("Remaining for mode", f"{len(pending_symbols):,}")
        metric_cols[3].metric("This run", f"{len(symbols_to_run):,}")
        metric_cols[4].metric("Chunks left", f"{chunks_remaining:,}")

        if scope_symbols:
            coverage_pct = 100.0 * len(complete_symbols) / max(len(scope_symbols), 1)
            st.progress(min(coverage_pct / 100.0, 1.0), text=f"Historical coverage: {coverage_pct:.1f}% for {int(years)} year(s)")

        st.caption(
            f"Date range: {start_date.isoformat()} → {end_date.isoformat()}. "
            f"Expected daily bars/symbol: ~{expected_bars:,}; complete threshold: {complete_bars:,}. "
            f"Estimated API calls remaining: {total_estimated_calls:,}. "
            f"This run will process {len(symbols_to_run):,} symbol(s) across {run_chunk_count:,} chunk(s). "
            f"Estimated API time: {estimated_minutes_total:.1f} min total, "
            f"{estimated_minutes_run:.1f} min for this run before network/API overhead."
        )

        preview_symbols = pending_symbols[: int(preview_rows)]
        if preview_symbols:
            st.markdown("#### Upcoming historical symbols")
            st.dataframe(
                _historical_progress_frame(preview_symbols, start_date, end_date, expected_bars),
                use_container_width=True,
                hide_index=True,
                column_config={"coverage_pct": st.column_config.NumberColumn("Coverage %", format="%.1f%%")},
            )
        elif scope_symbols:
            st.success("No pending symbols for the selected historical mode and range.")
        else:
            st.info("Select a scope with at least one symbol.")

        if st.button(
            "Run historical backfill",
            type="primary",
            use_container_width=True,
            disabled=not bool(settings.massive_api_key and symbols_to_run),
        ):
            service_scope = (
                f"{historical_scope}; mode={history_mode}; years={years}; "
                f"chunk_size={chunk_size}; chunks_to_run={run_chunk_count}; requests_per_minute={requests_per_minute}; "
                f"range={start_date.isoformat()}..{end_date.isoformat()}"
            )
            service_run_id = database.start_service_run(
                "historical_data",
                scope=service_scope,
                requested_count=len(symbols_to_run),
            )
            try:
                result = _sync_historical_symbols(
                    symbols_to_run,
                    start=start_date,
                    end=end_date,
                    requests_per_minute=int(requests_per_minute),
                    chunk_size=int(chunk_size),
                )
                errors = result["errors"]
                status = "partial" if errors else "success"
                database.finish_service_run(
                    service_run_id,
                    status=status,
                    processed_count=len(symbols_to_run),
                    success_count=int(result["symbols_success"]),
                    skipped_count=0,
                    error_count=len(errors),
                    duration_seconds=float(result["duration"]),
                    message=f"bars_written={result['bars_written']}, chunks={result.get('chunks_processed', 0)}, errors={len(errors)}",
                )
                st.success(
                    "Historical chunk complete: "
                    f"symbols={result['symbols_success']}/{len(symbols_to_run)}, "
                    f"chunks={result.get('chunks_processed', 0)}, bars_written={result['bars_written']:,}, errors={len(errors)}, "
                    f"duration={float(result['duration']):.1f}s"
                )
                if errors:
                    st.error("Some symbols failed. They can be retried in a later chunk.")
                    st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)
            except Exception as exc:
                database.finish_service_run(
                    service_run_id,
                    status="failed",
                    error_count=1,
                    message=str(exc),
                )
                st.cache_data.clear()
                st.error(f"Historical chunk failed: {exc}")

    with st.expander("Data ingestion: company profiles", expanded=False):
        st.markdown(
            "Populate or refresh company profile metadata such as company name, SIC/sector-style description, "
            "market cap, homepage, and description. For large universes, run this in chunks."
        )
        if not settings.massive_api_key:
            st.warning("MASSIVE_API_KEY is missing. Profile ingestion requires API access.")

        _render_service_scheduler("profiles", "company profiles")

        profile_scope_options = ["Stocks universe", "Selected lists", "Typed tickers", "Lists + typed tickers"]
        scope = st.radio(
            "Run service for",
            profile_scope_options,
            horizontal=True,
            key="profile_scope",
            index=_option_index(
                profile_scope_options,
                _app_setting("services.profiles.scope", "Stocks universe"),
            ),
        )
        selected_lists: list[str] = []
        if scope in {"Selected lists", "Lists + typed tickers"}:
            saved_profile_lists = _app_setting("services.profiles.lists", [])
            selected_lists = st.multiselect(
                "Lists",
                list_names,
                key="profile_lists",
                default=[item for item in saved_profile_lists if item in list_names]
                if isinstance(saved_profile_lists, list)
                else [],
                help="The service will run for the union of all selected list members.",
            )
            if not list_names:
                st.info("No custom lists yet. Create lists in the Lists tab first.")

        typed_symbols = ""
        if scope in {"Typed tickers", "Lists + typed tickers"}:
            typed_symbols = st.text_input(
                "Tickers",
                placeholder="AAPL, MSFT, NVDA",
                key="profile_tickers",
                value=str(_app_setting("services.profiles.typed_symbols", "")),
                help="Comma-separated tickers. These are added to the selected scope.",
            )

        profile_mode_options = ["Only missing profiles", "Refresh all selected profiles"]
        mode = st.radio(
            "Profile mode",
            profile_mode_options,
            horizontal=True,
            key="profile_mode",
            index=_option_index(
                profile_mode_options,
                _app_setting("services.profiles.mode", "Only missing profiles"),
            ),
            help="Missing mode skips symbols already present in company_profiles, including unavailable placeholders.",
        )
        option_cols = st.columns(3)
        chunk_size = option_cols[0].number_input(
            "Chunk size",
            min_value=1,
            max_value=500,
            value=int(_app_setting("services.profiles.chunk_size", 25)),
            step=5,
            key="profile_chunk_size",
            help="Maximum symbols to process per click. Keep this small for dashboard safety.",
        )
        requests_per_minute = option_cols[1].number_input(
            "Requests/minute",
            min_value=1,
            max_value=1000,
            value=int(_app_setting("services.profiles.requests_per_minute", settings.profile_requests_per_minute)),
            step=10,
            key="profile_requests_per_minute",
            help="Massive ticker-overview request pace. Your plan limits still apply.",
        )
        show_preview_limit = option_cols[2].number_input(
            "Preview rows",
            min_value=5,
            max_value=500,
            value=int(_app_setting("services.profiles.preview_rows", 50)),
            step=5,
            key="profile_preview_rows",
        )
        run_all_remaining = st.checkbox(
            "Run all remaining chunks automatically",
            value=bool(_app_setting("services.profiles.run_all_remaining", False)),
            key="profile_run_all_remaining",
            help=(
                "If enabled, the next run processes every remaining profile chunk for the selected scope/mode. "
                "If disabled, it processes one chunk at a time."
            ),
        )
        profile_defaults: dict[str, object] = {
            "services.profiles.scope": scope,
            "services.profiles.mode": mode,
            "services.profiles.chunk_size": int(chunk_size),
            "services.profiles.run_all_remaining": bool(run_all_remaining),
            "services.profiles.requests_per_minute": int(requests_per_minute),
            "services.profiles.preview_rows": int(show_preview_limit),
        }
        if scope in {"Selected lists", "Lists + typed tickers"}:
            profile_defaults["services.profiles.lists"] = selected_lists
        if scope in {"Typed tickers", "Lists + typed tickers"}:
            profile_defaults["services.profiles.typed_symbols"] = typed_symbols
        _save_app_settings(profile_defaults)

        scope_symbols = _profile_scope_symbols(
            scope=scope,
            selected_lists=selected_lists,
            typed_symbols=typed_symbols,
        )
        profiled = _symbols_with_profiles()
        pending_symbols = (
            [symbol for symbol in scope_symbols if symbol not in profiled]
            if mode == "Only missing profiles"
            else scope_symbols
        )
        chunks_remaining = (len(pending_symbols) + int(chunk_size) - 1) // max(int(chunk_size), 1)
        run_chunk_count = chunks_remaining if run_all_remaining else min(1, chunks_remaining)
        symbols_to_run = pending_symbols[: int(chunk_size) * max(run_chunk_count, 0)]

        metric_cols = st.columns(5)
        metric_cols[0].metric("Scope symbols", f"{len(scope_symbols):,}")
        metric_cols[1].metric("Already profiled", f"{len(set(scope_symbols) & profiled):,}")
        metric_cols[2].metric("Remaining for mode", f"{len(pending_symbols):,}")
        metric_cols[3].metric("This run", f"{len(symbols_to_run):,}")
        metric_cols[4].metric("Chunks left", f"{chunks_remaining:,}")

        if scope_symbols:
            coverage_pct = 100.0 * len(set(scope_symbols) & profiled) / max(len(scope_symbols), 1)
            st.progress(min(coverage_pct / 100.0, 1.0), text=f"Profile coverage: {coverage_pct:.1f}%")

        if pending_symbols:
            estimated_minutes_total = len(pending_symbols) / max(int(requests_per_minute), 1)
            estimated_minutes_run = len(symbols_to_run) / max(int(requests_per_minute), 1)
            st.caption(
                f"Estimated API time at {int(requests_per_minute):,} requests/min: "
                f"{estimated_minutes_total:.1f} min total remaining, "
                f"{estimated_minutes_run:.1f} min for this run before network/API overhead. "
                f"This run will process {len(symbols_to_run):,} symbol(s) across {run_chunk_count:,} chunk(s)."
            )

        preview_symbols = pending_symbols[: int(show_preview_limit)]
        if preview_symbols:
            st.markdown("#### Upcoming symbols")
            st.dataframe(
                _profile_progress_frame(preview_symbols),
                use_container_width=True,
                hide_index=True,
                column_config={"market_cap": st.column_config.NumberColumn("Market cap", format="$%.0f")},
            )
        elif scope_symbols:
            st.success("No pending symbols for the selected mode.")
        else:
            st.info("Select a scope with at least one symbol.")

        if st.button(
            "Run profile ingestion",
            type="primary",
            use_container_width=True,
            disabled=not bool(settings.massive_api_key and symbols_to_run),
        ):
            service_scope = (
                f"{scope}; mode={mode}; chunk_size={chunk_size}; "
                f"chunks_to_run={run_chunk_count}; run_all_remaining={run_all_remaining}; "
                f"requests_per_minute={requests_per_minute}"
            )
            service_run_id = database.start_service_run(
                "company_profiles",
                scope=service_scope,
                requested_count=len(symbols_to_run),
            )
            try:
                result = _sync_profile_chunk(symbols_to_run, requests_per_minute=int(requests_per_minute))
                errors = result["errors"]
                status = "partial" if errors else "success"
                database.finish_service_run(
                    service_run_id,
                    status=status,
                    processed_count=len(symbols_to_run),
                    success_count=int(result["fetched"]),
                    skipped_count=int(result["unavailable"]),
                    error_count=len(errors),
                    duration_seconds=float(result["duration"]),
                    message=(
                        f"fetched={result['fetched']}, unavailable={result['unavailable']}, "
                        f"chunks={run_chunk_count}, errors={len(errors)}"
                    ),
                )
                st.success(
                    "Profile ingestion complete: "
                    f"fetched={result['fetched']}, unavailable={result['unavailable']}, "
                    f"chunks={run_chunk_count}, errors={len(errors)}, duration={float(result['duration']):.1f}s"
                )
                if errors:
                    st.error("Some symbols failed. They were not marked unavailable and can be retried.")
                    st.dataframe(_format_timestamps(pd.DataFrame(errors)), use_container_width=True, hide_index=True)
            except Exception as exc:
                database.finish_service_run(
                    service_run_id,
                    status="failed",
                    error_count=1,
                    message=str(exc),
                )
                st.cache_data.clear()
                st.error(f"Profile chunk failed: {exc}")


def _set_main_navigation(page: str) -> None:
    st.session_state.main_navigation = page


page_options = ["Market data", "Lists", "Signal Builder", "Services", "Latest runs"]
if st.session_state.get("main_navigation") not in page_options:
    st.session_state.main_navigation = "Market data"

st.markdown(
    """
    <style>
    div[data-testid="stHorizontalBlock"] div.stButton > button {
        min-height: 2.25rem;
        padding: 0.25rem 0.75rem;
        border-radius: 0.6rem;
    }
    div.stButton > button[kind="primary"] {
        background: #0f766e;
        border-color: #0f766e;
        color: white;
    }
    div.stButton > button[kind="primary"]:hover {
        background: #0d9488;
        border-color: #0d9488;
        color: white;
    }
    div[data-baseweb="tag"] {
        background-color: #dbeafe;
        color: #1e3a8a;
    }
    .st-key-delete_signal_button button {
        background: #dc2626 !important;
        border-color: #dc2626 !important;
        color: white !important;
    }
    .st-key-delete_signal_button button:hover {
        background: #b91c1c !important;
        border-color: #b91c1c !important;
        color: white !important;
    }
    .st-key-cancel_preview_button button {
        background: #dc2626 !important;
        border-color: #dc2626 !important;
        color: white !important;
    }
    .st-key-cancel_preview_button button:hover {
        background: #b91c1c !important;
        border-color: #b91c1c !important;
        color: white !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
nav_columns = st.columns(len(page_options), gap="small")
for index, option in enumerate(page_options):
    button_type = "primary" if st.session_state.main_navigation == option else "secondary"
    with nav_columns[index]:
        st.button(
            option,
            key=f"main_nav_{index}",
            use_container_width=True,
            type=button_type,
            on_click=_set_main_navigation,
            args=(option,),
        )
selected_page = st.session_state.main_navigation

if selected_page == "Market data":
    _render_stock_lookup()
    selected_view = _select_market_view()
    view_title = "Stocks universe"
    view_caption = "General list of active symbols."
    requested_market_symbols: set[str] | None = None
    member_count = int(selected_view.get("symbol_count") or 0)
    if selected_view["kind"] == "list" and selected_view["list_id"] is not None:
        list_id = int(selected_view["list_id"])
        requested_market_symbols = set(database.symbols_in_list(list_id))
        member_count = len(requested_market_symbols)
        view_title = f"List: {selected_view['label']}"

    latest = _latest_watchlist(requested_market_symbols)
    visible_latest = latest
    if selected_view["kind"] == "list" and selected_view["list_id"] is not None:
        view_caption = f"{member_count:,} saved stock(s); {len(visible_latest):,} currently visible with market data."
    else:
        view_caption = f"General list of active symbols: {member_count:,} stock(s); {len(visible_latest):,} currently visible with market data."

    if latest.empty and requested_market_symbols is None:
        st.warning("No database data yet. Run `stock-notifier fetch-daily` or a backfill first.")
    else:
        available = visible_latest.loc[visible_latest["close"].notna(), "ticker"].tolist() if not visible_latest.empty else []
        if "market_selected_symbol" not in st.session_state or (
            available and st.session_state.market_selected_symbol not in available
        ):
            st.session_state.market_selected_symbol = available[0] if available else None
        selected = st.session_state.market_selected_symbol

        st.subheader(view_title)
        st.caption(view_caption)

        detail_symbol = str(st.session_state.get("market_detail_symbol") or "").upper().strip()
        if detail_symbol:
            _render_stock_detail(detail_symbol)

        if visible_latest.empty:
            st.info("No symbols to show for this view yet. Add symbols in the Lists tab or refresh market data.")
        else:
            st.caption("Tip: click/select a row to open its stock details. Use the search above for symbols outside the selected view.")
            market_display = _display_market_data_frame(visible_latest)
            dataframe_kwargs = {
                "use_container_width": True,
                "hide_index": True,
                "column_config": {
                    "Ticker": st.column_config.TextColumn(
                        "Ticker",
                        help="Select a row to open stock details.",
                    ),
                    "Close": st.column_config.NumberColumn(format="$%.2f"),
                    "Previous close": st.column_config.NumberColumn(format="$%.2f"),
                    "Volume": st.column_config.NumberColumn(format="%.0f"),
                    "Daily change %": st.column_config.NumberColumn(format="%.2f%%"),
                    "Dollar volume": st.column_config.NumberColumn(format="$%.0f"),
                    "Market cap": st.column_config.NumberColumn(format="$%.0f"),
                },
            }
            try:
                table_event = st.dataframe(
                    _styled_change_frame(market_display),
                    on_select="rerun",
                    selection_mode="single-row",
                    key="market_data_table",
                    **dataframe_kwargs,
                )
                selection = getattr(table_event, "selection", None)
                if selection is None and isinstance(table_event, dict):
                    selection = table_event.get("selection", {})
                selected_rows = (
                    list(selection.get("rows", []) or [])
                    if isinstance(selection, dict)
                    else list(getattr(selection, "rows", []) or [])
                )
                if selected_rows:
                    selected_ticker = str(market_display.iloc[int(selected_rows[0])]["Ticker"])
                    if selected_ticker and selected_ticker != st.session_state.get("market_detail_symbol"):
                        st.session_state.market_detail_symbol = selected_ticker
                        st.session_state.market_selected_symbol = selected_ticker
                        st.rerun()
            except TypeError:
                st.dataframe(_styled_change_frame(market_display), **dataframe_kwargs)

        if not available and not visible_latest.empty:
            st.info("No chartable symbols found in the selected view.")

if selected_page == "Lists":
    _render_lists_tab()

if selected_page == "Signal Builder":
    _render_signal_builder()

if selected_page == "Services":
    _render_services()

if selected_page == "Latest runs":
    st.subheader("Latest runs")
    st.caption("Times are shown in US Eastern time.")

    st.markdown("#### Notifications sent")
    notification_runs = read_frame(
        """
        SELECT d.created_at, d.channel_type, d.status, d.alert_id,
               COALESCE(a.direction, '') AS direction,
               COALESCE(a.symbol, '') AS symbol,
               COALESCE(a.signal_name, '') AS signal_name,
               d.error_text, d.request_json, d.response_json
        FROM notification_deliveries d
        LEFT JOIN alerts a ON a.id=d.alert_id
        ORDER BY d.id DESC
        LIMIT 30
        """
    )
    if notification_runs.empty:
        st.info("No Telegram notifications recorded yet.")
    else:
        st.dataframe(_display_notification_deliveries(notification_runs), use_container_width=True, hide_index=True)

    all_runs = read_frame(
        """
        SELECT started_at, finished_at, 'fetch' AS run_group, run_type AS run_name,
               status, symbols_requested AS requested_count, bars_written AS result_count,
               errors AS error_count, NULL AS duration_seconds, message
        FROM fetch_log
        UNION ALL
        SELECT started_at, finished_at, 'signal' AS run_group, signal_name AS run_name,
               status, NULL AS requested_count, symbols_scored AS result_count,
               errors AS error_count, NULL AS duration_seconds, message
        FROM signal_runs
        UNION ALL
        SELECT started_at, finished_at, 'service' AS run_group, service_name AS run_name,
               status, requested_count, success_count AS result_count,
               error_count, duration_seconds, message
        FROM service_runs
        UNION ALL
        SELECT started_at, finished_at, 'scan_cycle' AS run_group, 'run-scan-cycle' AS run_name,
               status, snapshots_fetched AS requested_count, symbols_scored AS result_count,
               0 AS error_count, duration_seconds, message
        FROM scan_cycle_runs
        ORDER BY started_at DESC
        LIMIT 30
        """
    )
    if all_runs.empty:
        st.info("No runs logged yet.")
    else:
        st.dataframe(_display_history_frame(all_runs), use_container_width=True, hide_index=True)

    st.subheader("Fetch run history")
    logs = read_frame(
        """
        SELECT started_at, finished_at, run_type, status, requested_date,
               symbols_requested, bars_written, errors, message
        FROM fetch_log ORDER BY id DESC LIMIT 10
        """
    )
    if logs.empty:
        st.info("No fetch runs logged yet.")
    else:
        st.dataframe(_display_history_frame(logs), use_container_width=True, hide_index=True)

    st.subheader("Signal run history")
    signal_runs = read_frame(
        """
        SELECT started_at, finished_at, signal_name, status, symbols_scored, errors, message
        FROM signal_runs ORDER BY id DESC LIMIT 20
        """
    )
    if signal_runs.empty:
        st.info("No signal runs logged yet.")
    else:
        st.dataframe(_display_history_frame(signal_runs), use_container_width=True, hide_index=True)

    st.subheader("Service run history")
    service_runs = read_frame(
        """
        SELECT service_name, scope, started_at, finished_at, status,
               requested_count, processed_count, success_count,
               skipped_count, error_count, duration_seconds, message
        FROM service_runs ORDER BY id DESC LIMIT 20
        """
    )
    if service_runs.empty:
        st.info("No service runs logged yet.")
    else:
        st.dataframe(_display_history_frame(service_runs), use_container_width=True, hide_index=True)

    st.subheader("Scan cycle history")
    scan_cycles = read_frame(
        """
        SELECT started_at, finished_at, status, snapshots_fetched,
               symbols_filtered, symbols_scored, alerts_created,
               deliveries_attempted, delivered, duration_seconds, message
        FROM scan_cycle_runs ORDER BY id DESC LIMIT 20
        """
    )
    if scan_cycles.empty:
        st.info("No intraday scan cycles logged yet.")
    else:
        st.dataframe(_display_history_frame(scan_cycles), use_container_width=True, hide_index=True)
