from __future__ import annotations

import json
import sqlite3

import altair as alt
import pandas as pd
import streamlit as st

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.notifications.service import scan_alerts, seed_alert_rules, send_telegram_test
from stock_notifier.notifications.schedule import next_eligible_send_at, parse_schedule
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
st.caption("Configurable signal scoring · full-market snapshots · scheduled Telegram alerts")


def _latest_watchlist() -> pd.DataFrame:
    return read_frame(
        """
        WITH ranked AS (
            SELECT b.*, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
            FROM daily_bars b
        )
        SELECT s.ticker,
               COALESCE(NULLIF(p.name, ''), s.name) AS name,
               s.asset_type,
               p.sic_description,
               p.market_cap,
               current.trading_date,
               current.close, current.volume,
               ROUND(100.0 * (current.close / previous.close - 1.0), 2) AS daily_change_pct
        FROM symbols s
        LEFT JOIN company_profiles p ON p.ticker = s.ticker
        LEFT JOIN ranked current ON current.symbol = s.ticker AND current.rn = 1
        LEFT JOIN ranked previous ON previous.symbol = s.ticker AND previous.rn = 2
        WHERE s.active = 1
        ORDER BY s.ticker
        """
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


def _history_for_range(history: pd.DataFrame, range_label: str) -> pd.DataFrame:
    rows_by_range = {
        "Intraday": 1,
        "5D": 5,
        "1W": 5,
        "1M": 23,
        "3M": 66,
        "6M": 132,
        "1Y": 252,
    }
    row_count = rows_by_range.get(range_label, 66)
    return history.tail(row_count).copy()


def _render_price_volume_chart(history: pd.DataFrame, selected: str) -> None:
    base = alt.Chart(history).encode(x=alt.X("trading_date:T", title="Date"))
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


def _list_member_frame(list_id: int, latest: pd.DataFrame) -> pd.DataFrame:
    members = database.symbols_in_list(list_id)
    if not members:
        return pd.DataFrame()
    columns = [
        column
        for column in ["ticker", "name", "sic_description", "market_cap", "close", "volume", "daily_change_pct"]
        if column in latest.columns
    ]
    member_frame = latest.loc[latest["ticker"].isin(members), columns].copy()
    visible_members = set(member_frame["ticker"].tolist()) if not member_frame.empty and "ticker" in member_frame else set()
    missing_members = sorted(set(members) - visible_members)
    if missing_members:
        member_frame = pd.concat([member_frame, pd.DataFrame({"ticker": missing_members})], ignore_index=True)
    return member_frame.sort_values("ticker") if "ticker" in member_frame.columns else member_frame


def _render_lists_tab() -> None:
    st.subheader("Lists")
    st.caption(
        "Create reusable stock groups such as Portfolio, Potential, or Sector Watch. "
        "These lists can be viewed in Market Data and used as Signal Builder universes."
    )
    latest = _latest_watchlist()
    lists = database.list_symbol_lists()

    create_col, manage_col = st.columns([1, 2])
    with create_col:
        st.markdown("#### Create list")
        with st.form("create_symbol_list"):
            new_list_name = st.text_input("Name", placeholder="Portfolio, Potential, Watchlist A...")
            new_list_description = st.text_input("Description", placeholder="Optional")
            if st.form_submit_button("Create list", use_container_width=True):
                try:
                    database.create_symbol_list(new_list_name, new_list_description)
                    st.success(f"List saved: {new_list_name}")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not create list: {exc}")

        st.markdown("#### Existing lists")
        if lists:
            st.dataframe(pd.DataFrame(lists), use_container_width=True, hide_index=True)
        else:
            st.info("No custom lists yet.")

    with manage_col:
        if not lists:
            st.info("Create a list to start adding symbols.")
            return

        list_options = [f"{item['id']}: {item['name']} ({item['symbol_count']})" for item in lists]
        selected_list_option = st.selectbox("Manage list", list_options)
        selected_list_id = int(selected_list_option.split(":", 1)[0])
        selected_list = next(item for item in lists if int(item["id"]) == selected_list_id)

        st.markdown("#### Edit list")
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
                    "volume": st.column_config.NumberColumn(format="%.0f"),
                    "daily_change_pct": st.column_config.NumberColumn("Change %", format="%.2f%%"),
                    "market_cap": st.column_config.NumberColumn("Market cap", format="$%.0f"),
                },
            )
            remove_symbol = st.selectbox("Remove ticker", database.symbols_in_list(selected_list_id))
            if st.button("Remove selected ticker", use_container_width=True):
                database.remove_symbol_from_list(selected_list_id, remove_symbol)
                st.cache_data.clear()
                st.rerun()

        st.markdown("#### Danger zone")
        if st.button("Delete list", type="secondary", use_container_width=True):
            database.delete_symbol_list(selected_list_id)
            st.cache_data.clear()
            st.rerun()


def _market_view_options() -> list[dict[str, object]]:
    options: list[dict[str, object]] = [{"kind": "universe", "label": "Stocks universe", "list_id": None}]
    options.extend(
        {"kind": "list", "label": str(item["name"]), "list_id": int(item["id"])}
        for item in database.list_symbol_lists()
    )
    return options


def _select_market_view() -> dict[str, object]:
    options = _market_view_options()
    labels = [str(option["label"]) for option in options]
    selected_label = str(st.session_state.get("market_view_label", labels[0]))
    if selected_label not in labels:
        selected_label = labels[0]
        st.session_state.market_view_label = selected_label
    generation = int(st.session_state.get("market_view_generation", 0))

    st.caption("Choose one view:")
    columns = st.columns(min(4, len(options)))
    checked_labels: list[str] = []
    for index, label in enumerate(labels):
        key = f"market_view_checkbox_{generation}_{index}"
        if key not in st.session_state:
            st.session_state[key] = label == selected_label
        with columns[index % len(columns)]:
            checked = st.checkbox(label, key=key)
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
        st.session_state.market_view_generation = generation + 1
        if next_label != selected_label:
            st.rerun()

    return options[labels.index(selected_label)]


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


def _render_signal_builder() -> None:
    st.subheader("Signal Builder")
    st.caption("Build weighted/gated technical signals without changing backend code.")
    _render_signal_builder_help()

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
            help="Use all active symbols, or restrict this signal to selected lists and/or typed tickers.",
        )
        available_lists = [item["name"] for item in database.list_symbol_lists()]
        selected_lists = st.multiselect(
            "Selected lists",
            available_lists,
            default=(default_config.get("universe") or {}).get("lists") or [],
            help="Only used when Universe is selected. Symbols from all selected lists are combined.",
        )
        selected_symbols_text = st.text_input(
            "Extra selected tickers",
            value=", ".join((default_config.get("universe") or {}).get("symbols") or []),
            help="Optional comma-separated tickers. Only used when Universe is selected.",
        )

    st.markdown("#### Component builder")
    default_components = list(default_config.get("components") or [])
    component_count = st.number_input(
        "Components",
        min_value=1,
        max_value=8,
        value=max(1, len(default_components) or 3),
        step=1,
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
        help="Leave this off unless you intentionally want the JSON text to override the visual component fields.",
    )
    with st.expander("Advanced JSON definition", expanded=use_advanced_json):
        config_text = st.text_area(
            "JSON override",
            value=json.dumps(generated_config, indent=2),
            height=320,
            help="Only used when the checkbox above is enabled.",
        )
        st.caption("Tip: if a component is a gate, its weight and score min/max are ignored.")

    action_left, action_right = st.columns(2)
    with action_left:
        if st.button("Save signal definition", type="primary", use_container_width=True):
            if not signal_name.strip():
                st.error("Signal name is required.")
            else:
                try:
                    parsed_config = json.loads(config_text) if use_advanced_json else generated_config
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
                parsed_config = json.loads(config_text) if use_advanced_json else generated_config
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
        if st.button("Run alert scan", use_container_width=True):
            result = scan_alerts(database, settings)
            st.success(
                "Alert scan complete: "
                f"{result.alerts_created} alerts, {result.queued} queued, "
                f"{result.deliveries_attempted} deliveries, "
                f"dry_run={result.dry_run}"
            )
            st.cache_data.clear()

    st.markdown("#### Alert rules")
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
        st.info("No alert rules yet. Seed rules after creating/scoring signals.")
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
        st.dataframe(display_rules, use_container_width=True, hide_index=True)

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
        st.dataframe(pending_alerts, use_container_width=True, hide_index=True)

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
        st.dataframe(alerts, use_container_width=True, hide_index=True)

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
        st.dataframe(deliveries, use_container_width=True, hide_index=True)


market_tab, lists_tab, builder_tab, notifications_tab, health_tab = st.tabs(
    ["Market data", "Lists", "Signal Builder", "Notifications", "Pipeline health"]
)

with market_tab:
    latest = _latest_watchlist()
    if latest.empty:
        st.warning("No database data yet. Run `stock-notifier fetch-daily` or a backfill first.")
    else:
        selected_view = _select_market_view()
        visible_latest = latest
        view_title = "Stocks universe"
        view_caption = "General list of active symbols."
        if selected_view["kind"] == "list" and selected_view["list_id"] is not None:
            list_id = int(selected_view["list_id"])
            members = set(database.symbols_in_list(list_id))
            visible_latest = latest.loc[latest["ticker"].isin(members)].copy()
            view_title = f"List: {selected_view['label']}"
            view_caption = f"{len(members)} saved symbol(s); {len(visible_latest)} currently visible with market data."

        available = visible_latest.loc[visible_latest["close"].notna(), "ticker"].tolist()
        if "market_selected_symbol" not in st.session_state or (
            available and st.session_state.market_selected_symbol not in available
        ):
            st.session_state.market_selected_symbol = available[0] if available else None
        selected = st.session_state.market_selected_symbol

        st.subheader(view_title)
        st.caption(view_caption)
        if visible_latest.empty:
            st.info("No symbols to show for this view yet. Add symbols in the Lists tab or refresh market data.")
        else:
            st.dataframe(
                visible_latest,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "close": st.column_config.NumberColumn(format="$%.2f"),
                    "volume": st.column_config.NumberColumn(format="%.0f"),
                    "daily_change_pct": st.column_config.NumberColumn("Change %", format="%.2f%%"),
                    "market_cap": st.column_config.NumberColumn("Market cap", format="$%.0f"),
                },
            )

        if available:
            st.subheader("Symbol history")
            query = st.text_input(
                "Search symbol or company",
                placeholder="Type a ticker or company name, e.g. NVDA or Nvidia",
            )
            matches = _symbol_matches(visible_latest, query)
            if query.strip() and matches.empty:
                st.warning("No matching symbols found in the selected view.")
            elif not matches.empty:
                st.caption("Closest matches")
                match_columns = st.columns(4)
                for index, row in matches.reset_index(drop=True).iterrows():
                    ticker = str(row["ticker"])
                    name = str(row["name"] or "").strip()
                    label = ticker if not name else f"{ticker} · {name[:28]}"
                    with match_columns[index % len(match_columns)]:
                        if st.button(label, key=f"symbol_match_{ticker}", use_container_width=True):
                            st.session_state.market_selected_symbol = ticker
                            st.rerun()

            selected = st.session_state.market_selected_symbol
            selected_row = visible_latest.loc[visible_latest["ticker"] == selected].head(1)
            if not selected_row.empty:
                company_name = str(selected_row.iloc[0].get("name") or "").strip()
                close = selected_row.iloc[0].get("close")
                change = selected_row.iloc[0].get("daily_change_pct")
                st.markdown(
                    f"#### {selected}"
                    + (f" · {company_name}" if company_name else "")
                    + (f" · ${close:,.2f}" if pd.notna(close) else "")
                    + (f" · {change:+.2f}%" if pd.notna(change) else "")
                )

            range_options = ["Intraday", "5D", "1W", "1M", "3M", "6M", "1Y"]
            selected_range = st.radio(
                "Range",
                range_options,
                horizontal=True,
                index=3,
                label_visibility="collapsed",
            )
            history = read_frame(
                """
                SELECT trading_date, open, high, low, close, volume
                FROM daily_bars WHERE symbol = ? ORDER BY trading_date
                """,
                (selected,),
            )
            history["trading_date"] = pd.to_datetime(history["trading_date"])
            visible_history = _history_for_range(history, selected_range)
            if selected_range == "Intraday":
                st.info("Intraday data is not enabled yet, so this shows the latest available daily bar for now.")
            _render_price_volume_chart(visible_history, selected)
            st.markdown(
                f"[TradingView](https://www.tradingview.com/chart/?symbol={selected}) · "
                f"[Yahoo Finance](https://finance.yahoo.com/quote/{selected})"
            )
        elif not visible_latest.empty:
            st.info("No chartable symbols found in the selected view.")

with lists_tab:
    _render_lists_tab()

with builder_tab:
    _render_signal_builder()

with notifications_tab:
    _render_notifications()

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
        st.dataframe(scan_cycles, use_container_width=True, hide_index=True)
