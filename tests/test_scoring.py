from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from stock_notifier.db import Database
from stock_notifier.models import DailyBar, Symbol
from stock_notifier.scoring.engine import SignalDefinition, evaluate_signal
from stock_notifier.scoring.indicators import price_change_pct, sma, volume_ratio
from stock_notifier.scoring.service import score_signal, seed_starter_signals


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
