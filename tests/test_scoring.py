from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from stock_notifier.db import Database
from stock_notifier.models import DailyBar, PriceTarget, Symbol
from stock_notifier.scoring.engine import SignalDefinition, evaluate_signal
from stock_notifier.scoring.indicators import price_change_pct, sma, volume_ratio
from stock_notifier.scoring.service import score_enabled_signals_grouped, score_signal, seed_starter_signals


def _bars(symbol: str, count: int = 260) -> list[DailyBar]:
    start = date(2025, 1, 1)
    rows: list[DailyBar] = []
    for offset in range(count):
        close = 100.0 + offset
        rows.append(
            DailyBar(
                symbol,
                start + timedelta(days=offset),
                close - 1,
                close + 2,
                close - 2,
                close,
                1_000 + offset,
            )
        )
    return rows


def test_basic_indicator_primitives():
    assert sma([1, 2, 3, 4, 5], 3).iloc[-1] == 4
    assert round(price_change_pct([100, 105, 110], 2).iloc[-1], 2) == 10
    assert round(volume_ratio([100, 100, 300], 3).iloc[-1], 2) == 1.8


def test_signal_engine_applies_gates_and_weights():
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "trading_date": (date(2025, 1, 1) + timedelta(days=offset)).isoformat(),
                "open": 100 + offset,
                "high": 102 + offset,
                "low": 99 + offset,
                "close": 101 + offset,
                "volume": 1_000 + offset,
            }
            for offset in range(80)
        ]
    )
    definition = SignalDefinition(
        "Test Signal",
        {
            "components": [
                {
                    "name": "Close above SMA20",
                    "type": "price_vs_sma",
                    "mode": "gate",
                    "operator": ">",
                    "threshold": 0,
                    "params": {"period": 20},
                },
                {
                    "name": "Five day momentum",
                    "type": "price_change_pct",
                    "mode": "score",
                    "weight": 2,
                    "score_min": 0,
                    "score_max": 5,
                    "params": {"days": 5},
                },
            ]
        },
    )

    result = evaluate_signal(definition, {"AAPL": frame})[0]

    assert result.eligible is True
    assert result.score > 0
    assert len(result.components) == 2


def test_signal_engine_supports_dollar_volume_gate_with_price_change_score():
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "trading_date": (date(2025, 1, 1) + timedelta(days=offset)).isoformat(),
                "open": 100 + offset,
                "high": 102 + offset,
                "low": 99 + offset,
                "close": 100 + offset,
                "volume": 10_000 + offset,
            }
            for offset in range(10)
        ]
    )
    definition = SignalDefinition(
        "Liquidity Momentum",
        {
            "components": [
                {
                    "name": "Dollar volume >= 100k",
                    "type": "dollar_volume",
                    "mode": "gate",
                    "operator": ">=",
                    "threshold": 100_000,
                },
                {
                    "name": "One day price change",
                    "type": "price_change_pct",
                    "mode": "score",
                    "operator": ">=",
                    "threshold": 0,
                    "weight": 1,
                    "score_min": 0,
                    "score_max": 5,
                    "params": {"days": 1},
                },
            ]
        },
    )

    result = evaluate_signal(definition, {"AAPL": frame})[0]

    assert result.eligible is True
    assert result.components[0].value and result.components[0].value > 100_000
    assert result.components[0].mode == "gate"
    assert result.components[0].score == 0
    assert result.components[0].weight == 0
    assert result.components[0].contribution == 0
    assert result.score > 0


def test_signal_engine_scores_price_target_expected_upside_from_current_price():
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "trading_date": "2026-07-01",
                "open": 95,
                "high": 101,
                "low": 94,
                "close": 100,
                "volume": 1_000,
            },
            {
                "symbol": "AAPL",
                "trading_date": "2026-07-02",
                "open": 180,
                "high": 191,
                "low": 179,
                "close": 190,
                "volume": 1_000,
            },
        ]
    )
    definition = SignalDefinition(
        "Target Upside",
        {
            "components": [
                {
                    "name": "Average target upside",
                    "type": "price_target",
                    "mode": "score",
                    "weight": 1,
                    "score_min": 0,
                    "score_max": 10,
                    "params": {"metric": "avg_upside_pct"},
                }
            ]
        },
    )

    result = evaluate_signal(
        definition,
        {"AAPL": frame},
        {
            "AAPL": [
                {
                    "target_price": 200,
                    "price_then": 100,
                    "effective_date": "2026-07-01",
                    "captured_at": "2026-07-01T20:00:00+00:00",
                    "reached_date": "",
                }
            ]
        },
    )[0]

    assert round(result.components[0].value or 0, 2) == 5.26
    assert 50 < result.score < 60


def test_signal_engine_price_target_gate_can_require_unreached_count():
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "trading_date": "2026-07-02",
                "open": 180,
                "high": 191,
                "low": 179,
                "close": 190,
                "volume": 1_000,
            }
        ]
    )
    definition = SignalDefinition(
        "Target Count Gate",
        {
            "components": [
                {
                    "name": "At least two unreached targets",
                    "type": "price_target",
                    "mode": "gate",
                    "operator": ">=",
                    "threshold": 2,
                    "params": {"metric": "unreached_count"},
                },
                {
                    "name": "Overall target strength",
                    "type": "price_target",
                    "mode": "score",
                    "weight": 1,
                    "score_min": 0,
                    "score_max": 100,
                    "params": {"metric": "target_score"},
                },
            ]
        },
    )

    result = evaluate_signal(
        definition,
        {"AAPL": frame},
        {
            "AAPL": [
                {"target_price": 200, "effective_date": "2026-07-01", "captured_at": "2026-07-01T20:00:00+00:00"},
                {"target_price": 210, "effective_date": "2026-07-01", "captured_at": "2026-07-01T20:00:00+00:00"},
                {"target_price": 180, "effective_date": "2026-07-01", "captured_at": "2026-07-01T20:00:00+00:00"},
            ]
        },
    )[0]

    assert result.eligible is True
    assert result.components[0].value == 2
    assert result.components[0].score == 0
    assert result.score > 0


def test_scoring_service_seeds_and_persists_latest_scores(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    database.sync_symbols([Symbol("AAPL", "Apple"), Symbol("MSFT", "Microsoft")])
    database.upsert_bars([*_bars("AAPL"), *_bars("MSFT")])

    assert seed_starter_signals(database) == 4
    definition = database.get_signal_definition("MA Momentum")
    assert definition is not None

    first = score_signal(database, definition)
    second = score_signal(database, definition)

    assert len(first) == 2
    assert len(second) == 2
    latest_rows = database.query(
        "SELECT symbol, score FROM signal_scores WHERE signal_name='MA Momentum' AND is_latest=1"
    )
    component_rows = database.query("SELECT COUNT(*) AS count FROM signal_score_components")
    assert len(latest_rows) == 2
    assert component_rows[0]["count"] > 0


def test_scoring_service_uses_symbol_lists_in_universe(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    database.sync_symbols([Symbol("AAPL", "Apple"), Symbol("MSFT", "Microsoft")])
    database.upsert_bars([*_bars("AAPL"), *_bars("MSFT")])
    list_id = database.create_symbol_list("Portfolio")
    database.add_symbols_to_list(list_id, ["AAPL"])
    signal_id = database.upsert_signal_definition(
        "Portfolio Momentum",
        {
            "universe": {"mode": "selected", "lists": ["Portfolio"], "symbols": []},
            "components": [
                {
                    "name": "One day momentum",
                    "type": "price_change_pct",
                    "mode": "score",
                    "weight": 1,
                    "score_min": 0,
                    "score_max": 5,
                    "params": {"days": 1},
                }
            ],
        },
    )
    definition = database.get_signal_definition(signal_id)
    assert definition is not None

    results = score_signal(database, definition, store=False)

    assert [item.symbol for item in results] == ["AAPL"]


def test_grouped_enabled_scoring_matches_individual_scoring(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    database.sync_symbols([Symbol("AAPL", "Apple"), Symbol("MSFT", "Microsoft")])
    database.upsert_bars([*_bars("AAPL"), *_bars("MSFT")])
    signal_id = database.upsert_signal_definition(
        "One Day Momentum",
        {
            "components": [
                {
                    "name": "One day momentum",
                    "type": "price_change_pct",
                    "mode": "score",
                    "weight": 1,
                    "score_min": 0,
                    "score_max": 5,
                    "params": {"days": 1},
                }
            ]
        },
    )
    definition = database.get_signal_definition(signal_id)
    assert definition is not None

    individual = score_signal(database, definition, store=False)
    grouped = score_enabled_signals_grouped(database, symbols={"AAPL", "MSFT"}, include_latest_snapshot=False)

    assert [item.symbol for item in grouped["One Day Momentum"]] == [item.symbol for item in individual]
    assert [item.score for item in grouped["One Day Momentum"]] == [item.score for item in individual]
