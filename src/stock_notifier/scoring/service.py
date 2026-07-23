from __future__ import annotations

import pandas as pd

from stock_notifier.db import Database
from stock_notifier.scoring.engine import ScoredSymbol, SignalDefinition, evaluate_signal
from stock_notifier.scoring.templates import starter_signal_definitions


def seed_starter_signals(database: Database) -> int:
    count = 0
    for name, config in starter_signal_definitions():
        database.upsert_signal_definition(name, config, enabled=True)
        count += 1
    return count


def _history_frames(history: dict[str, list[object]]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol, rows in history.items():
        records = [dict(row) for row in rows]
        if records:
            frames[symbol] = pd.DataFrame.from_records(records)
    return frames


def required_history_bars(config: dict, *, minimum: int = 30, maximum: int = 320) -> int:
    """Infer the smallest practical daily-bar window needed by a signal config."""
    required = int(minimum)
    for component in list(dict(config or {}).get("components") or []):
        component = dict(component or {})
        component_type = str(component.get("type") or "").strip()
        params = dict(component.get("params") or {})
        if component_type in {"price_vs_sma", "price_vs_ema", "volume_ratio"}:
            required = max(required, int(params.get("period") or component.get("period") or 20))
        elif component_type in {"sma_crossover", "ema_crossover"}:
            fast = int(params.get("fast_period") or component.get("fast_period") or 5)
            slow = int(params.get("slow_period") or component.get("slow_period") or 20)
            required = max(required, fast, slow)
        elif component_type == "adx":
            period = int(params.get("period") or component.get("period") or 14)
            required = max(required, period * 3)
        elif component_type == "price_change_pct":
            days = int(params.get("days") or component.get("days") or 5)
            required = max(required, days + 1)
        elif component_type in {"latest_volume", "dollar_volume", "price_target"}:
            required = max(required, 1)
    # A small buffer helps EMA/ADX stabilize and prevents exact-boundary surprises.
    return max(1, min(int(required) + 5, int(maximum)))


def _uses_price_targets(config: dict) -> bool:
    return any(
        str(dict(component or {}).get("type") or "").strip() == "price_target"
        for component in list(dict(config or {}).get("components") or [])
    )


def score_signal(
    database: Database,
    signal_row: dict,
    symbols: set[str] | None = None,
    *,
    store: bool = True,
    include_latest_snapshot: bool = False,
) -> list[ScoredSymbol]:
    config = dict(signal_row["config"])
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
    configured_list_symbols = database.symbols_for_list_names(configured_lists)
    configured_universe = configured_symbols | configured_list_symbols
    if symbols is None:
        if universe.get("mode") == "selected" and configured_universe:
            symbols = configured_universe
        else:
            symbols = set(database.active_symbols())
    if configured_universe and universe.get("mode") == "selected":
        symbols = set(symbols) & configured_universe

    history = database.load_price_history(
        symbols,
        min_bars=required_history_bars(config),
        include_latest_snapshot=include_latest_snapshot,
    )
    price_targets = database.price_targets_for_symbols(history.keys()) if _uses_price_targets(config) else {}
    definition = SignalDefinition(
        name=str(signal_row["name"]),
        config=config,
        signal_id=int(signal_row["id"]),
        enabled=bool(signal_row["enabled"]),
    )
    results = evaluate_signal(definition, _history_frames(history), price_targets)

    if store:
        run_id = database.start_signal_run(definition.signal_id, definition.name)
        try:
            count = database.store_signal_scores(
                run_id=run_id,
                signal_id=definition.signal_id,
                signal_name=definition.name,
                scores=results,
            )
            database.finish_signal_run(
                run_id,
                status="success",
                symbols_scored=count,
                message=f"Scored {count} symbols",
            )
        except Exception as exc:
            database.finish_signal_run(
                run_id,
                status="failed",
                symbols_scored=0,
                errors=1,
                message=str(exc),
            )
            raise

    return results


def score_enabled_signals(
    database: Database,
    symbols: set[str] | None = None,
    *,
    include_latest_snapshot: bool = False,
) -> dict[str, list[ScoredSymbol]]:
    results: dict[str, list[ScoredSymbol]] = {}
    for signal_row in database.list_signal_definitions(enabled_only=True):
        results[str(signal_row["name"])] = score_signal(
            database,
            signal_row,
            symbols=symbols,
            include_latest_snapshot=include_latest_snapshot,
        )
    return results


def _symbols_for_signal(database: Database, signal_row: dict, symbols: set[str] | None) -> set[str]:
    config = dict(signal_row["config"])
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
    if symbols is None:
        selected = set(database.active_symbols())
    else:
        selected = set(symbols)
    if configured_universe and universe.get("mode") == "selected":
        selected = selected & configured_universe
    return selected


def score_enabled_signals_grouped(
    database: Database,
    symbols: set[str] | None = None,
    *,
    include_latest_snapshot: bool = False,
) -> dict[str, list[ScoredSymbol]]:
    """Score enabled signals while loading candidate history once per scan cycle."""
    signal_rows = database.list_signal_definitions(enabled_only=True)
    if not signal_rows:
        return {}

    signal_symbols = {
        int(row["id"]): _symbols_for_signal(database, row, symbols)
        for row in signal_rows
    }
    all_symbols = set().union(*signal_symbols.values()) if signal_symbols else set()
    if not all_symbols:
        return {str(row["name"]): [] for row in signal_rows}

    max_required_bars = max(required_history_bars(dict(row["config"])) for row in signal_rows)
    history = database.load_price_history(
        all_symbols,
        min_bars=max_required_bars,
        include_latest_snapshot=include_latest_snapshot,
    )
    frames = _history_frames(history)

    results: dict[str, list[ScoredSymbol]] = {}
    for signal_row in signal_rows:
        signal_id = int(signal_row["id"])
        definition = SignalDefinition(
            name=str(signal_row["name"]),
            config=dict(signal_row["config"]),
            signal_id=signal_id,
            enabled=bool(signal_row["enabled"]),
        )
        selected_symbols = signal_symbols[signal_id]
        selected_frames = {
            symbol: frame
            for symbol, frame in frames.items()
            if symbol in selected_symbols
        }
        price_targets = database.price_targets_for_symbols(selected_frames.keys()) if _uses_price_targets(definition.config) else {}
        scored = evaluate_signal(definition, selected_frames, price_targets)
        run_id = database.start_signal_run(definition.signal_id, definition.name)
        try:
            count = database.store_signal_scores(
                run_id=run_id,
                signal_id=definition.signal_id,
                signal_name=definition.name,
                scores=scored,
            )
            database.finish_signal_run(
                run_id,
                status="success",
                symbols_scored=count,
                message=f"Scored {count} symbols from grouped scan-cycle history",
            )
        except Exception as exc:
            database.finish_signal_run(
                run_id,
                status="failed",
                symbols_scored=0,
                errors=1,
                message=str(exc),
            )
            raise
        results[definition.name] = scored
    return results
