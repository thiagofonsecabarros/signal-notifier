from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

from stock_notifier.config import Settings

st.set_page_config(page_title="Stock Signal Notifier", layout="wide")
settings = Settings.from_env(require_api_key=False)


@st.cache_data(ttl=60)
def read_frame(query: str, parameters: tuple[object, ...] = ()) -> pd.DataFrame:
    if not settings.db_path.exists():
        return pd.DataFrame()
    with sqlite3.connect(settings.db_path) as connection:
        return pd.read_sql_query(query, connection, params=parameters)


st.title("Stock Signal Notifier")
st.caption("Phase 1 · stored end-of-day market data")

latest = read_frame(
    """
    WITH ranked AS (
        SELECT b.*, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
        FROM daily_bars b
    )
    SELECT s.ticker, s.name, s.asset_type, current.trading_date,
           current.close, current.volume,
           ROUND(100.0 * (current.close / previous.close - 1.0), 2) AS daily_change_pct
    FROM symbols s
    LEFT JOIN ranked current ON current.symbol = s.ticker AND current.rn = 1
    LEFT JOIN ranked previous ON previous.symbol = s.ticker AND previous.rn = 2
    WHERE s.active = 1
    ORDER BY s.ticker
    """
)

if latest.empty:
    st.warning("No database data yet. Run `stock-notifier fetch-daily` or a backfill first.")
else:
    st.subheader("Watchlist")
    st.dataframe(
        latest,
        use_container_width=True,
        hide_index=True,
        column_config={
            "close": st.column_config.NumberColumn(format="$%.2f"),
            "volume": st.column_config.NumberColumn(format="%.0f"),
            "daily_change_pct": st.column_config.NumberColumn("Change %", format="%.2f%%"),
        },
    )

    available = latest.loc[latest["close"].notna(), "ticker"].tolist()
    if available:
        st.subheader("Symbol history")
        selected = st.selectbox("Symbol", available)
        history = read_frame(
            """
            SELECT trading_date, open, high, low, close, volume
            FROM daily_bars WHERE symbol = ? ORDER BY trading_date
            """,
            (selected,),
        )
        history["trading_date"] = pd.to_datetime(history["trading_date"])
        left, right = st.columns([2, 1])
        with left:
            st.line_chart(history, x="trading_date", y="close", x_label="Date", y_label="Close")
        with right:
            st.bar_chart(history, x="trading_date", y="volume", x_label="Date", y_label="Volume")
        st.markdown(
            f"[TradingView](https://www.tradingview.com/chart/?symbol={selected}) · "
            f"[Yahoo Finance](https://finance.yahoo.com/quote/{selected})"
        )

st.subheader("Pipeline health")
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
    st.dataframe(logs, use_container_width=True, hide_index=True)

