from __future__ import annotations

import json
import sqlite3

import pandas as pd
import streamlit as st

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.scoring.service import score_signal, seed_starter_signals

st.set_page_config(page_title="Stock Signal Notifier", layout="wide")
settings = Settings.from_env(require_api_key=False)
database = Database(settings.db_path)
database.initialize()


@st.cache_data(ttl=60)
def read_frame(query: str, parameters: tuple[object, ...] = ()) -> pd.DataFrame:
    if not settings.db_path.exists():
        return pd.DataFrame()
    with sqlite3.connect(settings.db_path) as connection:
        return pd.read_sql_query(query, connection, params=parameters)


st.title("Stock Signal Notifier")
st.caption("Phase 2 · stored end-of-day market data + configurable signal scoring")


def _latest_watchlist() -> pd.DataFrame:
    return read_frame(
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


def _component_from_inputs(index: int) -> dict[str, object]:
    columns = st.columns([1.4, 1.3, 0.9, 0.8, 0.8, 0.8, 0.8])
    with columns[0]:
        component_type = st.selectbox(
            "Type",
            [
                "price_vs_sma",
                "price_vs_ema",
                "sma_crossover",
                "ema_crossover",
                "adx",
                "volume_ratio",
                "price_change_pct",
            ],
            key=f"component_type_{index}",
        )
    with columns[1]:
        name = st.text_input("Name", value=f"Component {index + 1}", key=f"component_name_{index}")
    with columns[2]:
        mode = st.selectbox("Mode", ["score", "gate"], key=f"component_mode_{index}")
    with columns[3]:
        operator = st.selectbox("Op", [">=", ">", "<=", "<", "=="], key=f"component_op_{index}")
    with columns[4]:
        threshold = st.number_input("Threshold", value=0.0, key=f"component_threshold_{index}")
    with columns[5]:
        weight = st.number_input("Weight", value=1.0, min_value=0.0, key=f"component_weight_{index}")
    with columns[6]:
        period = st.number_input("Period/days", value=20, min_value=1, step=1, key=f"component_period_{index}")

    score_columns = st.columns([1, 1, 3])
    with score_columns[0]:
        score_min = st.number_input("Score min", value=0.0, key=f"component_score_min_{index}")
    with score_columns[1]:
        score_max = st.number_input("Score max", value=10.0, key=f"component_score_max_{index}")

    params: dict[str, object]
    if component_type in {"sma_crossover", "ema_crossover"}:
        params = {"fast_period": int(period), "slow_period": 50 if int(period) < 50 else 200}
        with score_columns[2]:
            slow = st.number_input(
                "Slow period",
                value=int(params["slow_period"]),
                min_value=1,
                step=1,
                key=f"component_slow_{index}",
            )
        params["slow_period"] = int(slow)
    elif component_type == "price_change_pct":
        params = {"days": int(period)}
    else:
        params = {"period": int(period)}

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


def _render_signal_builder() -> None:
    st.subheader("Signal Builder")
    st.caption("Build weighted/gated technical signals without changing backend code.")

    left, right = st.columns([1, 2])
    with left:
        if st.button("Seed/update starter signals", use_container_width=True):
            count = seed_starter_signals(database)
            st.success(f"Seeded {count} starter signals.")
            st.cache_data.clear()
            st.rerun()

        definitions = database.list_signal_definitions()
        options = ["New signal"] + [f"{item['id']}: {item['name']}" for item in definitions]
        selected = st.selectbox("Saved signal", options)
        selected_row = None
        if selected != "New signal":
            selected_id = int(selected.split(":", 1)[0])
            selected_row = database.get_signal_definition(selected_id)

        if selected_row and st.button("Delete selected signal", type="secondary", use_container_width=True):
            database.delete_signal_definition(int(selected_row["id"]))
            st.cache_data.clear()
            st.rerun()

    default_config = (
        selected_row["config"]
        if selected_row
        else {"description": "", "universe": {"mode": "all", "symbols": []}, "components": []}
    )
    with right:
        signal_name = st.text_input("Signal name", value=selected_row["name"] if selected_row else "")
        enabled = st.checkbox("Enabled", value=bool(selected_row["enabled"]) if selected_row else True)
        description = st.text_input(
            "Description",
            value=str(default_config.get("description") or selected_row.get("description") if selected_row else ""),
        )
        universe_mode = st.radio(
            "Universe",
            ["all", "selected"],
            horizontal=True,
            index=0 if (default_config.get("universe") or {}).get("mode") != "selected" else 1,
        )
        selected_symbols_text = st.text_input(
            "Selected tickers",
            value=", ".join((default_config.get("universe") or {}).get("symbols") or []),
            help="Only used when Universe is selected.",
        )

    st.markdown("#### Component builder")
    component_count = st.number_input("Components", min_value=1, max_value=8, value=3, step=1)
    built_components = [_component_from_inputs(index) for index in range(int(component_count))]

    generated_config = {
        "description": description,
        "universe": {
            "mode": universe_mode,
            "symbols": [
                item.strip().upper() for item in selected_symbols_text.split(",") if item.strip()
            ],
        },
        "components": built_components,
    }
    config_text = st.text_area(
        "Advanced JSON definition",
        value=json.dumps(default_config if selected_row else generated_config, indent=2),
        height=320,
        help="You can directly tweak parameters, thresholds, gates, and weights here.",
    )

    action_left, action_right = st.columns(2)
    with action_left:
        if st.button("Save signal definition", type="primary", use_container_width=True):
            if not signal_name.strip():
                st.error("Signal name is required.")
            else:
                try:
                    parsed_config = json.loads(config_text)
                    database.upsert_signal_definition(
                        signal_name.strip(),
                        parsed_config,
                        enabled=enabled,
                        description=str(parsed_config.get("description") or description),
                    )
                    st.success("Signal saved.")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not save signal: {exc}")
    with action_right:
        if st.button("Preview rankings", use_container_width=True):
            try:
                parsed_config = json.loads(config_text)
                preview_row = {
                    "id": selected_row["id"] if selected_row else 0,
                    "name": signal_name.strip() or "Preview",
                    "config": parsed_config,
                    "enabled": enabled,
                }
                results = score_signal(database, preview_row, store=False)
                preview = pd.DataFrame(
                    [
                        {
                            "symbol": item.symbol,
                            "score": item.score,
                            "eligible": item.eligible,
                            "trading_date": item.trading_date,
                            "close": item.close,
                            "message": item.message,
                        }
                        for item in results
                    ]
                )
                st.dataframe(preview, use_container_width=True, hide_index=True)
                if results:
                    detail_symbol = st.selectbox(
                        "Preview component breakdown",
                        [item.symbol for item in results[:25]],
                    )
                    selected_result = next(item for item in results if item.symbol == detail_symbol)
                    st.dataframe(
                        pd.DataFrame([component.__dict__ for component in selected_result.components]),
                        use_container_width=True,
                        hide_index=True,
                    )
            except Exception as exc:
                st.error(f"Preview failed: {exc}")

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
        st.dataframe(latest_scores, use_container_width=True, hide_index=True)


market_tab, builder_tab, health_tab = st.tabs(["Market data", "Signal Builder", "Pipeline health"])

with market_tab:
    latest = _latest_watchlist()
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

with builder_tab:
    _render_signal_builder()

with health_tab:
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
        st.dataframe(signal_runs, use_container_width=True, hide_index=True)
