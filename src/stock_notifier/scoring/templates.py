from __future__ import annotations

from typing import Any


def _component(
    name: str,
    component_type: str,
    *,
    mode: str = "score",
    weight: float = 1.0,
    threshold: float | None = None,
    operator: str = ">=",
    score_min: float | None = None,
    score_max: float | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "type": component_type,
        "mode": mode,
        "weight": weight,
        "operator": operator,
        "params": params or {},
    }
    if threshold is not None:
        payload["threshold"] = threshold
    if score_min is not None:
        payload["score_min"] = score_min
    if score_max is not None:
        payload["score_max"] = score_max
    return payload


def starter_signal_definitions() -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "MA Momentum",
            {
                "description": "Short-term MA momentum with price confirmation.",
                "universe": {"mode": "all", "symbols": []},
                "components": [
                    _component(
                        "SMA5 above SMA20",
                        "sma_crossover",
                        threshold=0,
                        score_min=-2,
                        score_max=6,
                        weight=2,
                        params={"fast_period": 5, "slow_period": 20},
                    ),
                    _component(
                        "Close above SMA50",
                        "price_vs_sma",
                        threshold=0,
                        score_min=-5,
                        score_max=10,
                        weight=1.5,
                        params={"period": 50},
                    ),
                    _component(
                        "5-day price change",
                        "price_change_pct",
                        score_min=-3,
                        score_max=8,
                        weight=1,
                        params={"days": 5},
                    ),
                ],
            },
        ),
        (
            "Institutional Momentum",
            {
                "description": "Classic trend structure: price above MA50, MA50 above MA200, ADX trend strength.",
                "universe": {"mode": "all", "symbols": []},
                "components": [
                    _component(
                        "Close must be above SMA50",
                        "price_vs_sma",
                        mode="gate",
                        threshold=0,
                        weight=0,
                        params={"period": 50},
                    ),
                    _component(
                        "SMA50 must be above SMA200",
                        "sma_crossover",
                        mode="gate",
                        threshold=0,
                        weight=0,
                        params={"fast_period": 50, "slow_period": 200},
                    ),
                    _component(
                        "ADX14 trend strength",
                        "adx",
                        threshold=25,
                        score_min=15,
                        score_max=45,
                        weight=2,
                        params={"period": 14},
                    ),
                    _component(
                        "Close vs SMA200",
                        "price_vs_sma",
                        threshold=0,
                        score_min=0,
                        score_max=20,
                        weight=1,
                        params={"period": 200},
                    ),
                ],
            },
        ),
        (
            "Volume Breakout",
            {
                "description": "Volume expansion with positive price action.",
                "universe": {"mode": "all", "symbols": []},
                "components": [
                    _component(
                        "Relative volume",
                        "volume_ratio",
                        threshold=2,
                        score_min=1,
                        score_max=5,
                        weight=2,
                        params={"period": 20},
                    ),
                    _component(
                        "1-day price change",
                        "price_change_pct",
                        threshold=0,
                        score_min=0,
                        score_max=8,
                        weight=1,
                        params={"days": 1},
                    ),
                    _component(
                        "5-day price change",
                        "price_change_pct",
                        score_min=-2,
                        score_max=12,
                        weight=1,
                        params={"days": 5},
                    ),
                ],
            },
        ),
        (
            "Trend Quality",
            {
                "description": "Balanced trend score using MA structure and ADX.",
                "universe": {"mode": "all", "symbols": []},
                "components": [
                    _component(
                        "Close above SMA20",
                        "price_vs_sma",
                        threshold=0,
                        score_min=-3,
                        score_max=8,
                        weight=1,
                        params={"period": 20},
                    ),
                    _component(
                        "Close above SMA50",
                        "price_vs_sma",
                        threshold=0,
                        score_min=-5,
                        score_max=12,
                        weight=1,
                        params={"period": 50},
                    ),
                    _component(
                        "SMA50 above SMA200",
                        "sma_crossover",
                        threshold=0,
                        score_min=-5,
                        score_max=10,
                        weight=1,
                        params={"fast_period": 50, "slow_period": 200},
                    ),
                    _component(
                        "ADX14",
                        "adx",
                        score_min=15,
                        score_max=45,
                        weight=1.5,
                        params={"period": 14},
                    ),
                ],
            },
        ),
    ]
