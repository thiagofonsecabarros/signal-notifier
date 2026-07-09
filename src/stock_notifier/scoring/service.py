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
        min_bars=320,
        include_latest_snapshot=include_latest_snapshot,
    )
    definition = SignalDefinition(
        name=str(signal_row["name"]),
        config=config,
        signal_id=int(signal_row["id"]),
        enabled=bool(signal_row["enabled"]),
    )
    results = evaluate_signal(definition, _history_frames(history))

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
