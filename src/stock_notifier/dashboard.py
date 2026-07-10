from __future__ import annotations

import json
import sqlite3
import time

import altair as alt
import pandas as pd
import streamlit as st

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.notifications.service import scan_alerts, seed_alert_rules, send_telegram_test
from stock_notifier.notifications.schedule import next_eligible_send_at, parse_schedule
from stock_notifier.providers.massive import MassiveClient
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


def _format_timestamp(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return str(value)
    if getattr(timestamp, "tzinfo", None) is not None:
        timestamp = timestamp.tz_convert(None)
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


def _render_signal_builder() -> None:
    st.subheader("Signal Builder")
    flash_message = st.session_state.pop("signal_builder_flash", None)
    if flash_message:
        st.success(str(flash_message))
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
        pending_selection = st.session_state.pop("signal_builder_pending_selection", None)
        force_reload = bool(st.session_state.pop("signal_builder_force_reload", False))
        if pending_selection in options:
            st.session_state.signal_builder_selected = pending_selection
            force_reload = True
        if force_reload:
            st.session_state.signal_builder_loaded_selection = None
        selected = st.selectbox("Saved signal", options, key="signal_builder_selected")
        selected_row = None
        if selected != "New signal":
            selected_id = int(selected.split(":", 1)[0])
            selected_row = database.get_signal_definition(selected_id)
        if st.session_state.get("signal_builder_loaded_selection") != selected:
            _load_signal_builder_form_state(selected_row)
            st.session_state.signal_builder_loaded_selection = selected

        if selected_row and st.button("Delete selected signal", type="secondary", use_container_width=True):
            database.delete_signal_definition(int(selected_row["id"]))
            st.cache_data.clear()
            st.rerun()

    default_config = selected_row["config"] if selected_row else _signal_builder_empty_config()
    with right:
        signal_name = st.text_input(
            "Signal name",
            value=selected_row["name"] if selected_row else "",
            key="signal_builder_name",
        )
        enabled = st.checkbox(
            "Enabled",
            value=bool(selected_row["enabled"]) if selected_row else True,
            key="signal_builder_enabled",
        )
        description = st.text_input(
            "Description",
            value=str(default_config.get("description") or selected_row.get("description") if selected_row else ""),
            key="signal_builder_description",
        )
        universe_mode = st.radio(
            "Universe",
            ["all", "selected"],
            horizontal=True,
            index=0 if (default_config.get("universe") or {}).get("mode") != "selected" else 1,
            key="signal_builder_universe_mode",
            help="Use all active symbols, or restrict this signal to selected lists and/or typed tickers.",
        )
        available_lists = [item["name"] for item in database.list_symbol_lists()]
        selected_lists = st.multiselect(
            "Selected lists",
            available_lists,
            default=(default_config.get("universe") or {}).get("lists") or [],
            key="signal_builder_selected_lists",
            help="Only used when Universe is selected. Symbols from all selected lists are combined.",
        )
        selected_symbols_text = st.text_input(
            "Extra selected tickers",
            value=", ".join((default_config.get("universe") or {}).get("symbols") or []),
            key="signal_builder_selected_symbols",
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

    action_left, action_right = st.columns(2)
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
                    st.session_state.signal_builder_pending_selection = f"{saved_id}: {saved_name}"
                    st.session_state.signal_builder_force_reload = True
                    st.session_state.signal_builder_flash = f"Signal saved: {saved_name}"
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
        st.dataframe(_format_timestamps(latest_scores), use_container_width=True, hide_index=True)


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
            "Max symbols to store",
            min_value=1,
            max_value=20_000,
            value=int(_app_setting("services.snapshot.max_store", 5_000)),
            step=500,
            key="snapshot_max_store",
            help="Keeps the dashboard safe if you only want the most liquid names.",
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
            "Tip: for a true universe foundation, use Stocks universe with low filters. "
            "For signal scans on the E2.Micro, use tighter price/volume/dollar-volume filters."
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

                database.ensure_symbols(snapshot.symbol for snapshot in selected_snapshots)
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
                    message=f"matched={len(filtered)}, stored={stored}",
                )
                st.success(
                    "Market snapshot complete: "
                    f"fetched={len(snapshots):,}, matched={len(filtered):,}, "
                    f"stored={stored:,}, duration={duration:.1f}s"
                )
                if selected_snapshots:
                    st.markdown("#### Stored snapshot preview")
                    st.dataframe(
                        _snapshot_rows(selected_snapshots, limit=50),
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
    with st.expander("Data ingestion: company profiles", expanded=False):
        st.markdown(
            "Populate or refresh company profile metadata such as company name, SIC/sector-style description, "
            "market cap, homepage, and description. For large universes, run this in chunks."
        )
        if not settings.massive_api_key:
            st.warning("MASSIVE_API_KEY is missing. Profile ingestion requires API access.")

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
        profile_defaults: dict[str, object] = {
            "services.profiles.scope": scope,
            "services.profiles.mode": mode,
            "services.profiles.chunk_size": int(chunk_size),
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
        next_chunk = pending_symbols[: int(chunk_size)]

        metric_cols = st.columns(4)
        metric_cols[0].metric("Scope symbols", f"{len(scope_symbols):,}")
        metric_cols[1].metric("Already profiled", f"{len(set(scope_symbols) & profiled):,}")
        metric_cols[2].metric("Remaining for mode", f"{len(pending_symbols):,}")
        metric_cols[3].metric("Next chunk", f"{len(next_chunk):,}")

        if scope_symbols:
            coverage_pct = 100.0 * len(set(scope_symbols) & profiled) / max(len(scope_symbols), 1)
            st.progress(min(coverage_pct / 100.0, 1.0), text=f"Profile coverage: {coverage_pct:.1f}%")

        if pending_symbols:
            estimated_minutes = len(pending_symbols) / max(int(requests_per_minute), 1)
            st.caption(
                f"Estimated API time remaining at {int(requests_per_minute):,} requests/min: "
                f"{estimated_minutes:.1f} minutes before network/API overhead."
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
            "Run next profile chunk",
            type="primary",
            use_container_width=True,
            disabled=not bool(settings.massive_api_key and next_chunk),
        ):
            service_scope = (
                f"{scope}; mode={mode}; chunk_size={chunk_size}; "
                f"requests_per_minute={requests_per_minute}"
            )
            service_run_id = database.start_service_run(
                "company_profiles",
                scope=service_scope,
                requested_count=len(next_chunk),
            )
            try:
                result = _sync_profile_chunk(next_chunk, requests_per_minute=int(requests_per_minute))
                errors = result["errors"]
                status = "partial" if errors else "success"
                database.finish_service_run(
                    service_run_id,
                    status=status,
                    processed_count=len(next_chunk),
                    success_count=int(result["fetched"]),
                    skipped_count=int(result["unavailable"]),
                    error_count=len(errors),
                    duration_seconds=float(result["duration"]),
                    message=f"fetched={result['fetched']}, unavailable={result['unavailable']}, errors={len(errors)}",
                )
                st.success(
                    "Profile chunk complete: "
                    f"fetched={result['fetched']}, unavailable={result['unavailable']}, "
                    f"errors={len(errors)}, duration={float(result['duration']):.1f}s"
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


page_options = ["Market data", "Lists", "Signal Builder", "Notifications", "Services", "Latest runs"]
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
                    "previous_close": st.column_config.NumberColumn("Previous close", format="$%.2f"),
                    "volume": st.column_config.NumberColumn(format="%.0f"),
                    "daily_change_pct": st.column_config.NumberColumn("Change %", format="%.2f%%"),
                    "dollar_volume": st.column_config.NumberColumn("Dollar volume", format="$%.0f"),
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

if selected_page == "Lists":
    _render_lists_tab()

if selected_page == "Signal Builder":
    _render_signal_builder()

if selected_page == "Notifications":
    _render_notifications()

if selected_page == "Services":
    _render_services()

if selected_page == "Latest runs":
    st.subheader("Latest runs")
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
        st.dataframe(_format_timestamps(all_runs), use_container_width=True, hide_index=True)

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
        st.dataframe(_format_timestamps(logs), use_container_width=True, hide_index=True)

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
        st.dataframe(_format_timestamps(signal_runs), use_container_width=True, hide_index=True)

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
        st.dataframe(_format_timestamps(service_runs), use_container_width=True, hide_index=True)

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
        st.dataframe(_format_timestamps(scan_cycles), use_container_width=True, hide_index=True)
